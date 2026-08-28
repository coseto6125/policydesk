"""
Lexical retrieval over the clause corpus: tantivy BM25, CJK-aware.

Ported from enoract's `shared/retrieval` + `chat/retrieval/bm25.py`, cut down to one
corpus and one tenant. What is kept is the part that makes BM25 work on Chinese at all:

**Two analyzers, not one.** Tantivy's built-in tokenizers split on whitespace, which for
一次給付型重大傷病保險金 yields a single token nobody will ever match. `cjk_jieba` cuts
the text with jieba before tantivy sees it, joined on U+2063, so tantivy's regex splits
on the sentinel and gets word-level tokens. `cjk_ngram` emits 1-2 grams as a fallback
for the vocabulary jieba does not know — 附加條款, 豁免保費, product names — where a
partial match is better than none.

**Both sides cut the same way, with the same dictionary.** jieba ships a Simplified
Chinese dictionary and cuts 住院日額保險金給付 into 住院日 / 額保險 / 金給付 — three
tokens, none of them a word, none of them anything a customer will type. So the corpus
supplies its own vocabulary: clause headings, benefit names and the 17,866 procedure
names out of 附表1 are exactly the terms a customer asks about, and they are already in
the database. The terms are written beside the index at build time and reloaded at open,
rather than re-derived — a query cut with a different dictionary than the documents
misses, and it misses silently, returning something every time and the right thing
never.

Analyzers are not persisted in the index directory, so `_register` runs after every
open. That is tantivy's contract, not a choice made here.

Why lexical and not embeddings: a clause search is mostly an exact-terminology search.
住院日額, 等待期, 除外責任 are the words the contract itself uses, and BM25 on the right
tokenization beats a similarity score at finding the clause that contains them. The
embedding channel is the thing to add next, alongside rather than instead.
"""

import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jieba
import tantivy

from policydesk.bootloader import logger

if TYPE_CHECKING:
    from policydesk.core.db import Database

INDEX_DIR = Path("data/bm25")
"""Where the index lives. Rebuilt from Postgres, so it is a cache and never the truth."""

ANALYZER_JIEBA = "cjk_jieba"
ANALYZER_NGRAM = "cjk_ngram"

F_KEY = "key"
F_PRODUCT = "product_id"
F_CLAUSE = "clause_id"
F_KIND = "kind"
F_HEADING = "heading"
F_BODY = "body"
F_NGRAM = "ngram"

BOOST_HEADING = 3.0
"""A clause's heading names what it does. A hit there is worth three in the body."""
BOOST_BODY = 1.0
BOOST_NGRAM = 0.4
"""Fallback channel. Weighted low because a 1-gram matches almost everything."""

_SEP = "⁣"
"""U+2063 INVISIBLE SEPARATOR. One codepoint, absent from real text, and jieba never
emits it — so `[^⁣]+` partitions on it with nothing else to filter."""


TERMS_FILE = "terms.txt"
"""Written beside the index. Both sides must cut with the same dictionary, so the
dictionary travels with the index rather than being rebuilt from a query-time guess."""

_MIN_TERM = 2
_MAX_TERM = 12


async def collect_terms(db: Database) -> list[str]:
    """
    Read the corpus's own vocabulary.

    Args:
        db: The database.

    Returns:
        Deduplicated terms, longest first so jieba prefers the specific one.

    Clause headings name what a clause does, benefit names name what is paid, and the
    procedure list names what a surgeon did. Those three are the words a customer's
    question is made of, and they are the words jieba's general dictionary does not have.

    """
    rows = await db.fetch(
        """SELECT DISTINCT heading AS term FROM clause WHERE heading <> ''
           UNION SELECT DISTINCT name FROM benefit
           UNION SELECT DISTINCT procedure FROM surgery_multiplier"""
    )
    terms: set[str] = set()
    for row in rows:
        for piece in _split_term(row["term"]):
            if _MIN_TERM <= len(piece) <= _MAX_TERM:
                terms.add(piece)
    return sorted(terms, key=len, reverse=True)


def _split_term(raw: str) -> list[str]:
    """
    Break one heading into the words worth adding.

    Args:
        raw: A clause heading or benefit name.

    Returns:
        The pieces, with the connectives and the bracketed qualifiers removed.

    保險金給付之限制 is one term; 之 is not. Splitting on the connectives keeps the
    domain nouns and drops the grammar, which is what jieba already knows.

    """
    cleaned = raw
    for noise in "（）()［］[]「」、，。；：/／ ":
        cleaned = cleaned.replace(noise, "\x00")
    for connective in ("的", "之", "及其", "及", "與", "或", "暨"):
        cleaned = cleaned.replace(connective, "\x00")
    return [piece for piece in cleaned.split("\x00") if piece]


