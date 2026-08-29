"""
Guards for the seven-tab console.

The reads themselves are checked against a live database by driving the page; what these
hold are the properties a passing query would not reveal. Three of them are the ones a
console gets wrong quietly: it starts writing, it drops the rows whose grouping key is
NULL, and it serves someone else's national ID to whoever knows the URL.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from msgspec import json

from policydesk.web import console as mod

SOURCE = Path("src/policydesk/web/console.py").read_text()
PAGE = Path("src/policydesk/web/static/index.html").read_text()


class _Args:
    """Sanic hands query parameters back through `.get`, which is all the handlers use."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, key: str, default=None):
        return self._values.get(key, default)


class _FakeDB:
    """Records the SQL it was asked for and answers with rows the caller supplied."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.queries: list[str] = []

    async def fetch(self, sql: str, params=None) -> list[dict]:
        self.queries.append(sql)
        return [dict(row) for row in self.rows]

    async def fetch_one(self, sql: str, params=None) -> dict | None:
        self.queries.append(sql)
        return dict(self.rows[0]) if self.rows else None

    async def fetch_val(self, sql: str, params=None):
        self.queries.append(sql)


class _Request:
    def __init__(self, args: dict[str, str] | None = None, db: _FakeDB | None = None) -> None:
        self.args = _Args(args or {})
        self.app = type("App", (), {"ctx": type("Ctx", (), {"db": db})()})()
        self.path = "/api/console/test"
        self.ip = "127.0.0.1"


def _decode(resp):
    """Read a handler's JSON body back."""
    return json.decode(resp.body)


# ---------------------------------------------------------------- registration

def test_the_blueprint_is_named_and_prefixed_so_one_line_registers_it():
    """server.py registers this by name; a different prefix silently 404s every tab."""
    assert mod.console.name == "console"
    assert mod.console.url_prefix == "/api/console"


def test_the_console_serves_every_tab_that_needs_a_read():
    """
    Six tabs fetch; 總覽 rides the socket it already had.

    Read off `_future_routes`, not `routes`: a blueprint holds its routes as futures until
    an app registers it, and `routes` is empty until then — which is exactly the state a
    test importing the module sees.
    """
    paths = {route.uri for route in mod.console._future_routes}
    for wanted in ("/inbox", "/transcript", "/profile", "/llm", "/live", "/scenarios", "/dashboard"):
        assert wanted in paths, f"{wanted} is not registered"


def test_nothing_in_the_console_writes():
    """
    `core.commands` is the only writer. A console that could move a case would be a
    second implementation of the stage rules, and the audit trail would stop answering
    "who moved this".
    """
    for statement in ("INSERT ", "UPDATE ", "DELETE ", "ALTER ", "DROP "):
        assert statement not in SOURCE, f"the console issues {statement.strip()}"


# ---------------------------------------------------------------- the guard

async def test_a_console_read_without_the_desk_token_is_refused():
    """
    The inbox names every customer and the trace carries the prompt they appear in. The
    desk socket refuses an untokened connection; these are the same rows over HTTP.
    """
    refusal = await mod._require_desk_token(_Request({"token": "not-the-token"}))
    assert refusal is not None
    assert refusal.status == 403


async def test_a_console_read_carrying_the_desk_token_is_let_through():
    from policydesk.web.server import DESK_TOKEN

    assert await mod._require_desk_token(_Request({"token": DESK_TOKEN})) is None


@pytest.mark.parametrize("handler", [mod.profile, mod.transcript])
async def test_a_member_scoped_read_refuses_rather_than_falling_back_to_everyone(handler):
    """
    An unscoped read here is every customer's record handed to whoever dropped the
    parameter. The socket refuses the same thing rather than defaulting.
    """
    refused = await handler(_Request({"token": "x"}, db=_FakeDB()))
    assert refused.status == 400


# ---------------------------------------------------------------- the pipeline

