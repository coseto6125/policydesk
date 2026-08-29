"""
The statute corpus, asserted against the real 保險法 rather than a hand-written sample.

Three things can go wrong here and only one of them is loud. The loud one is a parse that
crashes. The quiet ones are a citation that points at the wrong sentence, and a repealed
provision that survives an amendment and stays citable — both produce a desk that answers
confidently and wrongly, which is the failure this whole project is built against.

The fixture is a live fetch, skipped when the database is not up, because the shapes that
break the parser are in the government's markup and not in anything anyone would write by
hand: 之一 articles, 款 numbered in the text, articles whose 項 are split across
`show-number` divs, and chapters that apply forward.
"""

import pytest

from policydesk.agent import statute
from policydesk.agent.statute import Article, _cn_to_int, _doc_id, parse
from policydesk.core.db import Database

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
async def db():
    pool = Database()
    try:
        await pool.fetch_val("SELECT 1")
    except Exception:
        pytest.skip("policydesk-pg is not up")
    yield pool
    await pool.close()


@pytest.fixture(scope="module")
async def loaded(db):
    """Ingest the statute corpus once for the module."""
    if not await db.fetch_val("SELECT count(*) FROM statute_article WHERE statute_id = 'insurance_act'"):
        await statute.ingest(db, ["insurance_act"])
    return db


def test_cn_to_int_reads_compound_tens():
    assert _cn_to_int("二十三") == 23
    assert _cn_to_int("十") == 10
    assert _cn_to_int("七") == 7


def test_doc_id_of_whole_article_has_no_paragraph():
    assert _doc_id(64, 0, None, None) == "art.64"


def test_doc_id_of_branch_article_keeps_the_branch():
    # 第八條之一 is a distinct provision from 第八條, and a citation that flattens them
    # points at text the reader did not mean.
    assert _doc_id(8, 1, None, None) == "art.8-1"
    assert _doc_id(8, 1, 2, 3) == "art.8-1.2.3"


def test_parse_reads_amendment_date_as_gregorian():
    html = """<tr id="trLNNDate"><th>修正日期：</th><td>民國 114 年 06 月 18 日 </td></tr>"""
    meta, _ = parse(html)
    assert meta["amended_at"].year == 2025
    assert (meta["amended_at"].month, meta["amended_at"].day) == (6, 18)


def _row(number: str, lines: list[tuple[str, str]]) -> str:
    body = "".join(f'<div class="{cls}">{text}</div>' for cls, text in lines)
    return (
        f'<div class="row"><div class="col-no"> <a href="x" name="{number}">第 {number} 條</a></div>'
        f'<div class="col-data"><div class="law-article">\n{body}</div>\n</div></div>'
    )


def test_parse_numbers_paragraphs_in_order():
    html = _row("64", [("line-0000", "第一項"), ("line-0000", "第二項"), ("line-0000", "第三項")])
    _, articles = parse(html)
    assert [a.doc_id for a in articles] == ["art.64", "art.64.1", "art.64.2", "art.64.3"]


def test_parse_stores_the_whole_article_alongside_its_paragraphs():
    # A citation written as §64 has to resolve, so the concatenated article is a row of
    # its own rather than something a caller reassembles.
    html = _row("64", [("line-0000", "甲"), ("line-0000", "乙")])
    _, articles = parse(html)
    whole = next(a for a in articles if a.paragraph is None)
    assert whole.verbatim == "甲 乙"


def test_parse_reads_subparagraph_numbers_from_the_text():
    # Numbered in the source as 一、二、三、. Counting them instead would renumber a
    # statute that reserves an item, and the renumbered citation would point at a real
    # 款 saying something else.
    html = _row("65", [("line-0000", "前言"), ("line-0004", "一、甲"), ("line-0004", "二、乙"), ("line-0004", "三、丙")])
    _, articles = parse(html)
    assert [a.doc_id for a in articles if a.subparagraph] == ["art.65.1.1", "art.65.1.2", "art.65.1.3"]


