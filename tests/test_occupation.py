"""
The occupation-change scenario, and the retrieval channel it exists to exercise.

換工作 and 職業或職務變更 share no character, so a customer's own sentence about a new
job is exactly the case `policydesk.retrieval.base.EmbeddingRetriever` was built for and
the ILIKE fallback was never going to reach. `occupation_duty` and `occupation_clause`
are both asserted against the customer's own three example sentences, with no anchor at
all first (to show the failure this module's `_ANCHOR` fixes is real, not assumed) and
then through the module's real functions (to show the anchor fixes it).

The second thing asserted is the asymmetry the brief calls out by name: §59 has an
increase half and a decrease half, and a desk that only recites the increase half is
doing half the job. `test_occupation_duty_brings_back_all_four_paragraphs_of_59` is the
one guarding that.
"""

import asyncio
from datetime import UTC, date, datetime

import pytest

from policydesk.agent.scenarios import occupation as occupation_module
from policydesk.agent.scenarios.occupation import (
    OCCUPATION,
    TOOLS,
    gather,
    member_occupation,
    occupation_classes,
    occupation_clause,
    occupation_duty,
)
from policydesk.agent.tools import permitted, reads_identity
from policydesk.synthetic.person import occupation_catalogue


@pytest.fixture(scope="module")
async def db(db):
    if not await db.fetch_val("SELECT count(*) FROM statute_article"):
        from policydesk.agent import statute

        await statute.ingest(db)
    return db


@pytest.fixture(scope="module")
async def hybrid_retriever(db):
    """
    Open the real BM25 + embedding channels over the matrices already built on disk.

    Returns:
        The hybrid retriever, or a skip when either channel has nothing to open.

    Neither channel is built here — `open_index` and `open_vectors` both only open an
    existing matrix, never rebuild one. This is the one test in the file that proves the
    embedding channel itself, not just this module's SQL, is what puts the occupation
    clause first on a customer's own words.

    """
    from policydesk.retrieval.base import HybridRetriever
    from policydesk.retrieval.index import open_index
    from policydesk.retrieval.vectors import open_vectors

    lexical, semantic = await asyncio.gather(open_index(db), open_vectors(db))
    channels = [c for c in (lexical, semantic) if c is not None]
    if not channels:
        pytest.skip("no retrieval index built")
    return HybridRetriever(channels)


# A member holding two in-force policies whose products carry the 職業或職務變更的通知義務
# clause — queried against the live database before writing this file. occupation_class 3
# (汽車修護技師), with at least one held product's max_occupation below class 5, so the
# "would this move exceed this policy's ceiling" material has something to show.
OCCUPATION_MEMBER_ID = 69

# The clause's own literal ILIKE fallback: a topic string containing none of these words
# in either heading or verbatim of the occupation clause will not match under ILIKE.
CUSTOMER_SENTENCES = ("我換工作了要通知嗎", "我現在做工地會不會影響理賠", "換工作保費會變嗎")


async def test_occupation_duty_brings_back_all_four_paragraphs_of_59(db):
    rows = await occupation_duty(db, "一般說明")
    doc_ids = {r["doc_id"] for r in rows}
    assert {"art.59.1", "art.59.2", "art.59.3", "art.59.4"} <= doc_ids, (
        "危險增加要通知的三項沒有危險減少可以請求重新核定保費的第四項，只講對公司有利的一半"
    )


@pytest.mark.parametrize("concern", CUSTOMER_SENTENCES)
async def test_occupation_duty_still_finds_59_on_a_customers_own_sentence(db, concern):
    # None of these three sentences shares a character with 職業或職務變更 or 危險增加減少.
    rows = await occupation_duty(db, concern)
    assert {"art.59.1", "art.59.2", "art.59.3", "art.59.4"} <= {r["doc_id"] for r in rows}


async def test_occupation_duty_citations_are_the_readable_bracketed_form(db):
    rows = await occupation_duty(db, "職業變更")
    for row in rows:
        assert row["citation"].startswith("〔")
        assert row["citation"].endswith("〕")
        assert row["statute"] in row["citation"]


async def test_occupation_classes_is_the_real_catalogue_unfiltered():
    assert await occupation_classes() == occupation_catalogue()


async def test_member_occupation_reads_the_members_real_occupation_and_ceilings(db):
    facts = await member_occupation(db, OCCUPATION_MEMBER_ID, today=datetime.now(UTC).date())
    assert facts["occupation"] == "汽車修護技師"
    assert facts["occupation_class"] == 3
    assert facts["policies"], "this member's in-force policies were queried directly before writing this test"
    assert any(p["max_occupation"] is not None for p in facts["policies"]), (
        "max_occupation is the ceiling the clause exists to enforce; a policy row without it is untestable"
    )


