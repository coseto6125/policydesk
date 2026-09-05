"""
The lexical channel: tantivy BM25 over the clause corpus, CJK-aware.

The failure this replaces is not "slightly worse ranking". ILIKE returned the *same*
three clauses for 換工作會不會影響保險, 解約可以拿回多少 and 保費繳不出來 — none of those
phrases is a substring of anything in the corpus, so the `kind IN (exclusion, carve_back,
waiting)` arm of the WHERE fired every time and the topic was ignored.
"""

import itertools
from pathlib import Path

import pytest

SOURCE = Path("src/policydesk/retrieval/index.py").read_text()
TOOLS = Path("src/policydesk/agent/tools.py").read_text()

STATUTE = "statute"


@pytest.fixture(scope="module")
async def index(db):
    """
    Open the real index, once for the module.

    Returns:
        The retriever, or a skip when nothing has been built.

    Source-reading tests below prove the query is *assembled* a certain way. Only a real
    index proves what that assembly ranks, and the two failures worth guarding here —
    a filler word deciding the order, and a question of nothing but filler answering with
    silence — are both invisible to a string search.
    """
    from policydesk.retrieval.index import INDEX_DIR, open_index

    if not (INDEX_DIR / "meta.json").is_file():
        pytest.skip("no index built; run policydesk-index")
    opened = await open_index(db)
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
    assert "FROM contract_clause" in _SOURCES[CLAUSE]
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
    """
    A directory walk and an analyzer registration per turn is a turn's worth of it.

    Asserted on where the retriever is built, not on the arguments it is built with. The
    first version pinned `HybridRetriever(channels) if channels else None` verbatim and
    broke on the day a third channel was passed to it — which is the constructor's
    business, and says nothing about how often the constructor runs.
    """
    server = Path("src/policydesk/web/server.py").read_text()
    startup = server[server.index("@app.before_server_start") : server.index("@app.after_server_stop")]
    # Each name on its own. Matching the two-argument gather verbatim broke the day a
    # third channel joined it — how the three are arranged is the caller's business, and
    # says nothing about how often they are opened.
    for opened in ("open_index(", "open_vectors(", "HybridRetriever("):
        assert opened in startup, f"{opened} is not opened in the startup hook"
    assert "HybridRetriever(" not in server[server.index("async def customer_socket") :]
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
    """
    The statute half of this test asserted the opposite and was wrong.

    It held `bm25 > embedding` on the reasoning that the law is short and a complaint
    sometimes quotes it verbatim. `scripts/recall.py` scores fourteen statute questions,
    each pairing a sentence a scenario says it answers with the article that scenario
    names, and every setting that lifts embedding above 0.5 beats the shipped 1.0/0.5:
    MRR went 0.41 → 0.55 and R@3 0.43 → 0.64 at 0.75/1.0.

    What survives the measurement is the comparison across corpora, not within one. BM25
    keeps a larger share of the statute vote than of the clause vote, because the law
    really is sometimes quoted word for word and a contract never is — 我有據實說明啊 is
    the query that earns it. That is the claim asserted here now.
    """
    from policydesk.retrieval.base import CLAUSE, STATUTE, WEIGHTS

    assert WEIGHTS[CLAUSE]["embedding"] > WEIGHTS[CLAUSE]["bm25"], "the contract shares no words with the question"
    assert WEIGHTS[STATUTE]["bm25"] > WEIGHTS[CLAUSE]["bm25"], "the law is short and sometimes quoted verbatim"
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


async def test_the_weighted_hybrid_reaches_what_only_one_channel_can(index, db):
    # The query this whole second channel exists for. 換工作 and 職業變更 share no
    # character, so the lexical side has nothing to rank on.
    from policydesk.retrieval.base import CLAUSE, HybridRetriever
    from policydesk.retrieval.vectors import open_vectors

    semantic = await open_vectors(db)
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


@pytest.mark.parametrize("overlap", [0, 4, 8])
def test_passages_long_document_covers_tail_and_every_character(overlap):
    from tokenizers import Tokenizer, models, pre_tokenizers

    from policydesk.retrieval.vectors import passages

    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))  # noqa: S106
    tokenizer.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    text = "契約標題\n" + "給付條件 不同條件 " * 80 + "只有尾段才有的權利。\n"
    spans = passages(text, tokenizer, tokens=24, overlap=overlap)
    assert len(spans) > 1
    assert spans[0][0] == 0
    assert spans[-1][1] == len(text)
    assert all(left < right for left, right in spans)
    assert all(next_left <= right for (_, right), (next_left, _) in itertools.pairwise(spans))
    assert all(len(tokenizer.encode(text[left:right]).ids) <= 24 for left, right in spans)
    assert "只有尾段才有的權利。" in text[spans[-1][0]:spans[-1][1]]


