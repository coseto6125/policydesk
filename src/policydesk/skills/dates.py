"""
The date tool the model calls instead of counting days itself.

A deadline is the figure this desk gets asked about most and checks least. 保險法 第65條
gives a claim two years, a 復效 clause gives six months from the lapse, a rescission clause
gives ten days from the day after delivery — and a model that turns 「2026-03-01 起十日內」
into a calendar date in its head is a model that can be off by one and confident about it.
An amount has the calculator; a date had nothing, so the scenarios that state one forbade
the model from working it out at all. This tool lets a scenario allow it: the model writes
the expression, this module evaluates it, and the working is kept beside the result.

The grammar is one date followed by zero or more spans, or two dates whose difference is a
count of days. Dates are ISO (`2026-03-01`), the word `today`, or the ROC form a contract
prints (`民國115年3月1日`, `115/03/01`). Spans are a count and a unit in Chinese or English.
Anything else raises rather than evaluates, so an expression is date arithmetic or it is
an error, never a guess.

`today` is Taiwan's date. The desk answers Taiwanese policyholders about Taiwanese
contracts, and `datetime.now(UTC).date()` is yesterday for the first eight hours of every
Taiwanese day — a customer asking at 07:00 whether a deadline has passed was being judged
against the wrong day.
"""

import re
from calendar import monthrange
from datetime import date, datetime
from zoneinfo import ZoneInfo

from msgspec import Struct

TAIPEI = ZoneInfo("Asia/Taipei")

_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
# 民國115年3月1日, 115年3月1日, 115/03/01 — the forms a Taiwanese policy prints.
_ROC = re.compile(r"(?:民國)?(\d{1,3})(?:年|/)(\d{1,2})(?:月|/)(\d{1,2})日?")
# Any one date literal, for finding where an expression's dates start and stop.
_DATE = re.compile(rf"today|{_ISO.pattern}|{_ROC.pattern}")
# The count is capped at nine digits: `int()` of a longer string raises its own ValueError
# on 3.14 (the 4,300-digit limit), which is not a DateError, and no span a contract names
# needs more. A nine-digit span still leaves the calendar and becomes a DateError in `_shift`.
_SPAN = re.compile(r"([+-])\s*(\d{1,9})\s*(天|日|days?|d|個月|月|months?|m|年|years?|y)\b", re.IGNORECASE)
_UNITS: dict[str, str] = {
    "天": "days", "日": "days", "d": "days", "day": "days", "days": "days",
    "個月": "months", "月": "months", "m": "months", "month": "months", "months": "months",
    "年": "years", "y": "years", "year": "years", "years": "years",
}


class Dated(Struct, frozen=True):
    """
    One evaluated date expression.

    `basis` is the expression as the model wrote it, kept so a reviewer re-derives the
    date without reading code — the same role `Computed.basis` plays for an amount.
    """

    value: date | int
    """A calendar date, or a count of days when the expression subtracted two dates."""
    basis: str

    @property
    def text(self) -> str:
        """The value as the reply and the trace write it."""
        return self.value.isoformat() if isinstance(self.value, date) else f"{self.value} 天"


class DateError(ValueError):
    """The expression could not be evaluated, and the caller must not fall back."""


def today() -> date:
    """
    Return the date in Taiwan.

    Returns:
        The calendar date in Asia/Taipei now.

    """
    return datetime.now(TAIPEI).date()


def _parse_date(text: str, *, today: date) -> date:
    """
    Read one date literal.

    Args:
        text: `today`, an ISO date, or an ROC date.
        today: What `today` resolves to.

    Returns:
        The date.

    Raises:
        DateError: The text is not a date this tool reads, or names an impossible day.

    """
    if (text := text.strip()) == "today":
        return today
    if found := _ISO.fullmatch(text):
        year, month, day = (int(part) for part in found.groups())
    elif found := _ROC.fullmatch(text):
        year, month, day = (int(part) for part in found.groups())
        year += 1911
    else:
        raise DateError(f"{text!r} is not a date; write 2026-03-01, 民國115年3月1日 or today")
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise DateError(f"{text!r} names a day that does not exist") from exc


def _shift(start: date, count: int, unit: str) -> date:
    """
    Move a date by a span.

    Args:
        start: The date to move.
        count: How many units, signed.
        unit: `days`, `months` or `years`.

    Returns:
        The moved date. A month or year step that lands past the end of the target
        month clamps to that month's last day, so 1月31日 + 1 個月 is the last day of
        February rather than an error or a spill into March.

    """
    try:
        if unit == "days":
            return date.fromordinal(start.toordinal() + count)
        months = start.month - 1 + (count if unit == "months" else count * 12)
        year, month = start.year + months // 12, months % 12 + 1
        return date(year, month, min(start.day, monthrange(year, month)[1]))
    except (ValueError, OverflowError) as exc:
        raise DateError(f"{start.isoformat()} moved by {count} {unit} leaves the calendar") from exc


def compute_date(expression: str, *, today: date) -> Dated:
    """
    Evaluate a date expression and return it with its own working.

    Args:
        expression: A date, optionally followed by spans — `2026-03-01 + 10 天`,
            `民國115年3月1日 + 1 日 + 10 日`, `today - 6 個月` — or two dates joined by
            `-`, whose value is the number of days between them.
        today: What the word `today` means, passed in rather than read here so a turn
            evaluates every expression against the one date it was answered on.

    Returns:
        The resulting date or day count, alongside the expression that produced it.

    Raises:
        DateError: The expression is not parseable, names an impossible day, or holds
            anything beyond dates and spans. The caller states that it cannot work the
            date out; it never guesses one.

    """
    if not (expression := expression.strip()):
        raise DateError("empty expression")
    if (head := _DATE.match(expression)) is None:
        raise DateError(f"{expression!r} does not start with a date; write 2026-03-01, 民國115年3月1日 or today")
    current = _parse_date(head.group(), today=today)
    rest = expression[head.end():]
    # Two dates: the span between them, in days. Whitespace around the minus is free,
    # as it is for a span.
    if other := re.fullmatch(rf"\s*-\s*({_DATE.pattern})", rest):
        return Dated(value=(current - _parse_date(other.group(1), today=today)).days, basis=expression)
    consumed = 0
    for step in _SPAN.finditer(rest):
        if rest[consumed:step.start()].strip():
            raise DateError(f"cannot read {rest[consumed:step.start()]!r} in {expression!r}")
        sign, count, unit = step.groups()
        current = _shift(current, int(count) * (1 if sign == "+" else -1), _UNITS[unit.lower()])
        consumed = step.end()
    if rest[consumed:].strip():
        raise DateError(f"cannot read {rest[consumed:]!r} in {expression!r}")
    return Dated(value=current, basis=expression)
