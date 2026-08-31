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


def test_a_rebuild_never_moves_an_issued_policy_s_premium():
    """
    `catalog_entry` is fabricated, and a policy bills against its `unit_premium`.

    `_stable_premium` is deterministic, so a rebuild would usually write the same figure
    back — usually is not a property to bill somebody on. A contract that reclassified
    into another line would take a new rate with it, and the customer's premium would
    move on a rebuild that touched no contract of theirs.
    """
    assert "DO NOTHING" in _conflict_clause("catalog_entry")


def test_every_upsert_states_which_of_the_two_it_is():
    # A fourth table added later gets the same decision made deliberately: parsed from
    # the contract, or fabricated beside it.
    inserts = re.findall(r"INSERT INTO (\w+)\s", SOURCE)
    assert set(inserts) == {"product", "clause", "catalog_entry"}, (
        f"a table was added to the loader without a conflict rule this file names: {inserts}"
    )