def test_parse_attaches_subparagraph_to_the_paragraph_above_it():
    html = _row("36", [("line-0000", "項一"), ("line-0000", "項二"), ("line-0004", "一、屬於項二")])
    _, articles = parse(html)
    item = next(a for a in articles if a.subparagraph)
    assert (item.paragraph, item.doc_id) == (2, "art.36.2.1")


def test_parse_carries_the_chapter_forward():
    html = '<div class="h3 char-2">   第 一 章 總則</div>' + _row("1", [("line-0000", "甲")])
    _, articles = parse(html)
    assert all(a.chapter == "第 一 章 總則" for a in articles)


def test_parse_does_not_apply_a_later_chapter_to_an_earlier_article():
    html = (
        '<div class="h3 char-2">第 一 章 總則</div>'
        + _row("1", [("line-0000", "甲")])
        + '<div class="h3 char-2">第 二 章 保險契約</div>'
        + _row("43", [("line-0000", "乙")])
    )
    _, articles = parse(html)
    assert {a.article: a.chapter for a in articles} == {1: "第 一 章 總則", 43: "第 二 章 保險契約"}


async def test_ingest_loads_the_insurance_act(loaded):
    count = await loaded.fetch_val("SELECT count(*) FROM statute_article WHERE statute_id = 'insurance_act'")
    # 保險法 has ~180 articles; with paragraphs and subparagraphs it is several hundred.
    assert count > 500


async def test_article_64_paragraph_2_is_the_rescission_provision(loaded):
    rows = await statute.find_articles(loaded, ["art.64.2"], ["insurance_act"])
    assert "保險人得解除契約" in rows[0]["verbatim"]


async def test_article_64_paragraph_3_carries_the_time_limit(loaded):
    # The provision that protects the customer, and the reason the soothe scenario
    # retrieves the whole article rather than the paragraph the complaint quotes.
    rows = await statute.find_articles(loaded, ["art.64.3"], ["insurance_act"])
    assert "二年" in rows[0]["verbatim"]


async def test_branch_article_is_stored_separately_from_its_base(loaded):
    base, branch = await statute.find_articles(loaded, ["art.8", "art.8-1"], ["insurance_act"])
    assert base["verbatim"] != branch["verbatim"]
    assert "保險業務員" in branch["verbatim"]


async def test_find_articles_returns_them_in_the_order_asked_for(loaded):
    rows = await statute.find_articles(loaded, ["art.65", "art.64", "art.1"], ["insurance_act"])
    assert [r["doc_id"] for r in rows] == ["art.65", "art.64", "art.1"]


async def test_search_returns_paragraphs_not_whole_articles(loaded):
    # The whole-article row is the same text concatenated, so returning both would spend
    # half the budget saying the same thing twice.
    rows = await statute.search_statute(loaded, "解除契約", ["insurance_act"], limit=5)
    assert rows
    assert all(r["paragraph"] is not None for r in rows)


async def test_search_scoped_to_one_statute_returns_only_that_statute(loaded):
    rows = await statute.search_statute(loaded, "保險", ["insurance_act"], limit=5)
    assert {r["statute_id"] for r in rows} == {"insurance_act"}


async def test_terms_include_statutory_vocabulary_absent_from_the_clause_corpus(loaded):
    terms = await statute.statute_terms(loaded)
    # These are read out of the statute's own 本法所稱X，指… sentences, which is where the
    # words a complaining customer uses actually come from.
    assert {"要保人", "被保險人", "受益人", "保險利益"} <= set(terms)


