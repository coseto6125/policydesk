"""
The server behind both panes.

One page, two sockets. `/ws/customer` carries the conversation on the right;
`/ws/desk` carries the back office on the left. Both are fed by the same case
snapshot, and every mutation goes through `core.commands`, so the two panes cannot
disagree about a case: there is one writer, and both sides render what it returns.

A visitor is a display name. Claiming a name that is already live evicts the previous
holder with a notice, because a split session would show a caseworker one story while
the customer reads another. On first claim the visitor becomes a real member row with
a real portfolio — a demo whose users exist only in memory can show a caseworker
nothing, and cannot be reopened tomorrow.
"""

import asyncio
import base64
import contextlib
import os
import secrets
from datetime import UTC, date, datetime
from html import escape
from pathlib import Path
from typing import Any

from msgspec import DecodeError, json
from sanic import Request, Sanic, Websocket, html, response

from policydesk.agent import memory
from policydesk.agent.executor import Turn, run_turn
from policydesk.agent.scenario import OPENERS
from policydesk.bootloader import logger
from policydesk.core import commands as cmd
from policydesk.core.db import Database
from policydesk.gov.identity import Sex, verify
from policydesk.llm.provider import build_provider
from policydesk.retrieval.base import HybridRetriever
from policydesk.retrieval.index import open_index
from policydesk.retrieval.vectors import open_vectors
from policydesk.synthetic.alias import mint
from policydesk.synthetic.person import generate, insurance_age, occupation_catalogue
from policydesk.synthetic.portfolio import DEFAULT_PRESET, enrol, preset_catalogue
from policydesk.web.console import console
from policydesk.web.highlight import page_count, page_image
from policydesk.web.params import int_arg
from policydesk.web.session import Registry

STATIC = Path(__file__).parent / "static"

# The back office reads every member's national ID, occupation and address. Without a
# token anyone who can reach the port reads all of it — an acceptance run connected
# straight to /ws/desk and pulled 17 cases with full personal data. A shared secret is
# the smallest thing that is still true; a real deployment puts staff behind SSO.
#
# A hardcoded fallback would be worse than none: a default that ships in the source is
# a password everyone already has, and it makes an unconfigured deployment look
# protected. When the variable is unset a fresh token is minted per boot and logged, so
# the operator has to read it out of the log to open the pane, and nobody else can
# guess it.
DESK_TOKEN = os.environ.get("POLICYDESK_DESK_TOKEN") or secrets.token_urlsafe(16)

# A display name is an identifier, not prose. An acceptance run created a case whose
# customer name was several hundred characters, which no pane can render and no
# caseworker can search for.
MAX_NAME = 40

ID_LENGTH = 10
"""A Taiwanese national ID, exactly. One letter and nine digits."""

MAX_CONFIRM_ATTEMPTS = 3
"""Tries at 資料核對 before the session is handed to a person. Three is what a call
centre allows, and an unbounded retry is an offline guessing machine."""

def _mask(national_id: str) -> str:
    """
    Show enough of an ID to prompt for it, never enough to pass the check.

    Args:
        national_id: The full number.

    Returns:
        The first two characters and the last, with the middle hidden.

    """
    return f"{national_id[:2]}{'*' * max(0, len(national_id) - 3)}{national_id[-1:]}"


CORPUS = Path(os.environ.get("POLICYDESK_CORPUS", "data/cathay"))
"""Where the insurer's own PDFs live, named by digest. The desk serves a contract from
here so a figure quoted in the chat can be checked against the page it came from."""

app = Sanic("policydesk")
app.ctx.registry = Registry()
app.ctx.desk_sockets = set()
# The back office's seven tabs, read-only, under /api/console. Registered here rather
# than imported at the top of `console` because that module reads DESK_TOKEN from this
# one, and the read is deferred to request time for the same reason.
app.blueprint(console)


@app.before_server_start
async def _open_db(application: Sanic, _loop) -> None:
    """Open the pool once, before the first request."""
    application.ctx.db = Database()
    # One seam, and which implementation sits behind it is a deployment choice: the
    # HTTP API when a key is set, the locally signed-in codex CLI otherwise. Neither
    # one answers when it cannot reach a model — a desk that invents an answer about
    # someone's policy is worse than one that admits it is down.
    application.ctx.provider = build_provider()
    logger.info("provider_ready", provider=application.ctx.provider.name)
    # Memory is written off the reply path. A customer never waits on it, and a desk
    # whose sweep is down still answers — with a shorter memory, which is a degradation
    # rather than an outage.
    application.ctx.sweep = asyncio.create_task(memory.sweep_loop(application.ctx.db, application.ctx.provider))
    # Opened once. A directory walk and an analyzer registration do not belong on the
    # path a customer is waiting on, and the first build takes twenty seconds.
    lexical, semantic = await asyncio.gather(
        open_index(application.ctx.db), open_vectors(application.ctx.db)
    )
    channels = [r for r in (lexical, semantic) if r is not None]
    application.ctx.clauses = HybridRetriever(channels) if channels else None
    logger.info("retrieval_ready", channels=[r.name for r in channels])
    if not os.environ.get("POLICYDESK_DESK_TOKEN"):
        logger.warning("desk_token_generated", token=DESK_TOKEN, hint="set POLICYDESK_DESK_TOKEN to fix it across restarts")


@app.after_server_stop
async def _close_db(application: Sanic, _loop) -> None:
    """Close the pool and the model session on the way out."""
    application.ctx.sweep.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await application.ctx.sweep
    await application.ctx.db.close()
    await application.ctx.provider.close()


@app.get("/")
async def index(_request: Request):
    """Serve the two-pane page."""
    return html((STATIC / "index.html").read_text().replace("__DESK_TOKEN__", DESK_TOKEN))


