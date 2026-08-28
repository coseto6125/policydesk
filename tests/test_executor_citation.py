"""The citation check must catch a clause number no contract carries."""

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
