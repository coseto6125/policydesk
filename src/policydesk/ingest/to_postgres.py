"""
Move the parsed corpus into Postgres, and fabricate the catalogue the contracts omit.

Two jobs, kept in one place because the second only makes sense next to the first.

**The corpus** is copied from the SQLite index the parser writes. That file is a build
artefact — rebuildable from 660 PDFs in about ten minutes — so it is the parser's
output format, not a second database.

**The catalogue** is invented, and says so. Every contract in the corpus references
保險費率表 for its issue-age band and its rate; not one contains the table. Without
those numbers there is no answer to "can this 71-year-old buy this rider", so the
demo would have no suitability step at all. The numbers here are plausible rather than
real, they are derived deterministically from the product so they do not drift between
runs, and `catalog_entry` carries a MOCK DATA comment in the schema so nobody later
mistakes them for something scraped.
"""

import asyncio
import sqlite3
from decimal import Decimal
from hashlib import blake2b
from typing import TYPE_CHECKING

from msgspec import json
from pypdf import PdfReader

from policydesk.bootloader import logger
from policydesk.clauses.index import UNRESOLVED_PRODUCT_NAME, _tidy, _title_of, document_kind

if TYPE_CHECKING:
    from pathlib import Path

    from policydesk.core.db import Database

# Issue-age bands and occupation ceilings by product line. Drawn from what Taiwanese
# insurers publish on their own product pages, which is where a real catalogue would
# come from if the rate tables were public.
_BANDS: dict[str, tuple[int, int, int]] = {
    # line: (issue_age_min, issue_age_max, max_occupation)
    "health": (0, 75, 4),
    "accident": (0, 75, 6),
    "life": (0, 70, 6),
    "annuity": (20, 70, 6),
    "investment": (20, 70, 6),
    "other": (0, 80, 6),
}

_UNITS: dict[str, tuple[str, int, int, int]] = {
    # line: (demo unit label, numeric pricing basis, min annual premium, max)
    # Generic health cover includes lump sums. Daily-benefit prices cannot price this
    # basis; use a synthetic 1%-5% range without changing held policies' cover scale.
    "health": ("每 1,000 元保額", 1_000, 10, 50),
    "accident": ("每 100 萬元保額", 1_000_000, 900, 3600),
    "life": ("每 100 萬元保額", 1_000_000, 6000, 42000),
    "annuity": ("每 10 萬元年金", 100_000, 8000, 30000),
    "investment": ("每 10 萬元保額", 100_000, 5000, 25000),
    "other": ("每單位", 1, 1000, 5000),
}


def _stable_premium(product_id: str, line: str) -> Decimal:
    """
    Derive a premium that does not change between runs.

    Args:
        product_id: The product to price.
        line: Its product line, which sets the range.

    Returns:
        An annual premium per catalogue unit, to the nearest ten TWD. Decimal, not int:
        psqlpy binds a numeric column from Decimal only, and rejects an int, a float and
        a string alike with "insufficient data left in message" — a wire-protocol
        complaint that names neither the column nor the type it refused.

    """
    _, _, low, high = _UNITS.get(line, _UNITS["other"])
    spread = high - low
    offset = int.from_bytes(blake2b(product_id.encode(), digest_size=4).digest(), "big") % spread
    return Decimal(round((low + offset) / 10) * 10)


