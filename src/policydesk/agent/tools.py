"""
The deterministic tools a scenario may call.

Everything a customer is told about their own policies comes from here. Each tool
reads the database and returns rows; none of them asks a model anything, and none of
them produces prose. That division is what lets the desk claim its figures are
traceable while the conversation around them is generated.

`find_clause` is the one to watch. It returns clause ids and their verbatim text, and
the model may quote that text and cite those ids — but the ids are re-checked against
this same store before anything renders, so a citation the model invents fails a
lookup rather than reaching a customer.
"""

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from policydesk.bootloader import logger

if TYPE_CHECKING:
    from datetime import date

    from policydesk.core.db import Database


async def list_policies(db: Database, member_id: int, *, today: date) -> list[dict[str, Any]]:
    """
    List a member's policies with everything a decision needs.

    Args:
        db: The database.
        member_id: Whose policies.
        today: The date to judge currency against.

    Returns:
        One row per policy, carrying the facts a refusal would be derived from:
        effective date, lapse date, days in force, and whether the rider's main
        contract resolves.

    """
    return await db.fetch(
        """SELECT po.policy_id, po.policy_number, po.sum_insured, po.effective_at, po.lapsed_at,
                  po.main_policy_ref, pr.name AS product_name, pr.product_id, pr.attachment,
                  ($1::date - po.effective_at) AS days_in_force,
                  (po.lapsed_at IS NOT NULL AND po.lapsed_at <= $1::date) AS is_lapsed,
                  (po.main_policy_ref IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM policy x WHERE x.policy_number = po.main_policy_ref))
                    AS main_contract_missing
           FROM policy po JOIN product pr USING (product_id)
           WHERE po.member_id = $2::bigint
           ORDER BY po.effective_at DESC""",
        [today, member_id],
    )


async def find_clause(db: Database, product_ids: list[str], topic: str, limit: int = 6) -> list[dict[str, Any]]:
    """
    Find clauses of given products that bear on a topic.

    Args:
        db: The database.
        product_ids: Which contracts to search.
        topic: What the customer asked about.
        limit: Most clauses to return.

    Returns:
        Clause rows with their verbatim text.

    Exclusions, carve-backs and waiting periods sort first regardless of how well they
    match the words. A customer asking what their policy covers is answered wrongly by
    a grant clause alone, and those three are exactly what a keyword search buries.

    """
    if not product_ids:
        return []
    return await db.fetch(
        """SELECT c.product_id, c.clause_id, c.kind, c.heading, c.verbatim, c.page, p.name AS product_name
           FROM clause c JOIN product p USING (product_id)
           WHERE c.product_id = ANY($1::text[])
             AND (c.verbatim ILIKE '%' || $2::text || '%'
                  OR c.heading ILIKE '%' || $2::text || '%'
                  OR c.kind IN ('exclusion','carve_back','waiting'))
           ORDER BY CASE c.kind
                      WHEN 'waiting' THEN 0 WHEN 'exclusion' THEN 1 WHEN 'carve_back' THEN 2
                      WHEN 'grant' THEN 3 ELSE 4 END,
                    c.clause_id
           LIMIT $3::int""",
        [product_ids, topic, limit],
    )


LINES: frozenset[str] = frozenset({"health", "life", "accident", "annuity", "investment"})
"""The product lines a customer can be sold from. `other` is everything the catalogue
could not classify, so it is never offered."""


async def suitable_products(
    db: Database, *, insurance_age: int, occupation_class: int, budget: int, line: str, limit: int = 5
) -> list[dict[str, Any]]:
    """
    Select products this person could actually be sold.

    Args:
        db: The database.
        insurance_age: 保險年齡, not the plain age.
        occupation_class: 1 to 7.
        budget: Annual premium the customer can carry.
        line: Which line to select from, one of LINES. A customer asking about 壽險 was
            being shown health products only, because this was pinned to 'health' — the
            88 life products on sale could not be reached from any conversation.
        limit: Most products to return.

    Returns:
        Products within the issue-age band, the occupation ceiling and the budget, or
        an empty list when the line is not one this desk sells from.

    The selection is a query, not a judgement. That is deliberate: asked how the desk
    avoids steering a customer to a product that pays it more, the answer is that the
    ranking is by premium ascending and no commission figure exists in the schema.

    """
    # Decimal, not int: psqlpy binds a numeric parameter from Decimal only, and an int
    # fails with a wire-protocol error that names neither the parameter nor the type.
    # Converted here so no caller has to know.
    if line not in LINES:
        logger.warning("unsellable_line", line=line)
        return []
    return await db.fetch(
        """SELECT p.product_id, p.name, p.attachment, p.line, ce.unit_premium, ce.unit_label,
                  ce.issue_age_min, ce.issue_age_max, ce.max_occupation, ce.requires_main
           FROM catalog_entry ce JOIN product p USING (product_id)
           WHERE ce.on_sale
             AND p.line = $5::text
             AND $1::int BETWEEN ce.issue_age_min AND ce.issue_age_max
             AND $2::int <= ce.max_occupation
             AND ce.unit_premium <= $3::numeric
           ORDER BY ce.unit_premium ASC
           LIMIT $4::int""",
        [insurance_age, occupation_class, Decimal(budget), limit, line],
    )