def test_rrf_shared_hit_keeps_semantic_excerpt():
    from policydesk.retrieval.base import CLAUSE, Hit, rrf

    lexical = Hit(corpus=CLAUSE, scope_id="p", doc_id="art.1", score=8)
    semantic = Hit(corpus=CLAUSE, scope_id="p", doc_id="art.1", score=0.8, start=2000, end=2400)
    for rankings in ([lexical], [semantic]), ([semantic], [lexical]):
        result = rrf(rankings, limit=1)
        assert (result[0].start, result[0].end) == (2000, 2400)


async def test_find_clause_semantic_tail_reaches_tool_output():
    from unittest.mock import AsyncMock

    from policydesk.agent.tools import DOCUMENT_CHARS, find_clause
    from policydesk.retrieval.base import CLAUSE, Hit

    body = "無關開頭。" * DOCUMENT_CHARS + "尾端給付條件。"
    heading = "保障範圍"
    full_text = heading + "\n" + body
    start = full_text.index("尾端")
    db = AsyncMock()
    db.fetch.return_value = [{"product_id": "p", "clause_id": "art.1", "heading": heading, "verbatim": body}]

    class TailRetriever:
        def search(self, query, **kwargs):
            assert kwargs["scope"] == ["p"]
            return [Hit(corpus=CLAUSE, scope_id="p", doc_id="art.1", score=1, start=start, end=len(full_text))]

    result = await find_clause(db, ["p"], "尾端條件", index=TailRetriever())
    assert result[0]["verbatim"] == "…尾端給付條件。"


async def test_find_clause_short_article_preserves_proviso_outside_match():
    from unittest.mock import AsyncMock

    from policydesk.agent.executor import _short
    from policydesk.agent.tools import find_clause
    from policydesk.retrieval.base import CLAUSE, Hit

    body = "美容手術不予給付。但為重建基本功能所作之必要整型，不在此限。"
    heading = "除外責任"
    db = AsyncMock()
    db.fetch.return_value = [{"product_id": "p", "clause_id": "art.1", "heading": heading, "verbatim": body}]

    class NarrowRetriever:
        def search(self, query, **kwargs):
            return [Hit(corpus=CLAUSE, scope_id="p", doc_id="art.1", score=1,
                        start=len(heading) + 1, end=len(heading) + 11)]

    result = _short(await find_clause(db, ["p"], "美容手術", index=NarrowRetriever()))
    assert result[0]["verbatim"] == body


@pytest.mark.parametrize("size", [1398, 1436, 4000])
async def test_find_clause_bounded_article_preserves_tail_exception(size):
    from unittest.mock import AsyncMock

    from policydesk.agent.executor import _short
    from policydesk.agent.tools import find_clause
    from policydesk.retrieval.base import CLAUSE, Hit

    proviso = "但為重建基本功能所作之必要整型，不在此限。"
    body = "條" * (size - len(proviso)) + proviso
    heading = "除外責任"
    db = AsyncMock()
    db.fetch.return_value = [{"product_id": "p", "clause_id": "art.1", "heading": heading, "verbatim": body}]

    class NarrowRetriever:
        def search(self, query, **kwargs):
            return [Hit(corpus=CLAUSE, scope_id="p", doc_id="art.1", score=1,
                        start=len(heading) + 1, end=len(heading) + 100)]

    result = _short(await find_clause(db, ["p"], "除外責任", index=NarrowRetriever()))
    assert result[0]["verbatim"] == body
    assert not result[0]["verbatim"].endswith("…"), "a whole article carries no cut mark"


def test_short_long_clause_marks_text_truncation():
    from policydesk.agent.executor import _short
    from policydesk.agent.tools import DOCUMENT_CHARS

    result = _short({"verbatim": "條件" * DOCUMENT_CHARS, "heading": "保障範圍"})
    assert result["verbatim"].endswith("…"), "a quote cut here carries the same mark the retrieval path uses"
    assert result["truncated_fields"] == ["verbatim"]
    assert len(result["verbatim"]) == DOCUMENT_CHARS


