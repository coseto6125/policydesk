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
from collections import defaultdict
from datetime import date, datetime
from importlib import import_module
from itertools import zip_longest
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import etoon
from msgspec import DecodeError, Struct, json, structs

from policydesk.agent import i18n, memory, statute, tools
from policydesk.agent import locale as lang
from policydesk.agent.scenario import (
    ASKED_ALREADY,
    CATALOGUE,
    DOCUMENT_PROGRESS,
    IDENTITY_LOCKED_REPLY,
    IDENTITY_NEXT_STEP,
    LOOKUP_SCOPE,
    OPENERS,
    OUT_OF_SCOPE,
    POLICY_CLARIFICATION,
    PUBLIC_OPENERS,
    ROUTER_INSTRUCTIONS,
    WRITING,
    Emit,
    Scenario,
    anchored,
    closing_rules,
    tool_schema,
)
from policydesk.bootloader import logger
from policydesk.llm.pricing import cost
from policydesk.llm.provider import Completion, Phase, Provider, ProviderError
from policydesk.skills import dates
from policydesk.skills.calculator import CalculationError, calculate
from policydesk.validation.validator import QuotedField, Verdict, recheck

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


class _ProvisionQuote(Struct, forbid_unknown_fields=True):
    field: str
    text: str
    kind: Literal["benefit_condition", "exclusion", "waiting_period", "deadline"]


class _Answer(Struct, forbid_unknown_fields=True):
    reply: str
    citations: list[str]
    calculations: list[str]
    quoted_fields: list[_ProvisionQuote] = []
    date_calculations: list[str] = []


# The keys of `_Answer` beside `reply`, in the shape they take when a reply string keeps
# going after the field should have closed: `", "citations": [`. The JSON still decodes,
# `reply` is a valid string, and the customer reads the desk's own schema at the foot of
# their answer. Observed as a structured-output corruption in raccoon-ai-platform, which
# repairs it with a second model call; this desk withholds, as it does every other
# malformed answer. The match wants the closing quote, the comma and the quoted key —
# the structure, not the word — so a reply that mentions `citations:` in a sentence is
# not a leak, and a leak followed by three hundred characters of quoted clause still is.
_LEAK = re.compile(r'"\s*,\s*"(?:citations|calculations|date_calculations|quoted_fields)"\s*:')

UNDATED = (
    "本次回覆提到的日期無法對應到保單資料或日期工具的計算結果，"
    "為避免提供錯誤的期限，已保留該回覆並轉由專人與您確認。"
)
"""What the customer reads instead of a reply that states a date nothing backs.

A deadline is the figure a customer acts on first, and one day out is a claim not filed
in time. Withheld rather than annotated, for `WITHHELD`'s reason.
"""

# The one date form the reply is held to: ISO with a year a policy can carry, bounded by
# non-digits, which is the form the date tool emits and the material carries. A reply is
# read for this form only. Chinese and ROC forms are read from the *sources* (below) so a
# date the customer wrote as 民國115年3月1日 supports a reply that writes it 2026-03-01,
# but they are not enforced in the reply: a regex wide enough to catch 3月11日 also
# catches a service line (0800-01-1234) and a division (1000/10/2), and a withheld reply
# costs the customer more than a date form this check does not read. `anchored` tells
# the model to write dates in this form. The boundary is digits only, so a date at the
# end of an English sentence (on 2026-03-01.) and a record's ISO datetime
# (2026-03-01T00:00:00+08:00) both read; the year range is what keeps an identifier
# such as P_1234-01-02 from reading as one.
# Month and day take one or two digits, the same as the source side: a reply that writes
# 2026-3-15 is stating a date, and a check that read only zero-padded ones let exactly
# the invented deadline through that it exists to catch.
_ISO_IN_REPLY = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
# A calendar date as a customer, a clause or a record writes it: 2026-03-01, 2026/3/1,
# 2026.03.01, 2026年3月1日, 民國115年3月1日, 民國99年3月1日, 115/03/01. A year of one to
# three digits is 民國 and gains 1911.
_DATE_IN_SOURCE = re.compile(
    r"(?<!\d)(?:(民國)\s*(\d{1,3})|(\d{4})|(\d{3}))\s*[年/.-]\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})\s*日?(?!\d)"
)
# A month and day with no year — 3月1日, 3月1號, 3/1 — which a customer writes for a date
# in the current year, and which the reply completes with the year `anchored` gave it.
# Read only after every full date has been blanked out, so 2025年 3月1日 is one date with
# a year, not a bare month and day that would license 2026-03-01.
_MONTH_DAY = re.compile(r"(?<![\d年/.-])(\d{1,2})\s*(?:月|/)\s*(\d{1,2})\s*[日號]?(?![\d/.-])")
# Fullwidth digits and separators fold to ASCII before either regex runs, on the reply
# and on the sources alike, so a customer's ２０２６－０３－０１ supports the reply's 2026-03-01.
_ASCII = str.maketrans("０１２３４５６７８９－／．", "0123456789-/.")
_YEARS = range(1900, 2101)


def _answer_schema(
    sources: tuple[tuple[str, str], ...], *, calculator: bool = False, dates: bool = False
) -> dict[str, Any]:
    definitions = json.schema(_Answer)["$defs"]
    schema = definitions["_Answer"]
    schema["required"] = list(schema["properties"])
    quotes = schema["properties"]["quoted_fields"]
    quotes.pop("default", None)
    spans = schema["properties"]["date_calculations"]
    spans.pop("default", None)
    spans["description"] = (
        "One expression per calendar date or day count the reply states that is not written in the "
        "material: a date from the material or from the customer, then spans, for example "
        "'2026-03-01 + 1 日 + 10 日' or 'today - 2025-12-20'. Use 'today' for the current date. "
        "Use an empty list when the reply states no such date."
    )
    quotes["items"] = definitions["_ProvisionQuote"]
    quotes["description"] = (
        "For claims about benefit conditions, exclusions, waiting periods or deadlines, quote the supporting "
        "clause text verbatim. Use product_id|clause_id as field and include that key in citations. "
        "Use an empty list when the reply makes none of these four kinds of claim."
    )
    citations = schema["properties"]["citations"]
    if sources:
        citations["items"]["enum"] = [f"{product}|{clause}" for product, clause in sources]
        quotes["items"]["properties"]["field"]["enum"] = citations["items"]["enum"]
        quotes["items"]["properties"]["text"]["minLength"] = 1
    else:
        citations["maxItems"] = 0
        quotes["maxItems"] = 0
    if not calculator:
        schema["properties"]["calculations"]["maxItems"] = 0
    if not dates:
        spans["maxItems"] = 0
    return schema


