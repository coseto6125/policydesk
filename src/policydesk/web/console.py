"""
The back office's seven tabs, as one read-only API.

The desk pane used to render one case, pushed over a socket by the module that writes
it. Everything a caseworker needs beyond that case has no writer to hang off: the other
conversations on the deployment, one customer's whole record, and the model trace behind
every reply are all questions asked of Postgres on demand. They live here rather than in
`core.commands`, which writes, or in the socket handler, which mirrors one case.

Nothing in this module writes, and that is the seam. A console that could move a case
would be a second writer, and the stage rules would then have two implementations.

Four of the seven tabs answer for one customer and take `?member=`. Three do not: the
LLM trace, the per-scenario token spend and the dashboard read `llm_usage`, whose rows
are the operator's own bill and latency budget. Scoping them to a member would answer
"what did this one customer cost" and hide the question actually being asked, which is
what the deployment spends. They sit behind the same desk token as everything else here,
because the trace carries the prompts, and the prompts carry the customer.

**Say plainly what that token is worth: nothing, against anyone who can load the page.**
`GET /` substitutes `DESK_TOKEN` into `index.html` for every visitor, because the demo
puts both panes on one page on purpose — the point is watching the desk work beside the
chat. So the token stops a scan of the port and stops nothing else, and a comment claiming
otherwise is worse than no comment, because the next reader stops checking.

What would make it a boundary is a second route the customer pane never reaches, setting
an HttpOnly cookie the customer pane never receives. That is a different product from the
one asked for, so it is a decision rather than a fix.
"""

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from msgspec import json
from sanic import Blueprint, Request, response

from policydesk.bootloader import logger
from policydesk.synthetic.person import insurance_age
from policydesk.web.params import int_arg

if TYPE_CHECKING:
    from policydesk.core.db import Database

console = Blueprint("console", url_prefix="/api/console")

# The turn as the executor runs it. `response` is not an `llm_usage` phase — it is where
# a turn has got to once no further call is coming — so the pipeline diagram carries it
# as the terminal node and the phase columns never mention it.
PIPELINE: tuple[str, ...] = ("route", "scenario_tools", "answer", "validate", "repair", "response")

LIVE_IDLE_S = 60
"""How long after its last call a turn stops counting as in flight. A turn is only
visible through the rows it has already written, so "still running" cannot be read off
the table — it is inferred from the gap, and a turn quiet for a minute has answered."""

LIST_LIMIT = 200
"""Rows per tier in the trace list. The page renders every row it is given, and 200 of
them is already more than a reader scans; the drill-down is what finds the one call."""

LIVE_LIMIT = 12
"""Cards on the pipeline canvas. Fixed nodes with unbounded cards stacked on one of them
is a pile, not a diagram."""


@console.on_request
async def _require_desk_token(request: Request):
    """
    Refuse a console read that does not carry the back-office token.

    Args:
        request: The incoming request.

    Returns:
        A 403 response, which short-circuits the handler, or None to let it run.

    Every route under this prefix reads personal data — the inbox names every customer,
    the profile carries a national ID, and the trace carries the prompt those went into.
    The desk socket is guarded for exactly this reason and these are the same rows over
    HTTP, so they take the same guard.

    The guard is worth what the token is worth, which is nothing against a reader of the
    page: `GET /` hands the token to every visitor. See this module's own docstring. This
    check earns its place as the thing that fails closed when the page is not involved —
    a request with no token, from a scanner or a stale bookmark, gets 403 rather than a
    national ID.

    The import is deferred because `server` imports this blueprint at module scope: the
    same import at the top of this file would run while `server` is still half-executed
    and `DESK_TOKEN` is not yet bound, which is an ImportError rather than a cycle
    warning.
    """
    from policydesk.web.server import DESK_TOKEN

    if request.args.get("token", "") != DESK_TOKEN:
        logger.warning("console_read_rejected", path=request.path, peer=str(request.ip))
        return response.text("需要授權", status=403)
    return None


