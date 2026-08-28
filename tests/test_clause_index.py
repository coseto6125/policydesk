"""
The two traps in section-one of the index module are the reason this file exists.

Both were found in a real contract, so both are asserted against that contract rather
than against a hand-written sample that would only prove the regex matches itself.
"""

from pathlib import Path

import pytest
from msgspec import ValidationError  # noqa: F401  (kept: msgspec import proves env wiring)

from policydesk.clauses.index import build_index, cn_to_int
from policydesk.core.models import Citation, ClauseKind, Money, Stage, may_advance

FIXTURE = Path(__file__).parent.parent / "data" / "clauses" / "cathay-inpatient-daily.pdf"
needs_pdf = pytest.mark.skipif(not FIXTURE.exists(), reason="run scripts/fetch_fixtures.sh first")


def test_cn_to_int_units_returns_digit():
    assert cn_to_int("三") == 3


def test_cn_to_int_bare_ten_returns_ten():
    assert cn_to_int("十") == 10


def test_cn_to_int_teens_returns_ten_plus_unit():
    assert cn_to_int("十七") == 17


def test_cn_to_int_compound_tens_returns_full_value():
    assert cn_to_int("二十三") == 23


def test_money_without_citation_raises():
    with pytest.raises(ValueError, match="no citation"):
        Money(amount=8000, basis="日額 2,000 × 4 日", citations=())


def test_money_with_citation_keeps_amount():
    cite = Citation(doc_id="d", clause_id="art.11", page=5, verbatim="住院日額醫療保險金")
    assert Money(amount=8000, basis="日額 2,000 × 4 日", citations=(cite,)).amount == 8000


@needs_pdf
def test_build_index_real_contract_finds_every_article():
    index = build_index(FIXTURE)
    # The contract prints 24 articles; the parser also emits derived clauses.
    articles = [c for c in index.clauses if c.startswith("art.") and "." not in c[4:]]
    assert len(articles) >= 24


@needs_pdf
def test_build_index_classifies_exclusion_article():
    index = build_index(FIXTURE)
    assert index.clauses["art.17"].kind is ClauseKind.EXCLUSION
    assert "除外責任" in index.clauses["art.17"].heading


@needs_pdf
def test_build_index_finds_waiting_period_hidden_in_definition():
    """Trap 1: the contract never writes 等待期, so a label search finds nothing."""
    index = build_index(FIXTURE)
    raw = "".join(c.verbatim for c in index.clauses.values())
    assert "等待期" not in raw, "if the contract ever labels it, this test is testing the wrong thing"

    waiting = index.clauses["waiting"]
    assert waiting.kind is ClauseKind.WAITING
    assert "三十日" in waiting.verbatim


@needs_pdf
def test_build_index_splits_carve_back_out_of_exclusion():
    """Trap 2: art.17 excludes cosmetic surgery, then restores reconstructive surgery."""
    index = build_index(FIXTURE)
    carves = [c for c in index.clauses.values() if c.kind is ClauseKind.CARVE_BACK]
    assert carves, "an exclusion that carves back must not be indexed as one flat clause"

    reconstructive = next(c for c in carves if "重建" in c.verbatim)
    assert reconstructive.overrides == ("art.17",)


@needs_pdf
def test_cite_unknown_clause_raises_rather_than_inventing():
    index = build_index(FIXTURE)
    with pytest.raises(KeyError):
        index.cite("art.999")


def test_cn_to_int_arabic_returns_same_number():
    """113 年起修正的條款改印阿拉伯數字條號，兩套都要讀得懂。"""
    assert cn_to_int("19") == 19


def test_may_advance_one_step_forward_is_allowed():
    assert may_advance(Stage.INQUIRY, Stage.PROPOSED)


def test_may_advance_skipping_a_stage_is_refused():
    """驗證身分前不得送審，否則簽署不具本人親簽的推定效力。"""
    assert not may_advance(Stage.SIGNED, Stage.REVIEW)


def test_may_advance_backwards_is_refused():
    assert not may_advance(Stage.REVIEW, Stage.PROPOSED)


def test_may_advance_decision_requires_review():
    assert may_advance(Stage.REVIEW, Stage.APPROVED)
    assert not may_advance(Stage.VERIFIED, Stage.APPROVED)


def test_may_advance_from_decided_case_is_refused():
    assert not may_advance(Stage.REJECTED, Stage.INQUIRY)
    assert not may_advance(Stage.APPROVED, Stage.REVIEW)
