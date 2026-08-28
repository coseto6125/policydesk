"""
What the two panes say to the server and to each other.

One case, two views. The customer pane on the right is a conversation; the desk pane
on the left is that same case as a caseworker sees it. They are not two applications
sharing a database — they are one case object with two renderings, which is why every
mutation broadcasts to both rather than each side polling for the other's changes.

Messages are tagged unions so that adding a message type cannot silently produce an
untagged dict that the other side ignores.
"""

from typing import TYPE_CHECKING, Literal

from msgspec import Struct

if TYPE_CHECKING:
    from policydesk.core.models import Stage

# ---------------------------------------------------------------- customer → server


class Hello(Struct, tag="hello", tag_field="type"):
    """Claim the desk under a display name."""

    name: str


class Say(Struct, tag="say", tag_field="type"):
    """The customer typed something."""

    text: str


class Upload(Struct, tag="upload", tag_field="type"):
    """
    A signed document came back.

    Only the filename and the document it answers travel here; the bytes go over a
    separate POST, because a base64 PDF inside a websocket frame blocks the socket
    that the rest of the conversation is using.
    """

    document_id: str
    filename: str


CustomerMessage = Hello | Say | Upload


# -------------------------------------------------------------------- desk → server


class Decide(Struct, tag="decide", tag_field="type"):
    """
    A caseworker approved or rejected a case under review.

    A rejection carries its reason, shown to the customer verbatim: a refusal the
    customer cannot read is a refusal they will phone about. The invariant is enforced
    here rather than left to the docstring, because a default of "" would otherwise
    let an empty rejection through the wire and reach the customer as a blank.
    """

    case_id: str
    approved: bool
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.approved and not self.reason.strip():
            msg = f"case {self.case_id} was rejected with no reason; the customer would be shown a blank"
            raise ValueError(msg)


DeskMessage = Decide


# ---------------------------------------------------------------- server → both panes


class Document(Struct):
    """One document the customer must read, sign, or has signed."""

    document_id: str
    title: str
    kind: Literal["contract", "form", "disclosure", "signed"]
    signed: bool = False
    filename: str | None = None


class Evicted(Struct, tag="evicted", tag_field="type"):
    """Someone else claimed this name."""

    reason: str


class Chat(Struct, tag="chat", tag_field="type"):
    """A line of the conversation, shown in the right-hand pane."""

    speaker: Literal["customer", "agent", "system"]
    text: str
    at: float


class CaseView(Struct, tag="case", tag_field="type"):
    """
    The whole case, as both panes render it.

    Sent in full on every change rather than as a patch. A case is a few kilobytes and
    a demo that drifts because one pane missed a patch is worse than one that resends.
    """

    case_id: str
    customer: str
    stage: Stage
    documents: list[Document]
    identity_verified: bool
    adviser: str | None
    """責任業務員. Set the moment a recommendation is made, because 推介 is 招攬 and
    招攬 requires a registered individual to answer for it."""
    adviser_licence: str | None
    completeness: list[str]
    """What is still missing before the case can go to a human. The agent decides
    whether the file is complete; it does not decide whether the claim is payable."""
    decision_reason: str = ""


class Notice(Struct, tag="notice", tag_field="type"):
    """Something the agent wants the customer to see now, outside the chat flow."""

    text: str
    level: Literal["info", "warn", "good"] = "info"


ServerMessage = Evicted | Chat | CaseView | Notice
