"""
The health-declaration scenario, and the one way it hurts a customer if it goes wrong.

保險法 §64 II is the insurer's right to rescind for an untruthful or omitted health
declaration; §64 III is the two-year 除斥期間 that takes that right away. Told alone, II
reads as a threat to void the policy; told with III, it is bounded. `disclosure_duty` is
asserted here to bring back all three paragraphs together, on a real customer sentence
that shares no vocabulary with the statute's own words (a disease name is nowhere in
§64) — the failure mode this scenario exists to avoid is answering with §64 II and
nothing else because the search for a disease name came back empty.

The second thing asserted is narrower and just as real: `medical_declaration` reads a
member's actual recorded history, and the scenario's injection is checked for what it
never lets the model say about that record — never "這個不用寫", never a verdict on
whether a condition needs declaring. That judgment belongs to underwriting.
"""

import pytest

from policydesk.agent import statute
from policydesk.agent.scenarios import disclosure as disclosure_module
from policydesk.agent.scenarios.disclosure import (
    DISCLOSURE,
    TOOLS,
    disclosure_duty,
    gather,
    medical_declaration,
)
from policydesk.agent.tools import permitted, reads_identity
from policydesk.core.db import Database


@pytest.fixture(scope="module")
async def db():
    pool = Database()
    try:
        await pool.fetch_val("SELECT 1")
    except Exception:
        pytest.skip("policydesk-pg is not up")
    if not await pool.fetch_val("SELECT count(*) FROM statute_article"):
        from policydesk.agent import statute

        await statute.ingest(pool)
    yield pool
    await pool.close()


# A member whose recorded medical_history is real (queried against the live database
# before writing this file): {diabetes}. Any member with a non-empty history would do;
# this one was picked because it is not `none`, so the "already on file" branch of the
# injection has something to exercise.
DIABETIC_MEMBER_ID = 5


async def test_disclosure_duty_brings_back_all_three_paragraphs_of_64(db):
    rows = await disclosure_duty(db, "一般說明")
    doc_ids = {r["doc_id"] for r in rows}
    assert {"art.64.1", "art.64.2", "art.64.3"} <= doc_ids, "II without III reads as a threat"


async def test_disclosure_duty_still_finds_64_when_the_customer_names_a_disease(db):
    # 高血壓 appears nowhere in `statute_article` — checked directly against the live
    # corpus before this module was written, where the customer's sentence alone
    # returned zero rows. The anchor is what makes the search resolve regardless.
    rows = await disclosure_duty(db, "我有高血壓，之前也開過刀，要不要寫進健康告知")
    assert {"art.64.1", "art.64.2", "art.64.3"} <= {r["doc_id"] for r in rows}


async def test_disclosure_duty_citations_are_the_readable_bracketed_form(db):
    rows = await disclosure_duty(db, "健康告知")
    for row in rows:
        assert row["citation"].startswith("〔")
        assert row["citation"].endswith("〕")
        assert row["statute"] in row["citation"]


def test_readable_formats_64_iii_the_way_it_is_cited():
    row = {"statute_name": "保險法", "article": 64, "branch": None, "paragraph": 3, "subparagraph": None}
    assert statute.citation(row) == "〔保險法 第64條第3項〕"


async def test_medical_declaration_reads_the_members_real_recorded_history(db):
    facts = await medical_declaration(db, DIABETIC_MEMBER_ID)
    assert facts["declared"] == [{"code": "diabetes", "label": "糖尿病"}]


async def test_medical_declaration_translates_every_code_the_enum_defines(db):
    # Every value `MedicalHistory` can produce must have a label, or the desk hands the
    # model a raw English code to speak Chinese with.
    from policydesk.agent.scenarios.disclosure import _LABEL
    from policydesk.synthetic.person import MedicalHistory

    for value in MedicalHistory:
        assert value.value in _LABEL, f"{value.value} has no Chinese label"


async def test_medical_declaration_missing_member_returns_empty(db):
    assert await medical_declaration(db, 0) == {"declared": []}


