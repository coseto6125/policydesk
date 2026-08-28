"""Domain models.

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
    """Gates cover in time. Often not labelled 等待期 anywhere — the same contract hides
    its 30-day wait inside the cover-page definition of 疾病, which is why a keyword search
    for 等待期 returns nothing and a naive retrieval passes a claim it should refuse."""
    LIMIT = "limit"
    ENDORSEMENT = "endorsement"
    """批註. Amends the printed contract and outranks it."""
    PROCEDURE = "procedure"


class Citation(Struct, frozen=True):
    """Where an assertion came from, precise enough for a judge to look it up.

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
    """An amount the desk is willing to put on screen.

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
    """One line of one filing against one policy.

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
