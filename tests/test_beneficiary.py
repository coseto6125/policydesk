"""
The beneficiary-change scenario, and the misattribution it was written to avoid.

The obvious way to get this wrong was already sitting in the brief: "變更受益人對保險人
生效以通知為要件" is 保險法 §111 II — 要保人行使前項處分權，非經通知，不得對抗保險人 —
not §114, which is a different right in the opposite direction (a *beneficiary*
transferring their own claim to someone else). `designation_rules` is tested against the
live corpus to confirm which article the "takes effect on notice" text actually lives in,
rather than trusting either number from memory.

The second thing tested is the boundary the module docstring states outright: this
scenario prepares a change and executes none. Nothing here calls anything but `fetch` /
`fetch_one` on `member`, and the two identity-gated tools are exactly the two that touch
a member's own record.
"""

import pytest

from policydesk.agent.scenarios import beneficiary as beneficiary_module
from policydesk.agent.scenarios.beneficiary import (
    BENEFICIARY,
    TOOLS,
    _readable,
    current_beneficiary,
    designation_rules,
    gather,
    undesignated_fallback,
)
from policydesk.agent.tools import list_policies, permitted, reads_identity
from policydesk.core.db import Database

pytestmark = pytest.mark.asyncio


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


# A member whose beneficiary_relation is really 'legal_heir' (queried against the live
# database before writing this file) — the default §113 describes: nobody was named, so
# the death benefit falls to the estate. Any such member would do.
LEGAL_HEIR_MEMBER_ID = 20


async def test_designation_rules_finds_110_and_111_not_a_hardcoded_114(db):
    rows = await designation_rules(db, "我離婚了要換人")
    doc_ids = {r["doc_id"] for r in rows}
    assert {"art.110.1", "art.110.2", "art.111.1", "art.111.2"} <= doc_ids


async def test_the_notice_effect_provision_is_111_ii_not_114(db):
    # The claim the brief made in passing turns out to name the wrong article; this
    # asserts the real one against the live text rather than either number from memory.
    rows = await designation_rules(db, "受益人可以改成誰")
    hit = next(r for r in rows if r["doc_id"] == "art.111.2")
    assert "非經通知" in hit["verbatim"]
    assert "不得對抗保險人" in hit["verbatim"]


async def test_undesignated_fallback_finds_112_and_113(db):
    rows = await undesignated_fallback(db)
    assert {r["doc_id"] for r in rows} == {"art.112.1", "art.113.1"}


async def test_undesignated_fallback_matches_the_legal_heir_default(db):
    # `BeneficiaryRelation.LEGAL_HEIR`'s own docstring calls it "the default when the
    # applicant names nobody" — this is the provision that default rests on.
    rows = await undesignated_fallback(db)
    hit = next(r for r in rows if r["doc_id"] == "art.113.1")
    assert "未指定受益人" in hit["verbatim"]
    assert "遺產" in hit["verbatim"]


async def test_designation_rules_citations_are_the_readable_bracketed_form(db):
    rows = await designation_rules(db, "一般說明")
    for row in rows:
        assert row["citation"].startswith("〔")
        assert row["citation"].endswith("〕")


def test_readable_formats_111_ii_the_way_it_is_cited():
    row = {"statute_name": "保險法", "article": 111, "branch": None, "paragraph": 2, "subparagraph": None}
    assert _readable(row) == "〔保險法 第111條第2項〕"


async def test_current_beneficiary_reads_the_members_real_recorded_relation(db):
    facts = await current_beneficiary(db, LEGAL_HEIR_MEMBER_ID)
    assert facts == {"relation": "legal_heir", "label": "法定繼承人"}


async def test_current_beneficiary_missing_member_returns_empty(db):
    assert await current_beneficiary(db, 0) == {}


async def test_gather_includes_designation_and_current_state_when_identity_is_present(db):
    facts = await gather(db, {"concern": "我離婚了要換人"}, member_id=LEGAL_HEIR_MEMBER_ID)
    assert facts["designation_rules"]
    assert facts["undesignated_fallback"]
    assert facts["current_beneficiary"] == {"relation": "legal_heir", "label": "法定繼承人"}
    assert isinstance(facts["list_policies"], list)


async def test_gather_omits_member_reads_without_an_id(db):
    facts = await gather(db, {"concern": "受益人可以改成誰"}, member_id=None)
    assert facts["designation_rules"]
    assert "current_beneficiary" not in facts
    assert "list_policies" not in facts


