"""
What the loader is allowed to overwrite, and what it must leave alone.

The three upserts in `to_postgres` disagree on purpose, and nothing else records why.
Asserted against the module's own SQL because there is no other guard: the failure is
silent — a rebuild reports the same counts either way, and the tables keep whatever they
already held.
"""

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
        ("p", "sha", "test", "  一" + chr(0xF98E) + "期（Ａ）  ", "health", "main", 1),
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


@pytest.mark.parametrize("conflict", [False, True])
async def test_refresh_product_names_printed_title_updates_only_unchanged_source(tmp_path, monkeypatch, conflict):
    from policydesk.ingest import to_postgres

    old_name = "本商品說明書僅供參考，詳細內容請以保險單條款為準"
    title = "國泰人壽安心終身壽險"
    page = SimpleNamespace(extract_text=lambda: f"{old_name}\n{title}\n商品說明書")
    monkeypatch.setattr(to_postgres, "PdfReader", lambda path: SimpleNamespace(pages=[page]))
    pool = AsyncMock()
    pool.fetch.return_value = [{"product_id": "p", "doc_sha": "sha", "name": old_name,
                                "document_kind": "brochure", "source_url": "fixture"}]
    session = AsyncMock()
    session.fetch.return_value = [] if conflict else [{"product_id": "p"}]
    pool.transaction = MagicMock()
    context = pool.transaction.return_value
    context.__aenter__.return_value = session
    if conflict:
        with pytest.raises(RuntimeError, match="metadata changed"):
            await to_postgres.refresh_product_names(tmp_path, pool, apply=True)
        assert context.__aexit__.call_args.args[0] is RuntimeError
    else:
        report = await to_postgres.refresh_product_names(tmp_path, pool, apply=True)
        assert report["inspected"] == report["updated"] == 1
        assert report["unresolved"] == []
        assert report["changes"][0]["new_name"] == title
    sql, parameters = session.fetch.call_args.args
    assert parameters == [["p"], ["sha"], [old_name], [title]]
    assert "p.doc_sha = candidate.sha" in sql
    assert "p.name = candidate.old_name" in sql
    assert "UPDATE product p SET name" in sql
    pool.execute_many.assert_not_awaited()


async def test_refresh_product_names_unresolved_is_reported_not_guessed(tmp_path, monkeypatch):
    from policydesk.ingest import to_postgres

    page = SimpleNamespace(extract_text=lambda: "合作廠商資料及查閱方式\n公司地址及電話")
    monkeypatch.setattr(to_postgres, "PdfReader", lambda path: SimpleNamespace(pages=[page], metadata={"/Title": "不可信商品名"}))
    row = {"product_id": "p", "doc_sha": "sha", "name": "合作廠商資料及查閱方式",
           "document_kind": "unknown", "source_url": "fixture"}
    pool = AsyncMock()
    pool.fetch.return_value = [row]
    report = await to_postgres.refresh_product_names(tmp_path, pool)
    assert report["updated"] == 0
    assert report["unresolved"] == [{key: row[key] for key in ("product_id", "name", "document_kind", "source_url")}]
    pool.transaction.assert_not_called()


async def test_build_catalog_health_sum_insured_does_not_invent_daily_benefit():
    db = AsyncMock()
    db.fetch.return_value = [{"product_id": "major-illness", "line": "health", "attachment": "main", "clauses": 20}]
    await build_catalog(db)
    entry = db.execute_many.call_args.args[1][0]
    assert insured_amount(1000, entry[5]) == "1,000 元"
    assert entry[8] == "synthetic_demo"
    assert entry[9] == 1000


async def test_build_catalog_declared_source_is_not_overwritten_by_demo(db):
    from decimal import Decimal
    from uuid import uuid4

    product_id = f"catalog-source-test-{uuid4().hex}"
    try:
        await db.execute(
            """INSERT INTO product (product_id,doc_sha,insurer,name,line,attachment,document_kind)
               VALUES ($1::text,$1::text,'test','source fixture','health','main','contract')""",
            [product_id],
        )
        await db.execute_many(
            """INSERT INTO clause (product_id,clause_id,kind,heading,verbatim,page)
               VALUES ($1::text,$2::text,'grant','fixture','fixture',1)""",
            [(product_id, f"art.{number}") for number in range(1, 11)],
        )
        await db.execute(
            """INSERT INTO catalog_entry
               (product_id,issue_age_min,issue_age_max,max_occupation,unit_premium,unit_label,
                data_origin,rate_unit_amount,on_sale)
               VALUES ($1::text,10,60,2,$2::numeric,'fixture unit','curated_fixture',100,false)""",
            [product_id, Decimal(123)],
        )
        before = await db.fetch_one("SELECT * FROM catalog_entry WHERE product_id=$1::text", [product_id])
        await build_catalog(db)
        assert await db.fetch_one("SELECT * FROM catalog_entry WHERE product_id=$1::text", [product_id]) == before
    finally:
        await db.execute("DELETE FROM product WHERE product_id=$1::text", [product_id])


async def test_source_views_brochure_and_unknown_never_become_contract_evidence(db):
    from decimal import Decimal
    from uuid import uuid4

    from policydesk.agent.tools import _clauses_by_id, suitable_products
    prefix = f"source-test-{uuid4().hex}"
    ids = [f"{prefix}-{kind}" for kind in ("contract", "brochure", "unknown")]
    try:
        await db.execute_many(
            """INSERT INTO product (product_id,doc_sha,insurer,name,line,attachment,document_kind)
               VALUES ($1::text,$1::text,'test','source fixture','life','main',$2::text)""",
            list(zip(ids, ["contract", "brochure", "unknown"], strict=True)),
        )
        await db.execute_many(
            """INSERT INTO clause (product_id,clause_id,kind,heading,verbatim,page)
               VALUES ($1::text,'art.1','grant','保障範圍','條款原文',1)""",
            [(product,) for product in ids],
        )
        await db.execute_many(
            """INSERT INTO catalog_entry
               (product_id,issue_age_min,issue_age_max,max_occupation,unit_premium,unit_label)
               VALUES ($1::text,99,99,6,$2::numeric,'每單位')""",
            [(product, Decimal(1)) for product in ids],
        )
        evidence = await _clauses_by_id(db, [(product, "art.1") for product in ids])
        assert [row["product_id"] for row in evidence] == ids[:1]
        offers = await suitable_products(db, insurance_age=99, occupation_class=1, budget=1, line="life")
        assert [row["product_id"] for row in offers] == ids[:1]
        assert offers[0]["data_origin"] == "unknown"
        assert offers[0]["rate_unit_amount"] is None
        await db.execute("UPDATE product SET document_kind='brochure' WHERE product_id=$1::text", [ids[0]])
        assert await _clauses_by_id(db, [(ids[0], "art.1")]) == []
        assert await db.fetch_val("SELECT count(*) FROM clause WHERE product_id=ANY($1::text[])", [ids]) == 3
    finally:
        await db.execute("DELETE FROM product WHERE product_id=ANY($1::text[])", [ids])


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


async def test_a_clause_the_parser_stopped_emitting_is_withdrawn(tmp_path, db):
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

    from policydesk.ingest.to_postgres import copy_corpus

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
