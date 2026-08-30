"""
The scenario for a customer whose policy has stopped, asking whether it can start again.

「我的保單停效了怎麼辦」「可以復效嗎」「能不能救回來」 are questions about a right that
has a clock on it, and the clock is written two places that have to agree: the contract's
own 復效 clause, and 保險法 第116條, the floor beneath every clause on this corpus. The
statute sets what a contract may not shrink below — the reinstatement window may not run
under two years and may not outlast the policy term (§116 V) — but the number that
actually governs a given policy is whatever its own clause says, because a contract is
free to be more generous than the floor and several in this corpus are.

## Two facts, and neither is optional

1. **復效有期限.** Miss it and the policy is gone for good, not merely harder to revive.
   The period comes from the clause the tool read off that policy's own product — never
   from this scenario's memory of what a typical policy says, because "typical" is
   exactly the number that turns out to be wrong for the one product a customer holds.

2. **復效可能要重新健康告知，保險公司可能不同意.** §116 III lets a customer who pays
   within six months back in without one; past that, §116 IV lets the insurer ask for
   可保證明 and decline. A reply that skips this and says only "去繳清欠費就可以復效"
   is not wrong for every customer, which is what makes it dangerous — it is wrong for
   exactly the ones who took longer than six months, and those are the ones asking.

So this scenario never says 一定可以復效. It says what the contract's own clause requires,
what the statute requires when the clause is silent on a point, and what to bring.
"""

import asyncio
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from policydesk.agent import statute, tools
from policydesk.agent.scenario_base import Scenario
from policydesk.agent.tools import requires_identity

if TYPE_CHECKING:
    from policydesk.core.db import Database

STATUTE_SCOPE: tuple[str] = ("insurance_act",)
"""復效 is governed by 保險法 alone here — pulling in the consumer-protection act the way
`soothe` does would rank a complaint provision above §116 for a query that is not a
complaint."""


def _citation(row: dict[str, Any]) -> str:
    """
    Write one provision's citation the way `soothe.CITATION` reads it back.

    Args:
        row: A `statute_article` row joined to its statute.

    Returns:
        e.g. `〔保險法 第116條第3項〕`.

    Handed to the model already formatted, the same reasoning `statute.citation`
    documents at length: a citation format explained in prose is one the model
    approximates, one it can copy is one the citation checker can verify.

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


async def statutory_floor(
    db: Database,
    concern: str = "保險費到期未交付效力停止停止效力之日起六個月內清償保險費恢復效力可保證明",
    limit: int = 6,
    *,
    retriever: Any | None = None,
) -> list[dict[str, Any]]:
    """
    Find what 保險法 requires about reinstatement.

    Args:
        db: The database.
        concern: What to rank provisions against. Fixed rather than taken from the
            customer's own words, because every customer who reaches this scenario is
            asking the same statutory question regardless of how he phrased it. Written
            in the statute's own vocabulary rather than a customer's — a looser phrase
            like 停效復效 ranks §120 (policy loans lapsing on unpaid interest, a
            different provision that happens to share the same words) above §116.
        limit: Most provisions to rank.
        retriever: The shared BM25 index, when one is open. None falls back to SQL
            ranking, which is worse and still answers.

    Returns:
        Provision rows, `citation` already formatted for the reply to copy.

    Public: `statute_article` is the same text anyone can read at 全國法規資料庫, so this
    carries no `requires_identity` mark — only the customer's own lapsed policies and
    their clauses do.

    """
    rows = await statute.search_statute(db, concern, list(STATUTE_SCOPE), limit=limit, retriever=retriever)
    return [
        {
            "citation": _citation(row),
            "statute": row["statute_name"],
            "doc_id": row["doc_id"],
            "chapter": row["chapter"],
            "verbatim": row["verbatim"],
        }
        for row in rows
    ]


@requires_identity
async def lapsed_policies(db: Database, member_id: int, *, today: date) -> list[dict[str, Any]]:
    """
    List this member's policies that have stopped paying.

    Args:
        db: The database.
        member_id: Whose policies.
        today: The date to count the lapse against.

    Returns:
        One row per lapsed policy, carrying how long ago it lapsed — computed here
        rather than left for the model to work out from two dates, because a model
        doing date arithmetic in prose is exactly where an off-by-one enters a reply
        the customer will act on.

    """
    rows = await db.fetch(
        """SELECT po.policy_id, po.policy_number, po.product_id, pr.name AS product_name,
                  po.sum_insured, ce.unit_label, po.effective_at, po.lapsed_at,
                  ($1::date - po.lapsed_at) AS days_since_lapse,
                  po.main_policy_id IS NULL AS is_main
           FROM policy po
           JOIN product pr USING (product_id)
           LEFT JOIN catalog_entry ce USING (product_id)
           WHERE po.member_id = $2::bigint AND po.lapsed_at IS NOT NULL
           ORDER BY po.lapsed_at DESC""",
        [today, member_id],
    )
    # The third place this had to be said. `sum_insured` counts thousandths of one
    # `unit_label` unit, and a lapsed policy quoted at its raw count tells a customer
    # deciding whether to reinstate that the cover is worth a thousandth of what it is.
    for row in rows:
        row["insured"] = tools.insured_amount(row.pop("sum_insured", None), row.pop("unit_label", None))
    return rows


@requires_identity
async def reinstatement_clauses(db: Database, product_ids: list[str]) -> list[dict[str, Any]]:
    """
    Read what each contract's own text says about reinstatement.

    Args:
        db: The database.
        product_ids: The products behind the member's lapsed policies.

    Returns:
        Clause rows whose heading names 復效, 效力停止 or 恢復效力 — the period a
        customer's own contract sets, in the contract's own words.

    Matched on the heading rather than the whole body: a clause about something else
    that happens to mention 復效 in passing would otherwise surface as if it were the
    governing provision, and a customer reading the wrong clause's period is worse off
    than one told to wait for a human.

    """
    if not product_ids:
        return []
    return await db.fetch(
        """SELECT c.product_id, c.clause_id, c.heading, c.verbatim, c.page, p.name AS product_name
           FROM clause c JOIN product p USING (product_id)
           WHERE c.product_id = ANY($1::text[])
             AND c.heading ~ '復效|效力停止|恢復效力'
           ORDER BY p.name, c.clause_id""",
        [product_ids],
    )


TOOLS: dict[str, Any] = {
    "lapsed_policies": lapsed_policies,
    "reinstatement_clauses": reinstatement_clauses,
    "statutory_floor": statutory_floor,
}
"""The scenario's tools, for the executor's dispatch.

