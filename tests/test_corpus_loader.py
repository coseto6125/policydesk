"""
What the loader is allowed to overwrite, and what it must leave alone.

The three upserts in `to_postgres` disagree on purpose, and nothing else records why.
Asserted against the module's own SQL because there is no other guard: the failure is
silent — a rebuild reports the same counts either way, and the tables keep whatever they
already held.
"""

import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from msgspec import json

from policydesk.agent.tools import insured_amount
from policydesk.ingest.to_postgres import build_catalog

SOURCE = Path("src/policydesk/ingest/to_postgres.py").read_text(encoding="utf-8")


async def test_copy_corpus_legacy_ideographs_are_normalized_before_postgres(tmp_path):
    from policydesk.core.store import connect
    from policydesk.ingest.to_postgres import copy_corpus

    path = tmp_path / "legacy.db"
    source = connect(path)
    source.execute(
        "INSERT INTO product (product_id,doc_sha,insurer,name,line,attachment,pages) VALUES (?,?,?,?,?,?,?)",
        ("p", "sha", "test", "一" + chr(0xF98E) + "期（Ａ）", "health", "main", 1),
    )
    source.execute(
        "INSERT INTO clause (product_id,clause_id,kind,heading,verbatim,page) VALUES (?,?,?,?,?,?)",
        ("p", "art.1", "procedure", "申" + chr(0xF9B4), "受 " + chr(0xFA17) + " 人（Ａ）：文件。", 1),
    )
    source.commit()
    source.close()
    db = AsyncMock()
    db.fetch_val.return_value = 0
    assert await copy_corpus(path, db) == (1, 1)
    product_call, clause_call = db.execute_many.call_args_list
    assert product_call.args[1][0][3] == "一年期（Ａ）"
    assert clause_call.args[1][0][3:5] == ("申領", "受益人（Ａ）：文件。")








def test_connect_legacy_corpus_adds_unknown_source_kind_without_losing_product(tmp_path):
    import sqlite3

    from policydesk.core.store import connect

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as legacy:
        legacy.execute("CREATE TABLE product (product_id TEXT PRIMARY KEY, name TEXT)")
        legacy.execute("INSERT INTO product VALUES ('existing', '原有商品')")
    migrated = connect(path)
    try:
        assert migrated.execute("SELECT product_id,name,document_kind FROM product").fetchone() == (
            "existing", "原有商品", "unknown",
        )
    finally:
        migrated.close()


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


async def test_a_clause_the_parser_stopped_emitting_is_withdrawn(tmp_path):
    """
    An upsert cannot retract, and the citation check validates against this table.

    A clause the parser used to emit and no longer does stayed in Postgres for good, was
    rebuilt into both retrieval indexes, and passed the check that validates a model's
    〔art.25〕 — so a clause belonging to no contract could reach a customer as a clause
    of their own policy, page number and all.

    Driven through `copy_corpus` against the real database rather than by matching the
    DELETE's text: the conflict target, the scoping to loaded products, and the
    `NOT EXISTS` are three ways to write this wrong that a string assertion cannot tell
    apart. The row is planted and removed inside the test, and the corpus it plants
    beside is the real one, so a mistake in the scoping shows up as the rest of that
    product's clauses disappearing.
    """
    import sqlite3

    from policydesk.core.db import Database
    from policydesk.ingest.to_postgres import copy_corpus

    db = Database()
    try:
        await db.fetch_val("SELECT 1")
    except Exception:
        pytest.skip("policydesk-pg is not up")

    product_id = await db.fetch_val("SELECT product_id FROM clause GROUP BY product_id ORDER BY count(*) DESC LIMIT 1")
    if product_id is None:
        pytest.skip("no product carries a clause")
    held = await db.fetch(
        "SELECT clause_id, kind, heading, verbatim, page, overrides FROM clause WHERE product_id = $1::text",
        [str(product_id)],
    )

    # A SQLite index holding exactly what the parser emits now: this product's clauses,
    # and not the invented one.
    store = tmp_path / "corpus.db"
    src = sqlite3.connect(store)
    src.execute(
        "CREATE TABLE product (product_id TEXT PRIMARY KEY, doc_sha TEXT, insurer TEXT, name TEXT,"
        " line TEXT, attachment TEXT, approval TEXT, pages INT, source_url TEXT, document_kind TEXT)"
    )
    src.execute(
        "CREATE TABLE clause (product_id TEXT, clause_id TEXT, kind TEXT, heading TEXT,"
        " verbatim TEXT, page INT, overrides TEXT)"
    )
    row = await db.fetch_one(
        "SELECT doc_sha, insurer, name, line, attachment, approval, pages, source_url, document_kind"
        " FROM product WHERE product_id = $1::text",
        [str(product_id)],
    )
    src.execute("INSERT INTO product VALUES (?,?,?,?,?,?,?,?,?,?)", (str(product_id), *row.values()))
    src.executemany(
        "INSERT INTO clause VALUES (?,?,?,?,?,?,?)",
        [(str(product_id), c["clause_id"], c["kind"], c["heading"], c["verbatim"], c["page"],
          json.encode(c["overrides"]).decode()) for c in held],
    )
    src.commit()

    await db.execute(
        "INSERT INTO clause (product_id, clause_id, kind, heading, verbatim, page, overrides)"
        " VALUES ($1::text, 'art.9999', 'grant', '撤回測試', '這一條不存在於任何契約', 1, $2::text[])"
        " ON CONFLICT (product_id, clause_id) DO NOTHING",
        [str(product_id), []],
    )
    planted = await db.fetch_val(
        "SELECT count(*) FROM clause WHERE product_id = $1::text AND clause_id = 'art.9999'", [str(product_id)]
    )
    assert int(planted) == 1, "the row to be withdrawn was not planted"

    try:
        await copy_corpus(store, db)
        left = await db.fetch_val(
            "SELECT count(*) FROM clause WHERE product_id = $1::text AND clause_id = 'art.9999'", [str(product_id)]
        )
        assert int(left) == 0, "a clause the parser no longer emits survived the reload"
        kept = await db.fetch_val("SELECT count(*) FROM clause WHERE product_id = $1::text", [str(product_id)])
        assert int(kept) == len(held), f"the reload took {len(held) - int(kept)} clauses it should have kept"
        retained = await db.fetch("SELECT clause_id,overrides FROM clause WHERE product_id=$1::text", [str(product_id)])
        assert {row["clause_id"]: row["overrides"] for row in retained} == {
            row["clause_id"]: row["overrides"] for row in held
        }
        others = await db.fetch_val("SELECT count(*) FROM clause WHERE product_id <> $1::text", [str(product_id)])
        assert int(others) > 0, "a load carrying one product emptied every other product"
    finally:
        await db.execute(
            "DELETE FROM clause WHERE product_id = $1::text AND clause_id = 'art.9999'", [str(product_id)]
        )


def test_the_reload_writes_before_it_withdraws():
    """
    Each statement in `copy_corpus` is its own transaction, so the order decides the
    wreckage. Written-then-withdrawn leaves the new text in and the stale rows present,
    which is the state this delete was added to improve on. Withdrawn-then-written
    leaves a contract holding half its clauses, and a desk that answers from them.

    Asserted by position because there is no way to observe it: a crash between two
    statements is not something a test can stage, and the failure it produces looks like
    a corpus that was always short.
    """
    insert = SOURCE.index("INSERT INTO clause ")
    delete = SOURCE.index("DELETE FROM clause c")
    assert insert < delete, "the reload withdraws before it writes; a crash between them empties contracts"