_RELAXED = """\
SELECT p.product_id, p.name, p.line, ce.unit_premium, ce.unit_label,
       ce.issue_age_min, ce.issue_age_max, ce.max_occupation
FROM catalog_entry ce JOIN product p USING (product_id)
WHERE ce.on_sale
  AND p.line = $5::text
  AND ($1::int BETWEEN ce.issue_age_min AND ce.issue_age_max OR NOT $6::bool)
  AND ($2::int <= ce.max_occupation OR NOT $7::bool)
  AND (ce.unit_premium <= $3::numeric OR NOT $8::bool)
ORDER BY ce.unit_premium ASC
LIMIT $4::int"""


_CEILINGS = """\
SELECT max(ce.issue_age_max) AS age_max, min(ce.issue_age_min) AS age_min,
       max(ce.max_occupation) AS occupation_max, min(ce.unit_premium) AS cheapest,
       count(*) AS on_sale
FROM catalog_entry ce JOIN product p USING (product_id)
WHERE ce.on_sale AND ($1::text = '' OR p.line = $1::text)"""


async def alternatives(
    db: Database, *, insurance_age: int, occupation_class: int, budget: int, line: str, limit: int = 3
) -> dict[str, Any]:
    """
    Say why nothing qualified, and what would.

    Args:
        db: The database.
        insurance_age: 保險年齡.
        occupation_class: 1 to 7.
        budget: Annual premium the customer said they can carry.
        line: The line they asked about.
        limit: Products to name per opening.

    Returns:
        `binding` — the criteria the customer sits outside, each with their value and
        the catalogue's actual ceiling. `openings` — one entry per single relaxation
        that reaches something, naming the condition it drops and the products behind
        it. Either may be empty.

    A customer told 沒有符合的商品 learns nothing and has nowhere to go. An adviser in
    that position names the binding condition and says what happens if it moves. Both
    halves come from the catalogue, so neither is the model's opinion: `binding` is a
    comparison against a stored ceiling, and each opening is a query with exactly one
    predicate removed, which is what makes it attributable.

    The probes go out together. They are independent, and a customer waiting on a
    refusal should not wait on it six times over.

    """
    others = sorted(LINES - {line})
    probes: dict[str, Any] = {
        "放寬職業等級": db.fetch(_RELAXED, [insurance_age, occupation_class, Decimal(budget), limit, line, True, False, True]),
        "放寬投保年齡": db.fetch(_RELAXED, [insurance_age, occupation_class, Decimal(budget), limit, line, False, True, True]),
        "提高預算": db.fetch(_RELAXED, [insurance_age, occupation_class, Decimal(budget), limit, line, True, True, False]),
        **{
            f"改看{other}": db.fetch(_RELAXED, [insurance_age, occupation_class, Decimal(budget), limit, other, True, True, True])
            for other in others
        },
    }
    this_line, every_line, *results = await asyncio.gather(
        db.fetch_one(_CEILINGS, [line if line in LINES else ""]),
        db.fetch_one(_CEILINGS, [""]),
        *probes.values(),
    )

    binding: list[dict[str, Any]] = []
    if this_line and this_line["on_sale"]:
        if insurance_age > this_line["age_max"]:
            binding.append({"條件": "投保年齡", "保戶": insurance_age, "上限": this_line["age_max"], "範圍": line})
        elif insurance_age < this_line["age_min"]:
            binding.append({"條件": "投保年齡", "保戶": insurance_age, "下限": this_line["age_min"], "範圍": line})
        if occupation_class > this_line["occupation_max"]:
            scope = "全線" if every_line and occupation_class > every_line["occupation_max"] else line
            ceiling = (every_line if scope == "全線" else this_line)["occupation_max"]
            binding.append({"條件": "職業等級", "保戶": occupation_class, "上限": ceiling, "範圍": scope})
        if budget < this_line["cheapest"]:
            binding.append({"條件": "年繳預算", "保戶": budget, "最低": int(this_line["cheapest"]), "範圍": line})

    return {
        "binding": binding,
        "openings": [{"relaxes": name, "products": rows} for name, rows in zip(probes, results, strict=True) if rows],
    }