async def test_reingest_replaces_rather_than_accumulates(loaded):
    before = await loaded.fetch_val("SELECT count(*) FROM statute_article WHERE statute_id = 'insurance_act'")
    await statute.store(
        loaded,
        "insurance_act",
        statute.STATUTES["insurance_act"],
        {"name": "保險法", "authority": "金融監督管理委員會", "amended_at": None},
        [Article("art.1", 1, 0, None, None, "第 一 章 總則", "", "重寫")],
    )
    after = await loaded.fetch_val("SELECT count(*) FROM statute_article WHERE statute_id = 'insurance_act'")
    assert (before, after) != (1, 1)
    assert after == 1
    # Restore, so the module's other tests and the running desk see the real corpus.
    await statute.ingest(loaded, ["insurance_act"])
    assert await loaded.fetch_val("SELECT count(*) FROM statute_article WHERE statute_id = 'insurance_act'") == before


async def test_search_finds_a_provision_from_a_whole_complaint_sentence(loaded):
    # 你們憑什麼解除我的契約 is not a substring of any provision, so a LIKE over the whole
    # sentence returns nothing while §64 sits in the table. That empty list reads to the
    # customer as the law having nothing to say about what happened to him.
    rows = await statute.search_statute(loaded, "你們憑什麼解除我的契約", ["insurance_act"], limit=4)
    assert {r["doc_id"] for r in rows} & {"art.64.2", "art.64.3"}


async def test_search_brings_the_neighbouring_paragraphs_of_a_hit(loaded):
    rows = await statute.search_statute(loaded, "你們憑什麼解除我的契約", ["insurance_act"], limit=4)
    found = {r["doc_id"] for r in rows}
    # The customer's half of §64 arrives with the company's half, whichever one ranked.
    assert {"art.64.2", "art.64.3"} <= found


async def test_siblings_can_be_turned_off(loaded):
    rows = await statute.search_statute(loaded, "解除契約", ["insurance_act"], limit=3, siblings=False)
    assert len(rows) == 3


async def test_siblings_stay_within_the_window(loaded):
    # 保險法 §136 has eight 項, seven about company structure. A hit on one must not drag
    # the other seven into a reply someone is reading while angry.
    hit = (await statute.find_articles(loaded, ["art.136.1"], ["insurance_act"]))[0]
    rows = await statute.with_siblings(loaded, [hit])
    assert len(rows) <= 2 * statute.SIBLING_WINDOW + 1


async def test_siblings_keep_the_articles_in_rank_order(loaded):
    hits = await statute.search_statute(loaded, "解除契約", ["insurance_act"], limit=3, siblings=False)
    expanded = await statute.with_siblings(loaded, hits)
    # Each hit's article appears in the order it ranked, its paragraphs together.
    order = list(dict.fromkeys(r["article"] for r in expanded))
    assert order == list(dict.fromkeys(h["article"] for h in hits))


async def test_siblings_do_not_repeat_a_paragraph_two_hits_share(loaded):
    hits = await statute.find_articles(loaded, ["art.64.2", "art.64.3"], ["insurance_act"])
    rows = await statute.with_siblings(loaded, hits)
    assert len(rows) == len({r["doc_id"] for r in rows})


async def test_siblings_exclude_the_whole_article_row(loaded):
    # It is the same text concatenated; beside its own paragraphs it is the passage twice.
    hit = (await statute.find_articles(loaded, ["art.64.2"], ["insurance_act"]))[0]
    rows = await statute.with_siblings(loaded, [hit])
    assert all(r["paragraph"] is not None for r in rows)


def test_tokenise_does_not_manufacture_phrases_across_word_boundaries():
    # Overlapping n-grams were tried first: 金管會申訴 produced 會申, which matches
    # 公平交易委員會申報 inside an article about a receivership transfer. A fabricated
    # phrase is indistinguishable from a real one at ranking time.
    words = statute._tokenise("我要去金管會申訴你們")
    assert "會申" not in words
    assert "金管會" in words
    assert "申訴" in words


def test_tokenise_drops_the_words_of_complaining():
    assert statute._tokenise("你們憑什麼這樣") == []


def test_tokenise_drops_company_because_it_is_the_customers_word_for_you():
    # In the customer's sentence 公司 means *you*; in the statute it is a corporate entity
    # the regulator licenses. Keeping it ranked 第164-1條 命公司解除經理人職務 above 第64條.
    assert "公司" not in statute._tokenise("公司說要解除我的契約")