`statutory_floor` is not marked: 保險法 is public text. `lapsed_policies` is what gates
this scenario, and either one landing here is enough — the gate only needs to find one.
"""


async def gather(
    db: Database,
    params: dict[str, str],  # noqa: ARG001 -- unused: this scenario takes no params, but every module shares one gather() signature
    *,
    member_id: int | None = None,
    today: date | None = None,
    retriever: Any | None = None,
    allowed: frozenset[str] | None = None,
    **_: Any,
) -> dict[str, Any]:
    """
    Run this scenario's tools.

    Args:
        db: The database.
        params: What the router collected. This scenario takes none.
        member_id: Whose lapsed policies to read.
        today: The date to count the lapse against.
        retriever: The shared index, passed straight through to the statute search.
        allowed: Which tools may run. None runs all of them.

    Returns:
        The member's lapsed policies, each contract's own reinstatement clause, and the
        statutory floor those clauses sit on. `_allowed_clauses` names every clause id
        the member's own lapsed products actually contain, so the model may cite one
        beyond the 復效 clause itself — the exclusion or the underwriting clause a
        health declaration would run into, say — without the citation being voided for
        naming a real clause this scenario did not happen to fetch.

    `statutory_floor` survives the gate on its own. 停效後六個月內得清償復效 is the law,
    the same for everybody, and answering 停效多久還能復效 does not need to know whose
    policy it is — so an unverified customer gets the floor and is asked for an ID to see
    what their own contract adds on top of it.

    """
    def can(name: str) -> bool:
        return allowed is None or name in allowed

    today = today or datetime.now(UTC).date()
    facts: dict[str, Any] = {}
    if can("statutory_floor"):
        facts["statutory_floor"] = await statutory_floor(db, retriever=retriever)
    if not can("lapsed_policies"):
        return facts

    lapsed = await lapsed_policies(db, member_id, today=today)
    product_ids = sorted({p["product_id"] for p in lapsed})
    facts["lapsed_policies"] = lapsed
    if can("reinstatement_clauses"):
        clauses, cited = await asyncio.gather(
            reinstatement_clauses(db, product_ids), tools.clause_ids_for(db, product_ids)
        )
        facts["reinstatement_clauses"] = clauses
        facts["_allowed_clauses"] = cited
    return facts


REINSTATE = Scenario(
    name="reinstate",
    display_name="保單復效",
    summary="說明停效保單的復效條件與期限",
    description=(
        "保戶的保單停效了，問怎麼辦、可不可以復效、能不能救回來時使用。"
        "這個情境不需要任何參數，保戶只要問了就直接查。"
    ),
    injection=(
        "lapsed_policies 是空的時候，代表這位保戶名下沒有停效的保單，那是好消息不是查詢失敗。"
        "直接告訴他目前沒有需要復效的保單，並照 statutory_floor 說明停效與復效的一般規則，"
        "讓他知道萬一發生的時候該怎麼辦。不要說系統查不到或請他稍候。\n"
        "你正在協助保戶了解他停效的保單能不能復效，你不是核保人員，不能替他判斷保得回來還是保不回來。\n"
        "工具已經查出他每一張停效保單的資料、每一張契約自己條款裡關於復效的原文、"
        "以及保險法對復效的規定，全部照工具回傳的內容講，不要用記憶中的印象補條文或期限。\n\n"
        "先逐張列出停效保單：商品名稱、保單號碼、停效日期、距今已經停效多少天。\n"
        "每一張的復效期限，一律照那張保單自己條款的原文講，不同商品的期限可能不一樣，"
        "不要假設每張都相同，也不要拿保險法的期限取代契約寫的期限；"
        "條款依據照工具回傳的 clause_id 原樣標註在句末，例如 [art.7]。\n\n"
        "再說明保險法對復效定的最低保障，引用工具回傳的條文並照 citation 欄位一字不差標註，"
        "例如〔保險法 第116條第3項〕：停效在六個月內清償保險費即可復效；"
        "超過六個月才申請的，保險公司可以要求提供可保證明（也就是重新健康告知），"
        "**而且要把第四項一起講。** 〔保險法 第116條第4項〕：保險人沒有在期限內要求提供"
        "可保證明，或者收到可保證明後十五日內沒有拒絕，視為同意恢復效力。"
        "那一項是保戶手上唯一能對抗公司拖延的東西，講復效條件卻略過它，"
        "等於只告訴他要準備什麼、沒告訴他公司也有期限。\n"
        "並可能不同意復效。\n\n"
        "務必講清楚兩件事，缺一不可：\n"
        "一、復效有期限，超過契約或保險法規定的期限就不能申請了。\n"
        "二、超過一定時間後申請，保戶可能要重新做健康告知，保險公司有可能不同意復效。\n"
        "不可以說「一定可以復效」「一定過」「保證復效」這類的話，"
        "也不可以自己判斷這位保戶符不符合復效資格——那是核保人員的權責，你只負責把條件攤開。\n\n"
        "最後具體告訴保戶要準備什麼：確認還在條款與保險法的期限內、"
        "準備清償欠繳的保險費（以及條款要求的利息或其他費用）、"
        "超過六個月的話還要準備健康告知或核保要求的證明文件。\n"
        "每一個期限、每一個條文、每一個天數都必須是工具回傳的，想不起來或工具沒回傳就說這部分需要查證，"
        "寫錯的期限比不寫更傷害保戶。\n\n"
        "工具回傳 _identity_required 時，表示保戶尚未完成身分核對，material 裡沒有他個人的"
        "lapsed_policies、reinstatement_clauses、_allowed_clauses，但 statutory_floor 一定還在——"
        "保險法對復效的最低保障是公開的規定，不用核對身分也能先講。"
        "此時先照 statutory_floor 說明法律定的期限與健康告知規則，"
        "再說明要查他這張保單自己的停效日期、距今天數、以及契約自己寫的復效期限，需要先核對身分，"
        "請他提供身分證字號。不要憑空講任何關於他保單的內容，也不要假裝已經查過他的保單。"
    ),
    tools=("lapsed_policies", "reinstatement_clauses", "statutory_floor"),
    tools_module="policydesk.agent.scenarios.reinstate",
    quick_replies=("復效需要準備哪些文件？", "如果超過期限還有機會嗎？", "我想了解這張保單的保障內容"),
    transitions=("explain_cover", "policy_overview"),
)
