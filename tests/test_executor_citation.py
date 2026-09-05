"""The citation check must catch a clause number no contract carries."""

from unittest.mock import AsyncMock, Mock

import etoon
import pytest

from policydesk.agent.executor import _CITATION
from policydesk.validation.validator import Verdict, recheck

ALLOWED = frozenset({"art.16", "art.16.carve1", "art.17", "waiting"})


@pytest.mark.parametrize(("field", "quote", "withheld"), [
    ("p1|art.6", "生效日起三十日", False),
    ("p1|art.6", "生效日起十日", True),
    ("p1|art.6", "", True),
    ("p2|art.6", "生效日起三十日", True),
])
async def test_unverifiable_provision_quote_checks_current_selected_original(monkeypatch, field, quote, withheld):
    from policydesk.agent import executor

    monkeypatch.setattr(executor.statute, "unresolved", AsyncMock(return_value=[]))
    turn = executor.Turn(1, 1)
    turn.clause_sources = (("p1", "art.6"), ("p2", "art.6"))
    turn.clause_texts = {"p1|art.6": "自契約生效日起三十日。", "p2|art.6": "自契約生效日起三十日。"}
    held = await executor._unverifiable(
        AsyncMock(), turn, "等待期依約定。", frozenset({"art.6"}), sources=("p1|art.6",),
        quoted_fields=(executor._ProvisionQuote(field=field, text=quote, kind="waiting_period"),),
    )
    assert held is withheld


def test_clause_subject_only_collects_current_visible_evidence():
    from policydesk.agent.executor import _clause_subject, _short

    facts = {"clauses": [{"product_id": "p1", "clause_id": "art.6", "verbatim": "原文"}],
             "_private": {"product_id": "p2", "clause_id": "art.6", "verbatim": "不提供"}}
    assert _clause_subject(_short(facts)) == {"p1|art.6": "原文"}


def test_answer_schema_provision_quotes_are_scoped_and_limited_to_four_kinds():
    from policydesk.agent.executor import _answer_schema

    schema = _answer_schema((("p1", "art.6"),))
    assert "quoted_fields" in schema["required"]
    item = schema["properties"]["quoted_fields"]["items"]
    assert item["properties"]["field"]["enum"] == ["p1|art.6"]
    assert set(item["properties"]["kind"]["enum"]) == {
        "benefit_condition", "exclusion", "waiting_period", "deadline",
    }
    assert _answer_schema(())["properties"]["quoted_fields"]["maxItems"] == 0


@pytest.mark.parametrize("body_chars", [100, 4000])
def test_answer_context_twelve_products_obeys_evidence_budgets(body_chars):
    from policydesk.agent import executor, tools

    rows = [{"product_id": f"p{product}", "clause_id": f"art.{clause}", "verbatim": "文" * body_chars}
            for product in range(12) for clause in range(tools.DOCUMENTS_PER_PRODUCT)]
    context = executor._answer_context({"required_documents": rows})
    shown = context["required_documents"]
    assert len(shown) <= executor.MAX_EVIDENCE_ROWS
    assert sum(len(etoon.dumps(row)) for row in shown) <= executor.MAX_EVIDENCE_CHARS
    assert len(etoon.dumps(context)) <= executor.MAX_EVIDENCE_CHARS
    assert {row["product_id"] for row in shown} == {f"p{product}" for product in range(12)}
    assert context["evidence_coverage"]["omitted_rows"] == len(rows) - len(shown)
    assert context["evidence_coverage"]["complete"] is False
    keys = executor._answer_schema(executor._clause_sources(context))["properties"]["citations"]["items"]["enum"]
    assert set(keys) == {f"{row['product_id']}|{row['clause_id']}" for row in shown}


def test_answer_context_budget_is_shared_between_tool_results():
    from policydesk.agent import executor

    first = [{"product_id": "p", "clause_id": f"art.{i}", "verbatim": "原文"} for i in range(30)]
    second = [{"product_id": "q", "clause_id": f"art.{i}", "verbatim": "原文"} for i in range(30)]
    context = executor._answer_context({"first": first, "second": second})
    assert len(context["first"]) + len(context["second"]) <= executor.MAX_EVIDENCE_ROWS
    assert context["evidence_coverage"]["omitted_rows"] == 60 - executor.MAX_EVIDENCE_ROWS


@pytest.mark.parametrize("container", [list, tuple])
def test_answer_context_repeated_row_references_count_each_occurrence(container):
    from policydesk.agent import executor

    row = {"product_id": "p1", "clause_id": "art.1", "verbatim": "原文"}
    context = executor._answer_context({"clauses": container([row] * 96)})
    assert len(context["clauses"]) == executor.MAX_EVIDENCE_ROWS
    assert context["evidence_coverage"] == {"complete": False, "omitted_rows": 96 - executor.MAX_EVIDENCE_ROWS}


