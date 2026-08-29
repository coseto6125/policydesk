"""
The scenario for a customer asking how a claim already filed is coming along:
「我的理賠辦到哪了」「上次申請的賠款下來了嗎」「我的理賠被拒絕了嗎」.

## The desk reports, it does not decide

`claim.outcome` is NULL for most rows in this corpus, because most claims are still being
assessed — and NULL is exactly the state a reply must not paper over by guessing which way
the file will land. 核保與理賠的准駁由核保理賠人員決定, the same rule every other
scenario on this desk holds to. This one holds to it hardest, because "還在審核中" read
carelessly sounds like a stall, and the temptation to soften it into "應該快了" or "看起來
沒問題" is exactly the promise this desk may not make.

## `documents_pending` points, it does not list

A claim stuck at `documents_pending` is the one actionable state here, but the documents
themselves live on `required_document`, which `claim_checklist` already reads and formats
against the member's own products. Repeating that list here would drift the day one
scenario's copy changes and the other's does not — so this scenario names the state and
sends the customer to that scenario rather than inventing its own document list.

## `declined` is the hardest reply on this desk

A customer asking 我的理賠被拒絕了嗎 already suspects the answer. Hedging a recorded
decline into "目前尚未通過" is worse than stating it, because it invites him to wait for a
reversal that a closed file is not going to produce. So a decline is stated as recorded,
immediately followed by where to appeal — the same 申訴 route `soothe.complaint_channel`
already states, borrowed rather than restated so the two desks can never quote a different
number of days for the same right.

## `paid_amount` needs a unit the same way `sum_insured` did

`policydesk.agent.tools.insured_amount` exists because a bare 保險金額：1000 in a real
reply meant a thousand dollars to a customer and 100 萬元 to the table behind it. A bare
`paid_amount` in front of a customer is that same wrong number, so it is rendered with 元
here, before the model ever sees it.
"""

from typing import TYPE_CHECKING, Any

from policydesk.agent.scenario_base import Scenario, gather_tools
from policydesk.agent.scenarios.soothe import complaint_channel
from policydesk.agent.tools import requires_identity

if TYPE_CHECKING:
    from datetime import date
    from decimal import Decimal

    from policydesk.core.db import Database


def _paid_display(paid_amount: Decimal | None) -> str | None:
    """
    Render a claim's recorded payout, in the words a customer reads.

    Args:
        paid_amount: The raw `numeric` from `claim`, or None.

    Returns:
        e.g. `12,000 元`, or None while nothing has been paid — a claim still being
        assessed, or one decided against. None is left for the injection to explain
        rather than turned into a string here, because "0 元" reads as a payout and
        "尚未理算" is a claim about *why* that this function has no way to know.

    """
    if paid_amount is None:
        return None
    return f"{paid_amount:,.0f} 元"


@requires_identity
async def member_claims(db: Database, member_id: int) -> list[dict[str, Any]]:
    """
    List this member's claims, across every policy they hold.

    Args:
        db: The database.
        member_id: Whose claims.

    Returns:
        One row per claim, most recently filed first, with `paid` already rendered in
        元 and the raw `paid_amount` dropped — nothing downstream of this tool needs the
        bare `Decimal`, and keeping it around is one more place a figure could reach a
        customer unrendered.

    """
    rows = await db.fetch(
        """SELECT c.claim_id, po.policy_number, pr.name AS product_name, c.kind,
                  c.event_at, c.filed_at, c.stage, c.outcome, c.decided_at, c.paid_amount
           FROM claim c
           JOIN policy po USING (policy_id)
           JOIN product pr USING (product_id)
           WHERE po.member_id = $1::bigint
           ORDER BY c.filed_at DESC""",
        [member_id],
    )
    for row in rows:
        row["paid"] = _paid_display(row.pop("paid_amount"))
    return rows


TOOLS: dict[str, Any] = {"member_claims": member_claims, "complaint_channel": complaint_channel}
"""The scenario's tools, for the executor's dispatch.

`complaint_channel` is borrowed from `soothe` rather than copied — the appeal route a
declined claim needs is the same statutory channel a complaint needs, and a second copy
here is a second place for the deadline to go stale. It carries no `requires_identity`
mark, which `member_claims` does, so that mark alone is what gates this scenario.
"""


