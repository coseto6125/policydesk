"""
The Anthropic seam, tested without calling the API.

Everything here is the translation either side of the request: the Responses-shaped
tools and schemas the executor already builds, rewritten into the dialect Anthropic
accepts, and the reply read back into the shape every caller above this module reads.
The API itself is exercised by running the desk.
"""

import json as stdlib_json
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import anthropic
import pytest
from anthropic.types import Message

from policydesk.llm.oauth import OAuthCredentialError, SubscriptionOAuthToken
from policydesk.llm.provider import (
    AnthropicProvider,
    Phase,
    ProviderError,
    _anthropic_schema,
    _anthropic_tool,
    build_provider,
)


def _message(content: list[dict], *, usage: dict | None = None, model: str = "claude-haiku-4-5-20251001") -> Message:
    """Build the SDK's own response object, so the reader is tested against the real shape."""
    return Message.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": model,
        "content": content, "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5} | (usage or {}),
    })


def _provider_with(response: Message | Exception) -> tuple[AnthropicProvider, AsyncMock]:
    """Return a provider whose client is a mock, and that mock's `messages.create`."""
    provider = AnthropicProvider()
    create = AsyncMock(side_effect=[response] if isinstance(response, Exception) else None)
    if not isinstance(response, Exception):
        create.return_value = response
    client = Mock()
    client.messages.create = create
    provider._open_client = Mock(return_value=client)
    return provider, create


# ------------------------------------------------------------------ schema rewrite


def test_tool_schema_becomes_an_input_schema():
    """The two dialects differ only in where the parameter schema hangs."""
    from policydesk.agent.scenario import CATALOGUE
    from policydesk.agent.scenario_base import tool_schema

    source = tool_schema(CATALOGUE[0])
    converted = _anthropic_tool(source)
    assert converted["name"] == source["name"]
    assert converted["description"] == source["description"]
    assert converted["input_schema"] == source["parameters"]
    assert "parameters" not in converted


def test_an_empty_array_becomes_a_null_at_every_depth():
    """
    Anthropic rejects `maxItems` on an array: 400 "property 'maxItems' is not supported".

    `maxItems: 0` is how `_answer_schema` and `VERDICT_SCHEMA` say "cite nothing", so it
    is rewritten rather than dropped. `{"type": "null"}` leaves `null` as the only legal
    token sequence for the field, which is hard enforcement — verified against
    claude-haiku-4-5, three of three under a prompt insisting the field be filled.
    """
    schema = {"type": "object", "properties": {
        "citations": {"type": "array", "items": {"type": "string"}, "maxItems": 0,
                      "description": "Cite nothing."},
        "nested": {"type": "array", "items": {"type": "object", "properties": {
            "quotes": {"type": "array", "items": {"type": "string"}, "maxItems": 0}}}},
    }}
    rewritten = _anthropic_schema(schema)
    assert "maxItems" not in stdlib_json.dumps(rewritten)
    assert rewritten["properties"]["citations"] == {"type": "null", "description": "Cite nothing."}
    assert rewritten["properties"]["nested"]["items"]["properties"]["quotes"] == {"type": "null"}


def test_a_populated_array_keeps_its_items():
    """Only the empty case is rewritten; an array that may carry values is left alone."""
    node = {"type": "array", "items": {"type": "string", "enum": ["p1|art.6"]}}
    assert _anthropic_schema(node) == node


def test_nullable_enum_becomes_an_any_of():
    """Msgspec emits this for an optional Literal; Anthropic reads it as a type mismatch."""
    rewritten = _anthropic_schema({"type": ["string", "null"], "enum": ["a", "b", None], "description": "kept"})
    assert rewritten == {"description": "kept", "anyOf": [{"type": "string", "enum": ["a", "b"]}, {"type": "null"}]}