async def test_gather_does_not_call_the_member_tools_when_not_allowed(db, monkeypatch):
    # The property that matters is that neither query runs, not merely that their
    # output is missing afterwards. `member_id` is a real member, so if either ran, it
    # would return real data; both patched functions raise instead, so a wrongly-run
    # call fails the test loudly rather than passing on an accidental empty result.
    async def _must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a member-reading tool must not run when its name is not in `allowed`")

    monkeypatch.setattr(beneficiary_module, "current_beneficiary", _must_not_run)
    monkeypatch.setattr(beneficiary_module.tools, "list_policies", _must_not_run)
    facts = await gather(
        db,
        {"concern": "我離婚了要換人"},
        member_id=LEGAL_HEIR_MEMBER_ID,
        allowed=frozenset({"designation_rules", "undesignated_fallback"}),
    )
    assert facts["designation_rules"]
    assert facts["undesignated_fallback"]
    assert "current_beneficiary" not in facts
    assert "list_policies" not in facts


async def test_gather_still_runs_the_public_tools_when_only_they_are_allowed(db):
    # The shape the executor now produces for an unconfirmed session: `permitted`
    # excludes both gated tools, and the statutory half still answers.
    allowed = permitted(BENEFICIARY.tools, owner=beneficiary_module, confirmed=False)
    assert allowed == frozenset({"designation_rules", "undesignated_fallback"})
    facts = await gather(db, {"concern": "受益人可以改成誰"}, member_id=LEGAL_HEIR_MEMBER_ID, allowed=allowed)
    assert facts["designation_rules"]
    assert facts["undesignated_fallback"]
    assert "current_beneficiary" not in facts
    assert "list_policies" not in facts


def test_list_policies_is_reused_not_reimplemented():
    # A second copy of this query would be the same SQL maintained twice; this scenario
    # calls the one `tools.py` already gates.
    assert TOOLS["list_policies"] is list_policies


def test_two_of_four_tools_require_identity():
    gated = {name for name, fn in TOOLS.items() if getattr(fn, "requires_identity", False)}
    assert gated == {"current_beneficiary", "list_policies"}


def test_the_gate_derives_true_for_this_scenario():
    from importlib import import_module

    owner = import_module(BENEFICIARY.tools_module)
    assert reads_identity(BENEFICIARY.tools, owner=owner)


def test_beneficiary_names_the_module_its_gate_is_derived_from():
    assert BENEFICIARY.tools_module == "policydesk.agent.scenarios.beneficiary"


def test_the_scenario_module_contract_is_what_the_executor_calls():
    from policydesk.agent.scenarios import beneficiary

    code = beneficiary.gather.__code__
    assert "retriever" in code.co_varnames
    assert "allowed" in code.co_varnames, "the executor now derives and passes a per-tool allow-list"
    assert code.co_flags & 0x08, "member_id and today are passed to every scenario module"


def test_beneficiary_prepares_and_never_writes_to_member():
    import inspect

    from policydesk.agent.scenarios import beneficiary

    source = inspect.getsource(beneficiary)
    assert "UPDATE" not in source.upper()
    assert "INSERT" not in source.upper()
    assert "已經幫您改好了" in beneficiary.BENEFICIARY.injection, "named as the exact phrase to forbid"


def test_beneficiary_forbids_uncited_statute():
    assert "不可以引用工具沒有回傳的條文" in BENEFICIARY.injection


def test_beneficiary_names_the_minor_case_without_attaching_a_citation():
    # The corpus has nothing about a beneficiary who is a minor (only about a minor
    # *insured*, a different question) — checked by direct search before writing this
    # module — so the injection is required to mention it as paperwork, explicitly
    # told not to cite a provision for it.
    assert "未成年人" in BENEFICIARY.injection
    assert "不要幫這句話掛條號" in BENEFICIARY.injection


def test_beneficiary_requires_self_signature_like_the_document_scenario():
    assert "不得由他人代簽" in BENEFICIARY.injection


def test_beneficiary_collects_the_customers_own_reason():
    concern = next(p for p in BENEFICIARY.params if p.name == "concern")
    assert "自己的話" in concern.description


def test_beneficiary_quick_replies_are_questions_not_commitments():
    for reply in BENEFICIARY.quick_replies:
        assert reply.endswith(("？", "?")), reply
