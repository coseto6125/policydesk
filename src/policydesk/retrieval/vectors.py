"""
The semantic channel: local bge-m3, vectors on disk, scored by one matmul.

BM25 cannot bridge a synonym. 換工作 and 職業變更 share no character, so a customer
asking whether a new job affects their cover never reaches 職業或職務變更的通知義務 —
measured on this corpus, the lexical channel returns 保險範圍 instead. The same pair
scores 0.56 under bge-m3 against 0.46 for an unrelated clause, which is the whole reason
this channel exists.

**The model is local.** `bge-m3`, int8 ONNX, on disk, no key and no network. It costs
about 18 ms for a handful of short queries on CPU, which is nothing beside the six
seconds a turn already spends in the language model.

**The vectors are mmap'd, not loaded.** `np.lib.format.open_memmap(..., mode="r")` maps
`vectors.npy` into the address space and lets the page cache own it, so N workers share
one copy of the matrix rather than each holding its own. Stored fp32 rather than fp16
because numpy has no fp16 GEMM kernel — a fp16 matmul falls off BLAS onto an
object-ufunc path around twenty times slower, and halving a 48 MB file is not worth that.

Ported from enoract's `chat/retrieval/vector.py` and `shared/client/embedder.py`, cut to
one corpus and one process.
"""

import asyncio
import os
from functools import partial
from hashlib import sha256
from operator import itemgetter
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.request import Request, urlopen
from uuid import uuid4

import numpy as np
from msgspec import json
from tokenizers import Tokenizer

from policydesk.bootloader import logger
from policydesk.retrieval.base import CLAUSE, STATUTE, Hit

if TYPE_CHECKING:
    from collections.abc import Sequence

    from numpy.typing import NDArray

    from policydesk.core.db import Database

VECTOR_DIR = Path(os.environ.get("POLICYDESK_VECTOR_DIR", "data/vectors"))
"""Beside the BM25 index, and a cache in exactly the same way: rebuilt from Postgres."""

MODEL_DIR = Path(os.environ.get("POLICYDESK_EMBED_MODEL", "/home/enor/enoract/tmp/bge-m3-int8-pkg"))
"""The int8 export. Absent, the channel does not open and the hybrid runs lexical-only."""

MODEL_FILE = "onnx/model_quantized.onnx"
DIM = 1024
MAX_TOKENS = 512
"""Token budget per passage. Longer documents are split, never discarded."""
OVERLAP = 64

BATCH = 16

PROGRESS_EVERY = 512
"""Documents between progress lines during a build. Roughly every ninety seconds."""


class Embedder:
    """
    bge-m3 over onnxruntime, in this process.

    Loaded once — a session costs about a second to construct and the graph is 570 MB on
    disk, so building one per call would put that on the customer's turn.
    """

    def __init__(self, model_dir: Path = MODEL_DIR, *, threads: int = 4) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        self._session = ort.InferenceSession(
            str(model_dir / MODEL_FILE), options, providers=["CPUExecutionProvider"]
        )
        self._tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self._tokenizer.enable_truncation(MAX_TOKENS)
        self._tokenizer.enable_padding()
        # The export exposes a pooled output. Where it does not, CLS-pooling the token
        # embeddings is the correct fallback for the bge family — the mean would be
        # wrong, not merely different.
        self._pooled = next(
            (i for i, out in enumerate(self._session.get_outputs()) if out.name == "sentence_embedding"), None
        )

    def encode(self, texts: Sequence[str], *, progress: int = 0) -> NDArray:
        """
        Embed a batch.

        Args:
            texts: What to embed.
            progress: Log a line every this many documents. 0 stays quiet, which is what
                a query wants; a corpus build passes a figure, because forty minutes with
                no output is indistinguishable from a hang and the first thing anybody
                does about a hang is start a second one.

        Returns:
            An (n, DIM) fp32 array, L2-normalised so a dot product is a cosine.

        """
        if not texts:
            return np.zeros((0, DIM), dtype=np.float32)
        rows: list[NDArray] = []
        for start in range(0, len(texts), BATCH):
            if progress and start and not start % progress:
                logger.info("vectors_encoding", done=start, total=len(texts))
            chunk = [t or " " for t in texts[start : start + BATCH]]
            encoded = self._tokenizer.encode_batch(chunk)
            out = self._session.run(
                None,
                {
                    "input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
                    "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64),
                },
            )
            vectors = out[self._pooled] if self._pooled is not None else out[0][:, 0]
            rows.append(np.asarray(vectors, dtype=np.float32))
        stacked = np.vstack(rows)
        return stacked / np.linalg.norm(stacked, axis=1, keepdims=True).clip(min=1e-12)


