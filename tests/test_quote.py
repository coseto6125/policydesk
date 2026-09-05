"""
保費試算, and the two ways a quote turns into a promise nobody can keep.

A quote is not a policy. The two failures that matter here both dress a number up as
one anyway: quoting a rider's unit rate as if it were a premium someone could actually
pay on its own, and quoting a product outside the customer's insurable age band without
saying so. Integration tests use the generated demo catalogue; unit tests ensure
calculation uses its structured numeric basis rather than interpreting a text label.
"""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from policydesk.agent import tools
from policydesk.agent.scenarios.quote import (
    QUOTE,
    TOOLS,
    _as_amount,
    gather,
    product_rate,
)


async def test_standing_brief_floor_price_unit_and_origin_share_one_catalog_row(db):
    from datetime import date

    member = await db.fetch_one("SELECT member_id, birth_date, occupation_class FROM member ORDER BY member_id LIMIT 1")
    today = date(2026, 9, 5)
    age = tools.insurance_age(member["birth_date"], today)
    brief = await tools.standing_brief(db, member["member_id"], today=today)
    rows = await db.fetch(
        """SELECT p.product_id, p.line, ce.unit_premium, ce.unit_label, ce.data_origin, ce.rate_unit_amount
           FROM sale_catalog ce JOIN product p USING (product_id)
           WHERE ce.on_sale AND $1::int BETWEEN ce.issue_age_min AND ce.issue_age_max
             AND $2::int <= ce.max_occupation""",
        [age, member["occupation_class"]],
    )
    for floor in brief["可投保商品線最低年繳保費"]:
        cheapest = min((row for row in rows if row["line"] == floor["線別"]),
                       key=lambda row: (row["unit_premium"], row["product_id"]))
        assert floor == {"線別": cheapest["line"], "最低": int(cheapest["unit_premium"]),
                         "單位": cheapest["unit_label"], "data_origin": cheapest["data_origin"],
                         "rate_unit_amount": cheapest["rate_unit_amount"]}


async def test_alternatives_real_catalog_binding_includes_origin(db):
    result = await tools.alternatives(db, insurance_age=100, occupation_class=7, budget=1, line="health")
    assert result["binding"]
    assert all(binding["data_origin"] in {"synthetic_demo", "unknown"} for binding in result["binding"])


CATALOG_ROWS = """WITH product(product_id, line) AS (VALUES ('cheap', 'health'), ('other', 'health')),
sale_catalog(product_id, unit_premium, unit_label, data_origin, rate_unit_amount,
             on_sale, issue_age_min, issue_age_max, max_occupation) AS (
    VALUES ('cheap', 1::numeric, 'Z unit', 'synthetic_demo', 1000, true, 0, 70, 4),
           ('other', 2::numeric, 'A unit', 'unknown', 100, true, 0, 75, 6))
"""


@pytest.mark.parametrize("line", ["health", "accident", "life", "annuity", "investment"])
async def test_demo_catalog_annual_premium_is_bounded_by_its_declared_cover_unit(line):
    from policydesk.ingest.to_postgres import _UNITS, build_catalog

    pool = AsyncMock()
    pool.fetch.return_value = [
        {"product_id": f"unit-check-{number}", "line": line, "attachment": "main", "clauses": 20}
        for number in range(50)
    ]
    await build_catalog(pool)
    entries = pool.execute_many.call_args.args[1]
    assert entries
    _, basis, low, high = _UNITS[line]
    assert 0 < low <= high <= basis * Decimal("0.30")
    for entry in entries:
        assert entry[8] == "synthetic_demo"
        assert entry[9] == basis
        assert 0 < entry[4] <= basis * Decimal("0.30")
        if line == "health":
            assert entry[9] == 1000, "fix pricing, not the cover of existing policies"
            assert "日額" not in entry[5], "health includes lump-sum products, not only daily benefits"