async def gather(
    db: Database,
    params: dict[str, str],  # noqa: ARG001 -- unused: this scenario takes no params, but every module shares one gather() signature
    *,
    member_id: int | None = None,
    today: date | None = None,  # noqa: ARG001 -- unused: claim rows already carry their own dates
    retriever: Any | None = None,  # noqa: ARG001 -- unused: no clause or statute search here
    allowed: frozenset[str] | None = None,
    **_: Any,
) -> dict[str, Any]:
    """
    Run this scenario's tools.

    Args:
        db: The database.
        params: What the router collected. This scenario takes none.
        member_id: Whose claims to read.
        today: Unused. Kept so this module matches the shared `gather` signature.
        retriever: Unused, for the same reason.
        allowed: Which tools may run. None runs both.

    Returns:
        The member's claims and the appeal channel, by tool name — `gather_tools` runs
        whichever of the two `allowed` permits and skips the rest, so a claim never gets
        read before the customer has proved who they are.

    """
    return await gather_tools(
        {
            "member_claims": lambda: member_claims(db, member_id),
            "complaint_channel": lambda: complaint_channel(db),
        },
        allowed=allowed,
    )


CLAIM_STATUS = Scenario(
    name="claim_status",
    display_name="理賠進度查詢",
    summary="查已送件理賠案目前的進度與結果",
    description=(
        "保戶問一件已經送出的理賠案辦到哪了、上次申請的賠款下來了嗎、理賠有沒有過、"
        "或理賠是不是被拒絕時使用。例如「我的理賠辦到哪了」「上次申請的賠款下來了嗎」"
        "「我的理賠被拒絕了嗎」。保戶只是想知道申請理賠要準備什麼文件、還沒送出申請時，"
        "不要選這個，那是 claim_checklist。"
    ),
    injection=(
        "member_claims 是空的時候，代表這位保戶名下目前沒有申請中或已結案的理賠紀錄，"
        "那是好消息不是查詢失敗。直接告訴他「您目前沒有申請中的理賠」，"
        "不要說系統查不到或請他稍候。\n"
        "你正在幫保戶查一件理賠案辦到哪一步，你不是核保或理賠人員，"
        "不能替他判斷會不會賠、賠多少，也不能用「應該會過」「看起來沒問題」這種話安撫他——"
        "那是核保理賠人員的權責，不是你的。\n\n"
        "有理賠紀錄時，逐筆照 stage 講清楚進度，不要跳過任何一筆：\n"
        "stage 是 received：已收到申請，正在等待進入審核，還不需要保戶做任何事。\n"
        "stage 是 documents_pending：文件還沒齊，這是保戶現在唯一能動的狀態——"
        "請他去問「這個理賠案還缺什麼文件」查 claim_checklist 這個情境確認缺什麼，"
        "不要在這裡自己列文件清單，那份清單只有 claim_checklist 查得到。\n"
        "stage 是 assessing：正在審核中，outcome 一定是空的，"
        "不要猜審核結果，也不要說「快了」「應該沒問題」這類工具沒有回傳依據的話。\n"
        "stage 是 decided：已經有結果了，照 outcome 講：\n"
        "  outcome 是 paid：已核付理賠金，金額照 paid 欄位講，那是實際入帳的金額，不要自己換算或估計。\n"
        "  outcome 是 partial：部分理賠，金額照 paid 欄位講，並告訴他如果對金額有疑義可以申訴。\n"
        "  outcome 是 declined：這件理賠已經被拒絕，要照實講清楚，"
        "不可以講成「還在審核」「尚未通過」這種模糊的話——保戶問的就是這件事，"
        "含糊比講白更傷害他。講完之後立刻接上 complaint_channel 回傳的申訴管道："
        "可以在多少天內向公司提出申訴、公司要在多少天內回覆、"
        "沒有得到滿意答覆或超過期限後，還可以在多少天內向 ombudsman 申請評議，"
        "有 basis 就照 citation 語法一字不差標註，例如〔金融消費者保護法 第13條第2項〕，"
        "沒有 basis 就不要自己編一個條號出來。\n\n"
        "每一個天數、金額、狀態都必須是工具回傳的，想不起來或工具沒回傳就說這部分需要查證，"
        "寫錯的金額或條號比不寫更傷害保戶。\n\n"
        "工具回傳 _identity_required 時，表示保戶尚未完成身分核對，material 裡沒有他的 member_claims，"
        "但 complaint_channel 一定還在——申訴管道是公開的規定，不用核對身分也能先講。"
        "此時先告訴他查詢自己的理賠進度需要先核對身分，請他提供身分證字號，"
        "不要憑空講任何關於他理賠案件的內容，也不要假裝已經查過。"
    ),
    tools=("member_claims", "complaint_channel"),
    tools_module="policydesk.agent.scenarios.claim_status",
    # 我要申訴這個理賠結果 was here, and it is an intention rather than a question — a
    # mis-tap would have written a complaint the customer never made into the case record.
    quick_replies=("這個理賠案還缺什麼文件？", "理賠結果不滿意可以怎麼處理？", "我想查其他保單的理賠"),
    transitions=("claim_checklist", "soothe"),
)
