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

import os
import time
from enum import StrEnum
from typing import Any, Protocol

import aiohttp
import stamina
from msgspec import Struct

from policydesk.bootloader import logger

RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = os.environ.get("POLICYDESK_MODEL", "gpt-5.6-luna")


class Phase(StrEnum):
    """
    Where in a turn a model call sits.

    Matches the `llm_usage.phase` check constraint. `VALIDATE` is this project's
    addition to the enoract set: a prompt-based check is itself a traced call, which
    is how a non-deterministic verdict still leaves an auditable record.
    """

    ROUTE = "route"
    SCENARIO_TOOLS = "scenario_tools"
    ANSWER = "answer"
    VALIDATE = "validate"
    REPAIR = "repair"
    EMBEDDING = "embedding"


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
    ) -> Completion:
        """
        Ask the model once.

        Args:
            instructions: The system-level brief. Stable across a scenario.
            user_input: What this turn is about.
            tools: Tool schemas the model may call.
            schema: A JSON Schema the reply must satisfy, for a structured verdict.
            model: Overrides the configured model.

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
    ) -> Completion:
        """
        Ask the model once over HTTP.

        Args:
            instructions: The system-level brief.
            user_input: What this turn is about.
            tools: Tool schemas the model may call.
            schema: A JSON Schema the reply must satisfy.
            model: Overrides the configured model.

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
    ) -> Completion:
        """
        Return the scripted answer for this input.

        Args:
            instructions: Ignored, but recorded.
            user_input: Looked up in the script.
            tools: Ignored.
            schema: Ignored.
            model: Recorded as the model name.

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
