"""
PAYMENT: 繳費、下次繳費日、寬限期.

The most-asked question at an insurance counter, and until the `premium_payment` table
landed today the desk had nothing to read for it. `billing_summary` computed an annual
figure from the rate card — what a policy costs, never what anybody paid — so 我這期繳了嗎
had no answer and 我下次什麼時候繳 had no date.

## Why the grace period is the whole scenario

An unpaid instalment is not a bill, it is a countdown. 保險法 §116 I gives the insurer
thirty days from a 催告 reaching the customer, and at the end of them the contract stops. A
customer who rings up about a missed payment is inside that window and usually does not know
it exists — so the reply that just states a balance has told them the least useful true
thing available.

The date the thirty days run from is **the day the notice arrived**, not the due date and
not today. This desk does not hold that date. So it says what the period is and what it runs
from, and asks — the same shape `cooling_off` takes for 保險單送達的翌日, and for the same
reason: computing a deadline from a date the database does not have produces a confident
wrong answer about whether someone still has cover.

## What it may not do

It may not say a policy is or is not in force as a conclusion. `policy.lapsed_at` is a
recorded fact and gets reported; a policy with an unpaid instalment and no lapse date is
inside a window whose end nobody here can compute.
"""

from typing import TYPE_CHECKING, Any

from policydesk.agent import statute, tools
from policydesk.agent.scenario_base import Scenario, gather_tools

if TYPE_CHECKING:
    from datetime import date

    from policydesk.core.db import Database

STATUTE_SCOPE: list[str] = ["insurance_act"]

GRACE_QUERY = "保險費到期未交付催告到達後屆三十日仍不交付保險契約之效力停止"
"""§116's own words, not the customer's. 寬限期 and 繳費 appear nowhere in it, and a query
built from what the customer says reaches 第140條 保單紅利 instead — measured today on the
same corpus, which is why the stop-word list and this constant both exist."""

GRACE_ARTICLE = 116
"""保險法 §116 — 保險費到期未交付的效果. Named so the filter below reads as a decision."""

MODE_LABEL: dict[str, str] = {
    "annual": "年繳", "semiannual": "半年繳", "quarterly": "季繳", "monthly": "月繳",
}


@tools.requires_identity
async def payment_state(db: Database, member_id: int, *, today: date) -> list[dict[str, Any]]:
    """
    Read where each policy stands on its premiums.

    Args:
        db: The database.
        member_id: Whose book.
        today: The date to measure against.

    Returns:
        One row per policy: how it is paid, what one instalment costs, how far it is paid
        up to, what the next instalment is, and whether one is outstanding.

    `overdue_days` counts from the due date because that is the date this desk holds. It is
    NOT the thirty days §116 counts, which start when the 催告 arrives — the injection says
    so, because a number beside a policy is read as the number that matters.

    `next_due_at` is stepped from `paid_through` by the contract's own mode, and only when
    nothing is outstanding — with an unpaid instalment on the book, the next payment due is
    that one, not a date after it. The scenario offers 我想知道下一期什麼時候繳 as a
    one-tap reply and had no column to answer it with: the query returned the oldest
    unpaid and the newest paid, both in the past, leaving the model to either miss the
    question or produce a date from arithmetic no tool had done.

    `no_record` separates a policy nobody has recorded a payment for from one paid up to
    date. Both have every payment column NULL, and they are opposite facts. The state is
    reachable on an in-place upgrade: the migration adds `paid_through` without
    backfilling it, and `furnish` only writes history for a member as they enrol.

    """
    return await db.fetch(
        """SELECT po.policy_id, po.policy_number, pr.name AS product_name,
                  po.premium_mode, po.paid_through, po.lapsed_at,
                  (po.lapsed_at IS NOT NULL AND po.lapsed_at <= $2::date) AS is_lapsed,
                  due.due_at AS unpaid_due_at,
                  due.amount AS unpaid_amount,
                  ($2::date - due.due_at) AS overdue_days,
                  last.amount AS instalment,
                  last.paid_at AS last_paid_at,
                  (po.paid_through IS NULL AND due.due_at IS NULL) AS no_record,
                  CASE WHEN po.lapsed_at IS NULL AND due.due_at IS NULL AND po.paid_through IS NOT NULL
                       THEN (po.paid_through + CASE po.premium_mode
                                WHEN 'annual' THEN interval '1 year'
                                WHEN 'semiannual' THEN interval '6 months'
                                WHEN 'quarterly' THEN interval '3 months'
                                ELSE interval '1 month' END)::date
                  END AS next_due_at
           FROM policy po
           JOIN product pr USING (product_id)
           LEFT JOIN LATERAL (
               SELECT pp.due_at, pp.amount FROM premium_payment pp
               WHERE pp.policy_id = po.policy_id AND pp.paid_at IS NULL
               ORDER BY pp.due_at LIMIT 1
           ) due ON true
           LEFT JOIN LATERAL (
               SELECT pp.amount, pp.paid_at FROM premium_payment pp
               WHERE pp.policy_id = po.policy_id AND pp.paid_at IS NOT NULL
               ORDER BY pp.due_at DESC LIMIT 1
           ) last ON true
           WHERE po.member_id = $1::bigint
           ORDER BY due.due_at NULLS LAST, po.effective_at DESC""",
        [member_id, today],
    )


