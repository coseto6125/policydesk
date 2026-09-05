"""
The only thing that writes a case.

Both panes send commands here and both render what comes back. Neither writes the
database directly, and that is not fastidiousness: two writers over one case means two
implementations of the stage rules, an upload and a signature that cannot be one atomic
step, and an audit trail that cannot answer "who, under which authorisation, moved this
case". The panes are two renderings, not two applications.

Every command does the same four things in the same order: check the preconditions,
write the change, record an audit event, bump `case_version`. A command that refuses
returns a `Refusal` rather than raising, because a refused precondition is an ordinary
outcome the customer needs to read — not an error.

`may_advance` in models.py checks ordering only. The preconditions live here, because
they need the case and not just two stage names.

Three of them are statute rather than practice, and are marked in the code:

- **要保人與被保險人均須親自簽署**, or the contract may be void. Both signatures, no
  substitutes.
- **保險法 §64 告知義務** — a health declaration that is untrue lets the insurer rescind
  within two years and keep the premium. So the declaration is collected and stored,
  and the desk never rules on it.
- **契約撤銷權** (保險法施行細則 §4) — ten days from the day after the policy is
  received. The clock starts at delivery, so delivery is recorded with its date.
"""

from datetime import UTC, datetime
from functools import wraps
from typing import TYPE_CHECKING

from msgspec import Struct

from policydesk.agent.tools import insured_amount
from policydesk.bootloader import logger
from policydesk.core import documents
from policydesk.core.documents import ENROLMENT_DOCUMENTS, SIGNING_PARTIES, document_status, signing_documents
from policydesk.core.models import Stage, may_advance

if TYPE_CHECKING:
    from policydesk.core.db import Database


DocumentKind = documents.DocumentKind

class Refusal(Struct, frozen=True):
    """A command that could not run, and the reason a customer can read."""

    reason: str
    missing: tuple[str, ...] = ()


class Applied(Struct, frozen=True):
    """A command that ran."""

    case_id: int
    stage: Stage
    case_version: int


Outcome = Applied | Refusal


def _atomic_case(operation):
    """
    Serialize each case before checking it; document locks follow in ID order.

    Refusal can report persisted partial signatures or an unsuccessful identity check.
    Rollback follows exceptions, not the outcome type. True refusals precede writes.
    """
    @wraps(operation)
    async def run(db: Database, case_id: int, *args, **kwargs):
        async with db.transaction() as session:
            await session.fetch_one('SELECT case_id FROM "case" WHERE case_id = $1::bigint FOR UPDATE', [case_id])
            await session.fetch(
                "SELECT document_id FROM case_document WHERE case_id = $1::bigint ORDER BY document_id FOR UPDATE",
                [case_id],
            )
            outcome = await operation(session, case_id, *args, **kwargs)
        if isinstance(outcome, Applied):
            logger.info("case_moved", case_id=case_id, stage=outcome.stage.value, version=outcome.case_version)
        return outcome

    return run


def _atomic_member(operation):
    """Lock the member before looking for a case: a missing case has no row to lock."""
    @wraps(operation)
    async def run(db: Database, member_id: int, *args, **kwargs):
        async with db.transaction() as session:
            await session.fetch_one("SELECT member_id FROM member WHERE member_id = $1::bigint FOR UPDATE", [member_id])
            return await operation(session, member_id, *args, **kwargs)

    return run


async def _bump(db: Database, case_id: int, stage: Stage, actor: str, action: str, detail: dict) -> Applied:
    """
    Apply a stage change, record it, and return the new version.

    Args:
        db: The database.
        case_id: The case being moved.
        stage: Where it now is.
        actor: Who moved it.
        action: What they did.
        detail: Anything worth keeping alongside the event.

    Returns:
        The case's new stage and version.

    """
    version = await db.fetch_val(
        """UPDATE "case" SET stage = $2::text, case_version = case_version + 1, updated_at = now()
           WHERE case_id = $1::bigint RETURNING case_version""",
        [case_id, stage.value],
    )
    await db.execute(
        """INSERT INTO audit_event (case_id, actor, action, detail, case_version)
           VALUES ($1::bigint,$2::text,$3::text,$4::jsonb,$5::int)""",
        # psqlpy binds jsonb from a dict. An encoded string raises PyToRustValueMappingError.
        [case_id, actor, action, detail, version],
    )
    return Applied(case_id=case_id, stage=stage, case_version=version)


