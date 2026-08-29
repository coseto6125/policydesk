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

import asyncio
import re
import ssl
from datetime import date
from typing import TYPE_CHECKING, Any

import aiohttp
from msgspec import Struct

from policydesk.bootloader import logger
from policydesk.retrieval.base import QUERY_STOP

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


_NOT_A_TERM = re.compile(r"^(?:之|以|依|予|其|該|前|本|各|由|自|為|於|所|並|及|或|但|者|不|無|有)")
"""Openers that mean the match started mid-phrase.

之損害賠償 and 依超過部份 are fragments of a sentence the modal happened to sit in front
of, not things anyone does. A dictionary entry nobody will ever type is not free: it is a
decision the tokeniser makes on every query, forever.
"""

_TERM_TAIL = re.compile(r"[之的與和及或者]$")


def _is_term(candidate: str) -> bool:
    """
    Say whether a modal's object is a thing somebody does.

    Args:
        candidate: The characters after 應 / 得 / 不得 / 負.

    Returns:
        True when it reads as a complete act — 據實說明, 解除契約, 終止契約 — rather than a
        sentence fragment.

    """
    return not (_NOT_A_TERM.match(candidate) or _TERM_TAIL.search(candidate))


async def statute_terms(db: Database) -> list[str]:
    """
    Statute vocabulary for the shared tokeniser dictionary.

    Args:
        db: The database.

    Returns:
        Chapter titles, the terms the statute defines, and the acts it names.

    The general dictionary is Simplified and cuts 據實說明 into pieces that match nothing,
    so the corpus supplies its own words — the same fix the clause corpus needed, over
    vocabulary the clause corpus does not contain.

    Three sources, because a statute's vocabulary is not all in one place:

    - **Definitions.** 本法所稱X，指… / 稱X者，謂…. These are the nouns: 要保人, 被保險人,
      保險業務員.
    - **Chapter titles.** 保險利益, 複保險, 特約條款 — a whole 節 is named after the concept
      it governs, and nothing else in the text introduces the word.
    - **The acts the statute regulates**, read out of 得/應/不得 + verb-object. This is the
      half the first two miss and the half a complaint is made of: nobody types 要保人之據
      實說明義務, they type 我有據實說明. 據實說明 appears **once** in 保險法, so frequency
      cannot find it and the pattern must.

    Length is bounded at 6 because past that the match is a clause, not a term, and a
    dictionary entry nobody will ever type costs a tokeniser decision on every query.
    """
    rows = await db.fetch("SELECT chapter, verbatim FROM statute_article")
    terms: set[str] = set()
    defined = re.compile(r"(?:本法|本細則)?(?:所)?稱([\u4e00-\u9fff]{2,10})[者]?[，,]\s*(?:指|謂|係指)")
    # 應據實說明 / 得解除契約 / 不得終止契約. The modal is the anchor: it marks the next few
    # characters as the thing the law requires, permits or forbids somebody to do.
    acted = re.compile(r"(?:不得|應|得|負)([\u4e00-\u9fff]{2,6}?)(?=[，,。；、）]|$)")
    for row in rows:
        if chapter := row["chapter"]:
            # 第 一 章 總則 -> 總則
            terms.add(chapter.split()[-1] if " " in chapter else chapter)
        terms.update(defined.findall(row["verbatim"]))
        terms.update(term for term in acted.findall(row["verbatim"]) if _is_term(term))
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

# The stop list lives in `retrieval.base` as QUERY_STOP: it is a property of the questions
# customers ask rather than of either corpus, and the BM25 channel needs the same one. Two
# copies would drift, and the drift would show up as a query the fallback answers well and
# the index does not, or the reverse — the hardest kind of ranking difference to attribute.

_TOKENISER = None


def _tokenise(text: str) -> list[str]:
    """
    Cut a complaint into the words a provision might contain.

    Args:
        text: What the customer said, in their own words.

    Returns:
        Content words, two characters or longer, deduplicated in order.

    Overlapping n-grams were tried first and are wrong in a way that only shows on real
    sentences. They manufacture phrases that cross word boundaries — 已經 out of 我已經繳,
    會申 out of 金管會申訴 — and those match provisions about nothing the customer said:
    金管會申訴 reached 第149-7條, an article about a receivership transfer免依公平交易法,
    because 會申 appears inside 公平交易委員會申報. A fabricated phrase is indistinguishable
    from a real one at ranking time, and the shorter it is the more it matches.

    jieba is already in the project for the clause index, and it is loaded here with the
    statute's own vocabulary for the reason the clause corpus needed the same fix: the
    bundled dictionary is Simplified, and 據實說明義務 cut by it matches nothing.
    """
    global _TOKENISER
    if _TOKENISER is None:
        import jieba_next

        _TOKENISER = jieba_next.Tokenizer()
        for term in _STATIC_TERMS:
            _TOKENISER.add_word(term)
    words: list[str] = []
    for word in _TOKENISER.cut(text):
        if len(word) >= 2 and _CJK.fullmatch(word) and word not in QUERY_STOP and word not in words:
            words.append(word)
    return words


