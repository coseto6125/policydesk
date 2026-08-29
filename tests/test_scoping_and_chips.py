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

    The member scope was once described here as doing that work on its own. It cannot:
    `?member=` is chosen by the requester, so 200-for-a-holding and 404-for-a-miss is an
    oracle over 48 members by 660 products, answered without a token to anyone who can
    reach the port. The scope decides *which* contract a reader gets; the token decides
    whether there is a reader at all. Both are asserted, because removing either one puts
    the enumeration back.
    """
    from types import SimpleNamespace

    from policydesk.web.server import DESK_TOKEN, _unauthorised

    held = SERVER[SERVER.index("async def _held("):SERVER.index('@app.get("/contract/')]
    assert "EXISTS (SELECT 1 FROM policy po" in held
    assert "po.member_id = $2::bigint" in held
    body = SERVER[SERVER.index("async def contract("):SERVER.index('@app.get("/api/llm-turns")')]
    assert "_held(" in body, "every contract read goes through the ownership check"
    assert "contract_out_of_scope" in body
    anonymous = SimpleNamespace(args={"member": "4"}, ip="1.2.3.4")
    assert _unauthorised(anonymous, "contract") is not None, "the scope alone is an oracle"
    assert _unauthorised(SimpleNamespace(args={"token": DESK_TOKEN}, ip="1.2.3.4"), "contract") is None


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


def test_the_chips_a_refused_customer_sees_are_ones_the_desk_can_answer():
    """
    Measured on a live turn: 我想查一下我的保單保什麼 was answered with a request for the
    national ID, and then offered 我想了解目前的保單保什麼 as a chip — the question that had
    just been refused, one tap away.

    Every entry in the public set must belong to a scenario with a tool that survives the
    gate, or it is the same trap with different words.
    """
    from policydesk.agent import tools
    from policydesk.agent.scenario import CATALOGUE, OPENERS, PUBLIC_OPENERS

    assert not set(PUBLIC_OPENERS) & set(OPENERS)
    answerable = {
        s.name
        for s in CATALOGUE
        if tools.permitted(s.tools, owner=__import__("importlib").import_module(s.tools_module)
                           if s.tools_module else None, confirmed=False)
    }
    assert {"cooling_off", "disclosure", "reinstate", "browse_products"} <= answerable, (
        f"a chip promises an answer from a scenario that has nothing public: {sorted(answerable)}"
    )
    for chip in PUBLIC_OPENERS:
        assert chip.endswith(("？", "?")), f"a chip that is not a question is a commitment: {chip}"


def test_a_chip_never_repeats_the_question_it_answers():
    """
    Measured on two live turns.

    住院四天要準備什麼理賠文件？ was answered and then offered 理賠要準備哪些文件？ as a chip.
    我想查一下我的保單保什麼 was answered with a request for the national ID and then offered
    我想了解目前的保單保什麼. Both are one tap back to the same place.
    """
    from policydesk.agent.executor import _echoes

    for chip, said in (
        ("理賠要準備哪些文件？", "住院四天要準備什麼理賠文件？"),
        ("我想了解目前的保單保什麼", "我想查一下我的保單保什麼"),
        ("猶豫期是幾天？", "猶豫期是幾天"),
        ("想確認一年繳多少保費", "我一年繳多少保費？"),
    ):
        assert _echoes(chip, said), f"{chip!r} repeats {said!r} and was offered anyway"

    # And the other direction, because a filter that drops everything leaves the customer
    # with no next step at all.
    for chip, said in (
        ("想確認一年繳多少保費", "住院四天要準備什麼理賠文件？"),
        ("理賠要準備哪些文件？", "嗨"),
        ("你們有哪些商品？", "我有高血壓要講嗎"),
    ):
        assert not _echoes(chip, said), f"{chip!r} was dropped for an unrelated question"


def test_the_router_is_told_to_call_a_scenario_it_cannot_fully_parameterise():
    # Measured: 住院四天要準備什麼理賠文件？ never reached `claim_checklist`, because the
    # router had no 住院日期 and read 填不出來才問 as "ask instead of calling". The document
    # list does not depend on the date, so the customer waited a turn for something already
    # on the shelf.
    from policydesk.agent.scenario import ROUTER_INSTRUCTIONS

    assert "推不出來也要先呼叫情境工具" in ROUTER_INSTRUCTIONS
    assert "留空字串" in ROUTER_INSTRUCTIONS


def test_a_refused_customer_is_not_offered_a_question_from_two_turns_ago():
    """
    The echo filter ran, and `PUBLIC_OPENERS` then replaced its result.

    So it protected every path except the one that needed it: an unverified customer is
    handed a fixed list of four, the same four every turn, and the filter's output was
    discarded on exactly that path. It compares against the whole conversation now, not
    only the sentence being answered — a chip is stale the turn after it is asked, not
    just the moment it is asked.

    **It compares characters, so it catches a rephrasing and not a synonym.** 你們有哪些
    商品？ against 你們有什麼壽險可以保 shares 你們有 and nothing else: 43% of the chip's
    distinct characters, under the 60% bar. That pair reached a live turn and this filter
    does not close it — 商品 and 壽險 are the same thing to a customer and different
    strings to a comparison, which is the retrieval problem wearing a smaller hat.
    """
    from policydesk.agent.executor import _fresh
    from policydesk.agent.scenario import PUBLIC_OPENERS

    # Asked two turns back, and the chip row is rebuilt from scratch every turn.
    said = ["保單停效還能不能復效", "那要準備什麼"]
    offered = _fresh(PUBLIC_OPENERS, said)
    assert "保單停效還能不能復效？" not in offered
    assert offered, "an empty chip row leaves a customer with nowhere to start"


def test_a_chip_row_that_would_empty_keeps_its_chips():
    # The other direction. A customer who has asked about everything the row offers is
    # better served by a stale suggestion than by no suggestion at all.
    from policydesk.agent.executor import _fresh
    from policydesk.agent.scenario import PUBLIC_OPENERS

    assert _fresh(PUBLIC_OPENERS, list(PUBLIC_OPENERS)) == PUBLIC_OPENERS


def test_both_refusal_paths_say_not_to_repeat_the_same_request():
    """
    A customer who repeats a question is saying the last answer did not land.

    The desk answered 那我適合哪一張 with 請提供您的身分證字號, and answered it again with
    the same request in fewer words. The rule reaches both paths that can refuse: the
    scenario one through `IDENTITY_PENDING`, and the router's free answer, which is where
    the third of those three turns was written.
    """
    import inspect

    from policydesk.agent import executor
    from policydesk.agent.scenario import ASKED_ALREADY, IDENTITY_PENDING

    assert ASKED_ALREADY in IDENTITY_PENDING
    assert "ASKED_ALREADY" in inspect.getsource(executor.run_turn)
