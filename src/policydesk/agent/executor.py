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

import asyncio
import re
import time
from datetime import UTC, date, datetime
from importlib import import_module
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import etoon
from msgspec import DecodeError, json

from policydesk.agent import memory, statute, tools
from policydesk.agent.scenario import (
    BY_NAME,
    CATALOGUE,
    OPENERS,
    ROUTER_INSTRUCTIONS,
    WRITING,
    Emit,
    Scenario,
    tool_schema,
)
from policydesk.bootloader import logger
from policydesk.llm.provider import Completion, Phase, Provider, ProviderError
from policydesk.skills.calculator import TOOL_SCHEMA, CalculationError, calculate
from policydesk.validation.validator import Verdict, recheck

if TYPE_CHECKING:
    from policydesk.core.db import Database
    from policydesk.retrieval.base import Retriever

# "art.17", "art.17.carve1", "waiting" — the ids the clause index actually mints.
WITHHELD = (
    "本次查詢的回覆引用了無法查證的條款或法條，為避免提供錯誤資訊，"
    "已保留該回覆並轉由專人與您確認。"
)
"""What the customer reads instead of a reply whose citations do not resolve.

A constant rather than a literal inside the branch, so a test can assert the customer got
exactly this and not the model's prose with a caveat appended. Appending a caveat still
puts the invented clause number in front of them, which is the opposite of what the check
is for.
"""

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
        self.procedure_hint: str = ""
        """What the customer called the procedure, used to look up its multiplier."""
        self.quick_replies: tuple[str, ...] = OPENERS
        """One-tap follow-ups, all of them questions. A scenario that ran replaces the
        openers with its own; one that did not leaves the customer somewhere to start."""
        self.awaiting_identity = False
        """The gate stopped this turn. The socket reads it to know that the next thing
        the customer types is probably the number, not a new question."""
        self.params: dict[str, str] = {}
        """What the router filled from the conversation. Empty before a scenario runs."""
        self.computations: tuple[tuple[str, int], ...] = ()
        """Expressions the model asked the calculator to evaluate, and their results.
        Empty means the reply states no computed figure, or states one it should not."""


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


def reachable(stage: str) -> tuple[Scenario, ...]:
    """
    List the scenarios a case at this stage can actually enter.

    Args:
        stage: The case's current stage.

    Returns:
        The catalogue minus anything whose `requires_stage` is not met.

    The router picks from what it is offered, so a scenario it must not choose is one it
    must not see. 那我適合哪一張 was being routed to the signing-stage 身分驗證 scenario,
    which answered 請輸入身分證字號完成驗證。驗證通過後，本案才會送交核保人員審核 — a
    sentence about an application that did not exist. `requires_stage` was declared on
    two scenarios and read by nothing; this is where it is read.

    """
    return tuple(s for s in CATALOGUE if s.requires_stage in (None, stage))


async def _route(
    provider: Provider, db: Database, turn: Turn, text: str, past: str, stage: str
) -> tuple[Scenario | None, dict[str, str]]:
    """
    Ask the model which scenario this turn belongs to, and with what.

    Args:
        provider: The model seam.
        db: The database, for the trace.
        turn: The turn.
        text: What the customer said.
        past: The transcript of the case so far.
        stage: The case's stage, which decides what the router may choose from.

    Returns:
        The chosen scenario and the parameters the model filled from the conversation,
        or (None, {}) when nothing fits.

    The arguments are the point. A scenario declares the parameters it needs, the model
    collects them, and until they were returned from here they were discarded — the
    gather step then ran on hardcoded defaults, so a customer who asked for 壽險 with a
    budget of 20,000 was matched against health products at 30,000.

    """
    completion = await provider.complete(
        instructions=ROUTER_INSTRUCTIONS,
        user_input=f"{past}# 本次訊息\n{text}",
        tools=[tool_schema(s) for s in reachable(stage)],
    )
    await _record(db, turn, Phase.ROUTE, completion, None)

    for call in completion.tool_calls:
        if (scenario := BY_NAME.get(call.get("name", ""))) is not None:
            try:
                args = json.decode((call.get("arguments") or "{}").encode())
            except DecodeError:
                logger.warning("router_arguments_unreadable", call=str(call)[:200])
                args = {}
            return scenario, {k: str(v) for k, v in args.items() if isinstance(args, dict)}
    turn.reply = completion.text
    return None, {}


async def _nothing() -> dict[str, Any]:
    """
    Stand in for a member read this session may not make.

    Returns:
        An empty brief. `asyncio.gather` needs an awaitable in every slot, and a
        conditional that skips the slot would change the tuple's shape.

    """
    return {}


