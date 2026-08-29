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
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from policydesk.bootloader import logger
from policydesk.retrieval.base import CLAUSE, STATUTE, Hit

if TYPE_CHECKING:
    from collections.abc import Sequence

    from numpy.typing import NDArray

    from policydesk.core.db import Database

VECTOR_DIR = Path("data/vectors")
"""Beside the BM25 index, and a cache in exactly the same way: rebuilt from Postgres."""

MODEL_DIR = Path(os.environ.get("POLICYDESK_EMBED_MODEL", "/home/enor/enoract/tmp/bge-m3-int8-pkg"))
"""The int8 export. Absent, the channel does not open and the hybrid runs lexical-only."""

MODEL_FILE = "onnx/model_quantized.onnx"
DIM = 1024
MAX_TOKENS = 512
"""bge-m3 accepts 8192, but a clause is a paragraph and the cost is quadratic in the
attention. 512 covers every clause in this corpus with room to spare."""

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


async def build(db: Database, *, path: Path = VECTOR_DIR, model_dir: Path = MODEL_DIR) -> int:
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
    from policydesk.retrieval.index import _SOURCES

    documents: list[tuple[str, str, str]] = []
    texts: list[str] = []
    for corpus, sql in _SOURCES.items():
        offset = 0
        while True:
            rows = await db.fetch(sql, [2000, offset])
            if not rows:
                break
            for row in rows:
                documents.append((corpus, row["scope_id"], row["doc_id"]))
                texts.append(f"{row['heading'] or ''}\n{row['verbatim'] or ''}"[: MAX_TOKENS * 3])
            offset += 2000
    if not documents:
        return 0

    embedder = await asyncio.to_thread(Embedder, model_dir)
    matrix = await asyncio.to_thread(partial(embedder.encode, texts, progress=PROGRESS_EVERY))

    await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(np.save, path / "vectors.npy", matrix)
    # Keys as one array of strings rather than three: the row order is the join, and one
    # file cannot fall out of step with itself.
    keys = np.array([f"{c}|{s}|{d}" for c, s, d in documents], dtype=object)
    await asyncio.to_thread(np.save, path / "keys.npy", keys, allow_pickle=True)
    logger.info("vectors_built", documents=len(documents), dim=matrix.shape[1], path=str(path))
    return len(documents)


class EmbeddingRetriever:
    """
    The semantic channel over an mmap'd matrix.

    Scoring is one GEMM against the whole corpus. At 12,000 rows by 1024 dimensions that
    is 12 million multiply-adds — under a millisecond in BLAS, and cheaper than the
    branchy alternative of maintaining an approximate index for a corpus this size.
    """

    name = "embedding"

    def __init__(self, path: Path = VECTOR_DIR, *, model_dir: Path = MODEL_DIR) -> None:
        self._vectors = np.lib.format.open_memmap(path / "vectors.npy", mode="r")
        keys = np.load(path / "keys.npy", allow_pickle=True)
        self._keys = [str(k).split("|", 2) for k in keys]
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
        # argpartition finds the k largest in O(n); argsort then orders just those k.
        top = np.argpartition(scores, -take)[-take:]
        top = top[np.argsort(scores[top])[::-1]]
        return [
            Hit(corpus=corpus, doc_id=self._keys[rows[i]][2], scope_id=self._keys[rows[i]][1], score=float(scores[i]))
            for i in top
        ]


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
        if not await asyncio.to_thread((path / "vectors.npy").is_file):
            logger.info("vectors_absent", path=str(path), hint="build them with: policydesk-index")
            return None
        retriever = await asyncio.to_thread(EmbeddingRetriever, path, model_dir=model_dir)
    except (OSError, ValueError, ImportError) as exc:
        logger.warning("vectors_unavailable", error=str(exc), path=str(path))
        return None
    logger.info("vectors_ready", documents=retriever.size)
    return retriever

