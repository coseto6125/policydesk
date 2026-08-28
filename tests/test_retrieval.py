"""
The lexical channel: tantivy BM25 over the clause corpus, CJK-aware.

The failure this replaces is not "slightly worse ranking". ILIKE returned the *same*
three clauses for 換工作會不會影響保險, 解約可以拿回多少 and 保費繳不出來 — none of those
phrases is a substring of anything in the corpus, so the `kind IN (exclusion, carve_back,
waiting)` arm of the WHERE fired every time and the topic was ignored.
"""

from pathlib import Path

import pytest

SOURCE = Path("src/policydesk/retrieval/index.py").read_text()
TOOLS = Path("src/policydesk/agent/tools.py").read_text()


def test_both_sides_cut_with_the_same_function():
    """
    Cut the documents one way and the queries another and recall collapses silently:
    every query still returns something, just never the right thing.
    """
    build = SOURCE[SOURCE.index("async def build("):SOURCE.index("class BM25Retriever:")]
    search = SOURCE[SOURCE.index("    def search("):SOURCE.index("def _grams(")]
    assert "cut(heading)" in build
    assert "cut(query)" in search


def test_the_ngram_field_is_fed_uncut_text():
    """
    The ngram analyser re-tokenises raw text. Handing it the sentinel-joined form grams
    across the separator and produces tokens nothing matches.
    """
    build = SOURCE[SOURCE.index("async def build("):SOURCE.index("class BM25Retriever:")]
    ngram_line = build[build.index("F_NGRAM:"):build.index("\n", build.index("F_NGRAM:"))]
    assert "cut(" not in ngram_line


def test_the_boolean_is_assembled_rather_than_parsed():
    """
    `parse_query` on a sentinel-joined string sees two tokens at adjacent positions and
    builds a PhraseQuery, so 住院日額 only matched a clause with those words side by
    side and 不賠的情況 matched nothing at all.
    """
    search = SOURCE[SOURCE.index("    def search("):SOURCE.index("def _grams(")]
    code = search[search.index('"""', search.index('"""') + 3) + 3:]
    assert "parse_query" not in code
    assert "tantivy.Query.term_query(SCHEMA, field, token.lower())" in code
    assert "tantivy.Occur.Should" in code


def test_the_scope_filter_is_a_must_not_a_post_filter():
    """
    Scoring the whole corpus and then discarding what the customer does not own spends
    the limit on documents they will never be shown.
    """
    search = SOURCE[SOURCE.index("    def search("):SOURCE.index("def _grams(")]
    assert "tantivy.Occur.Must, tantivy.Query.term_query(SCHEMA, F_CORPUS, corpus)" in search
    assert "tantivy.Query.term_query(SCHEMA, F_SCOPE, s)" in search
    assert search.index("must: list") < search.index("must.append((tantivy.Occur.Must, tantivy.Query.boolean_query(text)))")


def test_one_index_holds_both_corpora():
    """
    Two indexes means two dictionaries built from two vocabularies, and the same query
    then cuts differently on each side — a miss that returns something every time and
    the right thing never.
    """
    from policydesk.retrieval.base import CLAUSE, STATUTE
    from policydesk.retrieval.index import _SOURCES

    assert set(_SOURCES) == {CLAUSE, STATUTE}
    assert "FROM clause" in _SOURCES[CLAUSE]
    assert "FROM statute_article" in _SOURCES[STATUTE]


def test_fusion_is_by_rank_not_by_score():
    """
    A BM25 score is unbounded in term frequency; a cosine is bounded to [-1, 1]. Adding
    them mixes units, and normalising per query makes the weights depend on how good the
    best hit happened to be.
    """
    from policydesk.retrieval.base import CLAUSE, Hit, rrf

    a = [Hit(CLAUSE, "art.1", "p", 900.0), Hit(CLAUSE, "art.2", "p", 12.0)]
    b = [Hit(CLAUSE, "art.2", "p", 0.61), Hit(CLAUSE, "art.3", "p", 0.60)]
    fused = rrf([a, b], limit=3)
    assert next(h.doc_id for h in fused) == "art.2", "found by both channels wins"
    assert {h.doc_id for h in fused} == {"art.1", "art.2", "art.3"}