class Turn:
    """One exchange, and everything it touched."""

    def __init__(self, case_id: int, member_id: int) -> None:
        self.case_id = case_id
        self.member_id = member_id
        self.turn_id = f"t-{uuid4().hex[:12]}"
        self.reply = ""
        self.scenario: str | None = None
        self.citations: tuple[str, ...] = ()
        self.clause_sources: tuple[tuple[str, str], ...] = ()
        self.clause_texts: dict[str, str] = {}
        self.cited_sources: tuple[tuple[str, str], ...] = ()
        self.faults: tuple[str, ...] = ()
        self.procedure_hint: str = ""
        """What the customer called the procedure, used to look up its multiplier."""
        self.quick_replies: tuple[str, ...] = OPENERS
        """Replaced by the scenario's own, or by the public set when nothing ran and the
        session is unverified."""
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
        self.dates: tuple[tuple[str, str], ...] = ()
        """Date expressions the model wrote, and what the date tool made of each. Same
        contract as `computations`: a date either came from here or from a row."""
        self.evidence: dict[str, Any] = {}
        """What the answer stood on, for the record beside the reply: the clause keys the
        tools offered, each one's retrieval score, and whether the evidence budget cut
        anything. Empty when no model wrote the reply. Read offline, never by the model —
        the scores are popped out of the material before it is serialised."""
        self.locale: str = lang.DEFAULT
        """The language the reply is written in, as `agent.locale` read it off the
        customer's message. The prompt names it, and the chips are rendered in it."""