def _json(payload: Any):
    """
    Encode a payload the way the sockets do.

    Args:
        payload: What to send.

    Returns:
        A JSON response.

    msgspec renders `datetime` and `date` as RFC 3339 strings on its own; Sanic's default
    encoder raises on both, which is what every one of these rows carries. Numerics are
    cast to float8 in the queries instead of arriving here as `Decimal`, because msgspec
    encodes a Decimal as a JSON string and the page would then have to remember to
    `Number()` every figure it does arithmetic on.
    """
    return response.raw(json.encode(payload), content_type="application/json")






def _needs_member(request: Request) -> tuple[int | None, Any]:
    """
    Resolve the scoped member, or the refusal to send instead.

    Args:
        request: The incoming request.

    Returns:
        The member id and None, or None and a 400 response.

    The desk socket refuses an unscoped connection rather than defaulting to "everyone",
    and a member-scoped read here that quietly fell back to every member would hand over
    the same rows the socket refuses.
    """
    if (member_id := int_arg(request)) is None:
        return None, response.text("需指定保戶", status=400)
    return member_id, None


# ---------------------------------------------------------------- 聊天紀錄

@console.get("/inbox")
async def inbox(request: Request):
    """
    List every conversation on the deployment, most recently active first.

    Args:
        request: The request. `?member=` names the row to pin, and is optional here
            because the list itself is not scoped.

    Returns:
        One row per member: their last message, when it landed, the stage their newest
        case sits at, and whether the desk still owes them a reply.

    This deployment is a demo, so the list is every member rather than a caseworker's own
    queue. A real desk assigns cases and this read would filter on the assignment; there
    is no assignment table to filter on, and inventing one to hide rows the token already
    protects would be a fiction in the schema.

    "Unread" is derived, not stored — there is no read receipt anywhere in the schema.
    A conversation whose last message came from the customer is one the desk has not
    answered, which is the state a caseworker is actually scanning for.
    """
    db: Database = request.app.ctx.db
    rows = await db.fetch(
        """SELECT m.member_id, m.display_name, c.case_id, c.stage,
                  lm.messages, lm.last_speaker, lm.last_text, lm.last_at
           FROM member m
           LEFT JOIN LATERAL (
               SELECT case_id, stage FROM "case"
               WHERE member_id = m.member_id ORDER BY updated_at DESC LIMIT 1
           ) c ON true
           LEFT JOIN LATERAL (
               SELECT count(*) AS messages,
                      (array_agg(speaker ORDER BY message_id DESC))[1] AS last_speaker,
                      (array_agg(text ORDER BY message_id DESC))[1] AS last_text,
                      max(created_at) AS last_at
               FROM conversation_message WHERE case_id = c.case_id
           ) lm ON true
           ORDER BY lm.last_at DESC NULLS LAST, m.member_id DESC""",
    )
    return _json({"member_id": int_arg(request), "rows": rows, "generated_at": datetime.now(UTC)})


@console.get("/transcript")
async def transcript(request: Request):
    """
    Read one customer's conversation, across every case they have opened.

    Args:
        request: The request, carrying `?member=`.

    Returns:
        Every message that member has exchanged, oldest first.

    Ordered by `message_id` rather than `created_at`: two messages written inside the
    same turn share a timestamp to the millisecond, and a timestamp sort puts the reply
    above the question that produced it.
    """
    member_id, refusal = _needs_member(request)
    if refusal is not None:
        return refusal

    db: Database = request.app.ctx.db
    rows = await db.fetch(
        """SELECT cm.message_id, cm.case_id, cm.speaker, cm.text, cm.turn_id, cm.created_at
           FROM conversation_message cm
           JOIN "case" c USING (case_id)
           WHERE c.member_id = $1::bigint
           ORDER BY cm.message_id""",
        [member_id],
    )
    who = await db.fetch_val("SELECT display_name FROM member WHERE member_id = $1::bigint", [member_id])
    return _json({"member_id": member_id, "display_name": who, "messages": rows})


# ---------------------------------------------------------------- 客戶資料