def test_a_single_channel_hybrid_is_that_channel():
    """It is what makes the embedding half optional rather than structural."""
    from policydesk.retrieval.base import CLAUSE, Hit, HybridRetriever

    class Only:
        name = "only"

        def search(self, query, *, corpus, scope, limit):
            return [Hit(CLAUSE, "art.9", "p", 1.0), Hit(CLAUSE, "art.8", "p", 0.5)]

    hybrid = HybridRetriever([Only()])
    assert [h.doc_id for h in hybrid.search("x", corpus=CLAUSE, scope=[], limit=2)] == ["art.9", "art.8"]
    assert hybrid.channels == ["only"]


@pytest.mark.parametrize(("text", "expected"), [("住院日額", ["住院", "院日", "日額"]), ("癌", []), ("", [])])
def test_grams_are_pairs_only(text: str, expected: list[str]):
    """
    A single CJK character matches most of the corpus. The fallback earns its place on
    the pairs jieba failed to cut, and those are two characters or longer.
    """
    from policydesk.retrieval.index import _grams

    assert _grams(text) == expected


def test_grams_ignore_whitespace():
    from policydesk.retrieval.index import _grams

    assert _grams("住院 日額") == _grams("住院日額")


def test_cut_drops_nothing_but_blanks():
    from policydesk.retrieval.index import _SEP, cut

    assert cut("住院日額").split(_SEP), "some segmentation, whatever the dictionary yields"
    assert cut("   ") == ""


def test_the_corpus_supplies_its_own_vocabulary():
    """
    Jieba's bundled dictionary is Simplified. On this corpus it cuts 住院日額保險金給付
    into 住院日 / 額保險 / 金給付 — three tokens, none of them a word, none of them
    anything a customer types. The headings, benefit names and 附表1 procedure names are
    the terms the questions are made of, and they are already in the database.
    """
    assert "async def collect_terms(" in SOURCE
    body = SOURCE[SOURCE.index("async def collect_terms("):SOURCE.index("def _split_term(")]
    for table in ("clause", "benefit", "surgery_multiplier"):
        assert table in body


def test_the_dictionary_travels_with_the_index():
    """
    A query cut with a different dictionary than the documents misses, and it misses
    silently: something comes back every time and the right thing never does.
    """
    build = SOURCE[SOURCE.index("async def build("):SOURCE.index("class BM25Retriever:")]
    assert "TERMS_FILE" in build
    assert "load_terms(path)" in build
    opened = SOURCE[SOURCE.index("class BM25Retriever:"):SOURCE.index("    def search(")]
    assert "self.terms = load_terms(path)" in opened


def test_split_term_keeps_the_nouns_and_drops_the_grammar():
    from policydesk.retrieval.index import _split_term

    assert _split_term("保險金給付之限制") == ["保險金給付", "限制"]
    assert _split_term("職業或職務變更的通知義務") == ["職業", "職務變更", "通知義務"]
    assert _split_term("等待期 30 日（載於「疾病」定義）") == ["等待期", "30", "日", "載於", "疾病", "定義"]


def test_a_missing_index_degrades_rather_than_fails():
    """A desk whose ranking is worse still answers; one that will not start does not."""
    assert "return None" in SOURCE[SOURCE.index("async def open_index("):]
    body = TOOLS[TOOLS.index("async def find_clause("):TOOLS.index("LINES: frozenset")]
    assert "if index is not None and (hits :=" in body
    assert "return await db.fetch(" in body, "the SQL search stays as the fallback"


def test_the_ranking_survives_the_round_trip_to_postgres():
    """A search whose ranking is discarded on the way back is a search that did nothing."""
    body = TOOLS[TOOLS.index("async def _clauses_by_id("):TOOLS.index("@requires_identity\nasync def find_clause(")]
    assert "rows.sort(key=" in body
    assert "rank.get(" in body


def test_the_keys_are_bound_as_parallel_arrays():
    """
    Psqlpy binds a tuple list to record[] by panicking in its Rust layer — `entered
    unreachable code`, no SQL error, no column named.
    """
    body = TOOLS[TOOLS.index("async def _clauses_by_id("):TOOLS.index("@requires_identity\nasync def find_clause(")]
    assert "unnest($1::text[], $2::text[])" in body
    assert "= ANY($1::record[])" not in body


def test_the_index_is_opened_once_per_process():
    """A directory walk and an analyzer registration per turn is a turn's worth of it."""
    server = Path("src/policydesk/web/server.py").read_text()
    assert "open_index(application.ctx.db), open_vectors(application.ctx.db)" in server
    assert "HybridRetriever(channels) if channels else None" in server
    assert "open_index" not in server[server.index("async def customer_socket"):]
