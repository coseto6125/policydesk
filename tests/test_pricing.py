"""
What a call costs, and what it costs when nobody has priced its model.

The column existed from the first migration and stayed NULL for every row, so the
console's 成本 field was blank on a deployment that had spent real money. These tests
hold the two halves of the fix: the arithmetic, and the distinction between a call that
was free and a call nobody priced — which is the difference between a bill and a bill
with rows silently missing from it.
"""

from decimal import Decimal

import pytest

from policydesk.llm import pricing
from policydesk.llm.provider import Completion

RATES = {
    "priced-with-cache": {"input": 1.0, "cached": 0.1, "output": 4.0},
    "priced-no-cache": {"input": 1.0, "output": 4.0},
    "free": {"input": 0.0, "output": 0.0},
}
"""Round numbers, so an arithmetic slip shows as a wrong figure rather than as a figure
that could be either. Per 1,000 tokens, the unit the file holds."""


@pytest.fixture(autouse=True)
def table(monkeypatch):
    monkeypatch.setattr(pricing, "_RATES", RATES)
    monkeypatch.setattr(pricing, "_WARNED", set())


def test_price_unknown_model_returns_none_not_zero():
    # Zero is a claim that the call was free. None is the true statement about a model
    # with no rate, and the only one the console can render as 未定價.
    assert pricing.price("never-heard-of-it", prompt_tokens=1000, completion_tokens=1000) is None


def test_price_free_model_returns_zero_not_none():
    # The other direction, and it is a different fact: a model priced at zero HAS a
    # rate. Collapsing the two would make a mocked run indistinguishable from an
    # unpriced one.
    assert pricing.price("free", prompt_tokens=1000, completion_tokens=1000) == 0.0


def test_price_charges_input_and_output_at_their_own_rates():
    # 2,000 prompt at $1.00/1k + 500 completion at $4.00/1k = 2.00 + 2.00
    assert pricing.price("priced-no-cache", prompt_tokens=2000, completion_tokens=500) == pytest.approx(4.0)


def test_price_bills_the_cached_slice_once_at_the_cached_rate():
    # 1,000 prompt of which 800 were cache reads: 200 at $1.00/1k + 800 at $0.10/1k.
    # The failure this guards is billing the 800 twice — once as prompt, once as cache.
    got = pricing.price("priced-with-cache", prompt_tokens=1000, completion_tokens=0, cached_tokens=800)
    assert got == pytest.approx(0.2 + 0.08)


def test_price_without_a_cached_rate_bills_cache_at_the_input_rate():
    # No `cached` entry means the provider offers no discount. Halving the bill on a
    # guess would understate what the key was charged.
    got = pricing.price("priced-no-cache", prompt_tokens=1000, completion_tokens=0, cached_tokens=800)
    assert got == pytest.approx(1.0)


def test_price_clamps_a_cached_count_above_the_prompt_count():
    # A provider reporting more cached tokens than prompt tokens is a bad reading, not
    # a negative bill. The uncached slice floors at zero.
    got = pricing.price("priced-with-cache", prompt_tokens=100, completion_tokens=0, cached_tokens=9999)
    assert got == pytest.approx(0.01)


def test_cost_returns_decimal_because_the_column_is_numeric():
    # psqlpy binds a numeric column from Decimal ONLY — an int, a float or a str all
    # fail with `insufficient data left in message`, which names neither the column nor
    # the type. This test is the one that catches it before a live turn does.
    got = pricing.cost(Completion(text="", model="priced-no-cache", prompt_tokens=1000, completion_tokens=500))
    assert isinstance(got, Decimal)
    assert got == Decimal("3.000000")


def test_cost_of_an_unpriced_model_is_none():
    assert pricing.cost(Completion(text="", model="never-heard-of-it", prompt_tokens=10, completion_tokens=10)) is None
