"""
What the cross-encoder is allowed to change, and what it is allowed to remove.

Driven by a stub scorer rather than the 544 MB export: the contract under test is that a
score reorders rows and that a floor drops them, and a real model would make the same
assertions depend on what it happens to think of four sentences.
"""

from typing import Any

import pytest

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


def test_a_floor_drops_what_does_not_belong():
    """
    The score says more than the order here.

    為什麼不願意跟我說原因 returns 金融消費者保護法 第19條 — a real article about a
    dispute proceeding the customer is not in. Reordering cannot express *none of these*,
    and the next-best candidate is 保險法 第57條, which is worse: it is the provision an
    insurer uses against a customer who failed to notify.
    """
    scorer = Scorer({"a": -1.0, "b": 5.0, "c": 2.0, "d": 0.0})
    kept = rerank.sift(scorer, "q", ROWS, passage=_passage, limit=4, floor=1.0)
    assert [r["t"] for r in kept] == ["b", "c"]


def test_everything_below_the_floor_returns_nothing():
    # The caller's signal to say the corpus does not answer this, rather than to show
    # its closest miss. Every scenario already has a rule for an empty tool result.
    scorer = Scorer({"a": -4.0, "b": -2.5, "c": -3.0, "d": -5.2})
    assert rerank.sift(scorer, "q", ROWS, passage=_passage, limit=4, floor=0.0) == []


def test_no_floor_keeps_every_row():
    # What the clause half does. These are the customer's own contracts, already narrowed
    # to the products they hold, so a clause that only sort of matches is still theirs.
    scorer = Scorer({"a": -4.0, "b": -2.5, "c": -3.0, "d": -5.2})
    assert len(rerank.sift(scorer, "q", ROWS, passage=_passage, limit=4)) == 4


def test_an_empty_candidate_list_is_not_an_error():
    assert rerank.sift(Scorer({}), "q", [], passage=_passage, limit=3) == []


@pytest.mark.parametrize("source", ["src/policydesk/agent/tools.py", "src/policydesk/agent/statute.py"])
def test_a_call_site_asks_for_more_candidates_than_it_returns(source: str):
    """
    Reranking a list already cut to its final length changes nothing but the order of it.

    Both call sites over-fetch to `rerank.DEPTH` when an encoder is open, and to their
    own limit when none is — so a desk without the model does not pay for candidates
    nobody will read.
    """
    from pathlib import Path

    body = Path(source).read_text(encoding="utf-8")
    assert "rerank.DEPTH" in body, f"{source} reranks whatever the caller's limit happened to be"
    assert "rerank.sift(" in body


def test_only_the_statute_half_carries_a_floor():
    # Named here because it is a decision, not an omission: dropping every clause leaves
    # a customer asking about their own policy with nothing, and dropping every provision
    # leaves them with the truth that the law does not cover it.
    from pathlib import Path

    clauses = Path("src/policydesk/agent/tools.py").read_text(encoding="utf-8")
    statutes = Path("src/policydesk/agent/statute.py").read_text(encoding="utf-8")
    assert "floor=" not in clauses.split("rerank.sift(")[1].split(")")[0]
    assert "floor=rerank.STATUTE_FLOOR" in statutes
