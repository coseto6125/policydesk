"""
DISCLOSURE: 健康告知與據實說明義務.

「我有高血壓要講嗎」「之前開過刀會不會影響」「沒寫進去會怎樣」 — every one of these is
the same legal question asked in different words: what does 保險法 §64 require, and what
happens when it is not met. The customer's vocabulary is the problem for retrieval. §64
never mentions a disease by name, so a search built from the customer's literal words
(高血壓, 開過刀) matches nothing in the statute — verified against the live corpus before
writing this module, where every one of those phrases alone returned zero rows. `_ANCHOR`
below is the fix: the fixed legal vocabulary of §64 itself, prepended to whatever the
customer said, so the search always resolves to the provisions this scenario exists to
quote and the customer's own words still count toward whatever else matches.

## The rule this scenario exists to enforce

§64 II is the insurer's right to rescind for an untruthful or omitted declaration. §64
III is the two-year 除斥期間 that takes that right away. Quoting II without III is
technically accurate and reads as a threat; the desk's job is to hand over both halves of
the same provision every time, never one without the other. `disclosure_duty` does this
structurally — one search, `search_statute`'s default `siblings=True` brings §64's three
項 back together — rather than leaving it to whatever the ranking happens to surface.

## What it may not do

**No 認錯, no 保證, no advice on what to write.** 這個不用寫 voids a contract if the
underwriter later decides otherwise; that decision belongs to underwriting, not this
desk. The desk states the duty, the consequence, and the two-year limit that bounds the
consequence. It does not tell the customer what to declare or what to leave out, and it
does not say whether declaring or omitting a specific condition would change anything —
that is exactly the judgment 核保 exists to make.

**No statute the retrieval did not return.** Same discipline as `soothe.py`: a citation
is copied from a tool row's `citation` field, never composed from memory.

## Identity

`medical_declaration` reads the member's own recorded health history, so it carries
`@requires_identity`. `disclosure_duty` reads `statute_article`, the same text anyone can
read on the government's site, so it carries no such mark — the duty itself is public
knowledge even before anyone proves who they are. The executor derives `allowed` from
these marks per tool (`policydesk.agent.tools.permitted`) and hands `gather` the set of
names that may run this turn; `gather` calls `disclosure_duty` regardless and calls
`medical_declaration` only when its name is in `allowed`. An unconfirmed customer
therefore still gets the duty and the two-year limit explained, with the request for an
ID attached to the missing half rather than standing in for the whole answer.
"""

from typing import TYPE_CHECKING, Any

from policydesk.agent import statute
from policydesk.agent.scenario_base import Param, Scenario, gather_tools
from policydesk.agent.tools import requires_identity

if TYPE_CHECKING:
    from policydesk.core.db import Database

STATUTE_SCOPE: list[str] = ["insurance_act"]
"""Where a disclosure question is answered from. Narrower than `soothe.py`'s three
statutes because §64 is squarely 保險法 — the 施行細則 and 金融消保法 have nothing to add
to a question about the underwriting declaration itself."""

_ANCHOR = "據實說明義務 健康告知 保險人得解除契約 二年除斥期間"
"""保險法 §64's own vocabulary, prepended to the customer's words before every search.

Why a fixed anchor rather than the customer's sentence alone: §64 is one general duty
provision, worded in the statute's own terms (據實說明, 危險之估計, 解除契約), and never
names a disease. A customer asking 有高血壓要不要講 contributes the word 高血壓, which
matches no row in `statute_article` — checked against the live corpus, where that phrase
alone returns nothing. The customer's words still ride along after the anchor and still
count toward the ranking when they do overlap the statute's own vocabulary (e.g. someone
quoting 解除契約 back at the desk); they are never load-bearing on their own.
"""

