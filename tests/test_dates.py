"""
The date tool's grammar is the boundary, so it gets the model's likely mistakes.

An expression arrives from a model. Every test that starts "rejects" is a thing a model
could write, deliberately or by accident, and each must raise rather than produce a date.
"""

from datetime import date, datetime

import pytest

from policydesk.skills.dates import DateError, compute_date, today

TODAY = date(2026, 9, 6)


def test_compute_date_adds_days_to_an_iso_date():
    result = compute_date("2026-03-01 + 10 天", today=TODAY)
    assert result.value == date(2026, 3, 11)
    assert result.basis == "2026-03-01 + 10 天"
    assert result.text == "2026-03-11"


def test_compute_date_chains_spans_left_to_right():
    assert compute_date("2026-03-01 + 1 日 + 10 日", today=TODAY).value == date(2026, 3, 12)
    assert compute_date("2026-03-01 + 1 個月 - 1 日", today=TODAY).value == date(2026, 3, 31)


def test_compute_date_the_day_after_delivery_plus_ten_days_ends_on_the_eleventh():
    """
    保險單送達的翌日起算十日: delivery on the 1st, the 2nd is day one, the 11th is day
    ten. The end of the period is delivery + 10, not delivery + 1 + 10.
    """
    assert compute_date("2026-03-01 + 10 日", today=TODAY).value == date(2026, 3, 11)


def test_compute_date_reads_the_roc_calendar_a_policy_prints():
    assert compute_date("民國115年3月1日 + 10 日", today=TODAY).value == date(2026, 3, 11)
    assert compute_date("115/03/01", today=TODAY).value == date(2026, 3, 1)


def test_compute_date_resolves_today_to_the_date_given():
    assert compute_date("today - 6 個月", today=TODAY).value == date(2026, 3, 6)
    assert compute_date("today", today=TODAY).value == TODAY


def test_compute_date_clamps_a_month_step_to_the_end_of_the_target_month():
    """1月31日 + 1 個月 is the last day of February, not an error and not March 3rd."""
    assert compute_date("2026-01-31 + 1 個月", today=TODAY).value == date(2026, 2, 28)
    assert compute_date("2024-01-31 + 1 month", today=TODAY).value == date(2024, 2, 29)


def test_compute_date_steps_years_across_a_leap_day():
    assert compute_date("2024-02-29 + 1 年", today=TODAY).value == date(2025, 2, 28)


def test_compute_date_counts_the_days_between_two_dates():
    result = compute_date("2026-03-11 - 2026-03-01", today=TODAY)
    assert result.value == 10
    assert result.text == "10 天"
    assert compute_date("today - 民國115年1月5日", today=TODAY).value == 244
    assert compute_date("2026-03-11-2026-03-01", today=TODAY).value == 10
    assert compute_date("today\t-\t2026-03-01", today=TODAY).value == 189


def test_compute_date_rejects_a_step_that_leaves_the_calendar():
    for expression in ("9999-12-31 + 1 日", "0001-01-01 - 1 日", "2026-09-06 + 999999999 年"):
        with pytest.raises(DateError):
            compute_date(expression, today=TODAY)


def test_compute_date_rejects_a_span_too_long_for_int_as_a_date_error():
    """Python refuses to parse 4,301 digits; that refusal must reach the caller as DateError."""
    with pytest.raises(DateError):
        compute_date("today + " + "9" * 4301 + " 日", today=TODAY)


@pytest.mark.parametrize("unit", ["days", "d", "day", "日"])
def test_compute_date_accepts_english_and_chinese_units(unit):
    assert compute_date(f"2026-03-01 + 2 {unit}", today=TODAY).value == date(2026, 3, 3)


@pytest.mark.parametrize("expression", [
    "",
    "十日後",
    "2026-03-01 + 十 日",
    "2026-02-30 + 1 日",
    "2026-03-01 + 10",
    "2026-03-01 * 2",
    "2026-03-01 + 10 日 - 2026-01-01",
    "__import__('os')",
    "today + 1 週",
])
def test_compute_date_rejects_what_is_not_date_arithmetic(expression):
    with pytest.raises(DateError):
        compute_date(expression, today=TODAY)


def test_today_is_the_date_in_taiwan(monkeypatch):
    """00:00 to 08:00 in Taipei is the previous calendar day in UTC: pin that window."""
    from datetime import UTC

    from policydesk.skills import dates

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None) -> datetime:
            return datetime(2026, 9, 5, 17, 30, tzinfo=UTC).astimezone(tz)  # 01:30 in Taipei, 2026-09-06

    monkeypatch.setattr(dates, "datetime", Clock)
    assert today() == date(2026, 9, 6)
