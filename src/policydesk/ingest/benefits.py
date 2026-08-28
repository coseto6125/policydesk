"""
Recover the benefit tables that the text layer destroys.

A contract prints its payment terms as a table — benefit name, trigger, formula — and
its claim requirements as another — benefit name, documents. Pulling the text layer
out of the PDF loses the column boundaries and leaves a stream in which the multiplier
and the cap are adjacent to nothing. pdfplumber reads the page geometry instead, so
the cells come back separated, which is why this module exists alongside the pypdf
parser rather than replacing it.

The footnote markers are the part worth the effort. A contract does not ask for "a
diagnosis certificate"; it asks for one and then hangs ①②③ off the benefit names,
where the actual requirement lives — 須列明手術或處置名稱及部位. That sentence is what
a claimant gets wrong, so it is stored as its own column rather than glued onto the
document name.

Only health contracts are processed. pdfplumber is roughly ten times slower than the
text layer, and an annuity's tables answer no question this desk is asked.
"""

import re
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pdfplumber

from policydesk.bootloader import logger

if TYPE_CHECKING:
    from pathlib import Path

    from policydesk.core.db import Database

# ①②③④⑤ hang off a benefit name and resolve to a sentence printed under the table.
_MARKERS = "①②③④⑤⑥⑦⑧⑨⑩"
_FOOTNOTE = re.compile(rf"([{_MARKERS}])\s*([^\n①-⑩]{{4,120}})")

# A benefit name always ends in 保險金 or 給付; anything else in that column is a header
# or a continuation line.
_BENEFIT_NAME = re.compile(r"^[^\n]{2,30}?(?:保險金|給付)$")

_DOC_HEADERS = ("申領文件", "應檢附", "檢附文件", "應備文件")
_AMOUNT_HEADERS = ("給付金額", "保險金額", "給付內容")

# "編號 | 手術項目 | 給付倍數" — 附表1, and by far the most common table in the corpus
# (36 of them across four contracts). The clause states 手術給付倍數 ╳ 住院醫療保險金日額
# and prints the multiplier only here, so without this table the formula has no number.
_SCHEDULE_HEADERS = (("手術項目", "surgery"), ("特定處置項目", "procedure"), ("處置項目", "procedure"))
_MULTIPLIER = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,2})?)\s*$")


def _clean(cell: str | None) -> str:
    """
    Flatten one table cell.

    Args:
        cell: What pdfplumber returned, possibly None.

    Returns:
        The cell as one line, with the spaces that PDF justification inserts between
        CJK glyphs removed.

    """
    if not cell:
        return ""
    text = " ".join(cell.split())
    return re.sub(r"(?<=[㐀-鿿])\s+(?=[㐀-鿿])", "", text)


def _footnotes(page_text: str) -> dict[str, str]:
    """
    Read the footnotes printed under a table.

    Args:
        page_text: The page's text.

    Returns:
        Marker to requirement, e.g. {"②": "須列明手術或處置名稱及部位"}.

    """
    return {marker: _clean(body) for marker, body in _FOOTNOTE.findall(page_text)}


def _split_markers(name: str) -> tuple[str, str]:
    """
    Separate a benefit name from the footnote markers attached to it.

    Args:
        name: A cell such as "住院手術醫療保險金②".

    Returns:
        The bare name and the markers it carried.

    """
    markers = "".join(c for c in name if c in _MARKERS)
    return name.rstrip(_MARKERS).strip(), markers


def _schedule_kind(header: str) -> str | None:
    """
    Say which appendix a table is, if it is one.

    Args:
        header: The table's joined header row.

    Returns:
        "surgery", "procedure", or None.

    """
    if "給付倍數" not in header:
        return None
    for token, kind in _SCHEDULE_HEADERS:
        if token in header:
            return kind
    return None


