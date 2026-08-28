"""
Run one turn: route it, gather the facts, say something.

Three steps, and the middle one is where the guarantee lives.

**Route.** The model picks a scenario by calling its tool. It may not answer from this
step, only choose — a router that answers has skipped every fact-gathering tool below
it.

**Gather.** Deterministic tools read the database. Nothing here asks a model anything.

**Say.** A scenario marked TEMPLATE renders its own text from those rows and no model
is called at all. Everything that states a figure, a document requirement or a clause
runs this way. A scenario marked MODEL passes the rows to the model, which writes the
prose around them and may cite clause ids — and every id it cites is checked against
the ids the tools actually returned before the reply leaves this function.

Every model call is written to `llm_usage` with its phase, turn, tokens and latency,
which is what makes the trace view a record rather than a diagram.
"""

import re
import time
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from msgspec import json

from policydesk.agent import tools
from policydesk.agent.scenario import BY_NAME, CATALOGUE, ROUTER_INSTRUCTIONS, Emit, Scenario, tool_schema
from policydesk.bootloader import logger
from policydesk.llm.provider import Completion, Phase, Provider, ProviderError
from policydesk.validation.validator import Verdict, recheck

if TYPE_CHECKING:
    from policydesk.core.db import Database

# "art.17", "art.17.carve1", "waiting" — the ids the clause index actually mints.
_CITATION = re.compile(r"\b(?:art\.\d{1,3}(?:\.carve\d)?|waiting)\b")


class Turn:
    """One exchange, and everything it touched."""

    def __init__(self, case_id: int, member_id: int) -> None:
        self.case_id = case_id
        self.member_id = member_id
        self.turn_id = f"t-{uuid4().hex[:12]}"
        self.reply = ""
        self.scenario: str | None = None
        self.citations: tuple[str, ...] = ()
        self.faults: tuple[str, ...] = ()


async def _record_failure(db: Database, turn: Turn, phase: Phase, scenario: str | None, error: str, latency_ms: int) -> None:
    """
    Record a model call that never returned.

    Args:
        db: The database.
        turn: The turn it belonged to.
        phase: Where in the turn it sat.
        scenario: Which scenario was active, if one had been chosen.
        error: What the provider said.
        latency_ms: How long the attempt took before giving up.

    An outage is the entry the trail most needs. Recording only successes leaves an
    auditor unable to distinguish "the desk never tried" from "the desk tried and the
    provider was silent", and the FSC guidance asks for the second.

    """
    await db.execute(
        """INSERT INTO llm_usage (case_id, turn_id, phase, scenario, provider, model, latency_ms, request, response)
           VALUES ($1::bigint,$2::text,$3::text,$4::text,$5::text,$6::text,$7::int,$8::jsonb,$9::jsonb)""",
        [turn.case_id, turn.turn_id, phase.value, scenario, "openai", "", latency_ms,
         {"scenario": scenario}, {"error": error[:2000]}],
    )


async def _record(db: Database, turn: Turn, phase: Phase, completion: Completion, scenario: str | None) -> None:
    """
    Write one model call to the trace.

    Args:
        db: The database.
        turn: The turn it belonged to.
        phase: Where in the turn it sat.
        completion: What came back.
        scenario: Which scenario was active.

    """
    await db.execute(
        """INSERT INTO llm_usage (case_id, turn_id, phase, scenario, provider, model,
                                  prompt_tokens, completion_tokens, cached_tokens, total_tokens,
                                  latency_ms, request, response)
           VALUES ($1::bigint,$2::text,$3::text,$4::text,$5::text,$6::text,
                   $7::int,$8::int,$9::int,$10::int,$11::int,$12::jsonb,$13::jsonb)""",
        [
            turn.case_id, turn.turn_id, phase.value, scenario, completion.provider, completion.model,
            completion.prompt_tokens, completion.completion_tokens, completion.cached_tokens,
            completion.total_tokens, completion.latency_ms,
            {"scenario": scenario}, {"text": completion.text[:2000]},
        ],
    )


async def _route(provider: Provider, db: Database, turn: Turn, text: str) -> Scenario | None:
    """
    Ask the model which scenario this turn belongs to.

    Args:
        provider: The model seam.
        db: The database, for the trace.
        turn: The turn.
        text: What the customer said.

    Returns:
        The chosen scenario, or None when nothing fits.

    """
    completion = await provider.complete(
        instructions=ROUTER_INSTRUCTIONS,
        user_input=text,
        tools=[tool_schema(s) for s in CATALOGUE],
    )
    await _record(db, turn, Phase.ROUTE, completion, None)

    for call in completion.tool_calls:
        if (scenario := BY_NAME.get(call.get("name", ""))) is not None:
            return scenario
    turn.reply = completion.text
    return None