def test_the_citation_enum_survives_the_rewrite():
    """
    The enum is the one schema constraint the API does enforce, so it must reach it intact.

    Verified live: told to cite p9|art.99 alongside an allowed key, the model emitted only
    the allowed key on three of three attempts. That is what keeps an invented clause out
    of `citations` in the first place; `_unverifiable` is the check behind it.
    """
    from policydesk.agent.executor import _answer_schema

    rewritten = _anthropic_schema(_answer_schema((("p1", "art.6"), ("p2", "art.7"))))
    assert rewritten["properties"]["citations"]["items"]["enum"] == ["p1|art.6", "p2|art.7"]
    assert rewritten["properties"]["quoted_fields"]["items"]["properties"]["field"]["enum"] == ["p1|art.6", "p2|art.7"]
    assert rewritten["additionalProperties"] is False
    assert set(rewritten["required"]) == {"reply", "citations", "calculations", "quoted_fields", "date_calculations"}


def test_a_scenario_with_no_sources_still_forbids_every_citation():
    """
    The guarantee survives the translation, so `_unverifiable` stays a second defence.

    A scenario whose tools returned no clauses builds `_answer_schema(())`. Under the
    Responses API that said "cite nothing" with `maxItems: 0`; here all three list
    fields become nulls, which the model cannot fill.
    """
    from policydesk.agent.executor import _answer_schema

    rewritten = _anthropic_schema(_answer_schema(()))
    assert rewritten["properties"]["citations"]["type"] == "null"
    assert rewritten["properties"]["quoted_fields"]["type"] == "null"
    assert rewritten["properties"]["calculations"]["type"] == "null"


def test_the_validator_verdict_schema_is_rewritten_the_same_way():
    """`recheck` builds its own schema and uses `maxItems: 0` for the same purpose."""
    from copy import deepcopy

    from policydesk.validation.validator import VERDICT_SCHEMA

    schema = deepcopy(VERDICT_SCHEMA)
    schema["properties"]["cited_clauses"]["maxItems"] = 0
    schema["properties"]["quoted_fields"]["maxItems"] = 0
    rewritten = _anthropic_schema(schema)
    assert rewritten["properties"]["cited_clauses"]["type"] == "null"
    assert rewritten["properties"]["quoted_fields"]["type"] == "null"


# ------------------------------------------------------- reading the nulls back


async def test_a_null_field_decodes_as_the_empty_list_the_caller_expects():
    """
    The rewrite must not reach the caller. `_Answer` types these as lists, so a raw
    `null` would raise and the turn would be withheld as a format fault — the model
    obeying the schema would look like the model breaking it.
    """
    from msgspec import json as msgspec_json

    from policydesk.agent.executor import _Answer, _answer_schema

    provider, _ = _provider_with(_message([
        {"type": "text", "text": '{"reply":"目前資料不足。","citations":null,"calculations":null,"quoted_fields":null}'},
    ]))
    result = await provider.complete(instructions="rules", user_input="question", schema=_answer_schema(()))
    answer = msgspec_json.decode(result.text.encode(), type=_Answer)
    assert (answer.citations, answer.calculations, answer.quoted_fields) == ([], [], [])


async def test_a_field_the_schema_did_not_empty_is_left_alone():
    """Only the rewritten names are repaired; a real citation list passes through."""
    from policydesk.agent.executor import _answer_schema

    payload = '{"reply":"好","citations":["p1|art.6"],"calculations":null,"quoted_fields":[]}'
    provider, _ = _provider_with(_message([{"type": "text", "text": payload}]))
    result = await provider.complete(
        instructions="rules", user_input="question", schema=_answer_schema((("p1", "art.6"),)),
    )
    assert stdlib_json.loads(result.text)["citations"] == ["p1|art.6"]
    assert stdlib_json.loads(result.text)["calculations"] == []


