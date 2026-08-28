"""
Give a demo applicant the policies they already hold.

The portfolio is **chosen**, not drawn. A demo rehearses a particular situation, so the
operator picks which one they are about to show — a new customer holding nothing, a book
with a lapsed rider, a policy still inside its waiting period — and the generator writes
exactly that. A random draw makes every run a different demo and none of them the one
that was prepared.

The situations stay consequences, never flags. Nothing here writes "this one should be
refused":

- **Waiting period not elapsed** — `effective_at` is inside the shortest waiting clause
  the corpus contains, which is 30 days. Two dates and a clause.
- **Lapsed** — `lapsed_at` is set and has passed. One date.

A rider is written against a real main policy of the same line, and the FK enforces it,
so a rider with no main contract is not a state this table can hold. It used to be
planted as a fault; the relational model makes it impossible instead.

Occupation is deliberately not a situation here. A class-6 member exceeding a product's
`max_occupation` ceiling is already a refusal waiting in the join between `member` and
`catalog_entry`.
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
    """One policy to write, and the situation it demonstrates."""

    product_id: str
    policy_number: str
    sum_insured: int
    effective_at: date
    lapsed_at: date | None = None
    rider_of: int | None = None
    """Index into the same plan, naming the main policy this rider attaches to."""
    fault: Fault | None = None


class Holding(Struct, frozen=True):
    """One line of a preset: which product line, main or rider, and its situation."""

    line: str
    rider: bool = False
    situation: str = "clean"
    """clean, waiting or lapsed. Only the first supports a claim today."""


class Preset(Struct, frozen=True):
    """A portfolio an operator can choose before the demo starts."""

    key: str
    label: str
    detail: str
    holdings: tuple[Holding, ...] = ()


PRESETS: tuple[Preset, ...] = (
    Preset("none", "尚無保單", "全新客戶，名下沒有任何契約，適合示範完整投保流程。"),
    Preset(
        "basic",
        "基本醫療",
        "住院醫療主約一張，附加手術附約一張，兩張都已生效多年。",
        (Holding("health"), Holding("health", rider=True)),
    ),
    Preset(
        "full",
        "完整保障",
        "壽險主約、醫療主約與其附約、意外主約與其附約，共五張皆有效。",
        (
            Holding("life"),
            Holding("health"),
            Holding("health", rider=True),
            Holding("accident"),
            Holding("accident", rider=True),
        ),
    ),
    Preset(
        "lapsed",
        "含停效保單",
        "醫療主約有效，附約已停效，適合示範復效與停效期間不理賠。",
        (Holding("health"), Holding("health", rider=True, situation="lapsed")),
    ),
    Preset(
        "waiting",
        "剛投保",
        "醫療主約生效未滿三十日，等待期尚未經過，適合示範等待期不理賠。",
        (Holding("health", situation="waiting"),),
    ),
)

BY_KEY: dict[str, Preset] = {preset.key: preset for preset in PRESETS}
DEFAULT_PRESET = "basic"


def preset_catalogue() -> list[dict[str, object]]:
    """
    List the portfolios a visitor may start from.

    Returns:
        One entry per preset, in the order they are offered.

    """
    return [{"key": p.key, "label": p.label, "detail": p.detail, "policies": len(p.holdings)} for p in PRESETS]


def _policy_number(rng: random.Random) -> str:
    """
    Mint a policy number in the insurer's own shape.

    Args:
        rng: The person's generator.

    Returns:
        A number of the form CL1234-567890.

    """
    return f"CL{rng.randint(1000, 9999)}-{rng.randint(100000, 999999)}"


_SITUATIONS: dict[str, str] = {
    "waiting": "生效未滿三十日，短於條款等待期",
    "lapsed": "已停效，停效期間不負給付責任",
}


async def plan(
    person: Person, db: Database, *, preset: str = DEFAULT_PRESET, today: date | None = None
) -> list[PlannedPolicy]:
    """
    Write out the portfolio the operator chose.

    Args:
        person: The applicant, whose insurance age and class bound what they could hold.
        db: Used to pick real products matching each holding.
        preset: Which preset to build. An unknown key falls back to the default.
        today: The date to plan against.

    Returns:
        The planned policies, mains before the riders that attach to them.

    A rider is planned only when a main policy of the same line was planned first. That
    ordering is not cosmetic: `enrol` writes it as a foreign key, so a rider planned
    without a main fails the insert rather than becoming an orphan.

    """
    today = today or datetime.now(UTC).date()
    chosen = BY_KEY.get(preset, BY_KEY[DEFAULT_PRESET])
    if not chosen.holdings:
        return []

    rng = rng_for(person.name, _SEED_SALT)
    age = person.insurance_age_on(today)

    # Only products this person could actually have been sold. A portfolio holding a
    # contract whose issue-age band excludes its owner is a bug that looks like a
    # situation.
    rows = await db.fetch(
        """SELECT p.product_id, p.name, p.line, ce.requires_main
           FROM catalog_entry ce JOIN product p USING (product_id)
           WHERE ce.on_sale
             AND $1::int BETWEEN ce.issue_age_min AND ce.issue_age_max
             AND $2::int <= ce.max_occupation
           ORDER BY p.product_id""",
        [max(0, age - 5), int(person.occupation_class)],
    )
    pool: dict[tuple[str, bool], list[dict]] = {}
    for row in rows:
        pool.setdefault((row["line"], row["requires_main"]), []).append(row)

    planned: list[PlannedPolicy] = []
    mains: dict[str, int] = {}
    for holding in chosen.holdings:
        available = pool.get((holding.line, holding.rider), [])
        if not available:
            logger.warning("preset_holding_unavailable", preset=chosen.key, line=holding.line, rider=holding.rider)
            continue
        if holding.rider and holding.line not in mains:
            logger.warning("rider_without_main", preset=chosen.key, line=holding.line)
            continue

        product = available[rng.randrange(len(available))]
        match holding.situation:
            case "waiting":
                effective, lapsed = today - timedelta(days=rng.randint(8, 25)), None
            case "lapsed":
                effective = today - timedelta(days=rng.randint(700, 1800))
                lapsed = today - timedelta(days=rng.randint(30, 200))
            case _:
                effective, lapsed = today - timedelta(days=rng.randint(400, 2600)), None

        note = _SITUATIONS.get(holding.situation)
        planned.append(
            PlannedPolicy(
                product_id=product["product_id"],
                policy_number=_policy_number(rng),
                sum_insured=rng.choice([1000, 1500, 2000, 3000]),
                effective_at=effective,
                lapsed_at=lapsed,
                rider_of=mains.get(holding.line) if holding.rider else None,
                fault=Fault(holding.situation, note) if note else None,
            )
        )
        if not holding.rider:
            mains[holding.line] = len(planned) - 1

    return planned


async def enrol(person: Person, db: Database, *, preset: str = DEFAULT_PRESET, today: date | None = None) -> int:
    """
    Write the member and the portfolio the operator chose.

    Args:
        person: The applicant.
        db: Where to write.
        preset: Which portfolio to give them.
        today: The date to plan against.

    Returns:
        The member id.

    Every visitor becomes a real row. A demo whose users exist only in a session cannot
    show a caseworker anything, and cannot be reopened tomorrow.

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

    policies = await plan(person, db, preset=preset, today=today)
    # One row at a time, because a rider needs its main policy's id and the plan lists
    # mains first. A batch insert cannot reference ids that do not exist yet.
    written: list[int | None] = []
    for policy in policies:
        parent = written[policy.rider_of] if policy.rider_of is not None else None
        written.append(
            await db.fetch_val(
                """INSERT INTO policy (member_id, product_id, policy_number, sum_insured,
                                       effective_at, lapsed_at, main_policy_id)
                   VALUES ($1::bigint,$2::text,$3::text,$4::int,$5::date,$6::date,$7::bigint)
                   ON CONFLICT (policy_number) DO NOTHING
                   RETURNING policy_id""",
                [
                    member_id, policy.product_id, policy.policy_number, policy.sum_insured,
                    policy.effective_at, policy.lapsed_at, parent,
                ],
            )
        )

    logger.info(
        "member_enrolled",
        name=person.name,
        member_id=member_id,
        preset=preset,
        policies=len(policies),
        situations=[p.fault.kind for p in policies if p.fault],
    )
    return member_id
