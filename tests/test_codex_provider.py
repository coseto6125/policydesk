"""
The codex CLI seam, tested without running the CLI.

Everything here is the parsing either side of the subprocess: what the prompt tells
the model about tools it cannot be handed as schemas, and what comes back out of the
event stream. The subprocess itself is exercised by running the desk.
"""

from unittest.mock import AsyncMock

import pytest

from policydesk.llm.provider import (
    CodexCliProvider,
    OpenAIProvider,
    Phase,
    ProviderError,
    _read_events,
    _split_envelope,
    _tool_brief,
    build_provider,
)


@pytest.mark.parametrize(("phase", "temperature"), [
    (Phase.ROUTE, 0.1),
    (Phase.SCENARIO_TOOLS, 0.1),
    (Phase.ANSWER, 0.3),
    (Phase.VALIDATE, 0.1),
    (Phase.FACTS, 0.1),
])
async def test_complete_http_phase_sets_sampling_temperature(monkeypatch, phase, temperature):
    provider = OpenAIProvider(api_key="test-key")
    post = AsyncMock(return_value={"output": []})
    monkeypatch.setattr(provider, "_post", post)
    await provider.complete(instructions="rules", user_input="question", phase=phase)
    assert post.call_args.args[0]["temperature"] == temperature
    assert temperature >= 0.1


async def test_complete_http_without_phase_preserves_provider_default(monkeypatch):
    provider = OpenAIProvider(api_key="test-key")
    post = AsyncMock(return_value={"output": []})
    monkeypatch.setattr(provider, "_post", post)
    await provider.complete(instructions="rules", user_input="question")
    assert "temperature" not in post.call_args.args[0]


async def test_complete_cli_phase_preserves_existing_invocation(monkeypatch):
    provider = CodexCliProvider()
    run = AsyncMock(return_value=(
        '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
        '{"type":"turn.completed","usage":{}}'
    ))
    monkeypatch.setattr(provider, "_run", run)
    result = await provider.complete(instructions="rules", user_input="question", phase=Phase.ROUTE)
    assert result.text == "ok"
    assert run.call_args.args == ("rules\n\n# 本回合\nquestion", None, provider._model)


def test_tool_brief_names_every_tool_and_its_parameters():
    """The CLI takes no tool schemas, so the prompt is the only place a tool exists."""
    brief = _tool_brief(
        [
            {
                "name": "calculate",
                "description": "Evaluate an expression.",
                "parameters": {"properties": {"expression": {"description": "Arithmetic."}}},
            }
        ]
    )
    assert "calculate" in brief
    assert "expression" in brief
    assert "Arithmetic" in brief


def test_tool_brief_says_the_reply_is_always_written():
    """
    A model that answers with a tool call alone leaves the customer an empty bubble.

    There is no second round in this desk: the tool result never goes back to the
    model, so whatever it wanted to say has to be in this one reply.
    """
    brief = _tool_brief([{"name": "calculate", "description": "", "parameters": {}}])
    assert "reply 一律寫滿" in brief


def test_read_events_takes_the_last_agent_message_and_the_usage():
    stream = '{"type": "thread.started", "thread_id": "x"}\n{"type": "item.completed", "item": {"type": "reasoning", "text": "ignored"}}\n{"type": "item.completed", "item": {"type": "agent_message", "text": "答覆"}}\n{"type": "turn.completed", "usage": {"input_tokens": 120, "output_tokens": 8}}'
    text, usage = _read_events(stream)
    assert text == "答覆"
    assert usage == {"input_tokens": 120, "output_tokens": 8}


def test_read_events_survives_the_banner_lines_the_cli_prints():
    """Not every line of the stream is JSON, and one bad line is not a failed turn."""
    stream = 'reading config\n{"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}\n{oops'
    text, _ = _read_events(stream)
    assert text == "ok"


def test_read_events_raises_when_the_turn_failed():
    with pytest.raises(ProviderError, match="turn failed"):
        _read_events('{"type": "turn.failed", "error": {"message": "rate limited"}}')