async def test_a_malformed_reply_is_handed_back_untouched():
    """The caller reports a format fault; repairing one here would hide it."""
    from policydesk.agent.executor import _answer_schema

    provider, _ = _provider_with(_message([{"type": "text", "text": "我覺得應該可以理賠"}]))
    result = await provider.complete(instructions="rules", user_input="question", schema=_answer_schema(()))
    assert result.text == "我覺得應該可以理賠"


# ------------------------------------------------------------------- request body


@pytest.mark.parametrize("phase", list(Phase))
async def test_no_phase_sends_a_sampling_temperature(phase):
    """
    Sampling is deliberately not forwarded here. Do not re-add it.

    `anthropic` 1.4.0's `messages.create` has no `temperature` parameter — Anthropic
    removed sampling from the models released after Haiku 4.5. Haiku 4.5 still honours
    the field on the wire, but only `extra_body` reaches it, and a customer's turn does
    not travel through an undeclared back door. The model answers at its own default.
    """
    provider, create = _provider_with(_message([{"type": "text", "text": "ok"}]))
    await provider.complete(instructions="rules", user_input="question", phase=phase)
    assert "extra_body" not in create.call_args.kwargs
    assert "temperature" not in create.call_args.kwargs


def test_the_sdk_still_has_no_temperature_parameter():
    """
    Pins the reason. When a future SDK takes `temperature` again, this fails and the
    next reader learns that forwarding `Phase.temperature` is available once more.
    """
    import inspect

    from anthropic.resources.messages import AsyncMessages

    assert "temperature" not in inspect.signature(AsyncMessages.create).parameters


def test_the_phase_temperatures_the_other_providers_read_are_untouched():
    """`Phase.temperature` stays: the HTTP and CLI paths still use it."""
    assert Phase.ANSWER.temperature == 0.3
    assert Phase.ROUTE.temperature == 0.1


async def test_the_brief_is_the_system_prompt_and_the_turn_is_the_one_user_message():
    provider, create = _provider_with(_message([{"type": "text", "text": "ok"}]))
    await provider.complete(instructions="rules", user_input="question")
    sent = create.call_args.kwargs
    assert sent["system"] == "rules"
    assert sent["messages"] == [{"role": "user", "content": "question"}]
    assert sent["model"] == "claude-haiku-4-5"


async def test_a_schema_travels_as_an_output_config_format():
    """`output_config`, not the deprecated `output_format`."""
    provider, create = _provider_with(_message([{"type": "text", "text": "{}"}]))
    await provider.complete(
        instructions="rules", user_input="question",
        schema={"type": "object", "properties": {"reply": {"type": "string"}}},
    )
    assert create.call_args.kwargs["output_config"] == {
        "format": {"type": "json_schema", "schema": {"type": "object", "properties": {"reply": {"type": "string"}}}}
    }