async def copy_corpus(sqlite_path: Path, db: Database) -> tuple[int, int]:
    """
    Copy products and clauses from the parser's SQLite index into Postgres.

    Args:
        sqlite_path: The index the parser wrote.
        db: The destination.

    Returns:
        How many products and clauses were written.

    """
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    has_document_kind = "document_kind" in {column["name"] for column in src.execute("PRAGMA table_info(product)")}

    products = [
        (
            r["product_id"],
            r["doc_sha"],
            r["insurer"],
            _tidy(r["name"]).strip(),
            r["line"],
            r["attachment"],
            r["approval"],
            r["pages"],
            r["source_url"],
            r["document_kind"] if has_document_kind else "unknown",
        )
        for r in src.execute("SELECT * FROM product")
    ]
    # Every parameter is cast explicitly. execute_many prepares once and infers the
    # parameter types from the FIRST row, so a leading NULL in a nullable column —
    # approval is null for 571 of 660 documents — leaves that parameter typed unknown
    # and the first row that does carry a value fails the batch with "insufficient
    # data left in message". The error names neither the column nor the row.
    await db.execute_many(
        """INSERT INTO product (product_id, doc_sha, insurer, name, line, attachment, approval, pages, source_url, document_kind)
           VALUES ($1::text,$2::text,$3::text,$4::text,$5::text,$6::text,$7::text,$8::int,$9::text,$10::text)
           ON CONFLICT (product_id) DO UPDATE SET
             doc_sha = excluded.doc_sha, insurer = excluded.insurer, name = excluded.name,
             line = excluded.line, attachment = excluded.attachment, approval = excluded.approval,
             pages = excluded.pages, source_url = excluded.source_url, document_kind = excluded.document_kind""",
        products,
    )

    clauses = [
        (
            r["product_id"],
            r["clause_id"],
            r["kind"],
            _tidy(r["heading"]),
            _tidy(r["verbatim"]),
            r["page"],
            # SQLite held this as a JSON string; Postgres wants a real array. Decoded
            # with the reader that wrote it — hand-splitting on commas and stripping
            # quotes happens to work only while every clause id is comma-free, and the
            # id format is not this module's to guarantee.
            json.decode(r["overrides"] or "[]", type=list[str]),
        )
        for r in src.execute("SELECT * FROM clause")
    ]
    await db.execute_many(
        """INSERT INTO clause (product_id, clause_id, kind, heading, verbatim, page, overrides)
           VALUES ($1::text,$2::text,$3::text,$4::text,$5::text,$6::int,$7::text[])
           ON CONFLICT (product_id, clause_id) DO UPDATE SET
             kind = excluded.kind, heading = excluded.heading, verbatim = excluded.verbatim,
             page = excluded.page, overrides = excluded.overrides""",
        clauses,
    )

    # **After the inserts, and that order is load-bearing.** Each statement here is its
    # own transaction, so a crash between them decides what survives: written-then-
    # withdrawn leaves the new text in and the stale rows still there, which is exactly
    # the state before this delete existed. Withdrawn-then-written would leave a contract
    # with half its clauses and a desk that answers from them.
    #
    # Withdrawn as well as written. An upsert alone cannot retract: a clause the parser
    # used to emit and no longer does stayed in Postgres for good, was rebuilt into both
    # retrieval indexes, and passed the citation check — which validates a model's
    # 〔art.25〕 against this very table. A clause that no longer exists in any contract
    # therefore reached a customer as a clause of their own policy, with a page number.
    #
    # Scoped to the products this load actually carried. A partial run — one directory, a
    # crash halfway — must not read as "every other contract has no clauses".
    #
    # Clauses only. `policy.product_id` references `product` with no `ON DELETE CASCADE`
    # (`20260829000000_initial.sql:126`), so the database refuses to drop a product
    # somebody holds a policy on — and it is right to: retracting the contract text under
    # an issued policy is a decision for a person, not for a rebuild.
    # Counted through a CTE because `db.execute` returns None — a rebuild that silently
    # withdrew four hundred clauses and a rebuild that withdrew none look identical in
    # the log otherwise, and this is the one statement here that destroys anything.
    removed = 0
    if clauses:
        removed = int(
            await db.fetch_val(
                """WITH gone AS (
                     DELETE FROM clause c
                     WHERE c.product_id IN (SELECT DISTINCT p FROM unnest($1::text[]) AS t(p))
                       AND NOT EXISTS (SELECT 1 FROM unnest($1::text[], $2::text[]) AS keep(p, cl)
                                       WHERE keep.p = c.product_id AND keep.cl = c.clause_id)
                     RETURNING 1)
                   SELECT count(*) FROM gone""",
                [[c[0] for c in clauses], [c[1] for c in clauses]],
            )
            or 0
        )

    src.close()
    logger.info("corpus_copied", products=len(products), clauses=len(clauses), withdrawn=removed)
    return len(products), len(clauses)