@tools.requires_identity
async def payment_history(db: Database, member_id: int, limit: int = 8) -> list[dict[str, Any]]:
    """
    Read the instalments themselves, newest first.

    Args:
        db: The database.
        member_id: Whose book.
        limit: Most rows to return.

    Returns:
        Each instalment with its policy, what it was for, when it fell due and when it was
        paid. `paid_at` NULL is the outstanding one.

    Bounded, because a monthly policy eight years old is ninety-six rows and a customer
    asking 我這期繳了嗎 wants the last few — the desk pane is where a full ledger belongs.

    """
    return await db.fetch(
        """SELECT pp.due_at, pp.paid_at, pp.amount, pp.method,
                  po.policy_number, pr.name AS product_name
           FROM premium_payment pp
           JOIN policy po USING (policy_id)
           JOIN product pr USING (product_id)
           WHERE po.member_id = $1::bigint
           ORDER BY pp.due_at DESC
           LIMIT $2::int""",
        [member_id, limit],
    )


async def grace_rule(db: Database, *, retriever: Any | None = None) -> list[dict[str, Any]]:
    """
    Read what the law says about a premium that was not paid.

    Args:
        db: The database.
        retriever: The shared index, when one is open.

    Returns:
        §116's provisions, with the citation the reply copies.

    Public: everyone is subject to it, and a customer asking 沒繳會怎樣 gets the answer
    before proving who they are. What needs the ID is whether it is happening to *them*.

    """
    rows = await statute.search_statute(db, GRACE_QUERY, STATUTE_SCOPE, limit=3, retriever=retriever)
    # Filtered to §116. `search_statute` widens a hit to its sibling paragraphs, which is
    # right — §116 has eight of them and the customer needs II and III as much as I. It
    # also brought back §120, 保單借款, whose vocabulary overlaps because a policy loan
    # can stop a contract too. A different provision under this key reads as more support
    # for the same sentence, which is the failure the beneficiary scenario was split for.
    return [
        {
            "citation": statute.citation(row),
            "statute": row["statute_name"],
            "doc_id": row["doc_id"],
            "verbatim": row["verbatim"],
        }
        for row in rows
        if row["article"] == GRACE_ARTICLE
    ]


TOOLS: dict[str, Any] = {
    "payment_state": payment_state,
    "payment_history": payment_history,
    "grace_rule": grace_rule,
}
"""The scenario's tools. `grace_rule` carries no mark, so it survives the gate and an
unverified customer still learns what a missed premium means."""