async def test_gather_returns_both_halves_when_identity_is_present(db):
    facts = await gather(db, {"concern": "有高血壓要不要講"}, member_id=DIABETIC_MEMBER_ID)
    assert facts["disclosure_duty"], "the duty without the record reads as reciting law at him"
    assert facts["medical_declaration"] == {"declared": [{"code": "diabetes", "label": "糖尿病"}]}


async def test_gather_omits_the_members_record_without_an_id(db):
    facts = await gather(db, {"concern": "健康告知"}, member_id=None)
    assert facts["disclosure_duty"]
    assert "medical_declaration" not in facts


async def test_gather_does_not_call_medical_declaration_when_not_allowed(db, monkeypatch):
    # The property that matters is that the query never runs, not merely that its
    # output is missing afterwards — a scenario that queried the record and then
    # dropped it from `facts` would still have read it. `member_id` is a real member
    # with real history, so if `medical_declaration` ran, this would return it; the
    # patched function raises instead, so a wrongly-run call fails the test loudly.
    async def _must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("medical_declaration must not run when its name is not in `allowed`")

    monkeypatch.setattr(disclosure_module, "medical_declaration", _must_not_run)
    facts = await gather(
        db,
        {"concern": "有高血壓要不要講"},
        member_id=DIABETIC_MEMBER_ID,
        allowed=frozenset({"disclosure_duty"}),
    )
    assert facts["disclosure_duty"]
    assert "medical_declaration" not in facts


async def test_gather_still_runs_the_public_tool_when_only_it_is_allowed(db):
    # This is exactly the shape the executor now produces for an unconfirmed session:
    # `permitted` excludes the gated tool, and the public half still answers.
    allowed = permitted(DISCLOSURE.tools, owner=disclosure_module, confirmed=False)
    assert allowed == frozenset({"disclosure_duty"})
    facts = await gather(db, {"concern": "健康告知"}, member_id=DIABETIC_MEMBER_ID, allowed=allowed)
    assert facts["disclosure_duty"]
    assert "medical_declaration" not in facts


def test_medical_declaration_requires_identity_and_disclosure_duty_does_not():
    assert getattr(TOOLS["medical_declaration"], "requires_identity", False)
    assert not getattr(TOOLS["disclosure_duty"], "requires_identity", False)


def test_the_gate_derives_true_for_this_scenario():
    from importlib import import_module

    owner = import_module(DISCLOSURE.tools_module)
    assert reads_identity(DISCLOSURE.tools, owner=owner)


def test_disclosure_names_the_module_its_gate_is_derived_from():
    assert DISCLOSURE.tools_module == "policydesk.agent.scenarios.disclosure"


def test_the_scenario_module_contract_is_what_the_executor_calls():
    from policydesk.agent.scenarios import disclosure

    code = disclosure.gather.__code__
    assert "retriever" in code.co_varnames
    assert "allowed" in code.co_varnames, "the executor now derives and passes a per-tool allow-list"
    assert code.co_flags & 0x08, "member_id and today are passed to every scenario module"


def test_disclosure_forbids_telling_the_customer_what_to_declare():
    assert "這個不用寫" in DISCLOSURE.injection
    assert "不是這個櫃台能決定的" in DISCLOSURE.injection


def test_disclosure_requires_the_two_year_limit_told_alongside_the_right_to_rescind():
    assert "只講前面那句" in DISCLOSURE.injection
    assert "除斥期間" in DISCLOSURE.injection


def test_disclosure_forbids_admitting_fault_and_promising_underwriting_outcomes():
    assert "不可以承認公司過去做錯" in DISCLOSURE.injection
    assert "不可以承諾一定能核保通過或一定會理賠" in DISCLOSURE.injection


def test_disclosure_forbids_uncited_statute():
    assert "不可以引用工具沒有回傳的條文" in DISCLOSURE.injection


def test_disclosure_collects_the_customers_own_words_not_company_language():
    concern = next(p for p in DISCLOSURE.params if p.name == "concern")
    assert "自己的話" in concern.description


def test_disclosure_quick_replies_are_questions_not_commitments():
    for reply in DISCLOSURE.quick_replies:
        assert reply.endswith(("？", "?")), reply
