"""
The 保單健檢 scenario: what counts as a gap, and what this scenario must never say.

Three properties matter more than the rest. First, a rider whose own `lapsed_at` is
untouched still stops paying the moment its main policy does — `_effectively_covered` is
tested against plain dicts because that property has nothing to do with the database, and
a unit test that needed one would be testing psqlpy instead of the rule. Second, this
scenario must never turn into a sales pitch; the injection is asserted to forbid one.
Third, the category vocabulary is read out of a grant clause's own heading by a regex,
and a regex that drifts widens every customer's gap list without anyone noticing — so
`_categories_in` is tested both against hand-picked fragments and against the live
corpus, asking for the exact names an earlier full sweep over 2,521 grant headings found.
"""

import pytest

from policydesk.agent import tools
from policydesk.agent.scenarios import review as review_module
from policydesk.agent.scenarios.review import (
    CATALOG_FLOOR,
    REVIEW,
    TOOLS,
    _categories_in,
    _effectively_covered,
    category_catalog,
    gather,
    held_categories,
)
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


def _policy(**over: object) -> dict[str, object]:
    """Build a minimal `list_policies` row, with sane defaults filled in for a main policy."""
    row = {"policy_number": "P1", "main_policy_number": None, "is_lapsed": False}
    row.update(over)
    return row


def test_effectively_covered_a_main_policy_reads_its_own_lapse_only():
    active = _policy(policy_number="MAIN", is_lapsed=False)
    assert _effectively_covered(active, {"MAIN": active})


def test_effectively_covered_a_lapsed_policy_is_not_covered():
    lapsed = _policy(policy_number="MAIN", is_lapsed=True)
    assert not _effectively_covered(lapsed, {"MAIN": lapsed})


def test_effectively_covered_a_rider_follows_its_lapsed_main_even_when_itself_is_not_lapsed():
    # The property the whole scenario exists to catch: a rider's own row says nothing
    # stopped, but its main did, and the rider's cover went with it.
    main = _policy(policy_number="MAIN", is_lapsed=True)
    rider = _policy(policy_number="RIDER", main_policy_number="MAIN", is_lapsed=False)
    assert not _effectively_covered(rider, {"MAIN": main, "RIDER": rider})


def test_effectively_covered_a_rider_is_covered_while_its_main_still_is():
    main = _policy(policy_number="MAIN", is_lapsed=False)
    rider = _policy(policy_number="RIDER", main_policy_number="MAIN", is_lapsed=False)
    assert _effectively_covered(rider, {"MAIN": main, "RIDER": rider})


def test_categories_in_reads_two_categories_out_of_one_heading():
    assert _categories_in("身故保險金或喪葬費用保險金的申領") == {"身故", "喪葬費用"}


def test_categories_in_reads_a_category_at_the_headings_own_start():
    assert _categories_in("完全失能保險金的給付及限制") == {"完全失能"}


def test_categories_in_drops_the_fragments_a_naive_regex_produced():
    # Each of these came back from an unfiltered `([一-龥]{2,8})保險金` sweep over the
    # real corpus — a sentence fragment about *how* a benefit is paid, not its name.
    assert _categories_in("保險事故的通知與保險金的申請時間") == set()
    assert _categories_in("有下列情形之一者，本公司不負給付保險金的責任：") == set()
    assert _categories_in("分期定期保險金給付") == set()
    assert _categories_in("減少基本保險金額") == set()
    assert _categories_in("被保險人於本契約有效期間內身故者，本公司按保險金額給付身故保") == set()


async def test_category_catalog_matches_a_full_sweep_of_the_live_corpus(db):
    # The exact names an earlier sweep over all 2,521 grant headings in this corpus
    # produced, cross-checked against `benefit`'s own 6 categories which are a strict
    # subset of what a real contract actually grants. A regression in the anchoring
    # either loses a real name here or lets a fragment back in — either way, loudly.
    rows = await category_catalog(db)
    names = {r["name"] for r in rows}
    for real in ("身故", "完全失能", "祝壽", "滿期", "住院日額", "住院手術醫療", "門診手術醫療"):
        assert real in names, f"{real} must survive a live sweep of the corpus"
    for fragment in ("給付", "通知", "公司", "減少", "分期"):
        assert not any(fragment in name for name in names), f"{fragment} must never appear inside a category name"


