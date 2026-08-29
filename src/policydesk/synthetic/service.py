"""
The service history a policy accumulates after it is issued.

`portfolio` writes the contract. This writes what happens to it afterwards: the premiums
that fell due and were or were not paid, who the benefit is designated to, and any claim
filed against it. Separated because the two answer different questions — one is what the
customer bought, the other is everything they will ring up about.

**Derived from the policy, not invented beside it.** A payment schedule that disagrees with
`effective_at` is worse than none: it makes 我下次繳費是什麼時候 answerable and wrong. So
the instalments are generated from the effective date forward at the policy's own mode, and
a lapsed policy stops being paid before its lapse date rather than at a random point.
"""

import random
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from policydesk.bootloader import logger

if TYPE_CHECKING:
    from policydesk.core.db import Database

MODES: dict[str, int] = {"annual": 12, "semiannual": 6, "quarterly": 3, "monthly": 1}
"""Months between instalments. The mode decides the amount of one payment and the grace
period the customer is actually in, so it is a property of the contract rather than a
display choice."""

MODE_WEIGHTS = (0.55, 0.15, 0.15, 0.15)
"""Most Taiwanese life policies are paid annually. A spread that made monthly as likely as
annual would make 寬限期 the common case in a demo where it is the uncommon one."""

GRACE_DAYS = 30
"""保險法 §116: 催告到達後屆三十日仍不交付, 保險契約之效力停止. The window a missed
instalment sits in before the contract stops."""

RELATIONS: tuple[tuple[str, float], ...] = (
    ("配偶", 0.42), ("子女", 0.28), ("父母", 0.16), ("兄弟姊妹", 0.07), ("法定繼承人", 0.07),
)
"""Who a Taiwanese policyholder names. 法定繼承人 is a designation in its own right and not
the same as naming nobody — the latter has no row at all, which is 保險法 §113."""

UNDESIGNATED = 0.12
"""Policies naming nobody. Kept deliberately common enough to demonstrate §113, because a
corpus where every policy has a beneficiary makes that provision untestable."""

CLAIM_KINDS: tuple[str, ...] = ("hospital", "surgery", "accident", "specific_illness")

CLAIM_RATE = 0.30
"""In-force policies carrying a claim. High for a real book and right for a demo: a desk
with no claim in it cannot show the one scenario people most want to see."""

CLAIM_STAGES: tuple[tuple[str, float], ...] = (
    ("decided", 0.55), ("assessing", 0.20), ("documents_pending", 0.15), ("received", 0.10),
)
"""Where a claim sits. Weighted toward 已決定 because most claims in a real book are closed,
and because the first draw made every stage equally likely: ten claims produced one decided
one, and it happened to be a refusal. A demo whose only finished claim is a rejection tells
a story nobody meant to tell."""

CLAIM_OUTCOMES: tuple[tuple[str, float], ...] = (("paid", 0.72), ("partial", 0.18), ("declined", 0.10))
"""What a decided claim decided. Most claims are paid, and a corpus that says otherwise
teaches the desk's own reviewers the wrong prior about the book they are looking at."""


