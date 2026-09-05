"""
The semantic channel: local bge-m3, vectors on disk, scored by one matmul.

BM25 cannot bridge a synonym. 換工作 and 職業變更 share no character, so a customer
asking whether a new job affects their cover never reaches 職業或職務變更的通知義務 —
measured on this corpus, the lexical channel returns 保險範圍 instead. The same pair
scores 0.56 under bge-m3 against 0.46 for an unrelated clause, which is the whole reason
this channel exists.

**The model can be local, or not.** Three encoders share one shape (`encode(texts) ->
NDArray`, dispatched through `manifest["encoder"]["backend"]`): `onnx` runs `bge-m3`
int8 on disk, no key and no network, costing about 18 ms for a handful of short queries
on CPU — nothing beside the six seconds a turn already spends in the language model, but
on a small ARM core it is the slow path: enoract measured its int8 ONNX bge-m3 at ~9.4
chunks/s on a 4-OCPU OCI Ampere box against Workers AI's ~121 chunks/s over the same
network, because Cloudflare runs bge-m3 on dedicated hardware and the ARM core pays only
the round trip. `llama-server` reaches an operator-run server on the LAN. `cf-workers-ai`
reaches Cloudflare's hosted bge-m3, which is why a cloud host with no LAN llama-server
and no local model directory still has a semantic channel.

**The vectors are mmap'd, not loaded.** `np.lib.format.open_memmap(..., mode="r")` maps
`vectors.npy` into the address space and lets the page cache own it, so N workers share
one copy of the matrix rather than each holding its own. Stored fp32 rather than fp16
because numpy has no fp16 GEMM kernel — a fp16 matmul falls off BLAS onto an
object-ufunc path around twenty times slower, and halving a 48 MB file is not worth that.

Ported from enoract's `chat/retrieval/vector.py` and `shared/client/embedder.py`, cut to
one corpus and one process. `CloudflareClient` below is a further, later port of
enoract's `shared/client/cloudflare.py` — the raw HTTP layer `CloudflareEmbedder` in
`embedder.py` wraps; this file keeps only what building and querying one matrix need,
not that Protocol or its per-worker embedding cache.
"""

import asyncio
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from hashlib import sha256
from operator import itemgetter
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from urllib.request import Request, urlopen
from uuid import uuid4

import aiohttp
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


CF_MODEL = "@cf/baai/bge-m3"
"""Unquantized bge-m3 on Workers AI's own hardware. Not the same weights as llama-server's
Q5_K_M.gguf — a manifest built on one backend and queried on the other is a silent
retrieval regression, which is why `EmbeddingRetriever` refuses the mix (see below)."""

CF_MAX_BATCH = 32
"""Sized so a CJK batch stays under Workers AI's 60000-token-per-request cap: 32 chunks
at the chunker's ~1500-char max size is ~48000 tokens (CJK runs close to 1 token/char),
clearing in one request. `max_batch=100` measured 4-7x higher neuron spend on CJK text,
because it leans on the halve-and-retry safety net below for every batch instead of
letting the common case clear in one request. Ported from enoract's
`shared/client/cloudflare.py`, which carries the measurement — do not raise without it."""

_CF_QUOTA_EXHAUSTED_CODE = 4006
"""Cloudflare's error code for "used up your daily free allocation of N neurons". Only
this 429 rotates accounts; a plain 429 (short-window rate limit) propagates so the
caller's own backoff handles it instead of burning a fresh account's daily quota."""
_CF_SECONDS_PER_DAY = 86400
_CF_SOCK_READ_S = 8.0
"""Per-socket read timeout, distinct from the request `total`. Workers AI occasionally
accepts the connection then never sends a first byte; this fails that fast instead of
blocking the full `total` on one wedged call."""
_CF_TIMEOUT_RETRIES = 2
_CF_BASE_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"


class _CFAccount(NamedTuple):
    """One Cloudflare credential pair and its own session (the token lives in headers)."""

    account_id: str
    session: aiohttp.ClientSession