@console.get("/profile")
async def profile(request: Request):
    """
    Read one customer's whole record: who they are, what they hold, what the desk remembers.

    Args:
        request: The request, carrying `?member=`.

    Returns:
        The member row, their policies, the facts extracted from their own words, and the
        running summary of each case.

    The member row is served whole rather than trimmed to the four fields the case header
    shows. Income band, marital status and declared history are what an underwriter reads
    the file for, and a console that holds them back sends the reader to psql.
    """
    member_id, refusal = _needs_member(request)
    if refusal is not None:
        return refusal

    db: Database = request.app.ctx.db
    member = await db.fetch_one(
        """SELECT member_id, display_name, national_id, sex, birth_date, occupation, occupation_class,
                  address_city, address_district, address_rest, phone, email, marital_status,
                  income_band, medical_history, beneficiary_relation, profile_frozen_at, created_at
           FROM member WHERE member_id = $1::bigint""",
        [member_id],
    )
    if member is None:
        return response.text("查無此保戶", status=404)

    # 保險年齡 is not a year subtraction — 未滿一歲的零數超過六個月者加算一歲, measured
    # against the half-year date. The generator already implements that rule and the
    # catalogue prices off it, so it is called rather than restated in SQL: a second
    # implementation would put a customer in one age band on this tab and another on the
    # case header.
    member["insurance_age"] = insurance_age(member["birth_date"], datetime.now(UTC).date())

    # Same shape as the case snapshot's book, so the two panels cannot disagree about a
    # premium: one derivation of it, repeated here rather than a second one invented.
    policies = await db.fetch(
        """SELECT po.policy_id, po.policy_number, po.product_id, po.sum_insured, po.effective_at,
                  po.lapsed_at, po.main_policy_id, main.policy_number AS main_policy_number,
                  pr.name AS product_name, pr.line, pr.attachment,
                  round(coalesce(ce.unit_premium, 0) * po.sum_insured / 1000.0)::float8 AS annual_premium
           FROM policy po
           JOIN product pr USING (product_id)
           LEFT JOIN catalog_entry ce USING (product_id)
           LEFT JOIN policy main ON main.policy_id = po.main_policy_id
           WHERE po.member_id = $1::bigint
           ORDER BY po.main_policy_id NULLS FIRST, po.policy_id""",
        [member_id],
    )
    facts = await db.fetch(
        """SELECT key, value, category, source_message_id, updated_at
           FROM member_fact WHERE member_id = $1::bigint ORDER BY updated_at DESC""",
        [member_id],
    )
    cases = await db.fetch(
        """SELECT case_id, kind, stage, case_version, summary, facts_extracted_at, created_at, updated_at
           FROM "case" WHERE member_id = $1::bigint ORDER BY updated_at DESC""",
        [member_id],
    )
    # The three service tables the pane was built before. A caseworker looking at a customer
    # sees what they bought and nothing about what has happened since — no premium behind or
    # ahead, nobody named on the contract, no claim in flight. Those are the three things the
    # customer is most likely to be ringing about.
    payments, beneficiaries, claims = await asyncio.gather(
        db.fetch(
            """SELECT pp.due_at, pp.paid_at, pp.amount, pp.method, po.policy_number
               FROM premium_payment pp JOIN policy po USING (policy_id)
               WHERE po.member_id = $1::bigint
               ORDER BY pp.paid_at IS NOT NULL, pp.due_at DESC
               LIMIT $2::int""",
            [member_id, LIST_LIMIT],
        ),
        db.fetch(
            """SELECT pb.display_name, pb.relation, pb.share, pb.designated_at, po.policy_number
               FROM policy_beneficiary pb JOIN policy po USING (policy_id)
               WHERE po.member_id = $1::bigint
               ORDER BY po.policy_number, pb.share DESC""",
            [member_id],
        ),
        db.fetch(
            """SELECT c.claim_id, c.kind, c.event_at, c.filed_at, c.stage, c.outcome,
                      c.decided_at, c.paid_amount, po.policy_number, pr.name AS product_name
               FROM claim c JOIN policy po USING (policy_id) JOIN product pr USING (product_id)
               WHERE po.member_id = $1::bigint
               ORDER BY c.filed_at DESC""",
            [member_id],
        ),
    )
    return _json({
        "member": member, "policies": policies, "facts": facts, "cases": cases,
        "payments": payments, "beneficiaries": beneficiaries, "claims": claims,
    })


