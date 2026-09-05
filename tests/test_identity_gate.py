"""
資料核對: the check that runs before this desk discusses anyone's contracts.

It is not the 投保身分驗證 that `identity_check` holds. That one runs once, against the
government mock, at the signing stage, and stays valid for the case. This one proves the
person on *this connection* is the customer, and it expires with the connection.
"""

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from msgspec import json

from conftest import connected_database
from policydesk.agent import executor, tools
from policydesk.agent.scenario import CATALOGUE, IDENTITY_PENDING
from policydesk.gov.identity import Sex, issue
from policydesk.llm.provider import Completion, Phase
from policydesk.web import server
from policydesk.web.server import _answer as answer_customer
from policydesk.web.session import Registry

SERVER = Path("src/policydesk/web/server.py").read_text()
EXECUTOR = Path("src/policydesk/agent/executor.py").read_text()


class _CustomerSocket:
    def __init__(self, messages):
        self.messages = messages
        self.sent = []
        self.closed = False

    async def __aiter__(self):
        for message in self.messages:
            yield json.encode(message).decode()

    async def send(self, message):
        self.sent.append(json.decode(message))

    async def close(self):
        self.closed = True


@pytest.fixture
def customer_handler(monkeypatch):
    members = {
        name: {"member_id": number, "national_id": issue(Sex.FEMALE, number), "sex": "female",
               "birth_date": date(1985, 3, 12), "occupation": "PRIVATE_OCCUPATION", "occupation_class": 1}
        for number, name in enumerate(("fixture-owner", "fixture-other"), 1)
    }
    db = AsyncMock()
    db.fetch_one.side_effect = lambda sql, params: members.get(params[0])
    db.fetch_val.return_value = members["fixture-owner"]["national_id"]
    monkeypatch.setattr(server.cmd, "open_case", AsyncMock(side_effect=lambda db, member: SimpleNamespace(case_id=member)))
    monkeypatch.setattr(server, "_last_message", AsyncMock(return_value=19))
    monkeypatch.setattr(server, "_next_serial", AsyncMock(return_value=3))
    snapshot = AsyncMock(return_value={
        "case_id": 1, "member_id": 1, "national_id": members["fixture-owner"]["national_id"],
        "policies": [{"policy_number": "PRIVATE_POLICY", "sum_insured": 234567}],
        "audit": [{"detail": {"private": "PRIVATE_AUDIT"}}],
    })
    monkeypatch.setattr(server.cmd, "snapshot", snapshot)
    answer = AsyncMock()
    monkeypatch.setattr(server, "_answer", answer)
    request = SimpleNamespace(app=SimpleNamespace(ctx=SimpleNamespace(db=db, registry=Registry(), desk_sockets=set())))
    return server, request, members, answer


async def test_customer_socket_unverified_returning_member_receives_no_personal_frames(customer_handler):
    server, request, members, _ = customer_handler
    socket = _CustomerSocket([{"type": "hello", "name": "fixture-owner"}])
    await server.customer_socket(request, socket)
    material = json.encode(socket.sent).decode()
    for private in (members["fixture-owner"]["national_id"], "1985-03-12", "PRIVATE_OCCUPATION", "PRIVATE_POLICY", "PRIVATE_AUDIT"):
        assert private not in material
    assert not any(frame["type"] == "case" for frame in socket.sent)
    assert socket.sent == [{"type": "profile", "name": "fixture-owner", "returning": True, "identity_required": True}]
    server.cmd.snapshot.assert_not_awaited()


async def test_customer_socket_rebinding_name_drops_previous_verification(customer_handler):
    server, request, members, answer = customer_handler
    socket = _CustomerSocket([
        {"type": "hello", "name": "fixture-owner"},
        {"type": "say", "text": members["fixture-owner"]["national_id"]},
        {"type": "hello", "name": "fixture-other"},
        {"type": "say", "text": "查我的保單"},
    ])
    await server.customer_socket(request, socket)
    assert any(frame["type"] == "confirmed" for frame in socket.sent)
    assert answer.call_args.kwargs["case_id"] == 2
    assert answer.call_args.kwargs["confirmed"] is False
    assert answer.call_args.kwargs["floor"] == 19