class ServerEmbedder:
    """The operator-configured llama-server, used for both documents and queries."""

    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self.model = self._request("/v1/models")["data"][0]["id"]

    def _request(self, route: str, payload: dict | None = None) -> dict:
        request = Request(  # noqa: S310
            self.url + route,
            data=json.encode(payload) if payload is not None else None,
            headers={"Content-Type": "application/json"},
        )
        # This endpoint is deployment configuration, never supplied by a customer or model.
        with urlopen(request, timeout=60) as response:  # noqa: S310
            return json.decode(response.read())

    def encode(self, texts: Sequence[str], *, progress: int = 0) -> NDArray:
        if not texts:
            return np.zeros((0, DIM), dtype=np.float32)
        batches = []
        for start in range(0, len(texts), BATCH):
            if progress and start and not start % progress:
                logger.info("vectors_encoding", done=start, total=len(texts))
            batch = texts[start : start + BATCH]
            result = self._request("/v1/embeddings", {"input": list(batch), "model": self.model})
            ordered = sorted(result["data"], key=itemgetter("index"))
            if [row["index"] for row in ordered] != list(range(len(batch))):
                raise ValueError("embedding response has missing or duplicate input indices")
            matrix = np.asarray([row["embedding"] for row in ordered], dtype=np.float32)
            if matrix.shape != (len(batch), DIM) or not np.isfinite(matrix).all():
                raise ValueError("embedding response has invalid dimensions or non-finite values")
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            if (norms == 0).any():
                raise ValueError("embedding response contains a zero vector")
            batches.append(matrix / norms)
        return np.vstack(batches)


def passages(text: str, tokenizer: Tokenizer, *, tokens: int = MAX_TOKENS, overlap: int = OVERLAP) -> list[tuple[int, int]]:
    """Return overlapping character spans covering the entire original text."""
    if not 0 <= overlap < tokens - 2:
        raise ValueError("overlap must fit inside the passage token budget")
    encoded = tokenizer.encode(text, add_special_tokens=False)
    positions = encoded.offsets
    count = len(positions)
    if not count:
        return [(0, len(text))]
    spans = []
    start = 0
    while start < count:
        stop = min(start + tokens - 2, count)
        left = 0 if start == 0 else positions[start][0]
        right = len(text) if stop == count else positions[stop][0]
        # A substring can tokenize differently at its leading boundary. Check the
        # exact text submitted to the encoder, including its special tokens.
        while len(tokenizer.encode(text[left:right]).ids) > tokens:
            stop -= 1
            right = positions[stop][0]
        spans.append((left, right))
        if stop == count:
            break
        start = max(start + 1, stop - overlap)
    return spans


async def documents(db: Database) -> list[dict[str, str]]:
    """Read all retrieval sources in their stable build order."""
    from policydesk.retrieval.index import _SOURCES

    result = []
    for corpus, sql in _SOURCES.items():
        offset = 0
        while rows := await db.fetch(sql, [2000, offset]):
            result.extend({"key": f"{corpus}|{row['scope_id']}|{row['doc_id']}",
                           "text": f"{row['heading'] or ''}\n{row['verbatim'] or ''}"} for row in rows)
            offset += 2000
    return result


def fingerprint(rows: list[dict[str, str]]) -> str:
    """Include text changes, not only additions or removals of document IDs."""
    digest = sha256()
    for row in rows:
        digest.update(json.encode(row))
    return digest.hexdigest()


async def audit(db: Database, *, path: Path = VECTOR_DIR) -> dict:
    """Check source identity, matrix integrity and uninterrupted passage coverage."""
    source = await documents(db)

    def inspect() -> dict:
        manifest = json.decode((path / "current.json").read_bytes()) if (path / "current.json").is_file() else None
        root = path / manifest["generation"] if manifest else path
        keys = np.load(root / "keys.npy", allow_pickle=manifest is None)
        matrix = np.load(root / "vectors.npy", mmap_mode="r")
        expected = {row["key"]: row["text"] for row in source}
        held = set(keys)
        missing, stale = set(expected) - held, held - set(expected)
        valid = matrix.shape == (len(keys), DIM) and bool(np.isfinite(matrix).all())
        if valid:
            valid = bool(np.allclose(np.linalg.norm(matrix, axis=1), 1, atol=1e-4))
        uncovered = len(source)
        # SQL rows can arrive in a different order without a text change. Hash in
        # the snapshot's original document order, retaining exact content checks.
        indexed_source = [{"key": str(key), "text": expected[key]} for key in dict.fromkeys(keys) if key in expected]
        fresh = (manifest is not None and not missing and not stale
                 and manifest["source_sha256"] == fingerprint(indexed_source))
        if manifest:
            spans = np.load(root / "spans.npy", allow_pickle=False)
            covered: dict[str, list[tuple[int, int]]] = {}
            for key, (left, right) in zip(keys, spans, strict=True):
                covered.setdefault(str(key), []).append((int(left), int(right)))
            uncovered = 0
            for key, text in expected.items():
                until = 0
                for left, right in sorted(covered.get(key, [])):
                    if left > until or left < 0 or right < left or right > len(text):
                        break
                    until = max(until, right)
                uncovered += until != len(text)
        return {"documents": len(source), "vectors": len(keys), "missing": len(missing), "stale": len(stale),
                "uncovered": uncovered, "source_matches": fresh, "matrix_valid": valid, "manifest": manifest,
                "complete": not missing and not stale and not uncovered and fresh and valid}

    return await asyncio.to_thread(inspect)


