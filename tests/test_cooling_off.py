"""
契約撤銷權, and the trap of stating a period nobody read off a row.

The premise this scenario started from was wrong: 保險法施行細則 §4 has nothing to do
with a cooling-off period — checked directly against `statute_article` in
`test_insurance_act_rules_article_4_is_not_a_cooling_off_clause` below, kept as a
regression test rather than a comment, because the whole scenario would be built on a
fabricated citation if this ever silently changed back.

The right actually lives in `clause`, and it is not the same ten days everywhere: one
on-sale product's own contract states fourteen. The tests assert that property directly
against the corpus, not against a fixture that only ever says ten.
"""


from policydesk.agent import tools
from policydesk.agent.scenarios.cooling_off import (
    COOLING_OFF,
    TOOLS,
    cooling_off_clause,
    gather,
    member_rescission,
)


async def test_insurance_act_rules_article_4_is_not_a_cooling_off_clause(db):
    # The brief's original premise. Kept as a regression test: if this corpus is ever
    # reingested and art.4 changes, the scenario built on top of it needs to be
    # rechecked rather than trusted from a comment nobody re-reads.
    row = await db.fetch_one(
        "SELECT verbatim FROM statute_article WHERE statute_id = 'insurance_act_rules' AND doc_id = 'art.4'"
    )
    assert row is not None
    assert "撤銷" not in row["verbatim"]
    assert "猶豫" not in row["verbatim"]


async def test_no_statute_article_states_a_cooling_off_period(db):
    rows = await db.fetch(
        "SELECT doc_id FROM statute_article WHERE verbatim ILIKE '%猶豫期%' OR verbatim ILIKE '%契約撤銷權%'"
    )
    assert not rows, "if this ever finds something, the scenario should cite it instead of the clause corpus"


async def test_cooling_off_clause_picks_a_stable_representative_row(db):
    rows = await cooling_off_clause(db)
    assert rows
    row = rows[0]
    assert row["heading"] == "契約撤銷權"
    assert row["product_id"] == "006657a0a25a", "the deterministic first product_id in the on-sale corpus"
    assert "撤銷" in row["verbatim"]


async def test_the_rescission_period_is_not_uniform_across_on_sale_products(db):
    # 238 of 239 on-sale rescission clauses say 十日; one says 十四日. The scenario
    # must never state "ten days" as a fact for that reason alone.
    rows = await db.fetch(
        """SELECT c.verbatim FROM clause c JOIN catalog_entry ce USING (product_id)
           WHERE c.heading = '契約撤銷權' AND ce.on_sale"""
    )
    assert rows
    ten = sum("十日" in r["verbatim"] for r in rows)
    fourteen = sum("十四日" in r["verbatim"] for r in rows)
    assert ten > 0
    assert fourteen > 0
    assert ten < len(rows), "the corpus is not one uniform period"


async def test_member_rescission_returns_only_policies_that_carry_the_clause(db):
    # Member 4 holds two policies; only one of the two products carries this clause.
    # The other must not be backfilled with a different product's wording.
    rows = await member_rescission(db, 4, today=await db.fetch_val("SELECT now()::date"))
    product_ids = {r["product_id"] for r in rows}
    assert "5d96c895b243" in product_ids
    assert "b0f63e06eb6f" not in product_ids


async def test_member_rescission_carries_the_policy_it_belongs_to(db):
    rows = await member_rescission(db, 4, today=await db.fetch_val("SELECT now()::date"))
    row = next(r for r in rows if r["product_id"] == "5d96c895b243")
    assert row["policy_number"] == "CL6405-266987"
    assert row["effective_at"] is not None
    assert "days_in_force" in row


async def test_member_rescission_is_empty_for_someone_with_no_policies(db):
    assert await member_rescission(db, -1, today=await db.fetch_val("SELECT now()::date")) == []


async def test_gather_answers_the_public_half_with_no_member_id(db):
    facts = await gather(db, {})
    assert facts["cooling_off_clause"]
    assert "member_rescission" not in facts, "no member_id means no policy row is read"
    assert facts["_allowed_clauses"] == {r["clause_id"] for r in facts["cooling_off_clause"]}


async def test_gather_adds_the_members_own_clause_once_confirmed(db):
    facts = await gather(db, {}, member_id=4)
    assert facts["member_rescission"]
    assert facts["_allowed_clauses"] >= {r["clause_id"] for r in facts["member_rescission"]}
    assert facts["_allowed_clauses"] >= {r["clause_id"] for r in facts["cooling_off_clause"]}


async def test_gather_withholds_the_member_query_the_gate_did_not_permit(db):
    # member_id=4 is real and does hold a rescission clause — the property under test
    # is that `allowed` stopped `member_rescission` from running at all, not that no
    # member happened to be given.
    facts = await gather(db, {}, member_id=4, allowed=frozenset({"cooling_off_clause"}))
    assert facts["cooling_off_clause"]
    assert "member_rescission" not in facts
    assert facts["_allowed_clauses"] == {r["clause_id"] for r in facts["cooling_off_clause"]}


def test_member_rescission_carries_its_own_identity_mark():
    assert getattr(TOOLS["member_rescission"], "requires_identity", False) is True


def test_cooling_off_clause_carries_no_identity_mark():
    assert not getattr(TOOLS["cooling_off_clause"], "requires_identity", False)


def test_the_gate_is_derived_correctly_through_this_modules_own_tools():
    from importlib import import_module

    owner = import_module(COOLING_OFF.tools_module)
    assert tools.reads_identity(COOLING_OFF.tools, owner=owner), "member_rescission is in the tool list"
    assert not tools.reads_identity(("cooling_off_clause",), owner=owner)


def test_permitted_withholds_only_the_member_reading_tool_unconfirmed():
    from importlib import import_module

    owner = import_module(COOLING_OFF.tools_module)
    assert tools.permitted(COOLING_OFF.tools, owner=owner, confirmed=False) == {"cooling_off_clause"}
    assert tools.permitted(COOLING_OFF.tools, owner=owner, confirmed=True) == set(COOLING_OFF.tools)


def test_the_scenario_module_contract_is_what_the_executor_calls():
    from policydesk.agent.scenarios import cooling_off

    code = cooling_off.gather.__code__
    assert "retriever" in code.co_varnames
    assert code.co_flags & 0x08, "member_id and today are passed to every scenario module"


def test_cooling_off_forbids_stating_a_fixed_ten_day_period():
    assert "不是固定十天" in COOLING_OFF.injection
    assert "不可以憑印象講十天" in COOLING_OFF.injection


def test_cooling_off_forbids_computing_a_deadline():
    assert "不可以自己算出撤銷期限是哪一天" in COOLING_OFF.injection
    assert "已經過期了" in COOLING_OFF.injection


def test_cooling_off_names_receipt_not_effective_date_as_the_trigger():
    assert "保險單送達的翌日" in COOLING_OFF.injection
    assert "不是保單生效日" in COOLING_OFF.injection


def test_cooling_off_distinguishes_the_general_clause_from_the_members_own():
    assert "一般約定" in COOLING_OFF.injection
    assert "member_rescission" in COOLING_OFF.injection


def test_cooling_off_has_no_params_because_it_needs_none_from_the_customer():
    assert COOLING_OFF.params == ()


def test_a_scenario_module_imports_cleanly_from_any_entry_point():
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-c", "import policydesk.agent.scenarios.cooling_off"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr.strip().splitlines()[-1:]