async def test_held_categories_returns_empty_for_no_products(db):
    assert await held_categories(db, []) == []


async def test_gather_names_a_category_no_policy_covers(monkeypatch):
    # A member with one narrow product cannot hold every category the catalog
    # recognises, so at least one gap must come back — against a stub database that
    # needs no live corpus, only the shape `gather` reads.
    class StubDB:
        async def fetch(self, sql: str, params: list[object] | None = None) -> list[dict[str, object]]:
            if "FROM policy" in sql:
                return [
                    {
                        "policy_id": 1,
                        "policy_number": "A1",
                        "sum_insured": 100000,
                        "effective_at": None,
                        "lapsed_at": None,
                        "main_policy_number": None,
                        "product_name": "測試商品",
                        "product_id": "P1",
                        "attachment": "main",
                        "days_in_force": 10,
                        "is_lapsed": False,
                    }
                ]
            if "FROM clause c JOIN product p" in sql:
                if params and params[0]:
                    # Scoped to the member's own covered products.
                    return [{"product_id": "P1", "product_name": "測試商品", "heading": "住院日額保險金的申領"}]
                # Corpus-wide: one category this member holds, one he does not.
                return [
                    {"product_id": "P1", "product_name": "測試商品", "heading": "住院日額保險金的申領"},
                    {"product_id": "P2", "product_name": "別的商品", "heading": "重大疾病保險金的給付"},
                ]
            raise AssertionError(sql)

    # Two products in the stub, so the real floor of ten would filter the whole catalogue
    # away and the gap this test is about would vanish for a reason that is not the gap.
    monkeypatch.setattr(review_module, "CATALOG_FLOOR", 1)
    facts = await gather(StubDB(), {}, member_id=1)
    assert facts["policies"][0]["effectively_covered"] is True
    assert {b["name"] for b in facts["held_benefits"]} == {"住院日額"}
    assert [g["name"] for g in facts["gaps"]] == ["重大疾病"]


async def test_gather_marks_a_rider_uncovered_when_its_main_has_lapsed():
    class StubDB:
        async def fetch(self, sql: str, params: list[object] | None = None) -> list[dict[str, object]]:
            if "FROM policy" in sql:
                return [
                    {
                        "policy_id": 1,
                        "policy_number": "MAIN",
                        "sum_insured": 100000,
                        "effective_at": None,
                        "lapsed_at": "2026-01-01",
                        "main_policy_number": None,
                        "product_name": "主約",
                        "product_id": "MP",
                        "attachment": "main",
                        "days_in_force": 400,
                        "is_lapsed": True,
                    },
                    {
                        "policy_id": 2,
                        "policy_number": "RIDER",
                        "sum_insured": 50000,
                        "effective_at": None,
                        "lapsed_at": None,
                        "main_policy_number": "MAIN",
                        "product_name": "附約",
                        "product_id": "RP",
                        "attachment": "rider",
                        "days_in_force": 400,
                        "is_lapsed": False,
                    },
                ]
            if "FROM clause c JOIN product p" in sql:
                # Nothing is effectively covered, so `held_categories` never reaches
                # this stub at all; only `category_catalog`'s corpus-wide call does.
                assert not (params and params[0])
                return [{"product_id": "MP", "product_name": "主約", "heading": "住院日額保險金的申領"}]
            raise AssertionError(sql)

    facts = await gather(StubDB(), {}, member_id=1)
    rider = next(p for p in facts["policies"] if p["policy_number"] == "RIDER")
    assert rider["is_lapsed"] is False, "the rider's own row never changed"
    assert rider["effectively_covered"] is False, "but its main did, and the rider's cover went with it"