def _as_budget(raw: str) -> int | None:
    """
    Read the budget the model collected.

    Args:
        raw: What the model put in the parameter.

    Returns:
        The annual premium as an integer, or None when it is not a plain number. None
        means no recommendation is made: a budget filter that silently falls back to a
        default is how a customer is shown a product they said they cannot afford.

    """
    try:
        return int(raw.strip().replace(",", ""))
    except (AttributeError, ValueError):
        logger.warning("budget_unreadable", raw=str(raw)[:60])
        return None


async def _gather(
    db: Database,
    scenario: Scenario,
    turn: Turn,
    *,
    today: date,
    params: dict[str, str],
    confirmed: bool = True,
    index: Retriever | None = None,
) -> dict[str, Any]:
    """
    Run the scenario's tools, on what the model collected.

    Args:
        db: The database.
        scenario: Which scenario is running.
        turn: The turn.
        today: The date to judge currency against.
        params: The scenario parameters the router filled from the conversation.
        confirmed: Whether this session has passed 資料核對. Unconfirmed, every tool
            marked `requires_identity` is skipped — the query never runs, so there is
            nothing read and then withheld.
        index: The clause retriever, when one is open. None falls back to the SQL
            search, which ranks worse and still answers.

    Returns:
        Everything the tools returned, by tool name. Unconfirmed, only the public ones.

    """
    if scenario.tools_module:
        # A scenario whose tools live in their own module is dispatched there, and its
        # gate is derived from that module's `TOOLS` rather than from `agent.tools` —
        # where the names are not defined, and would be gated as unknown. The two halves
        # have to move together: resolving the gate somewhere the dispatch does not look
        # is how a scenario ends up demanding an ID for a question about public law.
        owner = import_module(scenario.tools_module)
        # Per tool, not per scenario. A scenario that mixes a public statute lookup with
        # a read of the member's own book still answers the public half unverified, with
        # the request for an ID attached to material rather than standing in for it.
        allowed = tools.permitted(scenario.tools, owner=owner, confirmed=confirmed)
        facts = await owner.gather(
            db, params, member_id=turn.member_id, today=today, retriever=index, allowed=allowed
        )
        if allowed != frozenset(scenario.tools):
            # Set here rather than trusted to the module. A module that forgets the flag
            # would hand the model a partial answer with nothing saying a part is missing,
            # and the model would present it as the whole answer.
            facts["_identity_required"] = True
        # Empty rather than absent. A scenario module citing contract clauses returns its
        # own allow-list; one citing statute carries the 〔保險法 第64條第2項〕 syntax,
        # which the `art.NN` checker never sees, so it has nothing here to allow.
        facts.setdefault("_allowed_clauses", frozenset())
        return facts
    # Per tool, exactly as the `tools_module` branch does it. Asking the question per
    # scenario let a scenario whose declared tools are all public — `browse_products`,
    # whose only tool is `catalogue_sample` — fall through to the unconditional
    # `list_policies` below and put an unverified visitor's whole book in the prompt.
    allowed = tools.permitted(scenario.tools, confirmed=confirmed)
    facts: dict[str, Any] = {}
    if not confirmed:
        # `list_policies` and `clause_ids_for` are member reads that no scenario declares,
        # so `permitted` never sees them and they need saying out loud. With no book there
        # is no citable clause either, which is the right answer: a clause id in a reply
        # to an unverified session is one nothing can check.
        facts["_identity_required"] = True
        facts["_allowed_clauses"] = frozenset()
        product_ids: list[str] = []
        pending: dict[str, Any] = {}
    else:
        # list_policies first, because everything else needs its product_ids.
        policies = await tools.list_policies(db, turn.member_id, today=today)
        product_ids = [p["product_id"] for p in policies]
        facts["list_policies"] = policies
        # The rest depend on product_ids and member_id, not on each other, so they go out
        # together rather than one round trip at a time. Two or three queries per turn on
        # a local database is a few milliseconds; against a networked one it is a full
        # round trip each, on the path a customer is waiting on.
        pending = {"_allowed_clauses": tools.clause_ids_for(db, product_ids)}
    if "find_clause" in allowed:
        pending["find_clause"] = tools.find_clause(db, product_ids, params.get("topic", ""), index=index)
    if "find_multiplier" in allowed:
        pending["find_multiplier"] = tools.find_multiplier(db, product_ids, params.get("event", turn.procedure_hint))
    if "catalogue_sample" in allowed:
        pending["catalogue_sample"] = tools.catalogue_sample(db, params.get("line", "health"))
    if "benefit_headings" in allowed:
        pending["benefit_headings"] = tools.benefit_headings(db, product_ids)
    if "required_documents" in allowed:
        pending["required_documents"] = tools.required_documents(db, product_ids)
    if "billing_summary" in allowed:
        pending["billing_summary"] = tools.billing_summary(db, turn.member_id, today=today)
    if "coverage_summary" in allowed:
        pending["coverage_summary"] = tools.coverage_summary(db, turn.member_id, today=today)

    results = await asyncio.gather(*pending.values())
    facts.update(dict(zip(pending, results, strict=True)))

    if "suitable_products" in allowed and confirmed:
        # Not "a public tool calls a marked one" — `suitable_products` calls nothing, and
        # an audit of the tool graph comes back clean. It is this function that calls
        # `member_underwriting`, inline, in a branch keyed on an unmarked tool's name. So
        # the class to go looking for is a `tools.X(` call site in *this body* that no
        # `X in allowed` guards, and there are three: this one, and `list_policies` and
        # `clause_ids_for` in the `else:` above.
        member = await tools.member_underwriting(db, turn.member_id, today=today)
        budget = _as_budget(params.get("budget", ""))
        if member and budget is not None:
            age = member["insurance_age"]
            facts["suitable_products"] = await tools.suitable_products(
                db,
                insurance_age=age,
                occupation_class=member["occupation_class"],
                budget=budget,
                line=params.get("line", ""),
            )
            if not facts["suitable_products"]:
                # Nothing qualified, so find out what would. Six probes, each dropping
                # exactly one condition, sent together — a customer waiting on a refusal
                # should not wait on it six times over. Answering 沒有符合的商品 and
                # stopping there leaves them with nowhere to go, which is the one thing
                # an adviser in this position never does.
                facts["alternatives"] = await tools.alternatives(
                    db,
                    insurance_age=age,
                    occupation_class=member["occupation_class"],
                    budget=budget,
                    line=params.get("line", ""),
                )
            facts["_criteria"] = {
                "insurance_age": age,
                "occupation_class": member["occupation_class"],
                "budget": budget,
                "line": params.get("line", ""),
            }

    return facts


