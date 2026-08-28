"""
The scenario for a customer who is not asking a question yet.

「你們憑什麼解約」「我要申訴」「這根本就是騙人的」 — these arrive as complaints, and the
desk's other scenarios all assume a question. Routing them to `explain_cover` produces a
clause quotation aimed at someone who did not ask what their policy says, which reads as
the company answering an accusation with fine print. That is the reply that turns a
complaint into a 申訴.

So this scenario has one rule, and it is a rule about order rather than about tone:

**Acknowledge, then locate, then offer the next step.** The law is retrieved so the
customer can be told where they stand, not so the desk can win. 保險法 §64 III is a good
example of why the distinction is not cosmetic: cite §64 II alone and it reads as the
grounds for rescinding the customer's policy; cite III with it and the same provision is
the two-year limit that protects him. The provision that answers the complaint is usually
the one that constrains the insurer, so retrieving statute for a complaint is more often
in the customer's favour than against him.

## What it may not do

**No 認錯, no 保證, no 承諾.** The desk is not the underwriter and not the claims
committee. It may say what the law provides and what the contract says and where to take
the matter; it may not say 我們會賠 or 這是我們的錯. Both are decisions somebody with
authority has to make, and a promise made at the counter is one the customer will
reasonably rely on.

**No statute the retrieval did not return.** Statute citations are checked against
`statute_article` after the model writes them, the same way clause citations are checked
against `clause`. A misremembered §64 II is a sentence that sounds like law and is not.

## Identity

None of this needs it. The law is public, the complaint channel is public, and a person
angry enough to be shouting is the worst possible audience for 請先提供身分證字號. What
needs identity is anything about *his* policy, and none of these tools read one — so this
scenario answers before verification, and hands off to a scenario that gates when the
conversation turns to his own contract.
"""

import re
from typing import TYPE_CHECKING, Any

from policydesk.agent import statute
from policydesk.agent.scenario import Param, Scenario

if TYPE_CHECKING:
    from policydesk.core.db import Database

STATUTE_SCOPE: list[str] = ["insurance_act", "insurance_act_rules", "financial_consumer_protection_act"]
"""Which statutes a complaint may be answered from."""

CITATION = re.compile(
    r"〔([\u4e00-\u9fff]{2,12})\s*第\s*(\d{1,3})(?:[-之]\s*(\d{1,2}))?\s*條"
    r"(?:第\s*(\d{1,2})\s*項)?(?:第\s*(\d{1,2})\s*款)?〕"
)
"""How a statute citation is written in a reply: 〔保險法 第64條第2項〕.

Deliberately not the `art.64.2` shape the clause corpus uses. The executor extracts
`art.NN` from replies and voids any that no contract contains, and `art.64.2` contains
`art.64` — so a statute written that way would be read as a clause citation, found in no
policy, and the whole reply withheld. Two corpora sharing one citation syntax is a
collision the reader cannot see and the checker cannot resolve.

It is also the form a Taiwanese reader recognises, which is the other half of the point:
a customer who wants to check what he has been told can type it into 全國法規資料庫.

The branch takes two digits, not one. 保險法 runs to 第149-11條, and a single-digit
pattern read 第149-10條 as no citation at all — which is the dangerous direction of the
failure: an unreadable citation is not a citation the checker rejects, it is one the
checker never sees, so an invented 第149-10條 would have passed. Found by formatting all
1,212 provisions and reading each one back; 15 did not round-trip.

之 is accepted beside the hyphen because that is how the statute writes it in its own
cross-references (第六十四條第三項, 第一百四十九條之十), and a model copying the corpus
will sometimes copy that.
"""


def cited(text: str) -> list[tuple[str, str]]:
    """
    Read the statute citations out of a reply.

    Args:
        text: What the model wrote.

    Returns:
        (statute name, doc_id) pairs in order of appearance, deduplicated.

    """
    out: list[tuple[str, str]] = []
    for name, article, branch, paragraph, item in CITATION.findall(text):
        doc_id = f"art.{article}-{branch}" if branch else f"art.{article}"
        if paragraph:
            doc_id += f".{paragraph}"
            if item:
                doc_id += f".{item}"
        pair = (name, doc_id)
        if pair not in out:
            out.append(pair)
    return out


async def recheck_citations(db: Database, text: str) -> list[tuple[str, str]]:
    """
    Name the statute citations in a reply that do not exist.

    Args:
        db: The database.
        text: The reply.

    Returns:
        The (statute name, doc_id) pairs with no matching row. Empty means every
        citation resolves.

    Checked by *name as written* as well as by doc_id, because 保險法 §64 and 保險法施行
    細則 §64 are different sentences and a reply that attributes one to the other is wrong
    in the way hardest for a reader to catch.
    """
    pairs = cited(text)
    if not pairs:
        return []
    rows = await db.fetch(
        """SELECT s.name, a.doc_id
           FROM statute_article a JOIN statute s USING (statute_id)
           WHERE a.doc_id = ANY($1::text[])""",
        [[doc_id for _, doc_id in pairs]],
    )
    real = {(row["name"], row["doc_id"]) for row in rows}
    return [pair for pair in pairs if pair not in real]


async def statute_reference(db: Database, concern: str, limit: int = 4) -> list[dict[str, Any]]:
    """
    Find the provisions that bear on what the customer raised.

    Args:
        db: The database.
        concern: The complaint, in the customer's own words.
        limit: Most provisions to return.

    Returns:
        Provision rows with their verbatim text, ready to be quoted.

    Public: it reads `statute_article`, which is the same text anyone can read on the
    government's site, so it carries no `requires_identity` mark. That is what lets this
    scenario answer someone who has not verified — and a customer mid-complaint being
    asked for his ID reads as the counter refusing to engage.
    """
    rows = await statute.search_statute(db, concern, STATUTE_SCOPE, limit=limit)
    return [
        {
            "citation": _readable(row),
            "statute": row["statute_name"],
            "doc_id": row["doc_id"],
            "chapter": row["chapter"],
            "verbatim": row["verbatim"],
        }
        for row in rows
    ]


