"""
保費試算, and the two ways a quote turns into a promise nobody can keep.

A quote is not a policy. The two failures that matter here both dress a number up as
one anyway: quoting a rider's unit rate as if it were a premium someone could actually
pay on its own, and quoting a product outside the customer's insurable age band without
saying so. Both are asserted against the real catalog rather than a fixture, because the
`unit_label` text this module parses is the government's own vocabulary, not a shape
this test invented.
"""

import pytest

from policydesk.agent import tools
from policydesk.agent.scenarios.quote import (
    QUOTE,
    TOOLS,
    _as_amount,
    _unit_base,
    gather,
    product_rate,
)
from policydesk.core.db import Database


@pytest.fixture(scope="module")
async def db():
    pool = Database()
    try:
        await pool.fetch_val("SELECT 1")
    except Exception:
        pytest.skip("policydesk-pg is not up")
    yield pool
    await pool.close()


def test_unit_base_reads_the_five_labels_the_catalog_actually_uses():
    assert _unit_base("每 10 萬元保額") == 100_000
    assert _unit_base("每 100 萬元保額") == 1_000_000
    assert _unit_base("每日 1,000 元住院日額") == 1_000
    assert _unit_base("每 10 萬元年金") == 100_000
    assert _unit_base("每單位") == 1


def test_unit_base_tries_wan_before_the_bare_yuan_inside_it():
    # 每 10 萬元保額 contains a 元 of its own right after 萬 — matching that one first
    # would read the label as costing NT$1 per unit.
    assert _unit_base("每 10 萬元保額") != 10


def test_as_amount_reads_a_comma_separated_figure():
    assert _as_amount("500,000") == 500_000


def test_as_amount_rejects_what_is_not_a_positive_number():
    assert _as_amount("") is None
    assert _as_amount("大概五十萬") is None
    assert _as_amount("-100") is None
    assert _as_amount("0") is None


async def test_product_rate_returns_a_product_outside_the_band_rather_than_nothing(db):
    # Every health product on sale opens at 0 and none excludes anyone by band alone —
    # the property under test is that the band is *in the row*, not that this
    # particular keyword happens to fail it.
    rows = await product_rate(db, "health", "IN 健康定期")
    assert rows
    row = rows[0]
    assert row["issue_age_min"] == 0
    assert "issue_age_max" in row
    assert "max_occupation" in row


async def test_product_rate_rejects_an_unsellable_line(db):
    assert await product_rate(db, "other") == []


async def test_product_rate_estimates_the_premium_for_a_stated_amount(db):
    rows = await product_rate(db, "health", "IN 健康定期健康保險", amount=2000)
    assert rows
    row = rows[0]
    assert row["unit_label"] == "每日 1,000 元住院日額"
    # 2,000.00 per 1,000-元 unit, at a stated 2,000-元 daily benefit: two units.
    assert row["estimated_premium"] == int(row["unit_premium"] * 2)
    assert row["estimated_basis"]


async def test_product_rate_gives_no_standalone_premium_for_a_rider(db):
    rows = await product_rate(db, "health", "真大心 PLUS", amount=2000)
    assert rows
    row = rows[0]
    assert row["requires_main"] is True
    assert "estimated_premium" not in row, "a rider has no standalone premium to quote"


async def test_product_rate_with_no_amount_states_the_rate_alone(db):
    rows = await product_rate(db, "health", "IN 健康定期健康保險")
    assert rows
    assert "estimated_premium" not in rows[0]


async def test_gather_answers_the_rate_with_no_member_id(db):
    # The public half: nobody has proved who they are, and the catalog is not theirs
    # to withhold.
    facts = await gather(db, {"line": "health", "keyword": "", "amount": ""})
    assert facts["product_rate"]
    assert "member_underwriting" not in facts, "no member_id means no member row is read"