async def _public_only(db: Database, scenario: Scenario, params: dict[str, str]) -> dict[str, Any]:
    """
    Gather what can be said to someone who has not proved who they are.

    Args:
        db: The database.
        scenario: Which scenario is running.
        params: What the router collected.

    Returns:
        Public material only, plus a marker the injection reads. No member row is
        touched, so the decorator's promise holds at the query and not merely in the
        prose written afterwards.

    """
    # No member, so no contracts, so no clause id is legitimately citable. A citation
    # written here fails the recheck and the reply is withheld — which is the correct
    # outcome: an unverified session being told a clause number from someone's policy is
    # the leak this whole gate exists to prevent.
    facts: dict[str, Any] = {"_identity_required": True, "_allowed_clauses": frozenset()}
    if {"suitable_products", "catalogue_sample"} & set(scenario.tools):
        facts["catalogue_sample"] = await tools.catalogue_sample(db, params.get("line", "health"))
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
    provider: Provider,
    db: Database,
    *,
    case_id: int,
    member_id: int,
    text: str,
    confirmed: bool = False,
    index: Retriever | None = None,
    today: date | None = None,
) -> Turn:
    """
    Handle one thing the customer said.

    Args:
        provider: The model seam.
        db: The database.
        case_id: Which case.
        member_id: Whose case.
        text: What the customer said.
        confirmed: Whether this session has passed 資料核對. Everything this desk knows
            is about one named person's contracts, so an unconfirmed session reaches no
            tool marked `requires_identity` — the scenario is refused before its tools
            run, not after.
        today: The date to judge currency against.

    Returns:
        The turn, carrying the reply and anything that failed a check.

    A model that cannot be reached produces a turn that says so. It never produces a
    turn that answers anyway.

    """
    today = today or datetime.now(UTC).date()
    turn = Turn(case_id, member_id)
    # Two layers, gathered together. The transcript is the current session, cut at an
    # hour's silence; the card is what outlived that window. A customer who asks about a
    # claim, then a premium, then comes back to the application is coherent only because
    # the second layer exists — the budget they stated six turns ago is on the card
    # whether or not it is still in the transcript.
    #
    # The customer's own message is already written, so the window drops its last row
    # and never repeats what the router is reading as this turn.
    stage = await db.fetch_val('SELECT stage FROM "case" WHERE case_id = $1::bigint', [case_id]) or "inquiry"
    messages, profile, brief = await asyncio.gather(
        memory.recent(db, case_id),
        memory.card(db, member_id=member_id, case_id=case_id),
        tools.standing_brief(db, member_id, today=today) if confirmed else _nothing(),
    )
    # The brief is what turns a clarifying question into a grounded one. Without it the
    # router can only ask 想了解哪一項保障主題, which makes the customer do a lookup the
    # desk could have done; holding it, the same question arrives with the answers in it.
    known = f"# 這位保戶的現況\n{etoon.dumps(brief)}\n\n" if brief else ""
    if confirmed:
        # Stated, because the summary the sweep wrote during the unverified half of the
        # conversation says the opposite, and the model believed it — answering a
        # verified customer with 目前尚無法執行身分核對 one turn after the check passed.
        known = f"# 本次連線已完成身分核對，可以查詢這位保戶的資料\n\n{known}"
    if not confirmed:
        # Told to the model, so the ask arrives in the conversation rather than as a
        # system refusal, and enforced below, so a model that ignores it still reads
        # nothing. The prompt makes it natural; the gate makes it true.
        #
        # Note what it does not say: "ask for the number". A greeting is not a request
        # for anyone's policy data, and answering 嗨 with 請提供您的身分證字號 is a desk
        # frisking someone at the door. The check belongs to the question that needs it.
        known = (
            "# 本次連線尚未完成身分核對\n"
            "保戶還沒核對身分，所以你看不到他的任何保單資料。\n"
            "打招呼、詢問服務範圍、詢問一般保險常識這類不涉及他個人資料的問題，照常回答。\n"
            "一旦他問到自己的保單、保費、保額、理賠或投保規劃，就需要先核對身分——"
            "那時再請他提供身分證字號，不要提早要。\n"
            "任何情況下都不要猜測或編造他的保單內容。\n\n"
        )
    past = f"{known}{profile}{memory.transcript(messages[:-1])}"

    started = time.perf_counter()
    try:
        scenario, params = await _route(provider, db, turn, text, past, stage)
    except ProviderError as exc:
        latency = int((time.perf_counter() - started) * 1000)
        logger.warning("turn_unrouted", case_id=case_id, error=str(exc))
        await _record_failure(db, turn, Phase.ROUTE, None, str(exc), latency)
        turn.reply = "櫃台的語言服務目前無回應，請稍候再試，或改由專人與您聯繫。"
        return turn

    if scenario is None:
        # The router answers directly when nothing fits — `ROUTER_INSTRUCTIONS` says so in
        # as many words — and that answer used to be the one reply nothing checked. It has
        # no tools behind it, so no clause id is allowed and any citation in it is one the
        # model invented.
        await _unverifiable(db, turn, turn.reply, frozenset())
        return turn

    if not confirmed and tools.reads_identity(scenario.tools):
        # The gate withholds the member queries, not the conversation. Refusing the whole
        # scenario made the desk answer 我想加保 with nothing but a demand for a number;
        # what it should do is say what exists and ask for the number to judge the fit.
        # `_gather` skips every tool marked `requires_identity`, so no member row is read
        # — the withholding happens before the query, not after it.
        logger.info("scenario_gated", case_id=case_id, scenario=scenario.name)
        turn.awaiting_identity = True

    turn.scenario = scenario.name
    turn.quick_replies = scenario.quick_replies or OPENERS
    turn.procedure_hint = text
    turn.params = params
    facts = await _gather(db, scenario, turn, today=today, params=params, confirmed=confirmed, index=index)
    allowed: frozenset[str] = facts.pop("_allowed_clauses")

    if facts.get("_identity_required") and scenario.emit is Emit.TEMPLATE:
        # A template fills from rows, and the withheld query has none — so 您名下有效保單
        # 共 0 張 is what it renders. That is a false statement about the customer rather
        # than a withheld one, and it is the exact failure the review scenario's own
        # docstring names. The model path says the same thing correctly, because it reads
        # `_identity_required`; this path has no model to read it.
        turn.awaiting_identity = True
        turn.reply = (
            "查詢您名下的保單資料前，需要先核對您的身分。"
            "請提供您的身分證字號，核對通過後我立刻為您查詢。"
        )
        return turn

    if scenario.emit is Emit.TEMPLATE:
        turn.reply = _render(scenario, facts)
        return turn

    # etoon, not JSON. Tool results are tabular — the same keys over and over — and TOON
    # states each row's field names once instead of once per row. Measured on a product
    # list here: 41% fewer characters for the same rows, on the prompt the customer is
    # waiting on.
    material = etoon.dumps({k: _short(v) for k, v in facts.items()})
    answering = time.perf_counter()
    try:
        # The calculator is offered here, not merely described. Without it the
        # instruction "金額由計算工具產生" had no mechanism behind it: the model wrote
        # figures into prose from the material it had been handed, and nothing checked
        # them. A tool the model cannot reach is a claim, not a guarantee.
        completion = await provider.complete(
            instructions=f"{ROUTER_INSTRUCTIONS}\n\n{scenario.injection}\n\n{WRITING}",
            user_input=f"{past}# 本次訊息\n{text}\n\n# 工具回傳\n{material}",
            tools=[TOOL_SCHEMA],
        )
    except ProviderError as exc:
        latency = int((time.perf_counter() - answering) * 1000)
        logger.warning("turn_unanswered", case_id=case_id, scenario=scenario.name, error=str(exc))
        await _record_failure(db, turn, Phase.ANSWER, scenario.name, str(exc), latency)
        turn.reply = "櫃台的語言服務目前無回應，本次查詢已記錄，請稍候再試。"
        return turn

    await _record(db, turn, Phase.ANSWER, completion, scenario.name)
    turn.computations = _run_calculations(completion)

    if await _unverifiable(db, turn, completion.text, allowed):
        return turn
    if not completion.text.strip():
        # A model that answered with tool calls and no prose leaves the customer
        # looking at an empty bubble. There is no second round here to fill it in.
        logger.warning("answer_empty", case_id=case_id, scenario=scenario.name)
        turn.reply = "本次查詢未能組出完整回覆，已保留紀錄並轉由專人與您聯繫。"
        return turn
    turn.reply = completion.text
    return turn


