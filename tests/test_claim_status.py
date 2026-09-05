"""
The 理賠進度查詢 scenario: a desk that reports a claim's stage, and never its outcome.

Two failures matter more here than a typo would. Softening a recorded `outcome =
'declined'` into "還在審核中" hides that the file is closed and invites a customer to
wait for a reversal that is not coming. And a bare `paid_amount` reaching a customer is
the same defect `insured_amount` exists to prevent for `sum_insured` — a number with no
unit is a wrong number, not merely an unlabelled one.
"""

from decimal import Decimal

from policydesk.agent import tools
from policydesk.agent.scenarios import claim_status
from policydesk.agent.scenarios.claim_status import (
    CLAIM_STATUS,
    TOOLS,
    _paid_display,
    gather,
    member_claims,
)
from policydesk.agent.scenarios.soothe import complaint_channel


def test_paid_display_renders_a_decimal_with_a_unit():
    # The defect this exists to prevent: a bare `12000` in front of a customer reads as
    # a thousand dollars or a hundred, never as the 12,000 元 the row actually means.
    assert _paid_display(Decimal(12000)) == "12,000 元"


def test_paid_display_leaves_none_alone():
    # None means "not decided, or decided against" — a fact for the injection to explain,
    # not a number for this function to invent.
    assert _paid_display(None) is None


async def test_member_claims_returns_empty_for_a_member_with_none(db):
    # member_id=4 (陳大文) holds no row in `claim` — confirmed live against the corpus
    # below, not assumed from the fixture's shape.
    holders = {r["member_id"] for r in await db.fetch("SELECT DISTINCT po.member_id FROM claim c JOIN policy po USING (policy_id)")}
    assert 4 not in holders, "test assumes member 4 has no claim; the corpus changed"
    assert await member_claims(db, 4) == []


async def test_member_claims_reads_the_real_corpus(db):
    # A member from the corpus who actually holds a claim, found by SQL rather than a
    # hardcoded id that goes stale the day the fixture data regenerates.
    row = await db.fetch_one("SELECT DISTINCT po.member_id FROM claim c JOIN policy po USING (policy_id) LIMIT 1")
    assert row, "the corpus must contain at least one claim for this scenario to answer anything"
    found = await member_claims(db, row["member_id"])
    assert found
    assert all(c["stage"] in {"received", "documents_pending", "assessing", "decided"} for c in found)
    assert all("paid_amount" not in c for c in found), "the raw Decimal must not survive past this tool"


async def test_member_claims_states_a_decline_plainly(db):
    # A member with a decided-and-declined claim: the row must carry the outcome as
    # recorded, not paraphrased into something softer.
    row = await db.fetch_one(
        "SELECT DISTINCT po.member_id FROM claim c JOIN policy po USING (policy_id) WHERE c.outcome = 'declined'"
    )
    assert row, "the corpus must contain a declined claim for this scenario's hardest path to be testable"
    found = await member_claims(db, row["member_id"])
    declined = [c for c in found if c["outcome"] == "declined"]
    assert declined
    assert declined[0]["stage"] == "decided"
    assert declined[0]["paid"] is None, "a declined claim pays nothing; a figure here would be fabricated"


async def test_member_claims_orders_most_recent_first(db):
    row = await db.fetch_one(
        "SELECT po.member_id, count(*) AS n FROM claim c JOIN policy po USING (policy_id) "
        "GROUP BY po.member_id HAVING count(*) > 1 LIMIT 1"
    )
    assert row, "the corpus must hold a member with more than one claim to test ordering"
    found = await member_claims(db, row["member_id"])
    assert len(found) > 1
    assert found == sorted(found, key=lambda c: c["filed_at"], reverse=True)


def test_complaint_channel_is_borrowed_not_copied():
    # `claim_status` must reuse `soothe`'s route rather than restating it — a second copy
    # of the appeal deadline is a second place for it to go stale.
    assert TOOLS["complaint_channel"] is complaint_channel


def test_tools_matches_scenario_tools():
    assert set(CLAIM_STATUS.tools) == set(TOOLS)


async def test_gather_confirmed_returns_both_facts(db):
    row = await db.fetch_one("SELECT DISTINCT po.member_id FROM claim c JOIN policy po USING (policy_id) LIMIT 1")
    facts = await gather(db, {}, member_id=row["member_id"], today=None, retriever=None, allowed=None)
    assert facts["member_claims"]
    assert facts["complaint_channel"]


async def test_gather_unconfirmed_withholds_member_claims_but_keeps_complaint_channel(db):
    allowed = tools.permitted(CLAIM_STATUS.tools, owner=claim_status, confirmed=False)
    assert "member_claims" not in allowed, "member_claims reads the member's own record and must gate"
    assert "complaint_channel" in allowed, "the appeal route is public and must survive the gate"

    row = await db.fetch_one("SELECT DISTINCT po.member_id FROM claim c JOIN policy po USING (policy_id) LIMIT 1")
    facts = await gather(db, {}, member_id=row["member_id"], today=None, retriever=None, allowed=allowed)
    assert "member_claims" not in facts
    assert facts["complaint_channel"]


async def test_gather_unconfirmed_never_calls_member_claims(db, monkeypatch):
    # The gate withholds the query, not the query's output — a module that filters after
    # dispatch has already read the member's row before the customer proved who they are.
    def boom(*_args, **_kwargs):
        raise AssertionError("member_claims ran despite being withheld")

    monkeypatch.setattr(claim_status, "member_claims", boom)
    monkeypatch.setitem(claim_status.TOOLS, "member_claims", boom)
    facts = await gather(db, {}, member_id=1, today=None, retriever=None, allowed=frozenset({"complaint_channel"}))
    assert "member_claims" not in facts
