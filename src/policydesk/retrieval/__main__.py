"""
Build both retrieval indexes from Postgres.

Run as `policydesk-index`. Both are caches: delete `data/bm25` or `data/vectors` and this
rebuilds them from the tables, so nothing here is a source of truth.

Kept out of server startup on purpose. The BM25 build is two minutes and the app does it
on demand; the vector build is forty and would hold the port shut for all of them.
"""

import asyncio
import shutil
import sys
import time

from policydesk.bootloader import logger
from policydesk.core.db import Database
from policydesk.retrieval import index, vectors


async def rebuild(*, lexical: bool = True, semantic: bool = True) -> None:
    """
    Rebuild the indexes that were asked for.

    Args:
        lexical: Whether to rebuild the BM25 index.
        semantic: Whether to rebuild the vector matrix.

    """
    db = Database()
    if lexical:
        started = time.perf_counter()
        await asyncio.to_thread(shutil.rmtree, index.INDEX_DIR, ignore_errors=True)
        count = await index.build(db)
        logger.info("index_rebuilt", channel="bm25", documents=count, seconds=round(time.perf_counter() - started, 1))
    if semantic:
        started = time.perf_counter()
        await asyncio.to_thread(shutil.rmtree, vectors.VECTOR_DIR, ignore_errors=True)
        count = await vectors.build(db)
        logger.info(
            "index_rebuilt", channel="embedding", documents=count, seconds=round(time.perf_counter() - started, 1)
        )


def main() -> None:
    """Rebuild both channels, or the one named on the command line."""
    wanted = set(sys.argv[1:]) or {"bm25", "embedding"}
    asyncio.run(rebuild(lexical="bm25" in wanted, semantic="embedding" in wanted))


if __name__ == "__main__":
    main()