async def gather(
    db: Database,
    params: dict[str, str],  # noqa: ARG001 - this scenario takes none; the signature is shared
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
        params: What the router collected. None needed — a customer asking about their own
            premiums has already said everything the query needs.
        member_id: Whose book.
        today: The date to measure overdue against.
        retriever: The shared index, passed through to the statute search.
        allowed: Which tools may run. None runs all of them.

    Returns:
        Material for the injection, by tool name.

    """
    from datetime import UTC, datetime

    when = today or datetime.now(UTC).date()
    factories: dict[str, Any] = {"grace_rule": lambda: grace_rule(db, retriever=retriever)}
    if member_id is not None:
        factories["payment_state"] = lambda: payment_state(db, member_id, today=when)
        factories["payment_history"] = lambda: payment_history(db, member_id)
    return await gather_tools(factories, allowed=allowed)


PAYMENT = Scenario(
    name="payment",
    display_name="繳費與寬限期",
    summary="查各張保單繳到哪、有無欠繳與寬限期規則",
    description=(
        "保戶問繳費相關的事情時使用："
        "我這期繳了嗎、下次什麼時候繳、一期要繳多少、我忘記繳了會怎樣、寬限期還有多久、"
        "沒繳保單會不會失效、"
        # billing pushes these here by name and this list did not take them, so the
        # sentence 有月繳或季繳，差別在哪 was refused by one scenario and unrecognised by
        # the other. `payment_state` reads `premium_mode` and MODE_LABEL renders it —
        # the answer was ready and the route was not. billing's own quick reply
        # 我想了解可以改成月繳嗎 lands here too.
        "我是月繳還是年繳、有沒有月繳季繳、兩種繳別差在哪、我想改繳別。"
        "問的是總共一年繳多少或想比較商品費率時不要選這個情境。"
    ),
    injection=(
        "payment_state 是空的時候，代表這位保戶名下沒有保單，不是系統查不到；"
        "payment_state 某一列的 unpaid_due_at 是空的、no_record 為假、而且 is_lapsed 也為假時，"
        "代表那張保單每一期都繳齊了，那是好消息，直接告訴他目前沒有欠繳。"
        "這句話只能對那一列講，不可以因為某幾張沒有欠繳就說「您目前沒有欠繳」——"
        "同一位保戶名下可能有另一張正在欠繳或已經停效。\n"
        "no_record 為真是另一回事：那張保單沒有任何繳費紀錄可查，不是繳齊也不是欠繳。"
        "照實說本櫃台查不到那張保單的繳費紀錄，請他洽原業務員或客服，"
        "不要說他已經繳清，也不要說他欠繳。\n\n"
        "你正在回答保戶自己的繳費狀況。逐張說明，每一張講清楚四件事：繳別（premium_mode，"
        "年繳、半年繳、季繳、月繳照 MODE_LABEL 的說法講）、一期金額（instalment）、"
        "已繳到哪一天（paid_through）、以及有沒有未繳的一期（unpaid_due_at）。\n"
        "問到下一期什麼時候繳，就照 next_due_at 那一欄回答，那是依繳別推出來的應繳日。"
        "next_due_at 是空的時候不要自己推算日期：有未繳的一期時下一期就是那一期，"
        "保單已停效或查無繳費紀錄時則沒有下一期可談。\n\n"
        "**有未繳的一期時，這才是重點，不是餘額。** 照 grace_rule 回傳的條文說明："
        "保險費到期未交付，經催告到達後屆三十日仍不交付，契約效力停止。"
        "引用時照 citation 欄位一字不差標註，例如〔保險法 第116條第1項〕。\n\n"
        "那三十天從催告送達的翌日起算，不是從到期日起算，也不是從今天起算。"
        "本櫃台沒有催告送達日這筆資料，所以絕對不要算出一個剩幾天或哪一天截止的答案。"
        "說明期間怎麼算、從什麼時候起算，然後請他確認收到催告通知的日期。\n"
        "overdue_days 是從到期日算到今天的天數，只能拿來說明已經逾期多久，"
        "不可以拿來當作寬限期剩幾天。\n\n"
        "已經停效的保單（is_lapsed 為真）照實說目前不提供保障，並告訴他復效要另外辦理，"
        "細節請他回頭問復效的事。不要在這裡說明復效條件。\n\n"
        "不可以說保單一定會失效或一定不會失效，也不可以承諾任何寬限或通融——"
        "催告與停效由公司依條款作業，你能做的是把規則和他目前的狀態攤開。\n"
        "金額一律照工具回傳的數字說，不要自己加總或換算。"
    ),
    tools=("payment_state", "payment_history", "grace_rule"),
    tools_module="policydesk.agent.scenarios.payment",
    params=(),
    # 我想知道下一期什麼時候繳？ was here, and the injection now requires every reply to
    # state `next_due_at` per policy — so the chip spent a tap on lines already on screen.
    quick_replies=("逾期了還能補繳嗎？", "可以改成年繳嗎？", "催告通知會寄到哪裡？"),
    transitions=("reinstate", "billing", "soothe"),
)