async def build_catalog(db: Database) -> int:
    """
    Fabricate the issue-age bands, rates and rider compatibility the contracts omit.

    Args:
        db: The database holding the corpus.

    Returns:
        How many catalogue entries were written.

    Only products with at least ten articles get an entry. The rest are brochures and
    term sheets, and a brochure with a premium attached is a lie the suitability step
    would then act on.

    """
    rows = await db.fetch(
        """SELECT p.product_id, p.line, p.attachment, count(c.clause_id) AS clauses
           FROM product p LEFT JOIN clause c USING (product_id)
           WHERE p.document_kind = 'contract'
           GROUP BY p.product_id HAVING count(c.clause_id) >= 10"""
    )

    entries = []
    for row in rows:
        line = row["line"]
        age_min, age_max, max_occupation = _BANDS.get(line, _BANDS["other"])
        unit_label, rate_unit_amount, _, _ = _UNITS.get(line, _UNITS["other"])
        entries.append(
            (
                row["product_id"],
                age_min,
                age_max,
                max_occupation,
                _stable_premium(row["product_id"], line),
                unit_label,
                row["attachment"] == "rider",
                True,
                "synthetic_demo",
                rate_unit_amount,
            )
        )

    # Rewritten only where a derived value actually moved. Every column here except the
    # id comes from `product.line` or `product.attachment`, so an entry that stands still
    # while its product is reclassified describes the line the product used to be in —
    # three contracts renamed out of 認證編號：0610132-31 landed in `life` carrying
    # `other`'s 每單位 and its 1,100 元, sorted to the head of 最便宜的壽險, and were read
    # as the cheapest policies on sale beside real ones priced per 100 萬元保額.
    #
    # **The WHERE compares every column the SET writes, and that is the point.** An
    # earlier version compared `(unit_premium, unit_label)` alone on the reasoning that
    # `_stable_premium` hashes the id together with the line. It does not: it hashes the
    # id only, and takes the line's range from `_UNITS` — so `requires_main`, which comes
    # from `attachment` and not from the line at all, could change with the whole UPDATE
    # skipped, and a rider would go on being offered as a policy somebody can buy alone.
    # Comparing the written tuple needs no reasoning about which input feeds which
    # column, and stays correct when `_BANDS` is next edited.
    #
    # `on_sale` stays out of the SET on purpose: it is the one column a person sets by
    # hand, and a rebuild that reset it to true would put a withdrawn product back on the
    # shelf. Product and clause above take their update unconditionally, because those
    # are parsed out of the PDF and a better parser is a correction — the whitespace fix
    # wrote 11,741 clean clauses into SQLite and every row in Postgres kept the old
    # spacing until that changed.
    await db.execute_many(
        """INSERT INTO catalog_entry
             (product_id, issue_age_min, issue_age_max, max_occupation, unit_premium, unit_label, requires_main, on_sale,
              data_origin, rate_unit_amount)
           VALUES ($1::text,$2::int,$3::int,$4::int,$5::numeric,$6::text,$7::bool,$8::bool,$9::text,$10::int)
           ON CONFLICT (product_id) DO UPDATE SET
             issue_age_min = excluded.issue_age_min, issue_age_max = excluded.issue_age_max,
             max_occupation = excluded.max_occupation, unit_premium = excluded.unit_premium,
             unit_label = excluded.unit_label, requires_main = excluded.requires_main,
             data_origin = excluded.data_origin, rate_unit_amount = excluded.rate_unit_amount
           WHERE catalog_entry.data_origin IN ('unknown', 'synthetic_demo')
             AND (catalog_entry.issue_age_min, catalog_entry.issue_age_max,
                  catalog_entry.max_occupation, catalog_entry.unit_premium,
                  catalog_entry.unit_label, catalog_entry.requires_main,
                  catalog_entry.data_origin, catalog_entry.rate_unit_amount)
                 IS DISTINCT FROM
                 (excluded.issue_age_min, excluded.issue_age_max,
                  excluded.max_occupation, excluded.unit_premium,
                  excluded.unit_label, excluded.requires_main,
                  excluded.data_origin, excluded.rate_unit_amount)""",
        entries,
    )
    logger.info("catalog_built", entries=len(entries))
    return len(entries)