async def test_standing_brief_mixed_units_keeps_cheapest_product_metadata(db):
    from datetime import date

    pool = AsyncMock()
    pool.fetch_one.return_value = {"birth_date": date(1990, 1, 1), "sex": "female",
                                  "occupation": "test", "occupation_class": 1}

    async def fetch(sql, params):
        if "FROM sale_catalog" in sql:
            return await db.fetch(CATALOG_ROWS + sql, params)
        return []

    pool.fetch.side_effect = fetch
    brief = await tools.standing_brief(pool, 1, today=date(2026, 9, 5))
    assert brief["可投保商品線最低年繳保費"] == [
        {"線別": "health", "最低": 1, "單位": "Z unit", "data_origin": "synthetic_demo", "rate_unit_amount": 1000},
    ]


async def test_alternatives_mixed_catalog_sources_are_not_promoted_to_verified(db):
    pool = AsyncMock()
    pool.fetch.return_value = []

    async def fetch_one(sql, params):
        return await db.fetch_one(CATALOG_ROWS + sql, params)

    pool.fetch_one.side_effect = fetch_one
    result = await tools.alternatives(pool, insurance_age=80, occupation_class=7, budget=0, line="health")
    assert len(result["binding"]) == 3
    assert all(binding["data_origin"] == "unknown" for binding in result["binding"])


@pytest.mark.parametrize("label", ["每 100 萬元保額", "住院日額", "每計畫", "label with no number"])
async def test_product_rate_structured_basis_ignores_label(label):
    pool = AsyncMock()
    pool.fetch.return_value = [{"product_id": "demo", "requires_main": False, "unit_premium": 2000,
                               "unit_label": label, "rate_unit_amount": 1000, "data_origin": "synthetic_demo"}]
    result = await product_rate(pool, "health", amount=2000)
    assert result[0]["estimated_premium"] == 4000
    assert result[0]["data_origin"] == "synthetic_demo"


@pytest.mark.parametrize("basis", [None, 0, -1])
async def test_product_rate_missing_or_invalid_basis_returns_no_estimate(basis):
    pool = AsyncMock()
    pool.fetch.return_value = [{"product_id": "demo", "requires_main": False, "unit_premium": 2000,
                               "unit_label": "每 1,000 元保額", "rate_unit_amount": basis}]
    result = await product_rate(pool, "health", amount=2000)
    assert "estimated_premium" not in result[0]


@pytest.mark.parametrize(("budget", "expected"), [
    (20000, [(12, 19320), (10, 19500)]),
    (5000, [(3, 4830), (2, 3900)]),
])
async def test_suitable_products_budget_arithmetic_uses_current_budget(budget, expected):
    pool = AsyncMock()
    pool.fetch.side_effect = [[
        {"product_id": f"p{index}", "unit_premium": Decimal(premium), "requires_main": False,
         "rate_unit_amount": 1000, "unit_label": "不從文字猜計價單位", "data_origin": "synthetic_demo"}
        for index, premium in enumerate(("1610", "1950"))
    ], []]
    rows = await tools.suitable_products(pool, insurance_age=35, occupation_class=1, budget=budget, line="health")
    assert len(rows) == 2
    for row, (units, premium) in zip(rows, expected, strict=True):
        calculation = row["budget_calculation"]
        assert calculation == {
            "status": "standalone_rate_only", "annual_budget": budget,
            "rate_units": units, "annual_premium": premium,
            "units_expression": f"{budget} // {row['unit_premium']}",
            "premium_expression": f"{units} * {row['unit_premium']}",
        }
        assert row["data_origin"] == "synthetic_demo"


async def test_suitable_products_rider_keeps_rate_without_standalone_capacity():
    pool = AsyncMock()
    pool.fetch.side_effect = [[
        {"product_id": "rider", "unit_premium": Decimal(1610), "requires_main": True, "rate_unit_amount": 1000},
        {"product_id": "main", "unit_premium": Decimal(1950), "requires_main": False, "rate_unit_amount": 1000},
    ], []]
    rows = await tools.suitable_products(pool, insurance_age=35, occupation_class=1, budget=20000, line="health")
    assert len(rows) == 2
    assert rows[0]["unit_premium"] == Decimal(1610)
    assert rows[0]["budget_calculation"] == {"status": "main_contract_cost_unknown", "annual_budget": 20000}
    assert rows[1]["budget_calculation"]["rate_units"] == 10