async def build(
    db: Database, *, path: Path = VECTOR_DIR, model_dir: Path = MODEL_DIR,
    server_url: str | None = None, tokens: int = MAX_TOKENS, overlap: int = OVERLAP,
) -> int:
    """
    Embed every document and write the matrix.

    Args:
        db: The database.
        path: Where to write `vectors.npy` and `keys.npy`.
        model_dir: Where the ONNX export lives.

    Returns:
        How many documents were embedded.

    The heading is prepended to the body. A clause's heading is the one line that names
    what it does, and a chunk embedded without it is a paragraph of legal prose whose
    subject has to be inferred from the prose.

    Runs on a thread. It is minutes of CPU, and the event loop has sockets to serve.

    """
    source = await documents(db)
    if not source:
        raise ValueError("refusing to publish an empty vector index")
    tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    keys = []
    offsets = []
    texts: list[str] = []
    for number, row in enumerate(source, 1):
        for left, right in passages(row["text"], tokenizer, tokens=tokens, overlap=overlap):
            keys.append(row["key"])
            offsets.append((left, right))
            texts.append(row["text"][left:right])
        if number % PROGRESS_EVERY == 0:
            logger.info("vectors_splitting", documents=number, total=len(source), passages=len(texts))
    url = server_url or os.environ.get("POLICYDESK_EMBED_URL")
    if url:
        embedder = await asyncio.to_thread(ServerEmbedder, url)
        encoder = {"backend": "llama-server", "url": url, "model": embedder.model}
    else:
        if tokens > MAX_TOKENS:
            raise ValueError(f"ONNX encoder supports passages up to {MAX_TOKENS} tokens")
        embedder = await asyncio.to_thread(Embedder, model_dir)
        encoder = {"backend": "onnx", "model": str(await asyncio.to_thread(model_dir.resolve))}
    logger.info("vectors_building", documents=len(source), passages=len(texts), encoder=encoder["backend"])
    matrix = await asyncio.to_thread(partial(embedder.encode, texts, progress=PROGRESS_EVERY))
    generation = uuid4().hex
    destination = path / generation
    await asyncio.to_thread(destination.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(np.save, destination / "vectors.npy", matrix)
    await asyncio.to_thread(np.save, destination / "keys.npy", np.array(keys), allow_pickle=False)
    await asyncio.to_thread(np.save, destination / "spans.npy", np.array(offsets, dtype=np.int64))
    manifest = {"generation": generation, "encoder": encoder, "documents": len(source), "passages": len(texts),
                "source_sha256": fingerprint(source), "tokens": tokens, "overlap": overlap}
    pending = path / f"{generation}.json"
    await asyncio.to_thread(pending.write_bytes, json.encode(manifest))
    # Publish one pointer only after all immutable files exist. Existing mmap readers
    # keep their generation; failed builds leave the active generation untouched.
    await asyncio.to_thread(pending.replace, path / "current.json")
    logger.info("vectors_built", **manifest, path=str(path))
    return len(source)


class EmbeddingRetriever:
    """
    The semantic channel over an mmap'd matrix.

    Scoring is one GEMM against the whole corpus. At 12,000 rows by 1024 dimensions that
    is 12 million multiply-adds — under a millisecond in BLAS, and cheaper than the
    branchy alternative of maintaining an approximate index for a corpus this size.
    """

    name = "embedding"

    def __init__(self, path: Path = VECTOR_DIR, *, model_dir: Path = MODEL_DIR) -> None:
        self.manifest = json.decode((path / "current.json").read_bytes()) if (path / "current.json").is_file() else None
        root = path / self.manifest["generation"] if self.manifest else path
        self._vectors = np.lib.format.open_memmap(root / "vectors.npy", mode="r")
        keys = np.load(root / "keys.npy", allow_pickle=self.manifest is None)
        self._keys = [str(k).split("|", 2) for k in keys]
        self._spans = np.load(root / "spans.npy", allow_pickle=False) if self.manifest else None
        if len(keys) != len(self._vectors) or (self._spans is not None and len(self._spans) != len(keys)):
            raise ValueError("vector generation has inconsistent row counts")
        encoder = self.manifest["encoder"] if self.manifest else {"backend": "onnx"}
        if encoder["backend"] == "llama-server":
            self._embedder = ServerEmbedder(os.environ.get("POLICYDESK_EMBED_URL") or encoder["url"])
            if self._embedder.model != encoder["model"]:
                raise ValueError("query encoder differs from the indexed model; rebuild the vectors")
        else:
            self._embedder = Embedder(model_dir)
        # Row lookup by corpus, built once. A boolean mask per query would be a scan of
        # the whole key list on the path the customer waits on.
        self._by_corpus: dict[str, NDArray] = {
            corpus: np.array([i for i, k in enumerate(self._keys) if k[0] == corpus], dtype=np.int64)
            for corpus in (CLAUSE, STATUTE)
        }

    @property
    def size(self) -> int:
        """
        Give how many vectors are held.

        Returns:
            The row count.

        """
        return int(self._vectors.shape[0])

    def search(self, query: str, *, corpus: str = CLAUSE, scope: Sequence[str] = (), limit: int = 6) -> list[Hit]:
        """
        Find documents whose meaning is nearest the query.

        Args:
            query: What the customer asked.
            corpus: CLAUSE or STATUTE.
            scope: Which scope_ids to search. Empty means the whole corpus.
            limit: Most hits to return.

        Returns:
            Hits, best first, scored by cosine.

        The scope is applied as a row mask before the matmul, not as a filter after it.
        A customer holds two contracts out of six hundred; scoring all of them and
        keeping two spends the corpus to return a handful.

        """
        rows = self._by_corpus.get(corpus)
        if rows is None or rows.size == 0 or not query.strip():
            return []
        if scope:
            wanted = set(scope)
            rows = np.array([i for i in rows if self._keys[i][1] in wanted], dtype=np.int64)
            if rows.size == 0:
                return []

        vector = self._embedder.encode([query])[0]
        scores = np.asarray(self._vectors[rows], dtype=np.float32) @ vector
        take = min(limit, scores.shape[0])
        if take <= 0:
            # `[-0:]` is `[0:]`, so a limit of zero returned the whole corpus in an
            # arbitrary order rather than nothing. No caller passes 0 today; the negation
            # of a slice bound is not a thing the next one should have to notice.
            return []
        # Several passages may belong to one clause. Each clause gets one result,
        # carrying its best passage, so a long appendix cannot consume every slot.
        hits = []
        seen = set()
        for i in np.argsort(scores)[::-1]:
            row = rows[i]
            key = tuple(self._keys[row])
            if key in seen:
                continue
            seen.add(key)
            span = self._spans[row] if self._spans is not None else (None, None)
            hits.append(Hit(corpus=corpus, doc_id=key[2], scope_id=key[1], score=float(scores[i]),
                            start=int(span[0]) if span[0] is not None else None,
                            end=int(span[1]) if span[1] is not None else None))
            if len(hits) == take:
                break
        return hits


async def open_vectors(
    db: Database, *, path: Path = VECTOR_DIR, model_dir: Path = MODEL_DIR
) -> EmbeddingRetriever | None:
    """
    Open the vector channel over a matrix somebody else built.

    Args:
        db: Unused, and kept so this reads the same as `open_index` at the call site.
        path: Where the matrix lives.
        model_dir: Where the ONNX export lives.

    Returns:
        The retriever, or None when there is no matrix to open. None is a degradation the
        hybrid answers by running on the lexical channel alone — worse on synonyms, still
        an answer.

    **This never builds.** `open_index` does, because a BM25 build is two minutes and a
    desk that starts two minutes late has started. Embedding 12,953 documents through
    int8 ONNX on CPU is forty, and it runs inside `before_server_start` — so the port
    never opens, and what an operator sees is a desk that hung. Measured here: a second
    process started the app while a build was already running, and the two raced for the
    same `vectors.npy` at 3.2 GB each.

    Build it with `policydesk-index`.

    """
    del db
    try:
        if not await asyncio.to_thread((path / "vectors.npy").is_file) and not (path / "current.json").is_file():
            logger.info("vectors_absent", path=str(path), hint="build them with: policydesk-index")
            return None
        if not (path / "current.json").is_file() and not await asyncio.to_thread((model_dir / MODEL_FILE).is_file):
            # Checked separately from the matrix, because the two go missing separately.
            # A built matrix with no model to embed a query against raised out of
            # `before_server_start` and the desk did not come up at all — a channel that
            # cannot open is a degradation, never an outage.
            logger.info("vectors_skipped", reason="model absent", model=str(model_dir))
            return None
        retriever = await asyncio.to_thread(EmbeddingRetriever, path, model_dir=model_dir)
    except (OSError, ValueError, ImportError) as exc:
        logger.warning("vectors_unavailable", error=str(exc), path=str(path))
        return None
    logger.info("vectors_ready", documents=retriever.size)
    return retriever