def _turn(turn_id: str, phase: str, age_s: float) -> dict:
    return {
        "turn_id": turn_id, "case_id": 1, "display_name": "皓榕", "scenario": "explain_cover",
        "phases": ["route", phase], "phase": phase, "calls": 2, "total_tokens": 100,
        "latency_ms": 900, "last_at": datetime.now(UTC) - timedelta(seconds=age_s), "age_s": age_s,
    }


async def test_a_turn_still_writing_sits_on_the_phase_it_reached():
    db = _FakeDB([_turn("t-1", "answer", age_s=3)])
    body = _decode(await mod.live(_Request({}, db=db)))
    assert body["turns"][0]["node"] == "answer"
    assert body["turns"][0]["settled"] is False


async def test_a_turn_quiet_past_the_idle_gap_parks_on_the_terminal_node():
    """
    A turn is only visible through the rows it has already written, so "finished" cannot
    be read off the table. Past the gap the reply has gone out, and a card left on
    `answer` would claim a model call is still running an hour later.
    """
    db = _FakeDB([_turn("t-2", "answer", age_s=mod.LIVE_IDLE_S + 1)])
    body = _decode(await mod.live(_Request({}, db=db)))
    assert body["turns"][0]["node"] == "response"
    assert body["turns"][0]["settled"] is True


def test_the_terminal_node_is_not_a_phase_the_table_can_hold():
    """
    `response` closes the diagram; a phase column that emitted it would be inventing
    a row `llm_usage` never writes.
    """
    assert mod.PIPELINE[-1] == "response"
    assert "'response'" not in SOURCE.split("PIPELINE")[1].split("\n\n")[0].replace("PIPELINE", "")


# ---------------------------------------------------------------- the queries

def test_the_scenario_rollup_keeps_the_calls_that_belong_to_no_scenario():
    """
    `scenario` is NULL for `phase='route'` by design — routing is the call that chooses
    the scenario — and the offline facts sweep lands there too. Those rows are most of
    the token spend, so filtering them out under-counts the bill by more than any single
    scenario costs.
    """
    rollup = SOURCE[SOURCE.index("async def scenarios"):SOURCE.index("async def dashboard")]
    assert "GROUP BY u.scenario" in rollup
    assert "scenario IS NOT NULL" not in rollup
    assert "WHERE" not in rollup.split('"""SELECT')[1].split('"""')[0]


def test_the_page_names_the_null_bucket_rather_than_leaving_it_blank():
    """An unlabelled row at the top of a spend table reads as a rendering fault."""
    assert "（路由，未歸屬情境）" in PAGE


def test_the_fortnight_is_drawn_from_a_date_series_not_from_the_rows():
    """
    A day with no traffic has to be a zero on the axis. Drawn from the rows alone, a
    fortnight with a gap in it renders as a shorter, busier fortnight.
    """
    assert "generate_series" in SOURCE
    assert "interval '13 days'" in SOURCE


def test_no_query_formats_a_value_into_sql():
    """Every parameter is bound. One query that interpolates teaches the next one to."""
    for line in SOURCE.splitlines():
        stripped = line.strip()
        if stripped.startswith(('"""SELECT', "SELECT ", "FROM ", "WHERE ", "AND ", "GROUP BY", "ORDER BY")):
            assert "{" not in stripped, f"interpolated SQL: {stripped}"
    assert 'f"""SELECT' not in SOURCE


def test_the_optional_filters_bind_null_rather_than_switching_statement():
    """
    One statement serves the filtered and the unfiltered read, so the query cache
    holds one plan and neither variant can drift from the other.
    """
    assert "($1::bigint IS NULL OR u.case_id = $1::bigint)" in SOURCE


def test_the_deployment_wide_reads_say_why_they_are_not_member_scoped():
    """
    The three that ignore `?member=` answer for the operator, not the customer, and
    the next reader must not take the missing scope for an oversight.
    """
    assert "what the deployment spends" in SOURCE
    for name in ("async def llm_list", "async def scenarios", "async def dashboard"):
        assert name in SOURCE
        assert "_needs_member" not in SOURCE[SOURCE.index(name):SOURCE.index(name) + 900]


