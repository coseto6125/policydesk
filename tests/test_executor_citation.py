"""The citation check must catch a clause number no contract carries."""

from unittest.mock import AsyncMock

import pytest

from policydesk.agent.executor import _CITATION
from policydesk.validation.validator import Verdict, recheck

ALLOWED = frozenset({"art.16", "art.16.carve1", "art.17", "waiting"})






















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
    assert schema["properties"]["citations"]["items"]["enum"] == [f"p{number}|art.6" for number in range(12)]
    assert _answer_schema(())["properties"]["citations"]["maxItems"] == 0