async def _record_failure(
    db: Database, turn: Turn, phase: Phase, scenario: str | None, error: str, latency_ms: int, provider: str
) -> None:
    """
    Record a model call that never returned.

    Args:
        db: The database.
        turn: The turn it belonged to.
        phase: Where in the turn it sat.
        scenario: Which scenario was active, if one had been chosen.
        error: What the provider said.
        latency_ms: How long the attempt took before giving up.
        provider: Which seam was serving. Passed rather than assumed: this column read
            `"openai"` for every outage whatever answered, so an auditor counting which
            provider goes silent was reading a constant. `_record` has always written
            `completion.provider`; a failure has no completion to read it off.

    An outage is the entry the trail most needs. Recording only successes leaves an
    auditor unable to distinguish "the desk never tried" from "the desk tried and the
    provider was silent", and the FSC guidance asks for the second.

    """
    await db.execute(
        """INSERT INTO llm_usage (case_id, turn_id, phase, scenario, provider, model, latency_ms, request, response)
           VALUES ($1::bigint,$2::text,$3::text,$4::text,$5::text,$6::text,$7::int,$8::jsonb,$9::jsonb)""",
        [turn.case_id, turn.turn_id, phase.value, scenario, provider, "", latency_ms,
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

    The response column carries the tool calls as well as the text. A routing call
    returns no text at all now — `tool_choice` is `any`, so the whole answer is the call
    — and a row holding `{"text": ""}` reads in the trace tab as a model that said
    nothing, when what it did was choose.

    """
    await db.execute(
        """INSERT INTO llm_usage (case_id, turn_id, phase, scenario, provider, model,
                                  prompt_tokens, completion_tokens, cached_tokens, total_tokens,
                                  cost_usd, latency_ms, request, response)
           VALUES ($1::bigint,$2::text,$3::text,$4::text,$5::text,$6::text,
                   $7::int,$8::int,$9::int,$10::int,$11::numeric,$12::int,$13::jsonb,$14::jsonb)""",
        [
            turn.case_id, turn.turn_id, phase.value, scenario, completion.provider, completion.model,
            completion.prompt_tokens, completion.completion_tokens, completion.cached_tokens,
            completion.total_tokens, cost(completion), completion.latency_ms,
            {"scenario": scenario, **(completion.request or {})},
            {"text": completion.text[:2000],
             "tool_calls": [call.get("name", "") for call in completion.tool_calls]},
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
    provider: Provider, db: Database, turn: Turn, text: str, past: str, stage: str, fence: str, today: date
) -> tuple[Scenario, dict[str, str]]:
    """
    Ask the model which scenario this turn belongs to, and with what.

    Args:
        provider: The model seam.
        db: The database, for the trace.
        turn: The turn.
        text: What the customer said.
        past: The transcript of the case so far.
        stage: The case's stage, which decides what the router may choose from.
        fence: This turn's tag name, which marks where the customer's own words start
            and stop.
        today: The date the router resolves 「上禮拜」 and 「三個月前」 against when it
            fills a parameter. Without it a model fills the year it was trained in.

    Returns:
        The chosen scenario and the parameters the model filled from the conversation.

    **The model must call a tool.** `tool_choice` is `any`, so it has no way to answer
    from its own words, and every turn resolves to a named scenario. A message the desk
    does not serve lands on `out_of_scope`, whose reply is a template.

    A provider that answers anyway lands there too. The codex path builds calls from free
    text and cannot be constrained, so the fallback is `out_of_scope` rather than the
    model's own words: that reply has no row behind it, which is where 等待期是 30 天 and
    核准完成 were both written.

    The arguments are the point. A scenario declares the parameters it needs, the model
    collects them, and until they were returned from here they were discarded — the
    gather step then ran on hardcoded defaults, so a customer who asked for 壽險 with a
    budget of 20,000 was matched against health products at 30,000.

    """
    offered = {s.name: s for s in reachable(stage)}
    completion = await provider.complete(
        phase=Phase.ROUTE,
        # WRITING belongs here because `out_of_scope` may be entered from this call and
        # its template is the whole reply. No scenario injection shapes this brief.
        # `untrusted` closes the brief and carries the language line inside it, so the
        # fence rule is the last thing the model reads. A guard in the middle is one the
        # message can talk over, because later text wins the slot they both compete for.
        instructions=(
            f"{LOOKUP_SCOPE}{ROUTER_INSTRUCTIONS}\n\n{WRITING}\n\n{anchored(today)}"
            f"\n\n{closing_rules(fence, i18n.hint(turn.locale), sourced=False)}"
        ),
        user_input=f"{past}<{fence}>\n{text}\n</{fence}>",
        tools=[tool_schema(s) for s in offered.values()],
        # `any` means "call one of these, and you pick which". It removes the router's
        # free-text branch, which is where both live fabrications were written: 等待期是
        # 30 天 for a contract whose art.4 says 91 days, and 核准完成 for a case still at
        # review. Neither had a row behind it, because no tool had run.
        #
        # It needs `out_of_scope` on the offer list to be safe. Forced to choose with only
        # lookups available, the model routed 今天天氣如何 to `product_clauses` — measured
        # on the live endpoint.
        tool_choice={"type": "any"},
    )
    await _record(db, turn, Phase.ROUTE, completion, None)

    for call in completion.tool_calls:
        # Resolved against what this stage offered, not the whole catalogue. The two
        # lists differed by one or two scenarios at every stage — verify_identity at
        # all of them — so a name the router was never shown still dispatched. On the
        # Anthropic and OpenAI paths the API constrains the name to a declared tool,
        # but the codex path builds calls from free text, and a guard that holds only
        # for some providers is not a guard.
        if (scenario := offered.get(call.get("name", ""))) is not None:
            try:
                args = json.decode((call.get("arguments") or "{}").encode())
            except DecodeError:
                logger.warning("router_arguments_unreadable", call=str(call)[:200])
                args = {}
            return scenario, {k: str(v) for k, v in args.items() if isinstance(args, dict)}
    logger.info("router_unconstrained", case_id=turn.case_id, provider=completion.provider)
    return OUT_OF_SCOPE, {}


async def _blank() -> str:
    """
    Stand in for a withheld string, so the gather keeps its shape.

    Returns:
        The empty string, which reads downstream as "nothing is known" — the same thing
        an unverified session should see.

    """
    return ""


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

    An absent budget is not an unreadable one. 想買壽險 carries no figure, so the router
    returns an empty string, and warning on it filed a parse failure for every customer
    who had not yet said what they could spend — the operator reading that log sees a
    broken extractor where there was nothing to extract.

    """
    if not (text := str(raw).strip().replace(",", "")):
        return None
    try:
        return int(text)
    except ValueError:
        logger.warning("budget_unreadable", raw=text[:60])
        return None


def _visible_rows(value: Any) -> list[dict[str, Any]]:
    """List every returned clause row, duplicates included, excluding private metadata."""
    found: list[dict[str, Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            if isinstance(item.get("product_id"), str) and isinstance(item.get("clause_id"), str):
                found.append(item)
            for key, child in item.items():
                if not key.startswith("_"):
                    visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return found


def _clause_rows(value: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """Collect returned clause rows by identity, the last occurrence winning."""
    return {(row["product_id"], row["clause_id"]): row for row in _visible_rows(value)}


def _clause_sources(value: Any) -> tuple[tuple[str, str], ...]:
    """Preserve the document identities returned by permitted database tools."""
    return tuple(_clause_rows(value))


def _clause_subject(value: Any) -> dict[str, str]:
    """Keep the exact clause text made visible for this turn's answer."""
    return {f"{product}|{clause}": row["verbatim"]
            for (product, clause), row in _clause_rows(value).items() if isinstance(row.get("verbatim"), str)}


def _retrieval_trail(value: Any) -> list[dict[str, Any]]:
    """
    Take each visible clause's retrieval score out of the material and into the record.

    Args:
        value: The answer context, after the evidence budget. Mutated: `retrieval_score`
            is removed from every clause row it visits.

    Returns:
        One entry per offered clause, key and score. A row that reached the material
        without a hit — a cross-referenced sibling, an ILIKE fallback — has no score, and
        the record says so with None rather than inventing a rank for it. A clause two
        tools both returned is one entry, carrying the first non-null score seen; every
        copy has its score removed, so none reaches the model.

    """
    trail: dict[str, float | None] = {}
    for row in _visible_rows(value):
        key = f"{row['product_id']}|{row['clause_id']}"
        score = row.pop("retrieval_score", None)
        if trail.get(key) is None:
            trail[key] = score
    return [{"key": key, "score": score} for key, score in trail.items()]


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
        facts["_allowed_clauses"] = facts.get("_allowed_clauses", frozenset()) | frozenset(
            clause for _, clause in _clause_sources(facts)
        )
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
        #
        # A scenario that declares no tool reads nothing, so nothing about it is withheld
        # and the flag is false. `out_of_scope` is the one: it answers from a template,
        # and the unconditional flag made an unverified visitor who asked about the
        # weather read 查詢您名下的保單資料前，需要先核對您的身分.
        facts["_identity_required"] = bool(scenario.tools)
        facts["_allowed_clauses"] = frozenset()
        product_ids: list[str] = []
        pending: dict[str, Any] = {}
    elif "list_policies" not in allowed:
        # A confirmed session used to get the book whatever the scenario asked for, so
        # `browse_products` — one tool, the public catalogue — was handed every policy the
        # customer holds, directly beneath an injection telling the model 你還看不到既有
        # 保單. Not a leak: the session is verified and the data is theirs. It is an
        # instruction and a payload contradicting each other, with the model as the only
        # thing deciding which wins, and the failure is a 商品介紹 answer that opens by
        # reciting policy numbers nobody asked about.
        #
        # Every scenario that needs product_ids declares `list_policies` alongside the
        # tool that needs them, so honouring the declaration costs nothing.
        facts["_allowed_clauses"] = frozenset()
        product_ids = []
        pending = {}
    else:
        # list_policies first, because everything else needs its product_ids.
        policies = await tools.list_policies(db, turn.member_id, today=today)
        if any(param.name == "policy" for param in scenario.params):
            scope = tools._select_policies(policies, params.get("policy", ""))
            if scope["status"] in {"ambiguous", "not_found"}:
                return {
                    "_allowed_clauses": frozenset(),
                    "policy_scope": {
                        "status": scope["status"],
                        "reference": params.get("policy", ""),
                        "candidates": [{key: row[key] for key in ("policy_number", "product_name")}
                                       for row in scope["policies"]],
                    },
                }
            policies = scope["policies"]
        product_ids = [p["product_id"] for p in policies]
        facts["list_policies"] = policies
        # The rest depend on product_ids and member_id, not on each other, so they go out
        # together rather than one round trip at a time. Two or three queries per turn on
        # a local database is a few milliseconds; against a networked one it is a full
        # round trip each, on the path a customer is waiting on.
        pending = {"_allowed_clauses": tools.clause_ids_for(db, product_ids)}
    if scenario.coverage_verdict and product_ids:
        # Whatever tools this scenario declares. The gap that named the flag was a
        # scenario whose every tool was about documents, answering a question about
        # cover, with no clause in the material that decides it.
        pending["coverage_clauses"] = tools.coverage_clauses(db, product_ids)
    if "find_clause" in allowed:
        pending["find_clause"] = tools.find_clause(db, product_ids, params.get("topic", ""), index=index)
    if "find_multiplier" in allowed:
        pending["find_multiplier"] = tools.find_multiplier(db, product_ids, params.get("event", turn.procedure_hint))
    if "catalogue_sample" in allowed:
        # `""`, not `"health"`. The same fallback was removed from `quote.gather` and
        # this copy survived: `_route` sets `args = {}` on a DecodeError and on a call
        # carrying no arguments, so the default is reachable, and `health` answers about
        # medical cover to a customer who named no line. `catalogue_sample` reads an
        # empty line as no sample, which is the state `browse_products`' injection now
        # distinguishes from a line that genuinely has nothing on sale.
        pending["catalogue_sample"] = tools.catalogue_sample(db, params.get("line", ""))
    if "benefit_headings" in allowed:
        pending["benefit_headings"] = tools.benefit_headings(db, product_ids)
    if "required_documents" in allowed:
        pending["required_documents"] = tools.required_documents(
            db, product_ids, index=index, topic=params.get("event", ""),
        )
    if "billing_summary" in allowed:
        pending["billing_summary"] = tools.billing_summary(db, turn.member_id, today=today)
    if "coverage_summary" in allowed:
        pending["coverage_summary"] = tools.coverage_summary(db, turn.member_id, today=today)
    if "pending_signatures" in allowed:
        pending["pending_signatures"] = tools.pending_signatures(db, turn.case_id)

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
        # What the desk has not been told, named. Without this the filter simply does not
        # run, no key is set, and the model reads the absence as the catalogue being
        # unavailable: 目前沒有可供核對的商品目錄資料，因此我不能自行替您推薦商品 reached
        # two customers in a replay of the real transcript, in front of a catalogue
        # holding 660 products. An unasked question and a missing corpus look identical
        # from inside the reply, so the difference has to be stated.
        if missing := [
            what
            for what, given in (("商品線", bool(params.get("line"))), ("年繳預算", budget is not None))
            if not given
        ]:
            facts["_still_needed"] = missing
        # `elif`, not a second `if`. With a budget and no line, both branches used to
        # run: `_still_needed` said the filter had not been asked for, while
        # `suitable_products` searched `line=""`, found nothing, and `alternatives`
        # came back offering 改看 life、改看 health and three more — five "relaxations"
        # of a condition nobody set, with `binding` empty because no condition was
        # binding. Two stories about the same turn, and six probe queries to build the
        # one the injection then forbids the model to tell.
        elif member and budget is not None:
            age = member["insurance_age"]
            facts["suitable_products"] = await tools.suitable_products(
                db,
                insurance_age=age,
                occupation_class=member["occupation_class"],
                budget=budget,
                line=params.get("line", ""),
                need=params.get("need", ""),
                index=index,
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

    facts["_allowed_clauses"] = facts.get("_allowed_clauses", frozenset()) | frozenset(
        clause for _, clause in _clause_sources(facts)
    )
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
    template = scenario.template
    match scenario.name:
        case "billing":
            summary = facts.get("billing_summary") or {}
            # A total mixing instalment rows with rate-card estimates says so. Without
            # this the estimate disappears into a figure the customer reads as their bill.
            unscheduled = int(summary.get("no_schedule") or 0)
            uncosted = int(summary.get("uncosted") or 0)
            caveats = [
                f"其中 {unscheduled} 張查無繳費紀錄，以商品費率估算" if unscheduled else "",
                f"另有 {uncosted} 張查不到費率，未計入這個金額" if uncosted else "",
            ]
            said = "；".join(c for c in caveats if c)
            return template.format(
                active=summary.get("active", 0),
                premium=f"{float(summary.get('premium') or 0):,.0f}",
                caveat=f"（{said}）" if said else "",
            )
        case "coverage":
            rows = facts.get("coverage_summary") or []
            lines = "\n".join(f"　{r['product_name']}（{r['policy_number']}）：{r['insured']}" for r in rows)
            return template.format(lines=lines or "　（查無有效保單）")
        case _:
            # A template reaching here with a placeholder in it renders the braces to the
            # customer, which is how 「共 {count} 份」 was read by one. Every template that
            # needs a value has a case above; this branch is for the ones that need none,
            # and it says so rather than assuming it.
            if "{" in template:
                logger.warning("template_unfilled", scenario=scenario.name)
                return "本次查詢未能組出完整回覆，已保留紀錄並轉由專人與您聯繫。"
            return template


async def run_turn(
    provider: Provider,
    db: Database,
    *,
    case_id: int,
    member_id: int,
    text: str,
    confirmed: bool = False,
    identity_locked: bool = False,
    index: Retriever | None = None,
    today: date | None = None,
    since: int = 0,
    locale: str | None = None,
    document_event: bool = False,
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
        identity_locked: Whether this connection has exhausted its verification attempts.
            This changes the next-step guidance, not the tool authorization decision.
        today: The date to judge currency against.
        since: The message this connection started above.
        locale: The language to reply in, when the caller already resolved it. None
            reads it off the message and the conversation.
        document_event: Internal socket event, not customer-selected routing. Runs the
            read-only document scenario through the same identity and answer checks.

    Returns:
        The turn, carrying the reply and anything that failed a check.

    A model that cannot be reached produces a turn that says so. It never produces a
    turn that answers anyway.

    """
    today = today or dates.today()
    turn = Turn(case_id, member_id)
    turn.locale = locale or (await lang.resolve(db, case_id, text))[1]
    # Two layers, gathered together. The transcript is the current session, cut at an
    # hour's silence; the card is what outlived that window. A customer who asks about a
    # claim, then a premium, then comes back to the application is coherent only because
    # the second layer exists — the budget they stated six turns ago is on the card
    # whether or not it is still in the transcript.
    #
    # The customer's own message is already written, so the window drops its last row
    # and never repeats what the router is reading as this turn.
    stage = await db.fetch_val('SELECT stage FROM "case" WHERE case_id = $1::bigint', [case_id]) or "inquiry"
    # The card is gated for the same reason `standing_brief` is, and it was not. A visitor
    # types a display name; a name matching an existing member binds the session to that
    # member and reopens their live case, with `confirmed` false and the id masked. The
    # card then read `member_fact` — which is scoped to the member and bounded by nothing
    # — and handed a stranger whatever the sweep had extracted about them: budget, health
    # history, what they hold. `recent` is left alone: it cuts on SESSION_GAP_S, so it
    # shows only a conversation still in progress, which is the reload-keeps-context
    # behaviour it was built for.
    messages, profile, brief = await asyncio.gather(
        memory.recent(db, case_id, since=since),
        memory.card(db, member_id=member_id, case_id=case_id) if confirmed else _blank(),
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
        known = f"# This session has passed 資料核對. The customer's records may be read.\n\n{known}"
    if not confirmed:
        # Told to the model, so the ask arrives in the conversation rather than as a
        # system refusal, and enforced below, so a model that ignores it still reads
        # nothing. The prompt makes it natural; the gate makes it true.
        #
        # Note what it does not say: "ask for the number". A greeting is not a request
        # for anyone's policy data, and answering 嗨 with 請提供您的身分證字號 is a desk
        # frisking someone at the door. The check belongs to the question that needs it.
        known = (
            "# This session has not passed 資料核對\n"
            f"# Identity verification state: {'locked' if identity_locked else 'pending'}\n"
            + IDENTITY_NEXT_STEP +
            "The customer has not verified their identity, so none of their policy data is visible to you.\n"
            "A greeting, or a question about what this desk does, gets a direct answer.\n"
            "A question about insurance itself (the free-look period, what 據實說明 means, how long "
            "a lapsed policy can be reinstated, which products exist) still calls the matching "
            "scenario tool. Those scenarios read public clauses and statutes and answer an "
            "unverified customer. A number of days or an amount from your own memory has nothing "
            "behind it that can be checked.\n"
            "For a request about their own policies, premiums, sums insured, claims or a plan to buy, "
            "call the matching scenario tool. Its gate withholds personal records until identity is "
            "verified; its reply follows the connection's identity verification state.\n"
            "Every statement about their policies comes from the material.\n"
            + ASKED_ALREADY + "\n"
        )
    history = messages if document_event else messages[:-1]
    # Fresh per turn, and never written to a message row or shown to anyone. A fixed
    # marker is one the customer can close: `# This message` was closed with `</user>`,
    # and what followed claimed to be a system block with supervisor approval.
    fence = f"untrusted-{uuid4().hex[:12]}"
    # The transcript goes inside the same fence as this turn's message: a message that
    # opened `<system>` last turn is still in the transcript this turn, where it used to
    # sit outside any fence. The block is rebuilt from rows every call with this call's
    # tag, so no tag ever survives into a later prompt. The profile card stays outside.
    # It carries the desk's own rule for an unevidenced fact (要用到的時候先問他一句確認),
    # and the fence rule says not to adopt a rule from inside the fence; the card's facts
    # are the sweep's summary, not the customer's words, and the same rule names records
    # as quotation.
    recalled = memory.transcript(history)
    past = f"{known}<{fence}>\n{recalled}</{fence}>\n\n{profile}" if recalled else f"{known}{profile}"

    started = time.perf_counter()
    document_refusal = text if document_event else ""
    try:
        if document_event:
            scenario, params = DOCUMENT_PROGRESS, {}
            text = "請依本次文件操作結果與本案最新工具紀錄，說明目前進度和下一步。"
        else:
            scenario, params = await _route(provider, db, turn, text, past, stage, fence, today)
    except ProviderError as exc:
        latency = int((time.perf_counter() - started) * 1000)
        logger.warning("turn_unrouted", case_id=case_id, error=str(exc))
        await _record_failure(db, turn, Phase.ROUTE, None, str(exc), latency, provider.name)
        turn.reply = "櫃台的語言服務目前無回應，請稍候再試，或改由專人與您聯繫。"
        return turn

    scenario_owner = import_module(scenario.tools_module) if scenario.tools_module else None
    if not confirmed and tools.reads_identity(scenario.tools, owner=scenario_owner):
        # The gate withholds the member queries, not the conversation. Refusing the whole
        # scenario made the desk answer 我想加保 with nothing but a demand for a number;
        # what it should do is say what exists and ask for the number to judge the fit.
        # `_gather` skips every tool marked `requires_identity`, so no member row is read
        # — the withholding happens before the query, not after it.
        logger.info("scenario_gated", case_id=case_id, scenario=scenario.name)
        turn.awaiting_identity = True

    turn.scenario = scenario.name
    # The same rule the free-answer path already had, and this path is where it was
    # missing. A customer who asked 我的保單保額是多少 without an id was refused and then
    # offered 我想了解這些保額夠不夠, 已經理賠過的會扣掉嗎, 想確認有沒有重複投保 — three
    # more questions the same gate would refuse. Measured on a live turn.
    chips = PUBLIC_OPENERS if turn.awaiting_identity else (scenario.quick_replies or OPENERS)
    turn.quick_replies = _fresh(chips, _asked(messages))
    turn.procedure_hint = text
    turn.params = params
    facts = await _gather(db, scenario, turn, today=today, params=params, confirmed=confirmed, index=index)
    allowed: frozenset[str] = facts.pop("_allowed_clauses")
    if document_refusal and confirmed:
        facts["document_action"] = {"refusal": document_refusal}

    if (scope := facts.get("policy_scope")) and scope["status"] == "ambiguous":
        # The choice must identify each contract, even if the model omits or merges candidates.
        (question,) = await i18n.translate(db, turn.locale, (
            "有多張保單符合您指定的名稱，請提供要查詢的保單號碼：",
        ))
        candidates = scope["candidates"]
        choices = "\n".join(f"- {row['policy_number']}｜{row['product_name']}" for row in candidates)
        turn.reply = f"{question}\n\n{choices}"
        turn.quick_replies = tuple(row["policy_number"] for row in candidates)
        return turn

    if facts.get("_identity_required") and scenario.emit is Emit.TEMPLATE:
        # A template fills from rows, and the withheld query has none — so 您名下有效保單
        # 共 0 張 is what it renders. That is a false statement about the customer rather
        # than a withheld one, and it is the exact failure the review scenario's own
        # docstring names. The model path says the same thing correctly, because it reads
        # `_identity_required`; this path has no model to read it.
        turn.awaiting_identity = True
        turn.reply = IDENTITY_LOCKED_REPLY if identity_locked else (
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
    visible_facts = _answer_context(facts)
    coverage = visible_facts.get("evidence_coverage", {})
    if coverage.get("context_omitted"):
        turn.reply = EVIDENCE_LIMITED
        return turn
    turn.clause_sources = _clause_sources(visible_facts)
    turn.clause_texts = _clause_subject(visible_facts)
    # Popped before the material is serialised: the model reads the clause, never the
    # score. A score in the prompt is a number the model can quote, and it says nothing
    # about the customer's contract.
    turn.evidence = {"offered": _retrieval_trail(visible_facts), "coverage": coverage}
    material = etoon.dumps(visible_facts)
    clarifying_policy = "policy_scope" in facts
    instructions = POLICY_CLARIFICATION if clarifying_policy else (
        f"{ROUTER_INSTRUCTIONS}\n\n{WRITING}\n\n{scenario.injection}"
    )
    answering = time.perf_counter()
    try:
        # The calculator is offered only where the scenario asks for it. Offered
        # everywhere, it made the desk a calculator for a customer who had wandered off
        # topic; a scenario that needs a figure the rows do not carry sets `calculator`.
        completion = await provider.complete(
            phase=Phase.ANSWER,
            instructions=f"{instructions}\n\n{anchored(today)}\n\n{closing_rules(fence, i18n.hint(turn.locale), sourced=True)}",
            user_input=f"{past}<{fence}>\n{text}\n</{fence}>\n\n# Tool results\n{material}",
            schema=_answer_schema(turn.clause_sources, calculator=scenario.calculator, dates=scenario.dates),
        )
    except ProviderError as exc:
        latency = int((time.perf_counter() - answering) * 1000)
        logger.warning("turn_unanswered", case_id=case_id, scenario=scenario.name, error=str(exc))
        await _record_failure(db, turn, Phase.ANSWER, scenario.name, str(exc), latency, provider.name)
        turn.reply = "櫃台的語言服務目前無回應，本次查詢已記錄，請稍候再試。"
        return turn

    await _record(db, turn, Phase.ANSWER, completion, scenario.name)
    try:
        answer = json.decode(completion.text, type=_Answer)
    except DecodeError:
        turn.reply = WITHHELD
        turn.faults = ("answer_format",)
        return turn
    if _LEAK.search(answer.reply):
        turn.reply = WITHHELD
        turn.faults = ("answer_leak",)
        return turn
    if answer.calculations and not scenario.calculator:
        turn.reply = WITHHELD
        turn.faults = ("unoffered_calculator",)
        return turn
    if answer.date_calculations and not scenario.dates:
        turn.reply = WITHHELD
        turn.faults = ("unoffered_dates",)
        return turn
    computed = _run_dates(answer.date_calculations, today=today)
    turn.dates = tuple((expression, dated.text) for expression, dated in computed)
    completion = structs.replace(
        completion, text=answer.reply,
        tool_calls=tuple({"name": "calculate", "arguments": json.encode({"expression": expression}).decode()}
                         for expression in answer.calculations),
    )
    turn.computations = _run_calculations(completion)
    # Every date the reply states must be one the customer or the material gave, today,
    # or a result the date tool produced this turn. The expression list alone constrains
    # nothing — a model can write a correct expression and a different date beside it —
    # so the reply is read back, the way its clause ids are.
    backed = {dated.value for _, dated in computed if isinstance(dated.value, date)}
    if _withhold_undated(turn, answer.reply, sources=f"{past}{text}{material}", backed=backed, today=today):
        return turn

    if await _unverifiable(
        db, turn, completion.text, allowed, sources=tuple(answer.citations), quoted_fields=tuple(answer.quoted_fields),
    ):
        return turn
    turn.reply = completion.text
    if _withhold_promise(turn, case_id, scenario.name):
        return turn
    if not completion.text.strip():
        # A model that answered with tool calls and no prose leaves the customer
        # looking at an empty bubble. There is no second round here to fill it in.
        logger.warning("answer_empty", case_id=case_id, scenario=scenario.name)
        turn.reply = "本次查詢未能組出完整回覆，已保留紀錄並轉由專人與您聯繫。"
        return turn
    if coverage.get("complete") is False:
        turn.reply = f"{EVIDENCE_LIMITED}\n\n{turn.reply}"
    return turn


def _asked(messages: list[dict[str, Any]]) -> list[str]:
    """
    List what the customer has said this session.

    Args:
        messages: The window `memory.recent` returned, newest last.

    Returns:
        Their own lines, including the one being answered now.

    A chip is stale against the whole conversation, not against the last sentence. The
    customer who asks 你們有什麼壽險可以保 and then twice 那我適合哪一張 was offered
    你們有哪些商品？ under the third reply — the first question, answered two turns
    earlier, handed back as a suggestion.

    """
    return [m["text"] for m in messages if m["speaker"] == "customer"]


def _fresh(chips: tuple[str, ...], said: list[str]) -> tuple[str, ...]:
    """
    Drop the chips that repeat a question already asked.

    Args:
        chips: The offered questions.
        said: Everything the customer has said.

    Returns:
        The chips that are still new, or all of them when none is — an empty chip row
        is worse than a stale one, because a customer who does not know what to ask
        then has nowhere to start.

    """
    return tuple(c for c in chips if not any(_echoes(c, s) for s in said)) or chips


def _echoes(chip: str, text: str) -> bool:
    """
    Say whether a quick reply repeats what the customer just typed.

    Args:
        chip: The offered question.
        text: What they said.

    Returns:
        True when the two are the same question in different words.

    Compared on the characters that carry meaning — the punctuation and the polite
    scaffolding differ between 理賠要準備哪些文件？ and 住院四天要準備什麼理賠文件？, and
    everything else in them is the same.

    """
    strip = str.maketrans("", "", "？?。，、 ")
    a, b = chip.translate(strip), text.translate(strip)
    if not a or not b:
        return False
    shared = sum(1 for ch in set(a) if ch in b)
    return shared / len(set(a)) >= 0.6


PROMISED = (
    "本次回覆包含理賠或核保結果的判斷，那是核保理賠人員的權責，"
    "為避免給您錯誤的期待，已保留該回覆並轉由專人與您說明。"
)
"""What the customer reads instead of a reply that promised an outcome.

Withheld rather than annotated, for the reason `WITHHELD` is: a caveat under a promise
still leaves the promise on the screen, and the promise is the part a customer acts on.
"""

_PROMISE = re.compile(
    # 一定會賠, 保證理賠, 絕對可以復效 — the outcome asserted outright.
    r"(一定|必定|絕對|肯定)[^。；\n]{0,6}(會|可以|能|能夠)[^。；\n]{0,10}(賠|給付|核准|通過|理賠|復效|受理)"
    # 保證給付 — but not 可保證明, which is a document a customer is asked to provide.
    r"|(?<!可)保證[^。；\n]{0,8}(賠|給付|核准|通過|理賠|受理|沒問題)"
    # 應該會過, 看起來沒問題, 通常都會賠 — the hedge that a customer reads as a yes.
    r"|(應該|多半|通常|大概)[^。；\n]{0,4}(會|可以|都會|沒問題)[^。；\n]{0,8}(賠|給付|核准|通過|理賠|受理)"
    r"|看起來沒(什麼)?問題|不用擔心[^。；\n]{0,10}(賠|給付|核准|通過)"
    # 我們會核准 — the desk deciding on the insurer's behalf.
    r"|我(們)?(會|可以|能)[^。；\n]{0,6}(核准|核賠|給付|通融|放寬|加速)"
)
"""Ways a reply says an outcome the desk does not decide.

The desk may report what an assessor recorded — 審核中, 待補件, 已核付 with the figure
from the row — and may never say what an assessor will decide. Three shapes reach a
customer as a yes: the outright claim, the hedge that reads as one (應該會過), and the
desk speaking for the insurer (我們會核准).

The lookbehind on 保證 is not decoration. 可保證明 is the certificate of insurability a
customer is asked to produce for a reinstatement past six months, and it appeared in a
live reply — a screen that fired on it would withhold a correct answer about a document
the customer needs to go and get.
"""


_DENIAL = re.compile(r"不能|不會|不可|不得|無法|並非|不代表|不保證|不是說")
"""Words that turn the phrase after them into a denial.

Read from the clause the match sits in, so 不能據此判定…一定會加費 passes and
一定會加費 alone does not. Measured on the twenty-two replies this desk has written: one
of them denies a promise using the promise's own words, and a screen without this would
have withheld the most careful sentence in the set.
"""


def _promises(text: str) -> str:
    """
    Find where a reply promised an outcome it may not promise.

    Args:
        text: What the model wrote.

    Returns:
        The offending phrase, or the empty string when there is none.

    Mechanical, like the citation check beside it, and for the same reason: a promise
    about a claim is a red line rather than a judgement call, and the check that guards a
    red line does not itself run on a model. Every other check in this desk is
    prompt-based; these two are the exceptions, and both withhold rather than annotate.

    """
    # Every match, not the first. A denial of a promise is not a promise, but a denial
    # followed by a promise is still a promise: 我不保證會核准。不過這件一定會賠。 was
    # reaching the customer, because the first match was negated and the scan stopped
    # there. Each match is judged on the clause before it, and the first one that
    # survives is the offence.
    for found in _PROMISE.finditer(text):
        # A denial of a promise is not a promise. 不能據此判定您的外送工作一定會加費、退費或
        # 影響理賠 is a live reply saying the desk cannot decide, and the pattern reads its
        # 一定會…理賠 the same way it reads a claim. The clause before the match decides.
        before = text[:found.start()]
        # The negator can sit hard against the match — 我不保證會核准 puts 不 one character
        # before 保證, which no clause-level scan sees because the clause is then just 我不.
        if before[-1:] in {"不", "沒", "未", "毋"}:
            continue
        clause = before.rsplit("。", 1)[-1].rsplit("\n", 1)[-1]
        if not _DENIAL.search(clause):
            return found.group(0)
    return ""


def _withhold_promise(turn: Turn, case_id: int, scenario: str | None) -> bool:
    """
    Replace a reply that promised an outcome the desk does not decide.

    Args:
        turn: The turn, whose `reply` and `faults` this may set.
        case_id: For the log line.
        scenario: Which scenario wrote it, or None for the router's free answer.

    Returns:
        True when the reply was withheld, so the caller stops.

    Runs on both paths. The scenario injections forbid this in prose, and prose is what
    the model may quietly stop following — 理賠是人工審查, and a desk that says 應該會過
    has decided something no one at this counter is allowed to decide. The customer acts
    on the promise, not on the caveat under it, so the reply goes rather than gains a
    footnote.

    """
    if not (phrase := _promises(turn.reply)):
        return False
    logger.warning("promise_withheld", case_id=case_id, scenario=scenario, phrase=phrase)
    turn.faults = (*turn.faults, f"promise:{phrase}")
    turn.reply = PROMISED
    return True


async def _unverifiable(
    db: Database, turn: Turn, text: str, allowed: frozenset[str], *, sources: tuple[str, ...] | None = None,
    quoted_fields: tuple[_ProvisionQuote, ...] = (),
) -> bool:
    """
    Withhold a reply whose citations do not resolve.

    Args:
        db: The database, for the statute corpus.
        turn: The turn, whose `citations`, `faults` and `reply` this sets.
        text: What the model wrote.
        allowed: The clause ids the tools actually returned for this member.
        sources: Explicit product-and-clause keys from the answer. None is the router's
            free-text path, which has no contract evidence and grants no document access.
        quoted_fields: Exact supporting text for the four provision kinds in the answer schema.

    Returns:
        True when the reply was withheld, so the caller stops.

    Contract access follows explicit keys selected from this turn's returned evidence.
    A bare article number in prose cannot select every product sharing that number.
    Prose tokens are checked additionally, so an undeclared article cannot slip through
    beside an otherwise valid source list. Statutes retain their existing lookup.

    The unverifiable text is withheld, not annotated. Appending a caveat still puts the
    invented number in front of the customer, which is the opposite of the point.

    """
    source_faults: tuple[str, ...] = ()
    selected: tuple[tuple[str, str], ...] = ()
    if sources is not None:
        available = {f"{product}|{clause}": (product, clause) for product, clause in turn.clause_sources}
        source_faults = tuple(f"source:{key}" for key in dict.fromkeys(sources) if key not in available)
        selected = tuple(available[key] for key in dict.fromkeys(sources) if key in available)
        allowed = frozenset(clause for _, clause in selected)
    # Prose is scanned as a second line rather than the only one: a model that writes
    # art.5 into a sentence without listing it in `citations` is caught here, and the enum
    # alone does not stop that.
    #
    # This ran on the router's own words too, and read a refusal reading 「Cites a contract
    # article (art.99) without retrieving it」 as a citation of art.99 — withholding a
    # correct refusal and telling the customer the desk had cited an unverifiable clause.
    # It had not. That path is gone: the router calls a tool or lands on `out_of_scope`,
    # and neither writes prose here.
    found = _CITATION.findall(text)
    cited = tuple(dict.fromkeys([*found, *(clause for _, clause in selected)]))
    subject = {key: value for key, value in turn.clause_texts.items() if key in (sources or ())}
    checked = recheck(
        Verdict(passed=True, reason="", cited_clauses=cited,
                quoted_fields=tuple(QuotedField(field=quote.field, text=quote.text) for quote in quoted_fields)),
        subject=subject, allowed_clauses=allowed,
    )
    fabricated = await statute.unresolved(db, text)
    turn.citations = cited
    turn.faults = source_faults + checked.faults + tuple(f"{name}{doc_id}" for name, doc_id in fabricated)
    if not turn.faults:
        turn.cited_sources = selected
        return False
    logger.warning(
        "citation_unresolved", case_id=turn.case_id, faults=list(checked.faults), statute=list(fabricated)
    )
    turn.reply = WITHHELD
    return True


def _run_dates(expressions: list[str], *, today: date) -> tuple[tuple[str, dates.Dated], ...]:
    """
    Evaluate every date expression the model wrote.

    Args:
        expressions: What the model put in `date_calculations`.
        today: The date this turn is answered on.

    Returns:
        Each expression paired with its result. An expression the date tool cannot read
        is dropped and logged rather than guessed at, the same rule the calculator
        applies: a date either came from the tool or does not exist — and a reply that
        states the date anyway is caught by `_unsourced_dates`, since nothing backs it.

    """
    results: list[tuple[str, dates.Dated]] = []
    for expression in expressions:
        try:
            results.append((expression, dates.compute_date(expression, today=today)))
        except dates.DateError as exc:
            logger.warning("date_rejected", expression=expression[:200], error=str(exc))
    return tuple(results)


def _source_dates(text: str) -> tuple[set[date], set[tuple[int, int]]]:
    """
    Read the calendar dates a source text names, in every form a source writes them.

    Args:
        text: The brief, the transcript, the message and the material, as one string.

    Returns:
        The full dates found, and the month-day pairs written without a year. A day that
        does not exist (2月30日) is skipped rather than read as a date.

    """
    text = text.translate(_ASCII)
    full: set[date] = set()
    for roc, roc_year, year, short_year, month, day in _DATE_IN_SOURCE.findall(text):
        resolved = int(roc_year) + 1911 if roc else int(year) if year else int(short_year) + 1911
        if resolved in _YEARS and (found := _in_year(resolved, int(month), int(day))) is not None:
            full.add(found)
    dated_out = _DATE_IN_SOURCE.sub(" ", text)
    partial = {(int(month), int(day)) for month, day in _MONTH_DAY.findall(dated_out)}
    return full, partial


def _unsourced_dates(reply: str, *, sources: str, backed: set[date], today: date) -> list[str]:
    """
    Name every ISO date the reply states that nothing given this turn supports.

    Args:
        reply: What the model wrote.
        sources: Everything the model was shown — the brief, the transcript, the message,
            the material — as one string.
        backed: Dates the date tool produced this turn.
        today: The date the turn is answered on, which the reply may always state.

    Returns:
        The unsupported dates, empty when every date checks out. A source's month and
        day without a year supports the same month and day in this year, which is how a
        customer's 3月1日 and the reply's 2026-03-01 are the same date. A date-shaped
        string naming a day that does not exist (2099-02-30) is unsupported by
        definition and is returned as written.

    What this proves is that the date came from something the model was shown or from
    the date tool, the way the citation check proves a clause id exists in the material.
    It does not prove the date is the right one for the sentence it sits in: a policy's
    effective date is in the material, and a reply that calls it the rescission deadline
    passes here. That is the reviewer's question, and the record beside the reply
    (`turn.dates`, `turn.evidence`) is what they read to answer it.

    """
    stated: set[date] = set()
    impossible: list[str] = []
    for year, month, day in _ISO_IN_REPLY.findall(reply.translate(_ASCII)):
        if int(year) not in _YEARS:
            continue
        if (found := _in_year(int(year), int(month), int(day))) is None:
            impossible.append(f"{year}-{month}-{day}")
        else:
            stated.add(found)
    allowed, allowed_partial = _source_dates(sources)
    allowed |= backed | {today}
    allowed |= {d for month, day in allowed_partial if (d := _in_year(today.year, month, day)) is not None}
    return [d.isoformat() for d in sorted(stated - allowed)] + sorted(set(impossible))


def _in_year(year: int, month: int, day: int) -> date | None:
    """Return the date, or None when that month and day do not exist in that year."""
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _withhold_undated(turn: Turn, reply: str, *, sources: str, backed: set[date], today: date) -> bool:
    """
    Replace a reply that states a date nothing given this turn supports.

    Args:
        turn: The turn, whose `reply` and `faults` this may set.
        reply: What the model wrote.
        sources: Everything the model was shown, as one string.
        backed: Dates the date tool produced this turn.
        today: The date the turn is answered on.

    Returns:
        True when the reply was withheld, so the caller stops.

    """
    if not (unsourced := _unsourced_dates(reply, sources=sources, backed=backed, today=today)):
        return False
    logger.warning("date_unsourced", case_id=turn.case_id, scenario=turn.scenario, dates=unsourced)
    turn.faults = (*turn.faults, *(f"date:{stated}" for stated in unsourced))
    turn.reply = UNDATED
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


CHARS = 400
"""How much of a string reaches the model. A clause runs to 442,649 characters, so the
whole tool result is trimmed rather than trusted."""

MAX_EVIDENCE_ROWS = 40
MAX_EVIDENCE_CHARS = 128_000
"""Per-answer limits, independent of per-product retrieval depth.

The character ceiling includes the entire serialized tool context, not just clause
bodies. It reserves room for instructions, conversation and output; it is not a
token count or a bound on those other prompt sections.
"""

EVIDENCE_LIMITED = (
    "本次資料量超出單次查詢範圍，尚未完成全部條款核對。"
    "以下資料不足以確認所有保單的完整適用情形；請縮小保障主題或分批查詢後再確認。"
)

LONGER: dict[str, int] = {"verbatim": tools.DOCUMENT_CHARS}
"""Keys whose text the reply reads out rather than reads around, with the room they need.

`verbatim` carries conditions and exceptions that can fall outside a narrow match.
Use the same budget as retrieval so an article kept whole there is not clipped here.
"""


def _answer_context(facts: dict[str, Any]) -> dict[str, Any]:
    """
    Bound all tool evidence together, sharing capacity across products.

    Preserve rank within each product. Remove the lowest-priority retained row until
    the actual serialized context fits, including nested structure and coverage metadata.
    If non-evidence material alone exceeds the limit, withhold the context entirely.
    Coverage describes returned rows, not recall against every clause in the PDFs.
    """
    def collect(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            own = [value] if isinstance(value.get("product_id"), str) and isinstance(value.get("clause_id"), str) else []
            return own + [row for child in value.values() for row in collect(child)]
        if isinstance(value, (list, tuple)):
            return [row for child in value for row in collect(child)]
        return []

    total = len(collect(facts))
    shortened = _short(facts)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows = collect(shortened)
    for row in rows:
        groups[row["product_id"]].append(row)
    ranked = [row for batch in zip_longest(*groups.values()) for row in batch if row is not None]
    selected = [id(row) for row in ranked[:MAX_EVIDENCE_ROWS]]
    evidence_ids = {id(row) for row in rows}
    omitted = object()

    def retain(value: Any, allowed: set[int]) -> Any:
        if id(value) in evidence_ids and id(value) not in allowed:
            return omitted
        if isinstance(value, dict):
            return {key: kept for key, child in value.items() if (kept := retain(child, allowed)) is not omitted}
        if isinstance(value, (list, tuple)):
            return [kept for child in value if (kept := retain(child, allowed)) is not omitted]
        return value

    while True:
        context = retain(shortened, set(selected))
        shown = len(collect(context))
        if total:
            context["evidence_coverage"] = {"complete": shown == total, "omitted_rows": total - shown}
        if len(etoon.dumps(context)) <= MAX_EVIDENCE_CHARS:
            return context
        if not selected:
            return {"evidence_coverage": {"complete": False, "omitted_rows": total, "context_omitted": True}}
        selected.pop()


def _short(value: Any, limit: int = 40, chars: int = CHARS) -> Any:
    """
    Trim a tool result to what fits in a prompt, in types the encoder accepts.

    Args:
        value: Rows or a scalar.
        limit: Most rows to keep.
        chars: How much of a string to keep.

    Returns:
        The value, with long lists truncated, long text clipped, and dates and Decimals
        rendered as primitives.

    The type conversion is here rather than at the call site because this is the one
    function that already walks the whole structure. etoon serialises through stdlib
    json, which raises on a `date` — and a tool result carrying a policy's effective
    date is the common case, not the edge one.

    The width is per key rather than global. Raising it for everything is not available:
    the corpus holds a clause of 442,649 characters, and one row of it would be the whole
    prompt.

    """
    match value:
        case list() | tuple():
            # Apply the shared evidence budget in _answer_context, after gathering
            # all products and tools. This pass only clips individual fields.
            evidence = all(isinstance(row, dict) and "product_id" in row and "clause_id" in row for row in value)
            rows = value if evidence else value[:limit]
            return [_short(v, limit, chars) for v in rows]
        case dict():
            shortened = {k: _short(v, limit, LONGER.get(k, chars)) for k, v in value.items()}
            clipped = [k for k, v in value.items() if isinstance(v, str) and len(v) > LONGER.get(k, chars)]
            if clipped:
                shortened["truncated_fields"] = clipped
                if "verbatim" in clipped:
                    # The retrieval path marks its own slices with an ellipsis, so a
                    # clause cut again here carries the same mark rather than a second
                    # vocabulary. A boolean beside the text said the same thing to
                    # nobody: no prompt read it, and the model saw a quote that ended
                    # mid-sentence with nothing to say it had been cut. The mark
                    # replaces the last character rather than following it, because the
                    # limit is what the model's context can hold, not a target to pass.
                    shortened["verbatim"] = f"{shortened['verbatim'][:-1]}…"
            return shortened
        case str():
            return value[:chars]
        case datetime() | date():
            return value.isoformat()
        case _ if hasattr(value, "as_tuple"):  # Decimal
            return float(value)
        case _:
            return value