async def test_gather_with_only_the_public_tool_allowed_never_queries_the_members_book(monkeypatch):
    # Proves the query never ran, not that its output was dropped afterwards: `fetch`
    # raises for a `FROM policy` statement, so a call to `list_policies` fails loudly
    # instead of silently returning a row this test would then have to notice and drop.
    class StubDB:
        async def fetch(self, sql: str, params: list[object] | None = None) -> list[dict[str, object]]:
            if "FROM policy" in sql:
                raise AssertionError("list_policies must not run when it is not in `allowed`")
            if "FROM clause c JOIN product p" in sql:
                return [{"product_id": "P1", "product_name": "測試商品", "heading": "住院日額保險金的申領"}]
            raise AssertionError(sql)

    monkeypatch.setattr(review_module, "CATALOG_FLOOR", 1)
    facts = await gather(StubDB(), {}, member_id=1, allowed=frozenset({"category_catalog"}))
    assert facts == {"category_catalog": [{"name": "住院日額", "product_count": 1}]}
    assert "policies" not in facts
    assert "held_benefits" not in facts
    assert "gaps" not in facts


async def test_gather_with_held_categories_not_allowed_keeps_policies_but_drops_the_gap_pair(monkeypatch):
    # `list_policies` and `held_categories` are meant to move together, but the contract
    # is per tool — this proves the intermediate split does not fabricate a gap list from
    # an empty `held`, which would tell a verified customer he holds nothing at all.
    class StubDB:
        async def fetch(self, sql: str, params: list[object] | None = None) -> list[dict[str, object]]:
            if "FROM policy" in sql:
                return [
                    {
                        "policy_id": 1,
                        "policy_number": "A1",
                        "sum_insured": 100000,
                        "effective_at": None,
                        "lapsed_at": None,
                        "main_policy_number": None,
                        "product_name": "測試商品",
                        "product_id": "P1",
                        "attachment": "main",
                        "days_in_force": 10,
                        "is_lapsed": False,
                    }
                ]
            if "FROM clause c JOIN product p" in sql:
                if params and params[0]:
                    raise AssertionError("held_categories must not run when it is not in `allowed`")
                return [{"product_id": "P1", "product_name": "測試商品", "heading": "住院日額保險金的申領"}]
            raise AssertionError(sql)

    monkeypatch.setattr(review_module, "CATALOG_FLOOR", 1)
    facts = await gather(StubDB(), {}, member_id=1, allowed=frozenset({"category_catalog", "list_policies"}))
    assert facts["policies"]
    assert "held_benefits" not in facts
    assert "gaps" not in facts


def test_review_tools_are_gated_through_the_module_it_names():
    assert REVIEW.tools_module == "policydesk.agent.scenarios.review"
    from importlib import import_module

    owner = import_module(REVIEW.tools_module)
    assert tools.reads_identity(REVIEW.tools, owner=owner)
    assert tools.reads_identity(REVIEW.tools), "unresolved without the module, so it must read as gated"


def test_review_tools_dict_covers_every_name_the_scenario_lists():
    assert set(REVIEW.tools) <= set(TOOLS)


def test_review_reuses_list_policies_rather_than_re_querying_it():
    assert TOOLS["list_policies"] is tools.list_policies


def test_review_forbids_recommending_a_product():
    assert "不建議該買哪一張商品" in REVIEW.injection
    assert "不推銷" in REVIEW.injection


def test_review_forbids_a_bare_underinsured_verdict():
    assert "不要只講「保障不足」這種空話" in REVIEW.injection


def test_review_forbids_claims_decisions():
    assert "不可以判斷賠不賠" in REVIEW.injection


def test_review_checks_a_riders_own_main_before_calling_it_covered():
    assert "主約停效，附約的效力就跟著停止" in REVIEW.injection


def test_review_offers_recommend_only_as_a_transition_not_a_step():
    assert "recommend" in REVIEW.transitions


