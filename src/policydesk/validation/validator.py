"""
Validation by prompt, made auditable by re-checking what the model said.

Insurance rules do not reduce to regular expressions. Whether a diagnosis certificate
names the surgical site, whether a declared condition bears on the cover applied for,
whether a stated reason matches the contract — these are judgements, and a pattern that
tries to make them is either too loose to be useful or too tight to be right.

So the judgement is the model's. The audit is not.

A validator returns a structured verdict naming exactly two things: the clause ids it
relied on, and the document fields it quoted. Both are then re-checked against the
store, deterministically. A cited clause that does not exist voids the verdict. A
quoted field whose text is not in the document voids the verdict. A voided verdict does
not fall back to a guess — it becomes NEEDS_HUMAN, which is a real outcome in this
system rather than an error state.

That is the whole trick, and it is why "the validation is a prompt" and "the figures
are traceable" are not in conflict: the model decides, the store adjudicates whether
the model was reading the same documents everyone else can read.
"""

import re
from copy import deepcopy
from typing import Any

from msgspec import DecodeError, Struct, json

from policydesk.bootloader import logger
from policydesk.llm.provider import Completion, Phase, Provider, ProviderError

_WIDTH_PUNCTUATION = str.maketrans({ord(char) + 0xFEE0: char for char in "!(),.:;?[]{}"})

# Strict schema, so the reply is parseable or the call fails. A validator that has to
# guess at malformed output has already lost the property it exists for.
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "passed": {
            "type": "boolean",
            "description": "True when the subject satisfies the rule.",
        },
        "reason": {
            "type": "string",
            "description": "One sentence, in Traditional Chinese, stating what was found. Shown to a caseworker.",
        },
        "cited_clauses": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Clause ids relied on, e.g. art.17. Empty when the rule needs no clause.",
        },
        "quoted_fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["field", "text"],
                "additionalProperties": False,
            },
            "description": "Field name and the exact text quoted from it. Copy the text; do not paraphrase.",
        },
    },
    "required": ["passed", "reason", "cited_clauses", "quoted_fields"],
    "additionalProperties": False,
}

INSTRUCTIONS = """\
You check one rule against one subject and return a verdict.

Read the rule. Read the subject. Decide whether the subject satisfies the rule.

State every clause id you relied on in cited_clauses, using the ids exactly as the
subject presents them. Quote the text you relied on in quoted_fields, copying it
character for character from the field it appears in.

A verdict whose citations do not resolve is discarded, so cite only what you read.
When the subject does not contain enough to decide, set passed to false and say in
reason what is missing.

Write reason in Traditional Chinese, in one sentence.\
"""


class QuotedField(Struct, frozen=True):
    """One piece of text the model says it read, and where it says it read it."""

    field: str
    text: str


class Verdict(Struct, frozen=True):
    """What the model concluded, before re-checking."""

    passed: bool
    reason: str
    cited_clauses: tuple[str, ...] = ()
    quoted_fields: tuple[QuotedField, ...] = ()


class Checked(Struct, frozen=True):
    """
    A verdict that survived re-checking, or the record of why it did not.

    `trustworthy` is False when a citation or a quote failed to resolve. The caller
    treats that as NEEDS_HUMAN — never as a pass, and never as a refusal either, since
    a model that cited something imaginary has not established anything in either
    direction.
    """

    verdict: Verdict
    trustworthy: bool
    faults: tuple[str, ...] = ()
    completion: Completion | None = None


def recheck(verdict: Verdict, *, subject: dict[str, str], allowed_clauses: frozenset[str]) -> Checked:
    """
    Re-check a verdict against the material it claims to have read.

    Args:
        verdict: What the model returned.
        subject: The fields the model was shown, by name.
        allowed_clauses: Clause ids that exist for this subject.

    Returns:
        The verdict with a trustworthiness finding and every fault found.

    """
    faults: list[str] = [f"引用了不存在的條款 {c}" for c in verdict.cited_clauses if c not in allowed_clauses]

    for quoted in verdict.quoted_fields:
        if not (quoted_text := _squash(quoted.text)):
            faults.append(f"欄位 {quoted.field} 的引文為空")
        elif (source := subject.get(quoted.field)) is None:
            faults.append(f"引用了不存在的欄位 {quoted.field}")
        # Compare derived keys while retaining the original source and quote.
        elif quoted_text not in _squash(source):
            faults.append(f"欄位 {quoted.field} 中查無所引原文")

    return Checked(verdict=verdict, trustworthy=not faults, faults=tuple(faults))


