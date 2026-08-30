"""
The scenario for 我後悔了可以退嗎, 剛買的保單可以取消嗎, 有沒有猶豫期.

The right lives in the contract, not the statute. 保險法施行細則 has seventeen articles
and none of them is a cooling-off clause — art.4 is about when a policy may be issued
against a part payment, a fact checked against `statute_article` and not found the way
this module first assumed. What every 保單 actually carries is a 契約撤銷權 clause: 239
of 387 on-sale products state one, verbatim, in `clause`.

## Two halves, and which one needs a name

**The period is public.** Any customer can be told what a representative contract
says before proving who they are — an insurer's own printed terms are not a secret,
the same reasoning `browse_products` already answers on. `cooling_off_clause` reads
one, unmarked.

**Whether it still applies to *them* is not.** That needs their own policy: which
product they hold, and when it took effect. `member_rescission` reuses
`policydesk.agent.tools.list_policies` — already `@requires_identity` — rather than
querying `policy` again, so the mark travels with the read instead of being declared a
second time on a duplicate query.

## The number is not always ten

238 of 239 on-sale rescission clauses say 十日; one says 十四日. A scenario that states
"十天" as a fact would be right 99.6% of the time and wrong for a customer holding that
one product — which is a worse failure than being generic, because it is confidently
wrong. So `cooling_off_clause`'s public answer is one representative clause, named as
representative, and `member_rescission`'s confirmed answer is the customer's own
clause, verbatim. Neither path lets the model state a day count it did not read off a
row.

## The date arithmetic trap

The clock starts at 收到保單 — 保險單送達的翌日起算 — which is not `effective_at` and is
in no column this database has. `member_rescission` hands the model `effective_at` and
`days_in_force` as context (a policy that took effect 400 days ago is very unlikely to
still be within any version of this window), never as the answer. The injection forbids
computing a deadline outright: what it may do is state the period the clause names and
ask when the customer actually received the policy in hand.
"""

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from policydesk.agent import tools
from policydesk.agent.scenario_base import Scenario, gather_tools
from policydesk.agent.tools import requires_identity

if TYPE_CHECKING:
    from policydesk.core.db import Database

_HEADINGS = ("契約撤銷權", "附約撤銷權")


async def cooling_off_clause(db: Database, limit: int = 1) -> list[dict[str, Any]]:
    """
    Show a representative 契約撤銷權 clause, for anyone.

    Args:
        db: The database.
        limit: Most rows to return.

    Returns:
        Clause rows from on-sale products, ordered by `product_id` for a stable pick
        rather than "the" answer — the wording is not identical everywhere, so this is
        one contract's own terms, not a summary of all of them.

    Public: on-sale products' own printed terms are not this customer's book — the same
    standing `catalogue_sample` already has. What it cannot say is whether this period,
    or this product, is the one in the customer's own hand; that is `member_rescission`.

    """
    return await db.fetch(
        """SELECT c.product_id, c.clause_id, c.heading, c.verbatim, c.page, p.name AS product_name
           FROM clause c JOIN product p USING (product_id) JOIN catalog_entry ce USING (product_id)
           WHERE c.heading = '契約撤銷權' AND ce.on_sale
           ORDER BY c.product_id
           LIMIT $1::int""",
        [limit],
    )


LONGEST_WINDOW_DAYS = 14
"""The longest 撤銷期間 any contract in this corpus grants. Ten and fourteen days both
appear, and the clause's own verbatim is what a reply quotes — this constant only bounds
the one inference the dates support."""


@requires_identity
async def member_rescission(db: Database, member_id: int, *, today: date) -> list[dict[str, Any]]:
    """
    Read this customer's own 契約撤銷權/附約撤銷權 clause, with the policy it sits on.

    Args:
        db: The database.
        member_id: Whose policies.
        today: The date to judge currency against.

    Returns:
        One row per policy that carries either heading: the clause verbatim, the
        policy it belongs to, and `effective_at`/`days_in_force` from
        `policydesk.agent.tools.list_policies` — context for whether the window is
        plausibly still open, never the deadline itself, because the clock starts at
        收到保單 and this database has no such column. A policy with no matching
        clause is silently absent rather than backfilled from another product's text.

    """
    policies = await tools.list_policies(db, member_id, today=today)
    if not policies:
        return []
    product_ids = [p["product_id"] for p in policies]
    clauses = await db.fetch(
        """SELECT c.product_id, c.clause_id, c.heading, c.verbatim, c.page, p.name AS product_name
           FROM clause c JOIN product p USING (product_id)
           WHERE c.product_id = ANY($1::text[]) AND c.heading = ANY($2::text[])
           ORDER BY p.name""",
        [product_ids, list(_HEADINGS)],
    )
    by_product = {p["product_id"]: p for p in policies}
    return [
        {
            **row,
            "policy_number": by_product[row["product_id"]]["policy_number"],
            "effective_at": by_product[row["product_id"]]["effective_at"],
            "days_in_force": (days := by_product[row["product_id"]]["days_in_force"]),
            # The one thing about the window that IS computable. Delivery happens on or
            # after the effective date, so days-since-delivery never exceeds
            # `days_in_force`: a policy in force for fewer days than the longest window
            # in the corpus is certainly still inside its own. The converse does not
            # hold — a policy in force 600 days could have been delivered last week, so
            # false means unknown, never expired, and the field is named for what it
            # asserts rather than for the guess it would license.
            "certainly_within_window": days is not None and days <= LONGEST_WINDOW_DAYS,
        }
        for row in clauses
    ]