def test_read_events_raises_when_nothing_was_said():
    with pytest.raises(ProviderError, match="no agent message"):
        _read_events('{"type": "turn.completed", "usage": {}}')


def test_split_envelope_returns_the_reply_and_the_calls():
    reply, calls = _split_envelope('{"reply":"好的","tool_calls":[{"name":"calculate","arguments":"{\\"expression\\":\\"2*3\\"}"}]}')
    assert reply == "好的"
    assert calls == ({"type": "function_call", "name": "calculate", "arguments": '{"expression":"2*3"}'},)


def test_split_envelope_tolerates_a_fenced_object():
    reply, calls = _split_envelope('```json\n{"reply":"好","tool_calls":[]}\n```')
    assert reply == "好"
    assert calls == ()


def test_split_envelope_refuses_a_malformed_reply():
    """
    Returning an empty result here would read to the executor as "chose no tool".

    The router would then forward whatever prose came back as the answer, and the
    answering call would lose a calculation without anything saying so.
    """
    with pytest.raises(ProviderError):
        _split_envelope("我覺得應該可以理賠")


def test_calls_are_shaped_like_the_responses_api_returns_them():
    """Nothing above the provider knows which implementation it holds."""
    _, calls = _split_envelope('{"reply":"","tool_calls":[{"name":"explain_cover","arguments":"{}"}]}')
    assert calls[0]["type"] == "function_call"
    assert calls[0]["name"] == "explain_cover"


def test_build_provider_falls_back_to_the_cli_with_no_credentials_at_all(monkeypatch, tmp_path):
    """
    The CLI is now the last fallback, not the first.

    A machine with `codex` signed in and nothing else still drives the whole desk; a
    machine that also holds a Claude Code subscription token reaches Anthropic first,
    which is the path a container can take and this one cannot.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("POLICYDESK_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_OAUTH_CREDS_PATH", str(tmp_path / "absent.json"))
    assert build_provider().name == "codex-cli"


def test_build_provider_prefers_the_api_when_a_key_is_set(monkeypatch, tmp_path):
    """
    A key still beats the CLI. It no longer beats the Anthropic subscription token.

    The desk runs on Claude, so a readable credential file is chosen ahead of a key;
    this test is now about the two providers below that. `POLICYDESK_PROVIDER=openai`
    is how an operator picks the key over an available subscription.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("POLICYDESK_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_OAUTH_CREDS_PATH", str(tmp_path / "absent.json"))
    assert build_provider().name == "openai"


def test_build_provider_honours_an_explicit_choice(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("POLICYDESK_PROVIDER", "codex")
    assert build_provider().name == "codex-cli"


def test_the_cli_provider_closes_like_the_http_one():
    """The server closes whichever provider it built."""
    assert hasattr(CodexCliProvider, "close")


def test_the_cli_runs_where_it_reads_no_project_instructions():
    """
    A developer's own AGENTS.md is not part of a customer's turn.

    The scratch directory is empty and the run ignores user config and rules, so what
    the desk asks is all the model reads.
    """
    from pathlib import Path

    source = Path("src/policydesk/llm/provider.py").read_text()
    body = source[source.index("    async def _run("):source.index("def _read_events")]
    assert "--ignore-user-config" in body
    assert "--ignore-rules" in body
    assert "--sandbox" in body


def test_the_citation_format_the_checker_reads_is_the_format_the_prompt_asks_for():
    """
    The check finds nothing when the model cites in prose.

    A reply carrying 【第12條】 passes the citation gate with an empty citation list —
    the gate reports no faults because it was handed nothing to check, which reads
    from outside exactly like a verified reply. The clause_id is in the material the
    tools return, so the prompt names that token rather than 條號 in the abstract.
    """
    from policydesk.agent.executor import _CITATION
    from policydesk.agent.scenario import CLAIM_CHECKLIST, EXPLAIN_COVER

    for scenario in (EXPLAIN_COVER, CLAIM_CHECKLIST):
        assert "clause_id" in scenario.injection
        cited = _CITATION.findall(scenario.injection)
        assert cited, f"{scenario.name} shows no example the checker would match"