async def test_customer_socket_correct_identity_releases_profile_and_replays_question(customer_handler):
    server, request, members, answer = customer_handler
    socket = _CustomerSocket([
        {"type": "hello", "name": "fixture-owner"},
        {"type": "say", "text": "查我的保單"},
        {"type": "say", "text": members["fixture-owner"]["national_id"]},
    ])
    await server.customer_socket(request, socket)
    assert [frame["type"] for frame in socket.sent] == ["profile", "profile", "confirmed", "case"]
    assert socket.sent[1]["member_id"] == 1
    assert socket.sent[1]["occupation"] == "PRIVATE_OCCUPATION"
    assert socket.sent[-1]["policies"][0]["policy_number"] == "PRIVATE_POLICY"
    first, replay = answer.await_args_list
    assert first.kwargs["confirmed"] is False
    assert first.kwargs["floor"] == 19
    assert replay.kwargs == {"case_id": 1, "text": "查我的保單", "confirmed": True, "floor": 0, "identity_locked": False}


@pytest.mark.parametrize("next_name", ["fixture-owner", "fixture-other"])
async def test_customer_socket_locked_hello_preserves_lockout(customer_handler, next_name):
    server, request, members, _ = customer_handler
    socket = _CustomerSocket([
        {"type": "hello", "name": "fixture-owner"},
        *[{"type": "say", "text": issue(Sex.MALE, 9)} for _ in range(server.MAX_CONFIRM_ATTEMPTS)],
        {"type": "hello", "name": next_name},
        {"type": "say", "text": members[next_name]["national_id"]},
    ])
    await server.customer_socket(request, socket)
    assert not any(frame["type"] in {"confirmed", "case"} for frame in socket.sent)
    assert "暫停" in socket.sent[-1]["text"]
    assert request.app.ctx.db.fetch_val.await_count == server.MAX_CONFIRM_ATTEMPTS
    assert not socket.closed, "repeating hello must not evict its own socket"
    server.cmd.snapshot.assert_not_awaited()


async def test_customer_socket_locked_question_passes_lockout_to_answer(customer_handler):
    server, request, _, answer = customer_handler
    socket = _CustomerSocket([
        {"type": "hello", "name": "fixture-owner"},
        *[{"type": "say", "text": issue(Sex.MALE, 9)} for _ in range(server.MAX_CONFIRM_ATTEMPTS)],
        {"type": "say", "text": "那現在可以幫我查什麼？"},
    ])
    await server.customer_socket(request, socket)
    assert answer.await_count == 1
    assert answer.call_args.kwargs["identity_locked"] is True
    assert answer.call_args.kwargs["confirmed"] is False
    assert answer.call_args.kwargs["floor"] == 19
    assert not any(frame["type"] in {"confirmed", "case"} for frame in socket.sent)


async def test_customer_socket_rename_discards_previous_pending_question(customer_handler):
    server, request, members, answer = customer_handler
    request.app.ctx.db.fetch_val.return_value = members["fixture-other"]["national_id"]
    socket = _CustomerSocket([
        {"type": "hello", "name": "fixture-owner"},
        {"type": "say", "text": "前一人的私人問題"},
        {"type": "hello", "name": "fixture-other"},
        {"type": "say", "text": members["fixture-other"]["national_id"]},
    ])
    await server.customer_socket(request, socket)
    assert answer.await_count == 1
    assert socket.sent[-1]["text"].endswith("請問需要什麼協助？")
    assert next(frame for frame in socket.sent if frame.get("member_id"))["member_id"] == 2


