"""
The re-check is the whole point, so it gets the adversarial cases.

A model that cites a clause the contract does not have, or quotes text the document
does not contain, must not be able to pass a claim — regardless of how confident its
prose is. Every test here is a way that could happen.
"""

from unittest.mock import AsyncMock

import pytest
from msgspec import json

from policydesk.llm.provider import Completion, ScriptedProvider
from policydesk.validation.validator import VERDICT_SCHEMA, QuotedField, Verdict, recheck, validate

SUBJECT = {
    "診斷證明書": "病名：急性闌尾炎。手術名稱及部位：腹腔鏡闌尾切除術（右下腹）。住院日期：2026-08-01 至 2026-08-04。",
    "條款": "第十四條 手術醫療保險金：被保險人接受附表1所列手術治療時，本公司按手術給付倍數乘以住院醫療保險金日額給付。",
}
ALLOWED = frozenset({"art.14", "art.17", "waiting"})


def test_recheck_accepts_a_verdict_that_cites_and_quotes_correctly():
    verdict = Verdict(
        passed=True,
        reason="診斷證明書已列明手術名稱及部位。",
        cited_clauses=("art.14",),
        quoted_fields=(QuotedField(field="診斷證明書", text="腹腔鏡闌尾切除術（右下腹）"),),
    )
    checked = recheck(verdict, subject=SUBJECT, allowed_clauses=ALLOWED)
    assert checked.trustworthy
    assert checked.faults == ()


def test_recheck_rejects_a_clause_the_contract_does_not_have():
    verdict = Verdict(passed=True, reason="依第九十九條給付。", cited_clauses=("art.99",))
    checked = recheck(verdict, subject=SUBJECT, allowed_clauses=ALLOWED)
    assert not checked.trustworthy
    assert "art.99" in checked.faults[0]


def test_recheck_rejects_a_field_that_was_never_shown():
    verdict = Verdict(
        passed=True,
        reason="依收據記載。",
        quoted_fields=(QuotedField(field="醫療費用收據", text="自付額 12,000 元"),),
    )
    checked = recheck(verdict, subject=SUBJECT, allowed_clauses=ALLOWED)
    assert not checked.trustworthy
    assert "醫療費用收據" in checked.faults[0]


def test_recheck_rejects_a_quote_the_document_does_not_contain():
    """The plausible fabrication: right field, invented content."""
    verdict = Verdict(
        passed=True,
        reason="診斷證明書載明加護病房。",
        quoted_fields=(QuotedField(field="診斷證明書", text="入住加護病房三日"),),
    )
    checked = recheck(verdict, subject=SUBJECT, allowed_clauses=ALLOWED)
    assert not checked.trustworthy
    assert "查無所引原文" in checked.faults[0]


def test_recheck_tolerates_whitespace_differences_in_a_quote():
    """PDF extraction inserts spaces a model will not echo back."""
    subject = {"診斷證明書": "手術名稱及部位 ： 腹腔鏡 闌尾切除術 （右下腹）"}
    verdict = Verdict(
        passed=True,
        reason="已列明部位。",
        quoted_fields=(QuotedField(field="診斷證明書", text="腹腔鏡闌尾切除術（右下腹）"),),
    )
    assert recheck(verdict, subject=subject, allowed_clauses=frozenset()).trustworthy


@pytest.mark.parametrize(("source", "quote"), [
    ("本公司不給付（等待期30日），限住院。", "本公司不給付（等待期30日），限住院。"),
    ("本公司 不給付（等待期 30 日），限住院。", "本公司不給付（等待期30日），限住院。"),
    ("本公司 不給付（等待期 30 日），限住院。", "本公司不給付(等待期30日),限住院。"),
    ("本公司不給付(等待期30日),限住院。", "本公司 不給付（等待期 30 日），限住院。"),
])
def test_recheck_quote_width_comparison_preserves_original_inputs(source, quote):
    subject = {"條款": source}
    verdict = Verdict(passed=True, reason="核對原文。", quoted_fields=(QuotedField(field="條款", text=quote),))
    original = json.encode(verdict)
    checked = recheck(verdict, subject=subject, allowed_clauses=frozenset())
    assert checked.trustworthy
    assert checked.faults == ()
    assert checked.verdict is verdict
    assert json.encode(verdict) == original
    assert subject == {"條款": source}