def _readable(row: dict[str, Any]) -> str:
    """
    Write one provision's citation the way it is cited in Chinese.

    Args:
        row: A `statute_article` row joined to its statute.

    Returns:
        e.g. `〔保險法 第64條第2項〕`, exactly the form `CITATION` reads back.

    Handed to the model already formatted rather than described in the injection. A
    citation format explained in prose is one the model approximates; one it can copy is
    one the checker can verify.
    """
    number = f"第{row['article']}"
    if row.get("branch"):
        number += f"-{row['branch']}"
    number += "條"
    if row.get("paragraph"):
        number += f"第{row['paragraph']}項"
    if row.get("subparagraph"):
        number += f"第{row['subparagraph']}款"
    return f"〔{row['statute_name']} {number}〕"


COMPLAINT_ROUTE: dict[str, str] = {
    "internal_days": "30",
    "ombudsman": "財團法人金融消費評議中心",
    "ombudsman_phone": "0800-789-885",
    "ombudsman_deadline_days": "60",
    "basis": "〔金融消費者保護法 第13條第2項〕",
}
"""Where a complaint goes when the counter cannot settle it.

Statutory, not a service promise. 金融消費者保護法 §13 II: the complaint goes to the
company first, the company has 30 days, and the customer then has 60 days from that reply
— or from the deadline passing unanswered — to ask 評議中心 for a ruling. The 60 days is
the part a soothed customer loses by being soothed, so a desk that calms him down without
saying it has talked him out of a right.
"""


async def complaint_channel(db: Database) -> dict[str, str]:
    """
    State the escalation route, with the provision it rests on.

    Args:
        db: The database, used to confirm the cited provision is really there.

    Returns:
        The route. `basis` is dropped when the statute corpus does not contain the
        provision, so the desk states the channel without attributing it to a section it
        cannot show.

    """
    route = dict(COMPLAINT_ROUTE)
    if await recheck_citations(db, route["basis"]):
        route.pop("basis")
    return route


TOOLS: dict[str, Any] = {"statute_reference": statute_reference, "complaint_channel": complaint_channel}
"""The scenario's tools, for the executor's dispatch.

Neither is marked `requires_identity`, so `reads_identity` reports False and the gate
lets this scenario run unverified. That is the intended reading of the derivation rather
than an exception to it: these tools genuinely read no member row.
"""


async def gather(db: Database, params: dict[str, str]) -> dict[str, Any]:
    """
    Run this scenario's tools.

    Args:
        db: The database.
        params: What the router collected. `concern` is the complaint in the customer's
            words.

    Returns:
        Material for the injection, by tool name.

    One call so the wiring into `_gather` is a single line, and so the two tools stay
    together: the escalation route without the provisions reads as being shown the door,
    and the provisions without the route as being read the law.
    """
    return {
        "statute_reference": await statute_reference(db, params.get("concern", "")),
        "complaint_channel": await complaint_channel(db),
    }


SOOTHE = Scenario(
    name="soothe",
    display_name="疑義與申訴",
    description=(
        "保戶表達不滿、質疑公司做法、認為被騙、提到要申訴或提告、或引用法條質問時使用。"
        "例如「你們憑什麼解約」「這根本是騙人的」「我要去金管會申訴」「保險法不是寫…嗎」。"
        "保戶只是在問保障內容或理賠文件時不要選這個情境。"
    ),
    injection=(
        "保戶正在表達不滿或質疑，你的任務是讓他知道他的處境有依據，不是替公司辯護。\n"
        "順序固定，不可調換：\n"
        "一、先承接他的情緒，用一句話說明你聽到了什麼，不要在這句裡加上「但是」。\n"
        "二、再說明這件事的法律依據，引用工具回傳的條文，"
        "並照 citation 欄位一字不差地標註，例如〔保險法 第64條第2項〕。\n"
        "三、最後說明下一步：可以為他查什麼、需要他提供什麼、或申訴管道與期限。\n\n"
        "條文是幫他找路的工具，不是擋他的牆。"
        "同一條裡對保戶有利的部分要一起講——例如解除契約權有除斥期間，"
        "工具回傳了那一項就必須說出來，只講前一項等於用法律嚇他。\n\n"
        "不可以說的話：不可以承認公司有錯，不可以承諾會賠、會通融、會加保，"
        "不可以說某件事一定成立或一定不成立。核保與理賠的准駁由核保理賠人員決定，"
        "你能做的是把依據攤開、把流程講清楚。\n"
        "不可以引用工具沒有回傳的條文。想不起來就說這部分需要查證，"
        "寫錯的條號比不寫更傷害保戶。\n"
        "不要提到保戶名下任何一張保單的內容，這個情境沒有查他的資料。"
        "他問到自己的保單時，告訴他你可以為他查，並請他完成身分核對。"
    ),
    tools=("statute_reference", "complaint_channel"),
    params=(
        Param(
            name="concern",
            description=(
                "保戶不滿的事由，用他自己的說法摘要成一句話，不要改寫成公司用語。"
                "他提到具體條號或名詞時原樣保留"
            ),
            example="公司說要解除契約因為健康告知沒寫",
        ),
    ),
    quick_replies=("我想知道這條實際怎麼適用在我身上", "我要申訴，該找誰？", "請幫我查我的保單怎麼寫"),
    transitions=("explain_cover", "policy_overview", "claim_checklist"),
)