_STATIC_TERMS = (
    "要保人", "被保險人", "受益人", "保險人", "保險費", "保險金", "保險契約", "保險金額",
    "保險利益", "保單價值準備金", "據實說明", "解除契約", "終止契約", "復效", "停效",
    "等待期", "除外責任", "告知義務", "業務員", "評議", "申訴", "金管會", "主管機關",
)
"""Vocabulary the tokeniser must not split.

Seeded rather than read from the database because tokenising must not need a query — the
words here are the ones a complaint is actually made of. `statute_terms` supplies the
fuller list to the shared BM25 dictionary, which is where it belongs.
"""


async def search_statute(
    db: Database,
    topic: str,
    statute_ids: list[str] | None = None,
    limit: int = 5,
    *,
    siblings: bool = True,
    retriever: Any | None = None,
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
        retriever: A `policydesk.retrieval.base.Retriever` over the shared index. Given
            one, it does the ranking and the SQL below is not run; None falls back to it.

    Returns:
        Provision rows, best first.

    Paragraph-level rows only: the whole-article row is the same text concatenated, so
    including it would return every match twice and spend half the budget saying the
    same thing. A citation to a whole article still resolves through `find_articles`.

    Ranked by how many of the complaint's words a provision contains, then by their summed
    weight (length times rarity). Both orderings were arrived at by watching the other one
    fail on a real sentence:

    - Weight alone rewards a rare word that happens to appear. 公司說要解除我的契約，但我
      已經繳了五年 put 第166-1條 first — 散布流言損害保險業信用者處**五年**以下有期徒刑 —
      because 五年 is rare in the corpus, while the provisions matching both 解除 and 契約
      scored lower.
    - Count alone rewards common words. 公司 plus 解除 put 第164-1條 (the regulator
      ordering a company to dismiss an officer) above 第64條.

    Coverage first, weight as the tie-break, is what BM25 does with saturation and IDF and
    does properly. This is the fallback that has to answer until the shared index lands, on
    the same terms `find_clause` already has: a desk that ranks worse still answers, one
    that will not start does not.
    """
    if retriever is not None and (ranked := await _ranked_by(db, retriever, topic, statute_ids, limit)):
        return await with_siblings(db, ranked) if siblings else ranked

    grams = _tokenise(topic)
    if not grams:
        return []
    rows = await db.fetch(
        """WITH gram AS (SELECT DISTINCT word FROM unnest($1::text[]) AS g(word)),
                weighted AS (
                  SELECT g.word,
                         -- Rarity, the cheap stand-in for IDF. 公司 is in a tenth of the
                         -- statute and 據實說明 in two provisions; counting them equally
                         -- is what let 第164-1條 (主管機關 may order a company to dismiss
                         -- an officer) beat 第64條 on 憑什麼解除我的契約, on the strength
                         -- of 公司 plus 解除 and a shorter body.
                         ln(1 + (SELECT count(*) FROM statute_article a2
                                  WHERE a2.paragraph IS NOT NULL
                                    AND ($2::text[] IS NULL OR a2.statute_id = ANY($2::text[])))
                              / greatest(1.0, (SELECT count(*) FROM statute_article a3
                                                WHERE a3.paragraph IS NOT NULL
                                                  AND ($2::text[] IS NULL OR a3.statute_id = ANY($2::text[]))
                                                  AND a3.verbatim LIKE '%' || g.word || '%'))
                            ) * length(g.word) AS weight
                  FROM gram g)
           SELECT a.statute_id, s.name AS statute_name, a.doc_id, a.article, a.branch, a.paragraph,
                  a.subparagraph, a.chapter, a.verbatim, s.amended_at, s.source_url,
                  (SELECT count(*) FROM weighted w WHERE a.verbatim LIKE '%' || w.word || '%') AS covered,
                  (SELECT coalesce(sum(w.weight), 0) FROM weighted w
                    WHERE a.verbatim LIKE '%' || w.word || '%') AS score
           FROM statute_article a
           JOIN statute s USING (statute_id)
           WHERE a.paragraph IS NOT NULL
             AND ($2::text[] IS NULL OR a.statute_id = ANY($2::text[]))
             AND EXISTS (SELECT 1 FROM weighted w WHERE a.verbatim LIKE '%' || w.word || '%')
           ORDER BY covered DESC, score DESC, length(a.verbatim),
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


async def _ranked_by(
    db: Database, retriever: Any, topic: str, statute_ids: list[str] | None, limit: int
) -> list[dict[str, Any]]:
    """
    Rank through the shared index, then read the provisions it named.

    Args:
        db: The database.
        retriever: Anything satisfying the `Retriever` protocol.
        topic: The complaint.
        statute_ids: Statutes to restrict to, or None for all of them.
        limit: Most provisions to rank.

    Returns:
        Provision rows in the retriever's order. Empty when it found nothing, which sends
        the caller to the SQL fallback rather than to an empty reply.

    The index holds both corpora, so the corpus filter is what keeps a customer's own
    contract clauses out of an answer about the law. Whole-article rows are dropped for
    the reason they are dropped everywhere: the text is its paragraphs concatenated.
    """
    from policydesk.retrieval.base import STATUTE

    hits = await asyncio.to_thread(
        retriever.search, topic, corpus=STATUTE, scope=tuple(statute_ids or ()), limit=limit * 2
    )
    keys = [(h.scope_id, h.doc_id) for h in hits]
    if not keys:
        return []
    rows = await db.fetch(
        """SELECT a.statute_id, s.name AS statute_name, a.doc_id, a.article, a.branch, a.paragraph,
                  a.subparagraph, a.chapter, a.verbatim, s.amended_at, s.source_url
           FROM statute_article a
           JOIN statute s USING (statute_id)
           JOIN unnest($1::text[], $2::text[]) AS want(statute_id, doc_id)
             ON want.statute_id = a.statute_id AND want.doc_id = a.doc_id
           WHERE a.paragraph IS NOT NULL""",
        [[k[0] for k in keys], [k[1] for k in keys]],
    )
    rank = {key: position for position, key in enumerate(keys)}
    rows.sort(key=lambda r: rank.get((r["statute_id"], r["doc_id"]), len(rank)))
    return rows[:limit]


_LEAD = r"(?:依|依據|依照|按|按照|根據|參照|見|據)?"
"""Particles a sentence puts in front of a citation. Stripped rather than allowed into the
name, because the name is half the key the recheck looks up — 依保險法 matches no statute,
so a real citation would be reported as invented."""

_STATUTE_NAME = r"[\u4e00-\u9fff]{2,10}?(?:法|細則|辦法|準則|條例|規則)"

_INT = r"\d{1,4}|[一二三四五六七八九十百]{1,10}?"
_SMALL = r"\d{1,2}|[一二三四五六七八九十]{1,3}"

_CITED_NUMBER = (
    # The branch sits before 條 in digits (第8-1條) and after it in Chinese (第八條之一).
    # Both appear in the corpus: the statute cross-references itself in words and the
    # index writes ids in figures, and a model reading both will write either.
    rf"第\s*({_INT})\s*(?:[-之]\s*({_SMALL}))?\s*條(?:\s*之\s*({_SMALL}))?"
    rf"(?:\s*第\s*({_SMALL})\s*項)?"
    rf"(?:\s*第\s*({_SMALL})\s*款)?"
)

CITATION = re.compile(rf"[〔（(\[]?\s*{_LEAD}\s*({_STATUTE_NAME})\s*{_CITED_NUMBER}\s*[〕）)\]]?")
"""How a statute citation is read *out of* a reply: 〔保險法 第64條第2項〕 and its variants.

Written one way and read many. `citation` emits the bracketed form and the model is told
to copy it, but what the checker must catch is everything a model might write instead —
because a citation the pattern misses is not one the checker rejects, it is one it never
sees. Brackets optional, full-width or half-width or absent; the number in digits or in
Chinese numerals, since the statute writes its own cross-references as 第六十四條第三項
and a model reading the corpus will echo that.

The name must end in 法/細則/辦法/準則/條例/規則. Without that anchor the pattern reads
「合約第3條」 in a sentence about the customer's own contract as a statute citation, and
the recheck then voids a reply for a statute nobody cited.

Deliberately not the `art.64.2` shape the clause corpus uses. The executor extracts
`art.NN` from replies and voids any that no contract contains, and `art.64.2` contains
`art.64` — so a statute written that way would be read as a clause citation, found in no
policy, and the whole reply withheld. Two corpora sharing one citation syntax is a
collision the reader cannot see and the checker cannot resolve.

It is also the form a Taiwanese reader recognises, which is the other half of the point:
a customer who wants to check what he has been told can type it into 全國法規資料庫.

The branch takes two digits, not one. 保險法 runs to 第149-11條, and a single-digit
pattern read 第149-10條 as no citation at all — which is the dangerous direction of the
failure: an unreadable citation is not a citation the checker rejects, it is one the
checker never sees, so an invented 第149-10條 would have passed. Found by formatting all
1,212 provisions and reading each one back; 15 did not round-trip.

之 is accepted beside the hyphen because that is how the statute writes it in its own
cross-references (第六十四條第三項, 第一百四十九條之十), and a model copying the corpus
will sometimes copy that.
"""


_CN_DIGITS = "零一二三四五六七八九"


def _number(raw: str) -> int:
    """
    Read an article number written either way.

    Args:
        raw: `64` or `六十四`.

    Returns:
        Its value.

    Chinese numerals are here because the statute cites itself that way — 第六十四條第三
    項 appears verbatim inside 第68條 — so a model quoting the corpus will reproduce it,
    and a citation the reader cannot parse is one the checker never gets to reject.
    """
    if raw.isdigit():
        return int(raw)
    total = section = 0
    for char in raw:
        if char == "百":
            section = (section or 1) * 100
            total += section
            section = 0
        elif char == "十":
            section = (section or 1) * 10
        elif char in _CN_DIGITS:
            section += _CN_DIGITS.index(char)
    return total + section


def cited(text: str) -> list[tuple[str, str]]:
    """
    Read the statute citations out of a reply.

    Args:
        text: What the model wrote.

    Returns:
        (statute name, doc_id) pairs in order of appearance, deduplicated.

    """
    out: list[tuple[str, str]] = []
    for name, article, before, after, paragraph, item in CITATION.findall(text):
        doc_id = f"art.{_number(article)}"
        if branch := before or after:
            doc_id += f"-{_number(branch)}"
        if paragraph:
            doc_id += f".{_number(paragraph)}"
            if item:
                doc_id += f".{_number(item)}"
        pair = (name, doc_id)
        if pair not in out:
            out.append(pair)
    return out


_NAMES: set[str] | None = None


async def _statute_names(db: Database) -> set[str]:
    """
    Name every statute the corpus holds.

    Args:
        db: The database.

    Returns:
        The names, read once per process.

    Cached because the table holds three rows that change only when a migration runs, and
    `unresolved` is on the path of every customer turn — `complaint_channel` reaches it
    too, on a fixed constant whose answer cannot differ between calls. A round trip per
    turn for a value that is fixed for the process's life is the cost `gather_tools`
    parallelises tool calls to avoid.

    """
    global _NAMES
    if _NAMES is None:
        _NAMES = {row["name"] for row in await db.fetch("SELECT name FROM statute")}
    return _NAMES


async def unresolved(db: Database, text: str) -> list[tuple[str, str]]:
    """
    Name the statute citations in a reply that do not exist.

    Args:
        db: The database.
        text: The reply.

    Returns:
        The (statute name, doc_id) pairs with no matching row. Empty means every
        citation resolves.

    **The boundary is the corpus, and it is a boundary on purpose.** This desk carries three
    statutes. A reply citing a fourth — 遺產及贈與稅法第16條第9款 is the standard sentence
    for 身故保險金會不會被課遺產稅, and it is correct law — is reported here and the reply is
    withheld. That reads like a false positive and is the rule working: the desk may not
    state a provision no tool returned, and no tool returns that one. The fix for the
    customer is the injection, which names what the desk carries so the model does not
    reach for what it does not; loosening this check instead would let an invented
    provision through under any statute name nobody thought to carry.

    Four digits, not three: 民法 runs to 第1225條, so a three-digit pattern could not read
    第1138條 at all — and a citation the pattern never sees is one this function never
    rejects, which is the direction the `CITATION` docstring calls the dangerous one.

    Checked by *name as written* as well as by doc_id, because 保險法 §64 and 保險法施行
    細則 §64 are different sentences and a reply that attributes one to the other is wrong
    in the way hardest for a reader to catch.
    """
    pairs = cited(text)
    if not pairs:
        return []
    resolved = [(_resolve(name, await _statute_names(db)), doc_id) for name, doc_id in pairs]
    rows = await db.fetch(
        """SELECT s.name, a.doc_id
           FROM statute_article a JOIN statute s USING (statute_id)
           WHERE a.doc_id = ANY($1::text[])""",
        [[doc_id for _, doc_id in resolved]],
    )
    real = {(row["name"], row["doc_id"]) for row in rows}
    return [pair for pair in resolved if pair not in real]


_TRIM = re.compile(
    # Longest alternative first where two share a prefix, because Python's alternation
    # takes the first that matches: 就是 has to be tried before anything that would eat
    # its 就, and 就 itself is deliberately absent — 就業保險法 must NOT be trimmable.
    r"^(?:[^\u4e00-\u9fff]"
    r"|以及|此外|另外|其中|例如|像是|亦即|而是|就是|所謂"
    r"|另|並|且|又|還|亦|則|其|此|該|這|那|在|是|的|和|與|或|改|也|而|但|若|如"
    r"|可|應|得|須|要|由|為|於|自|之|新|舊|同"
    r"|我|你|您|他|們|本|貴|公司|台灣|臺灣|國內|現行"
    r"|法源|請|參考|參照|適用|準用|按照|按|依據|依照|依|根據|據|見)+"
)
"""Prose a sentence puts in front of a statute name, stripped one fragment at a time.

`_STATUTE_NAME` has no left boundary, so any CJK character abutting the name is captured
with it: 另依保險法第64條第2項 yields 另依保險法, which matches no row, and a correct
citation is reported as invented. Measured on 14 realistic phrasings of one real
provision, 10 were voided — 另依, 並依, 這在, 法源是, 請參考, 本公司依, 台灣的, 適用, and
both compound statute names.

Anchoring the pattern on the left does not fix it: 另 is itself preceded by a comma, so a
lookbehind still admits the match. The captured name is not the key this table joins on,
so it is resolved to one rather than used as one.
"""


def _resolve(name: str, known: set[str]) -> str:
    """
    Trim a captured statute name to the real one, or leave it to be reported.

    Args:
        name: The name as the pattern captured it, prose and all.
        known: Every statute name the corpus holds.

    Returns:
        The known name when the capture is one with a recognised lead in front of it, and
        the capture unchanged otherwise — which is a statute this corpus does not have,
        and the thing to report.

    **It strips a lead, it does not search for a suffix.** The earlier version took the
    longest known name the capture ended with, which admitted every real statute whose
    name ends in a carried one: 全民健康保險法第41條, 強制汽車責任保險法第27條 and
    就業保險法第11條 all resolved to 保險法 and passed, because those article numbers
    exist under 保險法. 保險法第41條 is about 再保險人不得向要保人請求交付保險費 — so a
    customer asking about 健保 read a citation to a statute this desk does not hold,
    attached to a provision about something else entirely. Measured on the live corpus.

    An unrecognised lead now leaves the name unresolved, so a real citation with unusual
    prose in front of it is reported as invented and the reply is repaired. That is the
    direction to fail in: a repair costs a turn, and a fabricated statute costs the
    customer a wrong answer they cannot check.

    Longest match first inside `_TRIM`'s alternation, so 依據 is one fragment rather than
    依 followed by an unmatched 據 — and 保險法施行細則 is never shortened to 保險法,
    because the trim never touches the tail.

    """
    if name in known:
        return name
    return trimmed if (trimmed := _TRIM.sub("", name)) in known else name


def citation(row: dict[str, Any]) -> str:
    """
    Write one provision's citation the way it is cited in Chinese.

    Args:
        row: A `statute_article` row joined to its statute, as `search_statute` returns.

    Returns:
        e.g. `〔保險法 第64條第2項〕`, exactly the form soothe's `CITATION` reads back.

    Handed to the model already formatted rather than described in the injection. A
    citation format explained in prose is one the model approximates; one it can copy is
    one the checker can verify.

    Public and here rather than private and in each scenario, because three scenarios
    wrote it and each explained in its own docstring that ten lines were not worth
    sharing. Three copies is the refutation: the 目 level a future amendment adds would
    have to be added three times, and two of them would drift.

    """
    number = f"第{row['article']}"
    if row.get("branch"):
        number += f"-{row['branch']}"
    number += "條"
    if row.get("paragraph"):
        number += f"第{row['paragraph']}項"
    if row.get("subparagraph"):
        number += f"第{row['subparagraph']}款"
    return f"〔{row['statute_name']} {number}〕"


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
