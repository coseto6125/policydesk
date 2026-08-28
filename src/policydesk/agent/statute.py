"""
The statute corpus: 保險法 and the law around it, at the granularity a citation uses.

The desk already answers from the contract. It cannot answer from the contract alone,
because the sentences a customer reaches for when they are upset are not in their policy:

- 「你們憑什麼解約」 is 保險法 §64 — and the answer is mostly the second and third 項,
  which say the insurer loses that right after two years, or after one month of knowing.
- 「我不記得填過那張健康告知」 is §64 I, a duty owed to a *written* question, so a
  question never asked was never answered untruthfully.
- 「我要申訴」 is 金融消費者保護法 §13, and the customer is entitled to be told where.

So the statute is a second corpus, not a second index. The retrieval session owns the
index; this module owns getting the law into the database in the shape that index reads,
and reading it back for a tool.

## Granularity, and why the whole article is stored too

Citations are written three ways — §64, §64 II, §65 款一 — so all three are rows.
`paragraph IS NULL` is the whole article, stored alongside its paragraphs rather than
instead of them. A retriever that could only return whole articles would answer
「解約權有沒有時效」 with all three 項 of §64 and leave the reader to find the one
sentence that answers it. One that could only return paragraphs would have nothing to
return for a citation written as §64.

## Verbatim means verbatim

Nothing here rewrites the text. The parser takes the government's own line divisions,
strips the markup, and stores what is between the tags. A statute the desk paraphrases
is a statute the customer cannot check, and being checkable is the entire reason to
quote law at a person who is already angry.
"""

import re
import ssl
from datetime import date
from typing import TYPE_CHECKING, Any

import aiohttp
from msgspec import Struct

from policydesk.bootloader import logger

if TYPE_CHECKING:
    from collections.abc import Iterable

    from policydesk.core.db import Database

SOURCE = "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode={pcode}"
"""全國法規資料庫, the Ministry of Justice's own publication of the consolidated text."""

STATUTES: dict[str, str] = {
    "insurance_act": "G0390002",
    "insurance_act_rules": "G0390003",
    "financial_consumer_protection_act": "G0380226",
}
"""The statutes the desk may cite, and their 法規資料庫 codes.

Deliberately short. Every statute added is vocabulary added to a shared dictionary and
rows added to a shared index, and a desk that can cite 銀行法 at an insurance customer
has gained no answer and lost precision. Add one when a scenario needs it.
"""

