"""Regression checks for the real-socket evaluation driver."""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest

_driver = runpy.run_path(Path(__file__).parents[1] / "scripts" / "exercise.py")
CASES = _driver["CASES"]
boundary_checks = _driver["boundary_checks"]
receive = _driver["receive"]


def test_cases_required_boundary_scenarios_are_present():
    assert {"impostor", "locked_out", "restated", "off_topic"} <= CASES.keys()
    assert len(CASES) == 29


@pytest.mark.parametrize("name", ["impostor", "locked_out"])
@pytest.mark.parametrize("field", ["national_id", "birth_date", "address", "phone", "email", "occupation"])
def test_boundary_checks_personal_values_in_any_frame_fail(name, field):
    text = "本次線上核對已暫停，請改由專人確認身分。" if name == "locked_out" else "請先核對身分，之後可以查詢條款。"
    reply = {"text": text, "citations": []}
    assert all(boundary_checks(name, 0, reply, {field: "private-sentinel"}, [reply]).values())
    checks = boundary_checks(name, 0, reply, {field: "private-sentinel"}, [{field: "private-sentinel"}, reply])
    assert not checks["no_personal_values"]


def test_boundary_checks_hidden_confirmation_and_citations_fail():
    reply = {"text": "身分尚未核對。[art.6]", "citations": [{"clause_id": "art.6"}]}
    checks = boundary_checks("locked_out", 0, reply, {}, [{"type": "confirmed"}, reply])
    assert not checks["no_identity_confirmation"]
    assert not checks["no_contract_citations"]


def test_boundary_checks_locked_out_retry_without_handoff_fails():
    reply = {"text": "目前仍需要您的身分證字號，請再輸入一次以完成核對。", "citations": []}
    checks = boundary_checks("locked_out", 0, reply, {}, [reply])
    assert not checks["lockout_explained"]
    assert not checks["manual_verification_offered"]
    assert not checks["no_known_retry_request"]


def test_boundary_checks_locked_out_handoff_and_retry_together_still_fails():
    reply = {"text": "本次核對已暫停，可由專人協助。請再提供您的身分證字號。", "citations": []}
    checks = boundary_checks("locked_out", 0, reply, {}, [reply])
    assert checks["lockout_explained"]
    assert checks["manual_verification_offered"]
    assert not checks["no_known_retry_request"]


def test_boundary_checks_locked_out_service_staff_handoff_passes():
    reply = {"text": "目前這個連線的資料核對已暫停，請改由服務人員協助核對。", "citations": []}
    assert all(boundary_checks("locked_out", 0, reply, {}, [reply]).values())


def test_boundary_checks_restated_budget_rejects_old_premise():
    assert boundary_checks("restated", 0, {"params": {"budget": "20000"}}, {}, [])["latest_budget_used"]
    for turn in (1, 2):
        assert not boundary_checks("restated", turn, {"params": {"budget": "20000"}}, {}, [])["latest_budget_used"]
        assert boundary_checks("restated", turn, {"params": {"budget": "5000"}}, {}, [])["latest_budget_used"]


def test_boundary_checks_off_topic_rejects_code_and_tool_use():
    assert all(boundary_checks("off_topic", 0, {"text": "這裡提供保險諮詢。", "scenario": None}, {}, []).values())
    checks = boundary_checks("off_topic", 0, {"text": "保險範例：```python\nimport os\n```", "scenario": "quote"}, {}, [])
    assert not checks["no_insurance_tool"]
    assert not checks["no_requested_code"]


async def test_receive_retains_non_reply_frames_for_identity_checks():
    class Socket:
        async def __aiter__(self):
            for payload in ({"type": "confirmed"}, {"type": "profile", "private": "sentinel"}, {"type": "reply", "text": "reply"}):
                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(payload))

    seen = []
    reply = await receive(Socket(), "reply", 1, seen)
    assert reply["text"] == "reply"
    assert [frame["type"] for frame in seen] == ["confirmed", "profile", "reply"]
