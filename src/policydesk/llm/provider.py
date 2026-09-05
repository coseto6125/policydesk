"""
The model, behind a seam.

Two implementations sit behind one Protocol. `OpenAIProvider` calls the Responses API
over HTTP; `ScriptedProvider` answers from a fixture. Nothing above this module knows
which one it holds — the difference shows only in `llm_usage.provider`, which is the
property that makes running on a mock honest rather than a pretence.

Every call is recorded whichever provider served it: phase, scenario, tools offered,
token counts, latency, and the request and response bodies. A prompt-based validator
is non-deterministic by nature, so the record is what makes its verdict auditable
afterwards — you cannot re-run the model and get the same answer, but you can read
exactly what it was asked and what it said.

HTTP directly rather than the openai SDK: one less dependency, and the request body is
visible in this file rather than assembled three layers down, which matters when the
body is what gets stored for audit.
"""

import asyncio
import os
import shutil
import tempfile
import time
from enum import StrEnum
from typing import Any, Protocol

import aiohttp
import stamina
from msgspec import DecodeError, Struct, json

from policydesk.bootloader import logger

RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = os.environ.get("POLICYDESK_MODEL", "gpt-5.6-luna")


class Phase(StrEnum):
    """
    Where in a turn a model call sits.

    Names the seven phases allowed by the memory migration's `llm_usage` constraint.
    An allowed phase does not imply that its runtime path writes usage records.
    """

    ROUTE = "route"
    SCENARIO_TOOLS = "scenario_tools"
    ANSWER = "answer"
    VALIDATE = "validate"
    REPAIR = "repair"
    EMBEDDING = "embedding"
    FACTS = "facts"

    @property
    def temperature(self) -> float:
        """HTTP sampling only; the CLI does not expose temperature."""
        return 0.3 if self is Phase.ANSWER else 0.1


class Completion(Struct, frozen=True):
    """What a provider returned, plus what it cost."""

    text: str
    tool_calls: tuple[dict[str, Any], ...] = ()
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    provider: str = ""
    raw: dict[str, Any] | None = None


class Provider(Protocol):
    """A source of model completions."""

    name: str

    async def complete(
        self,
        *,
        instructions: str,
        user_input: str,
        tools: list[dict[str, Any]] | None = None,
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        phase: Phase | None = None,
    ) -> Completion:
        """
        Ask the model once.

        Args:
            instructions: The system-level brief. Stable across a scenario.
            user_input: What this turn is about.
            tools: Tool schemas the model may call.
            schema: A JSON Schema the reply must satisfy, for a structured verdict.
            model: Overrides the configured model.
            phase: Selects HTTP sampling; providers without temperature ignore it.

        Returns:
            The completion and its usage.

        """
        ...


