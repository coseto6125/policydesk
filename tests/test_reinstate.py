"""
The 保單復效 scenario: a right with a clock on it, and a reply that must never guess it.

Two failures matter here more than a typo would. Naming the wrong period is worse than
naming none — a customer who trusts a fabricated six-month window and misses a real
one-year window has lost a policy the desk told him he still had time on. And a reply
that says 一定可以復效 makes a promise no counter is authorised to make, since past the
grace period reinstatement runs through underwriting and the insurer may decline. Both
are tested against the real corpus and the real statute rather than a fixture, because a
citation checker validated by a fixture it wrote itself proves nothing about the
provision anyone could look up at 全國法規資料庫.
"""

import pytest

from policydesk.agent import statute, tools
from policydesk.agent.scenarios.reinstate import (
    REINSTATE,
    TOOLS,
    _citation,
    gather,
    lapsed_policies,
    reinstatement_clauses,
    statutory_floor,
)
from policydesk.core.db import Database


@pytest.fixture(scope="module")
async def db():
    pool = Database()
    try:
        await pool.fetch_val("SELECT 1")
    except Exception:
        pytest.skip("policydesk-pg is not up")
    if not await pool.fetch_val("SELECT count(*) FROM statute_article"):
        await statute.ingest(pool)
    yield pool
    await pool.close()


async def test_lapsed_policies_returns_empty_for_a_member_with_none(db):
    today = await db.fetch_val("SELECT max(lapsed_at) + 1 FROM policy")
    assert await lapsed_policies(db, -1, today=today) == []


async def test_lapsed_policies_finds_the_real_lapsed_subset(db):
    # `SCENARIO-MODULE-BRIEF.md` says this table carries a real lapsed subset; this
    # confirms the tool reads it rather than an empty one, on the actual corpus.
    rows = await db.fetch(
        "SELECT DISTINCT member_id FROM policy WHERE lapsed_at IS NOT NULL ORDER BY member_id LIMIT 1"
    )
    assert rows, "the corpus must contain at least one lapsed policy for this scenario to answer anything"
    member_id = rows[0]["member_id"]
    today = await db.fetch_val("SELECT max(lapsed_at) + 1 FROM policy")
    found = await lapsed_policies(db, member_id, today=today)
    assert found
    assert all(row["lapsed_at"] is not None for row in found)
    assert all(row["days_since_lapse"] is not None and row["days_since_lapse"] >= 0 for row in found)


async def test_reinstatement_clauses_returns_empty_for_no_products(db):
    assert await reinstatement_clauses(db, []) == []


async def test_reinstatement_clauses_reads_the_contracts_own_period(db):
    # A member from the corpus whose lapsed rider's product carries its own 復效 clause,
    # found by SQL rather than assumed — a hardcoded product id here would go stale the
    # day the fixture data regenerates.
    row = await db.fetch_one(
        """SELECT po.member_id, po.product_id
           FROM policy po JOIN clause c
             ON c.product_id = po.product_id AND c.heading ~ '復效|效力停止|恢復效力'
           WHERE po.lapsed_at IS NOT NULL
           LIMIT 1"""
    )
    assert row, "at least one lapsed policy's product must carry a reinstatement clause"
    clauses = await reinstatement_clauses(db, [row["product_id"]])
    assert clauses
    assert all("復效" in c["heading"] or "效力" in c["heading"] for c in clauses)
    assert all(c["verbatim"] for c in clauses), "the clause text itself must be real, not paraphrased"


async def test_statutory_floor_reaches_article_116(db):
    rows = await statutory_floor(db, limit=8)
    assert any(r["doc_id"].startswith("art.116") for r in rows), "§116 is the statutory floor this scenario rests on"


async def test_statutory_floor_citations_survive_a_recheck_against_the_real_corpus(db):

    rows = await statutory_floor(db, limit=8)
    text = " ".join(r["citation"] for r in rows)
    assert not await statute.unresolved(db, text), "every citation this tool writes must resolve"


async def test_statutory_floor_names_the_six_month_grace_window(db):
    rows = await statutory_floor(db, limit=8)
    assert any("六個月" in r["verbatim"] for r in rows), "§116 III's grace period must be reachable"


def test_citation_matches_the_soothe_readable_format():
    row = {"statute_name": "保險法", "article": 116, "branch": 0, "paragraph": 3, "subparagraph": None}
    assert _citation(row) == "〔保險法 第116條第3項〕"


async def test_gather_names_all_three_facts(monkeypatch: pytest.MonkeyPatch):
    class StubDB:
        async def fetch(self, sql: str, params: list[object] | None = None) -> list[dict[str, object]]:
            if "FROM policy po" in sql and "JOIN product pr" in sql:
                return [
                    {
                        "policy_id": 1,
                        "policy_number": "L1",
                        "product_id": "P1",
                        "product_name": "測試附約",
                        "sum_insured": 100000,
                        "effective_at": None,
                        "lapsed_at": "2026-01-01",
                        "days_since_lapse": 90,
                        "is_main": True,
                    }
                ]
            if "FROM contract_clause c JOIN product p" in sql:
                return [{"product_id": "P1", "clause_id": "art.7", "heading": "停效及復效", "verbatim": "…", "page": 5, "product_name": "測試附約"}]
            raise AssertionError(sql)

    import policydesk.agent.scenarios.reinstate as reinstate_mod

    async def _fake_clause_ids_for(_db: object, product_ids: list[str]) -> frozenset[str]:
        return frozenset({"art.7"}) if product_ids else frozenset()

    async def _fake_statutory_floor(_db: object, *, retriever: object = None) -> list[dict[str, object]]:
        return [{"citation": "〔保險法 第116條第3項〕", "statute": "保險法", "doc_id": "art.116.3", "chapter": "", "verbatim": "…"}]

    monkeypatch.setattr(tools, "clause_ids_for", _fake_clause_ids_for)
    monkeypatch.setattr(reinstate_mod, "statutory_floor", _fake_statutory_floor)

    facts = await gather(StubDB(), {}, member_id=1, today=None)

    assert facts["lapsed_policies"]
    assert facts["reinstatement_clauses"]
    assert facts["statutory_floor"]
    assert facts["_allowed_clauses"] == frozenset({"art.7"})