_LABEL: dict[str, str] = {
    "none": "無",
    "hypertension": "高血壓",
    "diabetes": "糖尿病",
    "hepatitis_b": "B型肝炎",
    "asthma": "氣喘",
    "cancer_history": "癌症病史",
    "cardiac": "心臟疾病",
}
"""Chinese labels for `member.medical_history`'s values (`policydesk.synthetic.person
.MedicalHistory`). The column is `text[]` of these codes, and nothing else in the
codebase translates them for a reader — `web/server.py` joins the raw codes with 、 — so
a scenario that hands the model 'hypertension' gets a reply that says 'hypertension'."""




async def disclosure_duty(
    db: Database, concern: str, *, limit: int = 2, retriever: Any | None = None
) -> list[dict[str, Any]]:
    """
    Find §64's provisions on the duty to disclose truthfully.

    Args:
        db: The database.
        concern: What the customer described, in their own words.
        limit: Most provisions to rank, before `search_statute` fills in each hit's
            neighbouring 項.
        retriever: The shared BM25 index, when one is open. None falls back to the SQL
            ranking, which is worse and still answers.

    Returns:
        Provision rows with their verbatim text, ready to be quoted.

    Public: reads `statute_article`, not the member's own record, so this carries no
    `requires_identity` mark. That is what lets this half answer before verification —
    the executor's `allowed` set always includes this tool's name.
    """
    topic = f"{_ANCHOR} {concern}".strip()
    rows = await statute.search_statute(db, topic, STATUTE_SCOPE, limit=limit, retriever=retriever)
    return [
        {
            "citation": statute.citation(row),
            "statute": row["statute_name"],
            "doc_id": row["doc_id"],
            "paragraph": row["paragraph"],
            "verbatim": row["verbatim"],
        }
        for row in rows
    ]


@requires_identity
async def medical_declaration(db: Database, member_id: int) -> dict[str, Any]:
    """
    Read what this member has already declared as their health history.

    Args:
        db: The database.
        member_id: Whose declaration.

    Returns:
        `declared`: each recorded condition's code and Chinese label. Empty when the
        member has none on file or the member row does not exist.

    Reads one member's own record, so this carries `@requires_identity`. The point of
    reading it at all is narrow: letting the desk say what is already on file, so it can
    tell a customer "紀錄上已經有這一項" or "目前沒有這一項" without ever advising them
    whether to add or remove anything — that stays underwriting's call.

    """
    row = await db.fetch_one("SELECT medical_history FROM member WHERE member_id = $1::bigint", [member_id])
    if row is None:
        return {"declared": []}
    return {"declared": [{"code": code, "label": _LABEL.get(code, code)} for code in row["medical_history"]]}


TOOLS: dict[str, Any] = {"disclosure_duty": disclosure_duty, "medical_declaration": medical_declaration}
"""The scenario's tools, for the executor's dispatch.

`disclosure_duty` is unmarked and `medical_declaration` is marked `@requires_identity`,
so `reads_identity` reports True for this pair — the derivation is correct even though
only one of the two tools actually reads a member row.
"""


async def gather(
    db: Database,
    params: dict[str, str],
    *,
    member_id: int | None = None,
    today: Any | None = None,  # noqa: ARG001 - part of the shared scenario-module contract, unused here
    retriever: Any | None = None,
    allowed: frozenset[str] | None = None,
    **_: Any,
) -> dict[str, Any]:
    """
    Run this scenario's tools.

    Args:
        db: The database.
        params: What the router collected. `concern` is the health situation the
            customer described, in their own words.
        member_id: Whose declaration to read alongside the duty. Ignored when
            `medical_declaration` is not in `allowed` — an unconfirmed customer's session
            still carries no member_id worth reading.
        today: Unused; accepted because every scenario module is called the same way.
        retriever: The shared index, passed straight through to `disclosure_duty`.
        allowed: Which tool names may run this turn, from `policydesk.agent.tools
            .permitted`. None runs every tool — the shape a direct call (e.g. a test)
            gets, since nothing has withheld anything.

    Returns:
        Material for the injection, by tool name. Never sets `_identity_required` itself:
        the executor sets that flag whenever it withheld a tool, so a module that forgot
        to could not hand the model a partial answer that reads as a whole one.

    """
    factories: dict[str, Any] = {
        "disclosure_duty": lambda: disclosure_duty(db, params.get("concern", ""), retriever=retriever)
    }
    if member_id is not None:
        factories["medical_declaration"] = lambda: medical_declaration(db, member_id)
    return await gather_tools(factories, allowed=allowed)