def test_answer_context_current_five_product_depth_fits_row_budget():
    from policydesk.agent import executor, tools

    rows = [{"product_id": f"p{product}", "clause_id": f"art.{clause}", "verbatim": "原文"}
            for product in range(5) for clause in range(tools.DOCUMENTS_PER_PRODUCT)]
    context = executor._answer_context({"required_documents": rows})
    assert context["required_documents"] == rows
    assert context["evidence_coverage"] == {"complete": True, "omitted_rows": 0}


def test_answer_context_nested_evidence_shares_budget():
    from policydesk.agent import executor

    rows = [{"product_id": f"p{product}", "clause_id": "parent", "children": [
        {"product_id": f"p{product}", "clause_id": f"art.{clause}", "verbatim": "條文\n" * 1000}
        for clause in range(8)]} for product in range(12)]
    context = executor._answer_context({"groups": rows})
    shown = len(executor._clause_sources(context))
    assert shown <= executor.MAX_EVIDENCE_ROWS
    assert len(etoon.dumps(context)) <= executor.MAX_EVIDENCE_CHARS
    assert context["evidence_coverage"]["omitted_rows"] == 12 * 9 - shown


def test_answer_context_oversized_non_evidence_withholds_context():
    from policydesk.agent import executor

    facts = {f"tool_{i}": ["文" * executor.CHARS] * 40 for i in range(12)}
    context = executor._answer_context(facts)
    assert len(etoon.dumps(context)) <= executor.MAX_EVIDENCE_CHARS
    assert context == {"evidence_coverage": {"complete": False, "omitted_rows": 0, "context_omitted": True}}


@pytest.mark.parametrize("body", ["文" * 100, "文" * 4000, "條文\n" * 1500])
async def test_run_turn_twelve_products_sends_bounded_prompt_and_visible_sources(monkeypatch, body):
    from policydesk.agent import executor, tools
    from policydesk.agent.scenario import BY_NAME
    from policydesk.llm.provider import Completion

    rows = [{"product_id": f"p{product}", "clause_id": f"art.{clause}", "verbatim": body}
            for product in range(12) for clause in range(tools.DOCUMENTS_PER_PRODUCT)]
    captured = []
    original = executor._answer_context

    def capture(facts):
        context = original(facts)
        captured.append(context)
        return context

    monkeypatch.setattr(executor, "_route", AsyncMock(return_value=(BY_NAME["explain_cover"], {})))
    monkeypatch.setattr(executor, "_gather", AsyncMock(return_value={
        "required_documents": rows, "_allowed_clauses": frozenset(row["clause_id"] for row in rows),
    }))
    monkeypatch.setattr(executor.memory, "recent", AsyncMock(return_value=[]))
    monkeypatch.setattr(executor.memory, "card", AsyncMock(return_value=""))
    monkeypatch.setattr(executor.tools, "standing_brief", AsyncMock(return_value={}))
    monkeypatch.setattr(executor.statute, "unresolved", AsyncMock(return_value=[]))
    monkeypatch.setattr(executor, "_record", AsyncMock())
    builder = Mock(side_effect=capture)
    monkeypatch.setattr(executor, "_answer_context", builder)
    provider = AsyncMock()
    provider.complete.return_value = Completion(
        text='{"reply":"目前資料不足以確認完整適用情形。","citations":[],"calculations":[],"quoted_fields":[]}',
        provider="test",
    )
    db = AsyncMock()
    db.fetch_val.return_value = "inquiry"
    turn = await executor.run_turn(provider, db, case_id=1, member_id=1, text="查我的保單條款", confirmed=True, locale="zh-TW")
    builder.assert_called_once()
    call = provider.complete.call_args.kwargs
    material = call["user_input"].split("# Tool results\n", 1)[1]
    assert material == etoon.dumps(captured[0])
    assert len(captured[0]["required_documents"]) <= executor.MAX_EVIDENCE_ROWS
    assert len(material) <= executor.MAX_EVIDENCE_CHARS
    assert len(turn.clause_sources) <= executor.MAX_EVIDENCE_ROWS
    assert {product for product, _ in turn.clause_sources} == {f"p{product}" for product in range(12)}
    keys = call["schema"]["properties"]["citations"]["items"]["enum"]
    assert set(keys) == {f"{product}|{clause}" for product, clause in turn.clause_sources}
    assert turn.reply.startswith(executor.EVIDENCE_LIMITED)