@_atomic_member
async def open_case(db: Database, member_id: int, kind: str = "enrolment") -> Applied:
    """
    Start a case for a member, or return the one already open.

    Args:
        db: The database.
        member_id: Who the case belongs to.
        kind: enrolment, claim or service.

    Returns:
        The member's live case of this kind, opening one at INQUIRY when none is live.

    A customer who reconnects is the same customer mid-application, not a new
    applicant. Opening a case per connection filled the queue with a row per reload,
    each one holding the signatures and identity checks the previous row had already
    collected, and an underwriter could not tell which of them was the real case.
    A case stays live until it is decided, and the furthest-advanced one wins: an
    application already at 人工審核 outranks a fresh enquiry the same person opened.
    """
    live = await db.fetch_one(
        """SELECT case_id, stage, case_version FROM "case"
           WHERE member_id = $1::bigint AND kind = $2::text AND stage NOT IN ('approved','rejected')
           ORDER BY case_version DESC, case_id DESC LIMIT 1 FOR UPDATE""",
        [member_id, kind],
    )
    if live is not None:
        logger.info("case_resumed", case_id=live["case_id"], member_id=member_id, stage=live["stage"])
        return Applied(case_id=live["case_id"], stage=Stage(live["stage"]), case_version=live["case_version"])

    case_id = await db.fetch_val(
        """INSERT INTO "case" (member_id, kind, stage) VALUES ($1::bigint,$2::text,$3::text) RETURNING case_id""",
        [member_id, kind, Stage.INQUIRY.value],
    )
    await db.execute(
        """INSERT INTO audit_event (case_id, actor, action, detail, case_version)
           VALUES ($1::bigint,'agent','case_opened',$2::jsonb,1)""",
        [case_id, {"kind": kind}],
    )
    logger.info("case_opened", case_id=case_id, member_id=member_id, kind=kind)
    return Applied(case_id=case_id, stage=Stage.INQUIRY, case_version=1)


@_atomic_case
async def propose(db: Database, case_id: int, *, product_ids: list[str], adviser: str, licence: str) -> Outcome:
    """
    Record a recommendation, under the adviser who answers for it.

    Args:
        db: The database.
        case_id: The case.
        product_ids: What is being recommended.
        adviser: The registered adviser's name.
        licence: Their 登錄字號.

    Returns:
        The case at PROPOSED, or a refusal.

    A recommendation is 招攬, and 招攬 requires a registered individual, so a proposal
    without a licence number is refused here rather than accepted and flagged later.

    """
    case = await db.fetch_one('SELECT stage FROM "case" WHERE case_id = $1::bigint', [case_id])
    if case is None:
        return Refusal(reason="查無此案件")
    if not may_advance(Stage(case["stage"]), Stage.PROPOSED):
        return Refusal(reason=f"案件目前為 {case['stage']}，不可提出方案")
    if not product_ids:
        return Refusal(reason="未選定任何商品")
    if not adviser.strip() or not licence.strip():
        return Refusal(reason="推介屬招攬行為，須由登錄業務員具名負責", missing=("adviser", "licence"))

    await db.execute(
        'UPDATE "case" SET adviser_name = $2::text, adviser_licence = $3::text WHERE case_id = $1::bigint',
        [case_id, adviser, licence],
    )
    return await _bump(
        db, case_id, Stage.PROPOSED, "agent", "proposed",
        {"products": product_ids, "adviser": adviser, "licence": licence},
    )


@_atomic_case
async def issue_documents(db: Database, case_id: int) -> Outcome:
    """
    Put the signing set in front of the applicant.

    Args:
        db: The database.
        case_id: The case.

    Returns:
        The case at ISSUED, or a refusal.

    """
    case = await db.fetch_one('SELECT stage FROM "case" WHERE case_id = $1::bigint', [case_id])
    if case is None:
        return Refusal(reason="查無此案件")
    if not may_advance(Stage(case["stage"]), Stage.ISSUED):
        return Refusal(reason=f"案件目前為 {case['stage']}，不可交付文件")

    await db.execute_many(
        """INSERT INTO case_document (case_id, kind, title, sha)
           VALUES ($1::bigint,$2::text,$3::text,$4::text)""",
        [(case_id, kind.value, kind.value, f"{case_id}-{kind.name}") for kind in ENROLMENT_DOCUMENTS],
    )
    return await _bump(
        db, case_id, Stage.ISSUED, "agent", "documents_issued",
        {"documents": [k.value for k in ENROLMENT_DOCUMENTS]},
    )