# ---------------------------------------------------------------- LLM 追蹤

@console.get("/llm")
async def llm_list(request: Request):
    """
    List the model trace at one of its three tiers.

    Args:
        request: The request. `?tier=` is conversation, turn or call; `?case_id=` and
            `?turn_id=` narrow the tier below the one that was clicked.

    Returns:
        Rows for that tier.

    Deployment-wide on purpose — see the module docstring. A conversation is a case, a
    turn is one customer message answered, and a call is one row of `llm_usage`; the
    three tiers are the same table grouped at three widths, which is why they share an
    endpoint rather than growing three.
    """
    db: Database = request.app.ctx.db
    tier = request.args.get("tier", "turn")
    case_id = int_arg(request, "case_id")
    turn_id = request.args.get("turn_id") or None

    match tier:
        case "conversation":
            rows = await db.fetch(
                """SELECT u.case_id, m.member_id, m.display_name,
                          count(*) AS calls,
                          count(DISTINCT u.turn_id) AS turns,
                          sum(u.prompt_tokens) AS prompt_tokens,
                          sum(u.completion_tokens) AS completion_tokens,
                          sum(u.cached_tokens) AS cached_tokens,
                          sum(u.total_tokens) AS total_tokens,
                          sum(u.cost_usd)::float8 AS cost_usd,
                          min(u.created_at) AS started_at,
                          max(u.created_at) AS last_at
                   FROM llm_usage u
                   LEFT JOIN "case" c ON c.case_id = u.case_id
                   LEFT JOIN member m ON m.member_id = c.member_id
                   GROUP BY u.case_id, m.member_id, m.display_name
                   ORDER BY max(u.created_at) DESC
                   LIMIT $1::int""",
                [LIST_LIMIT],
            )
        case "call":
            rows = await db.fetch(
                """SELECT u.id, u.case_id, u.turn_id, u.phase, u.scenario, u.tool_names,
                          u.provider, u.model, u.prompt_tokens, u.completion_tokens,
                          u.cached_tokens, u.total_tokens, u.cost_usd::float8 AS cost_usd,
                          u.latency_ms, u.created_at, m.display_name
                   FROM llm_usage u
                   LEFT JOIN "case" c ON c.case_id = u.case_id
                   LEFT JOIN member m ON m.member_id = c.member_id
                   WHERE ($1::bigint IS NULL OR u.case_id = $1::bigint)
                     AND ($2::text IS NULL OR u.turn_id = $2::text)
                   ORDER BY u.id DESC
                   LIMIT $3::int""",
                [case_id, turn_id, LIST_LIMIT],
            )
        case _:
            # A turn is keyed by turn_id, and the offline facts sweep writes rows with
            # none — it answers no customer message. Those are calls, and they show at
            # the call tier; folding them into one nameless turn would invent a turn.
            rows = await db.fetch(
                """SELECT u.turn_id, u.case_id, m.display_name,
                          max(u.scenario) AS scenario,
                          array_agg(u.phase ORDER BY u.id) AS phases,
                          count(*) AS calls,
                          sum(u.prompt_tokens) AS prompt_tokens,
                          sum(u.completion_tokens) AS completion_tokens,
                          sum(u.cached_tokens) AS cached_tokens,
                          sum(u.total_tokens) AS total_tokens,
                          sum(u.latency_ms) AS latency_ms,
                          min(u.created_at) AS started_at
                   FROM llm_usage u
                   LEFT JOIN "case" c ON c.case_id = u.case_id
                   LEFT JOIN member m ON m.member_id = c.member_id
                   WHERE u.turn_id IS NOT NULL
                     AND ($1::bigint IS NULL OR u.case_id = $1::bigint)
                   GROUP BY u.turn_id, u.case_id, m.display_name
                   ORDER BY min(u.created_at) DESC
                   LIMIT $2::int""",
                [case_id, LIST_LIMIT],
            )
            tier = "turn"

    return _json({"tier": tier, "case_id": case_id, "turn_id": turn_id, "rows": rows})