async def test_customer_socket_rename_to_new_member_allows_enrol_without_old_case(customer_handler, monkeypatch):
    server, request, members, _ = customer_handler
    enrol = AsyncMock(return_value=(3, 2))
    monkeypatch.setattr(server, "enrol", enrol)
    socket = _CustomerSocket([
        {"type": "hello", "name": "fixture-owner"},
        {"type": "say", "text": members["fixture-owner"]["national_id"]},
        {"type": "hello", "name": "fixture-new"},
        {"type": "enrol", "sex": "female", "age": 35, "occupation": "內勤行政"},
    ])
    await server.customer_socket(request, socket)
    assert socket.sent[-2]["type"] == "draft"
    assert socket.sent[-1]["type"] == "profile"
    assert socket.sent[-1]["member_id"] == 3
    assert socket.sent[-1]["national_id"] == enrol.call_args.args[0].national_id
    assert socket.sent[-1]["policies"] == 2
    assert server.cmd.snapshot.await_count == 1, "new enrol must not inherit old confirmation"


@pytest.mark.parametrize("command", ["upload", "verify"])
@pytest.mark.parametrize("identity_locked", [False, True])
async def test_customer_socket_unverified_mutation_is_denied(customer_handler, monkeypatch, command, identity_locked):
    server, request, _, _ = customer_handler
    upload = AsyncMock()
    verification = AsyncMock()
    monkeypatch.setattr(server.cmd, "upload_document", upload)
    monkeypatch.setattr(server.cmd, "verify_identity", verification)
    socket = _CustomerSocket([
        {"type": "hello", "name": "fixture-owner"},
        *([{"type": "say", "text": issue(Sex.MALE, 9)}] * server.MAX_CONFIRM_ATTEMPTS if identity_locked else []),
        {"type": command, "document_id": 22, "filename": "signed.pdf", "national_id": issue(Sex.MALE, 9)},
    ])
    await server.customer_socket(request, socket)
    expected = server.IDENTITY_LOCKED_REPLY if identity_locked else "請先完成身分核對。"
    assert socket.sent[-1] == {"type": "notice", "text": expected, "level": "warn"}
    assert request.app.ctx.db.execute.await_count == (server.MAX_CONFIRM_ATTEMPTS if identity_locked else 0)
    assert request.app.ctx.db.fetch_one.await_count == 1
    upload.assert_not_awaited()
    verification.assert_not_awaited()
    server.cmd.snapshot.assert_not_awaited()


@pytest.mark.parametrize("owned", [False, True])
async def test_customer_socket_upload_passes_confirmed_case_to_core_command(customer_handler, monkeypatch, owned):
    server, request, members, _ = customer_handler
    db = request.app.ctx.db
    upload = AsyncMock(return_value=(
        server.cmd.Refusal(reason="尚有文件待簽署", missing=("fixture document",)) if owned
        else server.cmd.Refusal(reason="無法處理這份文件。")
    ))
    monkeypatch.setattr(server.cmd, "upload_document", upload)
    socket = _CustomerSocket([
        {"type": "hello", "name": "fixture-owner"},
        {"type": "say", "text": members["fixture-owner"]["national_id"]},
        {"type": "upload", "case_id": 999, "document_id": 22, "filename": "signed.pdf"},
    ])
    await server.customer_socket(request, socket)
    upload.assert_awaited_once_with(db, 1, document_id=22, filename="signed.pdf")
    if owned:
        assert socket.sent[-1]["type"] == "case"
    else:
        assert socket.sent[-1] == {"type": "notice", "text": "無法處理這份文件。", "level": "warn"}


@pytest.mark.parametrize("remaining", [(), ("健康告知書",)])
async def test_customer_socket_upload_accepted_runs_document_guidance(customer_handler, monkeypatch, remaining):
    server, request, members, answer = customer_handler
    outcome = server.cmd.Refusal("尚有文件", remaining) if remaining else SimpleNamespace()
    monkeypatch.setattr(server.cmd, "upload_document", AsyncMock(return_value=outcome))
    socket = _CustomerSocket([
        {"type": "hello", "name": "fixture-owner"},
        {"type": "say", "text": members["fixture-owner"]["national_id"]},
        {"type": "upload", "case_id": 999, "document_id": 22, "filename": "demo.pdf"},
    ])
    await server.customer_socket(request, socket)
    answer.assert_awaited_once()
    assert answer.call_args.kwargs["document_event"] is True
    assert answer.call_args.kwargs["case_id"] == 1
    assert answer.call_args.kwargs["confirmed"] is True


