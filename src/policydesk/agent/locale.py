"""
Which language the customer wrote in.

Ported from enoract's `shared/helpers/locale.py`: the same FastText lid.176 model, the
same script correction, the same short-Latin fallback, and the same zh-TW / zh-CN split
through OpenCC. Trimmed to what this desk needs — one detector, one entry point — and
without the emoji and cedict passes, which this desk's traffic does not exercise.

`detect(text)` names a locale or `UNKNOWN`. `resolve(db, case_id, text)` turns that into
the locale a reply is written in: what was detected, else the locale the conversation was
already in, else zh-TW, which is who this desk serves. A bare policy number or an 「ok」
carries no language, and a conversation does not switch language on one of those.
"""

from __future__ import annotations

import asyncio
import re
from functools import lru_cache
from typing import TYPE_CHECKING

from fast_langdetect import detect as _predict
from opencc import OpenCC

from policydesk.bootloader import logger

if TYPE_CHECKING:
    from policydesk.core.db import Database

UNKNOWN = "und"
"""No language could be read off the text. Never a reply language: `resolve` replaces it."""

DEFAULT = "zh-TW"
"""The desk's own language, used when nothing in the conversation says otherwise."""

_S2T = OpenCC("s2t")
_T2S = OpenCC("t2s")

_URL_RE = re.compile(r"https?://\S+|www\.\S+|[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/\S*)?", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_DIGITS_RE = re.compile(r"\d+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_LATIN_ONLY_RE = re.compile(r"^[a-zA-Z0-9\s!?.,:;'\"\-@]+$")


def _preprocess(text: str) -> str:
    """Strip what carries no language: addresses, numbers, punctuation, line breaks."""
    text = _EMAIL_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = _DIGITS_RE.sub(" ", text)
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _correct_by_script(language: str, text: str) -> str:
    """Override the model's verdict with the script on the page: CJK, kana or hangul."""
    has_cjk = has_kana = has_hangul = False
    for ch in text:
        cp = ord(ch)
        if "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿":
            has_cjk = True
        elif 0x3040 <= cp <= 0x30FF:
            has_kana = True
        elif "가" <= ch <= "힯" or "ᄀ" <= ch <= "ᇿ":
            has_hangul = True
    if has_cjk and not has_kana and not has_hangul:
        return "zh"
    if has_hangul and not has_cjk:
        return "ko"
    if has_kana:
        return "ja"
    if language not in {"zh", "ja", "ko"} and has_cjk:
        return "zh"
    return language


def _latin_fallback(raw: str, corrected: str, score: float, text: str) -> str:
    """
    Read nothing into a short or low-confidence Latin token.

    「ok」 and 「hi」 are not a language, and 「CL9926-658746」 is a policy number the model
    scored as Russian at 0.31. Both come back UNKNOWN, so `resolve` keeps the conversation's
    language. Longer low-confidence Latin text resolves to English, where real Latin
    content makes that the safer guess.
    """
    if text.isascii() and len(text) <= 2 and text.isalpha():
        return UNKNOWN
    if score < 0.6 and raw == corrected and _LATIN_ONLY_RE.match(text):
        return "en"
    return corrected


@lru_cache(maxsize=1024)
def _zh_variant(text: str) -> str:
    """
    zh-TW or zh-CN, from what OpenCC changes.

    A text that survives s2t unchanged is already traditional. One that survives t2s
    unchanged is already simplified. Text made only of characters the two scripts share
    is read as zh-TW, the desk's own. Mixed text goes to whichever script owns more of
    its characters.
    """
    to_t = _S2T.convert(text)
    to_s = _T2S.convert(text)
    if text == to_t:
        return "zh-TW"
    if text == to_s:
        return "zh-CN"
    trad = sum(a != b for a, b in zip(text, to_s, strict=False))
    simp = sum(a != b for a, b in zip(text, to_t, strict=False))
    return "zh-TW" if trad >= simp else "zh-CN"


def detect(text: str) -> str:
    """
    Name the language of one message.

    Args:
        text: What the customer typed.

    Returns:
        A locale tag: `zh-TW`, `zh-CN`, `en`, `ja`, ..., or `UNKNOWN`.

    Synchronous and a few milliseconds on the bundled lite model. `resolve` runs it off
    the event loop, which `single_process=True` shares with every other customer.
    """
    cleaned = _preprocess(text)
    if not cleaned:
        return UNKNOWN
    try:
        top = _predict(cleaned, model="lite")[0]
        raw, score = str(top["lang"]), float(top["score"])
    except Exception as exc:
        logger.warning("locale_detect_failed", error=str(exc))
        return UNKNOWN
    language = _latin_fallback(raw, _correct_by_script(raw, cleaned), score, cleaned)
    return _zh_variant(cleaned) if language == "zh" else language


async def previous(db: Database, case_id: int) -> str | None:
    """
    Read the last language this conversation was detected in.

    Args:
        db: The database.
        case_id: Which conversation.

    Returns:
        The newest customer message's locale that was not UNKNOWN, or None on a
        conversation that has not said anything with a language yet.
    """
    return await db.fetch_val(
        """SELECT locale FROM conversation_message
           WHERE case_id = $1::bigint AND speaker = 'customer' AND locale <> $2::text
           ORDER BY message_id DESC LIMIT 1""",
        [case_id, UNKNOWN],
    )


async def resolve(db: Database, case_id: int, text: str) -> tuple[str, str]:
    """
    Decide the language a reply is written in.

    Args:
        db: The database, for the conversation so far.
        case_id: Which conversation.
        text: The message just received.

    Returns:
        What this message was detected as (UNKNOWN included, for the record), and the
        locale to reply in: the detection, else the conversation's last, else the
        desk's own.
    """
    found = await asyncio.to_thread(detect, text)
    if found != UNKNOWN:
        return found, found
    return found, (await previous(db, case_id)) or DEFAULT