@_atomic_case
async def record_signature(db: Database, case_id: int, *, document_id: int, party: str, document_sha: str) -> Outcome:
    """
    Record one signature on one document.

    Args:
        db: The database.
        case_id: The case.
        document_id: Which document was signed.
        party: 要保人 or 被保險人.
        document_sha: The version identifier recorded for the signed document.

    Returns:
        Applied when this signature completes the set and the case moves to SIGNED;
        otherwise a Refusal naming what is still outstanding.

    A grant must match the current document version. Both roles must sign that version.
    These checks validate demo records; they do not verify uploaded bytes or cryptographic signatures.

    """
    if party not in SIGNING_PARTIES:
        return Refusal(reason=f"{party} 非要保人或被保險人，不得代簽")

    return await _record_signatures(db, case_id, document_id=document_id, parties=(party,), document_sha=document_sha)


@_atomic_case
async def upload_document(
    db: Database, case_id: int, *, document_id: int, filename: str = "", sample: str | None = None,
) -> Outcome:
    """
    Record the verified session's demo upload and both roles atomically, without writing file bytes.

    The caller supplies the case from the confirmed session, never from the message.
    A filename is display metadata, not a filesystem path or proof of a real signature.
    Fixed samples exercise matching and mismatched document kinds by demo rules only.
    They do not call a local model or inspect a real file.
    """
    if sample is not None and sample not in ("matching", "mismatched"):
        return Refusal(reason="請選擇有效的示範文件")
    if sample is None:
        filename = filename.strip()
        if not filename or len(filename) > 255:
            return Refusal(reason="請提供 1 至 255 字元的文件名稱")
    document = await db.fetch_one(
        "SELECT sha, kind FROM case_document WHERE case_id = $1::bigint AND document_id = $2::bigint",
        [case_id, document_id],
    )
    if document is None:
        return Refusal(reason="無法處理這份文件。")
    if sample is not None:
        stage = await db.fetch_val('SELECT stage FROM "case" WHERE case_id = $1::bigint', [case_id])
        if stage != Stage.ISSUED.value:
            return Refusal(reason="案件狀態不允許進行簽署")
        if sample == "mismatched":
            return Refusal(reason=f"示範規則檢查：所選樣本是空白便條紙，不是本欄需要的「{document['kind']}」，未記錄模擬簽署。")
        filename = f"示範-{document['kind']}.pdf"
    return await _record_signatures(
        db, case_id, document_id=document_id, parties=SIGNING_PARTIES,
        document_sha=document["sha"] or "", filename=filename,
    )


async def _record_signatures(
    db: Database, case_id: int, *, document_id: int, parties: tuple[str, ...],
    document_sha: str, filename: str | None = None,
) -> Outcome:
    """Run inside the caller's case transaction; validate everything before the first write."""
    # Ownership first. Without this the UPDATE below silently affects no row for a
    # document belonging to another case, and the grant is written anyway — an
    # authorisation record pointing at a document the case does not contain.
    document = await db.fetch_one(
        "SELECT case_id, sha FROM case_document WHERE document_id = $1::bigint", [document_id]
    )
    if document is None:
        return Refusal(reason="查無此文件")
    if int(document["case_id"]) != int(case_id):
        return Refusal(reason="該文件不屬於本案件，不得簽署")
    if not document["sha"] or document_sha != document["sha"]:
        return Refusal(reason="簽署的文件版本與目前文件不符，請重新確認文件")
    case = await db.fetch_one('SELECT stage FROM "case" WHERE case_id = $1::bigint', [case_id])
    if case is None or not may_advance(Stage(case["stage"]), Stage.SIGNED):
        return Refusal(reason="案件狀態不允許進行簽署")

    if filename is not None:
        await db.execute(
            "UPDATE case_document SET uploaded_name = $2::text WHERE document_id = $1::bigint AND case_id = $3::bigint",
            [document_id, filename, case_id],
        )
    await db.execute_many(
        """INSERT INTO authorization_grant (case_id, stage, scope, document_sha, provider)
           VALUES ($1::bigint,$2::text,$3::text,$4::text,'mock')""",
        [(case_id, Stage.SIGNED.value, f"{party} 簽署文件 {document_id}", document_sha) for party in parties],
    )

    documents = await signing_documents(db, case_id)
    current = next(row for row in documents if row["document_id"] == document_id)
    if current["signed_parties"] == len(SIGNING_PARTIES):
        await db.execute(
            """UPDATE case_document SET signed_at = now()
               WHERE document_id = $1::bigint AND case_id = $2::bigint AND sha = $3::text""",
            [document_id, case_id, document_sha],
        )

    outstanding = document_status(await signing_documents(db, case_id))["missing"]
    if outstanding:
        return Refusal(reason="尚有文件未經要保人及被保險人雙方簽署", missing=outstanding)

    detail = {"party": parties[0]} if len(parties) == 1 else {"parties": list(parties)}
    return await _bump(db, case_id, Stage.SIGNED, "customer", "documents_signed", detail)