@pytest.mark.parametrize(("wide", "narrow"), [
    ("\uff01", "!"), ("\uff08", "("), ("\uff09", ")"), ("\uff0c", ","),
    ("\uff0e", "."), ("\uff1a", ":"), ("\uff1b", ";"), ("\uff1f", "?"),
    ("\uff3b", "["), ("\uff3d", "]"), ("\uff5b", "{"), ("\uff5d", "}"),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_recheck_quote_width_punctuation_matches_symmetrically(wide, narrow, reverse):
    source, quote = (narrow, wide) if reverse else (wide, narrow)
    verdict = Verdict(passed=True, reason="核對標點。", quoted_fields=(QuotedField(field="條款", text=f"甲{quote}乙"),))
    checked = recheck(verdict, subject={"條款": f"甲{source}乙"}, allowed_clauses=frozenset())
    assert checked.trustworthy


@pytest.mark.parametrize("quote", [
    "本公司不理賠（等待期30日），限住院。",
    "本公司不給付（等待期3日），限住院。",
    "本公司給付（等待期30日），限住院。",
    "本公司不給付等待期30日，限住院。",
    "本公司不給付（等待期30日）；限住院。",
    "本公司不給付[等待期30日]，限住院。",
    "本公司不給付（等待期30日），限住院.",
    "本公司不給付（等待期\uff13\uff10日），限住院。",
])
def test_recheck_quote_content_change_still_rejects(quote):
    subject = {"條款": "本公司不給付（等待期30日），限住院。"}
    verdict = Verdict(passed=True, reason="核對原文。", quoted_fields=(QuotedField(field="條款", text=quote),))
    checked = recheck(verdict, subject=subject, allowed_clauses=frozenset())
    assert not checked.trustworthy
    assert checked.faults == ("欄位 條款 中查無所引原文",)


@pytest.mark.parametrize(("source", "quote"), [("A", "\uff21"), ("\u884c", "\ufa08")])
def test_recheck_quote_nonpunctuation_compatibility_change_rejects(source, quote):
    verdict = Verdict(passed=True, reason="核對原文。", quoted_fields=(QuotedField(field="條款", text=quote),))
    assert not recheck(verdict, subject={"條款": source}, allowed_clauses=frozenset()).trustworthy


@pytest.mark.parametrize("text", ["", " ", "\n\t", "　"])
def test_recheck_empty_quote_is_not_evidence(text):
    verdict = Verdict(
        passed=True,
        reason="條款支持此結論。",
        cited_clauses=("art.14",),
        quoted_fields=(QuotedField(field="條款", text=text),),
    )
    checked = recheck(verdict, subject=SUBJECT, allowed_clauses=ALLOWED)
    assert not checked.trustworthy
    assert "引文為空" in checked.faults[0]


def test_recheck_reports_every_fault_not_only_the_first():
    verdict = Verdict(
        passed=True,
        reason="兩處都錯。",
        cited_clauses=("art.99",),
        quoted_fields=(QuotedField(field="不存在的欄位", text="任意文字"),),
    )
    checked = recheck(verdict, subject=SUBJECT, allowed_clauses=ALLOWED)
    assert len(checked.faults) == 2


async def test_validate_returns_a_checked_verdict_from_the_model():
    reply = json.encode(
        Verdict(
            passed=True,
            reason="診斷證明書已列明手術名稱及部位。",
            cited_clauses=("art.14",),
            quoted_fields=(QuotedField(field="診斷證明書", text="腹腔鏡闌尾切除術（右下腹）"),),
        )
    ).decode()
    rule = "診斷證明書必須列明手術名稱及部位。"
    body = "\n\n".join(f"## {k}\n{v}" for k, v in SUBJECT.items())
    provider = ScriptedProvider({f"# 規則\n{rule}\n\n# 待判斷內容\n{body}": reply})

    checked = await validate(provider, rule=rule, subject=SUBJECT, allowed_clauses=ALLOWED)

    assert checked.trustworthy
    assert checked.verdict.passed
    assert checked.completion is not None
    assert checked.completion.provider == "scripted"


async def test_validate_schema_limits_references_to_supplied_evidence():
    provider = AsyncMock()
    provider.complete.return_value = Completion(text=json.encode(Verdict(passed=False, reason="需要核對")).decode())
    subject = {"answer": "候選答覆", "p1|art.2": "第一份契約", "p2|art.2": "第二份契約"}
    await validate(provider, rule="核對答覆", subject=subject, allowed_clauses=frozenset({"p1|art.2", "p2|art.2"}))
    schema = provider.complete.call_args.kwargs["schema"]
    assert schema["properties"]["cited_clauses"]["items"]["enum"] == ["p1|art.2", "p2|art.2"]
    fields = schema["properties"]["quoted_fields"]["items"]["properties"]
    assert fields["field"]["enum"] == list(subject)
    assert fields["text"]["minLength"] == 1
    assert "enum" not in VERDICT_SCHEMA["properties"]["cited_clauses"]["items"]


async def test_validate_schema_empty_evidence_has_no_reference_choices():
    provider = AsyncMock()
    provider.complete.return_value = Completion(text=json.encode(Verdict(passed=False, reason="資料不足")).decode())
    await validate(provider, rule="核對答覆", subject={})
    schema = provider.complete.call_args.kwargs["schema"]
    assert schema["properties"]["cited_clauses"]["maxItems"] == 0
    assert schema["properties"]["quoted_fields"]["maxItems"] == 0


async def test_validate_routes_an_unreachable_model_to_human_review():
    provider = ScriptedProvider({})
    checked = await validate(provider, rule="任何規則", subject=SUBJECT)
    assert not checked.trustworthy
    assert not checked.verdict.passed
    assert "轉人工" in checked.verdict.reason


async def test_validate_routes_an_unparseable_reply_to_human_review():
    rule = "任何規則"
    body = "\n\n".join(f"## {k}\n{v}" for k, v in SUBJECT.items())
    provider = ScriptedProvider({f"# 規則\n{rule}\n\n# 待判斷內容\n{body}": "當然可以理賠！"})

    checked = await validate(provider, rule=rule, subject=SUBJECT)

    assert not checked.trustworthy
    assert not checked.verdict.passed


async def test_validate_does_not_pass_a_claim_on_a_fabricated_citation():
    """A confident pass with an imaginary clause must not read as approval."""
    reply = json.encode(Verdict(passed=True, reason="依第九十九條，全額給付。", cited_clauses=("art.99",))).decode()
    rule = "本項給付是否成立"
    body = "\n\n".join(f"## {k}\n{v}" for k, v in SUBJECT.items())
    provider = ScriptedProvider({f"# 規則\n{rule}\n\n# 待判斷內容\n{body}": reply})

    checked = await validate(provider, rule=rule, subject=SUBJECT, allowed_clauses=ALLOWED)

    assert checked.verdict.passed, "the model said yes"
    assert not checked.trustworthy, "but the citation does not resolve, so the caller must not act on it"
