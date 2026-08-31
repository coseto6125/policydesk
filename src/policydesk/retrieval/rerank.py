"""
Ask one model whether a document answers the question, after two models guessed it might.

BM25 and the embedding channel both score a document without ever looking at it beside
the question: one counts term overlap, the other compares two vectors computed apart. A
cross-encoder reads the pair together, which is why it can tell 保密義務 from 據實說明 on
a complaint that mentions neither by name.

**The score is worth more than the order here.** Reranking moved recall@5 from 0.80 to
0.88 on the forty-question gold set, which is two questions; the depths between 8 and 17
are indistinguishable at that size, and any claim that one of them is best would be a
claim about noise. What the ranking alone cannot express is *nothing here answers this* —
and every channel above returns its best guess whether or not a best guess exists. Asked
為什麼不願意跟我說原因, the hybrid returns 金融消費者保護法 第19條, 爭議過程的保密義務:
a real article, about a situation the customer is not in, and it reads to him as the law
excusing the desk from explaining. The cross-encoder scores all twenty candidates
negative, which is the correct reading of a complaint the three acts do not address.

So callers take two things from here: an order, and a floor. The floor is per corpus,
because the two corpora fail differently — a clause that only sort of matches is still
the customer's own contract and worth showing, and a statute that only sort of matches is
a citation that misleads.
"""

import os
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from policydesk.bootloader import logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

MODEL_DIR = Path(os.environ.get("POLICYDESK_RERANK_MODEL", "/home/enor/enoract/tmp/bge-reranker-v2-m3-onnx"))
"""The int8 export. Absent, no reranking happens and the fused ranking stands."""

MODEL_FILE = "onnx/model_quantized.onnx"

MAX_TOKENS = 512
"""Query and document share the window. A clause is a paragraph; the tail that falls off
a long one is the part least likely to decide relevance."""

DEPTH = 12
"""Candidates to rerank. Measured at 34 ms per pair on this machine, so 12 costs about
420 ms — spent once per turn, against two model calls that cost seconds. 8, 10, 12, 15
and 17 score within one question of each other on a forty-question gold set, so this is
the cheapest depth in a band whose members cannot be told apart, not the best of them."""

STATUTE_FLOOR: float | None = None
"""Score below which a provision is not cited at all. None until measured — the number
is being calibrated against questions the corpus answers and questions it does not, and
a guessed floor either silences the law or lets the same misfit citation through."""

CHARS = 900
"""Of the document. Past this the pair stops fitting the window, and truncation inside
the tokenizer is silent."""

K = TypeVar("K")
R = TypeVar("R")


class CrossEncoder:
    """
    bge-reranker-v2-m3 over onnxruntime, in this process.

    Loaded once. The graph is 544 MB and a session costs about a second to build, so one
    per call would put that on the customer's turn.
    """

    name = "rerank"

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

    def order(self, query: str, documents: Sequence[tuple[K, str]]) -> list[tuple[K, float]]:
        """
        Score every document against the query and sort by that score.

        Args:
            query: What the customer asked, uncut.
            documents: (key, text) pairs. The key is whatever the caller uses to find the
                row again — this module never looks inside it.

        Returns:
            (key, score) best first. The score is a logit, so it is signed: above zero
            reads as *this document bears on the question*, below as *it does not*. Empty
            input returns empty, which is the one case a caller does not have to special
            case.

        """
        if not documents:
            return []
        import numpy as np

        encoded = self._tokenizer.encode_batch([(query, text[:CHARS]) for _, text in documents])
        scores = self._session.run(
            None,
            {
                "input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64),
            },
        )[0].reshape(-1)
        return sorted(
            ((key, float(score)) for (key, _), score in zip(documents, scores, strict=True)),
            key=lambda pair: pair[1],
            reverse=True,
        )


def open_reranker(model_dir: Path = MODEL_DIR, *, threads: int = 4) -> CrossEncoder | None:
    """
    Build the reranker, or report that this deployment has no model for it.

    Args:
        model_dir: Where the ONNX export and its tokenizer live.
        threads: Intra-op threads for the session.

    Returns:
        The encoder, or None when the export is not on disk. None is a running desk with
        the fused ranking unchanged, the same way a missing embedding model leaves the
        hybrid lexical-only — a demo machine without a 544 MB download still answers.

    """
    if not (model_dir / MODEL_FILE).is_file():
        logger.info("rerank_unavailable", path=str(model_dir / MODEL_FILE))
        return None
    try:
        encoder = CrossEncoder(model_dir, threads=threads)
    except Exception as exc:
        logger.warning("rerank_failed", path=str(model_dir), error=str(exc))
        return None
    logger.info("rerank_ready", path=str(model_dir), depth=DEPTH)
    return encoder


def sift[R](
    encoder: CrossEncoder | None,
    query: str,
    rows: Sequence[R],
    *,
    passage: Callable[[R], str],
    limit: int,
    floor: float | None = None,
) -> list[R]:
    """
    Reorder rows by the cross-encoder, and drop the ones it says do not belong.

    Args:
        encoder: The cross-encoder, or None to leave the rows exactly as they came.
        query: What the customer asked.
        rows: Whatever the caller fetched, in the fused ranking's order.
        passage: Reads the text to score out of one row.
        limit: Most rows to return.
        floor: Score below which a row is dropped. None keeps every row and only
            reorders them.

    Returns:
        The rows the encoder ranked highest, cut to `limit`. Empty when the floor
        rejected all of them, which is a caller's signal to say the corpus does not
        answer this rather than to show its closest miss.

    """
    if encoder is None or not rows:
        return list(rows)[:limit]
    ordered = encoder.order(query, [(position, passage(row)) for position, row in enumerate(rows)])
    return [rows[position] for position, score in ordered if floor is None or score >= floor][:limit]