def test_tokenise_keeps_statutory_compounds_whole():
    # The bundled dictionary is Simplified; 據實說明 cut by it matches nothing.
    assert "據實說明" in statute._tokenise("我明明有據實說明")
    assert "等待期" in statute._tokenise("沒人跟我說有等待期")


async def test_search_ranks_the_rescission_provision_above_the_regulators_one(loaded):
    # 第164-1條第1項第2款 命公司解除經理人或職員之職務 is short and contains 解除. Counting
    # matches equally put it first for a customer asking why his policy is being rescinded.
    rows = await statute.search_statute(
        loaded, "公司說要解除我的契約，但我已經繳了五年", ["insurance_act"], limit=4, siblings=False
    )
    found = [r["doc_id"] for r in rows]
    assert not any(f.startswith("art.164-1") for f in found), found
    assert any(f.startswith(("art.64", "art.68", "art.25", "art.57")) for f in found), found


async def test_two_phrasings_of_one_complaint_reach_the_same_provision(loaded):
    # 公司說要解除我的契約 and 你們憑什麼解除我的契約 are the same customer on two days.
    for phrasing in ("公司說要解除我的契約，但我已經繳了五年", "你們憑什麼解除我的契約"):
        rows = await statute.search_statute(loaded, phrasing, ["insurance_act"], limit=4)
        assert "art.64.3" in {r["doc_id"] for r in rows}, phrasing


async def test_search_does_not_reward_a_rare_word_that_merely_appears(loaded):
    # 第166-1條 punishes 散布流言 with 五年以下有期徒刑. 五年 is rare, so weight alone put
    # it above every provision matching both 解除 and 契約 for a customer who said 我繳了
    # 五年. Coverage has to outrank weight.
    rows = await statute.search_statute(
        loaded, "公司說要解除我的契約，但我已經繳了五年", ["insurance_act"], limit=4, siblings=False
    )
    assert "art.166-1.1" not in {r["doc_id"] for r in rows}


class _FakeRetriever:
    """A retriever that returns exactly what it is told to, to prove the wiring."""

    name = "fake"

    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def search(self, query, *, corpus, scope, limit):
        self.calls.append((query, corpus, tuple(scope), limit))
        return self.hits


def _hit(scope_id, doc_id, score):
    from policydesk.retrieval.base import STATUTE, Hit

    return Hit(corpus=STATUTE, doc_id=doc_id, scope_id=scope_id, score=score)


async def test_a_retriever_decides_the_order_and_the_sql_ranking_is_not_used(loaded):
    # Reversed against what the SQL fallback returns, so passing would be impossible if
    # the retriever's order were being discarded.
    ranked = _FakeRetriever([_hit("insurance_act", "art.64.3", 9.0), _hit("insurance_act", "art.64.1", 8.0)])
    rows = await statute.search_statute(
        loaded, "解除契約", ["insurance_act"], limit=2, siblings=False, retriever=ranked
    )
    assert [r["doc_id"] for r in rows] == ["art.64.3", "art.64.1"]


async def test_the_retriever_is_asked_for_the_statute_corpus_only(loaded):
    # One index, two corpora. Without the corpus filter a question about the law would be
    # answered with clauses out of some product's contract.
    from policydesk.retrieval.base import STATUTE

    ranked = _FakeRetriever([_hit("insurance_act", "art.64.2", 1.0)])
    await statute.search_statute(loaded, "解除契約", ["insurance_act"], limit=3, retriever=ranked)
    query, corpus, scope, _ = ranked.calls[0]
    assert corpus == STATUTE
    assert scope == ("insurance_act",)
    assert query == "解除契約", "the retriever cuts the query with the corpus dictionary; do not pre-cut it"