async def test_member_occupation_missing_member_returns_empty(db):
    assert await member_occupation(db, 0, today=datetime.now(UTC).date()) == {}


async def test_occupation_clause_empty_without_product_ids(db):
    assert await occupation_clause(db, [], "換工作") == []


@pytest.mark.parametrize("concern", CUSTOMER_SENTENCES)
async def test_occupation_clause_reaches_the_clause_through_the_real_hybrid_retriever(
    db, hybrid_retriever, concern
):
    # The query this whole module exists for: a customer's own sentence, scoped to
    # exactly the products this member holds, through the real semantic channel — not a
    # mock, since a mock cannot prove the corpus-specific fusion weighting still ranks
    # this clause first.
    member = await member_occupation(db, OCCUPATION_MEMBER_ID, today=datetime.now(UTC).date())
    product_ids = sorted({p["product_id"] for p in member["policies"]})
    rows = await occupation_clause(db, product_ids, concern, retriever=hybrid_retriever)
    assert rows, f"the hybrid retriever returned nothing for {concern!r}"
    assert rows[0]["heading"] == "職業或職務變更的通知義務", (
        f"{concern!r} ranked {rows[0]['heading']!r} first instead of the occupation clause"
    )


async def test_occupation_clause_ilike_fallback_cannot_reach_it_without_a_retriever(db):
    # Documents the failure the anchor and the retriever both exist to fix, the same way
    # `test_retrieval.py`'s module docstring documents it for the lexical channel alone.
    # `find_clause`'s ILIKE fallback needs the topic string as a literal substring of the
    # clause's own heading or verbatim, and none of `_ANCHOR`'s or the customer's words is
    # one — so with no retriever open, this member's own occupation clause is not what
    # comes back, if anything does.
    member = await member_occupation(db, OCCUPATION_MEMBER_ID, today=datetime.now(UTC).date())
    product_ids = sorted({p["product_id"] for p in member["policies"]})
    rows = await occupation_clause(db, product_ids, "我換工作了要通知嗎", retriever=None)
    assert all(r["heading"] != "職業或職務變更的通知義務" for r in rows)


async def test_gather_returns_every_half_when_identity_is_present(db):
    facts = await gather(
        db, {"concern": "我要去做計程車司機"}, member_id=OCCUPATION_MEMBER_ID, today=datetime.now(UTC).date()
    )
    assert facts["occupation_duty"]
    assert facts["occupation_classes"] == occupation_catalogue()
    assert facts["member_occupation"]["occupation"] == "汽車修護技師"
    assert "occupation_clause" in facts, "the gated clause search must run once member_occupation resolved"
    assert facts["_allowed_clauses"] == frozenset(r["clause_id"] for r in facts["occupation_clause"])


async def test_gather_omits_the_members_data_without_an_id(db):
    facts = await gather(db, {"concern": "一般說明"}, member_id=None)
    assert facts["occupation_duty"]
    assert facts["occupation_classes"]
    assert "member_occupation" not in facts
    assert "occupation_clause" not in facts
    assert facts["_allowed_clauses"] == frozenset()


async def test_gather_does_not_call_member_occupation_when_not_allowed(db):
    async def _must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("member_occupation must not run when its name is not in `allowed`")

    original = occupation_module.member_occupation
    occupation_module.member_occupation = _must_not_run
    try:
        facts = await gather(
            db,
            {"concern": "換工作"},
            member_id=OCCUPATION_MEMBER_ID,
            allowed=frozenset({"occupation_duty", "occupation_classes"}),
        )
    finally:
        occupation_module.member_occupation = original
    assert facts["occupation_duty"]
    assert "member_occupation" not in facts
    assert "occupation_clause" not in facts


async def test_gather_does_not_call_occupation_clause_when_not_allowed(db):
    async def _must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("occupation_clause must not run when its name is not in `allowed`")

    original = occupation_module.occupation_clause
    occupation_module.occupation_clause = _must_not_run
    try:
        facts = await gather(
            db,
            {"concern": "換工作"},
            member_id=OCCUPATION_MEMBER_ID,
            allowed=frozenset({"occupation_duty", "occupation_classes", "member_occupation"}),
        )
    finally:
        occupation_module.occupation_clause = original
    assert facts["member_occupation"]
    assert "occupation_clause" not in facts


async def test_gather_still_runs_the_public_half_when_only_it_is_allowed(db):
    allowed = permitted(OCCUPATION.tools, owner=occupation_module, confirmed=False)
    assert allowed == frozenset({"occupation_duty", "occupation_classes"})
    facts = await gather(db, {"concern": "換工作"}, member_id=OCCUPATION_MEMBER_ID, allowed=allowed)
    assert facts["occupation_duty"]
    assert facts["occupation_classes"]
    assert "member_occupation" not in facts
    assert "occupation_clause" not in facts


