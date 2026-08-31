"""
What the cross-encoder is allowed to change, and what it is allowed to remove.

Driven by a stub scorer rather than the 544 MB export: the contract under test is that a
score reorders rows and that a floor drops them, and a real model would make the same
assertions depend on what it happens to think of four sentences.
"""

from typing import Any

from policydesk.retrieval import rerank


class Scorer:
    """Returns the score attached to each passage, so a test states the ranking it wants."""

    def __init__(self, scores: dict[str, float]) -> None:
        self._scores = scores

    def order(self, _query: str, documents):
        return sorted(
            ((key, self._scores[text]) for key, text in documents), key=lambda p: p[1], reverse=True
        )


ROWS = [{"t": "a"}, {"t": "b"}, {"t": "c"}, {"t": "d"}]


def _passage(row: dict[str, Any]) -> str:
    return row["t"]


def test_no_encoder_leaves_the_fused_ranking_alone():
    """
    A desk with no model on disk still answers, with the ranking two channels agreed on.

    The same downgrade the embedding channel already has: absent, the hybrid runs
    lexical-only rather than refusing to start.
    """
    assert rerank.sift(None, "q", ROWS, passage=_passage, limit=2) == ROWS[:2]


def test_the_encoder_decides_the_order():
    scorer = Scorer({"a": -1.0, "b": 5.0, "c": 2.0, "d": 0.0})
    kept = rerank.sift(scorer, "q", ROWS, passage=_passage, limit=3)
    assert [r["t"] for r in kept] == ["b", "c", "d"]


def test_an_empty_candidate_list_is_not_an_error():
    assert rerank.sift(Scorer({}), "q", [], passage=_passage, limit=3) == []


def test_the_statute_half_reranks_and_the_clause_half_does_not():
    """
    Both halves were measured, and they went opposite ways.

    On 24 statute questions a cross-encoder takes recall@5 from 0.67 to 0.83. On the
    clause half, measured the way `find_clause` runs — scoped to the three to five
    contracts a member holds, cut to the six clauses it returns — the fused order is
    right 167 times in 180 and the reranked order 161.

    Pinned because the losing configuration is the one that looks obviously right: the
    same code, the same model, one more call site.
    """
    import inspect

    from policydesk.agent import statute, tools

    # Read the two functions, not the two files. The first version matched the literal
    # `rerank.sift(` and broke the day the call became an argument to `asyncio.to_thread`
    # — a change to how it is scheduled, which says nothing about which half reranks. It
    # also passed while naming no function at all: written against the file, it could not
    # tell `_ranked_by` from `reinstate.statutory_floor`, and the first function-scoped
    # version reached for the wrong one of the two.
    reranks = inspect.getsource(statute._ranked_by)
    assert "rerank.sift" in reranks, "the statute half stopped reranking"
    assert "rerank.DEPTH" in reranks, "it reranks whatever the caller's limit happened to be"
    # The word survives in `find_clause`'s comment saying why there is no call, which is
    # the note a later reader needs most; the call is what must stay gone.
    assert "rerank.sift" not in inspect.getsource(tools.find_clause), (
        "reranking the clause half was measured and it loses"
    )


def test_nothing_is_withheld_on_a_score():
    """
    The floor this module was built for did not survive its own calibration.

    The top score of a statute question the corpus answers runs from -5.4 to 5.5, and of
    a real question it does not answer, from -10.9 to 1.1. Dropping the one misfit
    citation the floor existed to stop costs five of the seventeen questions answered.
    """
    from pathlib import Path

    body = Path("src/policydesk/retrieval/rerank.py").read_text(encoding="utf-8")
    assert "floor" not in body.split('"""', 2)[2], "a floor is back without the calibration to support it"