TOOLS: dict[str, Any] = {"cooling_off_clause": cooling_off_clause, "member_rescission": member_rescission}
"""The scenario's tools, for the executor's dispatch.

`cooling_off_clause` carries no mark: it reads `clause` joined to `catalog_entry`, no
member row anywhere in it, so `policydesk.agent.tools.permitted` never withholds it.
`member_rescission` is marked directly, because it is a new function here rather than a
reuse of an already-marked one — the mark has to be declared exactly once, on whichever
function is the one that actually reaches `policy`.
"""




async def gather(
    db: Database,
    params: dict[str, str],  # noqa: ARG001 - no parameter this scenario needs; kept for the shared contract
    *,
    member_id: int | None = None,
    today: date | None = None,
    retriever: Any | None = None,  # noqa: ARG001 - part of the shared module contract
    allowed: frozenset[str] | None = None,
    **_: Any,
) -> dict[str, Any]:
    """
    Run this scenario's tools.

    Args:
        db: The database.
        params: Unused — nothing here needs a customer-supplied value, the same way
            `policy_overview` needs none.
        member_id: Whose own clause to add.
        today: The date to age the member's policies against.
        retriever: Unused here. Accepted because every scenario module is called the
            same way.
        allowed: Which tool names the executor's identity gate permits this turn. None
            permits both — what a direct call gets. `member_rescission` withheld here
            means `policy` is never read; this module never sets `_identity_required`
            itself, because the executor already knows it withheld something.

    Returns:
        `cooling_off_clause` when `allowed` permits it — normally always, since it names
        no member. `member_rescission` when `allowed` permits it and a member_id was
        given. `_allowed_clauses` names every `clause_id` actually returned, so the
        reply may cite `[art.N]` against exactly those rows and nothing invented.

    """
    factories: dict[str, Any] = {"cooling_off_clause": lambda: cooling_off_clause(db)}
    if member_id is not None:
        factories["member_rescission"] = lambda: member_rescission(
            db, member_id, today=today or datetime.now(UTC).date()
        )
    facts = await gather_tools(factories, allowed=allowed)
    # Built from what actually came back, so a withheld tool contributes no clause id the
    # reply could cite and the checker could not then find.
    facts["_allowed_clauses"] = frozenset(
        row["clause_id"] for rows in facts.values() for row in rows if isinstance(rows, list)
    )
    return facts


COOLING_OFF = Scenario(
    name="cooling_off",
    display_name="契約撤銷權",
    summary="說明猶豫期從哪天起算、能不能撤銷",
    description=(
        "保戶問「我後悔了可以退嗎」「剛買的保單可以取消嗎」「有沒有猶豫期」這類想撤銷剛簽的保單時使用。"
        "已經投保一段時間、問的是解約金或停效復效時不要選這個情境。"
    ),
    injection=(
        "你正在說明保戶的契約撤銷權（俗稱猶豫期）。\n\n"
        "撤銷期間不是固定十天——工具回傳的 verbatim 裡寫的天數才是天數，"
        "有的商品是十日、也有十四日的，一律照該筆 verbatim 原文說，不可以憑印象講十天。\n\n"
        "期間起算點是「保險單送達的翌日」，不是保單生效日 effective_at，"
        "這個送達日期系統裡沒有存，你也不知道。"
        "所以你不可以自己算出撤銷期限是哪一天，也不可以說「已經過期了」或「還來得及」——"
        "你只能照 verbatim 說明期間是幾天、從什麼時候起算，然後反問保戶是什麼時候實際收到保單的。"
        "effective_at 與 days_in_force 只是參考：保單生效很久了通常代表已經收到保單一段時間，"
        "但這不是送達日，不能拿來當作撤銷期限的計算依據。\n\n"
        "撤銷後的效果照 verbatim 說明即可，例如公司無息退還已繳保險費；不要自己加碼或另外承諾。\n\n"
        "cooling_off_clause 回傳的是本公司在售商品的一般約定範例，不是保戶自己保單的條款，"
        "說明時要講清楚這是「一般約定」，實際內容以保戶自己保單為準。"
        "member_rescission 回傳的才是保戶自己保單的條款，兩者都有的話優先使用 member_rescission。"
        "member_rescission 裡沒有出現的保單，代表查無撤銷權條款，不要用另一張保單的內容套用過去。\n\n"
        "**保戶名下不只一張保單時，先確定他問的是哪一張，再說「您這張」。**\n"
        "他說「上禮拜買的」「剛簽的」而 member_rescission 每一列的 certainly_within_window "
        "都是假的時候，代表他名下沒有任何一張是最近生效的。此時照實說明：把每一張的保單號碼與"
        "生效日列出來，問他指的是哪一張，不要挑其中一張當作「您這張保單」。\n"
        "certainly_within_window 為真，代表那張保單生效還沒超過最長的撤銷期間，一定還在期限內；"
        "為假只代表無法判斷，不代表已經過期——送達日還是只有他知道。\n\n"
        "條號一律照工具回傳的 clause_id 原樣標註，寫在該句句末的方括號內，例如 [art.7]。\n\n"
        "工具回傳 _identity_required 時，表示保戶尚未完成身分核對，所以你拿不到他自己保單的條款。"
        "此時只用 cooling_off_clause 的一般約定回答期間是幾天、從什麼時候起算，"
        "並說明這是一般約定，要確認他自己這張保單的條款與是否還在期限內，需要先核對身分，"
        "請他提供身分證字號。不要憑空猜測他的保單內容。"
    ),
    tools=("cooling_off_clause", "member_rescission"),
    tools_module="policydesk.agent.scenarios.cooling_off",
    quick_replies=("我這張保單也有這個權利嗎？", "撤銷之後保費怎麼退？", "我想確認我的保單什麼時候生效"),
    transitions=("policy_overview",),
)
