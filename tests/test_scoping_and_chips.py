"""
Who sees what, and what a tap can commit the customer to.

Both are red lines rather than preferences: one is another customer's national ID, the
other is an expression of intent the customer never made.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from policydesk.agent import executor
from policydesk.agent.scenario import ASKED_ALREADY, BY_NAME, IDENTITY_NEXT_STEP, Emit
from policydesk.llm.provider import Completion, Phase


@pytest.fixture(scope="module")
async def live_case(db):
    row = await db.fetch_one('SELECT case_id, member_id FROM "case" ORDER BY case_id DESC LIMIT 1')
    if row is None:
        pytest.skip("no case to run a turn against")
    return row

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

    assert "call the scenario tool anyway" in ROUTER_INSTRUCTIONS
    assert "empty string" in ROUTER_INSTRUCTIONS


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


@pytest.mark.parametrize("identity_locked", [False, True], ids=["pending", "locked"])
# `out_of_scope` is the router's only reply that calls no second model: its answer is a
# template. The free-answer branch it replaces is gone — the router calls a tool now.
@pytest.mark.parametrize("scenario_name", ["out_of_scope", "policy_overview"],
                         ids=["template_reply", "scenario_reply"])
async def test_run_turn_repeated_request_sends_state_aware_guidance_to_each_answering_phase(
    monkeypatch, identity_locked, scenario_name,
):
    """Assert provider input, not the constant or source location that assembles it."""
    question = "那我適合哪一張？"
    previous_reply = "請提供您的身分證字號。"
    monkeypatch.setattr(executor.memory, "recent", AsyncMock(return_value=[
        {"speaker": "customer", "text": question},
        {"speaker": "agent", "text": previous_reply},
        {"speaker": "customer", "text": question},
    ]))
    monkeypatch.setattr(executor.statute, "unresolved", AsyncMock(return_value=[]))
    provider, db = AsyncMock(), AsyncMock()
    db.fetch_val.return_value = "inquiry"
    provider.complete.side_effect = [
        Completion(
            text="可以先說明公開資訊。", provider="test",
            tool_calls=({"name": scenario_name, "arguments": "{}"},),
        ),
        Completion(
            text='{"reply":"可以先說明公開資訊。","citations":[],"calculations":[]}', provider="test",
        ),
    ]
    turn = await executor.run_turn(
        provider, db, case_id=1, member_id=1, text=question,
        confirmed=False, identity_locked=identity_locked, locale="zh-TW",
    )
    assert turn.scenario == scenario_name
    templated = BY_NAME[scenario_name].emit is Emit.TEMPLATE
    expected_phases = [Phase.ROUTE] if templated else [Phase.ROUTE, Phase.ANSWER]
    calls = provider.complete.call_args_list
    assert [call.kwargs["phase"] for call in calls] == expected_phases
    state = "locked" if identity_locked else "pending"
    for call in calls:
        payload = f"{call.kwargs['instructions']}\n{call.kwargs['user_input']}"
        assert payload.count(ASKED_ALREADY) == 1
        assert payload.count(IDENTITY_NEXT_STEP) == 1
        assert f"# Identity verification state: {state}" in payload
        assert previous_reply in call.kwargs["user_input"]
        assert question in call.kwargs["user_input"]
    db.fetch.assert_not_awaited()


def test_no_quick_reply_anywhere_commits_the_customer():
    """
    A tap is one pixel from a mis-tap, and a mis-tap writes into the case record.

    Two got through: 我要申訴這個理賠結果 on `claim_status`, and 我要申訴，該找誰？ on
    `soothe` — whose tail is a question and whose head is a decision the customer had
    not made. 請幫我查我的保單怎麼寫 is the same shape without the word 申訴.

    我想了解 and 想確認 stay: they name a subject the customer wants explained, which is
    what a follow-up chip is for.
    """
    import re

    from policydesk.agent.scenario import CATALOGUE

    commits = re.compile(r"^(我要|請幫我|幫我)")
    offending = [(s.name, q) for s in CATALOGUE for q in s.quick_replies if commits.match(q)]
    assert not offending, f"a tap on these writes an intention nobody expressed: {offending}"


@pytest.mark.asyncio
async def test_a_gated_turn_offers_only_what_the_desk_can_still_answer(db, live_case):
    """
    The swap ran on the free-answer path and not on the gated one.

    A customer asked 我的保單保額是多少 with no id. `coverage` ran, its member query was
    withheld, and the reply asked for the number — then offered 我想了解這些保額夠不夠,
    已經理賠過的會扣掉嗎, 想確認有沒有重複投保. All three need the same id, so every chip
    under a refusal led straight back to it. Measured on a live turn.

    Driven through `run_turn` with a provider that routes to `coverage`, so the assertion
    is on the chips a customer would see rather than on the premise that `coverage` reads
    identity — which was the first version of this test and proved nothing about the swap.
    """
    from policydesk.agent.executor import run_turn
    from policydesk.agent.scenario import BY_NAME, PUBLIC_OPENERS
    from policydesk.llm.provider import Completion

    class RoutesToCoverage:
        name = "stub"

        async def complete(self, **_: object) -> Completion:
            return Completion(text="", model="stub", provider="stub",
                              tool_calls=({"name": "coverage", "arguments": {}},))

    turn = await run_turn(
        RoutesToCoverage(), db, case_id=live_case["case_id"], member_id=live_case["member_id"],
        text="我的保單保額是多少", confirmed=False,
    )
    assert turn.awaiting_identity, "the premise: this turn was refused for want of an id"
    assert set(turn.quick_replies) <= set(PUBLIC_OPENERS), (
        f"a refused customer was offered {turn.quick_replies}, which the same gate refuses"
    )
    assert not set(turn.quick_replies) & set(BY_NAME["coverage"].quick_replies)


@pytest.mark.asyncio
async def test_a_sampled_catalogue_says_how_much_it_left_out(db):
    """
    Five of seventy-six health products reached a customer under 目前有以下醫療險商品.

    That reads as the whole catalogue, so the customer compared a list already cut to the
    five cheapest and never learned the other seventy-one existed. The count travels with
    the rows because the rows are a sample, and `count(*) OVER ()` runs before `LIMIT` so
    it is the real total rather than the length of what came back.
    """
    from policydesk.agent.tools import catalogue_sample

    rows = await catalogue_sample(db, "health", 3)
    if not rows:
        pytest.skip("no health products on sale")
    total = await db.fetch_val(
        """SELECT count(*) FROM catalog_entry ce JOIN product p USING (product_id)
           WHERE ce.on_sale AND p.line = 'health'""")
    assert len(rows) == 3
    assert all(r["on_sale_in_line"] == total for r in rows)
    assert total > len(rows), "this test says nothing unless the sample is actually short"


def test_the_injections_that_sample_a_catalogue_name_the_total():
    # A field nobody reads is the `source_message_id` failure: present, populated, and
    # changing no behaviour. Both scenarios that show a cut list must state the count.
    from policydesk.agent.scenario import BY_NAME

    assert "on_sale_in_line" in BY_NAME["browse_products"].injection
    assert "matching_in_line" in BY_NAME["quote"].injection


def test_nothing_the_customer_reads_points_at_the_caseworker_console():
    """
    「各張保單明細請見左側後台的保單清單」 went to a customer on a phone.

    The console is the caseworker's half of a two-pane page. The customer is looking at a
    chat window; that pane is not theirs and they cannot open it, so the sentence sends
    them to check a total against something that does not exist for them.
    """
    from policydesk.agent.scenario import CATALOGUE, WRITING

    console_words = ("左側", "後台", "右邊的面板", "上方分頁")
    offending = [
        (s.name, word)
        for s in CATALOGUE
        for word in console_words
        if word in s.template or word in s.injection
    ]
    assert not offending, f"a customer cannot open the console: {offending}"
    assert not [w for w in console_words if w in WRITING]


def test_the_reinstatement_answer_names_the_paragraph_that_protects_him():
    # 〔保險法 第116條第4項〕 — the insurer is deemed to agree when it does not refuse
    # within fifteen days. The tool returns it among thirteen paragraphs and the model
    # left it out, so the reply told the customer what to prepare and never that the
    # company is on a clock too.
    from policydesk.agent.scenario import BY_NAME

    assert "第116條第4項" in BY_NAME["reinstate"].injection


def test_the_beneficiary_answer_keeps_the_estate_rule_to_death_benefits():
    # §112 is about a death benefit paid to a named beneficiary. Stated as a class —
    # 已指定受益人的保單，其保險金額不作為遺產 — it sweeps in 住院日額 and 實支實付,
    # which the insured collects while alive and which are not an estate question. A
    # customer could plan an estate on the wider reading.
    from policydesk.agent.scenario import BY_NAME

    injection = BY_NAME["beneficiary"].injection
    assert "身故保險金" in injection
    assert "生前" in injection


async def test_a_year_of_premiums_is_what_the_instalments_add_to(db):
    """
    74,100 and 74,106 reached the same customer in the same replay.

    `billing` summed the rate card; `payment` listed the instalments. A member holding a
    monthly policy pays the rounded instalment twelve times, so the rate-card figure is
    not their bill — and the template calls the number 一年繳費合計, which is a claim
    about the bill.

    Checked across every member rather than one: the gap is a rounding per instalment, so
    a member whose instalments happen to divide evenly agrees under either definition and
    proves nothing. Member 70's five policies were the pair that diverged; picking one
    member by any rule risks picking one of the many that do not.
    """
    from datetime import UTC, datetime

    from policydesk.agent import tools
    from policydesk.agent.scenarios.payment import payment_state
    from policydesk.synthetic.service import MODES

    today = datetime.now(UTC).date()
    members = [
        int(r["member_id"])
        for r in await db.fetch(
            "SELECT DISTINCT po.member_id FROM policy po JOIN premium_payment USING (policy_id) "
            "WHERE po.lapsed_at IS NULL ORDER BY 1 LIMIT 40"
        )
    ]
    if not members:
        pytest.skip("no member holds a policy with a payment schedule")

    apart: list[str] = []
    for member_id in members:
        summary = await tools.billing_summary(db, member_id, today=today)
        if summary.get("no_schedule"):
            continue
        rows = await payment_state(db, member_id, today=today)
        a_year = sum(
            float(r["instalment"]) * (12 // MODES[r["premium_mode"]]) for r in rows if not r["is_lapsed"]
        )
        if round(float(summary["premium"])) != round(a_year):
            apart.append(f"member {member_id}: billing {summary['premium']} vs instalments {a_year}")
    assert not apart, f"{len(apart)} of {len(members)} members are told two different totals: {apart[:3]}"


def test_the_router_is_told_what_it_cannot_look_up():
    """
    「請告訴我您想前往的服務據點，我再協助您確認。」

    That is what 你們幾點上班? got in a replay of the real transcript. The desk holds no
    branch, no address, no phone number — there is no table with any of it — so the next
    turn cannot keep the promise the sentence makes. The existing rule for this branch
    forbids a figure, a proportion and an article number, and that reply carried none of
    them: it is well formed, it is polite, and it commits the desk to a lookup it will
    fail.

    The failing sentence is quoted into the rule rather than described. A rule about
    「不要承諾查不到的事」 leaves the model to decide what it can look up, which is the
    judgement it just got wrong.
    """
    from policydesk.agent.scenario import ROUTER_INSTRUCTIONS as ROUTER

    assert "請告訴我您想前往的服務據點" in ROUTER, "the rule names a shape, not the sentence"
    for holds in ("營業時間", "據點地址", "客服電話"):
        assert holds in ROUTER, f"{holds} is not named as outside what this desk can read"


def test_the_router_names_every_scenario_it_can_answer_from():
    """
    The sentence says 就這些, so the list has to be all of them.

    Written by hand it named seven of eighteen — 據實說明, 職業變更, 契約撤銷,
    理賠進度 and 保單健診 were missing. A customer whose wording missed one of those
    scenarios lands on this branch and is told the desk cannot answer, which is the
    failure the sentence exists to prevent, reached through the scenarios it forgot.
    """
    from policydesk.agent.scenario import CATALOGUE
    from policydesk.agent.scenario import ROUTER_INSTRUCTIONS as ROUTER

    absent = [s.display_name for s in CATALOGUE if s.tools and s.display_name not in ROUTER]
    assert not absent, f"the desk can answer these and the router's list does not say so: {absent}"


@pytest.mark.parametrize(
    ("summary", "expect", "reject"),
    [
        ({"active": 5, "premium": 74106, "no_schedule": 0, "uncosted": 0}, "74,106", "（"),
        ({"active": 5, "premium": 60000, "no_schedule": 2, "uncosted": 0}, "2 張查無繳費紀錄", "未計入"),
        ({"active": 5, "premium": 60000, "no_schedule": 0, "uncosted": 1}, "1 張查不到費率", "費率估算"),
        ({"active": 5, "premium": 60000, "no_schedule": 2, "uncosted": 1}, "；", None),
    ],
)
def test_the_billing_total_says_which_policies_it_could_not_price(summary, expect, reject):
    """
    A figure the customer reads as their bill has to say what it left out.

    Three states and they are not the same claim. Instalments are what the policy bills.
    A rate card is an estimate, and the reply has said so since the day the two figures
    disagreed by six dollars. A policy with neither can only contribute nothing — and
    counting it among the estimated ones says the desk priced it at zero, which is the
    same overclaim in a smaller place.
    """
    from policydesk.agent.executor import _render
    from policydesk.agent.scenario import BILLING

    reply = _render(BILLING, {"billing_summary": summary})
    assert expect in reply
    if reject is not None:
        assert reject not in reply