async def test_answer_document_event_does_not_invent_customer_speech(customer_handler, monkeypatch):
    server, request, _, _ = customer_handler
    request.app.ctx.provider = AsyncMock()
    request.app.ctx.clauses = None
    turn = executor.Turn(1, 1)
    turn.reply = "模擬簽署紀錄已更新。"
    turn.scenario = "document_progress"
    runner = AsyncMock(return_value=turn)
    monkeypatch.setattr(server, "run_turn", runner)
    monkeypatch.setattr(server.lang, "resolve", AsyncMock(return_value=("und", "zh-TW")))
    monkeypatch.setattr(server.i18n, "translate", AsyncMock(return_value=[]))
    monkeypatch.setattr(server, "cited", AsyncMock(return_value=[]))
    db = request.app.ctx.db
    await answer_customer(request, _CustomerSocket([]), db, case_id=1, text="", confirmed=True, document_event=True)
    sql = [call.args[0] for call in db.execute.await_args_list]
    assert not any("'customer'" in statement for statement in sql)
    assert any("'agent'" in statement for statement in sql)
    assert runner.call_args.kwargs["document_event"] is True


@pytest.mark.parametrize("previous_scenario", [None, "policy_overview", "document_progress"])
async def test_customer_socket_confirmation_replays_question_then_guides_documents(customer_handler, previous_scenario):
    server, request, members, answer = customer_handler
    answer.return_value = SimpleNamespace(scenario=previous_scenario)
    db = request.app.ctx.db
    db.fetch_val.side_effect = lambda sql, params: (
        "issued" if "SELECT stage" in sql else members["fixture-owner"]["national_id"]
    )
    socket = _CustomerSocket([
        {"type": "hello", "name": "fixture-owner"},
        {"type": "say", "text": "你好"},
        {"type": "say", "text": members["fixture-owner"]["national_id"]},
    ])
    await server.customer_socket(request, socket)
    calls = [call.kwargs for call in answer.await_args_list]
    assert calls[1]["text"] == "你好"
    assert calls[1]["confirmed"]
    if previous_scenario == "document_progress":
        assert len(calls) == 2
    else:
        assert len(calls) == 3
        assert calls[2]["document_event"] is True


async def test_customer_socket_wrong_sample_guides_without_claiming_success(customer_handler, monkeypatch):
    server, request, members, answer = customer_handler
    refusal = server.cmd.Refusal("示範規則檢查：空白便條紙不符合要保書，未記錄模擬簽署。")
    upload = AsyncMock(return_value=refusal)
    monkeypatch.setattr(server.cmd, "upload_document", upload)
    socket = _CustomerSocket([
        {"type": "hello", "name": "fixture-owner"},
        {"type": "say", "text": members["fixture-owner"]["national_id"]},
        {"type": "upload", "case_id": 999, "document_id": 22, "sample": "mismatched"},
    ])
    await server.customer_socket(request, socket)
    upload.assert_awaited_once_with(request.app.ctx.db, 1, document_id=22, sample="mismatched")
    assert answer.call_args.kwargs["document_event"] is True
    assert answer.call_args.kwargs["text"] == refusal.reason
    assert any(frame.get("level") == "warn" for frame in socket.sent)


async def test_push_case_without_confirmation_reads_and_sends_nothing(customer_handler):
    server, request, _, _ = customer_handler
    socket = _CustomerSocket([])
    await server._push_case(request.app.ctx.db, socket, request.app, 1)
    assert not socket.sent
    server.cmd.snapshot.assert_not_awaited()