def extract(pdf_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Read one contract's benefit and claim-requirement tables.

    Args:
        pdf_path: The contract.

    Returns:
        Benefit rows, required-document rows and schedule rows.

    A table whose header names neither documents nor an amount is skipped rather than
    guessed at. An unrecognised layout that produces rows is worse than one that
    produces none: the rows reach a customer looking exactly like the real ones.

    """
    benefits: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    schedules: list[dict[str, Any]] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text() or ""
            notes = _footnotes(page_text)

            for table in page.extract_tables() or []:
                if len(table) < 2:
                    continue
                header = " ".join(_clean(c) for c in table[0])

                if (kind := _schedule_kind(header)) is not None:
                    schedules.extend(_schedule_rows(table[1:], kind, page_number))
                    continue

                is_documents = any(h in header for h in _DOC_HEADERS)
                is_amounts = any(h in header for h in _AMOUNT_HEADERS)
                if not (is_documents or is_amounts):
                    continue

                # A benefit name in column one carries the rows under it until the next
                # name appears; contracts leave the cell blank on continuation rows.
                current = ""
                current_markers = ""
                for row in table[1:]:
                    cells = [_clean(c) for c in row]
                    if not any(cells):
                        continue
                    if cells[0] and _BENEFIT_NAME.match(cells[0].rstrip(_MARKERS)):
                        current, current_markers = _split_markers(cells[0])
                    if not current:
                        continue

                    if is_documents:
                        condition = " ".join(notes[m] for m in current_markers if m in notes)
                        documents.extend(
                            {"benefit": current, "document": doc, "condition": condition, "page": page_number}
                            for doc in _documents_in(cells[1:])
                        )
                    else:
                        # A two-column benefit table is 給付項目 | 給付金額, so the
                        # second cell is the formula. Reading it as a trigger left
                        # every formula column empty and every trigger holding a
                        # multiplication.
                        trigger = cells[1] if len(cells) > 2 else ""
                        formula = cells[2] if len(cells) > 2 else (cells[1] if len(cells) > 1 else "")
                        benefits.append({
                            "name": current,
                            "trigger": trigger,
                            "formula": formula,
                            "notes": " ".join(notes[m] for m in current_markers if m in notes),
                            "page": page_number,
                        })

    return (
        _dedupe(benefits, "name"),
        _dedupe(documents, "benefit", "document"),
        _dedupe(schedules, "schedule", "code"),
    )


def _schedule_rows(rows: list[list[str | None]], schedule: str, page: int) -> list[dict[str, Any]]:
    """
    Read an appendix of procedures and their multipliers.

    Args:
        rows: The table's body.
        schedule: "surgery" or "procedure".
        page: Where it was printed.

    Returns:
        One row per procedure that carries a numeric multiplier. A row whose last cell
        is not a number is a section heading or a wrapped line, and is dropped rather
        than stored with a guessed multiplier.

    """
    out = []
    for row in rows:
        cells = [_clean(c) for c in row]
        if len(cells) < 3 or not (m := _MULTIPLIER.match(cells[-1] or "")):
            continue
        code, name = cells[0], cells[1]
        if not name:
            continue
        out.append({
            "schedule": schedule,
            "code": code or name[:12],
            "procedure": name,
            "multiplier": Decimal(m.group(1)),
            "page": page,
        })
    return out


def _documents_in(cells: list[str]) -> list[str]:
    """
    Split a document cell into the numbered items it lists.

    Args:
        cells: The cells after the benefit name.

    Returns:
        One entry per numbered document.

    """
    joined = " ".join(c for c in cells if c)
    if not joined:
        return []
    parts = re.split(r"\d+\s*[.、）)]\s*", joined)
    return [p.strip(" 。;；") for p in parts if len(p.strip(" 。;；")) >= 3]


def _dedupe(rows: list[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    """
    Drop rows repeating a key, keeping the first.

    Args:
        rows: What was extracted.
        keys: Which fields identify a row.

    Returns:
        The rows, unique by those fields. Contracts repeat their benefit table in a
        summary on page one, and the primary key would reject the second copy anyway.

    """
    seen: set[tuple] = set()
    out = []
    for row in rows:
        key = tuple(row[k] for k in keys)
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


async def load(corpus: Path, db: Database, *, line: str = "health", limit: int = 0) -> tuple[int, int, int]:
    """
    Extract and store benefit tables for every contract of one product line.

    Args:
        corpus: Where the PDFs live.
        db: The database.
        line: Which product line to process.
        limit: Stop after this many contracts, or 0 for all.

    Returns:
        How many benefit rows, document rows and schedule rows were written.

    """
    products = await db.fetch(
        """SELECT p.product_id, p.doc_sha, p.name FROM product p
           JOIN catalog_entry ce USING (product_id)
           WHERE p.line = $1::text ORDER BY p.product_id""",
        [line],
    )
    if limit:
        products = products[:limit]

    benefit_rows: list[tuple] = []
    document_rows: list[tuple] = []
    schedule_rows: list[tuple] = []
    parsed = 0

    for product in products:
        path = corpus / f"{product['doc_sha']}.pdf"
        if not path.exists():
            continue
        try:
            benefits, documents, schedules = extract(path)
        except Exception as exc:
            logger.warning("benefit_extract_failed", product=product["product_id"], error=str(exc))
            continue
        parsed += 1
        benefit_rows.extend(
            (product["product_id"], b["name"], b["trigger"], b["formula"], b["notes"], b["page"]) for b in benefits
        )
        document_rows.extend(
            (product["product_id"], d["benefit"], d["document"], d["condition"], d["page"]) for d in documents
        )
        schedule_rows.extend(
            (product["product_id"], s["schedule"], s["code"], s["procedure"], s["multiplier"], s["page"])
            for s in schedules
        )

    await db.execute_many(
        """INSERT INTO benefit (product_id, name, trigger, formula, notes, page)
           VALUES ($1::text,$2::text,$3::text,$4::text,$5::text,$6::int)
           ON CONFLICT (product_id, name) DO NOTHING""",
        benefit_rows,
    )
    await db.execute_many(
        """INSERT INTO required_document (product_id, benefit, document, condition, page)
           VALUES ($1::text,$2::text,$3::text,$4::text,$5::int)
           ON CONFLICT (product_id, benefit, document) DO NOTHING""",
        document_rows,
    )

    await db.execute_many(
        """INSERT INTO surgery_multiplier (product_id, schedule, code, procedure, multiplier, page)
           VALUES ($1::text,$2::text,$3::text,$4::text,$5::numeric,$6::int)
           ON CONFLICT (product_id, schedule, code) DO NOTHING""",
        schedule_rows,
    )

    logger.info(
        "benefits_loaded", contracts=parsed, benefits=len(benefit_rows),
        documents=len(document_rows), schedules=len(schedule_rows),
    )
    return len(benefit_rows), len(document_rows), len(schedule_rows)