@app.get("/doc/<document_id:int>")
async def download_document(request: Request, document_id: int):
    """
    Render one document for the applicant to read and sign.

    Args:
        request: The request.
        document_id: Which document.

    Returns:
        The document as a printable page, or 404.

    Served as HTML rather than PDF on purpose: the applicant reads it in the browser,
    prints or saves it, and returns a signed copy. A PDF here would need a renderer in
    the image for no gain the applicant can see.

    """
    # Same guard as the desk socket. document_id is a sequential bigserial, so without
    # it `for i in $(seq 1 30)` walks every applicant's national ID, birth date,
    # address and declared conditions — which is exactly what an acceptance run did.
    if request.args.get("token", "") != DESK_TOKEN:
        logger.warning("document_access_rejected", document_id=document_id, peer=str(request.ip))
        return response.text("需要授權", status=403)

    row = await request.app.ctx.db.fetch_one(
        """SELECT d.document_id, d.kind, d.title, d.sha, d.signed_at,
                  m.display_name, m.national_id, m.birth_date, m.occupation, m.occupation_class,
                  m.address_city, m.address_district, m.address_rest, m.phone, m.email,
                  m.marital_status, m.medical_history, m.beneficiary_relation,
                  c.case_id, c.adviser_name, c.adviser_licence
           FROM case_document d
           JOIN "case" c USING (case_id)
           JOIN member m USING (member_id)
           WHERE d.document_id = $1::bigint""",
        [document_id],
    )
    if row is None:
        return response.text("查無此文件", status=404)
    return html(_render_document(row))


@app.get("/api/alias")
async def alias(request: Request):
    """
    Mint a display name nobody holds.

    Args:
        request: The request.

    Returns:
        One alias, checked against the members already enrolled and the names currently
        connected.

    The name is the account, so it has to be unique, and a demo audience typing their
    own collides on the third 陳. Minting it also keeps the room anonymous: nobody has
    to put a real name into a page that renders national IDs.

    """
    rows = await request.app.ctx.db.fetch("SELECT display_name FROM member")
    taken = {r["display_name"] for r in rows} | set(request.app.ctx.registry.names)
    return response.json({"alias": mint(taken)})


@app.get("/health")
async def health(request: Request):
    """Report whether the desk can reach its database."""
    products = await request.app.ctx.db.fetch_val("SELECT count(*) FROM product")
    return response.json({"ok": True, "products": products})


async def _broadcast_desk(application: Sanic, payload: dict[str, Any]) -> None:
    """
    Push a case snapshot to every open back-office pane.

    Args:
        application: The running app.
        payload: What to send.

    A caseworker watching a case must see it move as the customer moves it. Polling
    would show them a version behind, which is exactly the drift that makes two panes
    into two stories.
    """
    body = json.encode(payload).decode()
    sockets = list(application.ctx.desk_sockets)
    if not sockets:
        return

    # Sent together, not in turn. This runs inline on every customer message, so a
    # sequential loop lets one slow-but-alive desk pane stall both the panes behind it
    # and the customer's own turn — the except clause anticipated dead sockets, not
    # slow ones.
    results = await asyncio.gather(*(socket.send(body) for socket in sockets), return_exceptions=True)
    application.ctx.desk_sockets -= {
        socket for socket, outcome in zip(sockets, results, strict=True) if isinstance(outcome, BaseException)
    }


async def _next_serial(db: Database) -> int:
    """
    Give the next national-ID serial.

    Args:
        db: The database.

    Returns:
        How many members exist, which is the next position in the demo's ID series.

    """
    return await db.fetch_val("SELECT count(*) FROM member") or 0