def load_terms(path: Path = INDEX_DIR) -> int:
    """
    Teach jieba the vocabulary this index was built with.

    Args:
        path: Where the index and its term list live.

    Returns:
        How many terms were loaded, zero when the file is absent.

    """
    file = path / TERMS_FILE
    if not file.is_file():
        return 0
    terms = [line.strip() for line in file.read_text(encoding="utf-8").splitlines() if line.strip()]
    for term in terms:
        jieba.add_word(term)
    return len(terms)


def cut(text: str) -> str:
    """
    Cut text the way both the writer and the reader must cut it.

    Args:
        text: Raw Chinese text.

    Returns:
        Tokens joined on the sentinel, empty when nothing survives.

    """
    return _SEP.join(token for token in jieba.cut(text, cut_all=False, HMM=True) if token.strip())


def schema() -> tantivy.Schema:
    """
    Build the index schema.

    Returns:
        The schema the builder writes and the reader opens. One definition, because a
        reader whose schema differs from the writer's opens an index it cannot query.

    """
    sb = tantivy.SchemaBuilder()
    sb.add_text_field(F_KEY, stored=True, tokenizer_name="raw")
    sb.add_text_field(F_PRODUCT, stored=True, tokenizer_name="raw")
    sb.add_text_field(F_CLAUSE, stored=True, tokenizer_name="raw")
    sb.add_text_field(F_KIND, stored=True, tokenizer_name="raw")
    sb.add_text_field(F_HEADING, stored=False, tokenizer_name=ANALYZER_JIEBA)
    sb.add_text_field(F_BODY, stored=False, tokenizer_name=ANALYZER_JIEBA)
    sb.add_text_field(F_NGRAM, stored=False, tokenizer_name=ANALYZER_NGRAM)
    return sb.build()


SCHEMA: tantivy.Schema = schema()


def _register(index: tantivy.Index) -> None:
    """
    Attach the two CJK analyzers to a freshly opened index.

    Args:
        index: The index to register on.

    Tantivy does not persist analyzer registrations, so this runs after every open and
    not once at build time.

    """
    index.register_tokenizer(
        ANALYZER_JIEBA,
        tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.regex(rf"[^{_SEP}]+")).filter(tantivy.Filter.lowercase()).build(),
    )
    index.register_tokenizer(
        ANALYZER_NGRAM,
        tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.ngram(min_gram=1, max_gram=2, prefix_only=False))
        .filter(tantivy.Filter.lowercase())
        .build(),
    )


async def build(db: Database, *, path: Path = INDEX_DIR, batch: int = 2000) -> int:
    """
    Rebuild the index from the clause table.

    Args:
        db: The database, which is the only source of truth here.
        path: Where to write it.
        batch: Rows per commit.

    Returns:
        How many clauses were indexed.

    Rebuilt rather than updated. The corpus changes when the corpus is re-ingested,
    which is a batch event; an incremental writer would be machinery guarding against a
    thing that does not happen.

    """
    # Blocking filesystem work in an async function, deliberately: this runs once at
    # startup before the first request, and a thread hop to create one directory buys
    # nothing.
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
    await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)

    # The dictionary before the first document: every heading is cut with it, and the
    # query side reloads exactly this file.
    terms = await collect_terms(db)
    await asyncio.to_thread((path / TERMS_FILE).write_text, "\n".join(terms), encoding="utf-8")
    load_terms(path)

    index = tantivy.Index(SCHEMA, path=str(path), reuse=False)
    _register(index)
    writer = index.writer()

    total = 0
    offset = 0
    while True:
        rows = await db.fetch(
            """SELECT product_id, clause_id, kind, heading, verbatim FROM clause
               ORDER BY product_id, clause_id LIMIT $1::int OFFSET $2::int""",
            [batch, offset],
        )
        if not rows:
            break
        for row in rows:
            heading = row["heading"] or ""
            body = row["verbatim"] or ""
            writer.add_document(
                tantivy.Document(
                    **{
                        F_KEY: f"{row['product_id']}|{row['clause_id']}",
                        F_PRODUCT: row["product_id"],
                        F_CLAUSE: row["clause_id"],
                        F_KIND: row["kind"],
                        F_HEADING: cut(heading),
                        F_BODY: cut(f"{heading} {body}"),
                        # The ngram analyser re-tokenises raw text, so it gets the text
                        # uncut. Feeding it the sentinel-joined form would ngram across
                        # the separator and produce tokens nothing matches.
                        F_NGRAM: f"{heading} {body}",
                    }
                )
            )
        writer.commit()
        total += len(rows)
        offset += batch

    writer.wait_merging_threads()
    logger.info("bm25_built", clauses=total, terms=len(terms), path=str(path))
    return total


