"""Collect Cathay Life's published contract documents.

The insurer lists every public document in its own sitemap — 711 PDFs at the time of
writing, about 1.5 GB — so there is no scraping of product pages and no guessing at
URLs. We read the list they publish, honour the exclusions they publish, and fetch at
a rate that does not load their server.

Product pages carry no link to their contract: the site renders through Adobe Edge
Delivery and the document list is not in the HTML. It does not matter. Every contract
PDF states its own full product name on page one, so the corpus describes itself.

Compliance note, because this project argues about compliance elsewhere: `robots.txt`
names three PDFs and a set of transaction endpoints as off-limits. Those are skipped.
A product about following the rules cannot source its data by breaking them.
"""

import asyncio
import re
from hashlib import blake2b
from pathlib import Path

import aiohttp
from loguru import logger
from msgspec import Struct, json

SITEMAP = "https://www.cathaylife.com.tw/cathaylife/sitemap.xml"
ROBOTS = "https://www.cathaylife.com.tw/robots.txt"

_LOC = re.compile(r"<loc>([^<]+)</loc>")
_DISALLOW = re.compile(r"^Disallow:\s*(\S+)", re.MULTILINE)

# Their TLS chain omits an intermediate, so a strict client cannot complete the
# handshake even though browsers can. Verification is off for this host only, and the
# payloads are content-hashed on arrival, which is the property we actually need.
_VERIFY_SSL = False

CONCURRENCY = 4
DELAY_SECONDS = 0.4


class Document(Struct):
    """One fetched contract document."""

    url: str
    filename: str
    sha: str
    bytes: int
    title: str = ""
    """First non-empty line of page one, which is where these contracts print their
    full product name. Filled in by the indexing pass, not the fetch."""


class Manifest(Struct):
    """What the corpus holds, so a re-run fetches only what is missing."""

    documents: dict[str, Document] = {}
    """Keyed by url."""

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        """Read a manifest, or start an empty one.

        Args:
            path: Where the manifest lives.

        Returns:
            The stored manifest, or a fresh one when the file is absent.
        """
        if not path.exists():
            return cls()
        return json.decode(path.read_bytes(), type=cls)

    def save(self, path: Path) -> None:
        """Write the manifest.

        Args:
            path: Where to write it.
        """
        path.write_bytes(json.format(json.encode(self), indent=2))


async def _text(session: aiohttp.ClientSession, url: str) -> str:
    """Fetch a URL as text.

    Args:
        session: An open client session.
        url: What to fetch.

    Returns:
        The response body.
    """
    async with session.get(url, ssl=_VERIFY_SSL) as resp:
        resp.raise_for_status()
        return await resp.text()


async def discover(session: aiohttp.ClientSession) -> list[str]:
    """List the contract PDFs the insurer publishes, minus the ones it excludes.

    Args:
        session: An open client session.

    Returns:
        PDF URLs in sitemap order.
    """
    robots = await _text(session, ROBOTS)
    blocked = tuple(p for p in _DISALLOW.findall(robots) if p.endswith(".pdf"))

    sitemap = await _text(session, SITEMAP)
    urls = [u.replace("&amp;", "&") for u in _LOC.findall(sitemap) if ".pdf" in u]
    keep = [u for u in urls if not any(b in u for b in blocked)]

    logger.info("sitemap lists {} PDFs; robots.txt excludes {}", len(urls), len(urls) - len(keep))
    return keep


async def _fetch_one(
    session: aiohttp.ClientSession,
    url: str,
    out_dir: Path,
    gate: asyncio.Semaphore,
) -> Document | None:
    """Fetch one document to disk.

    Args:
        session: An open client session.
        url: The document to fetch.
        out_dir: Where to write it.
        gate: Concurrency limiter.

    Returns:
        Its manifest entry, or None when the fetch failed. A failure is logged and
        skipped rather than raised: one dead link must not abandon 710 good ones.
    """
    async with gate:
        await asyncio.sleep(DELAY_SECONDS)
        try:
            async with session.get(url, ssl=_VERIFY_SSL) as resp:
                resp.raise_for_status()
                body = await resp.read()
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.warning("skip {}: {}", url, exc)
            return None

    sha = blake2b(body, digest_size=16).hexdigest()
    filename = f"{sha}.pdf"
    (out_dir / filename).write_bytes(body)
    return Document(url=url, filename=filename, sha=sha, bytes=len(body))


async def fetch_all(out_dir: Path, limit: int = 0) -> Manifest:
    """Fetch the corpus, skipping documents already on disk.

    Args:
        out_dir: Corpus directory. Created if absent.
        limit: Stop after this many new documents, or 0 for all of them. A small limit
            is how you rehearse the run before committing to 1.5 GB.

    Returns:
        The updated manifest.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    manifest = Manifest.load(manifest_path)

    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        urls = await discover(session)
        pending = [u for u in urls if u not in manifest.documents]
        if limit:
            pending = pending[:limit]
        logger.info("{} already held, {} to fetch", len(manifest.documents), len(pending))

        gate = asyncio.Semaphore(CONCURRENCY)
        tasks = [_fetch_one(session, u, out_dir, gate) for u in pending]
        for i, coro in enumerate(asyncio.as_completed(tasks), start=1):
            if (doc := await coro) is not None:
                manifest.documents[doc.url] = doc
            if i % 25 == 0:
                manifest.save(manifest_path)
                logger.info("{}/{} fetched", i, len(pending))

    manifest.save(manifest_path)
    logger.info("corpus holds {} documents", len(manifest.documents))
    return manifest
