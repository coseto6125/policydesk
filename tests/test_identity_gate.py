"""
資料核對: the check that runs before this desk discusses anyone's contracts.

It is not the 投保身分驗證 that `identity_check` holds. That one runs once, against the
government mock, at the signing stage, and stays valid for the case. This one proves the
person on *this connection* is the customer, and it expires with the connection.
"""

from pathlib import Path

import pytest

from policydesk.agent import tools
from policydesk.agent.scenario import CATALOGUE

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


def test_the_gate_skips_the_query_rather_than_the_sentence():
    """
    Withholding after the gather means the data was already read and only the prose
    about it was dropped. `_gather` returns the public half without ever running a
    member query.
    """
    body = EXECUTOR[EXECUTOR.index("async def _gather("):EXECUTOR.index("async def _public_only(")]
    assert "if not confirmed and tools.reads_identity(scenario.tools):" in body, (
        "an ungated scenario must run normally even unconfirmed"
    )
    assert "return await _public_only(db, scenario, params)" in body
    public = EXECUTOR[EXECUTOR.index("async def _public_only("):EXECUTOR.index("def _render(")]
    assert "list_policies" not in public
    assert "catalogue_sample" in public, "the public catalogue is what it can still show"


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
    assert "本次連線尚未完成身分核對" in body
    assert "不要猜" in body


def test_the_number_is_compared_on_the_server():
    """A check the browser performs is a check anyone skips with the console open."""
    assert "given != held" in SERVER
    page = Path("src/policydesk/web/static/index.html").read_text()
    assert "== held" not in page
    assert "national_id ===" not in page, "the page must not compare the number itself"


def test_a_greeting_is_answered_rather_than_frisked():
    """
    Saying 嗨 is not a request for anyone's policy data.

    Answering it with 請提供您的身分證字號 is a desk frisking someone at the door. The
    check belongs to the question that needs it, so an unconfirmed turn still routes and
    still answers — only the member queries are withheld.
    """
    from policydesk.agent.executor import _public_only

    marker = "        # The gate withholds the member queries, not the conversation."
    body = EXECUTOR[EXECUTOR.index(marker):]
    assert "turn.awaiting_identity = True" in body[:900]
    assert "return turn" not in body[:body.index("turn.scenario = scenario.name")]
    assert _public_only is not None


def test_a_number_typed_in_answer_is_never_routed():
    """
    Routing it sends a national ID to a model and answers it as a question, which is how
    a near-miss ended up replayed as "the thing they wanted to know".
    """
    branch = SERVER[SERVER.index('case "say" if case_id is not None and not confirmed and len('):
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
    branch = SERVER[SERVER.index('case "say" if case_id is not None and not confirmed and len('):
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