@console.get("/llm/call/<call_id:int>")
async def llm_call(request: Request, call_id: int):
    """
    Read one model call whole, request and response bodies included.

    Args:
        request: The request.
        call_id: Which row of `llm_usage`.

    Returns:
        The row, or 404.

    The bodies are left as parsed JSON rather than pretty-printed here: the page decides
    its own indentation, and a server-side `json.dumps(indent=2)` would arrive as one
    long string the reader cannot fold.
    """
    db: Database = request.app.ctx.db
    row = await db.fetch_one(
        """SELECT u.id, u.case_id, u.turn_id, u.phase, u.scenario, u.tool_names, u.provider, u.model,
                  u.prompt_tokens, u.completion_tokens, u.cached_tokens, u.total_tokens,
                  u.cost_usd::float8 AS cost_usd, u.latency_ms, u.request, u.response, u.created_at,
                  m.display_name
           FROM llm_usage u
           LEFT JOIN "case" c ON c.case_id = u.case_id
           LEFT JOIN member m ON m.member_id = c.member_id
           WHERE u.id = $1::bigint""",
        [call_id],
    )
    if row is None:
        return response.text("查無此呼叫", status=404)
    return _json(row)


# ---------------------------------------------------------------- 即時流程圖

@console.get("/live")
async def live(request: Request):
    """
    Report where each recent turn has got to in the pipeline.

    Args:
        request: The request.

    Returns:
        The fixed node order and one card per recent turn, carrying the phase it has
        reached and how long ago it wrote its last row.

    Polled, not pushed. The page asks every two seconds; server-sent events would carry
    the same rows a second sooner and need a broadcast fan-out this module deliberately
    does not have — it writes nothing and holds no sockets. Out of scope, and named here
    so the next reader does not take the polling for an oversight.

    A turn is only visible through the rows it has already written, so "in flight" is
    inferred from the gap since its last one rather than read off the table. Past
    LIVE_IDLE_S the card parks on the terminal node: the reply went out.
    """
    db: Database = request.app.ctx.db
    rows = await db.fetch(
        """SELECT u.turn_id, u.case_id, m.display_name,
                  max(u.scenario) AS scenario,
                  array_agg(u.phase ORDER BY u.id) AS phases,
                  (array_agg(u.phase ORDER BY u.id DESC))[1] AS phase,
                  count(*) AS calls,
                  sum(u.total_tokens) AS total_tokens,
                  sum(u.latency_ms) AS latency_ms,
                  max(u.created_at) AS last_at,
                  extract(epoch FROM now() - max(u.created_at))::float8 AS age_s
           FROM llm_usage u
           LEFT JOIN "case" c ON c.case_id = u.case_id
           LEFT JOIN member m ON m.member_id = c.member_id
           WHERE u.turn_id IS NOT NULL
           GROUP BY u.turn_id, u.case_id, m.display_name
           ORDER BY max(u.created_at) DESC
           LIMIT $1::int""",
        [LIVE_LIMIT],
    )
    for row in rows:
        row["settled"] = (row["age_s"] or 0) > LIVE_IDLE_S
        row["node"] = "response" if row["settled"] else row["phase"]
    return _json({"nodes": PIPELINE, "idle_after_s": LIVE_IDLE_S, "turns": rows, "generated_at": datetime.now(UTC)})


# ---------------------------------------------------------------- 各情境 token 消耗

