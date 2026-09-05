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
import re
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unicodedata import normalize

from policydesk.bootloader import logger
from policydesk.retrieval.base import CLAUSE, Hit, Retriever
from policydesk.synthetic.person import insurance_age

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import date

    from policydesk.core.db import Database


_UNIT = re.compile(r"每\s*(?:日\s*)?([\d,]+)\s*(萬|)元")
"""How a `catalog_entry.unit_label` states its unit: 每 100 萬元保額, 每日 1,000 元住院日額."""

DOCUMENTS_PER_PRODUCT = 8
"""Per-product retrieval budget; eight covers the observed maximum document clauses."""

DOCUMENT_CHARS = 4000
"""Shared clause budget for retrieval and answer context.

Keep bounded articles whole so a narrow match does not discard their exceptions.
Over-budget articles remain explicitly marked excerpts, not complete evidence.
"""

UNITS_PER_LABEL = 1000
"""`policy.sum_insured` counts thousandths of one `unit_label` unit.

Not a convention anybody wrote down, and it took a live reply to find. `billing_summary`
computes `unit_premium * sum_insured / 1000.0`, and `unit_premium` is the annual cost of
exactly one unit — so 3000 against 每 100 萬元保額 is three units, or 300 萬元 of cover.
The desk was quoting the 3000.
"""


def insured_amount(sum_insured: int | None, unit_label: str | None) -> str:
    """
    Render what a policy actually covers, in the words a customer reads.

    Args:
        sum_insured: The raw count from `policy`.
        unit_label: The product's unit, from `catalog_entry`.

    Returns:
        e.g. `300 萬元`, `每日 1,500 元`, or `3 單位` when the label states no amount.

    A bare 3000 in front of a customer is a wrong figure, not an unlabelled one — measured
    on a live reply, where 保險金額：1000 was 100 萬元 of cover written as a thousand dollars.
    The unit lives one table away in `catalog_entry`, which is why it kept being left
    behind; `list_policies` now carries it so this can be called wherever the figure is
    shown.

    """
    if not sum_insured:
        return "0"
    units = sum_insured / UNITS_PER_LABEL
    if not unit_label or not (found := _UNIT.search(unit_label)):
        return f"{units:,.10g} 單位"
    amount = units * int(found.group(1).replace(",", ""))
    daily = "每日 " if "日" in unit_label else ""
    if found.group(2) == "萬":
        return f"{daily}{amount:,.10g} 萬元"
    return f"{daily}{amount:,.0f} 元"


def requires_identity(fn: Callable[..., Any]) -> Callable[..., Any]:
    """
    Mark a tool as reading one named customer's own record.

    Args:
        fn: The tool.

    Returns:
        The same function, flagged.

    The flag lives on the function that touches the data, not on the scenario that
    calls it. A scenario's gate is then derived rather than declared, so adding a
    member-reading tool to a scenario cannot leave the gate behind — which is exactly
    the mistake a hand-maintained list of protected scenarios makes eventually.

    """
    fn.requires_identity = True
    return fn


def public(fn: Callable[..., Any]) -> Callable[..., Any]:
    """
    Mark a tool as answering the same thing to everyone.

    Args:
        fn: The tool.

    Returns:
        The same function, flagged.

    The counterpart to `requires_identity`, and it exists so that no flag at all becomes a
    third state the desk can notice. Without it, an undecorated tool means both 「someone
    decided this is public」 and 「nobody looked」, and `tests/test_identity_inventory.py`
    cannot tell a decision from an omission. The runtime gate is unaffected: it already
    reads `requires_identity`, which this sets to False explicitly rather than by absence.

    """
    fn.requires_identity = False
    return fn


def reads_identity(tool_names: Iterable[str], *, owner: Any = None) -> bool:
    """
    Say whether any of these tools reads the customer's own record.

    Args:
        tool_names: Tool names, as a scenario lists them.
        owner: The module holding the scenario's own tools in a `TOOLS` mapping, when
            the scenario has one. None resolves every name against this module.

    Returns:
        True when at least one is marked, and True for a name neither place defines.

    The unknown name is the important half. A tool written in `agent/scenarios/` does
    not exist in this module's globals, and reading that absence as 「不需核對」 would
    let a scenario read member data before the customer has proved who they are —
    silently, with no line of code saying so. Unknown therefore means gated, so the
    mistake that gets made is the one that asks for an ID it did not need.

    """
    catalogue: dict[str, Any] = dict(getattr(owner, "TOOLS", {}))
    return any(
        getattr(known, "requires_identity", False)
        if (known := catalogue.get(name, globals().get(name))) is not None
        else True
        for name in tool_names
    )