async def test_build_failed_encoder_keeps_active_generation(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from tokenizers import Tokenizer, models

    from policydesk.retrieval import vectors

    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))  # noqa: S106
    tokenizer.save(str(tmp_path / "tokenizer.json"))
    (tmp_path / "current.json").write_text('{"generation":"previous"}')
    monkeypatch.setattr(vectors, "documents", AsyncMock(return_value=[{"key": "clause|p|a", "text": "合約"}]))

    class BrokenEncoder:
        model = "test-model"

        def __init__(self, url):
            pass

        def encode(self, *args, **kwargs):
            raise OSError("server disconnected")

    monkeypatch.setattr(vectors, "ServerEmbedder", BrokenEncoder)
    with pytest.raises(OSError, match="disconnected"):
        await vectors.build(AsyncMock(), path=tmp_path, model_dir=tmp_path, server_url="http://localhost:8090")
    assert (tmp_path / "current.json").read_text() == '{"generation":"previous"}'


@pytest.mark.parametrize(("covered", "unchanged"), [(True, True), (False, True), (True, False)])
async def test_audit_passage_coverage_serializable_and_source_sensitive(tmp_path, monkeypatch, covered, unchanged):
    from unittest.mock import AsyncMock

    import numpy as np
    from msgspec import json

    from policydesk.retrieval import vectors

    source = [{"key": "clause|p|art.1", "text": "保障條件與限制"}]
    root = tmp_path / "snapshot"
    root.mkdir()
    matrix = np.zeros((2, vectors.DIM), dtype=np.float32)
    matrix[:, 0] = 1
    np.save(root / "vectors.npy", matrix)
    np.save(root / "keys.npy", np.array([source[0]["key"]] * 2))
    np.save(root / "spans.npy", np.array([(0, 3), (3 if covered else 4, len(source[0]["text"]))]))
    manifest = {"generation": "snapshot", "source_sha256": vectors.fingerprint(source)}
    (tmp_path / "current.json").write_bytes(json.encode(manifest))
    if not unchanged:
        source[0]["text"] = "另有條件與限制"
    monkeypatch.setattr(vectors, "documents", AsyncMock(return_value=source))

    result = await vectors.audit(AsyncMock(), path=tmp_path)
    decoded = json.decode(json.encode(result))
    assert decoded["complete"] is (covered and unchanged)
    assert decoded["uncovered"] == (0 if covered else 1)
    assert decoded["source_matches"] is unchanged