_ROW = re.compile(r'<div class="row">(.*?)</div>\s*</div></div>', re.DOTALL)
_NUMBER = re.compile(r'name="([\d\-]+)"')
_LINE = re.compile(r'<div class="(line-\d+)[^"]*">(.*?)</div>', re.DOTALL)
_CHAPTER = re.compile(r'<div class="h3 char-\d+">(.*?)</div>', re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_NAME = re.compile(r'id="hlLawName"[^>]*>(.*?)</a>', re.DOTALL)
_AMENDED = re.compile(r"(?:修正|公布)日期：</th>\s*<td>\s*民國\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日", re.DOTALL)
_CATEGORY = re.compile(r"法規類別：</th>\s*<td>(.*?)</td>", re.DOTALL)

_PARAGRAPH_LEVEL = "line-0000"
"""項 level. 款 are line-0004 and 目 line-0006, each indented under the line above."""

_ITEM_MARK = re.compile(r"^([一二三四五六七八九十]+)、")
"""款 are numbered 一、二、三、 in the text itself, so the number is read rather than counted.

Counting would silently renumber a statute that skips or reserves an item, and the
renumbered citation would point at a real 款 that says something else.
"""

_CN = "零一二三四五六七八九"


def _cn_to_int(text: str) -> int:
    """
    Read a Chinese numeral up to 99.

    Args:
        text: 一 / 十 / 十七 / 二十三.

    Returns:
        Its value, or 0 when the string is not a numeral.

    """
    if not text:
        return 0
    if "十" not in text:
        return _CN.index(text[0]) if text[0] in _CN else 0
    tens, _, units = text.partition("十")
    high = _CN.index(tens[0]) if tens and tens[0] in _CN else 1
    low = _CN.index(units[0]) if units and units[0] in _CN else 0
    return high * 10 + low


def _plain(fragment: str) -> str:
    """
    Strip markup and entities from one line of statute text.

    Args:
        fragment: The inner HTML of a line div.

    Returns:
        The text, with the government's own line breaks preserved as a single space and
        the surrounding whitespace removed.

    """
    text = _TAG.sub("", fragment)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        text = text.replace(entity, char)
    return " ".join(text.split())


class Article(Struct, frozen=True):
    """One citable unit of a statute."""

    doc_id: str
    """art.64 / art.64.1 / art.64.1.2 / art.8-1 — the citation as it is written."""
    article: int
    branch: int
    """之一 / 之二, zero for a plain article."""
    paragraph: int | None
    subparagraph: int | None
    chapter: str
    heading: str
    verbatim: str


def _doc_id(article: int, branch: int, paragraph: int | None, subparagraph: int | None) -> str:
    """
    Build the citation string for one unit.

    Args:
        article: 條.
        branch: 之N, zero when absent.
        paragraph: 項, None for the whole article.
        subparagraph: 款, None when the unit is not one.

    Returns:
        The doc_id, e.g. `art.8-1.2.3`.

    """
    parts = [f"art.{article}-{branch}" if branch else f"art.{article}"]
    if paragraph is not None:
        parts.append(str(paragraph))
    if subparagraph is not None:
        parts.append(str(subparagraph))
    return ".".join(parts)


def parse(html: str) -> tuple[dict[str, Any], list[Article]]:
    """
    Parse a 全國法規資料庫 "所有條文" page into its citable units.

    Args:
        html: The page source.

    Returns:
        The statute's metadata, and every article, paragraph and subparagraph in
        document order.

    The chapter headings are interleaved with the articles in the source, so they are
    tracked by position rather than looked up: an article carries whichever 章 was last
    printed above it. That is also how a reader of the printed statute knows.

    """
    meta: dict[str, Any] = {
        "name": _plain(m.group(1)) if (m := _NAME.search(html)) else "",
        "authority": "",
        "amended_at": None,
    }
    if m := _AMENDED.search(html):
        # 民國 year. 114 is 2025; the desk reports a Gregorian date because that is what
        # the rest of the system compares against.
        meta["amended_at"] = date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
    if m := _CATEGORY.search(html):
        parts = [p.strip() for p in _plain(m.group(1)).split("＞")]
        meta["authority"] = parts[1] if len(parts) > 1 else parts[0]

    articles: list[Article] = []
    chapter = ""
    # One pass over chapters and rows together, in source order, so a chapter heading
    # applies to the articles printed after it and not to the ones before.
    for token in re.finditer(r'<div class="h3 char-\d+">(.*?)</div>|<div class="row">(.*?)</div>\s*</div></div>', html, re.DOTALL):
        if token.group(1) is not None:
            chapter = _plain(token.group(1))
            continue
        articles.extend(_articles_in(token.group(2), chapter))
    return meta, articles


def _articles_in(row: str, chapter: str) -> list[Article]:
    """
    Turn one 條 of the source into its whole-article, 項 and 款 rows.

    Args:
        row: The row div's inner HTML.
        chapter: The 章 currently in force.

    Returns:
        The article row first, then its paragraphs and their subparagraphs.

    A 款 attaches to the 項 printed above it, which is the indentation the source
    encodes as `line-0004` under `line-0000`. Attaching it to the article instead would
    make §65 款一 unfindable from a citation, since §65 has one 項 and three 款 that all
    qualify it.
    """
    number = _NUMBER.search(row)
    if not number:
        return []
    head, _, branch_text = number.group(1).partition("-")
    if not head.isdigit():
        return []
    article, branch = int(head), int(branch_text or 0)

    lines = [(cls, _plain(body)) for cls, body in _LINE.findall(row)]
    lines = [(cls, text) for cls, text in lines if text]
    if not lines:
        return []

    out: list[Article] = []
    whole = " ".join(text for _, text in lines)
    out.append(Article(_doc_id(article, branch, None, None), article, branch, None, None, chapter, "", whole))

    paragraph = 0
    for cls, text in lines:
        if cls == _PARAGRAPH_LEVEL:
            paragraph += 1
            out.append(
                Article(_doc_id(article, branch, paragraph, None), article, branch, paragraph, None, chapter, "", text)
            )
            continue
        if paragraph == 0:
            # A 款 before any 項 would be malformed source; hold it under 項 1 rather
            # than dropping the text on the floor.
            paragraph = 1
        if mark := _ITEM_MARK.match(text):
            item = _cn_to_int(mark.group(1))
            out.append(
                Article(
                    _doc_id(article, branch, paragraph, item), article, branch, paragraph, item, chapter, "", text
                )
            )
    return out


def _moj_ssl() -> ssl.SSLContext:
    """
    Build a TLS context that accepts 全國法規資料庫's certificate chain.

    Returns:
        A verifying context with X509_STRICT relaxed.

    The government's intermediate omits the Subject Key Identifier extension, which
    OpenSSL 3.x rejects under its strict profile — the chain is otherwise valid and
    verifies against the system roots. Only the strict-profile check is dropped;
    hostname checking and chain verification both stay on, because a statute fetched
    from whoever answered the connection is worse than no statute.
    """
    context = ssl.create_default_context()
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


async def fetch(pcode: str, *, timeout_s: int = 30) -> str:
    """
    Download one statute's consolidated text.

    Args:
        pcode: The 法規資料庫 code, e.g. G0390002.
        timeout_s: How long to wait.

    Returns:
        The page source.

    """
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    connector = aiohttp.TCPConnector(ssl=_moj_ssl())
    async with (
        aiohttp.ClientSession(timeout=timeout, connector=connector) as session,
        session.get(SOURCE.format(pcode=pcode)) as response,
    ):
        response.raise_for_status()
        # The page is Big5-era ASP.NET and declares utf-8 in a meta tag rather than in
        # the header, so aiohttp's own charset guess is not trusted.
        return await response.text(encoding="utf-8")


async def store(db: Database, statute_id: str, pcode: str, meta: dict[str, Any], articles: Iterable[Article]) -> int:
    """
    Write one statute and its articles, replacing whatever was there.

    Args:
        db: The database.
        statute_id: The scope_id the retriever filters on.
        pcode: The source code, used to rebuild the URL.
        meta: Name, authority and amendment date from `parse`.
        articles: The units to write.

    Returns:
        How many rows landed.

    A statute is replaced wholesale rather than merged. An amendment renumbers, merges
    and deletes provisions, so an upsert would leave the repealed text in the corpus,
    still citable, indistinguishable from law that is still in force.

    """
    rows = list(articles)
    await db.execute(
        """INSERT INTO statute (statute_id, name, authority, amended_at, source_url, fetched_at)
           VALUES ($1, $2, $3, $4, $5, now())
           ON CONFLICT (statute_id) DO UPDATE
             SET name = EXCLUDED.name, authority = EXCLUDED.authority,
                 amended_at = EXCLUDED.amended_at, source_url = EXCLUDED.source_url,
                 fetched_at = now()""",
        [statute_id, meta["name"], meta["authority"], meta["amended_at"], SOURCE.format(pcode=pcode)],
    )
    await db.execute("DELETE FROM statute_article WHERE statute_id = $1", [statute_id])
    await db.execute_many(
        """INSERT INTO statute_article
             (statute_id, doc_id, article, branch, paragraph, subparagraph, chapter, heading, verbatim)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
        [
            [statute_id, a.doc_id, a.article, a.branch, a.paragraph, a.subparagraph, a.chapter, a.heading, a.verbatim]
            for a in rows
        ],
    )
    logger.info("statute_stored", statute_id=statute_id, rows=len(rows), amended_at=str(meta["amended_at"]))
    return len(rows)


async def ingest(db: Database, statute_ids: Iterable[str] = ()) -> dict[str, int]:
    """
    Fetch, parse and store the statutes the desk may cite.

    Args:
        db: The database.
        statute_ids: Which to load. Empty means all of `STATUTES`.

    Returns:
        Rows written per statute.

    """
    wanted = list(statute_ids) or list(STATUTES)
    written: dict[str, int] = {}
    for statute_id in wanted:
        pcode = STATUTES[statute_id]
        meta, articles = parse(await fetch(pcode))
        written[statute_id] = await store(db, statute_id, pcode, meta, articles)
    return written


async def statute_terms(db: Database) -> list[str]:
    """
    Statute vocabulary for the shared tokeniser dictionary.

    Args:
        db: The database.

    Returns:
        Chapter titles and the defined terms the statute introduces.

    The general dictionary is Simplified and cuts 據實說明義務 into pieces that match
    nothing, so the corpus supplies its own words — the same fix the clause corpus needed,
    over vocabulary the clause corpus does not contain.

    A statute defines its terms in a fixed sentence: 本法所稱X，指… / 稱X者，謂…. Those
    are exactly the words the customer will type, so they are read out of the text rather
    than listed by hand.
    """
    rows = await db.fetch("SELECT chapter, verbatim FROM statute_article WHERE paragraph IS NULL")
    terms: set[str] = set()
    defined = re.compile(r"(?:本法|本細則)?(?:所)?稱([\u4e00-\u9fff]{2,10})[者]?[，,]\s*(?:指|謂|係指)")
    for row in rows:
        if chapter := row["chapter"]:
            # 第 一 章 總則 -> 總則
            terms.add(chapter.split()[-1] if " " in chapter else chapter)
        terms.update(defined.findall(row["verbatim"]))
    return sorted(t for t in terms if len(t) >= 2)


async def find_articles(
    db: Database, doc_ids: list[str], statute_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    """
    Read statute text by citation, in the order asked for.

    Args:
        db: The database.
        doc_ids: Citations, e.g. `["art.64.2", "art.64.3"]`.
        statute_ids: Restrict to these statutes. None searches all of them.

    Returns:
        Rows carrying the verbatim text and the statute's name, in the order of
        `doc_ids`.

    """
    if not doc_ids:
        return []
    rows = await db.fetch(
        """SELECT a.statute_id, s.name AS statute_name, a.doc_id, a.article, a.branch, a.paragraph,
                  a.subparagraph, a.chapter, a.verbatim, s.amended_at, s.source_url
           FROM statute_article a
           JOIN statute s USING (statute_id)
           WHERE a.doc_id = ANY($1::text[])
             AND ($2::text[] IS NULL OR a.statute_id = ANY($2::text[]))""",
        [doc_ids, statute_ids],
    )
    rank = {doc_id: position for position, doc_id in enumerate(doc_ids)}
    rows.sort(key=lambda r: rank.get(r["doc_id"], len(rank)))
    return rows


_CJK = re.compile(r"[\u4e00-\u9fff]{2,}")

_STOP = frozenset({"你們", "我們", "為什麼", "憑什麼", "怎麼", "這樣", "可以", "不可以", "應該", "什麼", "這個", "那個"})
"""Runs a complaint is made of that no provision is about.

Not a general stop list — 保險, 契約 and 解除 are all common and all load-bearing here.
These are the words of complaining rather than the words of insurance.
"""


def _keywords(topic: str) -> list[str]:
    """
    Break a complaint into the phrases a provision might contain.

    Args:
        topic: What the customer said, in their own words.

    Returns:
        Candidate 2- to 4-character phrases, longest first.

    A complaint arrives as a sentence — 你們憑什麼解除我的契約 — and no provision contains
    that string, so a substring search over the whole thing returns nothing while the
    provision that answers it sits in the table. Cutting into overlapping n-grams finds
    解除 and 契約 without a tokeniser, at the cost of nonsense phrases that simply match
    nothing.

    This is the fallback. The shared BM25 index does this properly, with the corpus
    dictionary; what matters here is that the fallback answers rather than silently
    returning an empty list, which reads to the customer as the law having nothing to say.
    """
    grams: list[str] = []
    for run in _CJK.findall(topic):
        for size in (4, 3, 2):
            for start in range(len(run) - size + 1):
                gram = run[start : start + size]
                if gram not in _STOP and gram not in grams:
                    grams.append(gram)
    return grams


# A stance boost was tried here and removed, which is worth recording because it looks
# obviously right. 保險法 is two statutes wearing one number: the contract law a customer
# lives under, and the supervisory law telling the regulator how to license and fine an
# insurer. Scoring provisions dense in 要保人 / 保險契約 above ones dense in 主管機關 /
# 罰鍰 therefore seems free — and it answered 你們憑什麼解除我的契約 with 第123條 保險人
# 破產 and 第138-2條 保險金信託, which are thick with those words and say nothing about
# rescission. Vocabulary is not stance, and a proxy that outranks relevance replaces a
# wrong answer with a differently wrong one. The real fix is the shared BM25 index, where
# IDF already discounts 保險契約 for being everywhere.


async def search_statute(
    db: Database, topic: str, statute_ids: list[str] | None = None, limit: int = 5, *, siblings: bool = True
) -> list[dict[str, Any]]:
    """
    Find statute provisions bearing on a topic.

    Args:
        db: The database.
        topic: What the customer raised, in their own words.
        statute_ids: Restrict to these statutes. None searches all.
        limit: Most provisions to *rank*. Siblings are added on top, so the list returned
            is longer than this.
        siblings: Whether to bring each hit's neighbouring 項 with it. See `with_siblings`
            for why the default is yes.

    Returns:
        Provision rows, best first.

    Paragraph-level rows only: the whole-article row is the same text concatenated, so
    including it would return every match twice and spend half the budget saying the
    same thing. A citation to a whole article still resolves through `find_articles`.

    Ranked by how many of the complaint's phrases a provision contains, longest phrase
    first, shorter provision as the tie-break — a 30-word paragraph that mentions 契約 is
    a worse answer than a 12-word one that does. This is the fallback ranking. The shared
    BM25 index takes over when the retrieval refactor lands, which is the same arrangement
    `find_clause` already has: a desk that ranks worse still answers, one that will not
    start does not.
    """
    grams = _keywords(topic)
    if not grams:
        return []
    rows = await db.fetch(
        """SELECT a.statute_id, s.name AS statute_name, a.doc_id, a.article, a.branch, a.paragraph,
                  a.subparagraph, a.chapter, a.verbatim, s.amended_at, s.source_url,
                  (SELECT count(*) FROM unnest($1::text[]) AS g(word) WHERE a.verbatim LIKE '%' || g.word || '%') AS hits,
                  (SELECT max(length(g.word)) FROM unnest($1::text[]) AS g(word)
                    WHERE a.verbatim LIKE '%' || g.word || '%') AS longest
           FROM statute_article a
           JOIN statute s USING (statute_id)
           WHERE a.paragraph IS NOT NULL
             AND ($2::text[] IS NULL OR a.statute_id = ANY($2::text[]))
             AND EXISTS (SELECT 1 FROM unnest($1::text[]) AS g(word) WHERE a.verbatim LIKE '%' || g.word || '%')
           ORDER BY longest DESC, hits DESC, length(a.verbatim),
                    a.statute_id, a.article, a.branch, a.paragraph, a.subparagraph
           LIMIT $3::int""",
        [grams, statute_ids, limit],
    )
    return await with_siblings(db, rows) if siblings else rows


SIBLING_WINDOW = 2
"""項 either side of a hit to bring with it.

Whole articles would be better and are unaffordable: 保險法 §136 has eight 項, seven of
them about company structure, and a hit on one of those drags the other seven into a reply
a customer is reading while angry. Two either side reaches 第64條第3項 from 第64條第2項,
which is the case this exists for.
"""


async def with_siblings(db: Database, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Add the neighbouring 項 of every article a hit came from, next to that hit.

    Args:
        db: The database.
        rows: The ranked hits.

    Returns:
        The same hits with their siblings interleaved: each article's paragraphs together,
        in statutory order, the articles still in rank order.

    A 項 retrieved alone is the commonest way to quote law dishonestly, and it does not
    take intent — ranking does it by itself. 保險法 §64 II is the insurer's right to
    rescind and §64 III is the two-year limit that takes it away; a search for 解除契約
    scores II higher because II is what the words are about, so the paragraph that
    protects the customer loses to the paragraph that threatens him, every time.

    Whole-article rows stay out: they are the same text concatenated, so including one
    beside its own paragraphs is the passage twice.
    """
    if not rows:
        return []
    keys = list(dict.fromkeys((r["statute_id"], r["article"], r["branch"]) for r in rows))
    family = await db.fetch(
        """SELECT a.statute_id, s.name AS statute_name, a.doc_id, a.article, a.branch, a.paragraph,
                  a.subparagraph, a.chapter, a.verbatim, s.amended_at, s.source_url
           FROM statute_article a
           JOIN statute s USING (statute_id)
           JOIN unnest($1::text[], $2::int[], $3::int[]) AS want(statute_id, article, branch)
             ON want.statute_id = a.statute_id AND want.article = a.article AND want.branch = a.branch
           WHERE a.paragraph IS NOT NULL AND a.subparagraph IS NULL""",
        [[k[0] for k in keys], [k[1] for k in keys], [k[2] for k in keys]],
    )
    by_article: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    for row in family:
        by_article.setdefault((row["statute_id"], row["article"], row["branch"]), []).append(row)
    for group in by_article.values():
        group.sort(key=lambda r: r["paragraph"])

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for hit in rows:
        key = (hit["statute_id"], hit["article"], hit["branch"])
        near = [
            row
            for row in by_article.get(key, [])
            if abs(row["paragraph"] - hit["paragraph"]) <= SIBLING_WINDOW
        ]
        # The hit itself may be a 款, which has no sibling row of its own in `family`.
        for row in [*near, hit]:
            if (row["statute_id"], row["doc_id"]) not in seen:
                seen.add((row["statute_id"], row["doc_id"]))
                out.append(row)
    return out