@app.websocket("/ws/customer")
async def customer_socket(request: Request, ws: Websocket) -> None:
    """
    Carry one customer's conversation.

    Args:
        request: The upgrade request.
        ws: The socket.

    """
    db: Database = request.app.ctx.db
    registry: Registry = request.app.ctx.registry
    name: str | None = None
    previous_name: str = ""
    session = None
    case_id: int | None = None
    member_id: int | None = None

    # Session state, and the reason it is a session and not a row: this is 資料核對, the
    # check a call centre runs before it will discuss anything about your policies. It
    # proves the person on this connection is the customer. It is not the 投保身分驗證
    # that `identity_check` holds — that one runs once, against the government mock, at
    # the signing stage, and stays valid for the case. This one expires with the socket,
    # because the next connection is a different person until it proves otherwise.
    confirmed = False
    attempts = 0
    pending_question: str | None = None
    """What they asked before the check interrupted them. Captured once, so a wrong
    number typed after it does not become the question the desk comes back to."""

    try:
        async for raw in ws:
            if (message := _decode(raw)) is None:
                await ws.send(json.encode({"type": "notice", "text": "訊息格式不正確", "level": "warn"}).decode())
                continue
            match message.get("type"):
                case "hello":
                    name = (message.get("name") or "").strip()
                    if not name:
                        await ws.send(json.encode({"type": "notice", "text": "請輸入姓名", "level": "warn"}).decode())
                        continue
                    if len(name) > MAX_NAME:
                        await ws.send(json.encode({
                            "type": "notice", "text": f"姓名請勿超過 {MAX_NAME} 字", "level": "warn",
                        }).decode())
                        continue

                    # One socket may say hello twice, under two names. Without
                    # releasing the first, its entry stays in the registry pointing at
                    # a socket that has since renamed itself, and the finally below
                    # only releases the last name — so the first is held until the
                    # process restarts and nobody can claim it again.
                    if session is not None and name != previous_name:
                        registry.release(previous_name, session)
                    session = await registry.claim(name, ws.send, ws.close)
                    previous_name = name

                    # Nothing is written yet. The visitor picks their own sex, age and
                    # occupation first, because those three decide what can be sold to
                    # them — a profile assigned behind their back is a demo where the
                    # underwriting result was chosen by a random seed.
                    #
                    # A member who already exists skips the picker: their profile froze
                    # on their first message and re-enrolling would silently rewrite the
                    # person their existing policies belong to.
                    existing = await db.fetch_one(
                        """SELECT member_id, national_id, sex, birth_date, occupation, occupation_class
                           FROM member WHERE display_name = $1::text""",
                        [name],
                    )
                    if existing is not None:
                        member_id = existing["member_id"]
                        case_id = (await cmd.open_case(db, member_id)).case_id
                        await ws.send(json.encode({
                            "type": "profile",
                            "name": name,
                            # Masked. A returning session has proved nothing yet, and
                            # sending the number to the browser before the check is
                            # sending the answer along with the question.
                            "national_id": _mask(existing["national_id"]),
                            "sex": existing["sex"],
                            "birth_date": existing["birth_date"].isoformat(),
                            "insurance_age": insurance_age(existing["birth_date"], datetime.now(UTC).date()),
                            "member_id": member_id,
                            "occupation": existing["occupation"],
                            "occupation_class": existing["occupation_class"],
                            "returning": True,
                        }).decode())
                        await _push_case(db, ws, request.app, case_id)
                        continue

                    draft = generate(name, await _next_serial(db))
                    await ws.send(json.encode({
                        "type": "draft",
                        "name": name,
                        "sex": draft.sex.value,
                        "age": draft.age_on(datetime.now(UTC).date()),
                        "occupation": draft.occupation,
                        "occupations": occupation_catalogue(),
                        "presets": preset_catalogue(),
                    }).decode())

                case "enrol" if name is not None and case_id is None:
                    # The class is looked up from the occupation server-side, so a client
                    # cannot name its own 職業等級 and sell itself a product it is barred
                    # from. An age outside 18–85 is clamped rather than refused: the band
                    # is the insurable range, not a form-validation rule.
                    try:
                        chosen_age = int(message.get("age") or 0)
                    except (TypeError, ValueError):
                        chosen_age = 0
                    person = generate(
                        name,
                        await _next_serial(db),
                        sex=Sex.MALE if message.get("sex") == "male" else Sex.FEMALE,
                        age=chosen_age or None,
                        occupation=str(message.get("occupation") or ""),
                    )
                    member_id, written = await enrol(
                        person, db, preset=str(message.get("preset") or DEFAULT_PRESET)
                    )
                    case_id = (await cmd.open_case(db, member_id)).case_id

                    await ws.send(json.encode({
                        "type": "profile",
                        "name": name,
                        "national_id": person.national_id,
                        "sex": person.sex.value,
                        "birth_date": person.birth_date.isoformat(),
                        "insurance_age": person.insurance_age_on(datetime.now(UTC).date()),
                        "member_id": member_id,
                        "occupation": person.occupation,
                        "occupation_class": int(person.occupation_class),
                        "address": str(person.address),
                        "phone": person.phone,
                        "email": person.email,
                        "policies": written,
                    }).decode())
                    await _push_case(db, ws, request.app, case_id)

                case "say" if case_id is not None and not confirmed and len(
                    (message.get("text") or "").strip()
                ) == ID_LENGTH:
                    # An unconfirmed session typing exactly ten characters is answering
                    # the one question this desk asks before anything else. Routing it
                    # would send a national ID to a model and answer it as a question,
                    # which is how a near-miss ended up replayed as "the thing they
                    # wanted to know". Not gated on having asked first: the desk may not
                    # have asked yet, and the number is still the number.
                    #
                    # Length, not shape: this project answers every other question with a
                    # prompt rather than a pattern, and the identity mechanism is the one
                    # stated exception. A Taiwanese national ID is exactly ten characters.
                    given = (message.get("text") or "").strip().upper()
                    held = await db.fetch_val(
                        'SELECT national_id FROM member WHERE member_id = ('
                        'SELECT member_id FROM "case" WHERE case_id = $1::bigint)',
                        [case_id],
                    )
                    attempts += 1
                    await db.execute(
                        """INSERT INTO audit_event (case_id, actor, action, detail, case_version)
                           VALUES ($1::bigint,'customer',$2::text,$3::jsonb,
                                   (SELECT case_version FROM "case" WHERE case_id = $1::bigint))""",
                        [
                            case_id,
                            "identity_confirmed" if given == held else "identity_attempt",
                            {"attempts": attempts, "channel": "chat"},
                        ],
                    )
                    # Written to the audit trail, never to the transcript. A near-miss
                    # national ID is one character from the real one, and either would
                    # ride in the history block of every later prompt.
                    if given != held:
                        await ws.send(json.encode({
                            "type": "reply",
                            "text": "這組號碼與檔案不符，請再確認一次您的身分證字號。",
                            "scenario": None, "citations": [], "faults": [], "params": {}, "quick": [],
                        }).decode())
                        continue

                    confirmed = True
                    logger.info("identity_confirmed", case_id=case_id, attempts=attempts)
                    await ws.send(json.encode({"type": "confirmed"}).decode())
                    await _push_case(db, ws, request.app, case_id)

                    if not pending_question:
                        await ws.send(json.encode({
                            "type": "reply",
                            "text": "感謝您的耐心核對，身分已確認。請問需要什麼協助？",
                            "scenario": None, "citations": [], "faults": [],
                            "params": {}, "quick": list(OPENERS),
                        }).decode())
                        continue

                    # Answer what they actually asked, before the check interrupted them.
                    text, pending_question = pending_question, None
                    await _answer(request, ws, db, case_id=case_id, text=text, confirmed=True)

                case "say" if case_id is not None:
                    text = (message.get("text") or "").strip()
                    if not text:
                        continue
                    await _answer(request, ws, db, case_id=case_id, text=text, confirmed=confirmed)
                    if not confirmed:
                        # The latest question, not the first. Keeping the first replayed
                        # 嗨 after the check passed; the thing worth coming back to is
                        # whatever they were asking when the desk stopped them. The ID
                        # attempts cannot overwrite it — they never reach this branch.
                        pending_question = text

                case "upload" if case_id is not None:
                    document_id = int(message.get("document_id") or 0)
                    filename = (message.get("filename") or "").strip()
                    await db.execute(
                        "UPDATE case_document SET uploaded_name = $2::text WHERE document_id = $1::bigint",
                        [document_id, filename],
                    )
                    sha = await db.fetch_val(
                        "SELECT sha FROM case_document WHERE document_id = $1::bigint", [document_id]
                    )
                    for party in cmd.SIGNING_PARTIES:
                        outcome = await cmd.record_signature(
                            db, case_id, document_id=document_id, party=party, document_sha=sha or ""
                        )
                    if isinstance(outcome, cmd.Refusal) and outcome.missing:
                        await ws.send(json.encode({
                            "type": "notice",
                            "text": f"尚有 {len(outcome.missing)} 份文件未簽署",
                            "level": "info",
                        }).decode())
                    await _push_case(db, ws, request.app, case_id)

                case "verify" if case_id is not None:
                    national_id = (message.get("national_id") or "").strip()
                    started = datetime.now(UTC)
                    result = verify(national_id)
                    latency = int((datetime.now(UTC) - started).total_seconds() * 1000)
                    outcome = await cmd.verify_identity(
                        db, case_id, national_id=national_id,
                        verified=result.verified, reason=result.reason, latency_ms=latency,
                    )
                    if isinstance(outcome, cmd.Refusal):
                        await ws.send(json.encode({"type": "notice", "text": outcome.reason, "level": "warn"}).decode())
                    await _push_case(db, ws, request.app, case_id)

    except (ConnectionError, asyncio.CancelledError):
        logger.info("customer_socket_closed", name=name)
    finally:
        if name and session is not None:
            registry.release(name, session)