def test_review_quick_replies_are_questions_not_commitments():
    for reply in REVIEW.quick_replies:
        assert "我要買" not in reply
        assert "我想了解" in reply or "確認" in reply or "是什麼" in reply


def test_review_handles_the_unverified_case_without_guessing():
    assert "_identity_required" in REVIEW.injection
    assert "不要憑空講" in REVIEW.injection


def test_the_scenario_module_contract_is_what_the_executor_calls():
    # Read off the code object rather than `inspect.signature`, which evaluates the
    # annotations — `Database` is only imported under TYPE_CHECKING, and evaluating it
    # raises NameError for a reason unrelated to the contract this test checks.
    from policydesk.agent.scenarios import review

    code = review.gather.__code__
    assert "member_id" in code.co_varnames
    assert "today" in code.co_varnames
    assert "retriever" in code.co_varnames
    assert code.co_flags & 0x08, "the executor always calls with retriever= as a keyword"


async def test_the_catalog_floor_keeps_the_categories_a_customer_would_recognise(db):
    # Without a floor the corpus yields 107 categories, 47 of them on a single product, and
    # every customer's gap list becomes ninety lines of things nobody sells them. The floor
    # is on product count rather than a keep-list, so this asserts the shape it produces
    # rather than the exact membership a reingest would change.
    catalog = await category_catalog(db)
    names = [c["name"] for c in catalog]
    assert len(catalog) < 30, f"a gap list this long is a wall, not a finding: {len(catalog)}"
    assert {"身故", "完全失能", "住院醫療"} <= set(names)
    assert all(c["product_count"] >= CATALOG_FLOOR for c in catalog)
    # Commonest first, so a model reading the list top-down reads the categories that
    # matter to the most people first.
    counts = [c["product_count"] for c in catalog]
    assert counts == sorted(counts, reverse=True)


async def test_a_lower_floor_admits_the_long_tail(db):
    # The floor is the whole mechanism, so prove it is doing something rather than
    # coinciding with the corpus.
    assert len(await category_catalog(db, floor=1)) > 3 * len(await category_catalog(db))


@pytest.mark.asyncio
async def test_no_category_is_both_held_and_missing(db):
    """
    The gap list was the catalogue minus the held names, compared as strings.

    Neither side is a controlled vocabulary — both are extracted from clause headings —
    and `category_catalog` applies `CATALOG_FLOOR` where `held_categories` does not, so
    one contract's 特定處置費用 never cancelled the catalogue's 特定處置. A live reply
    told one customer 門診手術費用：由新實全心意PLUS附約提供 and, eleven lines later,
    門診手術醫療：您名下沒有任何一張有效保單涵蓋. Three categories did that in one reply.

    A customer who cannot tell whether outpatient surgery is covered is worse off than
    one who was told the wrong thing: the wrong thing can at least be checked.
    """
    from policydesk.agent.scenarios.review import _stem, gather

    member_id = await db.fetch_val(
        """SELECT member_id FROM policy WHERE lapsed_at IS NULL
           GROUP BY member_id ORDER BY count(*) DESC LIMIT 1""")
    if member_id is None:
        pytest.skip("no member holds a policy")
    facts = await gather(db, {}, member_id=int(member_id))
    if not facts.get("gaps") or not facts.get("held_benefits"):
        pytest.skip("this member has no gaps or no recognised categories")

    held = {_stem(b["name"]) for b in facts["held_benefits"]}
    contradictory = [c["name"] for c in facts["gaps"] if _stem(c["name"]) in held]
    assert not contradictory, f"listed as covered and as missing in one reply: {contradictory}"


def test_a_name_that_is_only_a_modifier_is_not_a_category():
    # 醫療 alone names no event. It was reported as a gap to a customer holding 住院醫療
    # and 門診手術費用, which reads as a denial of the cover they were shown two lines up.
    from policydesk.agent.scenarios.review import _stem

    assert _stem("醫療") == ""
    assert _stem("特定處置費用") == "特定處置"
    assert _stem("住院醫療") == "住院"
    assert _stem("各項癌症") == "各項癌症"