def test_citation_pattern_reads_ids_out_of_prose():
    text = "依 art.17 除外責任，但 art.16.carve1 回復承保，另有 waiting 之約定。"
    assert _CITATION.findall(text) == ["art.17", "art.16.carve1", "waiting"]


def test_citation_pattern_ignores_chinese_article_numbers():
    """第十七條 is prose, not an id; only minted ids are checkable."""
    assert _CITATION.findall("依第十七條之約定") == []


def test_reply_citing_only_real_clauses_is_trustworthy():
    cited = tuple(_CITATION.findall("依 art.17 與 art.16.carve1"))
    assert recheck(Verdict(passed=True, reason="", cited_clauses=cited), subject={}, allowed_clauses=ALLOWED).trustworthy


def test_reply_citing_an_invented_clause_is_caught():
    """The failure this guards: a clause number the model wrote and no contract has."""
    cited = tuple(_CITATION.findall("依 art.99 全額給付"))
    checked = recheck(Verdict(passed=True, reason="", cited_clauses=cited), subject={}, allowed_clauses=ALLOWED)
    assert not checked.trustworthy
    assert "art.99" in checked.faults[0]


def test_a_reply_mixing_real_and_invented_ids_is_still_caught():
    cited = tuple(_CITATION.findall("依 art.17 與 art.42 之約定"))
    checked = recheck(Verdict(passed=True, reason="", cited_clauses=cited), subject={}, allowed_clauses=ALLOWED)
    assert not checked.trustworthy
    assert len(checked.faults) == 1


def test_clause_sources_keeps_product_identity_from_structured_rows():
    from policydesk.agent.executor import _clause_sources

    facts = {"rules": [{"product_id": "p1", "clause_id": "art.6", "verbatim": "art.99 is only text"},
                       {"product_id": "p2", "clause_id": "art.6"},
                       {"product_id": "p1", "clause_id": "art.6"}],
             "policy": [{"product_id": "p3"}], "_metadata": {"product_id": "p4", "clause_id": "art.99"}}
    assert _clause_sources(facts) == (("p1", "art.6"), ("p2", "art.6"))


async def test_unverifiable_same_article_selects_only_declared_product(monkeypatch):
    from policydesk.agent import executor

    monkeypatch.setattr(executor.statute, "unresolved", AsyncMock(return_value=[]))
    turn = executor.Turn(1, 1)
    turn.clause_sources = (("p1", "art.6"), ("p2", "art.6"))
    assert not await executor._unverifiable(
        AsyncMock(), turn, "第一張商品的條款內容 [art.6]", frozenset({"art.6"}), sources=("p1|art.6",),
    )
    assert turn.cited_sources == (("p1", "art.6"),)


@pytest.mark.parametrize("source", ["p3|art.6", "p1|art.99", "p1|art.6|extra"])
async def test_unverifiable_unreturned_product_clause_is_withheld(monkeypatch, source):
    from policydesk.agent import executor

    monkeypatch.setattr(executor.statute, "unresolved", AsyncMock(return_value=[]))
    turn = executor.Turn(1, 1)
    turn.clause_sources = (("p1", "art.6"), ("p2", "art.99"))
    assert await executor._unverifiable(
        AsyncMock(), turn, "條款內容", frozenset({"art.6", "art.99"}), sources=(source,),
    )
    assert turn.reply == executor.WITHHELD
    assert turn.cited_sources == ()


async def test_unverifiable_structured_source_does_not_require_prose_pattern(monkeypatch):
    from policydesk.agent import executor

    monkeypatch.setattr(executor.statute, "unresolved", AsyncMock(return_value=[]))
    turn = executor.Turn(1, 1)
    turn.clause_sources = (("p1", "art.6"),)
    assert not await executor._unverifiable(
        AsyncMock(), turn, "這張保單列有等待期。", frozenset({"art.6"}), sources=("p1|art.6",),
    )
    assert turn.cited_sources == (("p1", "art.6"),)


def test_answer_schema_hidden_rows_do_not_become_selectable_sources():
    from policydesk.agent.executor import _answer_schema, _clause_sources, _short

    facts = {"groups": [{"clauses": [{"product_id": f"p{number}", "clause_id": "art.6", "verbatim": "條款原文"}]}
                        for number in range(45)]}
    visible = _short(facts)
    schema = _answer_schema(_clause_sources(visible))
    assert schema["properties"]["citations"]["items"]["enum"] == [f"p{number}|art.6" for number in range(40)]
    assert _answer_schema(())["properties"]["citations"]["maxItems"] == 0
