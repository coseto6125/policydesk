"""
Rebuild the corpus end to end: parse the PDFs, then load what came out.

Run as `policydesk-ingest`. Three steps that were three separate invocations, and the
middle two had no caller at all — `copy_corpus` and `build_catalog` were imported by the
compose `ingest` service and never run by it, so the documented path fetched 1.19 GB of
PDFs and stopped. Every parser improvement since then reached SQLite and no further.

The order is not a preference. `build` writes SQLite from the PDFs, `copy_corpus` moves
products and clauses into Postgres, and `build_catalog` reads `product.line` back out to
derive the rate card — so a line the parser has just corrected has to be in Postgres
before the catalogue is rebuilt against it.

Not run at startup, and not on a schedule. It reparses 661 documents and rewrites 11,741
rows; a desk that did that while somebody was mid-conversation would change the contract
text under a reply already being written.
"""

import asyncio
import sys
from pathlib import Path

from policydesk.bootloader import logger
from policydesk.core.db import Database
from policydesk.ingest.build_db import build
from policydesk.ingest.to_postgres import build_catalog, copy_corpus

CORPUS = Path("data/cathay")
STORE = Path("data/policydesk.db")


async def rebuild(corpus: Path = CORPUS, store: Path = STORE) -> None:
    """
    Parse the corpus and load it, in the one order that works.

    Args:
        corpus: Directory of fetched PDFs, alongside their manifest.
        store: Where the parser's SQLite index lives.

    """
    products, clauses = await asyncio.to_thread(build, corpus, store)
    logger.info("ingest_parsed", products=products, clauses=clauses, store=str(store))
    db = Database()
    copied_products, copied_clauses = await copy_corpus(store, db)
    logger.info("ingest_copied", products=copied_products, clauses=copied_clauses)
    entries = await build_catalog(db)
    logger.info("ingest_catalogued", entries=entries)


def main() -> None:
    """Entry point for `policydesk-ingest`."""
    corpus = Path(sys.argv[1]) if len(sys.argv) > 1 else CORPUS
    if not corpus.is_dir():
        logger.error("corpus_absent", path=str(corpus), hint="fetch it first; see README")
        raise SystemExit(1)
    asyncio.run(rebuild(corpus))


if __name__ == "__main__":
    main()
