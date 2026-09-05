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

from typing import TYPE_CHECKING, Any

from policydesk.agent import statute
from policydesk.agent.scenario_base import Param, Scenario, gather_tools
from policydesk.agent.tools import public

if TYPE_CHECKING:
    from policydesk.core.db import Database

STATUTE_SCOPE: list[str] = ["insurance_act", "insurance_act_rules", "financial_consumer_protection_act"]
"""Which statutes a complaint may be answered from."""

@public
async def statute_reference(
    db: Database, concern: str, limit: int = 4, *, retriever: Any | None = None
) -> list[dict[str, Any]]:
    """
    Find the provisions that bear on what the customer raised.

    Args:
        db: The database.
        concern: The complaint, in the customer's own words.
        limit: Most provisions to return.
        retriever: The shared BM25 index, when one is open. None ranks by SQL, which is
            worse and still answers.

    Returns:
        Provision rows with their verbatim text, ready to be quoted.

    Public: it reads `statute_article`, which is the same text anyone can read on the
    government's site, so it carries no `requires_identity` mark. That is what lets this
    scenario answer someone who has not verified — and a customer mid-complaint being
    asked for his ID reads as the counter refusing to engage.
    """
    rows = await statute.search_statute(db, concern, STATUTE_SCOPE, limit=limit, retriever=retriever)
    return [
        {
            "citation": statute.citation(row),
            "statute": row["statute_name"],
            "doc_id": row["doc_id"],
            "chapter": row["chapter"],
            "verbatim": row["verbatim"],
        }
        for row in rows
    ]




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


@public
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
    if await statute.unresolved(db, route["basis"]):
        route.pop("basis")
    return route


TOOLS: dict[str, Any] = {"statute_reference": statute_reference, "complaint_channel": complaint_channel}
"""The scenario's tools, for the executor's dispatch.

Neither is marked `requires_identity`, so `reads_identity` reports False and the gate
lets this scenario run unverified. That is the intended reading of the derivation rather
than an exception to it: these tools genuinely read no member row.
"""


async def gather(
    db: Database,
    params: dict[str, str],
    *,
    retriever: Any | None = None,
    allowed: frozenset[str] | None = None,
    **_: Any,
) -> dict[str, Any]:
    """
    Run this scenario's tools.

    Args:
        db: The database.
        params: What the router collected. `concern` is the complaint in the customer's
            words.
        retriever: The shared index, passed straight through. The executor holds one on
            `app.ctx`; `_gather` can hand it over in the same line that calls this.

    Returns:
        Material for the injection, by tool name.

    One call so the wiring into `_gather` is a single line, and so the two tools stay
    together: the escalation route without the provisions reads as being shown the door,
    and the provisions without the route as being read the law.
    """
    return await gather_tools(
        {
            "statute_reference": lambda: statute_reference(db, params.get("concern", ""), retriever=retriever),
            "complaint_channel": lambda: complaint_channel(db),
        },
        allowed=allowed,
    )


SOOTHE = Scenario(
    name="soothe",
    display_name="疑義與申訴",
    summary="保戶不滿或引法條質問時的回應與申訴管道",
    description=(
        "保戶表達不滿、質疑公司做法、認為被騙、提到要申訴或提告、或引用法條質問時使用。"
        "例如「你們憑什麼解約」「這根本是騙人的」「我要去金管會申訴」「保險法不是寫…嗎」。"
        "保戶只是在問保障內容或理賠文件時不要選這個情境。"
        # 「我是要改年繳欸，你不懂嗎?」 landed here, and this scenario answered with the
        # complaint channel: the customer, who wanted a payment mode changed, was handed
        # 金融消費者保護法 §13 and a chip reading 申訴要向誰提出. Impatience at being
        # misread is not a complaint about the company; the scenario that can do the thing
        # is the apology.
        "保戶只是不耐煩地重述他剛才的要求（例如「我是要改年繳欸」「你不懂嗎」「我問的是這個」），"
        "也不要選這個情境：直接選能辦那件事的情境，把他要的答案給他。"
    ),
    injection=(
        "statute_reference 是空的時候，代表這件事在本櫃台手上的三部法規裡找不到對應條文，不是系統查不到。承接他的情緒之後，照實說這一點需要進一步查證，並把 complaint_channel 的申訴管道與期限講清楚，不要憑印象給一個條號。\n"
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
        "**回傳的條文跟他講的事情對不上時，一條都不要引。** 檢索是照字面找的，"
        "保戶說「為什麼不願意跟我說原因」，撈回來的是爭議處理程序的保密義務——"
        "條號是真的，但那條講的不是他的事，引出來會讀成「法律規定我們不用告訴你」。"
        "先問自己一句：這一條講的情境，是不是他正在講的情境？"
        "不是就跳過，全部都不是就承接情緒之後直接講申訴管道與期限，"
        "並說這件事需要進一步查證。硬湊一條比不引更傷害保戶。\n"
        "不要提到保戶名下任何一張保單的內容，這個情境沒有查他的資料。"
        "他問到自己的保單時，告訴他你可以為他查，並請他完成身分核對。"
    ),
    tools=("statute_reference", "complaint_channel"),
    tools_module="policydesk.agent.scenarios.soothe",
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
    # 我要申訴，該找誰？ was here. The tail is a question and the head is a decision, and a
    # mis-tap writes 我要申訴 into a case record for someone who was only asking how it
    # works. 請幫我查我的保單怎麼寫 was an instruction for the same reason.
    quick_replies=("我想知道這條實際怎麼適用在我身上", "申訴要向誰提出？", "我的保單條款是怎麼寫的？"),
    transitions=("explain_cover", "policy_overview", "claim_checklist"),
)
