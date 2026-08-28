"""
Domain models.

The shape of `Money` is the load-bearing decision in this file. Every figure the desk
shows a user carries the evidence that produced it, because a figure without a citation
is the failure mode this product exists to remove: a tool that is reachable is not a
tool that is correct, and an insurance figure nobody can trace is worse than no figure.

There is deliberately no way to construct a payable amount without a `Citation`. The
FSC's 2026-05 agentic-AI guidance requires a traceable trail; making the trail a type
invariant is cheaper than auditing for it later.
"""

from enum import StrEnum

from msgspec import Struct


class ClauseKind(StrEnum):
    """What a clause does to a claim, which decides how it resolves against others."""

    DEFINITION = "definition"
    GRANT = "grant"
    """Brings a loss inside cover."""
    EXCLUSION = "exclusion"
    """Carves a loss back out."""
    CARVE_BACK = "carve_back"
    """Restores cover that an exclusion removed. Real contracts nest these in one sentence:
    國泰全心住院日額 art.17 excludes 美容手術、外科整型 then restores 為重建其基本功能所作之必要整型."""
    WAITING = "waiting"
    """Gates cover in time. Measured over the 660-document corpus: 119 contracts write
    等待期 outright, but 51 never use the word and hide the period inside the cover-page
    definition of 疾病 ("自本附約生效日起持續有效三十日以後"). Searching for the label
    finds four fifths of them and silently passes claims the rest refuse, so detection
    reads the definitions too."""
    LIMIT = "limit"
    ENDORSEMENT = "endorsement"
    """批註. Amends the printed contract and outranks it."""
    PROCEDURE = "procedure"


class Citation(Struct, frozen=True):
    """
    Where an assertion came from, precise enough for a judge to look it up.

    `verbatim` is copied out of the source document, never generated. The model may
    select a citation id; it may not author the text behind one.
    """

    doc_id: str
    """Hash-addressed document in the local corpus."""
    clause_id: str
    """e.g. "art.17.1" — stable within a doc_id."""
    page: int
    verbatim: str


class Clause(Struct, frozen=True):
    """One indexed clause of one contract."""

    clause_id: str
    kind: ClauseKind
    heading: str
    verbatim: str
    page: int
    overrides: tuple[str, ...] = ()
    """Clause ids this one defeats when both apply. An endorsement lists the printed
    clause it amends; a carve-back lists the exclusion it reopens."""


class Money(Struct, frozen=True):
    """
    An amount the desk is willing to put on screen.

    Amounts are whole TWD. `basis` is the arithmetic in words ("日額 2,000 × 住院 4 日"),
    so a reviewer can re-derive the number without reading code.
    """

    amount: int
    basis: str
    citations: tuple[Citation, ...]

    def __post_init__(self) -> None:
        if not self.citations:
            msg = f"amount {self.amount} has no citation; unsourced figures never reach a user"
            raise ValueError(msg)


class Verdict(StrEnum):
    """What the desk concluded about one claimable item."""

    PAYABLE = "payable"
    REFUSED = "refused"
    NEEDS_HUMAN = "needs_human"
    """Evidence is incomplete, clauses conflict unresolvably, or the test is medical
    rather than contractual (是否為重建基本功能所作之必要整型). Held out of every total."""


class ClaimItem(Struct, frozen=True):
    """
    One line of one filing against one policy.

    A REFUSED or NEEDS_HUMAN item still carries its citations: the reason a claim was
    turned down is exactly as auditable as the reason one was paid.
    """

    policy_id: str
    benefit: str
    verdict: Verdict
    reason: str
    money: Money | None = None

    def __post_init__(self) -> None:
        if self.verdict is Verdict.PAYABLE and self.money is None:
            msg = f"{self.policy_id}/{self.benefit} is payable but carries no amount"
            raise ValueError(msg)
        if self.verdict is not Verdict.PAYABLE and self.money is not None:
            msg = f"{self.policy_id}/{self.benefit} is {self.verdict} yet carries an amount; it would leak into a total"
            raise ValueError(msg)


class Stage(StrEnum):
    """
    Where a case has reached.

    The order is the order of the desk's own flow, and it only moves forward: a case
    that reached REVIEW cannot quietly slip back to PROPOSED and be re-proposed under
    the caseworker's feet. Rejection ends the case rather than rewinding it, because a
    rejected application and a fresh enquiry are different things to an auditor.
    """

    INQUIRY = "inquiry"
    """保戶在問，還沒有具體標的。"""
    PROPOSED = "proposed"
    """已提出方案供選擇。推介行為在此發生，故此階段起需要責任業務員落款。"""
    ISSUED = "issued"
    """合約與應簽署文件已交付，等待保戶簽回。"""
    SIGNED = "signed"
    """文件已簽署上傳，尚未驗證身分。"""
    VERIFIED = "verified"
    """身分驗證通過。此前的簽署不具本人親簽的推定效力。"""
    REVIEW = "review"
    """送交核保理賠人員。agent 至此只確認件齊，不判斷准駁。"""
    APPROVED = "approved"
    REJECTED = "rejected"


_ORDER = (
    Stage.INQUIRY,
    Stage.PROPOSED,
    Stage.ISSUED,
    Stage.SIGNED,
    Stage.VERIFIED,
    Stage.REVIEW,
)


def may_advance(current: Stage, target: Stage) -> bool:
    """
    Say whether a case may move from one stage to another.

    Args:
        current: Where the case is.
        target: Where it is being moved to.

    Returns:
        True when the move is one step forward along the flow, or a decision on a case
        that is under review. Every other move is refused, including staying put.

    """
    if current in (Stage.APPROVED, Stage.REJECTED):
        return False
    if target in (Stage.APPROVED, Stage.REJECTED):
        return current is Stage.REVIEW
    if current not in _ORDER or target not in _ORDER:
        return False
    return _ORDER.index(target) == _ORDER.index(current) + 1
