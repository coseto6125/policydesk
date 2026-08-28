"""
Domain models.

The shape of `Money` is the load-bearing decision in this file. Every figure the desk
shows a user carries the evidence that produced it, because a figure without a citation
is the failure mode this product exists to remove: a tool that is reachable is not a
tool that is correct, and an insurance figure nobody can trace is worse than no figure.

`Money` cannot be constructed without a `Citation`, and `ClaimItem` refuses an amount
unless the item is going on a filing. Those two are type invariants and hold against
any caller.

What the types do NOT give you is a guarantee that a citation is genuine — see
`Citation`. That one is procedural: the model never reaches the constructor. Keeping
the two kinds of guarantee apart is the point, because a claimed invariant that turns
out to be a convention is worse than no claim.

The FSC's 2026-05 agentic-AI guidance requires a traceable trail. Enforcing what can
be enforced in the type is cheaper than auditing for it later.
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
    Where an assertion came from, precise enough for a reader to look it up.

    The guarantee here is procedural, not structural, and the difference matters
    enough to write down: this constructor is public, so any caller can build a
    Citation holding whatever `verbatim` it likes. What makes a fabricated one
    impossible is that the model never reaches this constructor — a model tool takes
    `product_id` and `clause_id` only, and the evidence layer rebuilds the verbatim
    text from the store before anything renders.

    Treat a Citation arriving from anywhere but the evidence layer as unverified.
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


class Standing(StrEnum):
    """
    Whether the desk will put an item on a filing.

    Deliberately not payable/refused. Only the insurer's own staff decide whether a
    claim pays, so a desk that labels an item "payable" has made a promise it has no
    standing to make, and one that labels it "refused" has issued a declination on the
    insurer's behalf. These three words say what the desk is entitled to say.
    """

    CLAIMABLE = "claimable"
    """擬申請. The contract's conditions read as met on the evidence held, so the item
    goes on the filing. The insurer still decides."""
    CONTESTED = "contested"
    """契約疑義. A clause on file reads against the item — waiting period not elapsed,
    an exclusion on point. Stated with its citation, kept off the filing, and never
    phrased to the customer as a declination."""
    NEEDS_HUMAN = "needs_human"
    """待人工. Evidence is incomplete, clauses conflict unresolvably, or the test is
    medical rather than contractual (是否為重建基本功能所作之必要整型)."""


class ClaimItem(Struct, frozen=True):
    """
    One line of one filing against one policy.

    A CONTESTED or NEEDS_HUMAN item keeps its citations: why an item stayed off the
    filing is exactly as auditable as why one went on it.
    """

    policy_id: str
    benefit: str
    standing: Standing
    reason: str
    money: Money | None = None

    def __post_init__(self) -> None:
        if self.standing is Standing.CLAIMABLE and self.money is None:
            msg = f"{self.policy_id}/{self.benefit} is claimable but carries no amount"
            raise ValueError(msg)
        if self.standing is not Standing.CLAIMABLE and self.money is not None:
            msg = f"{self.policy_id}/{self.benefit} is {self.standing} yet carries an amount; it would leak into a total"
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
    Say whether the flow permits a move from one stage to another.

    Ordering only. A stage also has preconditions this function knows nothing about —
    an adviser must have signed the recommendation before PROPOSED, the documents must
    be signed before SIGNED, identity must be verified before VERIFIED, the file must
    be complete before REVIEW. Those live with the case command that performs the
    move, because they need the case, not just two stage names. Calling this alone and
    treating a True as permission is the mistake this docstring exists to prevent.

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
