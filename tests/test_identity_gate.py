"""
資料核對: the check that runs before this desk discusses anyone's contracts.

It is not the 投保身分驗證 that `identity_check` holds. That one runs once, against the
government mock, at the signing stage, and stays valid for the case. This one proves the
person on *this connection* is the customer, and it expires with the connection.
"""

from datetime import date
from pathlib import Path

import pytest

from policydesk.agent import tools
from policydesk.agent.scenario import CATALOGUE, IDENTITY_PENDING

SERVER = Path("src/policydesk/web/server.py").read_text()
EXECUTOR = Path("src/policydesk/agent/executor.py").read_text()


@pytest.mark.parametrize(
    "name", ["list_policies", "billing_summary", "coverage_summary", "standing_brief",
             "benefit_headings", "find_clause", "find_multiplier", "required_documents",
             "clause_ids_for", "member_underwriting"],
)
def test_every_tool_that_reads_a_member_is_marked(name: str):
    """
    The flag lives on the function that touches the data, not on the scenario.

    A scenario's gate is derived from its tools, so adding a member-reading tool cannot
    leave the gate behind — which is the mistake a hand-maintained list of protected
    scenarios makes eventually.
    """
    assert getattr(getattr(tools, name), "requires_identity", False), f"{name} reads a member unmarked"


def test_a_tool_over_the_public_catalogue_is_not_marked():
    """The product catalogue is public; gating it would ask for an ID to answer nothing."""
    assert not getattr(tools.suitable_products, "requires_identity", False)
    assert not getattr(tools.alternatives, "requires_identity", False)


@pytest.mark.parametrize(
    "scenario", [s for s in CATALOGUE if s.name in {"policy_overview", "explain_cover", "billing", "coverage", "claim_checklist", "recommend"}]
)
def test_scenarios_touching_the_customer_book_derive_the_gate(scenario):
    assert tools.reads_identity(scenario.tools), f"{scenario.name} reaches member data ungated"


@pytest.fixture(scope="module")
async def db():
    from policydesk.core.db import Database

    pool = Database()
    try:
        await pool.fetch_val("SELECT 1")
    except Exception:
        pytest.skip("policydesk-pg is not up")
    return pool


class _RefusingDB:
    """
    A database that raises the moment a query names a member table.

    The gate's promise is that the query does not run, not that its output is dropped
    afterwards. Only a database that refuses to answer can tell those two apart, which a
    search of the executor's source cannot: it says the branch exists, never that the
    branch is reached. Measured cost of not knowing that — `browse_products` declares only
    public tools, so the per-scenario question answered "no gate needed" and an unverified
    visitor's whole book went into the prompt on the next line.
    """

    FORBIDDEN = ("from policy", "from member", "join policy", "join member")

    def __init__(self) -> None:
        self.seen: list[str] = []

    def _check(self, sql: str) -> None:
        self.seen.append(sql)
        flat = " ".join(sql.lower().split())
        for phrase in self.FORBIDDEN:
            if phrase in flat:
                raise AssertionError(f"an unverified session read a member table: {phrase!r}")

    async def fetch(self, sql: str, params: object = None) -> list[dict[str, object]]:
        self._check(sql)
        return []

    async def fetch_one(self, sql: str, params: object = None) -> dict[str, object] | None:
        self._check(sql)
        return None

    async def fetch_val(self, sql: str, params: object = None) -> object:
        self._check(sql)
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", [s for s in CATALOGUE if not s.tools_module], ids=lambda s: s.name)
async def test_an_unverified_session_runs_no_member_query(scenario):
    """
    Every scenario, not only the ones whose declared tools happen to be marked.

    A scenario declaring nothing but public tools still reached `list_policies` and
    `clause_ids_for`, because those are called by the gatherer rather than declared by the
    scenario. The gate is now per tool and those two are named explicitly; this asserts it
    for the whole catalogue rather than for the scenarios someone remembered.
    """
    from policydesk.agent.executor import Turn, _gather

    db = _RefusingDB()
    turn = Turn(case_id=1, member_id=1)
    facts = await _gather(
        db, scenario, turn, today=date(2026, 8, 29),
        params={p.name: p.example or "測試" for p in scenario.params}, confirmed=False,
    )
    assert facts.get("_identity_required") is True, f"{scenario.name} did not mark the answer partial"
    assert facts.get("_allowed_clauses") == frozenset(), (
        f"{scenario.name} offered clause ids nothing can check"
    )


