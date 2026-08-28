"""
Give a demo applicant the policies they already hold.

The portfolio has to be able to fail, because a desk that only ever says yes proves
nothing. But the failures are not planted as flags — every one of them is a
consequence of a field a caseworker can read:

- **Waiting period not elapsed** — `effective_at` is 20 days before the claimed event,
  and the contract's own waiting clause says 30. Two dates and a clause.
- **Lapsed** — `lapsed_at` is set and precedes the event. One date.
- **Orphan rider** — `main_policy_ref` names a policy number that is not in the table.
  A rider without its main contract is not cover, and the desk reports it as a data
  fault rather than quietly treating it as either.
Occupation is deliberately not in that list. A class-6 member exceeding a product's
`max_occupation` ceiling is already a refusal waiting in the join between `member` and
`catalog_entry`, and the generator draws class 6 and 拒保 on its own. Planting it here
as well labelled portfolios faulty whose member was two classes inside the ceiling.

Nothing here writes "this one should be refused". The refusal is a join.

Which faults a person gets is drawn from their own stable seed, so the same name
always has the same history, and each person gets at most two. One clean, claimable
policy is always present: a portfolio that fails in every direction demonstrates a
broken system rather than a careful one.
"""

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from msgspec import Struct

from policydesk.bootloader import logger
from policydesk.synthetic.seed import rng_for

if TYPE_CHECKING:
    import random

    from policydesk.core.db import Database
    from policydesk.synthetic.person import Person

_SEED_SALT = "policydesk-portfolio-v1"


class Fault(Struct, frozen=True):
    """
    One planted circumstance, named for the operator's own notes.

    The name never reaches the database. It exists so a rehearsal can look up which
    demo names carry which situation, while the running system sees only dates,
    references and class numbers.
    """

    kind: str
    note: str


class PlannedPolicy(Struct, frozen=True):
    """One policy to write, and why it is interesting."""

    product_id: str
    policy_number: str
    sum_insured: int
    effective_at: date
    lapsed_at: date | None = None
    main_policy_ref: str | None = None
    fault: Fault | None = None




def _policy_number(rng: random.Random) -> str:
    """
    Draw a policy number shaped like a Taiwanese insurer's.

    Args:
        rng: The person's generator.

    Returns:
        A policy number, e.g. "CL7413-208866".

    """
    return f"CL{rng.randint(1000, 9999)}-{rng.randint(100000, 999999)}"


async def plan(person: Person, db: Database, *, today: date | None = None) -> list[PlannedPolicy]:
    """
    Decide what policies this person holds.

    Args:
        person: The applicant.
        db: Used to pick real products they could plausibly have bought.
        today: The date to plan against.

    Returns:
        Between two and four policies, at least one of them clean.

    """
    today = today or datetime.now(UTC).date()
    rng = rng_for(person.name, _SEED_SALT)
    insurance_age = person.insurance_age_on(today)

    # Only products this person could actually have been sold: a portfolio holding a
    # rider whose issue-age band excludes its owner is not a failure case, it is a bug
    # that happens to look like one.
    candidates = await db.fetch(
        """SELECT p.product_id, p.name, p.attachment, ce.max_occupation, ce.issue_age_max
           FROM catalog_entry ce JOIN product p USING (product_id)
           WHERE p.line = 'health' AND ce.on_sale
             AND ce.issue_age_max >= $1::int
           ORDER BY p.product_id
           LIMIT 60"""
    , [max(0, insurance_age - 10)])
    if not candidates:
        logger.warning("portfolio_no_candidates", name=person.name, insurance_age=insurance_age)
        return []

    picks = rng.sample(candidates, k=min(len(candidates), rng.choice([2, 3, 3, 4])))
    planned: list[PlannedPolicy] = []

    # The clean one, bought long enough ago that every waiting period has run out.
    clean = picks[0]
    planned.append(
        PlannedPolicy(
            product_id=clean["product_id"],
            policy_number=_policy_number(rng),
            sum_insured=rng.choice([1000, 1500, 2000, 3000]),
            effective_at=today - timedelta(days=rng.randint(400, 2600)),
        )
    )

    faults = rng.sample(["waiting", "lapsed", "orphan"], k=rng.choice([1, 1, 2]))
    for product, kind in zip(picks[1:], faults, strict=False):
        planned.append(_with_fault(product, kind, rng, today))

    # Anything left over is ordinary cover.
    planned.extend(
        PlannedPolicy(
            product_id=product["product_id"],
            policy_number=_policy_number(rng),
            sum_insured=rng.choice([1000, 2000]),
            effective_at=today - timedelta(days=rng.randint(400, 2000)),
        )
        for product in picks[1 + len(faults) :]
    )

    return planned