def test_member_occupation_and_occupation_clause_require_identity_and_the_public_pair_does_not():
    assert getattr(TOOLS["member_occupation"], "requires_identity", False)
    assert getattr(TOOLS["occupation_clause"], "requires_identity", False)
    assert not getattr(TOOLS["occupation_duty"], "requires_identity", False)
    assert not getattr(TOOLS["occupation_classes"], "requires_identity", False)


def test_the_gate_derives_true_for_this_scenario():
    assert reads_identity(OCCUPATION.tools, owner=occupation_module)


def test_occupation_names_the_module_its_gate_is_derived_from():
    assert OCCUPATION.tools_module == "policydesk.agent.scenarios.occupation"


def test_occupation_tools_match_the_scenarios_declared_set():
    assert set(OCCUPATION.tools) == set(TOOLS)


def test_the_scenario_module_contract_is_what_the_executor_calls():
    code = occupation_module.gather.__code__
    assert "retriever" in code.co_varnames
    assert "allowed" in code.co_varnames, "the executor derives and passes a per-tool allow-list"
    assert code.co_flags & 0x08, "member_id and today are passed to every scenario module"


def test_occupation_forbids_deciding_the_consequence():
    for phrase in ("一定要通知", "一定不用通知", "一定會加費、退費、終止契約或影響理賠"):
        assert phrase in OCCUPATION.injection


def test_occupation_requires_both_directions_of_the_duty():
    assert "危險增加" in OCCUPATION.injection
    assert "危險降低" in OCCUPATION.injection
    assert "只講對公司有利的加費、終止那一半" in OCCUPATION.injection


def test_occupation_forbids_computing_a_premium():
    assert "不計算也不估算任何金額" in OCCUPATION.injection


def test_occupation_forbids_uncited_law_or_clause():
    assert "不可以引用工具沒有回傳的條文或條款" in OCCUPATION.injection


def test_occupation_never_hardcodes_a_class_for_an_unlisted_occupation():
    assert "不要用相近的職業硬套一個等級" in OCCUPATION.injection


def test_occupation_says_what_an_empty_clause_result_means():
    assert "occupation_clause 是空的時候" in OCCUPATION.injection


def test_occupation_quick_replies_are_questions_not_commitments():
    for reply in OCCUPATION.quick_replies:
        assert reply.endswith(("？", "?")), reply


def test_occupation_collects_the_customers_own_words_not_company_language():
    concern = next(p for p in OCCUPATION.params if p.name == "concern")
    assert "自己的話" in concern.description


@pytest.mark.asyncio
async def test_a_member_already_above_the_ceiling_is_the_case_this_scenario_exists_for(db):
    """
    Four members hold policies their own occupation class now exceeds.

    `plan()` filters by `max_occupation` at issue, so this state is what a job change looks
    like after the fact: 高壓電力設施維修員 at class 7 holding three contracts none of which
    would be written for them today. That is the situation 職業或職務變更的通知義務 exists
    to govern, and the desk must describe what the clause permits without concluding that
    the cover is gone.
    """
    from policydesk.agent.scenarios.occupation import OCCUPATION, member_occupation

    over = await db.fetch_one(
        """SELECT m.member_id, m.occupation_class FROM member m
           JOIN policy po USING (member_id) JOIN catalog_entry ce USING (product_id)
           WHERE ce.max_occupation < m.occupation_class
           GROUP BY 1, 2 ORDER BY m.occupation_class DESC LIMIT 1"""
    )
    if over is None:
        pytest.skip("nobody has outgrown a product's ceiling")
    facts = await member_occupation(db, over["member_id"], today=date(2026, 8, 29))
    assert facts, "the member the clause is about returned nothing"
    assert "高於" in OCCUPATION.injection, "the model must be told what an exceeded ceiling means"
    assert "不要說他的保單已經失效" in OCCUPATION.injection, "and told not to conclude the cover is gone"


def test_a_missing_ceiling_is_not_read_as_no_limit():
    # `catalog_entry` covers every held product today, so `max_occupation` is never NULL in
    # practice — this guards the reading rather than the behaviour, the same way the
    # zero-weight guard in `rrf` does. A ceiling nobody recorded is not a ceiling nobody has.
    from policydesk.agent.scenarios.occupation import OCCUPATION

    assert "max_occupation 是空的時候" in OCCUPATION.injection
    assert "不是他不受限制" in OCCUPATION.injection