def test_the_standing_brief_is_not_read_before_the_check():
    """It is the customer's whole book. It is the first thing to withhold, not the last."""
    body = EXECUTOR[EXECUTOR.index("messages, profile, brief = await asyncio.gather"):]
    assert "if confirmed else _nothing()" in body[:400]


def test_the_model_is_told_as_well_as_blocked():
    """
    The prompt makes the ask natural; the gate makes it true.

    Telling the model alone leaves a jailbreak reading real policies. Blocking alone
    produces a system refusal in the middle of a conversation.
    """
    body = EXECUTOR[EXECUTOR.index("if not confirmed:"):EXECUTOR.index('past = f"{known}')]
    assert "This session has not passed 資料核對" in body
    assert "comes from the material" in body


def test_the_number_is_compared_on_the_server():
    """A check the browser performs is a check anyone skips with the console open."""
    assert "given != held" in SERVER
    page = Path("src/policydesk/web/static/index.html").read_text()
    assert "== held" not in page
    assert "national_id ===" not in page, "the page must not compare the number itself"


@pytest.mark.asyncio
async def test_a_greeting_is_answered_rather_than_frisked(db):
    """
    Saying 嗨 is not a request for anyone's policy data.

    Answering it with 請提供您的身分證字號 is a desk frisking someone at the door. The
    check belongs to the question that needs it, so an unconfirmed turn still routes and
    still answers — only the member queries are withheld.
    """
    from policydesk.agent.executor import Turn, _gather
    from policydesk.agent.scenario import BY_NAME

    # A scenario an unverified customer can reach must still produce material, not a bare
    # refusal. `browse_products` is the case: its one tool is the public catalogue, and it
    # is what 你們有什麼壽險 routes to before anyone has proved who they are.
    facts = await _gather(
        db, BY_NAME["browse_products"], Turn(1, 1), today=date(2026, 8, 29),
        params={"line": "life"}, confirmed=False,
    )
    assert facts.get("catalogue_sample"), "the desk answered a public question with nothing"
    assert facts.get("_identity_required") is True, "and it must still say a part is withheld"


def test_a_number_typed_in_answer_is_never_routed():
    """
    Routing it sends a national ID to a model and answers it as a question, which is how
    a near-miss ended up replayed as "the thing they wanted to know".
    """
    branch = SERVER[SERVER.index('case "say" if case_id is not None and not confirmed and NATIONAL_ID'):
                    SERVER.index('case "say" if case_id is not None:')]
    assert "run_turn" not in branch
    assert "_answer(" in branch, "the replay after a pass still runs a real turn"


def test_a_returning_session_is_not_handed_the_number_it_must_supply():
    """Sending it to the browser before the check is sending the answer with the question."""
    returning = SERVER[SERVER.index("if existing is not None:"):SERVER.index("draft = generate(")]
    assert '_mask(existing["national_id"])' in returning
    assert 'existing["national_id"],' not in returning


def test_no_identity_attempt_enters_the_transcript():
    """
    Right or wrong, the number is never written as a message.

    It would sit in the history block of every later prompt, and a national ID in a
    model's context is a national ID that leaves. A near-miss is no better: it is one
    character from the real one.
    """
    branch = SERVER[SERVER.index('case "say" if case_id is not None and not confirmed and NATIONAL_ID'):
                    SERVER.index('case "say" if case_id is not None:')]
    assert "INSERT INTO conversation_message" not in branch
    assert "identity_attempt" in branch, "a refused attempt is still audited"


def test_the_question_they_asked_first_is_the_one_answered():
    """
    Making them retype it is the desk forgetting what it just asked them to wait for.

    Captured once and never overwritten, because the messages after it are wrong
    numbers — reading the transcript for "the last thing they said" replayed a failed
    ID attempt as the question.
    """
    assert "text, pending_question = pending_question, None" in SERVER
    # Captured on any unconfirmed turn, not only a blocked one: the router often asks
    # for the number in prose without reaching a scenario at all, and that question is
    # just as worth coming back to.
    capture = SERVER[SERVER.index("if not confirmed:\n                        # The latest question"):]
    assert "pending_question = text" in capture[:600]