async def _push_case(db: Database, ws: Websocket, application: Sanic, case_id: int) -> None:
    """
    Send the case to the customer and to every desk pane.

    Args:
        db: The database.
        ws: The customer's socket.
        application: The running app.
        case_id: Which case.

    """
    snap = await cmd.snapshot(db, case_id)
    if snap is None:
        return
    payload = {"type": "case", **_jsonable(snap)}
    body = json.encode(payload).decode()
    await ws.send(body)
    await _broadcast_desk(application, payload)


async def _cited(db: Database, case_id: int, clause_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    """
    Resolve the clause ids in a reply to contracts the customer actually holds.

    Args:
        db: The database.
        case_id: Whose case, and through it whose policies.
        clause_ids: The ids the reply cited.

    Returns:
        One entry per contract each id resolves to, with its heading and page. A bare id
        is not a link: the same `art.6` exists in every contract in the corpus, and which
        one the reply meant is decided by the member's own book.

    """
    if not clause_ids:
        return []
    rows = await db.fetch(
        """SELECT DISTINCT c.product_id, c.clause_id, c.heading, c.page, p.name AS product_name
           FROM clause c
           JOIN product p USING (product_id)
           JOIN policy po ON po.product_id = c.product_id
           JOIN "case" k ON k.member_id = po.member_id
           WHERE k.case_id = $1::bigint AND c.clause_id = ANY($2::text[])
           ORDER BY c.clause_id""",
        [case_id, list(clause_ids)],
    )
    order = {clause_id: position for position, clause_id in enumerate(clause_ids)}
    rows.sort(key=lambda r: order.get(r["clause_id"], len(order)))
    return rows


async def _answer(
    request: Request, ws: Websocket, db: Database, *, case_id: int, text: str, confirmed: bool
) -> Turn:
    """
    Run one turn and send it, recording both halves of the exchange.

    Args:
        request: The upgrade request, for the app context.
        ws: The customer's socket.
        db: The database.
        case_id: Which case.
        text: What the customer said.
        confirmed: Whether this session has passed 資料核對.

    Returns:
        The turn, so the caller can read whether the identity gate stopped it.

    """
    await db.execute(
        """INSERT INTO conversation_message (case_id, speaker, text)
           VALUES ($1::bigint,'customer',$2::text)""",
        [case_id, text],
    )
    # The profile freezes on the first message, as specified.
    await db.execute(
        """UPDATE member SET profile_frozen_at = coalesce(profile_frozen_at, now())
           WHERE member_id = (SELECT member_id FROM "case" WHERE case_id = $1::bigint)""",
        [case_id],
    )
    member_id = await db.fetch_val('SELECT member_id FROM "case" WHERE case_id = $1::bigint', [case_id])
    turn = await run_turn(
        request.app.ctx.provider, db,
        case_id=case_id, member_id=member_id, text=text, confirmed=confirmed,
        index=request.app.ctx.clauses,
    )
    await db.execute(
        """INSERT INTO conversation_message (case_id, speaker, text, turn_id)
           VALUES ($1::bigint,'agent',$2::text,$3::text)""",
        [case_id, turn.reply, turn.turn_id],
    )
    await ws.send(json.encode({
        "type": "reply",
        "text": turn.reply,
        "scenario": turn.scenario,
        "params": turn.params,
        "quick": list(turn.quick_replies),
        "citations": _jsonable(await _cited(db, case_id, turn.citations)),
        "faults": list(turn.faults),
    }).decode())
    await _push_case(db, ws, request.app, case_id)
    return turn


@app.websocket("/ws/desk")
async def desk_socket(request: Request, ws: Websocket) -> None:
    """
    Carry one caseworker's view, and their decisions.

    Args:
        request: The upgrade request.
        ws: The socket.

    """
    db: Database = request.app.ctx.db

    # Authenticate before anything is sent. The queue alone names every customer.
    if request.args.get("token", "") != DESK_TOKEN:
        logger.warning("desk_socket_rejected", peer=str(request.ip))
        await ws.send(json.encode({"type": "notice", "text": "後台需要授權", "level": "warn"}).decode())
        await ws.close()
        return

    # And scope it. This window is one visitor: the right pane is their conversation and
    # the left is the back office view *of their case*. A queue listing every member's
    # name, national ID and policies to whoever opened the page is a different product
    # with a different audience, and the token is not what separates them — the member is.
    try:
        viewer = int(request.args.get("member", ""))
    except (TypeError, ValueError):
        logger.warning("desk_socket_unscoped", peer=str(request.ip))
        await ws.send(json.encode({"type": "notice", "text": "後台需指定保戶", "level": "warn"}).decode())
        await ws.close()
        return

    request.app.ctx.desk_sockets.add(ws)
    try:
        await ws.send(json.encode({"type": "queue", "cases": _jsonable(await _queue(db, viewer))}).decode())
        async for raw in ws:
            if (message := _decode(raw)) is None:
                continue
            match message.get("type"):
                case "decide":
                    outcome = await cmd.decide(
                        db, int(message["case_id"]),
                        approved=bool(message.get("approved")),
                        reason=message.get("reason", ""),
                        by=message.get("by", "核保人員"),
                    )
                    if isinstance(outcome, cmd.Refusal):
                        await ws.send(json.encode({"type": "notice", "text": outcome.reason, "level": "warn"}).decode())
                    else:
                        snap = await cmd.snapshot(db, outcome.case_id)
                        await _broadcast_desk(request.app, {"type": "case", **_jsonable(snap or {})})
                case "queue":
                    await ws.send(json.encode({"type": "queue", "cases": _jsonable(await _queue(db, viewer))}).decode())
                case "open":
                    # The case id comes off the wire, so ownership is checked here and
                    # not inferred from the queue the client was handed.
                    snap = await cmd.snapshot(db, int(message["case_id"]))
                    if snap and snap["member_id"] == viewer:
                        await ws.send(json.encode({"type": "case", **_jsonable(snap)}).decode())
                    else:
                        logger.warning("case_out_of_scope", case_id=message.get("case_id"), viewer=viewer)
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        request.app.ctx.desk_sockets.discard(ws)


async def _queue(db: Database, member_id: int) -> list[dict[str, Any]]:
    """
    List one customer's cases, most recent first.

    Args:
        db: The database.
        member_id: Whose cases. Never optional — an unscoped read here is every
            customer's name and stage handed to whoever opened the page.

    Returns:
        Case rows for that member.

    """
    return await db.fetch(
        """SELECT c.case_id, c.kind, c.stage, c.case_version, c.updated_at, m.display_name
           FROM "case" c JOIN member m USING (member_id)
           WHERE c.member_id = $1::bigint
           ORDER BY c.updated_at DESC LIMIT 50""",
        [member_id],
    )


@app.get("/clause/<product_id:str>/<clause_id:str>")
async def clause_page(request: Request, product_id: str, clause_id: str):
    """
    Show the contract page a citation points at, with the clause marked.

    Args:
        request: The request, carrying the viewing member.
        product_id: Which contract.
        clause_id: Which clause, as the reply cited it.

    Returns:
        A page holding the rendered image and the clause text, or a refusal.

    A clause id in a reply is a promise that the sentence came from that contract on that
    page. This is where the promise is redeemed: the insurer's own page, the lines the
    clause occupies under a marker, and the stored text below it so the two can be read
    against each other.

    """
    try:
        viewer = int(request.args.get("member", ""))
    except (TypeError, ValueError):
        return response.text("需指定保戶", status=403)

    row = await request.app.ctx.db.fetch_one(
        """SELECT c.heading, c.verbatim, c.page, c.kind, p.name AS product_name, p.doc_sha
           FROM clause c JOIN product p USING (product_id)
           WHERE c.product_id = $1::text AND c.clause_id = $2::text
             AND EXISTS (SELECT 1 FROM policy po
                         WHERE po.product_id = c.product_id AND po.member_id = $3::bigint)""",
        [product_id, clause_id, viewer],
    )
    if row is None:
        logger.warning("clause_out_of_scope", product_id=product_id, clause_id=clause_id, viewer=viewer)
        return response.text("查無您名下保單的這條條款", status=404)

    # Off the event loop: a render is about 250 ms of CPU, and Sanic serves every other
    # socket from the same thread.
    png = await asyncio.to_thread(
        page_image, CORPUS / f"{row['doc_sha']}.pdf", row["page"], row["verbatim"]
    )
    return html(_render_clause(row, clause_id, png))


async def _held(db: Database, product_id: str, viewer: int) -> dict[str, Any] | None:
    """
    Read a contract, but only for someone holding a policy on it.

    Args:
        db: The database.
        product_id: Which contract.
        viewer: The member asking.

    Returns:
        The product row, or None. The catalogue is public; which contract this visitor
        may open is decided by the policies in their own name.

    """
    return await db.fetch_one(
        """SELECT p.product_id, p.name, p.doc_sha FROM product p
           WHERE p.product_id = $1::text
             AND EXISTS (SELECT 1 FROM policy po
                         WHERE po.product_id = p.product_id AND po.member_id = $2::bigint)""",
        [product_id, viewer],
    )




@app.get("/contract/<product_id:str>")
async def contract(request: Request, product_id: str):
    """
    Show a contract as pages, or hand over the file itself.

    Args:
        request: The request, carrying the viewing member and an optional `download`.
        product_id: Which contract.

    Returns:
        A viewer page, the PDF when `download=1`, or a refusal.

    Serving the PDF straight into a tab looked right and was not: a browser with no PDF
    plugin aborts the navigation — `net::ERR_ABORTED` in headless Chromium, a spinner
    that never stops in an embedded webview — and the reader is left with nothing. So the
    default is a page of rendered images, which every browser can draw, and the file
    stays one click away for anyone who wants it.

    """
    if (viewer := int_arg(request)) is None:
        return response.text("需指定保戶", status=403)
    row = await _held(request.app.ctx.db, product_id, viewer)
    if row is None:
        logger.warning("contract_out_of_scope", product_id=product_id, viewer=viewer)
        return response.text("查無您名下的這張契約", status=404)

    path = CORPUS / f"{row['doc_sha']}.pdf"
    if not path.is_file():
        return response.text("契約條款檔案不在本機語料庫中", status=404)

    if request.args.get("download") == "1":
        return await response.file(
            path,
            mime_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{row["doc_sha"]}.pdf"'},
        )
    pages = await asyncio.to_thread(page_count, path)
    return html(_render_contract(row, pages, viewer))


@app.get("/contract/<product_id:str>/page/<page:int>")
async def contract_page(request: Request, product_id: str, page: int):
    """
    Render one page of a contract.

    Args:
        request: The request, carrying the viewing member.
        product_id: Which contract.
        page: Which page, 1-based.

    Returns:
        A PNG, or a refusal.

    Requested lazily by the viewer, one image per page, so opening a 31-page contract
    costs one render rather than thirty-one.

    No `.png` on the path: Sanic's router rejects a parameter followed by a literal
    extension outright — `Invalid declaration: <page:int>.png`, raised at import.

    """
    if (viewer := int_arg(request)) is None:
        return response.text("需指定保戶", status=403)
    row = await _held(request.app.ctx.db, product_id, viewer)
    if row is None:
        return response.text("查無您名下的這張契約", status=404)

    png = await asyncio.to_thread(page_image, CORPUS / f"{row['doc_sha']}.pdf", page, "")
    if png is None:
        return response.text("這一頁無法還原", status=404)
    return response.raw(png, content_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/llm-turns")
async def llm_turns(request: Request):
    """
    Roll model calls up by turn — the trace list.

    Args:
        request: The request.

    Returns:
        One row per turn_id with its phases, tokens, cost and latency.

    """
    rows = await request.app.ctx.db.fetch(
        """SELECT turn_id, min(created_at) AS started_at, count(*) AS calls,
                  array_agg(phase ORDER BY id) AS phases,
                  sum(prompt_tokens) AS prompt_tokens, sum(completion_tokens) AS completion_tokens,
                  sum(cached_tokens) AS cached_tokens, sum(total_tokens) AS total_tokens,
                  sum(cost_usd) AS cost_usd, sum(latency_ms) AS latency_ms,
                  max(case_id) AS case_id
           FROM llm_usage WHERE turn_id IS NOT NULL
           GROUP BY turn_id ORDER BY min(created_at) DESC LIMIT 100"""
    )
    return response.json(_jsonable(rows))


@app.get("/api/llm-conversations")
async def llm_conversations(request: Request):
    """
    Roll model calls up by case — the session list.

    Args:
        request: The request.

    Returns:
        One row per case with its turn count, tokens and cost.

    """
    rows = await request.app.ctx.db.fetch(
        """SELECT u.case_id, m.display_name, count(DISTINCT u.turn_id) AS turns, count(*) AS calls,
                  sum(u.total_tokens) AS total_tokens, sum(u.cost_usd) AS cost_usd,
                  sum(u.latency_ms) AS latency_ms, max(u.created_at) AS last_at
           FROM llm_usage u
           LEFT JOIN "case" c ON c.case_id = u.case_id
           LEFT JOIN member m ON m.member_id = c.member_id
           WHERE u.case_id IS NOT NULL
           GROUP BY u.case_id, m.display_name ORDER BY max(u.created_at) DESC LIMIT 100"""
    )
    return response.json(_jsonable(rows))


def _decode(raw: str | bytes) -> dict[str, Any] | None:
    """
    Read one socket frame.

    Args:
        raw: What arrived.

    Returns:
        The message, or None when it was not a JSON object. A malformed frame
        previously raised out of the handler and closed the socket, so one bad client
        ended a session that was otherwise fine.

    """
    try:
        message = json.decode(raw.encode() if isinstance(raw, str) else raw)
    except (DecodeError, ValueError, UnicodeDecodeError):
        return None
    return message if isinstance(message, dict) else None


def _jsonable(value: Any) -> Any:
    """
    Convert database rows into something json.encode accepts.

    Args:
        value: A row, a list of rows, or a scalar.

    Returns:
        The same shape with dates, datetimes and Decimals rendered as strings or floats.

    """
    match value:
        case dict():
            return {k: _jsonable(v) for k, v in value.items()}
        case list():
            return [_jsonable(v) for v in value]
        case datetime() | date():
            return value.isoformat()
        case _ if hasattr(value, "as_tuple"):  # Decimal
            return float(value)
        case _:
            return value


def main() -> None:
    """
    Run the desk.

    Binds every interface by default, which is what a demo on a hackathon network needs
    and is also the whole of this deployment's exposure: the page hands `DESK_TOKEN` to
    every visitor, so anyone who can reach the port can read the console — every
    customer's national ID, address, transcript and the prompts behind each reply.

    `POLICYDESK_HOST=127.0.0.1` closes that to the machine it runs on. The default is left
    open because changing it silently would break the demo it was chosen for; the warning
    below is so nobody has to discover the trade-off from a stranger.

    """
    host = os.environ.get("POLICYDESK_HOST", "0.0.0.0")  # noqa: S104
    if host == "0.0.0.0":  # noqa: S104
        logger.warning(
            "desk_reachable_on_every_interface",
            hint="the page carries DESK_TOKEN, so anyone who can reach this port reads the console",
            close_it="POLICYDESK_HOST=127.0.0.1",
        )
    app.run(host=host, port=int(os.environ.get("POLICYDESK_PORT", "8100")), single_process=True, access_log=False)


_DOC_STYLE = """
  body { font-family: "Noto Sans TC", system-ui, sans-serif; max-width: 720px; margin: 40px auto;
         padding: 0 24px 60px; line-height: 1.9; color: #1a2230; }
  h1 { font-size: 20px; border-bottom: 2px solid #1a2230; padding-bottom: 10px; }
  .meta { font-family: ui-monospace, monospace; font-size: 11.5px; color: #6b7684; margin-bottom: 22px; }
  dl { display: grid; grid-template-columns: 130px 1fr; gap: 6px 16px; font-size: 14px; }
  dt { color: #6b7684; }
  dd { margin: 0; }
  .sign { margin-top: 40px; border-top: 1px dashed #b9c2cd; padding-top: 20px; font-size: 14px; }
  .sign .line { display: inline-block; border-bottom: 1px solid #1a2230; width: 220px; margin-left: 10px; }
  .note { margin-top: 26px; font-size: 12.5px; color: #6b7684; background: #f5f7fa; padding: 12px 16px; }
"""

# What each document actually says, so a signature means something. Statutory wording is
# summarised, not quoted in full: this is a demo document, and it says so at the foot.
_DOC_BODY: dict[str, str] = {
    "要保書": "要保人茲向本公司申請投保下列保險，並確認所填內容均屬真實。",
    "商品說明書": "本說明書載明商品給付項目、除外責任、等待期與費用，詳細內容以保險單條款為準。",
    "保險契約審閱期間確認聲明書": "本人確認已於簽約前取得保險契約條款並有充分審閱期間。",
    "客戶投保權益聲明書": "本人已瞭解投保後之各項權益，包括契約撤銷權、寬限期與停效復效之規定。",
    "健康告知書": "本人就下列告知事項均據實填寫。依保險法第 64 條，故意隱匿或不實說明足以變更或減少本公司對於危險之估計者，本公司得於契約訂立後二年內解除契約，且無須退還所繳保險費。",
    "個人資料告知同意書": "本人同意本公司於保險業務目的範圍內蒐集、處理及利用本人之個人資料。",
    "費率可能調整告知書": "本人已瞭解本保險之保險費得依主管機關核可之費率及被保險人年齡於續保時重新計算。",
    "保費付款授權書": "本人授權本公司依約定方式自指定帳戶或信用卡扣繳保險費。",
    "FATCA 及 CRS 身分聲明書": "本人聲明本人之稅務居住者身分如下，並同意於身分變更時通知本公司。",
    "契約撤銷權告知書": "依保險法施行細則第 4 條，要保人得於收到保險單之翌日起算十日內，以書面通知本公司撤銷契約。",
}


def _esc(value: object) -> str:
    """
    Escape a value for HTML.

    Args:
        value: Anything renderable.

    Returns:
        The text with the five markup characters replaced.

    The display name is chosen by the visitor and the document renderer interpolated it
    raw, so a name containing a tag became markup in a page an operator opens.

    """
    return escape(str(value if value is not None else ""), quote=True)


def _render_contract(row: dict[str, Any], pages: int, viewer: int) -> str:
    """
    Lay out a whole contract as pages the browser can draw.

    Args:
        row: The product with its digest.
        pages: How many pages it has.
        viewer: The member, carried into each image request.

    Returns:
        A standalone HTML page. Images load lazily, so opening a 31-page contract costs
        one render rather than thirty-one, and the reader can jump by page number.

    """
    numbers = "".join(
        f'<a href="#p{n}">{n}</a>' for n in range(1, pages + 1)
    )
    images = "".join(
        f'<figure id="p{n}"><img loading="lazy" alt="第 {n} 頁"'
        f' src="/contract/{_esc(row["product_id"])}/page/{n}?member={viewer}">'
        f"<figcaption>第 {n} 頁 · 共 {pages} 頁</figcaption></figure>"
        for n in range(1, pages + 1)
    )
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(row["name"])}</title>
<style>
 :root {{ color-scheme: light dark; --ink:#0f1a16; --ink-2:#46554f; --ink-3:#5d6e68;
          --paper:#fff; --ground:#eceff0; --rule:#dbe2df; --accent:#0b6b46; }}
 @media (prefers-color-scheme: dark) {{ :root {{ --ink:#e8efeb; --ink-2:#a6b5af; --ink-3:#7d8d87;
          --paper:#151d1a; --ground:#0e1513; --rule:#2a3531; --accent:#3fbd85; }} }}
 * {{ box-sizing:border-box }}
 body {{ margin:0; background:var(--ground); color:var(--ink); font-size:14px; line-height:1.7;
         font-family:"Noto Sans TC","PingFang TC","Microsoft JhengHei",system-ui,sans-serif }}
 header {{ position:sticky; top:0; z-index:5; background:var(--paper);
           border-bottom:1px solid var(--rule); padding:12px 16px }}
 h1 {{ margin:0 0 6px; font-size:16px; font-weight:600 }}
 .meta {{ font-size:12px; color:var(--ink-3) }}
 .meta a {{ color:var(--accent) }}
 nav {{ display:flex; flex-wrap:wrap; gap:4px; margin-top:8px; max-height:64px; overflow-y:auto }}
 nav a {{ font-family:ui-monospace,Menlo,monospace; font-size:11px; text-decoration:none;
          color:var(--ink-2); border:1px solid var(--rule); border-radius:4px; padding:1px 7px }}
 nav a:hover {{ border-color:var(--accent); color:var(--accent) }}
 main {{ max-width:1000px; margin:0 auto; padding:16px }}
 figure {{ margin:0 0 16px; background:var(--paper); border:1px solid var(--rule);
           border-radius:8px; overflow:hidden; scroll-margin-top:110px }}
 img {{ display:block; width:100%; height:auto; background:var(--paper) }}
 figcaption {{ padding:6px 12px; font-size:11.5px; color:var(--ink-3);
               border-top:1px solid var(--rule); font-variant-numeric:tabular-nums }}
</style></head><body>
<header>
  <h1>{_esc(row["name"])}</h1>
  <div class="meta">共 {pages} 頁 · 保險公司公開條款 ·
    <a href="/contract/{_esc(row["product_id"])}?member={viewer}&amp;download=1">下載 PDF</a></div>
  <nav>{numbers}</nav>
</header>
<main>{images}</main>
</body></html>"""


def _render_clause(row: dict[str, Any], clause_id: str, png: bytes | None) -> str:
    """
    Lay out one clause: the page it sits on, and the text as stored.

    Args:
        row: The clause with its product and page.
        clause_id: The id the reply cited.
        png: The rendered page, or None when it could not be produced.

    Returns:
        A standalone HTML page. Self-contained, image inlined as a data URI, because it
        opens in its own tab and a second request for one picture is a second thing that
        can fail.

    """
    kinds = {
        "grant": "給付", "exclusion": "除外責任", "carve_back": "但書",
        "waiting": "等待期", "definition": "定義", "procedure": "程序", "endorsement": "批註",
    }
    picture = (
        f'<img alt="契約第 {row["page"]} 頁，已標示本條" src="data:image/png;base64,{base64.b64encode(png).decode()}">'
        if png
        else '<p class="none">這一頁無法從語料庫的 PDF 還原，以下為資料庫中保存的條文原文。</p>'
    )
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(clause_id)} · {_esc(row["heading"])}</title>
<style>
 :root {{ color-scheme: light dark; --ink:#0f1a16; --ink-2:#46554f; --ink-3:#5d6e68;
          --paper:#fff; --ground:#eceff0; --rule:#dbe2df; --mark:#8a5300; --mark-bg:#fbf1e0; }}
 @media (prefers-color-scheme: dark) {{ :root {{ --ink:#e8efeb; --ink-2:#a6b5af; --ink-3:#7d8d87;
          --paper:#151d1a; --ground:#0e1513; --rule:#2a3531; --mark:#d5a45a; --mark-bg:#33291a; }} }}
 * {{ box-sizing:border-box }}
 body {{ margin:0; background:var(--ground); color:var(--ink); font-size:14px; line-height:1.7;
         font-family:"Noto Sans TC","PingFang TC","Microsoft JhengHei",system-ui,sans-serif; }}
 main {{ max-width:900px; margin:0 auto; padding:24px 16px 64px; }}
 header {{ margin-bottom:16px }}
 h1 {{ margin:0 0 4px; font-size:20px; font-weight:600 }}
 .id {{ font-family:ui-monospace,Menlo,monospace; font-size:12px; color:var(--ink-3) }}
 .meta {{ font-size:12.5px; color:var(--ink-2) }}
 .tag {{ display:inline-block; background:var(--mark-bg); color:var(--mark); border-radius:999px;
         padding:2px 9px; font-size:11px; font-weight:600; margin-left:6px }}
 figure {{ margin:0 0 20px; background:var(--paper); border:1px solid var(--rule); border-radius:8px;
           overflow:hidden }}
 img {{ display:block; width:100%; height:auto }}
 figcaption {{ padding:8px 12px; font-size:12px; color:var(--ink-3); border-top:1px solid var(--rule) }}
 .body {{ background:var(--paper); border:1px solid var(--rule); border-radius:8px; padding:16px 18px;
          white-space:pre-wrap; overflow-wrap:anywhere }}
 .body h2 {{ margin:0 0 8px; font-size:11px; letter-spacing:.13em; text-transform:uppercase;
             color:var(--ink-3); font-weight:700 }}
 .none {{ color:var(--ink-3); padding:16px }}
</style></head><body><main>
<header>
  <h1>{_esc(row["heading"])}<span class="tag">{kinds.get(row["kind"], _esc(row["kind"]))}</span></h1>
  <div class="id">{_esc(clause_id)}</div>
  <div class="meta">{_esc(row["product_name"])} · 第 {row["page"]} 頁</div>
</header>
<figure>{picture}<figcaption>黃色標示為本條在契約頁面上的位置，取自保險公司公開的條款 PDF。</figcaption></figure>
<div class="body"><h2>條文原文</h2>{_esc(row["verbatim"])}</div>
</main></body></html>"""


def _render_document(row: dict[str, Any]) -> str:
    """
    Render one signing document.

    Args:
        row: The document joined to its case and member.

    Returns:
        A printable HTML page.

    """
    kind = row["kind"]
    address = f"{_esc(row['address_city'])}{_esc(row['address_district'])}{_esc(row['address_rest'])}"
    history = "、".join(row["medical_history"] or []) or "無"
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<title>{kind}</title><style>{_DOC_STYLE}</style></head><body>
<h1>{kind}</h1>
<div class="meta">案號 #{_esc(row["case_id"])} · 文件編號 {_esc(row["document_id"])} · 文件雜湊 {_esc(row["sha"])}</div>
<p>{_DOC_BODY.get(kind, "")}</p>
<dl>
  <dt>要保人／被保險人</dt><dd>{_esc(row["display_name"])}</dd>
  <dt>身分證字號</dt><dd>{_esc(row["national_id"])}</dd>
  <dt>出生年月日</dt><dd>{_esc(row["birth_date"])}</dd>
  <dt>職業</dt><dd>{_esc(row["occupation"])}（第 {_esc(row["occupation_class"])} 類）</dd>
  <dt>通訊地址</dt><dd>{_esc(address)}</dd>
  <dt>聯絡電話</dt><dd>{_esc(row["phone"])}</dd>
  <dt>電子郵件</dt><dd>{_esc(row["email"])}</dd>
  <dt>婚姻狀況</dt><dd>{_esc(row["marital_status"])}</dd>
  <dt>既往症告知</dt><dd>{_esc(history)}</dd>
  <dt>受益人關係</dt><dd>{_esc(row["beneficiary_relation"])}</dd>
  <dt>招攬業務員</dt><dd>{row["adviser_name"] or "—"}（登錄字號 {row["adviser_licence"] or "—"}）</dd>
</dl>
<div class="sign">
  要保人簽章：<span class="line"></span><br><br>
  被保險人簽章：<span class="line"></span><br><br>
  中華民國 <span class="line" style="width:60px"></span> 年
  <span class="line" style="width:40px"></span> 月
  <span class="line" style="width:40px"></span> 日
</div>
<div class="note">
  本文件為 policydesk 示範系統產生，非真實保險文件，不具法律效力。
  簽署後請以「上傳簽署本」回傳，系統將以本文件雜湊 {_esc(row["sha"])} 綁定該次簽署。
</div>
</body></html>"""


if __name__ == "__main__":
    main()