def permitted(tool_names: Iterable[str], *, owner: Any = None, confirmed: bool) -> frozenset[str]:
    """
    Say which of a scenario's tools may run in this session.

    Args:
        tool_names: Tool names, as a scenario lists them.
        owner: The module holding the scenario's own tools in a `TOOLS` mapping.
        confirmed: Whether this session has passed 資料核對.

    Returns:
        The names that may run. Confirmed, that is all of them.

    Not the same question as `reads_identity`, and the difference is the point. That one
    asks whether a scenario touches member data at all, and answers for the scenario as a
    block. This one answers per tool, so a customer who has not proved who they are still
    gets the half of the answer that is public — 猶豫期是十天, 據實說明義務是什麼 — with
    the request for an ID attached to it rather than standing in place of it. Refusing the
    public half too is what makes a desk feel like it is stalling.

    A name that resolves to nothing is excluded, the same way `reads_identity` counts it
    as gated: an unknown tool is one nobody has checked.

    """
    catalogue: dict[str, Any] = dict(getattr(owner, "TOOLS", {}))
    # Resolved on both paths. Returning the names unread when `confirmed` was true left
    # the rule above true only for an unverified customer: `permitted(("no_such_tool",),
    # confirmed=True)` handed back the name it had not found, which is the opposite of
    # excluding what nobody has checked.
    return frozenset(
        name
        for name in tool_names
        if (fn := catalogue.get(name, globals().get(name))) is not None
        and (confirmed or not getattr(fn, "requires_identity", False))
    )


@requires_identity
async def list_policies(db: Database, member_id: int, *, today: date) -> list[dict[str, Any]]:
    """
    List a member's policies with everything a decision needs.

    Args:
        db: The database.
        member_id: Whose policies.
        today: The date to judge currency against.

    Returns:
        One row per policy, carrying the facts a refusal would be derived from:
        effective date, lapse date, days in force, and the main contract a rider hangs
        off. `main_contract_missing` is gone with the column it read — the FK makes an
        orphan rider unrepresentable, so a check for one could only ever return false.

    """
    rows = await db.fetch(
        """SELECT po.policy_id, po.policy_number, po.sum_insured, ce.unit_label,
                  po.effective_at, po.lapsed_at,
                  main.policy_number AS main_policy_number,
                  pr.name AS product_name, pr.product_id, pr.attachment, pr.document_kind,
                  ($1::date - po.effective_at) AS days_in_force,
                  (po.lapsed_at IS NOT NULL AND po.lapsed_at <= $1::date) AS is_lapsed
           FROM policy po
           JOIN product pr USING (product_id)
           LEFT JOIN catalog_entry ce USING (product_id)
           LEFT JOIN policy main ON main.policy_id = po.main_policy_id
           WHERE po.member_id = $2::bigint
           ORDER BY po.effective_at DESC""",
        [today, member_id],
    )
    for row in rows:
        # Popped, not copied. Handing the model both 2000 and 每日 2,000 元 and telling
        # it to state 保險金額 is asking it to choose, and it chose the bare count in a
        # live reply — 保險金額：3,000 for a policy paying 每日 3,000 元. The renderer was
        # added and the value it replaces was left in the row beside it.
        # Rendered here, once, so no caller has to know that `sum_insured` counts
        # thousandths of a unit named in another table.
        row["insured"] = insured_amount(row.pop("sum_insured", None), row.pop("unit_label", None))
    return rows


def _select_policies(policies: list[dict[str, Any]], reference: str) -> dict[str, Any]:
    """Resolve a stated choice only within holdings already read under the identity gate."""
    wanted = "".join(normalize("NFKC", reference).split()).casefold()
    if not policies:
        return {"status": "empty", "policies": []}
    if not wanted or wanted == "全部":
        return {"status": "all", "policies": policies}
    matched = []
    for policy in policies:
        number = "".join(normalize("NFKC", policy["policy_number"]).split()).casefold()
        name = "".join(normalize("NFKC", policy["product_name"]).split()).casefold()
        if wanted == number or wanted == policy["product_id"] or wanted in name:
            matched.append(policy)
    if len(matched) == 1:
        return {"status": "found", "policies": matched}
    return {"status": "ambiguous" if matched else "not_found", "policies": matched or policies}


