from datetime import date
from unittest.mock import AsyncMock, Mock

import pytest

from policydesk.core.db import Database
from policydesk.retrieval.base import CLAUSE, Hit


@pytest.mark.parametrize(("reference", "status", "numbers"), [
    ("", "all", ["CL1001", "CL1002"]),
    ("　 ", "all", ["CL1001", "CL1002"]),
    ("住院", "ambiguous", ["CL1001", "CL1002"]),
    ("ＣＬ１００２", "found", ["CL1002"]),
    ("康泰", "found", ["CL1001"]),
    ("不存在", "not_found", ["CL1001", "CL1002"]),
    ("全部", "all", ["CL1001", "CL1002"]),
])
def test_select_policies_reference_resolves_only_supplied_holdings(reference, status, numbers):
    from policydesk.agent.tools import _select_policies

    policies = [
        {"policy_number": "CL1001", "product_id": "p1", "product_name": "康泰住院醫療保險"},
        {"policy_number": "CL1002", "product_id": "p2", "product_name": "全心住院醫療附約"},
    ]
    result = _select_policies(policies, reference)
    assert result["status"] == status
    assert [row["policy_number"] for row in result["policies"]] == numbers


def test_select_policies_same_product_multiple_contracts_requires_selection():
    from policydesk.agent.tools import _select_policies

    rows = [{"policy_number": number, "product_id": "same", "product_name": "同商品"}
            for number in ("CL1", "CL2")]
    assert _select_policies(rows, "同商品")["status"] == "ambiguous"
    assert _select_policies(rows[:1], "")["policies"] == rows[:1]
    assert _select_policies([], "")["status"] == "empty"


@pytest.mark.parametrize("reference", ["住院", "外人的保單"])
@pytest.mark.parametrize("scenario_name", ["explain_cover", "claim_checklist"])
async def test_gather_unresolved_personal_policy_does_not_retrieve(monkeypatch, reference, scenario_name):
    from policydesk.agent import executor, tools
    from policydesk.agent.scenario import BY_NAME

    policies = [{"policy_number": f"CL{number}", "product_id": f"p{number}",
                 "product_name": f"住院保險{number}"} for number in (1, 2)]
    monkeypatch.setattr(tools, "list_policies", AsyncMock(return_value=policies))
    clauses = AsyncMock()
    monkeypatch.setattr(tools, "find_clause", clauses)
    documents, multipliers, clause_ids = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(tools, "required_documents", documents)
    monkeypatch.setattr(tools, "find_multiplier", multipliers)
    monkeypatch.setattr(tools, "clause_ids_for", clause_ids)
    db, index = AsyncMock(), Mock()
    facts = await executor._gather(db, BY_NAME[scenario_name], executor.Turn(1, 1),
                                   today=date(2026, 9, 5), params={"policy": reference, "topic": "住院"},
                                   confirmed=True, index=index)
    assert facts["policy_scope"]["candidates"]
    assert facts["_allowed_clauses"] == frozenset()
    assert executor._clause_sources(facts) == ()
    clauses.assert_not_called()
    documents.assert_not_called()
    multipliers.assert_not_called()
    clause_ids.assert_not_called()
    index.search.assert_not_called()
    db.fetch.assert_not_called()


@pytest.mark.parametrize(("params", "expected"), [
    ({"topic": "住院", "event": "住院"}, ["p1", "p2"]),
    ({"policy": "", "topic": "住院", "event": "住院"}, ["p1", "p2"]),
    ({"policy": "全部", "topic": "住院", "event": "住院"}, ["p1", "p2"]),
    ({"policy": "CL2", "topic": "住院", "event": "住院"}, ["p2"]),
])
@pytest.mark.parametrize("scenario_name", ["explain_cover", "claim_checklist"])
async def test_gather_personal_policy_scope_limits_retrieval(monkeypatch, params, expected, scenario_name):
    from policydesk.agent import executor, tools
    from policydesk.agent.scenario import BY_NAME

    policies = [{"policy_number": f"CL{number}", "product_id": f"p{number}",
                 "product_name": f"住院保險{number}"} for number in (1, 2)]
    monkeypatch.setattr(tools, "list_policies", AsyncMock(return_value=policies))
    clause_ids = AsyncMock(return_value=frozenset())
    monkeypatch.setattr(tools, "clause_ids_for", clause_ids)
    scenario = BY_NAME[scenario_name]
    lookups = {name: AsyncMock(return_value=[]) for name in scenario.tools if name != "list_policies"}
    for name, lookup in lookups.items():
        monkeypatch.setattr(tools, name, lookup)
    facts = await executor._gather(AsyncMock(), scenario, executor.Turn(1, 1),
                                   today=date(2026, 9, 5), params=params, confirmed=True)
    assert "policy_scope" not in facts
    assert [policy["product_id"] for policy in facts["list_policies"]] == expected
    assert clause_ids.call_args.args[1] == expected
    for lookup in lookups.values():
        assert lookup.call_args.args[1] == expected