@console.get("/scenarios")
async def scenarios(request: Request):
    """
    Total the model spend per scenario, heaviest first.

    Args:
        request: The request.

    Returns:
        One row per scenario, plus the bucket for calls that belong to none.

    `scenario` is NULL for `phase='route'` by design: routing is the call that *chooses*
    the scenario, so at the moment it runs there is not one yet. The offline facts sweep
    lands there too, for the same reason — it answers no customer message. Those rows are
    half the deployment's token spend, so the NULL group is returned as its own bucket
    with the phases that fell into it; dropping it would under-count the bill by more
    than any single scenario costs.
    """
    db: Database = request.app.ctx.db
    rows = await db.fetch(
        """SELECT u.scenario,
                  array_agg(DISTINCT u.phase) AS phases,
                  count(*) AS calls,
                  sum(u.prompt_tokens) AS prompt_tokens,
                  sum(u.completion_tokens) AS completion_tokens,
                  sum(u.cached_tokens) AS cached_tokens,
                  sum(u.total_tokens) AS total_tokens,
                  sum(u.cost_usd)::float8 AS cost_usd,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY u.latency_ms)::float8 AS p50_ms,
                  percentile_cont(0.95) WITHIN GROUP (ORDER BY u.latency_ms)::float8 AS p95_ms
           FROM llm_usage u
           GROUP BY u.scenario
           ORDER BY sum(u.total_tokens) DESC""",
    )
    return _json({"rows": rows})


# ---------------------------------------------------------------- LLM Dashboard

@console.get("/dashboard")
async def dashboard(request: Request):
    """
    Report the deployment's model usage: the mix, the fortnight, today, and the cache.

    Args:
        request: The request.

    Returns:
        Totals, the per-model and per-phase mixes, and a fourteen-day series.

    The series runs off `generate_series` rather than off the rows, so a day with no
    traffic is a zero on the axis instead of a missing bar. A fortnight of activity with
    a gap in the middle reads as a gap; the same fortnight drawn from the rows alone
    reads as a shorter, busier one.
    """
    db: Database = request.app.ctx.db
    totals = await db.fetch_one(
        """SELECT count(*) AS calls,
                  count(DISTINCT turn_id) AS turns,
                  count(DISTINCT case_id) AS cases,
                  sum(prompt_tokens) AS prompt_tokens,
                  sum(completion_tokens) AS completion_tokens,
                  sum(cached_tokens) AS cached_tokens,
                  sum(total_tokens) AS total_tokens,
                  sum(cost_usd)::float8 AS cost_usd,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)::float8 AS p50_ms,
                  percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)::float8 AS p95_ms,
                  count(*) FILTER (WHERE created_at >= date_trunc('day', now())) AS calls_today,
                  coalesce(sum(total_tokens) FILTER (WHERE created_at >= date_trunc('day', now())), 0) AS tokens_today,
                  min(created_at) AS first_at,
                  max(created_at) AS last_at
           FROM llm_usage""",
    )
    models = await db.fetch(
        """SELECT provider, model, count(*) AS calls, sum(total_tokens) AS total_tokens,
                  avg(latency_ms)::float8 AS avg_ms
           FROM llm_usage GROUP BY provider, model ORDER BY sum(total_tokens) DESC""",
    )
    phases = await db.fetch(
        """SELECT phase, count(*) AS calls, sum(total_tokens) AS total_tokens,
                  sum(cached_tokens) AS cached_tokens, avg(latency_ms)::float8 AS avg_ms
           FROM llm_usage GROUP BY phase ORDER BY sum(total_tokens) DESC""",
    )
    days = await db.fetch(
        """SELECT d::date AS day,
                  coalesce(u.calls, 0) AS calls,
                  coalesce(u.total_tokens, 0) AS total_tokens,
                  coalesce(u.prompt_tokens, 0) AS prompt_tokens,
                  coalesce(u.cached_tokens, 0) AS cached_tokens
           FROM generate_series((now() - interval '13 days')::date, now()::date, interval '1 day') d
           LEFT JOIN (
               SELECT created_at::date AS day, count(*) AS calls,
                      sum(total_tokens) AS total_tokens,
                      sum(prompt_tokens) AS prompt_tokens,
                      sum(cached_tokens) AS cached_tokens
               FROM llm_usage GROUP BY created_at::date
           ) u ON u.day = d::date
           ORDER BY d""",
    )
    return _json({"totals": totals or {}, "models": models, "phases": phases, "days": days})