@pytest.mark.parametrize("identity_locked", [False, True])
async def test_answer_public_reply_does_not_push_private_case(customer_handler, monkeypatch, identity_locked):
    server, request, _, _ = customer_handler
    request.app.ctx.provider = object()
    request.app.ctx.clauses = None
    turn = server.Turn(1, 1)
    turn.reply = "可以先了解公開商品資訊。"
    run_turn = AsyncMock(return_value=turn)
    monkeypatch.setattr(server, "run_turn", run_turn)
    monkeypatch.setattr(server.lang, "resolve", AsyncMock(return_value=("zh-TW", "zh-TW")))
    monkeypatch.setattr(server.i18n, "translate", AsyncMock(return_value=[]))
    monkeypatch.setattr(server, "cited", AsyncMock(return_value=[]))
    socket = _CustomerSocket([])
    await answer_customer(
        request, socket, request.app.ctx.db, case_id=1, text="有什麼商品",
        confirmed=False, floor=19, identity_locked=identity_locked,
    )
    assert [frame["type"] for frame in socket.sent] == ["reply"]
    assert socket.sent[0]["text"] == turn.reply
    assert run_turn.call_args.kwargs["confirmed"] is False
    assert run_turn.call_args.kwargs["identity_locked"] is identity_locked
    assert run_turn.call_args.kwargs["since"] == 19
    server.cmd.snapshot.assert_not_awaited()


@pytest.mark.parametrize("scenario_name", ["policy_overview", "browse_products"])
async def test_run_turn_locked_session_reaches_both_model_phases_without_personal_reads(monkeypatch, scenario_name):
    monkeypatch.setattr(executor.memory, "recent", AsyncMock(return_value=[]))
    monkeypatch.setattr(executor.memory, "card", AsyncMock())
    monkeypatch.setattr(executor.tools, "standing_brief", AsyncMock())
    monkeypatch.setattr(executor.statute, "unresolved", AsyncMock(return_value=[]))
    provider, db = AsyncMock(), AsyncMock()
    db.fetch_val.return_value = "inquiry"
    db.fetch.return_value = [{"product_id": "public-product", "name": "公開商品樣本"}]
    provider.complete.side_effect = [
        Completion(text="", provider="test", tool_calls=({
            "name": scenario_name, "arguments": '{"line":"life"}',
        },)),
        Completion(text='{"reply":"可由專人協助，公開資訊仍可查詢。","citations":[],"calculations":[]}', provider="test"),
    ]
    turn = await executor.run_turn(
        provider, db, case_id=1, member_id=1, text="這次先幫我說明可查的資料",
        confirmed=False, identity_locked=True, locale="zh-TW",
    )
    assert turn.scenario == scenario_name
    assert provider.complete.await_count == 2
    for call in provider.complete.call_args_list:
        assert "# Identity verification state: locked" in call.kwargs["user_input"]
        assert "request no identity information" in call.kwargs["user_input"]
    executor.memory.card.assert_not_awaited()
    executor.tools.standing_brief.assert_not_awaited()
    if scenario_name == "browse_products":
        db.fetch.assert_awaited_once()
        assert "FROM sale_catalog" in db.fetch.call_args.args[0]
        assert "公開商品樣本" in provider.complete.call_args.kwargs["user_input"]
    else:
        db.fetch.assert_not_awaited()
    assert not turn.faults


async def test_run_turn_locked_private_template_refers_to_staff_without_requesting_identity(monkeypatch):
    monkeypatch.setattr(executor.memory, "recent", AsyncMock(return_value=[]))
    provider, db = AsyncMock(), AsyncMock()
    db.fetch_val.return_value = "inquiry"
    provider.complete.return_value = Completion(text="", provider="test", tool_calls=({
        "name": "coverage", "arguments": "{}",
    },))
    turn = await executor.run_turn(
        provider, db, case_id=1, member_id=1, text="幫我查目前保額",
        confirmed=False, identity_locked=True, locale="zh-TW",
    )
    assert turn.scenario == "coverage"
    assert "暫停" in turn.reply
    assert "專人" in turn.reply
    assert "請提供" not in turn.reply
    assert "身分證字號" not in turn.reply
    assert provider.complete.await_count == 1
    db.fetch.assert_not_awaited()