async def refresh_document_kinds(corpus: Path, db: Database) -> dict[str, int]:
    """Classify existing sources from their PDFs without rewriting clauses or policies."""
    rows = await db.fetch(
        """SELECT p.product_id, p.doc_sha,
                  EXISTS (SELECT 1 FROM clause c WHERE c.product_id = p.product_id AND c.clause_id = 'art.1') AS has_first
           FROM product p ORDER BY p.product_id"""
    )

    def classify() -> tuple[list[tuple[str, str]], dict[str, int]]:
        updates = []
        counts: dict[str, int] = {}
        for row in rows:
            path = corpus / f"{row['doc_sha']}.pdf"
            reader = PdfReader(path)
            first_page = reader.pages[0].extract_text() if reader.pages else ""
            kind = document_kind(first_page or "", has_first_article=row["has_first"])
            updates.append((row["product_id"], kind.value))
            counts[kind.value] = counts.get(kind.value, 0) + 1
        return updates, counts

    updates, counts = await asyncio.to_thread(classify)
    await db.execute_many("UPDATE product SET document_kind = $2::text WHERE product_id = $1::text", updates)
    logger.info("document_kinds_refreshed", counts=counts)
    return counts


async def refresh_product_names(corpus: Path, db: Database, *, apply: bool = False) -> dict:
    """
    Correct names from printed cover titles without rewriting clauses, IDs or policy cover.

    An unresolved or multi-product document gets an explicit unknown-name label.
    The report retains its previous name for review; keeping that name in the product
    row would keep presenting rejected headings as verified names. Metadata, filenames
    and mere product references are not evidence; explicit self-definitions are.
    Only the operator's ingest path calls this; it is not a model-callable tool.
    """
    rows = await db.fetch("SELECT product_id, doc_sha, name, document_kind, source_url FROM product ORDER BY product_id")

    def inspect() -> tuple[list[dict], list[dict]]:
        changes, unresolved = [], []
        for row in rows:
            reader = PdfReader(corpus / f"{row['doc_sha']}.pdf")
            first_page = reader.pages[0].extract_text() if reader.pages else ""
            title = _title_of(first_page or "")
            if not title:
                unresolved.append({
                    "product_id": row["product_id"], "name": row["name"].strip(),
                    "document_kind": row["document_kind"], "source_url": row["source_url"],
                })
            name = title or UNRESOLVED_PRODUCT_NAME
            if name != row["name"]:
                changes.append({**row, "new_name": name})
        return changes, unresolved

    changes, unresolved = await asyncio.to_thread(inspect)
    if changes and apply:
        async with db.transaction() as session:
            written = await session.fetch(
                """UPDATE product p SET name = candidate.name
                   FROM unnest($1::text[], $2::text[], $3::text[], $4::text[]) AS candidate(id, sha, old_name, name)
                   WHERE p.product_id = candidate.id AND p.doc_sha = candidate.sha AND p.name = candidate.old_name
                   RETURNING p.product_id""",
                [[row[key] for row in changes] for key in ("product_id", "doc_sha", "name", "new_name")],
            )
            if len(written) != len(changes):
                raise RuntimeError("Product metadata changed during inspection; no name corrections were committed")
    return {
        "inspected": len(rows), "updated": len(changes) if apply else 0,
        "changes": changes, "unresolved": unresolved,
    }