@pytest.fixture(scope="module")
async def db():
    pool = Database()
    try:
        await pool.fetch_val("SELECT 1")
    except Exception:
        pytest.skip("policydesk-pg is not up")
    yield pool
    await pool.close()


@pytest.mark.parametrize("product", ["38cfb37f85cf", "新實全心意PLUS住院醫療健康保險附約", "新實全心意ＰＬＵＳ 住院醫療健康保險附約（外溢型）"])
async def test_catalogue_clauses_named_product_returns_only_its_contract(db, product):
    from policydesk.agent.scenarios.product_clauses import catalogue_clauses

    result = await catalogue_clauses(db, product, "除外責任")
    assert result["status"] == "found"
    assert [row["product_id"] for row in result["products"]] == ["38cfb37f85cf"]
    assert result["clauses"]
    assert {row["product_id"] for row in result["clauses"]} == {"38cfb37f85cf"}
    assert len(result["clauses"]) == len({(row["product_id"], row["clause_id"]) for row in result["clauses"]})


@pytest.mark.parametrize("clause_id", ["art.2", "art.15"])
async def test_catalogue_clauses_real_long_article_reaches_answer_context_whole(db, clause_id):
    from policydesk.agent.executor import _short
    from policydesk.agent.scenarios.product_clauses import catalogue_clauses

    product_id = "38cfb37f85cf"
    original = await db.fetch_val(
        "SELECT verbatim FROM contract_clause WHERE product_id = $1 AND clause_id = $2",
        [product_id, clause_id],
    )
    assert len(original) > 1200
    index = Mock()
    index.search.return_value = [Hit(CLAUSE, clause_id, product_id, 1, start=0, end=200)]
    result = _short(await catalogue_clauses(db, product_id, "條款條件與例外", index=index))
    row = next(row for row in result["clauses"] if row["clause_id"] == clause_id)
    assert row["verbatim"] == original
    assert not row.get("excerpt", False)


@pytest.mark.parametrize("product", ["", "　 "])
async def test_catalogue_clauses_missing_product_does_not_search(db, product):
    from policydesk.agent.scenarios.product_clauses import catalogue_clauses

    pool = AsyncMock()
    result = await catalogue_clauses(pool, product, "除外責任")
    assert result["status"] == "needs_product"
    pool.fetch.assert_not_called()


@pytest.mark.parametrize("product", ["7c46c305821e", "%", "_", "' OR true --", "不存在的商品九八七六五"])
async def test_catalogue_clauses_noncontract_or_literal_miss_returns_no_sources(db, product):
    from policydesk.agent.scenarios.product_clauses import catalogue_clauses

    result = await catalogue_clauses(db, product, "除外責任")
    assert result["status"] == "not_found"
    assert result["clauses"] == []


async def test_catalogue_clauses_ambiguous_name_never_merges_contracts(db):
    from policydesk.agent.scenarios.product_clauses import catalogue_clauses

    index = Mock()
    result = await catalogue_clauses(db, "實全心意", "除外責任", index=index)
    assert result["status"] == "ambiguous"
    assert result["match_count"] > 1
    assert result["clauses"] == []
    index.search.assert_not_called()


