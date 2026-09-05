"""
Turn the PDF corpus into the relational store.

Classification is by product name, because that is the only field every document in
this corpus reliably has. It is also how the insurer itself organises the catalogue:
a contract called 住院醫療健康保險附約 is a health rider and nothing else, and no
amount of reading the body text makes that more certain than the name already does.

Documents that yield no articles are recorded as `other` rather than discarded. They
are brochures, benefit schedules and structured-product term sheets — 262 of the 660,
and knowing which ones they are is worth more than pretending the corpus is clean.
"""

import re
from typing import TYPE_CHECKING

from msgspec import json

from policydesk.bootloader import logger
from policydesk.clauses.index import ClauseIndex, build_index
from policydesk.core.store import Attachment, Line, Product, connect
from policydesk.ingest.cathay import Manifest

if TYPE_CHECKING:
    from pathlib import Path

# "113.10.01 依 113.06.28 金管保壽字第 11304207572 號函修正" — every filed contract
# carries at least one of these, and it is what makes a version citable.
_APPROVAL = re.compile(r"((?:金管保[壽產險]字|台財保字|國壽字)第\s*\d{6,}\s*號)")

# Order matters: 投資型 wins over 年金 (變額年金 is investment-linked), and 年金 wins
# over 保險 (年金保險 is not life cover). First match takes the document.
_LINE_TOKENS = (
    (("變額", "投資型", "連結", "結構型", "指數連結"), Line.INVESTMENT),
    (("年金",), Line.ANNUITY),
    (("傷害", "意外"), Line.ACCIDENT),
    (
        ("住院", "醫療", "手術", "癌症", "重大疾病", "重大傷病", "特定傷病", "長期照顧", "失能", "健康保險"),
        Line.HEALTH,
    ),
    (("終身保險", "定期保險", "壽險", "死亡", "生死合險"), Line.LIFE),
)


def classify_line(name: str, article_count: int) -> Line:
    """
    Decide which class of 人身保險 a document describes.

    Args:
        name: The product name printed on page one.
        article_count: How many articles the clause parser found. A document with no
            articles is not a contract, whatever its name suggests.

    Returns:
        The product line.

    """
    if article_count == 0:
        return Line.OTHER
    for tokens, line in _LINE_TOKENS:
        if any(t in name for t in tokens):
            return line
    return Line.OTHER


def classify_attachment(name: str) -> Attachment:
    """
    Decide whether the contract stands alone.

    Args:
        name: The product name printed on page one.

    Returns:
        Whether it is a main contract or a rider.

    """
    return Attachment.RIDER if "附約" in name else Attachment.MAIN


def _approval_of(index: ClauseIndex) -> str | None:
    """
    Find the regulatory approval reference, which identifies the contract version.

    Args:
        index: A built clause index.

    Returns:
        The first approval reference found, or None.

    """
    for clause in index.clauses.values():
        if m := _APPROVAL.search(clause.verbatim):
            return re.sub(r"\s+", "", m.group(1))
    return None


def build(corpus: Path, db_path: Path, insurer: str = "國泰人壽") -> tuple[int, int]:
    """
    Index every document in the corpus into the store.

    Args:
        corpus: Directory of fetched PDFs, alongside their manifest.
        db_path: Where to write the database.
        insurer: Who published this corpus.

    Returns:
        How many products and how many clauses were written.

    """
    manifest = Manifest.load(corpus / "manifest.json")
    url_by_sha = {d.sha: d.url for d in manifest.documents.values()}

    conn = connect(db_path)
    products = clauses = 0

    with conn:
        for pdf in sorted(corpus.glob("*.pdf")):
            try:
                index = build_index(pdf)
            except Exception as exc:
                logger.warning("index_skipped", document=pdf.name, error=str(exc))
                continue

            sha = pdf.stem
            articles = sum(1 for c in index.clauses if c.startswith("art.") and c.count(".") == 1)
            product = Product(
                product_id=sha[:12],
                doc_sha=sha,
                insurer=insurer,
                name=index.title,
                line=classify_line(index.title, articles),
                attachment=classify_attachment(index.title),
                pages=max((c.page for c in index.clauses.values()), default=0),
                approval=_approval_of(index),
                source_url=url_by_sha.get(sha),
                document_kind=index.document_kind,
            )
            conn.execute(
                "INSERT OR REPLACE INTO product "
                "(product_id,doc_sha,insurer,name,line,attachment,approval,pages,source_url,document_kind) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    product.product_id,
                    product.doc_sha,
                    product.insurer,
                    product.name,
                    product.line.value,
                    product.attachment.value,
                    product.approval,
                    product.pages,
                    product.source_url,
                    product.document_kind.value,
                ),
            )
            products += 1

            conn.executemany(
                "INSERT OR REPLACE INTO clause VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        product.product_id,
                        c.clause_id,
                        c.kind.value,
                        c.heading,
                        c.verbatim,
                        c.page,
                        json.encode(c.overrides).decode(),
                    )
                    for c in index.clauses.values()
                ],
            )
            clauses += len(index.clauses)

        conn.execute("INSERT INTO clause_fts(clause_fts) VALUES('rebuild')")

    logger.info("store_built", products=products, clauses=clauses)
    return products, clauses
