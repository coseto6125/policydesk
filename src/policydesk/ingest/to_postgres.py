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

import sqlite3
from decimal import Decimal
from hashlib import blake2b
from typing import TYPE_CHECKING

from msgspec import json

from policydesk.bootloader import logger

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

_UNITS: dict[str, tuple[str, int, int]] = {
    # line: (unit_label, min annual premium per unit, max)
    "health": ("每日 1,000 元住院日額", 1200, 4800),
    "accident": ("每 100 萬元保額", 900, 3600),
    "life": ("每 100 萬元保額", 6000, 42000),
    "annuity": ("每 10 萬元年金", 8000, 30000),
    "investment": ("每 10 萬元保額", 5000, 25000),
    "other": ("每單位", 1000, 5000),
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
    _, low, high = _UNITS.get(line, _UNITS["other"])
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

    products = [
        (
            r["product_id"],
            r["doc_sha"],
            r["insurer"],
            r["name"],
            r["line"],
            r["attachment"],
            r["approval"],
            r["pages"],
            r["source_url"],
        )
        for r in src.execute("SELECT * FROM product")
    ]
    # Every parameter is cast explicitly. execute_many prepares once and infers the
    # parameter types from the FIRST row, so a leading NULL in a nullable column —
    # approval is null for 571 of 660 documents — leaves that parameter typed unknown
    # and the first row that does carry a value fails the batch with "insufficient
    # data left in message". The error names neither the column nor the row.
    await db.execute_many(
        """INSERT INTO product (product_id, doc_sha, insurer, name, line, attachment, approval, pages, source_url)
           VALUES ($1::text,$2::text,$3::text,$4::text,$5::text,$6::text,$7::text,$8::int,$9::text)
           ON CONFLICT (product_id) DO UPDATE SET
             doc_sha = excluded.doc_sha, insurer = excluded.insurer, name = excluded.name,
             line = excluded.line, attachment = excluded.attachment, approval = excluded.approval,
             pages = excluded.pages, source_url = excluded.source_url""",
        products,
    )

    clauses = [
        (
            r["product_id"],
            r["clause_id"],
            r["kind"],
            r["heading"],
            r["verbatim"],
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

    src.close()
    logger.info("corpus_copied", products=len(products), clauses=len(clauses))
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
           GROUP BY p.product_id HAVING count(c.clause_id) >= 10"""
    )

    entries = []
    for row in rows:
        line = row["line"]
        age_min, age_max, max_occupation = _BANDS.get(line, _BANDS["other"])
        unit_label = _UNITS.get(line, _UNITS["other"])[0]
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
            )
        )

    # `DO NOTHING` here, and `DO UPDATE` on product and clause above. The difference is
    # what each row is: product and clause are parsed out of the PDF, so a better parser
    # is a correction and must reach the table — the whitespace fix wrote clean text into
    # SQLite and every one of the 11,741 rows in Postgres kept the old spacing, because
    # this loader could not correct a row it had already written. A catalog_entry is
    # fabricated (`_stable_premium`), and a member's policy bills against `unit_premium`;
    # overwriting it would move somebody's premium on a rebuild that touched no contract.
    await db.execute_many(
        """INSERT INTO catalog_entry
             (product_id, issue_age_min, issue_age_max, max_occupation, unit_premium, unit_label, requires_main, on_sale)
           VALUES ($1::text,$2::int,$3::int,$4::int,$5::numeric,$6::text,$7::bool,$8::bool)
           ON CONFLICT (product_id) DO NOTHING""",
        entries,
    )
    logger.info("catalog_built", entries=len(entries))
    return len(entries)