class ClauseIndex:
    """
    An opened index, held for the life of the process.

    Opening costs a directory walk and an analyzer registration; a searcher costs a
    segment reload. Neither belongs on the path a customer is waiting on.
    """

    def __init__(self, path: Path = INDEX_DIR) -> None:
        self.terms = load_terms(path)
        self._index = tantivy.Index(SCHEMA, path=str(path), reuse=True)
        _register(self._index)
        self._index.reload()
        self._searcher = self._index.searcher()

    @property
    def size(self) -> int:
        """
        Give how many clauses are indexed.

        Returns:
            The document count.

        """
        return self._searcher.num_docs

    def search(self, query: str, product_ids: list[str], limit: int = 6) -> list[dict[str, Any]]:
        """
        Find the clauses of these contracts that bear on a query.

        Args:
            query: What the customer asked about, uncut.
            product_ids: The contracts the search is confined to.
            limit: Most hits to return.

        Returns:
            Hits as product_id, clause_id, kind and score, best first. Empty for an
            empty query or no contracts.

        The boolean is assembled here rather than handed to `parse_query`. Given a
        sentinel-joined string the parser sees two tokens at adjacent positions and
        builds a **phrase** query — so 住院日額 only matched a clause containing those
        two words side by side, and 不賠的情況 matched nothing at all. Term queries
        under Should give the OR that BM25 scoring assumes; the ranking does the rest.

        The product filter is a Must over an Or of term queries rather than a filter
        applied afterwards: scoring the whole corpus and then discarding everything the
        customer does not own wastes the limit on clauses they will never be shown.

        """
        if not query.strip() or not product_ids:
            return []

        scoped = tantivy.Query.boolean_query(
            [(tantivy.Occur.Should, tantivy.Query.term_query(SCHEMA, F_PRODUCT, pid)) for pid in product_ids]
        )

        text: list[tuple[Any, Any]] = []
        for token in cut(query).split(_SEP):
            if not token:
                continue
            for field, boost in ((F_HEADING, BOOST_HEADING), (F_BODY, BOOST_BODY)):
                term = tantivy.Query.term_query(SCHEMA, field, token.lower())
                text.append((tantivy.Occur.Should, tantivy.Query.boost_query(term, boost)))
        for gram in _grams(query):
            term = tantivy.Query.term_query(SCHEMA, F_NGRAM, gram)
            text.append((tantivy.Occur.Should, tantivy.Query.boost_query(term, BOOST_NGRAM)))
        if not text:
            return []

        combined = tantivy.Query.boolean_query(
            [
                (tantivy.Occur.Must, scoped),
                (tantivy.Occur.Must, tantivy.Query.boolean_query(text)),
            ]
        )
        hits = self._searcher.search(combined, limit=limit).hits
        return [
            {
                "product_id": (doc := self._searcher.doc(addr))[F_PRODUCT][0],
                "clause_id": doc[F_CLAUSE][0],
                "kind": doc[F_KIND][0],
                "score": float(score),
            }
            for score, addr in hits
        ]


def _grams(text: str, minimum: int = 2, maximum: int = 2) -> list[str]:
    """
    Cut a query into the grams the ngram field holds.

    Args:
        text: The raw query.
        minimum: Shortest gram.
        maximum: Longest gram.

    Returns:
        Deduplicated grams, in order.

    Bigrams only, though the field indexes 1-2. A single CJK character matches most of
    the corpus and adds noise proportional to its own frequency; the fallback earns its
    place on the pairs jieba failed to cut, and those are two characters or longer.

    """
    clean = "".join(ch for ch in text if not ch.isspace())
    grams = [clean[i : i + n] for n in range(minimum, maximum + 1) for i in range(len(clean) - n + 1)]
    return list(dict.fromkeys(g.lower() for g in grams))


async def open_index(db: Database, *, path: Path = INDEX_DIR) -> ClauseIndex | None:
    """
    Open the index, building it first when it is not there.

    Args:
        db: The database, for a build.
        path: Where the index lives.

    Returns:
        The opened index, or None when it could not be opened. None is a degradation the
        caller handles by falling back to the SQL search — a desk whose ranking is worse
        still answers, and one that fails to start does not.

    """
    try:
        if not await asyncio.to_thread((path / "meta.json").is_file):
            await build(db, path=path)
        index = ClauseIndex(path)
    except (OSError, ValueError) as exc:
        logger.warning("bm25_unavailable", error=str(exc), path=str(path))
        return None
    logger.info("bm25_ready", clauses=index.size, terms=index.terms)
    return index