async def required_documents(db: Database, product_ids: list[str]) -> list[dict[str, Any]]:
    """
    List what a claim on these products must be accompanied by.

    Args:
        db: The database.
        product_ids: Which contracts.

    Returns:
        Document requirements with the condition attached to each.

    The condition is the part claimants get wrong. A contract does not ask for "a
    diagnosis certificate"; it asks for one that 須列明手術或處置名稱及部位, and a
    certificate without the site named comes back.

    """
    if not product_ids:
        return []
    return await db.fetch(
        """SELECT rd.product_id, rd.benefit, rd.document, rd.condition, rd.page, p.name AS product_name
           FROM required_document rd JOIN product p USING (product_id)
           WHERE rd.product_id = ANY($1::text[])
           ORDER BY rd.benefit, rd.document""",
        [product_ids],
    )


async def clause_ids_for(db: Database, product_ids: list[str]) -> frozenset[str]:
    """
    List every clause id that legitimately exists for these products.

    Args:
        db: The database.
        product_ids: Which contracts.

    Returns:
        The ids a citation may name. Anything else is fabricated.

    """
    if not product_ids:
        return frozenset()
    rows = await db.fetch("SELECT clause_id FROM clause WHERE product_id = ANY($1::text[])", [product_ids])
    return frozenset(r["clause_id"] for r in rows)


async def billing_summary(db: Database, member_id: int, *, today: date) -> dict[str, Any]:
    """
    Total what a member currently pays.

    Args:
        db: The database.
        member_id: Whose policies.
        today: The date to judge currency against.

    Returns:
        Active policy count and total annual premium.

    """
    row = await db.fetch_one(
        """SELECT count(*) AS active, coalesce(sum(ce.unit_premium * po.sum_insured / 1000.0), 0) AS premium
           FROM policy po JOIN catalog_entry ce USING (product_id)
           WHERE po.member_id = $1::bigint
             AND (po.lapsed_at IS NULL OR po.lapsed_at > $2::date)""",
        [member_id, today],
    )
    return row or {"active": 0, "premium": 0}


async def coverage_summary(db: Database, member_id: int, *, today: date) -> list[dict[str, Any]]:
    """
    List the sum insured of each policy in force.

    Args:
        db: The database.
        member_id: Whose policies.
        today: The date to judge currency against.

    Returns:
        Product name and 保險金額 per policy. Not the remaining benefit — that is a
        different number, it is computed rather than stored, and conflating the two is
        how a customer is told they can claim an amount they cannot.

    """
    return await db.fetch(
        """SELECT pr.name AS product_name, po.sum_insured, po.policy_number, ce.unit_label
           FROM policy po JOIN product pr USING (product_id)
           LEFT JOIN catalog_entry ce USING (product_id)
           WHERE po.member_id = $1::bigint
             AND (po.lapsed_at IS NULL OR po.lapsed_at > $2::date)
           ORDER BY po.sum_insured DESC""",
        [member_id, today],
    )


async def find_multiplier(db: Database, product_ids: list[str], procedure: str, limit: int = 5) -> list[dict[str, Any]]:
    """
    Look up what a procedure pays, as a multiple of the daily benefit.

    Args:
        db: The database.
        product_ids: Which contracts to search.
        procedure: The procedure as the diagnosis certificate names it.
        limit: Most rows to return.

    Returns:
        Matching entries from 附表1 and 附表2 with their multipliers.

    This is where a benefit formula gets its number. The clause says 手術給付倍數 ╳
    住院醫療保險金日額 and stops there; without this table the formula has a shape and
    no value, and the calculator has nothing to evaluate.

    """
    if not product_ids or not procedure.strip():
        return []
    return await db.fetch(
        """SELECT sm.product_id, sm.schedule, sm.code, sm.procedure, sm.multiplier, sm.page,
                  p.name AS product_name
           FROM surgery_multiplier sm JOIN product p USING (product_id)
           WHERE sm.product_id = ANY($1::text[]) AND sm.procedure ILIKE '%' || $2::text || '%'
           ORDER BY sm.multiplier DESC
           LIMIT $3::int""",
        [product_ids, procedure.strip(), limit],
    )