@_atomic_case
async def verify_identity(db: Database, case_id: int, *, national_id: str, verified: bool, reason: str, latency_ms: int) -> Outcome:
    """
    Record an identity check and advance if it passed.

    Args:
        db: The database.
        case_id: The case.
        national_id: What was checked.
        verified: The provider's answer.
        reason: Why, when it refused.
        latency_ms: How long the provider took.

    Returns:
        The case at VERIFIED, or a refusal carrying the provider's reason.

    Every attempt is recorded, including refusals. A verification path whose failures
    leave no trace cannot be audited.

    """
    case = await db.fetch_one(
        """SELECT c.stage, m.national_id AS member_national_id
           FROM "case" c JOIN member m USING (member_id) WHERE c.case_id = $1::bigint""",
        [case_id],
    )
    if case is None:
        return Refusal(reason="查無此案件")

    # The stage gate runs before the row is written. Writing first put verified=true
    # rows on cases still at INQUIRY, and submit_for_review reads bool_or(verified) —
    # so a check taken before any document existed satisfied the identity leg forever.
    if not may_advance(Stage(case["stage"]), Stage.VERIFIED):
        return Refusal(reason="案件尚未完成簽署，不可進行身分驗證")

    # A well-formed number is not the case owner's number. The provider answers
    # "is this a valid identity"; only this system knows whose case it is, and an
    # acceptance run passed two strangers' IDs on someone else's case because nothing
    # here compared them.
    if verified and national_id != case["member_national_id"]:
        verified, reason = False, "所輸入身分證字號與本案要保人不符"

    await db.execute(
        """INSERT INTO identity_check (case_id, national_id, verified, reason, latency_ms)
           VALUES ($1::bigint,$2::text,$3::bool,$4::text,$5::int)""",
        [case_id, national_id, verified, reason, latency_ms],
    )
    if not verified:
        return Refusal(reason=reason or "身分驗證未通過")

    return await _bump(db, case_id, Stage.VERIFIED, "gov:mock", "identity_verified", {"national_id": national_id})


@_atomic_case
async def submit_for_review(db: Database, case_id: int) -> Outcome:
    """
    Hand a complete file to a human.

    Args:
        db: The database.
        case_id: The case.

    Returns:
        The case at REVIEW, or a refusal listing what is missing.

    The desk decides whether the file is complete. It does not decide whether the
    application is accepted, and nothing in this function looks at the answer.

    """
    case = await db.fetch_one('SELECT stage, adviser_licence FROM "case" WHERE case_id = $1::bigint', [case_id])
    if case is None:
        return Refusal(reason="查無此案件")

    missing: list[str] = []
    if not case["adviser_licence"]:
        missing.append("責任業務員登錄字號")

    missing.extend(document_status(await signing_documents(db, case_id))["missing"])

    verified = await db.fetch_val(
        "SELECT bool_or(verified) FROM identity_check WHERE case_id = $1::bigint", [case_id]
    )
    if not verified:
        missing.append("身分驗證")

    if missing:
        return Refusal(reason="件不齊，尚未送審", missing=tuple(missing))
    if not may_advance(Stage(case["stage"]), Stage.REVIEW):
        return Refusal(reason=f"案件目前為 {case['stage']}，不可送審")

    return await _bump(db, case_id, Stage.REVIEW, "agent", "submitted_for_review", {})