async def _unverifiable(db: Database, turn: Turn, text: str, allowed: frozenset[str]) -> bool:
    """
    Withhold a reply whose citations do not resolve.

    Args:
        db: The database, for the statute corpus.
        turn: The turn, whose `citations`, `faults` and `reply` this sets.
        text: What the model wrote.
        allowed: The clause ids the tools actually returned for this member.

    Returns:
        True when the reply was withheld, so the caller stops.

    Two corpora, two syntaxes, one gate. Clause ids are read out of the text and checked
    against what the tools returned; statute citations are read out in their own bracketed
    form and checked against `statute_article`. Both are read OUT of the reply rather than
    intersected with what is allowed — intersecting would only ever find ids that exist,
    so it would pass every time and prove nothing. The failure being guarded against is a
    number the model wrote and no document contains.

    The unverifiable text is withheld, not annotated. Appending a caveat still puts the
    invented number in front of the customer, which is the opposite of the point.

    """
    cited = tuple(dict.fromkeys(_CITATION.findall(text)))
    checked = recheck(Verdict(passed=True, reason="", cited_clauses=cited), subject={}, allowed_clauses=allowed)
    fabricated = await statute.unresolved(db, text)
    turn.citations = cited
    turn.faults = checked.faults + tuple(f"{name}{doc_id}" for name, doc_id in fabricated)
    if checked.trustworthy and not fabricated:
        return False
    logger.warning(
        "citation_unresolved", case_id=turn.case_id, faults=list(checked.faults), statute=list(fabricated)
    )
    turn.reply = WITHHELD
    return True