def test_both_outcomes_reach_the_audit_trail():
    """It is the failures an auditor asks about."""
    assert '"identity_confirmed" if given == held else "identity_attempt"' in SERVER


def test_the_confirmation_lives_on_the_connection():
    """
    A refresh is a new connection, and a new connection is a different person until it
    proves otherwise. The history carries over; the confirmation does not.
    """
    socket = SERVER[SERVER.index("async def customer_socket"):SERVER.index('case "hello":')]
    assert "confirmed = False" in socket, "the flag must be per-socket local state"
    assert "confirmed" not in SERVER[:SERVER.index("async def customer_socket")], "no module-level confirmation"


def test_a_scenario_the_case_cannot_reach_is_not_offered_to_the_router():
    """
    The router picks from what it is offered, so what it must not choose it must not see.

    那我適合哪一張 was routed to the signing-stage 身分驗證 scenario, which answered with
    a sentence about送交核保人員審核 for an application that did not exist. `requires_stage`
    was declared on two scenarios and read by nothing.
    """
    from policydesk.agent.executor import reachable

    early = {s.name for s in reachable("inquiry")}
    assert "verify_identity" not in early
    assert "issue_documents" not in early
    assert "recommend" in early

    assert "issue_documents" in {s.name for s in reachable("proposed")}
    assert "verify_identity" in {s.name for s in reachable("signed")}


def test_the_replay_takes_the_latest_question_not_the_first():
    """
    Keeping the first replayed 嗨 after the check passed.

    What is worth coming back to is whatever they were asking when the desk stopped
    them, and the ID attempts cannot overwrite it because they never reach that branch.
    """
    capture = SERVER[SERVER.index("if not confirmed:\n                        # The latest question"):]
    assert "pending_question = text" in capture[:600]
    assert "pending_question or text" not in capture[:600]


def test_the_public_catalogue_reaches_an_unverified_visitor():
    """
    An insurer publishes its catalogue. Refusing to name a product until someone proves
    who they are is a desk that will not speak, and the customer's question was about
    the products, not about themselves.
    """
    from policydesk.agent.scenario import BROWSE_PRODUCTS

    assert not tools.reads_identity(BROWSE_PRODUCTS.tools)
    assert not getattr(tools.catalogue_sample, "requires_identity", False)
    assert {p.name for p in BROWSE_PRODUCTS.params} == {"line"}, "no budget: that is a question about them"


