"""
The allow-list is the security boundary, so it gets adversarial cases.

An expression arrives from a model, so it is untrusted input. Every test below that
starts "rejects" is a thing a model could emit, deliberately or by accident.
"""

import pytest

from policydesk.skills.calculator import CalculationError, calculate


def test_calculate_daily_benefit_times_nights():
    result = calculate("2000 * 4")
    assert result.amount == 8000
    assert result.basis == "2000 * 4"


def test_calculate_applies_a_per_year_cap_with_min():
    """門診手術醫療保險金的給付以十次為限 — twelve occurrences pay for ten."""
    assert calculate("min(12, 10) * 3 * 2000").amount == 60000


def test_calculate_rounds_to_whole_twd():
    assert calculate("2000 / 3").amount == 667


def test_calculate_keeps_decimal_precision_across_terms():
    """In binary floating point this is 0.30000000000000004; here it is 0.3."""
    assert calculate("(0.1 + 0.2) * 10").amount == 3


def test_calculate_floored_division_matches_python():
    assert calculate("-1 // 9").amount == -1


def test_calculate_floored_modulo_matches_python():
    assert calculate("-1 % 9").amount == 8


def test_calculate_rejects_a_name():
    with pytest.raises(CalculationError, match="not allowed"):
        calculate("daily_rate * 4")


def test_calculate_rejects_an_attribute_access():
    with pytest.raises(CalculationError, match="not allowed"):
        calculate("(2000).real * 4")


def test_calculate_rejects_an_unlisted_call():
    with pytest.raises(CalculationError, match="not allowed"):
        calculate("__import__('os').system('ls')")


def test_calculate_rejects_a_comparison():
    with pytest.raises(CalculationError, match="not allowed"):
        calculate("2000 > 1000")


def test_calculate_rejects_a_boolean():
    with pytest.raises(CalculationError, match="not an amount"):
        calculate("True")


def test_calculate_rejects_division_by_zero():
    with pytest.raises(CalculationError, match="division by zero"):
        calculate("2000 / 0")


def test_calculate_rejects_an_empty_expression():
    with pytest.raises(CalculationError, match="empty"):
        calculate("   ")


def test_calculate_rejects_unparseable_text():
    with pytest.raises(CalculationError, match="cannot parse"):
        calculate("2000 *")


def test_calculate_rejects_a_keyword_argument():
    """round(x, n=1) would reach the allow-listed function by an unchecked path."""
    with pytest.raises(CalculationError, match="not allowed"):
        calculate("round(2000.55, n=1)")
