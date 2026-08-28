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
from enum import StrEnum
from typing import TYPE_CHECKING

from msgspec import Struct

from policydesk.bootloader import logger
from policydesk.core.models import Stage, may_advance

if TYPE_CHECKING:
    from policydesk.core.db import Database


class DocumentKind(StrEnum):
    """
    The documents a Taiwanese life application actually produces.

    Sourced from what insurers list on their own forms pages, not invented. The set is
    closed: a document kind the flow does not know about cannot be signed, and an
    unsigned unknown document would otherwise sit forever in the completeness check.
    """

    APPLICATION = "要保書"
    """Carries the applicant, the insured, the beneficiary and the health declaration."""
    PRODUCT_BRIEF = "商品說明書"
    REVIEW_PERIOD = "保險契約審閱期間確認聲明書"
    """Confirms the applicant was given the contract to read before signing."""
    RIGHTS_NOTICE = "客戶投保權益聲明書"
    HEALTH_DECLARATION = "健康告知書"
    """保險法 §64 attaches here."""
    PRIVACY_CONSENT = "個人資料告知同意書"
    RATE_ADJUSTMENT = "費率可能調整告知書"
    PAYMENT_MANDATE = "保費付款授權書"
    TAX_STATUS = "FATCA 及 CRS 身分聲明書"
    CANCELLATION_NOTICE = "契約撤銷權告知書"
    """十日撤銷權, counted from the day after delivery."""


# Everything an enrolment must have signed before it can go to a human. Ordered as an
# applicant meets them, because the customer pane lists them in this order.
ENROLMENT_DOCUMENTS: tuple[DocumentKind, ...] = (
    DocumentKind.PRODUCT_BRIEF,
    DocumentKind.REVIEW_PERIOD,
    DocumentKind.APPLICATION,
    DocumentKind.HEALTH_DECLARATION,
    DocumentKind.RIGHTS_NOTICE,
    DocumentKind.PRIVACY_CONSENT,
    DocumentKind.RATE_ADJUSTMENT,
    DocumentKind.PAYMENT_MANDATE,
    DocumentKind.TAX_STATUS,
    DocumentKind.CANCELLATION_NOTICE,
)

# 要保人 and 被保險人 must each sign personally. In this demo one person is both, so
# the pair is recorded rather than collapsed — an application signed by one party on
# the other's behalf is the defect this records the shape of.
SIGNING_PARTIES: tuple[str, ...] = ("要保人", "被保險人")


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
    logger.info("case_moved", case_id=case_id, stage=stage.value, version=version, actor=actor)
    return Applied(case_id=case_id, stage=stage, case_version=version)


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
           ORDER BY case_version DESC, case_id DESC LIMIT 1""",
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


async def record_signature(db: Database, case_id: int, *, document_id: int, party: str, document_sha: str) -> Outcome:
    """
    Record one signature on one document.

    Args:
        db: The database.
        case_id: The case.
        document_id: Which document was signed.
        party: 要保人 or 被保險人.
        document_sha: The bytes that were signed.

    Returns:
        Applied when this signature completes the set and the case moves to SIGNED;
        otherwise a Refusal naming what is still outstanding.

    A signature binds to the document's hash. Amending a document after it was signed
    changes the hash, which invalidates the signature rather than silently replacing it.

    """
    if party not in SIGNING_PARTIES:
        return Refusal(reason=f"{party} 非要保人或被保險人，不得代簽")

    # Ownership first. Without this the UPDATE below silently affects no row for a
    # document belonging to another case, and the grant is written anyway — an
    # authorisation record pointing at a document the case does not contain.
    owner = await db.fetch_val(
        "SELECT case_id FROM case_document WHERE document_id = $1::bigint", [document_id]
    )
    if owner is None:
        return Refusal(reason="查無此文件")
    if int(owner) != int(case_id):
        return Refusal(reason="該文件不屬於本案件，不得簽署")

    await db.execute(
        """INSERT INTO authorization_grant (case_id, stage, scope, document_sha)
           VALUES ($1::bigint,$2::text,$3::text,$4::text)""",
        [case_id, Stage.SIGNED.value, f"{party} 簽署文件 {document_id}", document_sha],
    )

    # 要保人 and 被保險人 must each sign personally or the contract may be void, so a
    # document counts as signed only once both grants exist. Marking it on the first
    # signature let a case reach SIGNED on one party's, with the other's absence
    # visible nowhere: the party was recorded only inside a grant's scope text, which
    # nothing read.
    signed_parties = await db.fetch_val(
        """SELECT count(DISTINCT split_part(scope, ' ', 1)) FROM authorization_grant
           WHERE case_id = $1::bigint AND scope LIKE '%' || $2::text""",
        [case_id, f"簽署文件 {document_id}"],
    )
    if int(signed_parties or 0) >= len(SIGNING_PARTIES):
        await db.execute(
            "UPDATE case_document SET signed_at = now() WHERE document_id = $1::bigint AND case_id = $2::bigint",
            [document_id, case_id],
        )

    outstanding = await db.fetch(
        "SELECT kind FROM case_document WHERE case_id = $1::bigint AND signed_at IS NULL",
        [case_id],
    )
    if outstanding:
        return Refusal(reason="尚有文件未經要保人及被保險人雙方簽署", missing=tuple(r["kind"] for r in outstanding))

    case = await db.fetch_one('SELECT stage FROM "case" WHERE case_id = $1::bigint', [case_id])
    if case is None or not may_advance(Stage(case["stage"]), Stage.SIGNED):
        return Refusal(reason="案件狀態不允許進入已簽署")
    return await _bump(db, case_id, Stage.SIGNED, "customer", "documents_signed", {"party": party})


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

    unsigned = await db.fetch(
        "SELECT kind FROM case_document WHERE case_id = $1::bigint AND signed_at IS NULL", [case_id]
    )
    missing.extend(r["kind"] for r in unsigned)

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

    case["documents"] = await db.fetch(
        "SELECT document_id, kind, title, signed_at, uploaded_name FROM case_document WHERE case_id = $1::bigint ORDER BY document_id",
        [case_id],
    )
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
                  po.main_policy_ref, pr.name AS product_name, pr.line,
                  round(coalesce(ce.unit_premium, 0) * po.sum_insured / 1000.0) AS annual_premium
           FROM policy po
           JOIN product pr USING (product_id)
           LEFT JOIN catalog_entry ce USING (product_id)
           WHERE po.member_id = $1::bigint
           ORDER BY po.main_policy_ref NULLS FIRST, po.policy_id""",
        [case["member_id"]],
    )
    case["generated_at"] = datetime.now(UTC).isoformat()
    return case