@_atomic_case
async def decide(db: Database, case_id: int, *, approved: bool, reason: str, by: str) -> Outcome:
    """
    Record a caseworker's decision.

    Args:
        db: The database.
        case_id: The case.
        approved: Their decision.
        reason: Required on a rejection, shown to the customer verbatim.
        by: Who decided.

    Returns:
        The decided case, or a refusal.

    """
    if not approved and not reason.strip():
        return Refusal(reason="退件須填具理由，否則保戶只會看到空白")

    case = await db.fetch_one('SELECT stage FROM "case" WHERE case_id = $1::bigint', [case_id])
    if case is None:
        return Refusal(reason="查無此案件")

    target = Stage.APPROVED if approved else Stage.REJECTED
    if not may_advance(Stage(case["stage"]), target):
        return Refusal(reason="僅審核中的案件可作成決定")

    await db.execute(
        'UPDATE "case" SET decided_by = $2::text, decision_reason = $3::text WHERE case_id = $1::bigint',
        [case_id, by, reason],
    )
    return await _bump(db, case_id, target, by, "decided", {"approved": approved, "reason": reason})


@_atomic_case
async def snapshot(db: Database, case_id: int) -> dict | None:
    """
    Read the case as both panes render it.

    Args:
        db: The database.
        case_id: The case.

    Returns:
        The case with its documents, checks and outstanding items, or None.

    Sent whole on every change rather than as a patch. A case is a few kilobytes, and a
    pane that drifts because it missed one patch is worse than one that resends.

    """
    case = await db.fetch_one(
        """SELECT c.case_id, c.member_id, c.kind, c.stage, c.case_version, c.adviser_name, c.adviser_licence,
                  c.decided_by, c.decision_reason, m.display_name, m.national_id, m.occupation, m.occupation_class
           FROM "case" c JOIN member m USING (member_id) WHERE c.case_id = $1::bigint""",
        [case_id],
    )
    if case is None:
        return None

    case["documents"] = await signing_documents(db, case_id)
    case["document_status"] = document_status(case["documents"])
    case["identity_checks"] = await db.fetch(
        "SELECT verified, reason, latency_ms, checked_at FROM identity_check WHERE case_id = $1::bigint ORDER BY check_id",
        [case_id],
    )
    case["audit"] = await db.fetch(
        "SELECT actor, action, detail, case_version, created_at FROM audit_event WHERE case_id = $1::bigint ORDER BY event_id",
        [case_id],
    )
    # The member's own book, scoped by member_id like every other read here. The agent
    # tells a customer 各張保單明細請見左側後台的保單清單, and until this was here that
    # sentence pointed at a panel the back office did not have — the figure it quoted
    # was true and there was nowhere to check it.
    case["policies"] = await db.fetch(
        """SELECT po.policy_id, po.policy_number, po.product_id, po.sum_insured, po.effective_at, po.lapsed_at,
                  po.main_policy_id, main.policy_number AS main_policy_number,
                  pr.name AS product_name, pr.line, ce.unit_label,
                  round(coalesce(ce.unit_premium, 0) * po.sum_insured / 1000.0) AS annual_premium
           FROM policy po
           JOIN product pr USING (product_id)
           LEFT JOIN catalog_entry ce USING (product_id)
           LEFT JOIN policy main ON main.policy_id = po.main_policy_id
           WHERE po.member_id = $1::bigint
           ORDER BY po.main_policy_id NULLS FIRST, po.policy_id""",
        [case["member_id"]],
    )
    # Rendered here for the same reason `list_policies` renders it: `sum_insured` counts
    # thousandths of one `unit_label` unit, so 1000 against 每 100 萬元保額 is 100 萬元,
    # and a back office printing the raw count shows a figure a thousand times too small
    # beside a premium that is right. The desk's own renderer, not a second one.
    for policy in case["policies"]:
        policy["insured"] = insured_amount(policy["sum_insured"], policy["unit_label"])
    case["generated_at"] = datetime.now(UTC).isoformat()
    return case
