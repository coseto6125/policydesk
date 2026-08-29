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

STATUTE = "statute"


@pytest.fixture(scope="module")
def index():
    """
    Open the real index, once for the module.

    Returns:
        The retriever, or a skip when nothing has been built.

    Source-reading tests below prove the query is *assembled* a certain way. Only a real
    index proves what that assembly ranks, and the two failures worth guarding here —
    a filler word deciding the order, and a question of nothing but filler answering with
    silence — are both invisible to a string search.
    """
    import asyncio

    from policydesk.core.db import Database
    from policydesk.retrieval.index import INDEX_DIR, open_index

    if not (INDEX_DIR / "meta.json").is_file():
        pytest.skip("no index built; run policydesk-index")
    opened = asyncio.run(open_index(Database()))
    if opened is None:
        pytest.skip("the index would not open")
    return opened


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


def test_a_query_of_nothing_but_stop_words_still_answers(index):
    # 保單 alone cuts to one token, the stop list drops it, and without a fallback the
    # boolean had no clause and `search` returned []. A customer reads an empty result as
    # 法規裡沒有這件事, which is a claim about the corpus; what actually happened is that
    # the question carried nothing to rank on. A bad ranking says something.
    assert index.search("保單", corpus=STATUTE, limit=2), "an all-stop-word query returned silence"
    assert index.search("可以", corpus=STATUTE, limit=2)


def test_a_stop_word_no_longer_decides_the_ranking(index):
    # 可以 appears in 4 of 1,212 articles and 復效 in 7, so the filler had the higher IDF
    # and both of these returned the same three provisions as each other. They must not.
    lapse = [h.doc_id for h in index.search("保單停效可以復效嗎", corpus=STATUTE, limit=3)]
    rescind = [h.doc_id for h in index.search("猶豫期幾天可以撤銷", corpus=STATUTE, limit=3)]
    assert lapse != rescind, "two different questions returned one ranking"
    assert lapse[0].startswith("art.116"), f"停效復效 should reach 第116條, got {lapse}"


def test_weighting_is_per_corpus_and_leaves_a_beaten_channel_a_vote():
    from policydesk.retrieval.base import CLAUSE, STATUTE, WEIGHTS

    assert WEIGHTS[CLAUSE]["embedding"] > WEIGHTS[CLAUSE]["bm25"], "the contract shares no words with the question"
    assert WEIGHTS[STATUTE]["bm25"] > WEIGHTS[STATUTE]["embedding"], "the law is short and sometimes quoted verbatim"
    for corpus in (CLAUSE, STATUTE):
        assert min(WEIGHTS[corpus].values()) > 0, (
            "a weight of zero is one channel per corpus wearing a hybrid's name"
        )


def test_a_confident_channel_is_not_diluted_by_a_confident_mistake():
    # Unweighted RRF gives a wrong channel's first place the same 1/(k+1) as a right
    # channel's. Measured: 換工作會不會影響保險 had embedding on 職業或職務變更的通知義務
    # and BM25 on 本商品說明書僅供參考 boilerplate, and the fusion led with the boilerplate.
    from policydesk.retrieval.base import CLAUSE, Hit, rrf

    right = [Hit(corpus=CLAUSE, doc_id="art.18", scope_id="p1", score=0.6)]
    wrong = [Hit(corpus=CLAUSE, doc_id="art.6", scope_id="p1", score=9.0)]
    flat = rrf([("bm25", wrong), ("embedding", right)], limit=2)
    weighted = rrf([("bm25", wrong), ("embedding", right)], limit=2, weights={"bm25": 0.5, "embedding": 1.0})
    assert flat[0].doc_id == "art.6", "unweighted, the two first places tie and insertion order decides"
    assert weighted[0].doc_id == "art.18"
    assert [h.doc_id for h in weighted] == ["art.18", "art.6"], "the halved channel still ranks, it does not vanish"


def test_the_weighted_hybrid_reaches_what_only_one_channel_can(index):
    # The query this whole second channel exists for. 換工作 and 職業變更 share no
    # character, so the lexical side has nothing to rank on.
    import asyncio

    from policydesk.core.db import Database
    from policydesk.retrieval.base import CLAUSE, HybridRetriever
    from policydesk.retrieval.vectors import open_vectors

    semantic = asyncio.run(open_vectors(Database()))
    if semantic is None:
        pytest.skip("no vectors built")
    hits = HybridRetriever([index, semantic]).search("換工作會不會影響保險", corpus=CLAUSE, scope=(), limit=3)
    assert hits, "the hybrid returned nothing for a question the corpus answers"


def test_a_zero_weight_switches_a_channel_off_rather_than_ranking_it_last():
    # Without the guard the zero-weighted channel's documents still enter the fusion at
    # score 0.0 and still take slots. Nothing in WEIGHTS is zero, so this guards the
    # reading rather than the behaviour: the next person writing 0.0 means "off".
    from policydesk.retrieval.base import CLAUSE, Hit, rrf

    kept = [Hit(corpus=CLAUSE, doc_id="a", scope_id="p", score=1.0)]
    dropped = [Hit(corpus=CLAUSE, doc_id="b", scope_id="p", score=9.0)]
    fused = rrf([("bm25", dropped), ("embedding", kept)], limit=4, weights={"bm25": 0.0, "embedding": 1.0})
    assert [h.doc_id for h in fused] == ["a"]


def test_a_limit_of_zero_returns_nothing_rather_than_everything(index):
    # `[-0:]` is `[0:]`. The lexical channel is asserted here too, so the two channels
    # cannot disagree about what a limit of zero means.
    from policydesk.retrieval.base import STATUTE

    assert index.search("復效", corpus=STATUTE, limit=0) == []