@pytest.mark.parametrize("unexpected", [Hit(CLAUSE, "art.14", "c9bcc741c6c6", 0.9),
                                        Hit("statute", "art.15", "38cfb37f85cf", 0.9)])
async def test_catalogue_clauses_retriever_scope_and_returned_sources_are_checked(db, unexpected):
    from policydesk.agent.scenarios.product_clauses import catalogue_clauses

    index = Mock()
    index.search.return_value = [Hit(CLAUSE, "art.15", "38cfb37f85cf", 1), unexpected]
    result = await catalogue_clauses(db, "38cfb37f85cf", "除外責任", index=index)
    assert index.search.call_args.kwargs["scope"] == ["38cfb37f85cf"]
    assert result["clauses"]
    assert {row["product_id"] for row in result["clauses"]} == {"38cfb37f85cf"}


async def test_catalogue_clauses_missing_topic_does_not_run_unscoped_query(db):
    from policydesk.agent.scenarios.product_clauses import catalogue_clauses

    index = Mock()
    result = await catalogue_clauses(db, "38cfb37f85cf", "", index=index)
    assert result["status"] == "needs_topic"
    assert result["clauses"] == []
    index.search.assert_not_called()


@pytest.mark.parametrize("unexpected", [Hit(CLAUSE, "art.14", "c9bcc741c6c6", 0.9),
                                        Hit("statute", "art.15", "38cfb37f85cf", 0.9)])
async def test_catalogue_clauses_invalid_hits_are_rejected_before_clause_read(db, unexpected):
    from policydesk.agent.scenarios.product_clauses import catalogue_clauses

    class CatalogOnly:
        async def fetch(self, sql, params):
            assert "FROM contract_clause" not in sql
            return await db.fetch(sql, params)

    index = Mock()
    index.search.return_value = [unexpected]
    result = await catalogue_clauses(CatalogOnly(), "38cfb37f85cf", "除外責任", index=index)
    assert result["clauses"] == []


async def test_catalogue_clauses_broad_name_reports_total_without_retrieving_all(db):
    from policydesk.agent.scenarios.product_clauses import catalogue_clauses

    index = Mock()
    result = await catalogue_clauses(db, "保險", "除外責任", index=index)
    assert result["status"] == "ambiguous"
    assert result["match_count"] > len(result["products"]) == 5
    assert result["clauses"] == []
    index.search.assert_not_called()


async def test_gather_public_contract_without_member_read_or_identity_request(db):
    from policydesk.agent.executor import Turn, _gather
    from policydesk.agent.scenarios.product_clauses import PRODUCT_CLAUSES

    class PublicOnly:
        async def fetch(self, sql, params=None):
            flat = " ".join(sql.lower().split())
            assert not any(word in flat for word in ("from member", "join member", "from policy", "join policy"))
            return await db.fetch(sql, params)

    facts = await _gather(PublicOnly(), PRODUCT_CLAUSES, Turn(case_id=1, member_id=1),
                          today=date(2026, 9, 5), confirmed=False,
                          params={"product": "38cfb37f85cf", "topic": "除外責任"})
    assert not facts.get("_identity_required")
    assert facts["catalogue_clauses"]["clauses"]
    assert facts["_allowed_clauses"]


async def test_gather_withheld_catalogue_tool_does_not_run():
    from policydesk.agent.scenarios.product_clauses import gather

    pool = AsyncMock()
    assert await gather(pool, {"product": "38cfb37f85cf", "topic": "除外責任"}, allowed=frozenset()) == {}
    pool.fetch.assert_not_called()


