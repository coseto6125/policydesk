"""
繳費、寬限期, and the deadline this desk must not compute.

An unpaid instalment is a countdown, not a balance. 保險法 §116 counts thirty days from the
催告 reaching the customer — a date this database does not hold — so the one thing a reply
here must never do is state how long they have left.
"""

from datetime import date

import pytest

from policydesk.agent import tools
from policydesk.agent.scenarios import payment
from policydesk.agent.scenarios.payment import GRACE_ARTICLE, MODE_LABEL, PAYMENT, grace_rule, payment_state
from policydesk.core.db import Database

pytestmark = pytest.mark.asyncio


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
    assert "都繳齊了" in PAYMENT.injection, "the model must be told what all-clear looks like"


def test_the_injection_forbids_computing_the_deadline():
    """
    The thirty days run from the 催告 arriving, and this desk does not hold that date.

    `cooling_off` has the same shape for 保險單送達的翌日 and the same reason: a deadline
    computed from a date the database does not have is a confident wrong answer about
    whether somebody still has cover.
    """
    assert "不是從到期日起算" in PAYMENT.injection
    assert "絕對不要算出一個剩幾天" in PAYMENT.injection
    assert "overdue_days" in PAYMENT.injection, "the one number on the row must be given its meaning"


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