async def validate(
    provider: Provider,
    *,
    rule: str,
    subject: dict[str, str],
    allowed_clauses: frozenset[str] = frozenset(),
    model: str | None = None,
) -> Checked:
    """
    Put one rule to the model and re-check what comes back.

    Args:
        provider: The model seam.
        rule: What must hold, in the same language a caseworker would use.
        subject: The material to judge, field name to text.
        allowed_clauses: Clause ids the subject legitimately contains.
        model: Overrides the configured model.

    Returns:
        The re-checked verdict. An unreachable model, an unparseable reply and a
        fabricated citation all produce `trustworthy=False`, which the caller reads as
        NEEDS_HUMAN rather than as a decision.

    """
    body = "\n\n".join(f"## {name}\n{text}" for name, text in subject.items())
    user_input = f"# 規則\n{rule}\n\n# 待判斷內容\n{body}"
    schema = deepcopy(VERDICT_SCHEMA)
    citations = schema["properties"]["cited_clauses"]
    if allowed_clauses:
        citations["items"]["enum"] = sorted(allowed_clauses)
    else:
        citations["maxItems"] = 0
    quotes = schema["properties"]["quoted_fields"]
    if subject:
        fields = quotes["items"]["properties"]
        fields["field"]["enum"] = list(subject)
        fields["text"]["minLength"] = 1
    else:
        quotes["maxItems"] = 0

    try:
        completion = await provider.complete(
            phase=Phase.VALIDATE,
            instructions=INSTRUCTIONS,
            user_input=user_input,
            schema=schema,
            model=model,
        )
    except ProviderError as exc:
        logger.warning("validate_unreachable", rule=rule[:60], error=str(exc))
        return Checked(
            verdict=Verdict(passed=False, reason="驗證服務無回應，本項轉人工"),
            trustworthy=False,
            faults=(f"provider unreachable: {exc}",),
        )

    try:
        verdict = json.decode(completion.text.encode(), type=Verdict)
    except (DecodeError, ValueError) as exc:
        logger.warning("validate_unparseable", rule=rule[:60], error=str(exc))
        return Checked(
            verdict=Verdict(passed=False, reason="驗證回覆格式不符，本項轉人工"),
            trustworthy=False,
            faults=(f"unparseable verdict: {exc}",),
            completion=completion,
        )

    checked = recheck(verdict, subject=subject, allowed_clauses=allowed_clauses)
    if not checked.trustworthy:
        logger.warning("validate_untrustworthy", rule=rule[:60], faults=list(checked.faults))
    return Checked(
        verdict=checked.verdict,
        trustworthy=checked.trustworthy,
        faults=checked.faults,
        completion=completion,
    )


_PAGE_FOOTER = re.compile(r"第\d+頁.{0,3}共\d+頁")
"""The running footer the PDFs carry, as it survives into a clause's own text.

940 clauses in the corpus hold one, and it lands mid-sentence: art.6 of one contract
reads …係按前款約定方式計算。第2頁，共5頁(二)自第三十一日起…. A model quoting that
clause writes the sentence, not the footer, so the quote and the source stop being
contiguous and a correct answer is withheld. Four of ten recording runs lost the same
question that way.
"""


def _squash(text: str) -> str:
    """
    Build a comparison-only key for whitespace, punctuation width and page furniture.

    Source and displayed text remain unchanged. Only the listed punctuation folds;
    letters, digits and other compatibility characters retain their original codepoints.
    Never write this key back to database clauses or use it as embedding input.

    Args:
        text: Any text.

    Returns:
        A new comparison string with whitespace removed, punctuation width folded and
        the PDF page footer dropped.

    The footer is dropped here rather than in the loader because this is the only place
    it is wrong. It belongs in `verbatim`: that column is what the contract page says,
    and a clause peek shows the reader the page they would see. It is only when the
    column is compared against a quotation that the footer is noise, and both sides pass
    through here.

    """
    return _PAGE_FOOTER.sub("", "".join(text.translate(_WIDTH_PUNCTUATION).split()))


VALIDATE_PHASE = Phase.VALIDATE