@pytest.fixture
async def ownership_members():
    """Create isolated records in the real DB; remove them even after an assertion fails."""
    async with connected_database() as db:
        member_ids, case_ids, policy_ids, claim_ids = [], [], [], []
        members = []
        nonce = uuid4().hex
        try:
            before = await db.fetch_val("SELECT count(*) FROM policy")
            product_id = await db.fetch_val(
                "SELECT product_id FROM product WHERE attachment = 'main' AND document_kind = 'contract' ORDER BY product_id LIMIT 1"
            )
            assert product_id is not None, "ownership integration requires an existing contract product"
            for ordinal in range(2):
                name = f"auth-{nonce}-{ordinal}"
                national_id = issue(Sex.FEMALE, (int(nonce, 16) + ordinal) % 10_000_000, "Z")
                assert not await db.fetch_val("SELECT EXISTS(SELECT 1 FROM member WHERE national_id = $1::text)", [national_id])
                member_id = await db.fetch_val(
                    """INSERT INTO member (display_name, national_id, sex, birth_date, occupation, occupation_class,
                           address_city, address_district, address_rest, phone, email, marital_status, income_band,
                           beneficiary_relation)
                       VALUES ($1::text, $2::text, 'female', '1986-01-01', '內勤行政', 1,
                               '測試', '測試', '測試', '0000000000', 'fixture@example.invalid', 'single', 'medium', 'legal_heir')
                       RETURNING member_id""",
                    [name, national_id],
                )
                member_ids.append(member_id)
                case_id = (await server.cmd.open_case(db, member_id)).case_id
                case_ids.append(case_id)
                member = {"member_id": member_id, "case_id": case_id, "name": name, "national_id": national_id,
                          "policy_ids": [], "claim_ids": [], "numbers": [], "beneficiaries": []}
                members.append(member)
                for contract in range(2):
                    number = f"AUTH-{nonce}-{ordinal}-{contract}"
                    policy_id = await db.fetch_val(
                        """INSERT INTO policy (member_id, product_id, policy_number, sum_insured, effective_at)
                           VALUES ($1::bigint, $2::text, $3::text, 1000, '2025-01-01') RETURNING policy_id""",
                        [member_id, product_id, number],
                    )
                    policy_ids.append(policy_id)
                    member["policy_ids"].append(policy_id)
                    member["numbers"].append(number)
                    beneficiary = f"受益人-{nonce}-{ordinal}-{contract}"
                    member["beneficiaries"].append(beneficiary)
                    await db.execute(
                        """INSERT INTO policy_beneficiary (policy_id, display_name, relation, share, designated_at)
                           VALUES ($1::bigint, $2::text, 'parent', 100, '2025-01-01')""",
                        [policy_id, beneficiary],
                    )
                    claim_id = await db.fetch_val(
                        """INSERT INTO claim (policy_id, kind, event_at, filed_at, stage)
                           VALUES ($1::bigint, 'hospital', '2026-01-01', '2026-01-02', 'assessing') RETURNING claim_id""",
                        [policy_id],
                    )
                    claim_ids.append(claim_id)
                    member["claim_ids"].append(claim_id)
            yield db, members
        finally:
            if member_ids:
                # llm_usage uses SET NULL on case deletion, unlike the cascading fixture records.
                await db.execute(
                    'DELETE FROM llm_usage WHERE case_id IN (SELECT case_id FROM "case" WHERE member_id = ANY($1::bigint[]))',
                    [member_ids],
                )
                await db.execute("DELETE FROM member WHERE member_id = ANY($1::bigint[])", [member_ids])
                remaining = await db.fetch_val(
                    """SELECT (SELECT count(*) FROM member WHERE member_id = ANY($1::bigint[]))
                            + (SELECT count(*) FROM policy WHERE policy_id = ANY($2::bigint[]))
                            + (SELECT count(*) FROM claim WHERE claim_id = ANY($3::bigint[]))
                            + (SELECT count(*) FROM policy_beneficiary WHERE policy_id = ANY($2::bigint[]))
                            + (SELECT count(*) FROM "case" WHERE case_id = ANY($4::bigint[]))
                            + (SELECT count(*) FROM conversation_message WHERE case_id = ANY($4::bigint[]))
                            + (SELECT count(*) FROM llm_usage WHERE case_id = ANY($4::bigint[]))""",
                    [member_ids, policy_ids, claim_ids, case_ids],
                )
                assert remaining == 0, "ownership fixture records survived cleanup"
                assert await db.fetch_val("SELECT count(*) FROM policy") == before