def test_the_polling_choice_is_recorded_where_the_poll_is():
    """SSE is out of scope; a reader who cannot see that written down re-litigates it."""
    live = SOURCE[SOURCE.index("async def live"):SOURCE.index("# ---------------------------------------------------------------- 各情境")]
    assert "server-sent events" in live
    assert "Out of scope" in live


# ---------------------------------------------------------------- the page

@pytest.mark.parametrize(
    "tab", ["總覽", "聊天紀錄", "客戶資料", "LLM 追蹤", "即時流程圖", "各情境 token", "LLM Dashboard"]
)
def test_the_rail_offers_all_seven_tabs(tab: str):
    assert tab in PAGE


@pytest.mark.parametrize(
    "pane", ["overview", "inbox", "profile", "trace", "flow", "tokens", "dash"]
)
def test_every_tab_has_a_pane_to_show(pane: str):
    assert f'data-pane="{pane}"' in PAGE


def test_every_phase_colour_is_declared_on_bare_root():
    """
    A token defined only inside a media query never applies in the un-stamped state. The
    phase hues are mixes of tokens that are themselves redefined per theme, so one
    declaration is correct in both — but it has to be the bare one.
    """
    root = PAGE[PAGE.index(":root {"):PAGE.index(':root:not([data-theme="light"]) { }')]
    for phase in ("route", "scenario_tools", "answer", "validate", "repair", "embedding", "facts", "response"):
        assert f"--ph-{phase}:" in root, f"--ph-{phase} is not declared on bare :root"


def test_the_case_pane_still_carries_every_panel_it_had():
    """
    總覽 was moved into a tab, not redesigned. A panel lost in the move is a panel the
    agent still tells customers to look at.
    """
    overview = PAGE[PAGE.index('data-pane="overview"'):PAGE.index('data-pane="inbox"')]
    for panel in ("保單清單", "要保人 · 被保險人", "應簽署文件", "案件資料", "身分驗證", "稽核軌跡"):
        assert panel in overview, f"{panel} did not survive the move into a tab"


def test_only_the_open_tab_fetches():
    """
    Seven tabs each polling puts seven queries a second on a database that is also
    answering a live conversation, and six of the answers are thrown away unread.
    """
    assert "LOAD[key]?.()" in PAGE
    assert "clearInterval(livePoll)" in PAGE


def test_the_profile_tab_shows_the_service_history_not_only_the_contract():
    """
    The pane was built before `premium_payment`, `policy_beneficiary` and `claim` existed.

    A caseworker opening a customer saw what they bought and nothing about what has happened
    since — no premium behind or ahead, nobody named on the contract, no claim in flight.
    Those are the three things a customer is most likely to be ringing about.
    """
    source = Path("src/policydesk/web/console.py").read_text()
    body = source[source.index("async def profile("):source.index("async def llm_list(")]
    for table in ("premium_payment", "policy_beneficiary", "claim c"):
        assert table in body, f"the profile endpoint never reads {table}"

    page = Path("src/policydesk/web/static/index.html").read_text()
    for slot in ("profilePayments", "profileBeneficiaries", "profileClaims"):
        assert page.count(slot) >= 2, f"{slot} has no cell and no renderer"


def test_a_claim_with_no_outcome_is_shown_as_a_stage_not_a_verdict():
    # `outcome` is NULL for most claims and means still being assessed. A pane that inferred
    # a verdict from the stage would put a decision on screen that nobody made — the same
    # rule the scenario follows, applied where the caseworker reads it.
    page = Path("src/policydesk/web/static/index.html").read_text()
    body = page[page.index("function claimState("):page.index("async function loadProfile(")]
    assert "if (c.outcome)" in body, "the outcome must gate the verdict wording"
    assert "審核中" in body
    assert "never inferred from" in body, "the reason belongs beside the branch"
