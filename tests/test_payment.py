"""
繳費、寬限期, and the deadline this desk must not compute.

An unpaid instalment is a countdown, not a balance. 保險法 §116 counts thirty days from the
催告 reaching the customer — a date this database does not hold — so the one thing a reply
here must never do is state how long they have left.
"""

from datetime import date, timedelta

import pytest

from policydesk.agent import tools
from policydesk.agent.scenarios import payment
from policydesk.agent.scenarios.payment import GRACE_ARTICLE, MODE_LABEL, PAYMENT, grace_rule, payment_state
from policydesk.core.db import Database


@pytest.fixture(scope="module")
async def db():
    pool = Database()
    try:
        await pool.fetch_val("SELECT 1")
    except Exception:
        pytest.skip("policydesk-pg is not up")
    return pool


@pytest.fixture(scope="module")
async def in_grace(db):
    """
    Find a member with an instalment that fell due and was not paid.

    Returns:
        Their member id, or a skip when nobody is inside a grace period.

    """
    found = await db.fetch_val(
        """SELECT po.member_id FROM premium_payment pp JOIN policy po USING (policy_id)
           WHERE pp.paid_at IS NULL LIMIT 1"""
    )
    if found is None:
        pytest.skip("nobody is inside a grace period")
    return int(found)


async def test_the_grace_rule_is_116_and_only_116(db):
    """
    `search_statute` widens a hit to its siblings, which §116 needs — I states the rule, II
    the reinstatement window, III the effect. It also brought back §120, 保單借款, because a
    policy loan can stop a contract too and the vocabulary overlaps. A different provision
    under this key reads as more support for the same sentence, which is exactly what the
    beneficiary scenario had to be split to prevent.
    """
    rows = await grace_rule(db)
    assert rows, "the provision the whole scenario rests on came back empty"
    assert all(f"第{GRACE_ARTICLE}條" in row["citation"] for row in rows), [r["citation"] for r in rows]


async def test_an_unpaid_instalment_is_reported_with_the_date_it_fell_due(db, in_grace):
    rows = await payment_state(db, in_grace, today=date(2026, 8, 29))
    overdue = [r for r in rows if r["unpaid_due_at"]]
    assert overdue, "the fixture member has no unpaid instalment"
    for row in overdue:
        assert row["overdue_days"] is not None
        assert row["overdue_days"] >= 0, "an instalment due in the future is not overdue"
        assert row["premium_mode"] in MODE_LABEL


async def test_a_member_who_owes_nothing_says_so_rather_than_nothing(db):
    # Every one of their policies must be clear, not just one — a member owing on a second
    # contract is not the all-clear case this asserts.
    clean = await db.fetch_val(
        """SELECT po.member_id FROM policy po
           GROUP BY po.member_id
           HAVING NOT EXISTS (
               SELECT 1 FROM premium_payment pp JOIN policy p2 USING (policy_id)
               WHERE p2.member_id = po.member_id AND pp.paid_at IS NULL
           )
           LIMIT 1"""
    )
    if clean is None:
        pytest.skip("everybody owes something")
    rows = await payment_state(db, int(clean), today=date(2026, 8, 29))
    assert rows, "a member with policies returned no payment state"
    assert all(r["unpaid_due_at"] is None for r in rows)
    assert all(r["status"].startswith("paid_up") for r in rows), "the row itself must say all-clear"


def test_the_deadline_is_computed_from_the_notice_on_record_and_never_guessed():
    """
    The thirty days run from the 催告 arriving. With that date on record the row carries
    the deadline, computed in SQL; without it the desk asks. The model computes nothing.
    """
    assert "When grace_days_left is empty" in PAYMENT.injection
    assert "ask the customer when it arrived" in PAYMENT.injection


