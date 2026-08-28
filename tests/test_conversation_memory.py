"""
The desk remembers what it was just told.

Every turn was already written to `conversation_message` and none was ever read, so a
customer who said 想加保壽險 and then 一年約兩萬內 was asked, one turn later, which line
of cover they wanted and whether their budget was around twenty thousand. The router
was answering each message as if it were the first.
"""

from datetime import date

import pytest

from policydesk.agent.executor import _as_budget
from policydesk.agent.memory import transcript
from policydesk.agent.tools import LINES
from policydesk.synthetic.person import insurance_age


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