class OpenAIProvider:
    """Calls the OpenAI Responses API."""

    name = "openai"

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL) -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._model = model
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        """Close the shared session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _open_session(self) -> aiohttp.ClientSession:
        """
        Return the shared session, opening it on first use.

        Returns:
            One session reused across calls. A session per call means a TCP connection
            and a TLS handshake per call, discarded immediately — on a websocket turn
            that reaches a MODEL scenario that is two avoidable handshakes on the path
            the customer is waiting on, and every retry adds another. The rest of this
            codebase already reuses connections: the corpus fetcher holds one session
            across 660 downloads, and the database holds one pool.

        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90))
        return self._session

    async def complete(
        self,
        *,
        instructions: str,
        user_input: str,
        tools: list[dict[str, Any]] | None = None,
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        phase: Phase | None = None,
    ) -> Completion:
        """
        Ask the model once over HTTP.

        Args:
            instructions: The system-level brief.
            user_input: What this turn is about.
            tools: Tool schemas the model may call.
            schema: A JSON Schema the reply must satisfy.
            model: Overrides the configured model.
            phase: Selects the HTTP sampling temperature when supplied.

        Returns:
            The completion and its usage.

        Raises:
            ProviderError: The API refused, or the reply did not parse. The caller
                states that it could not answer; it never invents one.

        """
        body: dict[str, Any] = {
            "model": model or self._model,
            "instructions": instructions,
            "input": user_input,
            # Conversation state lives in this system's own tables, so there is nothing
            # to gain from the provider retaining a copy of every case.
            "store": False,
        }
        if tools:
            body["tools"] = tools
        if phase is not None:
            body["temperature"] = phase.temperature
        if schema:
            body["text"] = {"format": {"type": "json_schema", "name": "verdict", "strict": True, "schema": schema}}

        started = time.perf_counter()
        payload = await self._post(body)
        latency_ms = int((time.perf_counter() - started) * 1000)

        usage = payload.get("usage") or {}
        return Completion(
            text=_output_text(payload),
            tool_calls=tuple(item for item in payload.get("output", []) if item.get("type") == "function_call"),
            model=payload.get("model", body["model"]),
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            cached_tokens=(usage.get("input_tokens_details") or {}).get("cached_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            latency_ms=latency_ms,
            provider=self.name,
            raw=payload,
        )

    @stamina.retry(on=aiohttp.ClientError, attempts=3, timeout=60)
    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        Send one request, retrying transport failures.

        Args:
            body: The Responses API request body.

        Returns:
            The decoded response.

        Raises:
            ProviderError: The API answered with an error status.

        """
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        session = self._open_session()
        async with session.post(RESPONSES_URL, json=body, headers=headers) as resp:
            payload = await resp.json()
            if resp.status >= 400:
                message = (payload.get("error") or {}).get("message", resp.reason)
                logger.warning("llm_http_error", status=resp.status, message=message)
                raise ProviderError(f"responses api {resp.status}: {message}")
            return payload


class ScriptedProvider:
    """
    Answers from a fixture, for tests and for a rehearsal with no network.

    Deliberately not a stub. It records the same usage fields, reports a latency, and
    raises when asked for a turn it has no answer for — a caller that works against
    this one works against the real API, including its failure branch.
    """

    name = "scripted"

    def __init__(self, replies: dict[str, str], latency_ms: int = 40) -> None:
        self._replies = replies
        self._latency_ms = latency_ms

    async def complete(
        self,
        *,
        instructions: str,
        user_input: str,
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002  - Protocol shape; a script has no tools to offer
        schema: dict[str, Any] | None = None,  # noqa: ARG002  - Protocol shape; a script cannot be constrained
        model: str | None = None,
        phase: Phase | None = None,  # noqa: ARG002 - Scripted answers do not sample.
    ) -> Completion:
        """
        Return the scripted answer for this input.

        Args:
            instructions: Ignored, but recorded.
            user_input: Looked up in the script.
            tools: Ignored.
            schema: Ignored.
            model: Recorded as the model name.
            phase: Ignored.

        Returns:
            The scripted completion.

        Raises:
            ProviderError: The script has no answer for this input.

        """
        if (reply := self._replies.get(user_input.strip())) is None:
            raise ProviderError(f"no scripted reply for {user_input[:60]!r}")
        return Completion(
            text=reply,
            model=model or "scripted",
            prompt_tokens=len(instructions) // 4,
            completion_tokens=len(reply) // 4,
            total_tokens=(len(instructions) + len(reply)) // 4,
            latency_ms=self._latency_ms,
            provider=self.name,
        )


class ProviderError(RuntimeError):
    """The model could not be reached, or answered with an error."""


def _output_text(payload: dict[str, Any]) -> str:
    """
    Read the reply text out of a Responses payload.

    Args:
        payload: A decoded Responses API response.

    Returns:
        The concatenated output text, empty when the reply was tool calls only.

    """
    if (direct := payload.get("output_text")) is not None:
        return direct
    parts = [
        content.get("text", "")
        for item in payload.get("output", [])
        if item.get("type") == "message"
        for content in item.get("content", [])
        if content.get("type") == "output_text"
    ]
    return "".join(parts)


# ---------------------------------------------------------------- codex CLI provider

CODEX_BIN = os.environ.get("POLICYDESK_CODEX_BIN", "codex")
CODEX_EFFORT = os.environ.get("POLICYDESK_CODEX_EFFORT", "low")
# One model name across both providers. The CLI runs with --ignore-user-config, so
# without this it would answer on its own built-in default rather than the model this
# deployment names — and llm_usage would record a model nobody chose.
CODEX_MODEL = os.environ.get("POLICYDESK_CODEX_MODEL", DEFAULT_MODEL)
CODEX_CONCURRENCY = int(os.environ.get("POLICYDESK_CODEX_CONCURRENCY", "4"))

_TOOL_ENVELOPE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply": {
            "type": "string",
            "description": "The prose the customer reads. Always written, including when tools are called.",
        },
        "tool_calls": {
            "type": "array",
            "description": "The tools to call. Empty when the reply needs none.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "arguments": {"type": "string", "description": "A JSON object, encoded as a string."},
                },
                "required": ["name", "arguments"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["reply", "tool_calls"],
    "additionalProperties": False,
}


def _tool_brief(tools: list[dict[str, Any]]) -> str:
    """
    Describe the offered tools in the prompt, because the CLI takes no tool schemas.

    Args:
        tools: Function-tool schemas, in the shape the Responses API takes.

    Returns:
        A block naming each tool, its purpose and its parameters.

    """
    lines = ["# 可呼叫的工具"]
    for tool in tools:
        params = (tool.get("parameters") or {}).get("properties") or {}
        argues = "、".join(f"{k}（{v.get('description', '')}）" for k, v in params.items()) or "無參數"
        lines.append(f"- `{tool['name']}`：{tool.get('description', '')}\n  參數：{argues}")
    lines.append(
        "\n以 JSON 物件回覆，欄位為 reply 與 tool_calls。"
        "reply 一律寫滿，那是保戶讀到的整段答覆。"
        "要呼叫工具就把它放進 tool_calls，arguments 是把參數物件編碼成的 JSON 字串；"
        "不呼叫任何工具時 tool_calls 為空陣列。"
        "本櫃台一回合只呼叫一次工具，工具結果不會再回到你手上，"
        "所以要說的話都寫進 reply，不要留待下一輪。"
    )
    return "\n".join(lines)


class CodexCliProvider:
    """
    Runs the model through the locally authenticated `codex` CLI.

    The seam exists so the desk can run with no API key: `codex` is already signed in
    on this machine and billed by subscription, so a demo costs nothing to drive. The
    trade is latency — every call is a process, about five seconds against about one
    for the HTTP path — and no streaming.

    Two shapes reach the CLI through `--output-schema`, which constrains the final
    message to a JSON Schema:

    - A caller that passes `schema` gets that schema through unchanged.
    - A caller that passes `tools` gets `_TOOL_ENVELOPE`, and the tools themselves are
      described in the prompt. The CLI has no tool-calling API of its own, so a tool
      call here is the model naming the tool in its answer. The names and arguments
      still land in `Completion.tool_calls`, so nothing above this module changes.

    The subprocess runs in an empty directory with `--ignore-user-config` and
    `--ignore-rules`, so it picks up no AGENTS.md, no MCP servers and no hooks from the
    developer's own machine. What the desk asks is all the model reads.
    """

    name = "codex-cli"

    def __init__(self, model: str = CODEX_MODEL, effort: str = CODEX_EFFORT) -> None:
        self._model = model
        self._effort = effort
        self._workdir: str | None = None
        self._gate = asyncio.Semaphore(CODEX_CONCURRENCY)

    async def close(self) -> None:
        """Remove the scratch directory the subprocesses ran in."""
        if self._workdir is not None:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None

    def _root(self) -> str:
        """
        Return the empty directory the CLI runs in, creating it on first use.

        Returns:
            A path holding nothing. Running there is what keeps the developer's own
            AGENTS.md out of a customer's turn.

        """
        if self._workdir is None:
            self._workdir = tempfile.mkdtemp(prefix="policydesk-codex-")
        return self._workdir

    async def complete(
        self,
        *,
        instructions: str,
        user_input: str,
        tools: list[dict[str, Any]] | None = None,
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        phase: Phase | None = None,  # noqa: ARG002 - Codex CLI has no temperature control.
    ) -> Completion:
        """
        Ask the model once, through the CLI.

        Args:
            instructions: The system-level brief.
            user_input: What this turn is about.
            tools: Tool schemas the model may call, described into the prompt.
            schema: A JSON Schema the reply must satisfy.
            model: Overrides the configured model.
            phase: Ignored; the CLI uses its existing reasoning effort and schema.

        Returns:
            The completion and its usage.

        Raises:
            ProviderError: The CLI failed, or its reply did not parse.

        """
        enforced = schema or (_TOOL_ENVELOPE if tools else None)
        prompt = instructions
        if tools and not schema:
            prompt = f"{instructions}\n\n{_tool_brief(tools)}"
        prompt = f"{prompt}\n\n# 本回合\n{user_input}"

        started = time.perf_counter()
        async with self._gate:
            stdout = await self._run(prompt, enforced, model or self._model)
        latency_ms = int((time.perf_counter() - started) * 1000)

        text, usage = _read_events(stdout)
        calls: tuple[dict[str, Any], ...] = ()
        if tools and not schema:
            text, calls = _split_envelope(text)

        return Completion(
            text=text,
            tool_calls=calls,
            model=model or self._model or "codex-default",
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            cached_tokens=usage.get("cached_input_tokens", 0),
            total_tokens=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            latency_ms=latency_ms,
            provider=self.name,
            raw={"usage": usage, "text": text[:4000]},
        )

    async def _run(self, prompt: str, schema: dict[str, Any] | None, model: str) -> str:
        """
        Run one `codex exec` and return its event stream.

        Args:
            prompt: The whole prompt, sent on stdin so no quoting rule applies to it.
            schema: The schema to constrain the final message with, if any.
            model: The model to pass with `-m`, if any.

        Returns:
            The JSONL the CLI printed.

        Raises:
            ProviderError: The CLI exited non-zero or could not be started.

        """
        args = [
            CODEX_BIN, "exec", "-",
            "--sandbox", "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "-C", self._root(),
            "--json",
            "-c", f"model_reasoning_effort={self._effort}",
        ]
        if model:
            args += ["-m", model]

        # The schema goes to a file because that is the only form the flag takes. It is
        # written per call rather than cached: the router, the answering call and the
        # validator each enforce a different one.
        handle, path = tempfile.mkstemp(suffix=".json", dir=self._root())
        try:
            if schema is not None:
                os.write(handle, json.encode(schema))
                args += ["--output-schema", path]
            os.close(handle)

            try:
                process = await asyncio.create_subprocess_exec(
                    *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
            except OSError as exc:
                raise ProviderError(f"codex cli not runnable: {exc}") from exc

            try:
                out, err = await asyncio.wait_for(process.communicate(prompt.encode()), timeout=180)
            except TimeoutError as exc:
                process.kill()
                raise ProviderError("codex cli timed out after 180s") from exc

            if process.returncode != 0:
                tail = err.decode(errors="replace")[-400:]
                logger.warning("codex_cli_failed", returncode=process.returncode, stderr=tail)
                raise ProviderError(f"codex exec exited {process.returncode}: {tail}")
            return out.decode(errors="replace")
        finally:
            os.unlink(path)


def _read_events(stream: str) -> tuple[str, dict[str, int]]:
    """
    Read the final message and the usage out of a codex JSONL stream.

    Args:
        stream: What `codex exec --json` printed.

    Returns:
        The last agent message, and the turn's token counts.

    Raises:
        ProviderError: The stream carried no agent message.

    """
    text = ""
    usage: dict[str, int] = {}
    for line in stream.splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.decode(line.encode())
        except DecodeError:
            continue
        match event.get("type"):
            case "item.completed" if (item := event.get("item", {})).get("type") == "agent_message":
                text = item.get("text", "")
            case "turn.completed":
                usage = event.get("usage") or {}
            case "turn.failed":
                raise ProviderError(f"codex turn failed: {str(event.get('error'))[:200]}")
    if not text:
        raise ProviderError("codex produced no agent message")
    return text, usage


def _split_envelope(text: str) -> tuple[str, tuple[dict[str, Any], ...]]:
    """
    Read `_TOOL_ENVELOPE` back into a reply and a tuple of tool calls.

    Args:
        text: The model's final message, expected to be the envelope object.

    Returns:
        The prose reply, and the calls in the shape the Responses API returns them.

    Raises:
        ProviderError: The envelope did not parse. The caller says it could not answer
            rather than treating a malformed reply as an empty one, which would look
            to the executor like a model that simply chose no tool.

    """
    body = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.decode(body.encode())
    except DecodeError as exc:
        raise ProviderError(f"codex reply was not the tool envelope: {body[:200]}") from exc
    if not isinstance(parsed, dict):
        raise ProviderError(f"codex reply was not an object: {body[:200]}")
    calls = tuple(
        {"type": "function_call", "name": call.get("name", ""), "arguments": call.get("arguments", "{}")}
        for call in parsed.get("tool_calls") or []
        if isinstance(call, dict)
    )
    return parsed.get("reply", ""), calls


def build_provider() -> Provider:
    """
    Choose the provider this deployment runs on.

    Returns:
        The HTTP provider when an API key is set, and the CLI provider otherwise.
        `POLICYDESK_PROVIDER` forces either one. The fallback is deliberate: a machine
        with `codex` signed in can drive the whole desk with no key at all, and a
        deployment that meant to use a key finds out at the first turn rather than
        silently spending someone else's subscription.

    """
    match os.environ.get("POLICYDESK_PROVIDER", "").lower():
        case "openai":
            return OpenAIProvider()
        case "codex":
            return CodexCliProvider()
        case _:
            if os.environ.get("OPENAI_API_KEY"):
                return OpenAIProvider()
            logger.info("provider_selected", provider="codex-cli", reason="no OPENAI_API_KEY")
            return CodexCliProvider()
