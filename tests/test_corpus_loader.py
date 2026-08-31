"""
What the loader is allowed to overwrite, and what it must leave alone.

The three upserts in `to_postgres` disagree on purpose, and nothing else records why.
Asserted against the module's own SQL because there is no other guard: the failure is
silent — a rebuild reports the same counts either way, and the tables keep whatever they
already held.
"""

import re
from pathlib import Path

SOURCE = Path("src/policydesk/ingest/to_postgres.py").read_text(encoding="utf-8")


def _conflict_clause(table: str) -> str:
    # The table name is followed by a space on two of the three and by a newline on the
    # third, where the column list wrapped.
    found = re.search(rf"INSERT INTO {table}\s", SOURCE)
    assert found, f"{table} is not loaded here any more"
    return SOURCE[found.start() : SOURCE.index('"""', found.start())]


def test_a_reparsed_clause_reaches_the_table():
    """
    `DO NOTHING` froze the corpus against its own parser.

    The whitespace fix wrote clean text into SQLite for all 11,741 clauses and none of
    them changed in Postgres, because this loader could not correct a row it had already
    written. Every improvement to the PDF parser was landing nowhere.
    """
    assert "DO UPDATE" in _conflict_clause("clause"), "a clause the parser corrected must reach the table"
    assert "verbatim = excluded.verbatim" in _conflict_clause("clause")


def test_a_reparsed_product_reaches_the_table():
    # Same reason: 22 contracts were named after their approval number, and fixing the
    # title filter changed nothing until the name could be written over.
    assert "DO UPDATE" in _conflict_clause("product")
    assert "name = excluded.name" in _conflict_clause("product")


def test_a_rebuild_moves_a_premium_only_where_the_line_moved():
    """
    `catalog_entry` is fabricated, and a member's policy bills against its `unit_premium`.

    Held at `DO NOTHING` first, so a rebuild could not move anybody's premium. That was
    too blunt once `product` began to update: three contracts renamed out of
    認證編號：0610132-31 reclassified into `life` and kept `other`'s 每單位 and its
    1,100 元, which sorted them to the head of 最便宜的壽險 beside real policies priced
    per 100 萬元保額. Every column here except the id is a function of the line.

    The guard is the WHERE, not the conflict action. `_stable_premium` hashes the id
    with the line, so an unchanged line produces the identical figure and nothing is
    written — a rebuild that reclassifies no contract still moves no premium.
    """
    conflict = _conflict_clause("catalog_entry")
    assert "DO UPDATE" in conflict
    assert "IS DISTINCT FROM" in conflict, "an unconditional update moves premiums on every rebuild"
    assert "unit_premium = excluded.unit_premium" in conflict


def test_every_upsert_states_which_of_the_three_it_is():
    # A fourth table added later gets the same decision made deliberately: parsed from
    # the contract and always updated, fabricated and updated only where its inputs
    # moved, or neither.
    inserts = re.findall(r"INSERT INTO (\w+)\s", SOURCE)
    assert set(inserts) == {"product", "clause", "catalog_entry"}, (
        f"a table was added to the loader without a conflict rule this file names: {inserts}"
    )