def _run_calculations(completion: Completion) -> tuple[tuple[str, int], ...]:
    """
    Evaluate every calculator call the model made.

    Args:
        completion: What the model returned.

    Returns:
        Each expression paired with its amount. A call whose expression steps outside
        the allow-list is dropped and logged rather than guessed at, so a figure either
        came from the calculator or does not exist.

    """
    results: list[tuple[str, int]] = []
    for call in completion.tool_calls:
        if call.get("name") != "calculate":
            continue
        try:
            expression = json.decode((call.get("arguments") or "{}").encode())["expression"]
            results.append((expression, calculate(expression).amount))
        except (CalculationError, KeyError, ValueError) as exc:
            logger.warning("calculation_rejected", call=str(call)[:200], error=str(exc))
    return tuple(results)


def _short(value: Any, limit: int = 12) -> Any:
    """
    Trim a tool result to what fits in a prompt, in types the encoder accepts.

    Args:
        value: Rows or a scalar.
        limit: Most rows to keep.

    Returns:
        The value, with long lists truncated, long text clipped, and dates and Decimals
        rendered as primitives.

    The type conversion is here rather than at the call site because this is the one
    function that already walks the whole structure. etoon serialises through stdlib
    json, which raises on a `date` — and a tool result carrying a policy's effective
    date is the common case, not the edge one.

    """
    match value:
        case list():
            return [_short(v) for v in value[:limit]]
        case dict():
            return {k: _short(v) for k, v in value.items()}
        case str():
            return value[:400]
        case datetime() | date():
            return value.isoformat()
        case _ if hasattr(value, "as_tuple"):  # Decimal
            return float(value)
        case _:
            return value