async def test_audit_identical_sources_in_different_order_remain_current(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    import numpy as np
    from msgspec import json

    from policydesk.retrieval import vectors

    source = [{"key": "statute|s|art.1", "text": "原文甲"}, {"key": "statute|s|art.1.1", "text": "原文乙"}]
    root = tmp_path / "snapshot"
    root.mkdir()
    matrix = np.zeros((2, vectors.DIM), dtype=np.float32)
    matrix[:, 0] = 1
    np.save(root / "vectors.npy", matrix)
    np.save(root / "keys.npy", np.array([row["key"] for row in source]))
    np.save(root / "spans.npy", np.array([(0, 3), (0, 3)]))
    (tmp_path / "current.json").write_bytes(json.encode({
        "generation": "snapshot", "source_sha256": vectors.fingerprint(source),
    }))
    monkeypatch.setattr(vectors, "documents", AsyncMock(return_value=source[::-1]))
    result = await vectors.audit(AsyncMock(), path=tmp_path)
    assert result["complete"] is True


def test_server_embedder_invalid_rows_rejects_response(monkeypatch):
    import numpy as np

    from policydesk.retrieval.vectors import DIM, ServerEmbedder

    def response(self, route, payload=None):
        if route == "/v1/models":
            return {"data": [{"id": "bge"}]}
        return {"data": [{"index": 1, "embedding": np.ones(DIM).tolist()}]}

    monkeypatch.setattr(ServerEmbedder, "_request", response)
    with pytest.raises(ValueError, match="indices"):
        ServerEmbedder("http://localhost:8090").encode(["一筆輸入"])


def _write_vector_generation(root, *, generation: str, encoder: dict) -> None:
    """Shared fixture: one manifest, one one-row matrix, matching `EmbeddingRetriever`'s layout."""
    import numpy as np
    from msgspec import json

    from policydesk.retrieval import vectors

    gen_dir = root / generation
    gen_dir.mkdir()
    np.save(gen_dir / "vectors.npy", np.zeros((1, vectors.DIM), dtype=np.float32))
    np.save(gen_dir / "keys.npy", np.array(["clause|p|art.1"]))
    np.save(gen_dir / "spans.npy", np.array([(0, 3)]))
    manifest = {"generation": generation, "encoder": encoder, "documents": 1, "passages": 1,
                "source_sha256": "x", "tokens": 512, "overlap": 64}
    (root / "current.json").write_bytes(json.encode(manifest))


def test_cf_backend_dispatches_to_cloudflare_encoder(tmp_path, monkeypatch):
    """A manifest built by `cf-workers-ai` must open with `CloudflareEncoder`, not ONNX."""
    import numpy as np

    from policydesk.retrieval import vectors

    _write_vector_generation(tmp_path, generation="gen1", encoder={"backend": "cf-workers-ai", "model": vectors.CF_MODEL})

    class FakeCFEncoder:
        def __init__(self, model):
            self.model = model

        def encode(self, texts, *, progress=0):
            return np.zeros((len(texts), vectors.DIM), dtype=np.float32)

    monkeypatch.setattr(vectors, "CloudflareEncoder", FakeCFEncoder)
    retriever = vectors.EmbeddingRetriever(tmp_path)
    assert isinstance(retriever._embedder, FakeCFEncoder)
    assert retriever._embedder.model == vectors.CF_MODEL


def test_cf_backend_model_mismatch_fails_loudly(tmp_path, monkeypatch):
    """
    llama-server and Workers AI serve different weights of bge-m3 (quantized vs not) —
    a query encoded on one against an index built by the other is a silent regression,
    never an error. `POLICYDESK_CF_MODEL` disagreeing with the manifest must raise before
    a single vector is compared, not degrade the ranking quietly.
    """
    from policydesk.retrieval import vectors

    _write_vector_generation(tmp_path, generation="gen1", encoder={"backend": "cf-workers-ai", "model": vectors.CF_MODEL})
    monkeypatch.setenv("POLICYDESK_CF_MODEL", "@cf/some-other-model")
    with pytest.raises(ValueError, match="differs from the indexed model"):
        vectors.EmbeddingRetriever(tmp_path)


async def test_build_selects_cf_workers_ai_when_no_server_url_but_creds_present(tmp_path, monkeypatch):
    """
    `build` follows the same presence-based dispatch as the llama-server branch: a
    server URL wins, cf-workers-ai is next when configured, ONNX is the fallback.
    """
    from unittest.mock import AsyncMock

    import numpy as np
    from msgspec import json
    from tokenizers import Tokenizer, models

    from policydesk.retrieval import vectors

    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))  # noqa: S106
    tokenizer.save(str(tmp_path / "tokenizer.json"))
    monkeypatch.setattr(vectors, "documents", AsyncMock(return_value=[{"key": "clause|p|a", "text": "合約"}]))
    monkeypatch.delenv("POLICYDESK_EMBED_URL", raising=False)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_AUTH_TOKEN", "token")

    class FakeCFEncoder:
        def __init__(self, model):
            self.model = model

        def encode(self, texts, *, progress=0):
            return np.zeros((len(texts), vectors.DIM), dtype=np.float32)

    monkeypatch.setattr(vectors, "CloudflareEncoder", FakeCFEncoder)
    await vectors.build(AsyncMock(), path=tmp_path, model_dir=tmp_path)
    manifest = json.decode((tmp_path / "current.json").read_bytes())
    assert manifest["encoder"] == {"backend": "cf-workers-ai", "model": vectors.CF_MODEL}


def test_parse_cf_credentials_requires_matching_counts(monkeypatch):
    from policydesk.retrieval.vectors import parse_cf_credentials

    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_AUTH_TOKEN", raising=False)
    assert parse_cf_credentials(None, None) == []
    assert parse_cf_credentials("a,b", "t1,t2") == [("a", "t1"), ("b", "t2")]
    with pytest.raises(ValueError, match="counts must match"):
        parse_cf_credentials("a,b", "t1")


async def test_cloudflare_embed_halves_batch_on_400_with_more_than_one_item(monkeypatch):
    """
    The measured split: a 400 on a multi-item batch means "this batch, not this text" —
    halve and retry rather than fail the whole request. A single-item 400 has nowhere
    left to halve to, so it propagates.
    """
    import aiohttp

    from policydesk.retrieval.vectors import DIM, CloudflareClient

    calls: list[int] = []

    async def fake_run(self, model, payload):
        texts = payload["text"]
        calls.append(len(texts))
        if len(texts) > 1:
            raise aiohttp.ClientResponseError(request_info=None, history=(), status=400)
        return {"result": {"data": [[0.0] * DIM]}}

    monkeypatch.setattr(CloudflareClient, "run", fake_run)
    client = object.__new__(CloudflareClient)
    rows = await client.embed("model", ["a", "b", "c", "d"], max_batch=4)
    assert len(rows) == 4
    assert calls[0] == 4, "the whole batch is tried first"
    assert calls.count(1) == 4, "it must bottom out at one item per call, not fail early"


