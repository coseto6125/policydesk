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


def test_the_gate_refuses_before_the_tools_run_not_after():
    """
    Refusing after the gather means the data was already read and only the sentence
    about it was withheld.
    """
    body = EXECUTOR[EXECUTOR.index("    if not confirmed and tools.reads_identity"):]
    gate_return = body.index("return turn")
    gather = body.index("facts = await _gather")
    assert gate_return < gather, "the gate must return before the gather"


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
    assert "text.upper() != held" in SERVER
    page = Path("src/policydesk/web/static/index.html").read_text()
    assert "== held" not in page
    assert "national_id ===" not in page, "the page must not compare the number itself"


def test_an_unconfirmed_turn_reaches_no_model_at_all():
    """
    A near-miss ID was being routed, and the enrolment-stage 身分驗證 scenario answered
    it with its own template — so the customer read 請輸入身分證字號完成驗證 and thought
    the check had moved on. While unconfirmed the desk only takes the number or asks for
    it, which is also four model calls it no longer spends on failed attempts.
    """
    branch = SERVER[SERVER.index('case "say" if case_id is not None and not confirmed:'):
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
    branch = SERVER[SERVER.index('case "say" if case_id is not None and not confirmed:'):
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
    branch = SERVER[SERVER.index('case "say" if case_id is not None and not confirmed:'):
                    SERVER.index('case "say" if case_id is not None:')]
    assert "pending_question = pending_question or text" in branch
    assert "text = pending_question" in branch


def test_both_outcomes_reach_the_audit_trail():
    """It is the failures an auditor asks about."""
    assert "'identity_confirmed'" in SERVER
    assert "'identity_attempt'" in SERVER


def test_the_confirmation_lives_on_the_connection():
    """
    A refresh is a new connection, and a new connection is a different person until it
    proves otherwise. The history carries over; the confirmation does not.
    """
    socket = SERVER[SERVER.index("async def customer_socket"):SERVER.index('case "hello":')]
    assert "confirmed = False" in socket, "the flag must be per-socket local state"
    assert "confirmed" not in SERVER[:SERVER.index("async def customer_socket")], "no module-level confirmation"
