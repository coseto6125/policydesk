"""
The desk remembers what it was just told.

Every turn was already written to `conversation_message` and none was ever read, so a
customer who said 想加保壽險 and then 一年約兩萬內 was asked, one turn later, which line
of cover they wanted and whether their budget was around twenty thousand. The router
was answering each message as if it were the first.
"""

from datetime import date
from unittest.mock import AsyncMock

import pytest

from policydesk.agent.executor import _as_budget
from policydesk.agent.memory import transcript
from policydesk.agent.tools import LINES
from policydesk.synthetic.person import insurance_age


async def test_sweep_once_passes_facts_phase_to_provider_and_usage(monkeypatch):
    from policydesk.agent import memory
    from policydesk.llm.provider import Completion, Phase

    monkeypatch.setattr(memory, "_claim", AsyncMock(return_value=[
        {"case_id": 1, "member_id": 2, "summary": ""},
    ]))
    monkeypatch.setattr(memory, "_write", AsyncMock())
    db = AsyncMock()
    db.fetch.side_effect = [[{"message_id": 3, "speaker": "customer", "text": "謝謝"}], []]
    provider = AsyncMock()
    provider.complete.return_value = Completion(text='{"facts":[],"summary":""}', model="test")
    assert await memory.sweep_once(db, provider) == 1
    assert provider.complete.call_args.kwargs["phase"] is Phase.FACTS
    sql, params = db.execute.call_args.args
    assert "INSERT INTO llm_usage" in sql
    assert "'facts'" not in sql
    assert params[1] == Phase.FACTS.value


def test_transcript_is_empty_before_anything_is_said():
    assert transcript([]) == ""


def test_transcript_labels_both_sides_in_order():
    block = transcript([
        {"speaker": "customer", "text": "想加保壽險"},
        {"speaker": "agent", "text": "請問預算？"},
        {"speaker": "customer", "text": "一年兩萬內"},
    ])
    assert block.index("想加保壽險") < block.index("請問預算？") < block.index("一年兩萬內")
    assert "保戶：想加保壽險" in block
    assert "櫃台：請問預算？" in block


def test_the_transcript_is_labelled_so_the_router_can_tell_it_from_this_turn():
    assert "# 先前對話" in transcript([{"speaker": "customer", "text": "x"}])


@pytest.mark.parametrize(("raw", "expected"), [("20000", 20000), (" 20,000 ", 20000), ("0", 0)])
def test_budget_reads_a_plain_number(raw: str, expected: int):
    assert _as_budget(raw) == expected


@pytest.mark.parametrize("raw", ["", "兩萬", "about 20k", "20000元"])
def test_an_unreadable_budget_makes_no_recommendation(raw: str):
    """
    Falling back to a default is how a customer is shown a product they cannot afford.

    The gather step skips suitable_products entirely on None, so the reply says the
    figure is still needed rather than quoting a plan chosen against a number nobody
    gave.
    """
    assert _as_budget(raw) is None


@pytest.mark.parametrize(("raw", "warns"), [("", False), ("  ", False), ("兩萬", True), ("20000元", True)])
def test_only_a_budget_that_was_given_can_be_unreadable(raw: str, warns: bool, monkeypatch):
    """
    An absent budget is not a parse failure, and the log should not say it is.

    想買壽險 carries no figure, so the router hands back an empty string on the first
    turn of every recommendation. Warning on it filed `budget_unreadable` for a customer
    who simply had not been asked yet — three of them in one replay of the real
    transcript — and an operator reading that log sees a broken extractor.
    """
    from policydesk.agent import executor

    called: list[str] = []
    monkeypatch.setattr(executor.logger, "warning", lambda event, **kw: called.append(event))
    assert _as_budget(raw) is None
    assert bool(called) is warns, f"{raw!r}: warned={called}"


def test_the_router_returns_what_it_collected():
    """The arguments were being discarded, so gather ran on hardcoded defaults."""
    from pathlib import Path

    source = Path("src/policydesk/agent/executor.py").read_text()
    body = source[source.index("async def _route"):source.index("def _as_budget")]
    assert "tuple[Scenario | None, dict[str, str]]" in body
    assert 'call.get("arguments")' in body


def test_gather_selects_the_line_the_customer_asked_for():
    """
    Pinning the query to health meant 88 life products on sale could not be reached.

    A customer asking about 壽險 was matched against health products and told none fit.
    """
    from pathlib import Path

    source = Path("src/policydesk/agent/tools.py").read_text()
    body = source[source.index("async def suitable_products"):source.index("async def required_documents")]
    assert "p.line = $5::text" in body
    assert "p.line = 'health'" not in body