async def test_cloudflare_embed_retries_5xx_and_propagates_other_4xx(monkeypatch):
    import aiohttp

    from policydesk.retrieval.vectors import DIM, CloudflareClient

    client = object.__new__(CloudflareClient)

    attempts = {"n": 0}

    async def flaky_5xx(self, model, payload):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise aiohttp.ClientResponseError(request_info=None, history=(), status=503)
        return {"result": {"data": [[0.0] * DIM]}}

    monkeypatch.setattr(CloudflareClient, "run", flaky_5xx)
    rows = await client.embed("model", ["a"], max_batch=4)
    assert len(rows) == 1
    assert attempts["n"] == 2, "a transient 5xx must be retried, not fatal"

    async def permanent_403(self, model, payload):
        raise aiohttp.ClientResponseError(request_info=None, history=(), status=403)

    monkeypatch.setattr(CloudflareClient, "run", permanent_403)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.embed("model", ["a"], max_batch=4)


async def test_cloudflare_encoder_embeds_real_clause_text_at_1024_dims(db):
    """
    Acceptance: real clause text, through the live Workers AI endpoint, asserting
    dimension and row count — not a mock of the response shape.
    """
    import asyncio
    import os

    import numpy as np

    if not os.environ.get("CLOUDFLARE_ACCOUNT_ID") or not os.environ.get("CLOUDFLARE_AUTH_TOKEN"):
        pytest.skip("CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_AUTH_TOKEN not set in this environment")

    from policydesk.retrieval.vectors import DIM, CloudflareEncoder

    rows = await db.fetch("SELECT verbatim FROM contract_clause WHERE verbatim IS NOT NULL LIMIT $1", [5])
    texts = [row["verbatim"] for row in rows]
    assert texts, "contract_clause has no verbatim text in this database"

    vectors_out = await asyncio.to_thread(CloudflareEncoder().encode, texts)

    assert vectors_out.shape == (len(texts), DIM)
    assert np.isfinite(vectors_out).all()
    assert np.allclose(np.linalg.norm(vectors_out, axis=1), 1, atol=1e-4)


async def test_suitable_products_retrieves_only_sql_eligible_contracts():
    from unittest.mock import AsyncMock

    from policydesk.agent.tools import DOCUMENT_CHARS, suitable_products
    from policydesk.retrieval.base import CLAUSE, Hit

    db = AsyncMock()
    evidence = {"product_id": "hospital", "clause_id": "art.9", "heading": "住院保障",
                "verbatim": "不相干內容" * DOCUMENT_CHARS + "契約原文"}
    full_text = evidence["heading"] + "\n" + evidence["verbatim"]
    db.fetch.side_effect = [
        [{"product_id": "cheap", "requires_main": False}, {"product_id": "hospital", "requires_main": True}],
        [{"product_id": "hospital", "clause_id": "waiting", "kind": "waiting", "verbatim": "等待期原文"}],
        [evidence],
    ]

    class ScopedRetriever:
        def search(self, query, **kwargs):
            assert query == "住院保障"
            assert kwargs["scope"] == ["cheap", "hospital"]
            return [Hit(corpus=CLAUSE, scope_id="ineligible", doc_id="art.1", score=1),
                    Hit(corpus=CLAUSE, scope_id="hospital", doc_id="art.9", score=0.9,
                        start=len(full_text) - 4, end=len(full_text))]

    rows = await suitable_products(db, insurance_age=35, occupation_class=1, budget=20000,
                                   line="health", limit=1, need="住院保障", index=ScopedRetriever())
    assert [row["product_id"] for row in rows] == ["hospital"]
    assert rows[0]["requires_main"] is True
    assert rows[0]["selection_basis"] == "eligibility and contract retrieval"
    assert {row["clause_id"] for row in rows[0]["contract_evidence"]} == {"art.9", "waiting"}
    matched = next(row for row in rows[0]["contract_evidence"] if row["clause_id"] == "art.9")
    assert matched["verbatim"] == "…契約原文"