async def _clauses_by_id(db: Database, keys: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """
    Read the clauses the index named, in the order it ranked them.

    Args:
        db: The database.
        keys: (product_id, clause_id) pairs, best first.

    Returns:
        The full rows, still in rank order. Postgres returns a set, so the ordering is
        reimposed here rather than trusted — a search whose ranking is discarded on the
        way back is a search that did nothing.

    """
    if not keys:
        return []
    # Two parallel arrays joined through unnest, not a record[]. psqlpy binds a Python
    # tuple list to record[] by panicking in its Rust layer — `entered unreachable
    # code`, no SQL error, no column named.
    products, clauses = [k[0] for k in keys], [k[1] for k in keys]
    rows = await db.fetch(
        """SELECT c.product_id, c.clause_id, c.kind, c.heading, c.verbatim, c.page, p.name AS product_name
           FROM contract_clause c
           JOIN product p USING (product_id)
           JOIN unnest($1::text[], $2::text[]) AS want(product_id, clause_id)
             ON want.product_id = c.product_id AND want.clause_id = c.clause_id""",
        [products, clauses],
    )
    rank = {key: position for position, key in enumerate(keys)}
    rows.sort(key=lambda r: rank.get((r["product_id"], r["clause_id"]), len(rank)))
    return rows


def _apply_passages(rows: list[dict[str, Any]], hits: Iterable[Hit]) -> None:
    """Keep short articles whole; carry the matched text for longer ones explicitly."""
    spans = {(hit.scope_id, hit.doc_id): hit for hit in hits if hit.start is not None}
    for row in rows:
        if len(row.get("verbatim") or "") <= DOCUMENT_CHARS:
            continue
        if hit := spans.get((row["product_id"], row["clause_id"])):
            full_text = f"{row.get('heading') or ''}\n{row.get('verbatim') or ''}"
            row["verbatim"] = full_text[hit.start:hit.end]
            row["excerpt"] = hit.start > 0 or hit.end < len(full_text)
            row["excerpt_start"] = hit.start
            row["excerpt_end"] = hit.end
            row["source_chars"] = len(full_text)


_CROSS = re.compile(r"第([一二三四五六七八九十百]+|\d+)條")
"""A clause pointing at a sibling. 4,276 of the corpus's 11,741 clauses carry one."""

_DIGIT = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

CROSS_LIMIT = 4
"""Most referenced clauses fetched per search. A clause naming five siblings, six hits
deep, would otherwise hand the model thirty rows of context around six of answer."""


def _article_number(written: str) -> int | None:
    """
    Read an article number as a contract writes it.

    Args:
        written: The digits between 第 and 條, Arabic or Chinese.

    Returns:
        The number, or None when it is not one this reads — a reference nobody can
        resolve is skipped rather than guessed at.

    Handles 十 as both a digit and a place: 十二 is 12, 二十 is 20, 二十二 is 22.

    """
    if written.isdigit():
        return int(written)
    total = section = 0
    for char in written:
        if char == "百":
            total += (section or 1) * 100
            section = 0
        elif char == "十":
            section = (section or 1) * 10
        elif (digit := _DIGIT.get(char)) is not None:
            section = section + digit if section % 10 == 0 and section else digit
        else:
            return None
    return (total + section) or None


def _referenced(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """
    Name the clauses these clauses point at.

    Args:
        rows: Clauses already found, carrying `product_id` and `verbatim`.

    Returns:
        (product_id, clause_id) pairs not already in `rows`, in the order met.

    **A clause that cites a sibling is half an answer without it.** 因第三條約定而住院時，
    可依日額給付型或實支實付型擇優給付 reached a customer with 第三條 not retrieved, and
    the desk told them 目前回傳的條款內容沒有第三條 — machine words over an incomplete
    answer, while `art.3` sat in the same table saying 因疾病或傷害住院診療. Measured on a
    live turn; a third of this corpus cross-references.

    Same product only. Article numbering restarts per contract, so 第三條 in one policy
    is a different sentence from 第三條 in another.

    """
    have = {(r["product_id"], r["clause_id"]) for r in rows}
    wanted: list[tuple[str, str]] = []
    for row in rows:
        for written in _CROSS.findall(row.get("verbatim") or ""):
            if (number := _article_number(written)) is None:
                continue
            key = (row["product_id"], f"art.{number}")
            if key not in have and key not in wanted:
                wanted.append(key)
    return wanted[:CROSS_LIMIT]


ANY_TOPIC: frozenset[str] = frozenset({"全部", "全部條款", "所有", "不限"})
"""What a customer who named no topic is filled in as. Read here as no topic at all.

A sentinel only works where something reads it. `explain_cover.topic` has promised
工具會回傳整份條款 since it was written, and this module had never heard of the word —
so 全部 went into BM25 as a query term and into the fallback as `ILIKE '%全部%'`, and a
customer asking 我這幾張保單保什麼 was ranked against that word rather than against
nothing."""


@requires_identity
async def find_clause(
    db: Database, product_ids: list[str], topic: str, limit: int = 6, index: Retriever | None = None
) -> list[dict[str, Any]]:
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

    A clause naming a sibling brings that sibling back with it, capped at `CROSS_LIMIT`.
    Without it the model reads 因第三條約定而住院 with no 第三條 in front of it and has to
    say so — which it did, to a customer asking whether cancer admission is covered.

    Ranked by the retriever when one is open, by ILIKE when none is. The difference is
    not subtle on this corpus: ILIKE returned the same three clauses for three unrelated
    questions, because the words a customer uses are not substrings of the words a
    contract uses and the `kind IN (exclusion, carve_back, waiting)` arm caught every
    query. The fallback stays because a desk whose ranking is worse still answers, and
    one that will not start does not.

    """
    if not product_ids:
        return []
    if topic.strip() in ANY_TOPIC:
        topic = ""
    # No cross-encoder on this half, and the reason is measured. `statutory_floor`
    # reranks and gains four of twenty-four; here it loses six of a hundred and eighty.
    #
    # The first measurement said the opposite, because it searched the whole clause
    # corpus. This tool never does: `scope=product_ids` is the three to five contracts
    # one member holds, and against 660 products the same `art.2` filled all five top
    # slots — a distribution production cannot produce. Reranking that pile is a real
    # improvement to a ranking nobody sees. Scoped, at the six clauses this tool returns,
    # the fused order is already right 167 times in 180 and reranking it is right 161.
    if index is not None and (hits := await asyncio.to_thread(
        index.search, topic, corpus=CLAUSE, scope=product_ids, limit=limit,
    )):
        hits = [hit for hit in hits if hit.corpus == CLAUSE and hit.scope_id in product_ids]
        found = await _clauses_by_id(db, [(h.scope_id, h.doc_id) for h in hits])
        _apply_passages(found, hits)
        # Appended after the ranked hits, not mixed into them: a clause is here because
        # another one pointed at it, not because it matched the question.
        if cross := _referenced(found):
            found += await _clauses_by_id(db, cross)
        return found
    return await db.fetch(
        """SELECT c.product_id, c.clause_id, c.kind, c.heading, c.verbatim, c.page, p.name AS product_name
           FROM contract_clause c JOIN product p USING (product_id)
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


@public
async def suitable_products(
    db: Database, *, insurance_age: int, occupation_class: int, budget: int, line: str, limit: int = 5,
    need: str = "", index: Retriever | None = None,
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
    candidates = await db.fetch(
        """SELECT p.product_id, p.name, p.attachment, p.line, ce.unit_premium, ce.unit_label,
                  ce.data_origin, ce.rate_unit_amount,
                  ce.issue_age_min, ce.issue_age_max, ce.max_occupation, ce.requires_main
           FROM sale_catalog ce JOIN product p USING (product_id)
           WHERE ce.on_sale
             AND p.line = $5::text
             AND $1::int BETWEEN ce.issue_age_min AND ce.issue_age_max
             AND $2::int <= ce.max_occupation
             AND ce.unit_premium <= $3::numeric
           ORDER BY ce.unit_premium ASC
           LIMIT $4::int""",
        [insurance_age, occupation_class, Decimal(budget), None if need and index is not None else limit, line],
    )
    if not candidates:
        return []
    matches = {}
    if need and index is not None:
        # Eligibility is decided by SQL. Retrieval ranks only those eligible products,
        # by their contract text, rather than letting a cheap unrelated product stand
        # in for the customer's need merely because it shares a product line.
        hits = await asyncio.to_thread(
            index.search, need, corpus=CLAUSE, scope=[row["product_id"] for row in candidates],
            limit=max(limit, len(candidates) * 4),
        )
        by_product = {row["product_id"]: row for row in candidates}
        chosen = []
        for hit in hits:
            if hit.scope_id in by_product and hit.scope_id not in matches:
                matches[hit.scope_id] = hit
                chosen.append(by_product[hit.scope_id])
                if len(chosen) == limit:
                    break
        if chosen:
            candidates = chosen
    candidates = candidates[:limit]
    product_ids = [row["product_id"] for row in candidates]
    clauses = await db.fetch(
        """SELECT product_id, clause_id, kind, heading, verbatim, page
           FROM contract_clause WHERE product_id = ANY($1::text[])
             AND kind = ANY($2::text[])
           ORDER BY product_id, kind, clause_id""",
        [product_ids, ["waiting", "exclusion", "carve_back"]],
    )
    if matches:
        matched = await _clauses_by_id(db, [(hit.scope_id, hit.doc_id) for hit in matches.values()])
        _apply_passages(matched, matches.values())
        clauses += matched
    evidence: dict[str, dict[str, dict[str, Any]]] = {}
    for clause in clauses:
        evidence.setdefault(clause["product_id"], {})[clause["clause_id"]] = clause
    for row in candidates:
        row["selection_basis"] = "eligibility and contract retrieval" if row["product_id"] in matches else "eligibility only"
        row["contract_evidence"] = list(evidence.get(row["product_id"], {}).values())
    return candidates


_RELAXED = """\
SELECT p.product_id, p.name, p.line, ce.unit_premium, ce.unit_label,
       ce.data_origin, ce.rate_unit_amount,
       ce.issue_age_min, ce.issue_age_max, ce.max_occupation
FROM sale_catalog ce JOIN product p USING (product_id)
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
       count(*) AS on_sale,
       CASE WHEN count(DISTINCT ce.data_origin) = 1 THEN min(ce.data_origin)
            ELSE 'unknown' END AS data_origin
FROM sale_catalog ce JOIN product p USING (product_id)
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
            binding.append({"條件": "投保年齡", "保戶": insurance_age, "上限": this_line["age_max"], "範圍": line,
                            "data_origin": this_line["data_origin"]})
        elif insurance_age < this_line["age_min"]:
            binding.append({"條件": "投保年齡", "保戶": insurance_age, "下限": this_line["age_min"], "範圍": line,
                            "data_origin": this_line["data_origin"]})
        if occupation_class > this_line["occupation_max"]:
            scope = "全線" if every_line and occupation_class > every_line["occupation_max"] else line
            ceiling = every_line if scope == "全線" else this_line
            binding.append({"條件": "職業等級", "保戶": occupation_class, "上限": ceiling["occupation_max"], "範圍": scope,
                            "data_origin": ceiling["data_origin"]})
        if budget < this_line["cheapest"]:
            binding.append({"條件": "年繳預算", "保戶": budget, "最低": int(this_line["cheapest"]), "範圍": line,
                            "data_origin": this_line["data_origin"]})

    return {
        "binding": binding,
        "openings": [{"relaxes": name, "products": rows} for name, rows in zip(probes, results, strict=True) if rows],
    }


@requires_identity
async def benefit_headings(db: Database, product_ids: list[str], limit: int = 40) -> list[dict[str, Any]]:
    """
    List what each contract actually pays for.

    Args:
        db: The database.
        product_ids: Which contracts.
        limit: Most rows to return.

    Returns:
        One row per granting clause: the product, the clause id and its heading.

    Answers 我的保單保什麼 without asking anything back. A customer who says that has
    told the desk everything it needs — which contracts they hold is a query, and the
    granting clauses of those contracts are another. Asking 想了解哪一項 in reply is the
    desk making the customer do a lookup it could have done itself.

    **`kind = 'grant'` alone is too wide, so the heading is read too.** That bucket holds
    2,521 clauses and only 1,400 name something the contract pays: 901 of the rest are
    的申領, which is how to claim a benefit rather than a benefit, and the remainder are
    保險金額之減少, 保險事故的通知 and 受益人之指定. A live reply listed eight 給付項目
    for one policy and three of them were procedure — a customer reading that list cannot
    tell which line is cover and which is paperwork.

    Matched on 保險金 rather than on 給付, because 住院日額保險金, 祝壽保險金 and
    癌症住院醫療保險金 are benefits whose headings never use the word. A heading ending in
    之限制 is dropped and one merely containing 限制 is kept: 保險金給付之限制 is a cap on
    a benefit rather than a benefit, while 完全失能保險金的給付及其限制 is the grant with
    its own conditions attached — 13 of the first shape against 132 of the second.

    Ordered by the article number, not by `clause_id` as text. `art.11` sorts before
    `art.3` as a string, so a live reply listed 保險範圍 [art.3] after [art.11] and the
    whole list read out of contract order. The 50 ids with no article number (`waiting`)
    sort last rather than raising on the cast.

    """
    if not product_ids:
        return []
    return await db.fetch(
        r"""SELECT c.product_id, c.clause_id, c.heading, p.name AS product_name
           FROM contract_clause c JOIN product p USING (product_id)
           WHERE c.product_id = ANY($1::text[]) AND c.kind = 'grant'
             AND c.heading ~ '保險金|保險範圍|承保範圍'
             AND c.heading !~ '申領|申請|通知|指定|減少|變更|受益人'
             AND c.heading !~ '之限制$|的限制$'
           ORDER BY p.name,
                    coalesce(nullif(substring(c.clause_id from 'art\.([0-9]+)'), '')::int, 999999),
                    c.clause_id
           LIMIT $2::int""",
        [product_ids, limit],
    )


@public
async def catalogue_sample(db: Database, line: str, limit: int = 5) -> list[dict[str, Any]]:
    """
    List what is on sale in a line, for anyone.

    Args:
        db: The database.
        line: Which line, one of LINES.
        limit: Most products to name.

    Returns:
        Products with their unit premium and issue-age band, cheapest first, each
        carrying `on_sale_in_line` — how many the line actually holds. Empty for a line
        this desk does not sell from.

    **The count travels with the rows because the rows are a sample.** Five of the
    seventy-six health products reached a customer under the words 目前有以下醫療險商品,
    which reads as the whole catalogue; they then compared a list that had been cut to
    the five cheapest. The window function runs before `LIMIT`, so the total is the real
    one rather than the length of what came back.

    Deliberately ungated. The catalogue is a public document — an insurer publishes it —
    so a visitor who has not proved who they are can still be told what exists. What they
    cannot be told is which of it fits *them*, because that reads their age, their
    occupation class and their existing cover. So the desk introduces the products and
    asks for the number in the same breath, instead of refusing to speak.

    """
    if line not in LINES:
        return []
    return await db.fetch(
        """SELECT p.product_id, p.name, p.line, ce.unit_premium, ce.unit_label,
                  ce.data_origin, ce.rate_unit_amount,
                  ce.issue_age_min, ce.issue_age_max, ce.requires_main,
                  count(*) OVER () AS on_sale_in_line
           FROM sale_catalog ce JOIN product p USING (product_id)
           WHERE ce.on_sale AND p.line = $1::text
           ORDER BY ce.unit_premium ASC
           LIMIT $2::int""",
        [line, limit],
    )


@requires_identity
async def member_underwriting(db: Database, member_id: int, *, today: date) -> dict[str, Any]:
    """
    Read the three figures underwriting selects on.

    Args:
        db: The database.
        member_id: Whose figures.
        today: The date to age against.

    Returns:
        Insurance age, occupation class and occupation, or an empty dict.

    A separate tool rather than an inline query in the gather step, because the gate is
    derived from a scenario's tool list: a member read that happens outside a marked
    tool is a member read the gate cannot see.

    """
    row = await db.fetch_one(
        "SELECT birth_date, occupation, occupation_class FROM member WHERE member_id = $1::bigint",
        [member_id],
    )
    if row is None:
        return {}
    return {
        "insurance_age": insurance_age(row["birth_date"], today),
        "occupation": row["occupation"],
        "occupation_class": row["occupation_class"],
    }


@requires_identity
async def standing_brief(db: Database, member_id: int, *, today: date) -> dict[str, Any]:
    """
    Read everything about this customer that is true before they say anything.

    Args:
        db: The database.
        member_id: Whose book.
        today: The date to judge currency against.

    Returns:
        The member's own figures, their policies with the benefits each one grants, and
        the cheapest annual premium on each line they are eligible for.

    Handed to the router on every turn, so a question it has to ask is asked from data.
    A desk that answers 想了解哪一項保障主題 has made the customer do a lookup it could
    have done itself; one holding this asks 您有三張保單，住院日額、手術、重大疾病，
    想先看哪一項 — the same question, with the answers already in it.

    The floors matter for the same reason. A budget cannot be queried, so it has to be
    asked; asking it against 壽險最低年繳 7,430 元 is a question the customer can
    actually answer.

    """
    member = await db.fetch_one(
        "SELECT birth_date, sex, occupation, occupation_class FROM member WHERE member_id = $1::bigint",
        [member_id],
    )
    if member is None:
        return {}

    age = insurance_age(member["birth_date"], today)
    policies, floors = await asyncio.gather(
        db.fetch(
            """SELECT po.policy_number, po.sum_insured, ce.unit_label,
                      po.effective_at, po.lapsed_at,
                      pr.product_id, pr.name AS product_name, pr.line,
                      (po.lapsed_at IS NULL OR po.lapsed_at > $2::date) AS in_force
               FROM policy po JOIN product pr USING (product_id)
               LEFT JOIN catalog_entry ce USING (product_id)
               WHERE po.member_id = $1::bigint
               ORDER BY po.main_policy_id NULLS FIRST, po.policy_id""",
            [member_id, today],
        ),
        db.fetch(
            """SELECT DISTINCT ON (p.line) p.line, ce.unit_premium::int AS cheapest,
                      ce.unit_label AS unit, ce.data_origin, ce.rate_unit_amount
               FROM sale_catalog ce JOIN product p USING (product_id)
               WHERE ce.on_sale AND p.line = ANY($3::text[])
                 AND $1::int BETWEEN ce.issue_age_min AND ce.issue_age_max
                 AND $2::int <= ce.max_occupation
               ORDER BY p.line, ce.unit_premium, p.product_id""",
            [age, member["occupation_class"], sorted(LINES)],
        ),
    )

    benefits = await benefit_headings(db, [p["product_id"] for p in policies])
    by_product: dict[str, list[str]] = {}
    for row in benefits:
        by_product.setdefault(row["product_id"], []).append(f"{row['heading']}[{row['clause_id']}]")

    return {
        "保戶": {
            "保險年齡": age,
            "性別": "男" if member["sex"] == "male" else "女",
            "職業": member["occupation"],
            "職業等級": member["occupation_class"],
        },
        "名下保單": [
            {
                "保單號碼": p["policy_number"],
                "商品": p["product_name"],
                "保險金額": insured_amount(p["sum_insured"], p.get("unit_label")),
                "狀態": "有效" if p["in_force"] else "已停效",
                "給付項目": by_product.get(p["product_id"], [])[:8],
            }
            for p in policies
        ],
        "可投保商品線最低年繳保費": [
            {"線別": f["line"], "最低": f["cheapest"], "單位": f["unit"],
             "data_origin": f["data_origin"], "rate_unit_amount": f["rate_unit_amount"]}
            for f in floors
        ],
    }


@requires_identity
async def pending_signatures(db: Database, case_id: int) -> dict[str, Any]:
    """
    List the documents this case still needs signed.

    Args:
        db: The database.
        case_id: Which case.

    Returns:
        `count` and `names` — the two the 交付文件 template asks for.

    The template had asked for them since it was written and nothing supplied them, so
    `_render` fell through to its default branch and the customer read the literal
    「已為您備妥應簽署文件共 {count} 份」. A placeholder is a promise the renderer makes on
    the tool's behalf, and an unmade one reaches the customer looking like a bug in their
    insurer.

    """
    rows = await db.fetch(
        """SELECT title FROM case_document
           WHERE case_id = $1::bigint AND signed_at IS NULL
           ORDER BY document_id""",
        [case_id],
    )
    return {"count": len(rows), "names": "\n".join(f"　{r['title']}" for r in rows)}


@requires_identity
async def required_documents(
    db: Database, product_ids: list[str], *, index: Retriever | None = None, topic: str = "",
) -> list[dict[str, Any]]:
    """
    List what a claim on these products must be accompanied by.

    Args:
        db: The database.
        product_ids: Which contracts.
        index: The shared lexical/semantic retriever, when available.
        topic: The claim context supplied by the scenario.

    Returns:
        Document requirements with the condition attached to each.

    The condition is the part claimants get wrong. A contract does not ask for "a
    diagnosis certificate"; it asks for one that 須列明手術或處置名稱及部位, and a
    certificate without the site named comes back.

    Search each contract independently so a member's other policies do not disappear
    behind the first product's matches. Hits identify candidate evidence, not an
    exhaustive document checklist. The fallback is a normalized literal heading lookup;
    it cannot infer requirements absent from those headings.

    Return the full text here. The shared answer-context budget applies once in
    `_short`, which also marks any clipping so missing document requirements cannot
    silently appear to be a complete list.

    """
    if not product_ids:
        return []
    product_ids = list(dict.fromkeys(product_ids))
    if index is not None:
        query = f"{topic}\n申請保險金時，受益人應檢具哪些文件及證明？".strip()
        rankings = await asyncio.gather(*(
            asyncio.to_thread(index.search, query, corpus=CLAUSE, scope=[product], limit=DOCUMENTS_PER_PRODUCT)
            for product in product_ids
        ))
        hits = [hit for product, ranked in zip(product_ids, rankings, strict=True)
                for hit in ranked if hit.corpus == CLAUSE and hit.scope_id == product]
        keys = list(dict.fromkeys((hit.scope_id, hit.doc_id) for hit in hits))
        rows = await _clauses_by_id(db, keys)
        _apply_passages(rows, hits)
        return rows
    return await db.fetch(
        """SELECT product_id, clause_id, heading, verbatim, page, product_name
           FROM (
               SELECT c.product_id, c.clause_id, c.heading, c.page, p.name AS product_name,
                      c.verbatim,
                      row_number() OVER (PARTITION BY c.product_id ORDER BY c.clause_id) AS rank
               FROM contract_clause c JOIN product p USING (product_id)
               WHERE c.product_id = ANY($1::text[])
                 AND EXISTS (
                     SELECT 1 FROM unnest($3::text[]) AS term(value)
                     WHERE strpos(normalize(c.heading, NFKC), term.value) > 0
                 )
           ) ranked
           WHERE rank <= $2::int
           ORDER BY product_id, clause_id""",
        [product_ids, DOCUMENTS_PER_PRODUCT, ["申領", "保險金的申請", "檢具", "應檢附"]],
    )


@requires_identity
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
    rows = await db.fetch("SELECT clause_id FROM contract_clause WHERE product_id = ANY($1::text[])", [product_ids])
    return frozenset(r["clause_id"] for r in rows)


@requires_identity
async def billing_summary(db: Database, member_id: int, *, today: date) -> dict[str, Any]:
    """
    Total what a member currently pays.

    Args:
        db: The database.
        member_id: Whose policies.
        today: The date to judge currency against.

    Returns:
        Active policy count, what a year of premiums comes to, and how many policies
        that figure had to fall back to the rate card for.

    **What a year costs, not what the rate card says it costs.** The two differ, and a
    customer asking 我一年繳多少 is asking the first. This summed `unit_premium ×
    sum_insured / 1000` per policy and reached 74,100 元 for a member whose five policies
    actually bill 768×12 + 770×4 + 49,740 + 5,320 + 6,750 = 74,106 元 — the gap is one
    rounding per instalment, and it is the customer's own bill that carries it. Replayed
    side by side: `billing` said 74,100 and `payment` listed the instalments that add to
    74,106, in the same session, to the same person.

    A policy with no `premium_payment` row has no instalment to read, so it falls back to
    the rate card and is counted in `no_schedule` — the reply says which policies the
    figure is an estimate for rather than presenting a mixed total as one certain number.

    **`LEFT JOIN catalog_entry`, not an inner one.** A catalogue entry is written only
    for a product carrying ten articles or more, and nothing in the schema requires a
    policy's product to have one. On an inner join a member holding one such policy is
    told `active=0` and `premium=0` — every policy they hold disappears, not merely the
    one, and the customer reads that as owning nothing.

    That leaves three states and they are counted apart. A policy with instalments is
    read from them. A policy without them but with a rate falls back to the rate and is
    `no_schedule`, which the reply reports as an estimate. A policy with neither can only
    contribute nothing, and calling that an estimate would be the same overclaim in a
    smaller place — it is `uncosted`, and the reply says the total leaves it out.

    **`due_at <= today`.** Without it `ORDER BY due_at DESC` reaches for the newest row
    in the table rather than the newest one that has fallen due, so a schedule written
    ahead of time would have this quoting next year's instalment as what a year costs
    now. No such row exists in the current data, which is exactly why the guard belongs
    in the query rather than in a note about the data.
    """
    row = await db.fetch_one(
        """SELECT count(*) AS active,
                  coalesce(sum(CASE WHEN due.amount IS NOT NULL THEN due.amount * f.per_year
                                    WHEN ce.unit_premium IS NOT NULL
                                      THEN ce.unit_premium * po.sum_insured / 1000.0
                                    ELSE 0 END), 0) AS premium,
                  count(*) FILTER (WHERE due.amount IS NULL AND ce.unit_premium IS NOT NULL)
                    AS no_schedule,
                  count(*) FILTER (WHERE due.amount IS NULL AND ce.unit_premium IS NULL)
                    AS uncosted
           FROM policy po
           LEFT JOIN catalog_entry ce USING (product_id)
           CROSS JOIN LATERAL (SELECT CASE po.premium_mode
                                        WHEN 'annual' THEN 1 WHEN 'semiannual' THEN 2
                                        WHEN 'quarterly' THEN 4 ELSE 12 END AS per_year) f
           LEFT JOIN LATERAL (SELECT amount FROM premium_payment
                              WHERE policy_id = po.policy_id AND due_at <= $2::date
                              ORDER BY due_at DESC LIMIT 1) due ON true
           WHERE po.member_id = $1::bigint
             AND (po.lapsed_at IS NULL OR po.lapsed_at > $2::date)""",
        [member_id, today],
    )
    return row or {"active": 0, "premium": 0, "no_schedule": 0, "uncosted": 0}


@requires_identity
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
    rows = await db.fetch(
        """SELECT pr.name AS product_name, po.sum_insured, po.policy_number, ce.unit_label
           FROM policy po JOIN product pr USING (product_id)
           LEFT JOIN catalog_entry ce USING (product_id)
           WHERE po.member_id = $1::bigint
             AND (po.lapsed_at IS NULL OR po.lapsed_at > $2::date)
           ORDER BY po.sum_insured DESC""",
        [member_id, today],
    )
    for row in rows:
        row["insured"] = insured_amount(row.get("sum_insured"), row.get("unit_label"))
    return rows


@requires_identity
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