def test_the_sellable_lines_exclude_the_unclassified_bucket():
    """`other` is what the catalogue could not classify, so it is never offered."""
    assert "other" not in LINES
    assert {"health", "life", "accident", "annuity", "investment"} == set(LINES)


def test_recommend_collects_a_line_as_well_as_a_need():
    from policydesk.agent.scenario import RECOMMEND

    names = {p.name for p in RECOMMEND.params}
    assert names == {"need", "line", "budget"}


def test_insurance_age_adds_a_year_past_six_months():
    """以足歲計算，但未滿一歲的零數超過六個月者，加算一歲."""
    born = date(1990, 1, 15)
    assert insurance_age(born, date(2026, 1, 15)) == 36  # on the birthday
    assert insurance_age(born, date(2026, 7, 15)) == 36  # exactly six months, not 超過
    assert insurance_age(born, date(2026, 7, 16)) == 37  # one day past, so 加算一歲
    assert insurance_age(born, date(2026, 6, 30)) == 36


def test_insurance_age_measures_six_months_by_date_not_by_month_number():
    """
    Subtracting month numbers compared 7 to 6, so the whole seventh month read as six.

    Anyone whose half-year date fell inside that window was underwritten a year young,
    which moves both the issue-age band and the premium.
    """
    born = date(1990, 3, 31)
    assert insurance_age(born, date(2026, 9, 30)) == 36  # the half-year date itself
    assert insurance_age(born, date(2026, 10, 1)) == 37


def test_insurance_age_is_not_a_year_subtraction():
    """The desk was doing today.year - birth.year, wrong before the birthday and wrong after."""
    assert insurance_age(date(1990, 12, 31), date(2026, 1, 1)) == 35


def test_the_collected_parameters_reach_the_screen():
    """
    A demo of memory that shows no evidence of remembering proves nothing.

    The chat prints what the router carried over, so a reader can see the desk answered
    from the whole conversation and not from the last sentence.
    """
    from pathlib import Path

    assert '"params": turn.params' in Path("src/policydesk/web/server.py").read_text()
    assert "取得 ${carried}" in Path("src/policydesk/web/static/index.html").read_text()


def test_a_portfolio_is_dated_from_the_issue_age_band_not_filtered_by_it():
    """
    Filtering on today's age emptied the book of anyone past the ceiling.

    An 82-year-old got 尚無保單 whatever preset the operator picked, with a log line and
    nothing on screen — because no health product issues at 77. What has to hold is that
    they were inside the band on the day they bought it, so the band picks the effective
    date rather than excluding the product.
    """
    from pathlib import Path

    source = Path("src/policydesk/synthetic/portfolio.py").read_text()
    body = source[source.index("async def plan("):source.index("async def enrol(")]
    assert "ce.issue_age_min <= $1::int" in body
    assert "BETWEEN ce.issue_age_min AND ce.issue_age_max" not in body
    assert "bought_at = rng.randint(" in body


def test_a_policy_inside_its_waiting_period_needs_a_product_that_issues_today():
    """It was bought this month, so its owner has to be inside the band now."""
    from pathlib import Path

    source = Path("src/policydesk/synthetic/portfolio.py").read_text()
    body = source[source.index("async def plan("):source.index("async def enrol(")]
    waiting = body[body.index('if holding.situation == "waiting":'):]
    assert 'p["issue_age_min"] <= age <= p["issue_age_max"]' in waiting[:400]


def test_enrol_reports_what_it_actually_wrote():
    """An empty 保單清單 with no explanation reads as a bug, because sometimes it is one."""
    from pathlib import Path

    source = Path("src/policydesk/synthetic/portfolio.py").read_text()
    assert ") -> tuple[int, int]:" in source
    assert "return member_id, len(policies)" in source
    page = Path("src/policydesk/web/static/index.html").read_text()
    assert "已為您帶入既有保單" in page
    assert "所選組合中的商品目前無法投保" in page


@pytest.fixture(scope="module")
async def db():
    from policydesk.core.db import Database

    pool = Database()
    try:
        await pool.fetch_val("SELECT 1")
    except Exception:
        pytest.skip("policydesk-pg is not up")
    return pool