@pytest.mark.parametrize(("premium", "basis"), [("0", 1000), ("-1", 1000), ("1610", None), ("1610", 0), ("1610", -1)])
async def test_suitable_products_unknown_pricing_basis_has_no_budget_estimate(premium, basis):
    pool = AsyncMock()
    pool.fetch.side_effect = [[{
        "product_id": "unknown", "unit_premium": Decimal(premium), "requires_main": False,
        "rate_unit_amount": basis, "unit_label": "每 1,000 元保額", "data_origin": "unknown",
    }], []]
    rows = await tools.suitable_products(pool, insurance_age=35, occupation_class=1, budget=5000, line="health")
    assert len(rows) == 1
    assert rows[0]["budget_calculation"] == {"status": "pricing_basis_unavailable", "annual_budget": 5000}
    assert rows[0]["data_origin"] == "unknown"


async def test_suitable_products_decimal_rate_keeps_each_products_unit_basis():
    pool = AsyncMock()
    pool.fetch.side_effect = [[
        {"product_id": "daily", "unit_premium": Decimal("1666.67"), "requires_main": False,
         "rate_unit_amount": 1000, "unit_label": "每日 1,000 元住院日額"},
        {"product_id": "lump", "unit_premium": Decimal("1666.67"), "requires_main": False,
         "rate_unit_amount": 1000000, "unit_label": "每 100 萬元保額"},
    ], []]
    rows = await tools.suitable_products(pool, insurance_age=35, occupation_class=1, budget=5000, line="health")
    assert len(rows) == 2
    assert [(row["rate_unit_amount"], row["unit_label"]) for row in rows] == [
        (1000, "每日 1,000 元住院日額"), (1000000, "每 100 萬元保額"),
    ]
    for row in rows:
        assert row["budget_calculation"] == {
            "status": "standalone_rate_only", "annual_budget": 5000,
            "rate_units": 2, "annual_premium": 3333,
            "units_expression": "5000 // 1666.67", "premium_expression": "2 * 1666.67",
        }


@pytest.mark.parametrize("budget", [20000, 5000])
async def test_suitable_products_real_catalog_estimates_agree_with_stored_rates(db, budget):
    # Cover both main and rider calculations without depending on the cheapest five's composition.
    catalog_size = await db.fetch_val("SELECT count(*) FROM sale_catalog")
    rows = await tools.suitable_products(
        db, insurance_age=35, occupation_class=1, budget=budget, line="health", limit=catalog_size,
    )
    assert rows
    stored = {row["product_id"]: row for row in await db.fetch(
        """SELECT product_id, unit_premium, rate_unit_amount, unit_label, requires_main, data_origin
           FROM sale_catalog WHERE product_id = ANY($1::text[])""",
        [[row["product_id"] for row in rows]],
    )}
    assert {row["requires_main"] for row in stored.values()} == {False, True}
    for row in rows:
        rate = stored[row["product_id"]]
        assert all(row[key] == value for key, value in rate.items())
        calculation = row["budget_calculation"]
        assert calculation["annual_budget"] == budget
        if rate["requires_main"]:
            assert calculation == {"status": "main_contract_cost_unknown", "annual_budget": budget}
        else:
            assert calculation["status"] == "standalone_rate_only"
            units = calculation["rate_units"]
            assert units * rate["unit_premium"] <= budget < (units + 1) * rate["unit_premium"]
            assert calculation["annual_premium"] == int((units * rate["unit_premium"]).quantize(Decimal(1)))


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
    assert row["data_origin"] == "synthetic_demo"
    assert row["rate_unit_amount"] == 1000
    # This is the demo pricing basis, not evidence of the contract's benefit type.
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