async def _gather(db: Database, scenario: Scenario, turn: Turn, *, today: date) -> dict[str, Any]:
    """
    Run the scenario's tools.

    Args:
        db: The database.
        scenario: Which scenario is running.
        turn: The turn.
        today: The date to judge currency against.

    Returns:
        Everything the tools returned, by tool name.

    """
    policies = await tools.list_policies(db, turn.member_id, today=today)
    product_ids = [p["product_id"] for p in policies]
    facts: dict[str, Any] = {"list_policies": policies}

    if "find_clause" in scenario.tools:
        facts["find_clause"] = await tools.find_clause(db, product_ids, "住院")
    if "required_documents" in scenario.tools:
        facts["required_documents"] = await tools.required_documents(db, product_ids)
    if "billing_summary" in scenario.tools:
        facts["billing_summary"] = await tools.billing_summary(db, turn.member_id, today=today)
    if "coverage_summary" in scenario.tools:
        facts["coverage_summary"] = await tools.coverage_summary(db, turn.member_id, today=today)
    if "suitable_products" in scenario.tools:
        member = await db.fetch_one(
            "SELECT birth_date, occupation_class FROM member WHERE member_id = $1::bigint", [turn.member_id]
        )
        if member:
            age = today.year - member["birth_date"].year
            facts["suitable_products"] = await tools.suitable_products(
                db, insurance_age=age, occupation_class=member["occupation_class"], budget=30000
            )

    facts["_allowed_clauses"] = await tools.clause_ids_for(db, product_ids)
    return facts


def _render(scenario: Scenario, facts: dict[str, Any]) -> str:
    """
    Fill a TEMPLATE scenario from the rows the tools returned.

    Args:
        scenario: The scenario, whose template is being rendered.
        facts: What the tools returned.

    Returns:
        The reply, assembled from data rather than generated.

    """
    match scenario.name:
        case "billing":
            summary = facts.get("billing_summary") or {}
            return scenario.template.format(
                active=summary.get("active", 0), premium=f"{float(summary.get('premium') or 0):,.0f}"
            )
        case "coverage":
            rows = facts.get("coverage_summary") or []
            lines = "\n".join(f"　{r['product_name']}（{r['policy_number']}）：{r['sum_insured']:,} 元" for r in rows)
            return scenario.template.format(lines=lines or "　（查無有效保單）")
        case _:
            return scenario.template


async def run_turn(
    provider: Provider, db: Database, *, case_id: int, member_id: int, text: str, today: date | None = None
) -> Turn:
    """
    Handle one thing the customer said.

    Args:
        provider: The model seam.
        db: The database.
        case_id: Which case.
        member_id: Whose case.
        text: What the customer said.
        today: The date to judge currency against.

    Returns:
        The turn, carrying the reply and anything that failed a check.

    A model that cannot be reached produces a turn that says so. It never produces a
    turn that answers anyway.

    """
    today = today or datetime.now(UTC).date()
    turn = Turn(case_id, member_id)

    started = time.perf_counter()
    try:
        scenario = await _route(provider, db, turn, text)
    except ProviderError as exc:
        latency = int((time.perf_counter() - started) * 1000)
        logger.warning("turn_unrouted", case_id=case_id, error=str(exc))
        await _record_failure(db, turn, Phase.ROUTE, None, str(exc), latency)
        turn.reply = "櫃台的語言服務目前無回應，請稍候再試，或改由專人與您聯繫。"
        return turn

    if scenario is None:
        return turn

    turn.scenario = scenario.name
    facts = await _gather(db, scenario, turn, today=today)
    allowed: frozenset[str] = facts.pop("_allowed_clauses")

    if scenario.emit is Emit.TEMPLATE:
        turn.reply = _render(scenario, facts)
        return turn

    material = json.encode({k: _short(v) for k, v in facts.items()}).decode()
    answering = time.perf_counter()
    try:
        completion = await provider.complete(
            instructions=f"{ROUTER_INSTRUCTIONS}\n\n{scenario.injection}",
            user_input=f"# 保戶說\n{text}\n\n# 工具回傳\n{material}",
        )
    except ProviderError as exc:
        latency = int((time.perf_counter() - answering) * 1000)
        logger.warning("turn_unanswered", case_id=case_id, scenario=scenario.name, error=str(exc))
        await _record_failure(db, turn, Phase.ANSWER, scenario.name, str(exc), latency)
        turn.reply = "櫃台的語言服務目前無回應，本次查詢已記錄，請稍候再試。"
        return turn

    await _record(db, turn, Phase.ANSWER, completion, scenario.name)

    # Read the citations OUT of the reply, then check them against what the tools
    # returned. Intersecting `allowed` with the text instead would only ever find ids
    # that exist, so it would pass every time and prove nothing — the failure this
    # guards against is a clause number the model wrote and no contract contains.
    cited = tuple(dict.fromkeys(_CITATION.findall(completion.text)))
    checked = recheck(
        Verdict(passed=True, reason="", cited_clauses=cited),
        subject={},
        allowed_clauses=allowed,
    )
    turn.citations = cited
    turn.faults = checked.faults
    if not checked.trustworthy:
        logger.warning("citation_unresolved", case_id=case_id, faults=list(checked.faults))
        turn.reply = (
            f"{completion.text}\n\n"
            "（本次回覆引用的部分條號無法在您的保單中查得，該部分請以專人確認為準。）"
        )
        return turn
    turn.reply = completion.text
    return turn


def _short(value: Any, limit: int = 12) -> Any:
    """
    Trim a tool result to what fits in a prompt.

    Args:
        value: Rows or a scalar.
        limit: Most rows to keep.

    Returns:
        The value, with long lists truncated and long text clipped.

    """
    match value:
        case list():
            return [_short(v) for v in value[:limit]]
        case dict():
            return {k: (v[:400] if isinstance(v, str) else _short(v)) for k, v in value.items()}
        case _:
            return value