async def test_offered_tools_reach_the_api_as_native_tools():
    """No prompt-described tool brief here: this API takes the schemas."""
    provider, create = _provider_with(_message([{"type": "text", "text": "ok"}]))
    await provider.complete(
        instructions="rules", user_input="question",
        tools=[{"type": "function", "name": "calculate", "description": "Evaluate.",
                "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}}}],
    )
    assert create.call_args.kwargs["tools"] == [
        {"name": "calculate", "description": "Evaluate.",
         "input_schema": {"type": "object", "properties": {"expression": {"type": "string"}}}}
    ]


# ------------------------------------------------------------------ reading it back


async def test_calls_are_shaped_like_the_responses_api_returns_them():
    """Nothing above the provider knows which implementation it holds."""
    provider, _ = _provider_with(_message([
        {"type": "text", "text": "好的"},
        {"type": "tool_use", "id": "t1", "name": "explain_cover", "input": {"topic": "住院"}},
    ]))
    result = await provider.complete(instructions="rules", user_input="question")
    assert result.text == "好的"
    assert result.tool_calls == (
        {"type": "function_call", "name": "explain_cover", "arguments": '{"topic":"住院"}'},
    )


async def test_tool_arguments_are_encoded_json_the_executor_can_decode():
    """
    `_route` decodes `arguments` with msgspec, so an escaped string is what must arrive.

    The SDK hands back the parsed input object; encoding it here rather than passing the
    object through is what keeps the router's decode step the same on both providers.
    """
    from msgspec import json as msgspec_json

    provider, _ = _provider_with(_message([
        {"type": "tool_use", "id": "t1", "name": "quote", "input": {"product": "壽/險", "budget": "20000"}},
    ]))
    result = await provider.complete(instructions="rules", user_input="question")
    assert msgspec_json.decode(result.tool_calls[0]["arguments"].encode()) == {"product": "壽/險", "budget": "20000"}


async def test_the_model_recorded_is_the_dated_id_the_api_answered_with():
    """`llm_usage.model` names what actually served the turn, not what was asked for."""
    provider, _ = _provider_with(_message([{"type": "text", "text": "ok"}]))
    result = await provider.complete(instructions="rules", user_input="question")
    assert result.model == "claude-haiku-4-5-20251001"
    assert result.provider == "anthropic"


async def test_cached_tokens_are_folded_into_the_prompt_count():
    """
    `pricing.price` reads `cached_tokens` as a slice of `prompt_tokens`.

    Anthropic reports the two side by side instead, so passing `input_tokens` through
    unchanged would leave every cached token unpriced.
    """
    provider, _ = _provider_with(_message(
        [{"type": "text", "text": "ok"}],
        usage={"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 400,
               "cache_creation_input_tokens": 50},
    ))
    result = await provider.complete(instructions="rules", user_input="question")
    assert result.prompt_tokens == 550
    assert result.cached_tokens == 400
    assert result.total_tokens == 570


# ---------------------------------------------------------------------- failure


async def test_an_api_error_becomes_a_provider_error():
    """The executor catches one exception type and says the desk could not answer."""
    provider, _ = _provider_with(_message([{"type": "text", "text": "ok"}]))
    provider._open_client().messages.create.side_effect = anthropic.BadRequestError(
        "bad schema", response=Mock(status_code=400, headers={}), body=None
    )
    with pytest.raises(ProviderError, match="anthropic api 400"):
        await provider.complete(instructions="rules", user_input="question")


async def test_an_unreadable_credential_file_becomes_a_provider_error():
    """A desk with no credentials says it is down; it never answers anyway."""
    provider = AnthropicProvider()
    provider._open_client = Mock(side_effect=OAuthCredentialError("cannot read /nowhere"))
    with pytest.raises(ProviderError, match="credentials unreadable"):
        await provider.complete(instructions="rules", user_input="question")


def test_the_anthropic_provider_closes_like_the_others():
    """The server closes whichever provider it built."""
    assert hasattr(AnthropicProvider, "close")


# ------------------------------------------------------------------- credentials


def _write_creds(path: Path, *, expires_in_s: float) -> None:
    path.write_text(stdlib_json.dumps(
        {"claudeAiOauth": {"accessToken": f"sk-ant-oat01-{expires_in_s}", "expiresAt": int((time.time() + expires_in_s) * 1000)}}
    ))


def test_the_credential_path_honours_the_environment_override(monkeypatch, tmp_path):
    """A host nobody is signed in on reaches its synced token file through this."""
    creds = tmp_path / "synced.json"
    _write_creds(creds, expires_in_s=3600)
    monkeypatch.setenv("ANTHROPIC_OAUTH_CREDS_PATH", str(creds))
    assert SubscriptionOAuthToken.available()
    assert SubscriptionOAuthToken().token() == "sk-ant-oat01-3600"


def test_a_token_near_expiry_is_re_read_rather_than_refreshed(monkeypatch, tmp_path):
    """
    Freshness is Claude Code's job. Anthropic's refresh tokens are single-use rotating,
    so refreshing here without writing the rotated one back signs Claude Code out.
    """
    creds = tmp_path / "creds.json"
    _write_creds(creds, expires_in_s=60)  # inside the 300s skew, so every read re-opens the file
    monkeypatch.setenv("ANTHROPIC_OAUTH_CREDS_PATH", str(creds))
    reader = SubscriptionOAuthToken()
    assert reader.token() == "sk-ant-oat01-60"
    _write_creds(creds, expires_in_s=3600)
    assert reader.token() == "sk-ant-oat01-3600"


def test_a_valid_token_is_cached_rather_than_re_read(monkeypatch, tmp_path):
    creds = tmp_path / "creds.json"
    _write_creds(creds, expires_in_s=3600)
    monkeypatch.setenv("ANTHROPIC_OAUTH_CREDS_PATH", str(creds))
    reader = SubscriptionOAuthToken()
    assert reader.token() == "sk-ant-oat01-3600"
    creds.unlink()
    assert reader.token() == "sk-ant-oat01-3600"


def test_reading_the_credentials_never_writes_them(monkeypatch, tmp_path):
    creds = tmp_path / "creds.json"
    _write_creds(creds, expires_in_s=60)
    monkeypatch.setenv("ANTHROPIC_OAUTH_CREDS_PATH", str(creds))
    before = (creds.read_bytes(), creds.stat().st_mtime_ns)
    reader = SubscriptionOAuthToken()
    for _ in range(3):
        reader.token()
    assert (creds.read_bytes(), creds.stat().st_mtime_ns) == before


def test_the_credential_module_never_calls_the_refresh_endpoint():
    """
    A red line the type system cannot hold: this module reads and does nothing else.

    Read from the source rather than mocked, for the same reason the codex provider's
    sandbox flags are — the failure is a line somebody adds later, not a branch.
    """
    source = Path("src/policydesk/llm/oauth.py").read_text()
    assert "refreshToken" not in source
    assert "oauth/token" not in source
    for writer in ("write_bytes", "write_text", "open(", "requests", "aiohttp", "httpx"):
        assert writer not in source, f"{writer} has no place in a read-only credential reader"


def test_a_missing_credential_file_is_reported_not_guessed(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_OAUTH_CREDS_PATH", str(tmp_path / "absent.json"))
    assert not SubscriptionOAuthToken.available()
    with pytest.raises(OAuthCredentialError, match="cannot read"):
        SubscriptionOAuthToken().token()


def test_a_malformed_credential_file_is_reported_not_guessed(monkeypatch, tmp_path):
    creds = tmp_path / "creds.json"
    creds.write_text("{not json")
    monkeypatch.setenv("ANTHROPIC_OAUTH_CREDS_PATH", str(creds))
    with pytest.raises(OAuthCredentialError, match="malformed"):
        SubscriptionOAuthToken().token()


# ------------------------------------------------------------------- selection


def test_build_provider_honours_an_explicit_anthropic_choice(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("POLICYDESK_PROVIDER", "anthropic")
    assert build_provider().name == "anthropic"


def test_build_provider_reaches_anthropic_without_a_key(monkeypatch, tmp_path):
    """Acceptance: a host with neither an API key nor a signed-in codex still runs."""
    creds = tmp_path / "creds.json"
    _write_creds(creds, expires_in_s=3600)
    monkeypatch.setenv("ANTHROPIC_OAUTH_CREDS_PATH", str(creds))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("POLICYDESK_PROVIDER", raising=False)
    assert build_provider().name == "anthropic"


def test_a_readable_credential_file_outranks_an_openai_key(monkeypatch, tmp_path):
    """
    Claude is what this desk runs on, so a present subscription token wins.

    An operator who wants the key spent says so with `POLICYDESK_PROVIDER=openai`.
    """
    creds = tmp_path / "creds.json"
    _write_creds(creds, expires_in_s=3600)
    monkeypatch.setenv("ANTHROPIC_OAUTH_CREDS_PATH", str(creds))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("POLICYDESK_PROVIDER", raising=False)
    assert build_provider().name == "anthropic"