async def test_an_empty_scope_reaches_the_retriever_as_no_restriction(loaded):
    ranked = _FakeRetriever([_hit("insurance_act", "art.64.2", 1.0)])
    await statute.search_statute(loaded, "解除契約", None, limit=3, retriever=ranked)
    assert ranked.calls[0][2] == ()


async def test_a_retriever_that_finds_nothing_falls_back_rather_than_answering_empty(loaded):
    # An empty reply reads to the customer as the law having nothing to say about what
    # happened to him. A worse ranking does not.
    rows = await statute.search_statute(
        loaded, "你們憑什麼解除我的契約", ["insurance_act"], limit=4, retriever=_FakeRetriever([])
    )
    assert "art.64.3" in {r["doc_id"] for r in rows}


async def test_a_retriever_naming_a_whole_article_row_is_dropped_not_duplicated(loaded):
    # art.64 is its paragraphs concatenated; returning it beside them is the passage twice.
    ranked = _FakeRetriever([_hit("insurance_act", "art.64", 9.0), _hit("insurance_act", "art.64.2", 8.0)])
    rows = await statute.search_statute(
        loaded, "解除契約", ["insurance_act"], limit=4, siblings=False, retriever=ranked
    )
    assert [r["doc_id"] for r in rows] == ["art.64.2"]


async def test_siblings_still_apply_when_a_retriever_ranked(loaded):
    # The reason siblings exist does not go away because the ranking got better: BM25
    # scores 第64條第2項 on 解除契約 too, and alone it is the company's half.
    ranked = _FakeRetriever([_hit("insurance_act", "art.64.2", 9.0)])
    rows = await statute.search_statute(loaded, "解除契約", ["insurance_act"], limit=1, retriever=ranked)
    assert "art.64.3" in {r["doc_id"] for r in rows}


async def test_terms_include_the_acts_a_complaint_is_made_of(loaded):
    # The half definitions and chapter titles both miss. Nobody types 要保人之據實說明義務;
    # they type 我有據實說明. It appears once in 保險法, so frequency cannot find it either
    # — the modal (應/得/不得/負) is what marks it.
    terms = set(await statute.statute_terms(loaded))
    assert {"據實說明", "解除契約", "終止契約"} <= terms


async def test_terms_include_concepts_only_a_chapter_title_names(loaded):
    # 保險利益 and 複保險 head a 節 and are never introduced by a definition sentence.
    terms = set(await statute.statute_terms(loaded))
    assert {"保險利益", "複保險", "特約條款"} <= terms


def test_a_modal_object_that_starts_mid_phrase_is_not_a_term():
    # 之損害賠償 and 依超過部份 are pieces of a sentence the modal happened to precede, not
    # things anyone does. A dictionary entry nobody types is a tokeniser decision on every
    # query, forever.
    assert not statute._is_term("之損害賠償")
    assert not statute._is_term("依超過部份")
    assert not statute._is_term("予以限制")
    assert not statute._is_term("保險人之")


def test_a_complete_act_is_a_term():
    assert statute._is_term("據實說明")
    assert statute._is_term("解除契約")
    assert statute._is_term("負賠償責任")


async def test_the_modal_pattern_contributes_no_fragments(loaded):
    # Asserted on the modal's output specifically. The full term list legitimately holds
    # 其他財產保險 and 金融消費者, which come from a chapter title and a definition — the
    # rule is about where a term came from, not about the characters it starts with.
    import re

    rows = await loaded.fetch("SELECT verbatim FROM statute_article")
    acted = re.compile(r"(?:不得|應|得|負)([\u4e00-\u9fff]{2,6}?)(?=[，,。；、）]|$)")
    kept = {t for row in rows for t in acted.findall(row["verbatim"]) if statute._is_term(t)}
    assert not [t for t in kept if t.startswith(("之", "依", "予", "其", "該", "前"))]
    assert "據實說明" in kept