@pytest.mark.asyncio
async def test_a_fact_whose_source_is_gone_is_not_quoted_back_as_something_he_said(db):
    """
    `source_message_id` is `ON DELETE SET NULL`, and the card never loaded it.

    So a fact nobody can trace reached the model beside one quoted from a message still on
    file, under one instruction covering both: 直接沿用，不要重問. The migration's own
    comment says that column "is what makes the difference checkable", and nothing checked
    it — the column changed no behaviour at all.

    Marked, not dropped. 已離婚 does not stop being true when a case is closed and its
    messages age out; what changes is whether the desk may say 您上次提到 about it.
    """
    from policydesk.agent import memory

    member_id = await db.fetch_val("SELECT member_id FROM member ORDER BY member_id DESC LIMIT 1")
    case_id = await db.fetch_val(
        """INSERT INTO "case" (member_id, kind, stage) VALUES ($1::bigint,'service','inquiry')
           RETURNING case_id""", [member_id])
    kept = await db.fetch(
        "SELECT key, value, category, source_message_id FROM member_fact WHERE member_id = $1::bigint",
        [member_id])
    try:
        await db.execute("DELETE FROM member_fact WHERE member_id = $1::bigint", [member_id])
        message_id = await db.fetch_val(
            """INSERT INTO conversation_message (case_id, speaker, text)
               VALUES ($1::bigint,'customer','我的預算是每年三萬元') RETURNING message_id""", [case_id])
        await db.execute_many(
            """INSERT INTO member_fact (member_id, key, value, category, source_message_id)
               VALUES ($1::bigint,$2::text,$3::text,$4::text,$5::bigint)""",
            [
                [member_id, "預算", "每年三萬元", "cons", message_id],
                [member_id, "婚姻狀態", "已離婚", "hist", None],
            ],
        )
        card = await memory.card(db, member_id=int(member_id), case_id=int(case_id))
        assert "每年三萬元" in card
        assert "已離婚" in card
        # The traceable one keeps the plain instruction; the other is quarantined under its
        # own heading with the caveat stated once, not stapled to every line.
        heading = "以下這幾項的原始對話已經不在了"
        assert heading in card
        assert card.index("每年三萬元") < card.index(heading) < card.index("已離婚")
        assert card.count("您上次提到") == 1
    finally:
        await db.execute("DELETE FROM member_fact WHERE member_id = $1::bigint", [member_id])
        if kept:
            await db.execute_many(
                """INSERT INTO member_fact (member_id, key, value, category, source_message_id)
                   VALUES ($1::bigint,$2::text,$3::text,$4::text,$5::bigint)""",
                [[member_id, r["key"], r["value"], r["category"], r["source_message_id"]] for r in kept])
        await db.execute('DELETE FROM "case" WHERE case_id = $1::bigint', [case_id])


@pytest.mark.asyncio
async def test_a_card_of_only_traceable_facts_carries_no_warning(db):
    # The other direction: the quarantine block must not appear when nothing is in it, or
    # every card grows a section about a problem it does not have.
    from policydesk.agent import memory

    member_id = await db.fetch_val("SELECT member_id FROM member ORDER BY member_id LIMIT 1")
    case_id = await db.fetch_val(
        """INSERT INTO "case" (member_id, kind, stage) VALUES ($1::bigint,'service','inquiry')
           RETURNING case_id""", [member_id])
    kept = await db.fetch(
        "SELECT key, value, category, source_message_id FROM member_fact WHERE member_id = $1::bigint",
        [member_id])
    try:
        await db.execute("DELETE FROM member_fact WHERE member_id = $1::bigint", [member_id])
        message_id = await db.fetch_val(
            """INSERT INTO conversation_message (case_id, speaker, text)
               VALUES ($1::bigint,'customer','我一年只能付兩萬') RETURNING message_id""", [case_id])
        await db.execute(
            """INSERT INTO member_fact (member_id, key, value, category, source_message_id)
               VALUES ($1::bigint,'預算','每年兩萬元','cons',$2::bigint)""", [member_id, message_id])
        card = await memory.card(db, member_id=int(member_id), case_id=int(case_id))
        assert "每年兩萬元" in card
        assert "原始對話已經不在了" not in card
    finally:
        await db.execute("DELETE FROM member_fact WHERE member_id = $1::bigint", [member_id])
        if kept:
            await db.execute_many(
                """INSERT INTO member_fact (member_id, key, value, category, source_message_id)
                   VALUES ($1::bigint,$2::text,$3::text,$4::text,$5::bigint)""",
                [[member_id, r["key"], r["value"], r["category"], r["source_message_id"]] for r in kept])
        await db.execute('DELETE FROM "case" WHERE case_id = $1::bigint', [case_id])
