"""
The contract corpus as a queryable store.

A PDF is a document; a product is a record. The desk needs to ask "which of this
person's policies pay for a four-day hospital stay" and get an answer in one query,
which a folder of 660 PDFs cannot give.

SQLite rather than PostgreSQL, deliberately: the demo runs with the network unplugged,
and a file that ships with the repo has no service to start, no credentials to leak,
and no way to differ between the machine it was built on and the machine it runs on.

The schema records one thing per table and refuses to record what it must not hold.
There is no commission column anywhere. When a judge asks how the recommendation
avoids steering people to the highest-paying product, the answer is not a prompt
instruction — it is that the number is not in the database.
"""

import sqlite3
from enum import StrEnum
from typing import TYPE_CHECKING

from msgspec import Struct

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS product (
    product_id   TEXT PRIMARY KEY,
    doc_sha      TEXT NOT NULL,
    insurer      TEXT NOT NULL,
    name         TEXT NOT NULL,
    line         TEXT NOT NULL,
    attachment   TEXT NOT NULL,
    approval     TEXT,
    pages        INTEGER NOT NULL,
    source_url   TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS clause (
    product_id   TEXT NOT NULL REFERENCES product(product_id) ON DELETE CASCADE,
    clause_id    TEXT NOT NULL,
    kind         TEXT NOT NULL,
    heading      TEXT NOT NULL,
    verbatim     TEXT NOT NULL,
    page         INTEGER NOT NULL,
    overrides    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (product_id, clause_id)
) STRICT;

CREATE INDEX IF NOT EXISTS clause_by_kind ON clause(kind);

CREATE VIRTUAL TABLE IF NOT EXISTS clause_fts USING fts5(
    verbatim, heading, product_id UNINDEXED, clause_id UNINDEXED,
    content='clause', content_rowid='rowid', tokenize='trigram'
);
"""


BENEFIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS benefit (
    product_id   TEXT NOT NULL REFERENCES product(product_id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    trigger      TEXT NOT NULL DEFAULT '',
    formula      TEXT NOT NULL DEFAULT '',
    notes        TEXT NOT NULL DEFAULT '',
    page         INTEGER NOT NULL,
    PRIMARY KEY (product_id, name)
) STRICT;

CREATE TABLE IF NOT EXISTS required_document (
    product_id   TEXT NOT NULL REFERENCES product(product_id) ON DELETE CASCADE,
    benefit      TEXT NOT NULL,
    document     TEXT NOT NULL,
    condition    TEXT NOT NULL DEFAULT '',
    page         INTEGER NOT NULL,
    PRIMARY KEY (product_id, benefit, document)
) STRICT;
"""

# `condition` carries the footnote a contract hangs off a benefit name — "須列明手術或
# 處置名稱及部位". That is the part a claimant actually gets wrong: they send a diagnosis
# certificate, but not one that names the site. Checking it is work the desk can do and
# a caseworker would otherwise do by hand, so it is stored as its own field rather than
# glued onto the document name.

# Trigram tokenising, because the queries are Chinese and the default tokeniser splits
# on whitespace — which a 條款 sentence does not have.


class Line(StrEnum):
    """Which class of 人身保險 a product belongs to (保險法 §13)."""

    LIFE = "life"
    HEALTH = "health"
    ACCIDENT = "accident"
    ANNUITY = "annuity"
    INVESTMENT = "investment"
    """投資型. Carries a policy contract but pays on unit value, so claim logic differs."""
    OTHER = "other"
    """Filed under the insurer's product list but not a policy contract: brochures,
    structured-product term sheets, benefit schedules."""


class Attachment(StrEnum):
    """Whether the contract stands alone."""

    MAIN = "main"
    """主契約."""
    RIDER = "rider"
    """附約. Cannot exist without a main contract, and its waiting period and
    effective date are frequently anchored to the main one."""


class Product(Struct, frozen=True):
    """
    One insurance product as the desk records it.

    There is no premium and no commission field. Premium depends on age, sex, term and
    payment mode and belongs to a rating engine; commission belongs to nobody here.
    """

    product_id: str
    doc_sha: str
    insurer: str
    name: str
    line: Line
    attachment: Attachment
    pages: int
    approval: str | None = None
    source_url: str | None = None


def connect(db_path: Path) -> sqlite3.Connection:
    """
    Open the store, creating the schema when it is absent.

    Args:
        db_path: Where the database file lives.

    Returns:
        An open connection with foreign keys enforced.

    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    conn.executescript(BENEFIT_SCHEMA)
    return conn