@pytest.mark.parametrize(("scenario_name", "pending"), [("product_clauses", False), ("quote", True), ("claim_status", True)])
async def test_run_turn_module_uses_its_own_identity_requirements(monkeypatch, scenario_name, pending):
    from policydesk.agent import executor
    from policydesk.agent.scenario import BY_NAME, PUBLIC_OPENERS
    from policydesk.llm.provider import Completion

    scenario = BY_NAME[scenario_name]
    monkeypatch.setattr(executor, "_route", AsyncMock(return_value=(scenario, {"product": "x", "topic": "除外責任"})))
    monkeypatch.setattr(executor, "_gather", AsyncMock(return_value={"_allowed_clauses": frozenset()}))
    monkeypatch.setattr(executor.memory, "recent", AsyncMock(return_value=[]))
    monkeypatch.setattr(executor.statute, "unresolved", AsyncMock(return_value=[]))
    provider = AsyncMock()
    provider.complete.return_value = Completion(text='{"reply":"公開商品條款說明。","citations":[],"calculations":[]}', provider="test")
    db = AsyncMock()
    db.fetch_val.return_value = "inquiry"
    turn = await executor.run_turn(provider, db, case_id=1, member_id=1, text="查指定商品的除外責任", confirmed=False, locale="zh-TW")
    assert turn.awaiting_identity is pending
    assert turn.quick_replies == (PUBLIC_OPENERS if pending else scenario.quick_replies)


async def test_run_turn_lookup_scope_instruction_is_only_sent_to_router(monkeypatch):
    from policydesk.agent import executor
    from policydesk.agent.scenario import LOOKUP_SCOPE
    from policydesk.llm.provider import Completion

    monkeypatch.setattr(executor, "_gather", AsyncMock(return_value={"_allowed_clauses": frozenset()}))
    monkeypatch.setattr(executor.memory, "recent", AsyncMock(return_value=[]))
    monkeypatch.setattr(executor.statute, "unresolved", AsyncMock(return_value=[]))
    provider = AsyncMock()
    provider.complete.side_effect = [
        Completion(text="", tool_calls=({"name": "product_clauses", "arguments": '{"product":"x","topic":"保障"}'},), provider="test"),
        Completion(text='{"reply":"現有資料不足以確認。","citations":[],"calculations":[]}', provider="test"),
    ]
    db = AsyncMock()
    db.fetch_val.return_value = "inquiry"
    turn = await executor.run_turn(provider, db, case_id=1, member_id=1, text="商品保障", locale="zh-TW")
    route, answer = [call.kwargs for call in provider.complete.call_args_list]
    assert route["instructions"].startswith(LOOKUP_SCOPE)
    assert LOOKUP_SCOPE not in answer["instructions"]
    assert turn.scenario == "product_clauses"
    assert not turn.awaiting_identity


async def test_run_turn_stable_sections_precede_variable_prompt_sections(monkeypatch):
    from policydesk.agent import executor
    from policydesk.agent.scenario import BY_NAME, ROUTER_INSTRUCTIONS, WRITING
    from policydesk.llm.provider import Completion

    monkeypatch.setattr(executor, "_gather", AsyncMock(return_value={"_allowed_clauses": frozenset()}))
    monkeypatch.setattr(executor.memory, "recent", AsyncMock(return_value=[]))
    monkeypatch.setattr(executor.memory, "transcript", lambda messages: "TRANSCRIPT_MARKER\n")
    monkeypatch.setattr(executor.memory, "card", AsyncMock(return_value="PROFILE_MARKER\n"))
    monkeypatch.setattr(executor.tools, "standing_brief", AsyncMock(return_value={"known": "KNOWN_MARKER"}))
    monkeypatch.setattr(executor.statute, "unresolved", AsyncMock(return_value=[]))
    provider = AsyncMock()
    provider.complete.side_effect = [
        Completion(text="", tool_calls=({"name": "product_clauses", "arguments": '{"product":"x","topic":"保障"}'},), provider="test"),
        Completion(text='{"reply":"資料不足。","citations":[],"calculations":[],"quoted_fields":[]}', provider="test"),
    ]
    db = AsyncMock()
    db.fetch_val.return_value = "inquiry"
    await executor.run_turn(provider, db, case_id=1, member_id=1, text="商品保障", confirmed=True, locale="zh-TW")
    route, answer = [call.kwargs for call in provider.complete.call_args_list]
    for call in (route, answer):
        text = call["user_input"]
        assert text.index("KNOWN_MARKER") < text.index("TRANSCRIPT_MARKER") < text.index("PROFILE_MARKER")
    instructions = answer["instructions"]
    assert instructions.index(ROUTER_INSTRUCTIONS) < instructions.index(WRITING) < instructions.index(BY_NAME["product_clauses"].injection)