def _cf_next_utc_midnight(now: float) -> float:
    """Epoch seconds of the next UTC midnight, when the daily neuron quota resets."""
    return (int(now) // _CF_SECONDS_PER_DAY + 1) * _CF_SECONDS_PER_DAY


def parse_cf_credentials(account_id: str | None = None, auth_token: str | None = None) -> list[tuple[str, str]]:
    """
    Resolve ``CLOUDFLARE_ACCOUNT_ID`` / ``CLOUDFLARE_AUTH_TOKEN`` into ``(id, token)`` pairs.

    Both vars accept a single value or a comma-separated list of equal length, so a big
    build can rotate past one account's daily free-neuron allocation. Empty when either
    var is unset.
    """
    ids = [s.strip() for s in (account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")).split(",") if s.strip()]
    tokens = [s.strip() for s in (auth_token or os.environ.get("CLOUDFLARE_AUTH_TOKEN", "")).split(",") if s.strip()]
    if not ids or not tokens:
        return []
    if len(ids) != len(tokens):
        raise ValueError(f"cloudflare creds: {len(ids)} account id(s) but {len(tokens)} auth token(s) — counts must match")
    return list(zip(ids, tokens, strict=True))


class CloudflareClient:
    """
    Raw Workers AI HTTP client over one or more accounts.

    Ported from enoract's `shared/client/cloudflare.py`, production-proven there. Each
    account gets its own aiohttp session (the auth token sits in the session header). On
    a quota-exhausted 429 the active account is parked until the next UTC midnight and
    the next live account takes over, transparent to `embed`.
    """

    __slots__ = ("_accounts", "_active", "_exhausted_until")

    def __init__(self, *, account_id: str | None = None, auth_token: str | None = None, timeout_s: float = 30.0) -> None:
        creds = parse_cf_credentials(account_id, auth_token)
        if not creds:
            raise ValueError("CloudflareClient: missing CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_AUTH_TOKEN env")
        timeout = aiohttp.ClientTimeout(total=timeout_s, sock_read=_CF_SOCK_READ_S)
        self._accounts = [
            _CFAccount(account_id=aid, session=aiohttp.ClientSession(timeout=timeout, headers={"Authorization": f"Bearer {tok}"}))
            for aid, tok in creds
        ]
        self._active = 0
        # account index -> epoch seconds it becomes usable again (quota reset).
        self._exhausted_until: dict[int, float] = {}

    async def aclose(self) -> None:
        for acct in self._accounts:
            if not acct.session.closed:
                await acct.session.close()

    def _pick_live(self) -> int | None:
        """Active account if usable, else the next non-exhausted one. None if all exhausted."""
        now = time.time()
        for offset in range(len(self._accounts)):
            idx = (self._active + offset) % len(self._accounts)
            until = self._exhausted_until.get(idx)
            if until is None or now >= until:
                self._exhausted_until.pop(idx, None)
                self._active = idx
                return idx
        return None

    async def run(self, model: str, payload: dict) -> dict:
        """POST to `/ai/run/{model}` on a live account; rotate past quota-exhausted ones."""
        while (idx := self._pick_live()) is not None:
            acct = self._accounts[idx]
            url = _CF_BASE_URL.format(account_id=acct.account_id, model=model)
            async with acct.session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return json.decode(await resp.read())
                body = await resp.text()
                if resp.status == 429 and self._is_quota_exhausted(body) and len(self._accounts) > 1:
                    self._exhausted_until[idx] = _cf_next_utc_midnight(time.time())
                    logger.warning("cloudflare_quota_exhausted", account=idx,
                                   parked=len(self._exhausted_until), total=len(self._accounts))
                    continue
                logger.warning("cloudflare_api_error", model=model, status=resp.status, body=body[:300])
                resp.raise_for_status()
        raise RuntimeError(f"cloudflare: all {len(self._accounts)} account(s) quota-exhausted for today")

    @staticmethod
    def _is_quota_exhausted(body: str) -> bool:
        """Return True when a 429 body carries Cloudflare's daily-allocation-used error code."""
        try:
            errors = json.decode(body.encode()).get("errors") or []
        except Exception:
            return False
        return any(e.get("code") == _CF_QUOTA_EXHAUSTED_CODE for e in errors)

    async def embed(self, model: str, texts: Sequence[str], *, max_batch: int = CF_MAX_BATCH) -> list[list[float]]:
        """Auto-chunk to `max_batch` per call; concatenate raw vector rows. See `CF_MAX_BATCH`."""
        rows: list[list[float]] = []
        pending: deque[tuple[int, int]] = deque((s, min(s + max_batch, len(texts))) for s in range(0, len(texts), max_batch))
        attempts: dict[tuple[int, int], int] = {}
        while pending:
            start, stop = pending.popleft()
            batch = list(texts[start:stop])
            span = (start, stop)

            def _retry_transient(exc: BaseException, span: tuple[int, int] = span) -> bool:
                """Re-queue the batch up to `_CF_TIMEOUT_RETRIES` times; False once the budget is spent."""
                attempts[span] = attempts.get(span, 0) + 1
                if attempts[span] > _CF_TIMEOUT_RETRIES:
                    return False
                logger.warning("cloudflare_embed_retry", span=span, error=type(exc).__name__, attempt=attempts[span])
                pending.appendleft(span)
                return True

            try:
                data = await self.run(model, {"text": batch})
            except aiohttp.ClientResponseError as e:
                if e.status == 400 and stop - start > 1:
                    # A batch too large for this text can still fit in two. Split and
                    # retry rather than propagating — the operator sized max_batch for
                    # the common case, not the worst one.
                    mid = (start + stop) // 2
                    pending.appendleft((mid, stop))
                    pending.appendleft((start, mid))
                    continue
                # A transient 5xx is retryable like a socket wedge (ClientResponseError
                # is a ClientError subclass, so it must be handled HERE — the broad
                # except below never sees it). Other 4xx (auth, bad request) is
                # permanent: propagate.
                if e.status >= 500 and _retry_transient(e):
                    continue
                raise
            except (TimeoutError, aiohttp.ClientError) as e:
                if _retry_transient(e):
                    continue
                raise
            chunk_rows = (data.get("result") or {}).get("data") or []
            if not chunk_rows:
                raise RuntimeError(f"cloudflare embed empty response: {str(data)[:300]!r}")
            rows.extend(chunk_rows)
        return rows


class CloudflareEncoder:
    """
    Workers AI bge-m3, over HTTP — the encoder a cloud host runs when there is no
    llama-server on the LAN and no local ONNX export on disk.

    `encode` blocks its caller for the length of the HTTP round trip. That is the same
    contract `Embedder` and `ServerEmbedder` already make (both run synchronously off a
    worker thread via `asyncio.to_thread`), so this opens and closes one event loop per
    call rather than asking every caller to become async.
    """

    def __init__(self, model: str = CF_MODEL) -> None:
        if not parse_cf_credentials():
            raise ValueError("CloudflareEncoder: missing CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_AUTH_TOKEN env")
        self.model = model

    def encode(self, texts: Sequence[str], *, progress: int = 0) -> NDArray:
        if not texts:
            return np.zeros((0, DIM), dtype=np.float32)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._encode(list(texts), progress))
        # A loop is already turning on this thread. `asyncio.run` raises there, and the
        # desk never reaches this branch because `search` is called through
        # `asyncio.to_thread`, which has no loop of its own. A caller that skips the
        # thread — a test, or a future synchronous path — would otherwise fail on a
        # RuntimeError naming asyncio rather than anything in this file, so the work
        # moves to a thread of its own and this one waits for it.
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, self._encode(list(texts), progress)).result()

    async def _encode(self, texts: list[str], progress: int) -> NDArray:
        client = CloudflareClient()
        try:
            rows: list[list[float]] = []
            for start in range(0, len(texts), CF_MAX_BATCH):
                if progress and start and not start % progress:
                    logger.info("vectors_encoding", done=start, total=len(texts))
                chunk = [t or " " for t in texts[start : start + CF_MAX_BATCH]]
                rows.extend(await client.embed(self.model, chunk, max_batch=CF_MAX_BATCH))
        finally:
            await client.aclose()
        matrix = np.asarray(rows, dtype=np.float32)
        if matrix.shape != (len(texts), DIM) or not np.isfinite(matrix).all():
            raise ValueError("cloudflare embedding response has invalid dimensions or non-finite values")
        # Normalised explicitly rather than trusted from the API, matching
        # `ServerEmbedder` — the index's own integrity check (`audit`) requires unit
        # rows, and a provider's "dense" output is not contractually guaranteed to be one.
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        if (norms == 0).any():
            raise ValueError("cloudflare embedding response contains a zero vector")
        return matrix / norms


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
    server_url: str | None = None, cf_model: str | None = None, tokens: int = MAX_TOKENS, overlap: int = OVERLAP,
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
    model = cf_model or os.environ.get("POLICYDESK_CF_MODEL")
    if url:
        embedder = await asyncio.to_thread(ServerEmbedder, url)
        encoder = {"backend": "llama-server", "url": url, "model": embedder.model}
    elif model or parse_cf_credentials():
        model = model or CF_MODEL
        embedder = await asyncio.to_thread(CloudflareEncoder, model)
        encoder = {"backend": "cf-workers-ai", "model": model}
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
        elif encoder["backend"] == "cf-workers-ai":
            # Workers AI has no live "what model is this" endpoint the way llama-server's
            # /v1/models is; the manifest's recorded model is the only source of truth,
            # so an override that disagrees with it must fail here rather than at query
            # time with a quietly wrong score.
            model = os.environ.get("POLICYDESK_CF_MODEL") or encoder["model"]
            if model != encoder["model"]:
                raise ValueError("query encoder differs from the indexed model; rebuild the vectors")
            self._embedder = CloudflareEncoder(model)
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