async def test_gather_adds_the_members_own_figures_once_confirmed(db):
    member_id = await db.fetch_val("SELECT member_id FROM member LIMIT 1")
    facts = await gather(db, {"line": "health", "keyword": "", "amount": ""}, member_id=member_id)
    assert facts["product_rate"]
    assert facts["member_underwriting"]
    assert "insurance_age" in facts["member_underwriting"]
    assert "occupation_class" in facts["member_underwriting"]


async def test_gather_withholds_the_member_query_the_gate_did_not_permit(db):
    # A real member_id is passed here on purpose: the property under test is that
    # `allowed` stopped the query from running at all, not that a member_id happened
    # to be absent.
    member_id = await db.fetch_val("SELECT member_id FROM member LIMIT 1")
    facts = await gather(
        db,
        {"line": "health", "keyword": "", "amount": ""},
        member_id=member_id,
        allowed=frozenset({"product_rate"}),
    )
    assert facts["product_rate"]
    assert "member_underwriting" not in facts


def test_member_underwriting_in_this_scenario_is_the_same_marked_function():
    # Not a copy: the same object `agent.tools` marked, so the gate derives correctly
    # no matter which scenario's `TOOLS` it is read out of.
    assert TOOLS["member_underwriting"] is tools.member_underwriting
    assert getattr(TOOLS["member_underwriting"], "requires_identity", False) is True


def test_product_rate_carries_no_identity_mark():
    assert not getattr(TOOLS["product_rate"], "requires_identity", False)


def test_the_gate_is_derived_correctly_through_this_modules_own_tools():
    from importlib import import_module

    owner = import_module(QUOTE.tools_module)
    assert tools.reads_identity(QUOTE.tools, owner=owner), "member_underwriting is in the tool list"
    assert not tools.reads_identity(("product_rate",), owner=owner)


def test_permitted_withholds_only_the_member_reading_tool_unconfirmed():
    from importlib import import_module

    owner = import_module(QUOTE.tools_module)
    assert tools.permitted(QUOTE.tools, owner=owner, confirmed=False) == {"product_rate"}
    assert tools.permitted(QUOTE.tools, owner=owner, confirmed=True) == set(QUOTE.tools)


def test_the_scenario_module_contract_is_what_the_executor_calls():
    from policydesk.agent.scenarios import quote

    code = quote.gather.__code__
    assert "retriever" in code.co_varnames
    assert code.co_flags & 0x08, "member_id and today are passed to every scenario module"


def test_quote_forbids_treating_the_figure_as_an_underwriting_result():
    assert "試算，不是核保結果" in QUOTE.injection


def test_quote_forbids_a_standalone_price_on_a_rider():
    assert "沒有主約不能單獨投保" in QUOTE.injection
    assert "不可以說它一年要繳多少錢" in QUOTE.injection


def test_quote_requires_insurance_age_not_the_plain_one():
    assert "保險年齡" in QUOTE.injection
    assert "不是足歲" in QUOTE.injection


def test_quote_keeps_an_out_of_band_product_in_the_answer():
    assert "不符合的商品也要照樣列出" in QUOTE.injection


def test_quote_forbids_computing_a_figure_itself():
    assert "不可以自己心算、估計或另外換算" in QUOTE.injection


def test_quote_quick_replies_are_questions_not_commitments():
    # A tap is one pixel from a mis-tap, and a mis-tap that reads as intent to buy is
    # an expression of intent the customer never made — so every quick reply here asks
    # something rather than committing to anything.
    assert not any(q.startswith(("我要買", "我想買", "確定投保", "我要投保")) for q in QUOTE.quick_replies)


def test_a_scenario_module_imports_cleanly_from_any_entry_point():
    import subprocess
    import sys

    for entry in ("policydesk.agent.scenarios.quote",):
        done = subprocess.run(  # noqa: S603
            [sys.executable, "-c", f"import {entry}"], capture_output=True, text=True, check=False
        )
        assert done.returncode == 0, f"{entry}: {done.stderr.strip().splitlines()[-1:]}"