DISCLOSURE = Scenario(
    name="disclosure",
    display_name="健康告知說明",
    summary="說明據實說明義務與未告知的後果",
    description=(
        "保戶問健康告知或據實說明義務，例如自己有沒有的病史、開過的刀、"
        "算不算要寫進健康告知、沒寫會怎樣時使用。這個情境不判斷該不該寫，只說明規則、"
        "後果與除斥期間，實際紀錄查詢也在這裡查。"
    ),
    injection=(
        "medical_declaration 是空的時候，或是沒有任何項目時，代表他的紀錄上沒有健康告知事項，不是系統查不到。說明紀錄上目前沒有這一項，並提醒實際認定由核保人員判斷。\n"
        "保戶在問健康告知的義務，你的任務是說清楚規則本身，不是幫他判斷要不要寫、能不能不寫。\n"
        "先說明訂立契約時對保險人的書面詢問要據實說明，引用工具回傳的條文並照 citation 欄位"
        "一字不差標註，例如〔保險法 第64條第1項〕。\n"
        "接著說明違反的後果：隱匿、遺漏或不實說明，如果足以變更或減少保險人對危險的估計，"
        "保險人可以解除契約。這句話後面一定要接著把工具回傳的除斥期間規定一起講——"
        "保險人知道原因後一個月不行使、或契約訂立後已經過兩年，就不能再解除契約。"
        "只講前面那句、不講除斥期間，等於只講對公司有利的一半，這個情境不可以這樣答。\n"
        "工具回傳保戶目前的健康告知紀錄時，把他描述的情況拿去對照紀錄：紀錄上已經有的，"
        "就說「紀錄上已經有這一項」；紀錄上沒有的，只能說「目前紀錄上沒有這一項」，"
        "不可以接著建議他要不要補、要不要寫、或說「這個不用寫」——這是核保人員的判斷，"
        "不是這個櫃台能決定的。\n"
        "不可以說某個病況一定要寫或一定不用寫，不可以說寫了就一定會怎樣、"
        "不寫就一定沒事，不可以承認公司過去做錯，不可以承諾一定能核保通過或一定會理賠。"
        "要不要解除契約、核保怎麼判斷，都由核保與理賠人員決定，你能做的是把依據攤開。\n"
        "不可以引用工具沒有回傳的條文，想不起來就說這部分需要查證，寫錯的條號比不寫更傷害保戶。\n"
        "工具回傳 _identity_required 時，表示保戶尚未完成身分核對，這次只有公開的條文可以用，"
        "拿不到他的健康告知紀錄。此時照工具回傳的條文把據實說明義務、解除契約的後果與除斥期間"
        "完整說一次——這部分不需要身分就能答，先把能答的答完，不要因為缺一半資料就整段都不答。"
        "答完再說明：要對照他自己紀錄上寫了什麼，需要先核對身分，請他提供身分證字號。"
        "不要憑空講任何關於他保單或紀錄的內容，也不要說紀錄上有沒有某一項——這件事你現在還查不到。"
    ),
    tools=("disclosure_duty", "medical_declaration"),
    tools_module="policydesk.agent.scenarios.disclosure",
    params=(
        Param(
            name="concern",
            description=(
                "保戶描述的健康狀況或情境，用他自己的話摘要成具體病名或事件，例如「高血壓」"
                "「開過闌尾炎手術」。保戶只是籠統問據實說明義務、沒有提到具體狀況時填「一般說明」"
            ),
            example="有高血壓，不確定要不要寫進健康告知",
        ),
    ),
    quick_replies=("除斥期間是怎麼算的？", "我想知道紀錄上現在寫了什麼？", "這件事要跟誰確認比較準？"),
    transitions=("soothe", "explain_cover"),
)