def _instalments(start: date, mode: str, until: date) -> list[date]:
    """
    List the dates a premium falls due.

    Args:
        start: The policy's effective date.
        mode: How often it is paid.
        until: The last date to generate up to, inclusive.

    Returns:
        Due dates, earliest first.

    Stepped by whole months from the effective date rather than by a fixed day count, so
    an anniversary stays an anniversary — a customer whose policy started on the 15th is
    asked for money on the 15th.

    """
    step = MODES[mode]
    dates: list[date] = []
    at = start
    while at <= until:
        dates.append(at)
        month = at.month - 1 + step
        year, month = at.year + month // 12, month % 12 + 1
        # The 31st of a month the next one does not have falls back to its last day, the
        # same way a real billing anniversary does.
        day = min(at.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
                           31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
        at = date(year, month, day)
    return dates


async def furnish(db: Database, member_id: int, *, today: date, seed: int | None = None) -> dict[str, int]:
    """
    Write the service history for everything this member holds.

    Args:
        db: Where to write.
        member_id: Whose book.
        today: The date to generate up to.
        seed: Fixes the draw, so a member's history is the same on a rebuild.

    Returns:
        How many payments, beneficiaries and claims were written.

    Idempotent per member: existing rows for these policies are removed first, so running
    it twice does not double a customer's payment history — which would double the premium
    total the desk quotes.

    """
    policies = await db.fetch(
        """SELECT po.policy_id, po.effective_at, po.lapsed_at, po.sum_insured,
                  coalesce(ce.unit_premium, 0) AS unit_premium
           FROM policy po LEFT JOIN catalog_entry ce USING (product_id)
           WHERE po.member_id = $1::bigint""",
        [member_id],
    )
    if not policies:
        return {"payments": 0, "beneficiaries": 0, "claims": 0}

    ids = [p["policy_id"] for p in policies]
    for table in ("premium_payment", "policy_beneficiary", "claim"):
        await db.execute(f"DELETE FROM {table} WHERE policy_id = ANY($1::bigint[])", [ids])  # noqa: S608 - a literal per loop, no input reaches it

    rng = random.Random(seed if seed is not None else member_id)
    payments = beneficiaries = claims = 0
    # Collected across the loop and written once. `furnish` runs inside a real customer's
    # sign-up turn on the websocket, not in an offline seeding job, so every statement in
    # the loop is a round trip on somebody's own turn — the largest preset held five
    # policies and spent about a dozen of them on rows whose values were all known before
    # the first await.
    instalment_rows: list[list[Any]] = []
    beneficiary_rows: list[list[Any]] = []
    claim_rows: list[list[Any]] = []
    policy_ids: list[int] = []
    modes: list[str] = []
    paid_throughs: list[date] = []

    for policy in policies:
        mode = rng.choices(list(MODES), weights=MODE_WEIGHTS)[0]
        annual = float(policy["unit_premium"]) * (policy["sum_insured"] or 0) / 1000.0
        # Decimal, not float. psqlpy binds a `numeric` column from Decimal only, and an
        # int, a float or a str all fail with `insufficient data left in message` — a
        # wire-protocol error naming neither the column nor the type.
        # Whole dollars. A monthly instalment of 408.33 元 reached a customer in a live run,
        # and no insurer bills a third of a dollar — the fraction is an artefact of dividing
        # an annual rate by twelve, and it reads as a system that cannot count.
        instalment = Decimal(str(round(annual * MODES[mode] / 12) or 1000))
        lapsed = policy["lapsed_at"]
        # A lapsed policy stopped being paid before it lapsed, by the grace period — that
        # is what made it lapse. An in-force one is paid up to the last due date behind us.
        horizon = (lapsed - timedelta(days=GRACE_DAYS)) if lapsed else today
        due = _instalments(policy["effective_at"], mode, horizon)
        if not due:
            due = [policy["effective_at"]]
        paid_through = due[-1]

        # An in-force policy sometimes has its most recent instalment outstanding — the
        # customer is inside the grace period right now, which is the state 寬限期 exists
        # to explain and the one 保險法 §116 counts thirty days from.
        #
        # Written by leaving the last due date unpaid rather than by appending a future
        # one. The first shape could not fire at all: `paid_through` is already the last
        # due date at or before today, so the next instalment is always in the future and
        # the branch was dead — 0 unpaid rows across 4,253, measured before this changed.
        in_grace = not lapsed and len(due) > 1 and rng.random() < 0.15
        # A lapsed policy owes the premium that lapsed it. `horizon` stops the schedule a
        # grace period before the lapse date, and every row was then written as paid — so
        # 11 lapsed policies carried 0 unpaid instalments, and the scenario's own rule
        # ("no unpaid row means every instalment is settled") told a customer whose cover
        # had stopped for non-payment that they owed nothing. The missed instalment is the
        # last one before the lapse, which is the due date §116's thirty days ran from.
        owes = in_grace or (lapsed is not None and len(due) > 1)
        rows = [(policy["policy_id"], d, None if owes and d == due[-1] else d, instalment) for d in due]
        if owes:
            paid_through = due[-2]

        instalment_rows.extend([list(r) for r in rows])
        payments += len(rows)
        modes.append(mode)
        paid_throughs.append(paid_through)
        policy_ids.append(policy["policy_id"])

        if rng.random() >= UNDESIGNATED:
            shares = [100] if rng.random() < 0.75 else [60, 40]
            # Distinct relations, drawn without replacement. The first draw gave two 配偶
            # splitting 60/40 on the same contract, and a person has one spouse — a row a
            # caseworker reads as a data fault, correctly.
            pool = [r for r, _ in RELATIONS]
            weights = [w for _, w in RELATIONS]
            named: list[str] = []
            for _ in shares:
                pick = rng.choices(pool, weights=weights)[0]
                at = pool.index(pick)
                pool.pop(at)
                weights.pop(at)
                named.append(pick)
            for who, share in zip(named, shares, strict=True):
                beneficiary_rows.append([policy["policy_id"], who, who, share, policy["effective_at"]])
                beneficiaries += 1

        if not lapsed and rng.random() < CLAIM_RATE:
            # Inside the policy's own life, not an arbitrary window before today: a claim
            # dated before the contract started is one no assessor could ever have opened.
            span = (today - policy["effective_at"]).days
            event = today - timedelta(days=rng.randint(20, max(21, min(span, 700))))
            if event >= policy["effective_at"]:
                filed = event + timedelta(days=rng.randint(3, 25))
                stage = rng.choices([s for s, _ in CLAIM_STAGES], weights=[w for _, w in CLAIM_STAGES])[0]
                decided = stage == "decided"
                outcome = (
                    rng.choices([o for o, _ in CLAIM_OUTCOMES], weights=[w for _, w in CLAIM_OUTCOMES])[0]
                    if decided
                    else None
                )
                claim_rows.append(
                    [
                        policy["policy_id"], rng.choice(CLAIM_KINDS), event, filed, stage, outcome,
                        # Capped at today. A decided claim is one an assessor has already
                        # ruled on, and a `decided_at` after today put a decision in the
                        # future on a claim the console shows as 已核付 — 1 of 36 rows.
                        min(filed + timedelta(days=rng.randint(5, 20)), today) if decided else None,
                        Decimal(str(round(float(instalment) * rng.uniform(0.8, 6.0), 2)))
                        if outcome in {"paid", "partial"}
                        else None,
                        "",
                    ],
                )
                claims += 1

    if instalment_rows:
        await db.execute_many(
            """INSERT INTO premium_payment (policy_id, due_at, paid_at, amount)
               VALUES ($1::bigint, $2::date, $3::date, $4::numeric)""",
            instalment_rows,
        )
    if policy_ids:
        # Parallel arrays through `unnest`, not a `record[]`: psqlpy panics in its Rust
        # layer on a list of tuples bound to `$1::record[]`, with no SQL error and no
        # column named.
        await db.execute(
            """UPDATE policy SET premium_mode = v.mode, paid_through = v.paid
               FROM unnest($1::bigint[], $2::text[], $3::date[]) AS v(pid, mode, paid)
               WHERE policy.policy_id = v.pid""",
            [policy_ids, modes, paid_throughs],
        )
    if beneficiary_rows:
        await db.execute_many(
            """INSERT INTO policy_beneficiary (policy_id, display_name, relation, share, designated_at)
               VALUES ($1::bigint, $2::text, $3::text, $4::int, $5::date)""",
            beneficiary_rows,
        )
    if claim_rows:
        await db.execute_many(
            """INSERT INTO claim (policy_id, kind, event_at, filed_at, stage, outcome,
                                  decided_at, paid_amount, note)
               VALUES ($1::bigint,$2::text,$3::date,$4::date,$5::text,$6::text,$7::date,
                       $8::numeric,$9::text)""",
            claim_rows,
        )

    logger.info("service_history_written", member_id=member_id, payments=payments,
                beneficiaries=beneficiaries, claims=claims)
    return {"payments": payments, "beneficiaries": beneficiaries, "claims": claims}