async def test_the_statute_corpus_is_reachable_through_the_hybrid_the_server_builds(loaded):
    # The server does not hand `statute_reference` a BM25Retriever; it hands it whatever
    # `HybridRetriever` wraps. Fusion re-scores and re-orders, and a corpus filter dropped
    # anywhere in that stack turns a statute question into a contract answer — so the
    # assertion is made on the object the server actually constructs.
    from policydesk.retrieval.base import CLAUSE, STATUTE, HybridRetriever

    ranked = _FakeRetriever(
        [_hit("insurance_act", "art.64.3", 9.0), _hit("insurance_act", "art.64.2", 8.0)]
    )
    rows = await statute.search_statute(
        loaded, "解除契約", ["insurance_act"], limit=2, siblings=False, retriever=HybridRetriever([ranked])
    )
    assert [r["doc_id"] for r in rows] == ["art.64.3", "art.64.2"]
    assert ranked.calls[0][1] == STATUTE != CLAUSE


async def test_two_channels_disagreeing_still_yield_statute_rows(loaded):
    # One channel is optional by design, so the shape that matters is two of them ranking
    # differently: the fused order must still be provisions this module can read back.
    from policydesk.retrieval.base import HybridRetriever

    lexical = _FakeRetriever([_hit("insurance_act", "art.64.2", 9.0), _hit("insurance_act", "art.68.1", 1.0)])
    semantic = _FakeRetriever([_hit("insurance_act", "art.68.1", 9.0), _hit("insurance_act", "art.64.2", 1.0)])
    rows = await statute.search_statute(
        loaded, "解除契約", ["insurance_act"], limit=4, siblings=False,
        retriever=HybridRetriever([lexical, semantic]),
    )
    assert {r["doc_id"] for r in rows} == {"art.64.2", "art.68.1"}
    assert all(r["verbatim"] for r in rows)


REAL_STATUTES_THIS_DESK_DOES_NOT_HOLD = [
    "依全民健康保險法第41條的規定",
    "依強制汽車責任保險法第27條",
    "就業保險法第11條",
    "依勞工保險條例第53條",
]
"""Real Taiwanese law, none of it in this corpus. The first three end in 保險法, and every
one of their article numbers exists under 保險法 — 第41條 is 再保險人不得向要保人請求交付
保險費, which has nothing to do with 健保."""

PHRASINGS_OF_A_PROVISION_THE_DESK_DOES_HOLD = [
    "依保險法第64條第2項", "另依保險法第64條", "改依保險法施行細則第4條",
    "這在保險法施行細則第14條", "本公司依保險法第116條", "法源是保險法第64條",
    "請參考保險法第116條", "適用保險法第64條", "台灣的保險法第64條",
    "〔保險法 第116條第1項〕", "並依金融消費者保護法第13條", "就是保險法第64條",
]


@pytest.mark.parametrize("text", REAL_STATUTES_THIS_DESK_DOES_NOT_HOLD)
async def test_a_statute_outside_the_corpus_is_reported_however_real_it_is(db, text):
    """
    The resolver took the longest known name a capture ended with.

    Every real statute ending in 保險法 therefore resolved to 保險法 and passed, because
    the article numbers exist there — a customer asking about 健保 could read 依全民健康
    保險法第41條 attached to a provision about reinsurance premiums, under a statute this
    desk does not carry. The boundary is the corpus, and it has to hold against real law
    as well as invented law.
    """
    assert await statute.unresolved(db, text), f"{text} was admitted as a citation this desk can support"


@pytest.mark.parametrize("text", PHRASINGS_OF_A_PROVISION_THE_DESK_DOES_HOLD)
async def test_a_carried_provision_survives_the_prose_in_front_of_it(db, text):
    # The other direction, and the reason the resolver strips at all: `_STATUTE_NAME` has
    # no left boundary, so 另依保險法 is what the pattern captures and it matches no row.
    # Trimming a recognised lead keeps a correct citation; an unrecognised one is reported
    # and repaired, which costs a turn rather than a wrong answer.
    assert not await statute.unresolved(db, text), f"{text} is a real citation and was voided"
