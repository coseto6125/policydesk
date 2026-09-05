"""
The model, behind a seam.

Four implementations sit behind one Protocol. `AnthropicProvider` calls the Anthropic
Messages API through the official SDK and is what a deployment runs on; `OpenAIProvider`
calls the Responses API over HTTP; `CodexCliProvider` shells out to a locally signed-in
`codex`; `ScriptedProvider` answers from a fixture. Nothing above this module knows which
one it holds — the difference shows only in `llm_usage.provider`, which is the property
that makes running on a mock honest rather than a pretence.

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
from hashlib import sha256
from typing import Any, Protocol

import aiohttp
import anthropic
import stamina
from msgspec import DecodeError, Struct, json

from policydesk.bootloader import logger
from policydesk.llm.oauth import OAUTH_BETA_HEADER, OAuthCredentialError, SubscriptionOAuthToken

RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = os.environ.get("POLICYDESK_MODEL", "gpt-5.6-luna")


class Phase(StrEnum):
    """
    Where in a turn a model call sits.

    Names the six phases allowed by the `llm_usage` constraint.
    An allowed phase does not imply that its runtime path writes usage records.
    """

    ROUTE = "route"
    SCENARIO_TOOLS = "scenario_tools"
    ANSWER = "answer"
    VALIDATE = "validate"
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
    request: dict[str, Any] | None = None
    """What the trace records about the call that produced this, built from the payload
    that actually went out. See `audit_request`."""


def audit_request(body: dict[str, Any]) -> dict[str, Any]:
    """
    Describe the request that actually went out, without copying the customer.

    Args:
        body: The payload handed to the provider's API, after every rewrite it makes.

    Returns:
        What an auditor needs: the tools this call was offered, the reply format it was
        held to, and digests plus lengths of the brief and the input.

    Read from the built body rather than from the arguments the executor passed, because
    the two differ. `_anthropic_schema` rewrites `maxItems: 0` into `{"type": "null"}`,
    so a record built from the arguments says a field was available on a call where the
    model could not fill it — the field names match and the constraint does not. Reading
    the body means a future rewrite is recorded without anyone remembering to describe
    it here.

    The brief is digested rather than stored. It carries names, national IDs and
    policies, and a second copy of those in a table with no retention rule is a worse
    trade than the gap it closes. A digest still proves two rows shared a prompt, and
    matches a prompt someone still holds.
    """
    schema = (body.get("output_config") or {}).get("format", {}).get("schema") or {}
    system = body.get("system") or ""
    messages = body.get("messages") or []
    user_input = "".join(
        part.get("text", "") if isinstance(part, dict) else str(part)
        for message in messages
        for part in ([message.get("content")] if isinstance(message.get("content"), str) else message.get("content", []))
    ) if messages else ""
    return {
        "model": body.get("model", ""),
        "tools": sorted(tool.get("name", "") for tool in body.get("tools") or ()),
        "response_fields": sorted(schema.get("properties", {})),
        # A field the schema pinned to null is one the model could not fill, whatever
        # its name suggests. Naming them is the difference between "citations were
        # available" and "citations were forbidden on this call".
        "emptied_fields": sorted(
            name for name, spec in schema.get("properties", {}).items()
            if isinstance(spec, dict) and spec.get("type") == "null"
        ),
        "system_sha256": sha256(system.encode()).hexdigest(),
        "system_chars": len(system),
        "input_sha256": sha256(user_input.encode()).hexdigest(),
        "input_chars": len(user_input),
    }


class Provider(Protocol):
    """A source of model completions."""

    name: str

    async def complete(
        self,
        *,
        instructions: str,
        user_input: str,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
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
            tool_choice: Constrains the call. `{"type": "any"}` requires a tool without
                naming which one, so a router cannot answer from its own words.
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
        tool_choice: dict[str, Any] | None = None,
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
            tool_choice: Constrains the call. `{"type": "any"}` requires a tool without
                naming which one, so a router cannot answer from its own words.
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
            if tool_choice:
                # This API spells the same constraint as a bare string. `any` on the
                # Anthropic path and `required` here both mean "call one of these, and
                # you choose which".
                body["tool_choice"] = "required" if tool_choice.get("type") == "any" else tool_choice
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
        tool_choice: dict[str, Any] | None = None,  # noqa: ARG002 - Protocol shape; a script calls nothing
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
        tool_choice: dict[str, Any] | None = None,  # noqa: ARG002 - The CLI builds calls from free text; it cannot be constrained.
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


# ------------------------------------------------------------- Anthropic provider

ANTHROPIC_MODEL = os.environ.get("POLICYDESK_ANTHROPIC_MODEL", "claude-haiku-4-5")
# The answer call has to hold a full reply plus its citations and quoted clause text.
# Haiku 4.5 does not think unless asked, so nothing shares this ceiling with a
# reasoning block and the whole budget is the answer.
ANTHROPIC_MAX_TOKENS = int(os.environ.get("POLICYDESK_ANTHROPIC_MAX_TOKENS", "8000"))


def _anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """
    Rewrite a Responses function tool into the shape Anthropic takes.

    Args:
        tool: A `tool_schema` result: `type`, `name`, `description`, `parameters`.

    Returns:
        The same tool with its parameter schema under `input_schema`, which is the only
        difference between the two dialects for the tools this desk offers.

    """
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "input_schema": _anthropic_schema(tool.get("parameters") or {"type": "object", "properties": {}}),
    }


def _anthropic_schema(node: Any) -> Any:
    """
    Rewrite a JSON Schema into the subset Anthropic's validator accepts.

    Args:
        node: Any node of the schema.

    Returns:
        The rewritten node. Two shapes differ from the dialect the Responses API takes,
        and both are rewritten into an equivalent rather than dropped — a constraint
        this desk states about citations is not one to lose in a translation.

        - `maxItems: 0` on an array is rejected outright: verified 400 "For 'array'
          type, property 'maxItems' is not supported", the same answer `prefixItems`
          gets. `_answer_schema` and `VERDICT_SCHEMA` use it to say "cite nothing", so
          the array becomes `{"type": "null"}` — under structured outputs the only
          legal token sequence for that field is then `null`, which is hard enforcement
          and not the model choosing to comply. Verified against `claude-haiku-4-5` on
          three of three attempts under a prompt demanding the field be filled.
          `_restore_empty` reads that `null` back as the empty list the caller's struct
          expects, so nothing above this module sees the rewrite. Keeping the guarantee
          here is what leaves `_unverifiable` a second line of defence rather than the
          only one. (`items: false` and `minItems` are accepted but unenforced — the
          model fills the array anyway — and an empty `enum` is rejected.)
        - `{"type": ["string", "null"], "enum": [..., null]}` — the form msgspec emits
          for a nullable enum — is rejected as an enum whose values do not match the
          declared type. It becomes the equivalent `anyOf` of a typed enum and null.

    """
    if isinstance(node, dict):
        if node.get("type") == "array" and node.get("maxItems") == 0:
            # The description survives, so the model still reads why the field is empty.
            return {"type": "null"} | ({"description": node["description"]} if "description" in node else {})
        if isinstance(kinds := node.get("type"), list) and "null" in kinds and "enum" in node:
            plain = [kind for kind in kinds if kind != "null"]
            base = {
                "type": plain[0] if len(plain) == 1 else plain,
                "enum": [value for value in node["enum"] if value is not None],
            }
            rest = {k: _anthropic_schema(v) for k, v in node.items() if k not in ("type", "enum")}
            return rest | {"anyOf": [base, {"type": "null"}]}
        return {k: _anthropic_schema(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_anthropic_schema(item) for item in node]
    return node


def _emptied(schema: dict[str, Any]) -> tuple[str, ...]:
    """
    Name the fields `_anthropic_schema` turned into a null.

    Args:
        schema: The schema the caller passed, before the rewrite.

    Returns:
        The top-level property names that said `maxItems: 0`. Both schemas that use it —
        `_answer_schema` and `VERDICT_SCHEMA` — are flat objects, so a top-level scan is
        the whole set; a nested one would need the body walked as well, and none exists.

    """
    return tuple(
        name
        for name, node in (schema.get("properties") or {}).items()
        if isinstance(node, dict) and node.get("type") == "array" and node.get("maxItems") == 0
    )


def _restore_empty(text: str, fields: tuple[str, ...]) -> str:
    """
    Read the nulls `_anthropic_schema` asked for back as empty lists.

    Args:
        text: The model's reply, as the API returned it.
        fields: The names that were rewritten.

    Returns:
        The reply with each rewritten field set to `[]`, so it decodes into the caller's
        struct unchanged. A reply that does not parse is handed back untouched: the
        caller already reports a malformed answer as a format fault, and repairing one
        here would only hide it.

    """
    try:
        body = json.decode(text.encode())
    except DecodeError:
        return text
    if not isinstance(body, dict):
        return text
    for field in fields:
        if body.get(field) is None:
            body[field] = []
    return json.encode(body).decode()


class AnthropicProvider:
    """
    Calls the Anthropic Messages API on Claude Code's subscription OAuth token.

    The seam exists so the desk runs on a host nobody is signed in on. `CodexCliProvider`
    needs a `codex` session on the machine, which a container cannot have; this one reads
    a credential file, and `ANTHROPIC_OAUTH_CREDS_PATH` points it at a token synced to
    the deployment. The trade against the CLI is the same one the HTTP path already made:
    a request instead of a process, so about one second instead of about five.

    The official SDK rather than raw HTTP, unlike `OpenAIProvider`: the request body here
    is small enough to read in one screen, and the SDK carries the retry policy, the
    typed errors and the response model that the HTTP path spells out by hand.

    Sampling reaches the API through `extra_body`. Anthropic removed `temperature` from
    the models released after Haiku 4.5, so the SDK no longer takes it as a parameter —
    but Haiku 4.5 itself still honours it, and `Phase.temperature` is the reason this
    desk routes at 0.1 and answers at 0.3.
    """

    name = "anthropic"

    def __init__(self, model: str = ANTHROPIC_MODEL) -> None:
        self._model = model
        self._credentials = SubscriptionOAuthToken()
        self._client: anthropic.AsyncAnthropic | None = None

    async def close(self) -> None:
        """Close the shared HTTP client."""
        if self._client is not None:
            await self._client.close()
        self._client = None

    def _open_client(self) -> anthropic.AsyncAnthropic:
        """
        Return the shared client, opening it on first use.

        Returns:
            One client reused across calls, carrying a token read fresh each time.

        The token is re-set per call because Claude Code rotates it in the background and
        the SDK reads this attribute at request time. Rebuilding the client instead would
        throw away the connection pool on every turn, which is the cost `OpenAIProvider`'s
        shared session exists to avoid.

        """
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(
                auth_token=self._credentials.token(),
                # The header is what makes the API accept a subscription Bearer token
                # instead of a Console key. `anthropic-version` the SDK sends itself.
                default_headers={"anthropic-beta": OAUTH_BETA_HEADER},
                timeout=90.0,
                max_retries=3,
            )
        self._client.auth_token = self._credentials.token()
        return self._client

    async def complete(
        self,
        *,
        instructions: str,
        user_input: str,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        phase: Phase | None = None,  # noqa: ARG002 - see below; the SDK takes no temperature.
    ) -> Completion:
        """
        Ask the model once.

        Args:
            instructions: The system-level brief, sent as the top-level `system`.
            user_input: What this turn is about, sent as the one user message.
            tools: Tool schemas the model may call, rewritten by `_anthropic_tool`.
            tool_choice: Forwarded as sent. `{"type": "any"}` makes a tool call the only
                shape the reply may take.
            schema: A JSON Schema the reply must satisfy, enforced by `output_config`.
            model: Overrides the configured model.
            phase: Ignored. `Phase.temperature` is deliberately not forwarded on this
                path — do not re-add it. Anthropic removed sampling from the models
                released after Haiku 4.5, so `anthropic` 1.4.0's `messages.create` takes
                no `temperature` parameter at all; Haiku 4.5 itself still honours the
                field on the wire, but the only way to reach it is `extra_body`, and
                this desk does not route a customer's turn through an undeclared back
                door. The model answers at its own default. `Phase.temperature` stays
                because the HTTP and CLI paths still read it.

        Returns:
            The completion and its usage.

        Raises:
            ProviderError: The API refused, or the credential file could not be read.
                The caller states that it could not answer; it never invents one.

        """
        body: dict[str, Any] = {
            "model": model or self._model,
            "max_tokens": ANTHROPIC_MAX_TOKENS,
            "system": instructions,
            "messages": [{"role": "user", "content": user_input}],
        }
        if tools:
            body["tools"] = [_anthropic_tool(tool) for tool in tools]
            if tool_choice:
                body["tool_choice"] = tool_choice
        if schema:
            body["output_config"] = {"format": {"type": "json_schema", "schema": _anthropic_schema(schema)}}

        started = time.perf_counter()
        try:
            message = await self._open_client().messages.create(**body)
        except OAuthCredentialError as exc:
            raise ProviderError(f"anthropic credentials unreadable: {exc}") from exc
        except anthropic.APIStatusError as exc:
            logger.warning("llm_http_error", status=exc.status_code, message=str(exc)[:400])
            raise ProviderError(f"anthropic api {exc.status_code}: {str(exc)[:400]}") from exc
        except anthropic.APIError as exc:
            logger.warning("llm_transport_error", message=str(exc)[:400])
            raise ProviderError(f"anthropic api unreachable: {str(exc)[:400]}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        text = "".join(block.text for block in message.content if block.type == "text")
        if schema and (emptied := _emptied(schema)):
            text = _restore_empty(text, emptied)
        usage = message.usage
        # Anthropic reports the cached slice beside the uncached count; `pricing.price`
        # reads `cached_tokens` as a slice *of* `prompt_tokens`, so the two are summed
        # here rather than passed through, or the cached tokens would go unpriced.
        cached = usage.cache_read_input_tokens or 0
        prompt_tokens = usage.input_tokens + cached + (usage.cache_creation_input_tokens or 0)
        return Completion(
            text=text,
            tool_calls=tuple(
                {"type": "function_call", "name": block.name, "arguments": json.encode(block.input).decode()}
                for block in message.content
                if block.type == "tool_use"
            ),
            model=message.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=usage.output_tokens,
            cached_tokens=cached,
            total_tokens=prompt_tokens + usage.output_tokens,
            latency_ms=latency_ms,
            provider=self.name,
            raw={"stop_reason": message.stop_reason, "usage": usage.model_dump(mode="json"), "text": text[:4000]},
            request=audit_request(body),
        )


def build_provider() -> Provider:
    """
    Choose the provider this deployment runs on.

    Returns:
        The provider named by `POLICYDESK_PROVIDER`, or the first of these that this
        machine can actually run: the Anthropic subscription path when a credential file
        is readable, the OpenAI HTTP path when a key is set, the codex CLI otherwise.

    Anthropic leads because it is what this desk is meant to run on, and because it is
    the only one a cloud host can reach: `ANTHROPIC_OAUTH_CREDS_PATH` points at a synced
    token, and neither an API key nor a signed-in `codex` session is needed. The other
    two stay reachable, by name or by being the only thing present.

    """
    match os.environ.get("POLICYDESK_PROVIDER", "").lower():
        case "anthropic":
            return AnthropicProvider()
        case "openai":
            return OpenAIProvider()
        case "codex":
            return CodexCliProvider()
        case _:
            if SubscriptionOAuthToken.available():
                return AnthropicProvider()
            if os.environ.get("OPENAI_API_KEY"):
                logger.info("provider_selected", provider="openai", reason="no anthropic credentials")
                return OpenAIProvider()
            logger.info("provider_selected", provider="codex-cli", reason="no credentials")
            return CodexCliProvider()