@pytest.mark.asyncio
async def test_no_scenario_returns_a_value_out_of_the_members_own_row():
    """
    The sentinel form of the gate, over the whole catalogue at once.

    `_RefusingDB` above proves no member query ran. This proves no member *value* came
    back, which is the stronger statement: it also catches a value arriving from a table
    nobody thought to forbid, or from a cache, or from a default somebody filled in.

    Ported from the correctness reviewer's sweep, which is the run that established the
    invariant — sixteen scenarios, unconfirmed, against a real member's real values.
    """
    import json

    from policydesk.agent.executor import Turn, _gather
    from policydesk.core.db import Database

    db = Database()
    try:
        await db.fetch_val("SELECT 1")
    except Exception:
        pytest.skip("policydesk-pg is not up")

    row = await db.fetch_one(
        """SELECT m.member_id, m.national_id, m.birth_date, m.beneficiary_relation,
                  array_agg(po.policy_number) AS numbers
           FROM member m JOIN policy po USING (member_id)
           GROUP BY 1, 2, 3, 4 LIMIT 1"""
    )
    if row is None:
        pytest.skip("no member holds a policy")

    sentinels = {str(row["national_id"]), str(row["birth_date"]), str(row["beneficiary_relation"])}
    sentinels |= {str(n) for n in (row["numbers"] or [])}
    sentinels = {s for s in sentinels if s and len(s) > 3}
    assert sentinels, "the sweep proves nothing without values to look for"

    params = {
        "topic": "住院", "line": "life", "budget": "20000", "need": "加保", "event": "住院四天",
        "event_date": "2026-08-01", "concern": "我離婚了", "keyword": "", "amount": "1000000",
        "national_id": "A123456789",
    }
    leaked: dict[str, list[str]] = {}
    for scenario in CATALOGUE:
        facts = await _gather(
            db, scenario, Turn(1, row["member_id"]), today=date(2026, 8, 29),
            params=params, confirmed=False, index=None,
        )
        blob = json.dumps({k: str(v) for k, v in facts.items()}, ensure_ascii=False)
        if found := sorted(s for s in sentinels if s in blob):
            leaked[scenario.name] = found
    assert not leaked, f"an unverified session was handed the member's own values: {leaked}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [s for s in CATALOGUE if IDENTITY_PENDING in s.injection and not s.tools_module],
    ids=lambda s: s.name,
)
async def test_a_scenario_promising_public_material_actually_returns_some(scenario, db):
    """
    The paragraph tells the model to answer from the public material it was handed.

    `recommend` promised that and returned two flags: killing `_public_only` removed the
    `catalogue_sample` fallback and nothing noticed, because the test guarding this asserted
    `_public_only is not None` and read the executor's source for a marker.

    A scenario carrying the paragraph either has a public tool that survives the gate, or
    its own copy of the paragraph is a promise it cannot keep — and the model fills that gap
    from what it already knows about insurance.
    """
    from policydesk.agent.executor import Turn, _gather

    public = tools.permitted(scenario.tools, confirmed=False)
    facts = await _gather(
        db, scenario, Turn(1, 4), today=date(2026, 8, 29),
        params={p.name: p.example or "測試" for p in scenario.params}, confirmed=False,
    )
    material = set(facts) - {"_identity_required", "_allowed_clauses"}
    if public:
        assert material, f"{scenario.name} declares {sorted(public)} and returned none of it"
    else:
        assert "holds no public information" in scenario.injection, (
            f"{scenario.name} has no public tool, so the paragraph must say what to do with none"
        )


def test_a_ten_character_question_is_not_read_as_a_national_id():
    """
    Measured over a real socket, and it cost the customer the session.

    The branch used to fire on length alone. 我想查我的保單保什麼 is exactly ten characters,
    so it was consumed as a failed identity attempt: the customer was told 這組號碼與檔案
    不符 for a sentence, `pending_question` was never set so the desk had nothing to come
    back to after a pass, and three such questions locked a check the customer had not yet
    been asked to make.
    """
    from policydesk.web.server import NATIONAL_ID

    for sentence in (
        "我想查我的保單保什麼",
        "住院四天要準備什麼理賠",
        "我想知道保費多少錢啊",
        "0912345678",
        "ABCDEFGHIJ",
    ):
        assert not NATIONAL_ID.fullmatch(sentence), f"{sentence!r} was read as a national ID"

    for real in ("A123456789", "a123456789", "F229876543"):
        assert NATIONAL_ID.fullmatch(real), f"{real!r} is a national ID and was not read as one"


def test_an_unknown_tool_name_is_dropped_even_on_a_confirmed_turn():
    """
    The gate resolved names only when it was about to withhold some.

    `permitted` returned `frozenset(tool_names)` unread whenever the session was
    confirmed, so a name nobody could resolve came back as permitted — the rule that an
    unchecked tool is excluded held for an unverified customer and not for a verified
    one. The asymmetry is the bug: a gate that fails closed only half the time is a gate
    whose contract cannot be relied on by the code downstream of it.
    """
    assert tools.permitted(("no_such_tool",), confirmed=True) == frozenset()
    assert tools.permitted(("no_such_tool",), confirmed=False) == frozenset()


def test_a_confirmed_turn_still_gets_every_tool_that_does_resolve():
    # The other half. Fixing the fail-closed hole must not withhold real tools from a
    # customer who has proved who they are.
    from policydesk.agent.scenarios import payment

    assert tools.permitted(payment.PAYMENT.tools, owner=payment, confirmed=True) == frozenset(payment.PAYMENT.tools)