@pytest.mark.parametrize("owner_index", [0, 1], ids=["member_a", "member_b"])
async def test_customer_socket_verified_queries_return_only_owners_records(ownership_members, monkeypatch, owner_index):
    """Verify real identity/SQL routing in both directions; only model and socket I/O are scripted."""
    db, members = ownership_members
    owner, other = members[owner_index], members[1 - owner_index]
    scenarios = ("policy_overview", "claim_status", "beneficiary")
    routes = iter(scenarios)
    gathered = []

    async def complete(*, phase, user_input, **kwargs):
        if phase is Phase.ROUTE:
            return Completion(text="", tool_calls=({"name": next(routes), "arguments": "{}"},),
                              provider="scripted", model="ownership-test")
        assert phase is Phase.ANSWER
        # Echo real query values only after checking that they reached the model input.
        facts = gathered[-1]
        rows = next(facts[key] for key in ("member_claims", "current_beneficiary", "list_policies") if key in facts)
        values = [row["policy_number"] for row in rows]
        values.extend(person["name"] for row in rows for person in row.get("beneficiaries", []))
        assert all(value in user_input for value in values)
        return Completion(
            text=json.encode({"reply": "查詢結果：" + "、".join(values), "citations": [], "calculations": [], "quoted_fields": []}).decode(),
            provider="scripted", model="ownership-test",
        )

    provider = SimpleNamespace(complete=AsyncMock(side_effect=complete))
    request = SimpleNamespace(app=SimpleNamespace(ctx=SimpleNamespace(
        db=db, registry=Registry(), desk_sockets=set(), provider=provider, clauses=None,
    )))
    gather = executor._gather

    async def capture_gather(*args, **kwargs):
        facts = await gather(*args, **kwargs)
        gathered.append(facts)
        return facts

    monkeypatch.setattr(executor, "_gather", capture_gather)
    socket = _CustomerSocket([
        {"type": "hello", "name": owner["name"]},
        {"type": "say", "text": owner["national_id"]},
        {"type": "say", "text": "請列出我的保單"},
        {"type": "say", "text": "我的理賠進度如何"},
        {"type": "say", "text": "各張保單的受益人是誰"},
    ])
    await server.customer_socket(request, socket)
    assert any(frame["type"] == "confirmed" for frame in socket.sent)
    assert len(gathered) == len(scenarios)
    replies = [frame for frame in socket.sent if frame.get("scenario") in scenarios]
    assert [frame["scenario"] for frame in replies] == list(scenarios)
    assert all(not frame["faults"] for frame in replies)
    assert all(frame["member_id"] == owner["member_id"] for frame in socket.sent if "member_id" in frame)

    policy_groups = [facts["list_policies"] for facts in gathered if "list_policies" in facts]
    assert len(policy_groups) == 2, "overview and beneficiary tools must each return policies"
    cases = [frame for frame in socket.sent if frame["type"] == "case"]
    assert len(cases) == len(scenarios) + 1
    assert {frame["case_id"] for frame in cases} == {owner["case_id"]}
    confirmations = await db.fetch(
        """SELECT c.member_id FROM audit_event a JOIN "case" c USING (case_id)
           WHERE a.case_id = $1::bigint AND a.action = 'identity_confirmed'""",
        [owner["case_id"]],
    )
    assert confirmations == [{"member_id": owner["member_id"]}]
    policy_groups.extend(frame["policies"] for frame in cases)
    claims = gathered[1]["member_claims"]
    beneficiaries = gathered[2]["current_beneficiary"]
    for rows in (*policy_groups, beneficiaries):
        assert rows, "an empty result does not prove ownership"
        returned_ids = {row["policy_id"] for row in rows}
        actual = await db.fetch("SELECT policy_id, member_id FROM policy WHERE policy_id = ANY($1::bigint[])", [list(returned_ids)])
        assert len(actual) == len(returned_ids)
        assert {row["member_id"] for row in actual} == {owner["member_id"]}, "returned policy belongs to another member"
        assert returned_ids == set(owner["policy_ids"])
    assert claims, "an empty claim result does not prove ownership"
    actual_claims = await db.fetch(
        "SELECT c.claim_id, p.member_id FROM claim c JOIN policy p USING (policy_id) WHERE c.claim_id = ANY($1::bigint[])",
        [[row["claim_id"] for row in claims]],
    )
    assert len(actual_claims) == len(claims)
    assert {row["member_id"] for row in actual_claims} == {owner["member_id"]}, "returned claim belongs to another member"
    assert {row["claim_id"] for row in claims} == set(owner["claim_ids"])
    assert {person["name"] for row in beneficiaries for person in row["beneficiaries"]} == set(owner["beneficiaries"])
    actual_beneficiaries = await db.fetch(
        """SELECT pb.policy_id, p.member_id, pb.display_name, pb.relation, pb.share
           FROM policy_beneficiary pb JOIN policy p USING (policy_id)
           WHERE pb.policy_id = ANY($1::bigint[])""",
        [[row["policy_id"] for row in beneficiaries]],
    )
    assert actual_beneficiaries
    assert {row["member_id"] for row in actual_beneficiaries} == {owner["member_id"]}
    assert {
        (row["policy_id"], person["name"], person["relation"], person["share"])
        for row in beneficiaries for person in row["beneficiaries"]
    } == {
        (row["policy_id"], row["display_name"], row["relation"], row["share"])
        for row in actual_beneficiaries
    }
    for rows in (*policy_groups, claims, beneficiaries):
        assert {row["policy_number"] for row in rows} == set(owner["numbers"])
    for frame in replies:
        assert all(number in frame["text"] for number in owner["numbers"]), "reply omitted the owner's query results"
    material = str(gathered) + json.encode(socket.sent).decode()
    material += "".join(call.kwargs["user_input"] for call in provider.complete.call_args_list)
    assert not any(secret in material for secret in (*other["numbers"], *other["beneficiaries"], other["national_id"]))


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
async def test_no_scenario_returns_a_value_out_of_the_members_own_row(db):
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


async def test_a_desk_pane_receives_only_its_own_members_snapshots():
    """
    A case snapshot reaches the pane scoped to that member and no other.

    `desk_socket` read `?member=` at connect and then stored the bare socket, so
    `_broadcast_desk` sent every snapshot to every pane. A snapshot carries
    `national_id`, `display_name`, `occupation` and the member's whole policy book,
    and the browser renders it with no check of its own — two visitors with the demo
    open at once was enough for one confirming their identity to push their record
    into the other's pane. The `open` branch in the same handler already compared
    `member_id` against the viewer; this exit did not.
    """
    from types import SimpleNamespace

    from policydesk.web.server import _broadcast_desk

    class Pane:
        def __init__(self) -> None:
            self.received: list[str] = []

        async def send(self, body: str) -> None:
            self.received.append(body)

    owner, stranger = Pane(), Pane()
    application = SimpleNamespace(ctx=SimpleNamespace(desk_sockets={(owner, 101), (stranger, 202)}))
    await _broadcast_desk(application, {"type": "case", "member_id": 101, "national_id": "A123456789"})

    assert len(owner.received) == 1
    assert stranger.received == [], "a snapshot reached a pane scoped to a different member"