async def test_a_recorded_notice_yields_a_deadline_and_an_absent_one_yields_none(db):
    with_notice = await db.fetch_one(
        """SELECT po.member_id, pp.notice_arrived_at, po.policy_number FROM premium_payment pp
           JOIN policy po USING (policy_id)
           WHERE pp.paid_at IS NULL AND pp.notice_arrived_at IS NOT NULL AND po.lapsed_at IS NULL LIMIT 1"""
    )
    if with_notice is None:
        pytest.skip("no unpaid instalment carries a 催告 date")
    today = date(2026, 9, 5)
    rows = await payment_state(db, int(with_notice["member_id"]), today=today)
    row = next(r for r in rows if r["policy_number"] == with_notice["policy_number"])
    assert row["grace_ends_at"] == with_notice["notice_arrived_at"] + timedelta(days=payment.GRACE_DAYS)
    assert row["grace_days_left"] == (row["grace_ends_at"] - today).days
    assert row["status"].startswith("unpaid")
    assert "催告已送達" in row["status"]

    without = await db.fetch_one(
        """SELECT po.member_id, po.policy_number FROM premium_payment pp JOIN policy po USING (policy_id)
           WHERE pp.paid_at IS NULL AND pp.notice_arrived_at IS NULL AND po.lapsed_at IS NULL LIMIT 1"""
    )
    if without is None:
        pytest.skip("every unpaid instalment carries a 催告 date")
    rows = await payment_state(db, int(without["member_id"]), today=today)
    row = next(r for r in rows if r["policy_number"] == without["policy_number"])
    assert row["grace_ends_at"] is None
    assert row["grace_days_left"] is None
    assert "尚無催告送達紀錄" in row["status"]


def test_the_public_half_survives_the_gate():
    allowed = tools.permitted(PAYMENT.tools, owner=payment, confirmed=False)
    assert allowed == {"grace_rule"}, allowed
    assert tools.permitted(PAYMENT.tools, owner=payment, confirmed=True) == set(PAYMENT.tools)


async def test_a_withheld_session_gets_the_rule_and_none_of_the_book(db, in_grace):
    allowed = tools.permitted(PAYMENT.tools, owner=payment, confirmed=False)
    facts = await payment.gather(db, {}, member_id=in_grace, today=date(2026, 8, 29), allowed=allowed)
    assert facts["grace_rule"], "the law is public and must still answer"
    assert "payment_state" not in facts
    assert "payment_history" not in facts


async def test_an_instalment_is_a_whole_number_of_dollars(db):
    # 408.33 元 reached a customer in a live run. No insurer bills a third of a dollar; the
    # fraction was an artefact of dividing an annual rate by twelve.
    fractional = await db.fetch(
        "SELECT amount FROM premium_payment WHERE amount <> round(amount) LIMIT 5"
    )
    assert not fractional, f"a premium is billed in whole dollars: {fractional}"


# ---------------------------------------------------------------- 改繳別

async def test_a_mode_change_reads_the_contracts_own_words_on_how_premiums_are_paid(db):
    """
    可以改成年繳嗎 was answered with the ledger three times running, because the scenario
    held nothing else about a mode. The contract's 交付 article is what it can show.
    """
    member_id = await db.fetch_val(
        "SELECT member_id FROM policy WHERE lapsed_at IS NULL GROUP BY member_id ORDER BY count(*) DESC LIMIT 1"
    )
    rows = await payment.mode_change_rule(db, member_id)
    assert rows, "a member with policies in force gets at least their 交付 article"
    assert all(r["product_name"] for r in rows), "each clause names the contract it is from"
    assert any("繳" in r["heading"] or "交付" in r["heading"] for r in rows)


def test_a_named_policy_scopes_the_rows_and_a_stranger_leaves_them_whole():
    """
    可以改成年繳嗎 → CL6490628670 → 658746張呢: the number is the whole message, and the
    reply is about that policy. A number matching nothing keeps every row, so the reply can
    say which policies exist.
    """
    facts = {
        "payment_state": [{"policy_number": "CL9926-658746", "premium_mode": "monthly"},
                          {"policy_number": "CL6490-628670", "premium_mode": "annual"}],
        "grace_rule": [{"citation": "〔保險法 第116條第1項〕"}],
    }
    assert [r["policy_number"] for r in payment._scoped(facts, "658746")["payment_state"]] == ["CL9926-658746"]
    assert [r["policy_number"] for r in payment._scoped(facts, "cl6490 628670")["payment_state"]] == ["CL6490-628670"]
    assert payment._scoped(facts, "999999")["payment_state"] == facts["payment_state"]
    assert payment._scoped(facts, "658746")["grace_rule"] == facts["grace_rule"]
    assert [p.name for p in PAYMENT.params] == ["policy_number"]


def test_impatience_at_being_misread_is_not_routed_to_the_complaint_desk():
    """「我是要改年繳欸，你不懂嗎?」 got 金融消費者保護法 §13 and a 申訴 chip."""
    from policydesk.agent.scenarios.soothe import SOOTHE

    assert "不耐煩地重述" in SOOTHE.description
    assert "你不懂嗎" in SOOTHE.description