def _with_fault(product: dict, kind: str, rng: random.Random, today: date) -> PlannedPolicy:
    """
    Build one policy that will not support a claim, for a readable reason.

    Args:
        product: The catalogue row to base it on.
        kind: Which circumstance to create.
        rng: The person's generator.
        today: The date to plan against.

    Returns:
        The policy, tagged with an operator-facing note.

    """
    number = _policy_number(rng)
    common = {"product_id": product["product_id"], "policy_number": number, "sum_insured": rng.choice([1000, 2000])}

    match kind:
        case "waiting":
            # Inside every waiting period the corpus contains: the shortest is 30 days.
            days = rng.randint(8, 25)
            return PlannedPolicy(
                **common,
                effective_at=today - timedelta(days=days),
                fault=Fault("waiting", f"生效僅 {days} 日，短於條款等待期"),
            )
        case "lapsed":
            effective = today - timedelta(days=rng.randint(700, 1800))
            return PlannedPolicy(
                **common,
                effective_at=effective,
                lapsed_at=today - timedelta(days=rng.randint(30, 200)),
                fault=Fault("lapsed", "事故前已停效"),
            )
        case "orphan":
            return PlannedPolicy(
                **common,
                effective_at=today - timedelta(days=rng.randint(400, 1500)),
                # A number in the right shape that no policy row carries.
                main_policy_ref=f"CL0000-{rng.randint(100000, 999999)}",
                fault=Fault("orphan", "附約所繫主契約查無此號"),
            )
        case _:
            msg = f"unknown fault kind {kind!r}"
            raise ValueError(msg)


async def enrol(person: Person, db: Database, *, today: date | None = None) -> int:
    """
    Write the member and their policies.

    Args:
        person: The applicant.
        db: Where to write.
        today: The date to plan against.

    Returns:
        The member id.

    Every visitor becomes a real row. A demo whose users exist only in a session
    cannot show a caseworker anything, and cannot be reopened tomorrow.

    """
    today = today or datetime.now(UTC).date()
    member_id = await db.fetch_val(
        """INSERT INTO member (display_name, national_id, sex, birth_date, occupation, occupation_class,
                               address_city, address_district, address_rest, phone, email,
                               marital_status, income_band, medical_history, beneficiary_relation)
           VALUES ($1::text,$2::text,$3::text,$4::date,$5::text,$6::int,$7::text,$8::text,$9::text,
                   $10::text,$11::text,$12::text,$13::text,$14::text[],$15::text)
           ON CONFLICT (display_name) DO UPDATE SET display_name = EXCLUDED.display_name
           RETURNING member_id""",
        [
            person.name,
            person.national_id,
            person.sex.value,
            person.birth_date,
            person.occupation,
            int(person.occupation_class),
            person.address.city,
            person.address.district,
            str(person.address).removeprefix(person.address.city).removeprefix(person.address.district),
            person.phone,
            person.email,
            person.marital_status.value,
            person.income_band.value,
            [m.value for m in person.medical_history],
            person.beneficiary_relation.value,
        ],
    )

    policies = await plan(person, db, today=today)
    await db.execute_many(
        """INSERT INTO policy (member_id, product_id, policy_number, sum_insured, effective_at, lapsed_at, main_policy_ref)
           VALUES ($1::bigint,$2::text,$3::text,$4::int,$5::date,$6::date,$7::text)
           ON CONFLICT (policy_number) DO NOTHING""",
        [
            (member_id, p.product_id, p.policy_number, p.sum_insured, p.effective_at, p.lapsed_at, p.main_policy_ref)
            for p in policies
        ],
    )

    logger.info(
        "member_enrolled",
        name=person.name,
        member_id=member_id,
        policies=len(policies),
        faults=[p.fault.kind for p in policies if p.fault],
    )
    return member_id
