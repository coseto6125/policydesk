"""
The customer's language, applied to the chips.

The reply is written by the model, in the customer's language, because the answering
prompt ends on `hint`. The one-tap chips under it are literals the code holds in zh-TW.
`ui_text` holds those sentences in other locales, keyed by the zh-TW sentence, so the code
names nothing new and a further language is rows in a table. zh-CN needs no rows: it is
zh-TW through OpenCC.

A locale with no row for a sentence falls back to English, then to the zh-TW sentence
itself, so a customer is never handed a blank where a chip was.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opencc import OpenCC

if TYPE_CHECKING:
    from collections.abc import Iterable

    from policydesk.core.db import Database

NATIVE = "zh-TW"
"""The language the code's own literals are written in."""

_T2S = OpenCC("t2s")

HINTS: dict[str, str] = {
    "zh-TW": "以台灣繁體中文回覆，不要混用簡體字。",
    "zh-CN": "使用简体中文回复，不要混用繁体。",
    "ja": "日本語で返信してください。",
    "ko": "한국어로 답변해 주세요.",
    "th": "กรุณาตอบเป็นภาษาไทย",
    "vi": "Vui lòng trả lời bằng tiếng Việt.",
    "ar": "يرجى الرد باللغة العربية.",
    "en": "Reply in English.",
}
"""In-language reinforcement, after enoract's `hints.py`. A sentence in the target
language is a stronger instruction than the same sentence about it in English."""


def hint(locale: str) -> str:
    """
    Name the reply language, in one line appended to every model call.

    Args:
        locale: The locale `agent.locale.resolve` chose for this turn.

    Returns:
        The in-language sentence when one exists, else an English instruction naming
        the tag.
    """
    return HINTS.get(locale) or f"Reply in the language tagged {locale}."


async def translate(db: Database, locale: str, texts: Iterable[str]) -> tuple[str, ...]:
    """
    Render zh-TW sentences in the customer's locale.

    Args:
        db: The database, where `ui_text` lives.
        locale: The target.
        texts: The zh-TW sentences, as the code holds them.

    Returns:
        The same sentences in `locale`, in the same order. A sentence with no row keeps
        its English row, and with no English row keeps the zh-TW original.

    One query per call, not one per sentence: the chips under a reply are four sentences
    and the customer is waiting on this.
    """
    wanted = tuple(texts)
    if not wanted or locale in (NATIVE, "", "und"):
        return wanted
    if locale == "zh-CN":
        return tuple(_T2S.convert(t) for t in wanted)
    rows = await db.fetch(
        "SELECT locale, key, text FROM ui_text WHERE locale = ANY($1::text[]) AND key = ANY($2::text[])",
        [[locale, "en"], list(wanted)],
    )
    found: dict[str, str] = {}
    for row in rows:
        if row["locale"] == locale or row["key"] not in found:
            found[row["key"]] = row["text"]
    return tuple(found.get(t, t) for t in wanted)


