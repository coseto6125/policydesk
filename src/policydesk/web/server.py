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
import contextlib
import os
import secrets
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from msgspec import DecodeError, json
from sanic import Request, Sanic, Websocket, html, response

from policydesk.agent import memory
from policydesk.agent.executor import run_turn
from policydesk.bootloader import logger
from policydesk.core import commands as cmd
from policydesk.core.db import Database
from policydesk.gov.identity import Sex, verify
from policydesk.llm.provider import build_provider
from policydesk.synthetic.alias import mint
from policydesk.synthetic.person import generate, insurance_age, occupation_catalogue
from policydesk.synthetic.portfolio import enrol
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

CORPUS = Path(os.environ.get("POLICYDESK_CORPUS", "data/cathay"))
"""Where the insurer's own PDFs live, named by digest. The desk serves a contract from
here so a figure quoted in the chat can be checked against the page it came from."""

app = Sanic("policydesk")
app.ctx.registry = Registry()
app.ctx.desk_sockets = set()


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
                            "national_id": existing["national_id"],
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
                    member_id = await enrol(person, db)
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
                    }).decode())
                    await _push_case(db, ws, request.app, case_id)

                case "say" if case_id is not None:
                    text = (message.get("text") or "").strip()
                    if not text:
                        continue
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

                    member_id = await db.fetch_val(
                        'SELECT member_id FROM "case" WHERE case_id = $1::bigint', [case_id]
                    )
                    turn = await run_turn(
                        request.app.ctx.provider, db, case_id=case_id, member_id=member_id, text=text
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
                        # What the router carried over from earlier turns. Shown in the
                        # chat so a reader can see the desk answered from the whole
                        # conversation rather than from the last sentence.
                        "params": turn.params,
                        "quick": list(turn.quick_replies),
                        "citations": list(turn.citations),
                        "faults": list(turn.faults),
                    }).decode())
                    await _push_case(db, ws, request.app, case_id)

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


@app.get("/contract/<product_id:str>")
async def contract(request: Request, product_id: str):
    """
    Serve the contract behind a policy the customer holds.

    Args:
        request: The request, carrying the desk token and the viewing member.
        product_id: Which product's contract.

    Returns:
        The insurer's own PDF, or a refusal.

    The file is the one the corpus was built from, named by its digest. Serving it means
    a figure quoted in the chat can be checked against the page it came from, which is
    the difference between a citation and a claim.

    """
    if request.args.get("token", "") != DESK_TOKEN:
        return response.text("需要授權", status=403)
    try:
        viewer = int(request.args.get("member", ""))
    except (TypeError, ValueError):
        return response.text("需指定保戶", status=403)

    # Held, not merely known: the product catalogue is public, but which contract this
    # visitor may open is decided by the policies in their own name.
    row = await request.app.ctx.db.fetch_one(
        """SELECT p.doc_sha, p.name FROM product p
           WHERE p.product_id = $1::text
             AND EXISTS (SELECT 1 FROM policy po WHERE po.product_id = p.product_id AND po.member_id = $2::bigint)""",
        [product_id, viewer],
    )
    if row is None:
        logger.warning("contract_out_of_scope", product_id=product_id, viewer=viewer)
        return response.text("查無您名下的這張契約", status=404)

    path = CORPUS / f"{row['doc_sha']}.pdf"
    if not path.is_file():
        return response.text("契約條款檔案不在本機語料庫中", status=404)
    return await response.file(
        path, mime_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{row["doc_sha"]}.pdf"'}
    )


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
    """Run the desk."""
    app.run(host="0.0.0.0", port=8100, single_process=True, access_log=False)  # noqa: S104


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


def _render_document(row: dict[str, Any]) -> str:
    """
    Render one signing document.

    Args:
        row: The document joined to its case and member.

    Returns:
        A printable HTML page.

    """
    kind = row["kind"]
    address = f"{row['address_city']}{row['address_district']}{row['address_rest']}"
    history = "、".join(row["medical_history"] or []) or "無"
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<title>{kind}</title><style>{_DOC_STYLE}</style></head><body>
<h1>{kind}</h1>
<div class="meta">案號 #{row["case_id"]} · 文件編號 {row["document_id"]} · 文件雜湊 {row["sha"]}</div>
<p>{_DOC_BODY.get(kind, "")}</p>
<dl>
  <dt>要保人／被保險人</dt><dd>{row["display_name"]}</dd>
  <dt>身分證字號</dt><dd>{row["national_id"]}</dd>
  <dt>出生年月日</dt><dd>{row["birth_date"]}</dd>
  <dt>職業</dt><dd>{row["occupation"]}（第 {row["occupation_class"]} 類）</dd>
  <dt>通訊地址</dt><dd>{address}</dd>
  <dt>聯絡電話</dt><dd>{row["phone"]}</dd>
  <dt>電子郵件</dt><dd>{row["email"]}</dd>
  <dt>婚姻狀況</dt><dd>{row["marital_status"]}</dd>
  <dt>既往症告知</dt><dd>{history}</dd>
  <dt>受益人關係</dt><dd>{row["beneficiary_relation"]}</dd>
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
  簽署後請以「上傳簽署本」回傳，系統將以本文件雜湊 {row["sha"]} 綁定該次簽署。
</div>
</body></html>"""


if __name__ == "__main__":
    main()
