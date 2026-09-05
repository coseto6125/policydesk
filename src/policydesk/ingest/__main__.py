"""
Rebuild the corpus end to end: parse the PDFs, then load what came out.

Run as `policydesk-ingest`. Four steps that were four separate invocations, and the
middle two had no caller at all — `copy_corpus` and `build_catalog` were imported by the
compose `ingest` service and never run by it, so the documented path fetched 1.19 GB of
PDFs and stopped. Every parser improvement since then reached SQLite and no further.

The order is not a preference. `build` writes SQLite from the PDFs, `copy_corpus` moves
products and clauses into Postgres, and `build_catalog` reads `product.line` back out to
derive the rate card — so a line the parser has just corrected has to be in Postgres
before the catalogue is rebuilt against it.

Not run at startup, and not on a schedule. It reparses 660 documents and rewrites 11,741
rows; a desk that did that while somebody was mid-conversation would change the contract
text under a reply already being written.
"""

import asyncio
import sys
from pathlib import Path

from policydesk.bootloader import logger
from policydesk.core.db import Database
from policydesk.ingest.build_db import build
from policydesk.ingest.cathay import fetch_all
from policydesk.ingest.to_postgres import build_catalog, copy_corpus, refresh_document_kinds
from policydesk.retrieval.__main__ import rebuild as index_rebuild

CORPUS = Path("data/cathay")
STORE = Path("data/policydesk.db")


async def rebuild(corpus: Path = CORPUS, store: Path = STORE, *, fetch: bool = False) -> None:
    """
    Parse the corpus and load it, in the one order that works.

    Args:
        corpus: Directory of fetched PDFs, alongside their manifest.
        store: Where the parser's SQLite index lives.
        fetch: Download the contracts first. Off by default because it is 1.19 GB over
            somebody's network and the usual reason to run this is a parser change, not
            a missing corpus.

    """
    if fetch:
        await fetch_all(corpus)
        logger.info("ingest_fetched", corpus=str(corpus))
    products, clauses = await asyncio.to_thread(build, corpus, store)
    logger.info("ingest_parsed", products=products, clauses=clauses, store=str(store))
    db = Database()
    copied_products, copied_clauses = await copy_corpus(store, db)
    logger.info("ingest_copied", products=copied_products, clauses=copied_clauses)
    entries = await build_catalog(db)
    logger.info("ingest_catalogued", entries=entries)
    # The fourth step, and it is not optional. `open_index` builds only when `meta.json`
    # is absent, so an existing BM25 index never refreshes on its own: a parser fix would
    # reach Postgres and the desk would go on ranking against the text it replaced. The
    # vector half takes about an hour on CPU, which is why this is the end of the command
    # rather than something a developer is trusted to remember afterwards.
    await index_rebuild()
    logger.info("ingest_reindexed")


def main() -> None:
    """
    Entry point for `policydesk-ingest [--fetch | --source-kinds-only] [corpus]`.

    `--fetch` folds the download in, so the whole pipeline is one command with one entry
    point. It was four stages and the first three had scripts; the download sat in
    `docker-compose.yml` as a `python -c` string nested inside an `sh -c` string inside a
    YAML block scalar, which is four levels of quoting for the one stage nobody could
    run by name.
    """
    args = [a for a in sys.argv[1:] if a not in {"--fetch", "--source-kinds-only"}]
    corpus = Path(args[0]) if args else CORPUS
    fetch = "--fetch" in sys.argv[1:]
    if "--source-kinds-only" in sys.argv[1:]:
        async def refresh() -> None:
            db = Database()
            try:
                await refresh_document_kinds(corpus, db)
            finally:
                await db.close()
        asyncio.run(refresh())
        return
    if not fetch and not corpus.is_dir():
        logger.error("corpus_absent", path=str(corpus), hint="pass --fetch, or see README")
        raise SystemExit(1)
    asyncio.run(rebuild(corpus, fetch=fetch))


if __name__ == "__main__":
    main()
