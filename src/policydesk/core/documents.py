"""
Current-version signing records shared by commands and customer-facing reads.

Version matching validates the demo's records, not file bytes or digital signatures.
"""

from enum import StrEnum
from typing import TYPE_CHECKING, Any

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


SIGNING_PARTIES: tuple[str, ...] = ("要保人", "被保險人")
"""Both roles sign, even when one person fills both roles in the demo."""


async def signing_documents(db: Database, case_id: int) -> list[dict[str, Any]]:
    """Read only this case; stale timestamps never establish a current signature."""
    return await db.fetch(
        """SELECT d.document_id, d.kind, d.title, d.sha, d.uploaded_name,
                  CASE WHEN signed.parties = cardinality($2::text[]) THEN d.signed_at END AS signed_at,
                  signed.parties AS signed_parties
           FROM case_document d
           CROSS JOIN LATERAL (
               SELECT count(DISTINCT g.scope) AS parties FROM authorization_grant g
               WHERE g.case_id = d.case_id AND g.stage = 'signed'
                 AND g.document_sha = d.sha AND d.sha <> ''
                 AND g.scope IN (
                     SELECT party || ' 簽署文件 ' || d.document_id::text
                     FROM unnest($2::text[]) AS expected(party)
                 )
           ) signed
           WHERE d.case_id = $1::bigint ORDER BY d.document_id""",
        [case_id, list(SIGNING_PARTIES)],
    )


def document_status(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Count required but unissued forms as pending, without inventing document records."""
    present = {row["kind"] for row in documents}
    unissued = tuple(kind.value for kind in ENROLMENT_DOCUMENTS if kind.value not in present)
    unsigned = tuple(row["title"] for row in documents if row["signed_at"] is None)
    return {
        "signed": len(documents) - len(unsigned), "total": len(documents) + len(unissued),
        "pending": len(unissued) + len(unsigned), "unissued": unissued, "missing": unissued + unsigned,
    }