async def test_gather_with_only_the_public_tool_allowed_never_queries_the_members_book(monkeypatch: pytest.MonkeyPatch):
    # Proves the query never ran, not that its output was dropped afterwards: `fetch`
    # raises for a `FROM policy` statement, so a call to `lapsed_policies` fails loudly
    # instead of silently returning a row this test would then have to notice and drop.
    class StubDB:
        async def fetch(self, sql: str, params: list[object] | None = None) -> list[dict[str, object]]:
            if "FROM policy po" in sql:
                raise AssertionError("lapsed_policies must not run when it is not in `allowed`")
            raise AssertionError(sql)

    import policydesk.agent.scenarios.reinstate as reinstate_mod

    async def _fake_statutory_floor(_db: object, *, retriever: object = None) -> list[dict[str, object]]:
        return [{"citation": "〔保險法 第116條第3項〕", "statute": "保險法", "doc_id": "art.116.3", "chapter": "", "verbatim": "…"}]

    monkeypatch.setattr(reinstate_mod, "statutory_floor", _fake_statutory_floor)

    facts = await gather(StubDB(), {}, member_id=1, allowed=frozenset({"statutory_floor"}))
    assert facts == {"statutory_floor": [{"citation": "〔保險法 第116條第3項〕", "statute": "保險法", "doc_id": "art.116.3", "chapter": "", "verbatim": "…"}]}
    assert "lapsed_policies" not in facts
    assert "reinstatement_clauses" not in facts
    assert "_allowed_clauses" not in facts


async def test_gather_with_reinstatement_clauses_not_allowed_keeps_lapsed_but_drops_the_clause_pair(
    monkeypatch: pytest.MonkeyPatch,
):
    # `reinstatement_clauses` and `_allowed_clauses` are meant to move together — this
    # proves neither leaks when the tool that would justify a citation is withheld.
    class StubDB:
        async def fetch(self, sql: str, params: list[object] | None = None) -> list[dict[str, object]]:
            if "FROM policy po" in sql and "JOIN product pr" in sql:
                return [
                    {
                        "policy_id": 1,
                        "policy_number": "L1",
                        "product_id": "P1",
                        "product_name": "測試附約",
                        "sum_insured": 100000,
                        "effective_at": None,
                        "lapsed_at": "2026-01-01",
                        "days_since_lapse": 90,
                        "is_main": True,
                    }
                ]
            if "FROM contract_clause c JOIN product p" in sql:
                raise AssertionError("reinstatement_clauses must not run when it is not in `allowed`")
            raise AssertionError(sql)

    facts = await gather(StubDB(), {}, member_id=1, allowed=frozenset({"lapsed_policies"}))
    assert facts["lapsed_policies"]
    assert "reinstatement_clauses" not in facts
    assert "_allowed_clauses" not in facts


def test_reinstate_tools_are_gated_through_the_module_it_names():
    from importlib import import_module

    assert REINSTATE.tools_module == "policydesk.agent.scenarios.reinstate"
    owner = import_module(REINSTATE.tools_module)
    assert tools.reads_identity(REINSTATE.tools, owner=owner)
    assert tools.reads_identity(REINSTATE.tools), "unresolved without the module, so it must read as gated"


def test_reinstate_tools_dict_covers_every_name_the_scenario_lists():
    assert set(REINSTATE.tools) <= set(TOOLS)


def test_reinstate_forbids_a_guaranteed_reinstatement():
    assert "不可以說「一定可以復效」" in REINSTATE.injection


def test_reinstate_forbids_the_desk_judging_eligibility():
    assert "也不可以自己判斷這位保戶符不符合復效資格" in REINSTATE.injection


def test_reinstate_requires_the_period_from_the_clause_not_memory():
    assert "一律照那張保單自己條款的原文講" in REINSTATE.injection
    assert "不要拿保險法的期限取代契約寫的期限" in REINSTATE.injection


def test_reinstate_requires_naming_the_health_declaration_risk():
    assert "重新做健康告知" in REINSTATE.injection
    assert "並可能不同意復效" in REINSTATE.injection


def test_reinstate_handles_the_unverified_case_without_guessing():
    assert "_identity_required" in REINSTATE.injection
    assert "不要憑空講" in REINSTATE.injection


def test_reinstate_quick_replies_are_questions_not_commitments():
    for reply in REINSTATE.quick_replies:
        assert "我要" not in reply


def test_the_scenario_module_contract_is_what_the_executor_calls():
    from policydesk.agent.scenarios import reinstate

    code = reinstate.gather.__code__
    assert "member_id" in code.co_varnames
    assert "today" in code.co_varnames
    assert "retriever" in code.co_varnames
    assert code.co_flags & 0x08, "the executor always calls with retriever= as a keyword"
