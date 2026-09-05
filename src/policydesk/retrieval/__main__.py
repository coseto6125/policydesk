"""
Build both retrieval indexes from Postgres.

Run as `policydesk-index`. Both are caches: delete `data/bm25` or `data/vectors` and this
rebuilds them from the tables, so nothing here is a source of truth.

Kept out of server startup on purpose. The BM25 build is two minutes and the app does it
on demand; the vector build is forty and would hold the port shut for all of them.
"""

import argparse
import asyncio
import shutil
import time
from pathlib import Path

from msgspec import json

from policydesk.bootloader import logger
from policydesk.core.db import Database
from policydesk.retrieval import index, vectors


async def rebuild(
    *, lexical: bool = True, semantic: bool = True, path: Path = vectors.VECTOR_DIR,
    server_url: str | None = None, tokens: int = vectors.MAX_TOKENS, overlap: int = vectors.OVERLAP,
) -> None:
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
        count = await vectors.build(db, path=path, server_url=server_url, tokens=tokens, overlap=overlap)
        logger.info(
            "index_rebuilt", channel="embedding", documents=count, seconds=round(time.perf_counter() - started, 1)
        )
    await db.close()


def main() -> None:
    """Rebuild both channels, or the one named on the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channels", nargs="*", choices=("bm25", "embedding"))
    parser.add_argument("--path", type=Path, default=vectors.VECTOR_DIR)
    parser.add_argument("--server-url", default=None)
    parser.add_argument("--tokens", type=int, default=vectors.MAX_TOKENS)
    parser.add_argument("--overlap", type=int, default=vectors.OVERLAP)
    parser.add_argument("--audit", action="store_true")
    args = parser.parse_args()
    if args.audit:
        async def check() -> bool:
            db = Database()
            try:
                result = await vectors.audit(db, path=args.path)
                print(json.encode(result).decode())
                return result["complete"]
            finally:
                await db.close()
        raise SystemExit(0 if asyncio.run(check()) else 1)
    wanted = set(args.channels) or {"bm25", "embedding"}
    asyncio.run(rebuild(lexical="bm25" in wanted, semantic="embedding" in wanted, path=args.path,
                        server_url=args.server_url, tokens=args.tokens, overlap=args.overlap))


if __name__ == "__main__":
    main()
