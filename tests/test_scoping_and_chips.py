"""
Who sees what, and what a tap can commit the customer to.

Both are red lines rather than preferences: one is another customer's national ID, the
other is an expression of intent the customer never made.
"""

from pathlib import Path

import pytest

SERVER = Path("src/policydesk/web/server.py").read_text()
PAGE = Path("src/policydesk/web/static/index.html").read_text()

# Phrases that turn a tap into a commitment. A mis-tap is one pixel away from a tap.
COMMITTING = ("我要買", "購買", "我要投保", "確認投保", "我同意", "幫我簽", "成交", "下單")


@pytest.mark.parametrize("phrase", COMMITTING)
def test_no_quick_reply_commits_the_customer(phrase: str):
    """
    A chip is a question. Buying is typed.

    A one-tap 我要買這張 is an expression of intent the customer may never have formed,
    and the tap is indistinguishable from the mis-tap.
    """
    from policydesk.agent.scenario import CATALOGUE, OPENERS

    everything = [*OPENERS, *(q for s in CATALOGUE for q in s.quick_replies)]
    for reply in everything:
        assert phrase not in reply, f"quick reply {reply!r} commits the customer"


def test_every_scenario_that_answers_offers_somewhere_to_go():
    """A reply with no follow-up leaves the customer guessing what else the desk does."""
    from policydesk.agent.scenario import CATALOGUE, Emit

    for scenario in CATALOGUE:
        if scenario.emit is Emit.MODEL or scenario.name in {"billing", "coverage"}:
            assert scenario.quick_replies, f"{scenario.name} offers no follow-up"


def test_a_turn_that_routed_nowhere_still_offers_the_openers():
    from policydesk.agent.executor import Turn
    from policydesk.agent.scenario import OPENERS

    assert Turn(1, 1).quick_replies == OPENERS


def test_the_desk_socket_refuses_to_open_unscoped():
    """
    The queue alone names every customer. The token is not what separates them.

    This window is one visitor: the right pane is their conversation and the left is the
    back office view of *their* case.
    """
    body = SERVER[SERVER.index("async def desk_socket"):SERVER.index("async def _queue")]
    assert 'request.args.get("member"' in body
    assert "desk_socket_unscoped" in body


def test_opening_a_case_checks_ownership_rather_than_trusting_the_queue():
    """The case id comes off the wire, so it is not the queue that decides access."""
    body = SERVER[SERVER.index("async def desk_socket"):SERVER.index("async def _queue")]
    opened = body[body.index('case "open":'):]
    assert 'snap["member_id"] == viewer' in opened


def test_the_queue_read_takes_a_member_and_has_no_default():
    """An optional scope is a scope somebody forgets to pass."""
    assert "async def _queue(db: Database, member_id: int)" in SERVER
    assert "WHERE c.member_id = $1::bigint" in SERVER


def test_the_contract_route_serves_only_what_the_viewer_holds():
    """
    The catalogue is public; which contract this visitor may open is not.

    No desk token here — /doc/<id> is gated because it renders an applicant's national
    ID and address, and a contract PDF renders neither. The member scope is what does
    the work.
    """
    body = SERVER[SERVER.index("async def contract("):SERVER.index('@app.get("/api/llm-turns")')]
    assert "EXISTS (SELECT 1 FROM policy po WHERE po.product_id = p.product_id AND po.member_id = $2::bigint)" in body
    assert "contract_out_of_scope" in body


def test_the_policy_row_links_to_its_contract():
    assert "/contract/${encodeURIComponent(p.product_id)}" in PAGE


def test_the_snapshot_carries_the_product_id_the_link_needs():
    commands = Path("src/policydesk/core/commands.py").read_text()
    policies = commands[commands.index('case["policies"]'):]
    assert "po.product_id" in policies


def test_the_composer_waits_for_the_profile():
    """There is nothing to answer a question against before the member exists."""
    gate = PAGE[PAGE.index('$("enterBtn").onclick'):PAGE.index('$("nameInput").addEventListener')]
    assert 'chatInput").disabled = false' not in gate
    profile = PAGE[PAGE.index('case "profile":'):PAGE.index('case "case": renderCase(m)')]
    assert 'chatInput").disabled = false' in profile


def test_the_occupation_class_is_looked_up_and_never_accepted_from_the_client():
    """A client that names its own 職業等級 sells itself a product it is barred from."""
    person = Path("src/policydesk/synthetic/person.py").read_text()
    body = person[person.index("def generate("):]
    assert "OCCUPATION_CLASS.get(occupation" in body
    enrol = SERVER[SERVER.index('case "enrol"'):SERVER.index('case "say"')]
    assert "occupation_class" not in enrol.split("await ws.send")[0]
