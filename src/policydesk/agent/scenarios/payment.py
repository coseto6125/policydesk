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
not today. `premium_payment.notice_arrived_at` holds it when the 催告 is on record, and
`payment_state` then computes `grace_ends_at` and `grace_days_left` in SQL — a figure from a
tool, which the desk may quote. When it is not on record the desk says what the period is
and what it runs from, and asks — the same shape `cooling_off` takes for 保險單送達的翌日,
and for the same reason: a deadline the model computes from the due date is a confident
wrong answer about whether someone still has cover. Measured: handed §116 verbatim, Haiku
did exactly that 3/3 until the row carried the figure.

## What it may not do

It may not say a policy is or is not in force as a conclusion. `policy.lapsed_at` is a
recorded fact and gets reported; a policy with an unpaid instalment and no lapse date is
inside a window whose end nobody here can compute.
"""

from typing import TYPE_CHECKING, Any

from policydesk.agent import statute, tools
from policydesk.agent.scenario_base import Param, Scenario, gather_tools
from policydesk.agent.tools import public

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

GRACE_DAYS = 30
"""§116 I: 催告到達後屆三十日. Counted from the day after the notice arrives, so the last
day is arrival + 30. Computed here, in SQL, from a date on record — never by the model."""

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
    rows = await db.fetch(
        """SELECT po.policy_id, po.policy_number, pr.name AS product_name,
                  po.premium_mode, po.paid_through, po.lapsed_at,
                  (po.lapsed_at IS NOT NULL AND po.lapsed_at <= $2::date) AS is_lapsed,
                  due.due_at AS unpaid_due_at,
                  due.amount AS unpaid_amount,
                  ($2::date - due.due_at) AS overdue_days,
                  due.notice_arrived_at,
                  (due.notice_arrived_at + $3::int)::date AS grace_ends_at,
                  (due.notice_arrived_at + $3::int - $2::date) AS grace_days_left,
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
               SELECT pp.due_at, pp.amount, pp.notice_arrived_at FROM premium_payment pp
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
        [member_id, today, GRACE_DAYS],
    )
    # The row says what it means, so the prompt does not have to. `no_record` and
    # paid-up both leave every payment column NULL and are opposite facts; a model told
    # the difference in a paragraph of prose got it right, and a model handed a row that
    # says `status: paid_up 每期都已繳` needs no paragraph. Measured with the validate
    # skill: with `grace_days_left` on the row, the control quoted it 3/3 with no rule.
    for row in rows:
        row["mode"] = MODE_LABEL.get(row["premium_mode"], row["premium_mode"])
        row["status"] = (
            "lapsed 已停效" if row["is_lapsed"]
            else "unpaid 有一期未繳，催告已送達" if row["unpaid_due_at"] and row["notice_arrived_at"]
            else "unpaid 有一期未繳，尚無催告送達紀錄" if row["unpaid_due_at"]
            else "no_record 查無繳費紀錄，請洽業務員或客服" if row["no_record"]
            else "paid_up 每期都已繳"
        )
    return rows


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


@public
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


PAYING_HEADING = "交付|寬限"
"""The articles about how a premium is paid: 保險責任的開始及交付保險費 and 第二期以後保險費的
交付、寬限期間及契約效力的停止, in their several spellings. Matched on the heading rather
than retrieved: no product in the corpus carries a 繳費方式變更 article — the 123 clauses
that hold 變更 beside 繳費方式 are 名詞定義 and 復效 — and the ranked search puts
exclusions first for any topic, which buried the 交付 article under a limit of four."""


@tools.requires_identity
async def mode_change_rule(db: Database, member_id: int) -> list[dict[str, Any]]:
    """
    Read what each of the member's contracts says about how its premiums are paid.

    Args:
        db: The database.
        member_id: Whose book.

    Returns:
        Clause rows for the contracts still in force, each naming its product and the
        policy it sits under. Empty for a book whose contracts carry no such article —
        single-premium products and riders that follow their main contract.

    A customer asking 可以改成年繳嗎 was answered with the ledger three times running,
    because this scenario held nothing else to say about a mode. The change itself is a
    契約變更 the company processes; what the desk can show is the contract's own words on
    how the premium is paid — where a mode has a consequence the customer should hear
    before switching: 年繳 gets a 催告 before its thirty days start, 月繳 does not.

    """
    return await db.fetch(
        """SELECT po.policy_number, c.product_id, c.clause_id, c.heading, c.verbatim, c.page,
                  pr.name AS product_name
           FROM policy po
           JOIN contract_clause c USING (product_id)
           JOIN product pr USING (product_id)
           WHERE po.member_id = $1::bigint AND po.lapsed_at IS NULL AND c.heading ~ $2::text
           ORDER BY po.policy_number, c.clause_id""",
        [member_id, PAYING_HEADING],
    )


TOOLS: dict[str, Any] = {
    "payment_state": payment_state,
    "payment_history": payment_history,
    "grace_rule": grace_rule,
    "mode_change_rule": mode_change_rule,
}
"""The scenario's tools. `grace_rule` carries no mark, so it survives the gate and an
unverified customer still learns what a missed premium means."""


def _key(number: str) -> str:
    """
    Reduce a policy number as typed to what identifies it.

    「CL9926-658746」, 「cl9926 658746」 and 「658746」 are one policy to the customer, and the
    last of these matches by suffix.
    """
    return "".join(ch for ch in number.upper() if ch.isalnum())


def _scoped(facts: dict[str, Any], number: str) -> dict[str, Any]:
    """
    Keep the rows of the policy the customer named.

    Args:
        facts: What the tools returned, by tool name.
        number: The policy number the router collected. Empty scopes nothing.

    Returns:
        The same material, each list of policy rows cut to the named policy. A number that
        matches none of a tool's rows leaves that tool's rows whole, so the reply can say
        which policies exist rather than that none does.

    """
    wanted = _key(number)
    if not wanted:
        return facts
    scoped: dict[str, Any] = {}
    for tool, rows in facts.items():
        if isinstance(rows, list) and rows and isinstance(rows[0], dict) and "policy_number" in rows[0]:
            kept = [r for r in rows if _key(r["policy_number"]).endswith(wanted)]
            scoped[tool] = kept or rows
        else:
            scoped[tool] = rows
    return scoped


async def gather(
    db: Database,
    params: dict[str, str],
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
        params: What the router collected. `policy_number` names one policy when the
            customer did; the rows are then cut to it, so a question about one policy is
            answered about that policy.
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
        factories["mode_change_rule"] = lambda: mode_change_rule(db, member_id)
    return _scoped(await gather_tools(factories, allowed=allowed), params.get("policy_number", ""))


PAYMENT = Scenario(
    name="payment",
    display_name="繳費與寬限期",
    summary="查各張保單繳到哪、有無欠繳與寬限期規則",
    # Routing material. The examples are the customer's own words, in the customer's
    # language; the rule around them is for the router.
    description=(
        "Use for anything about paying premiums: whether an instalment is paid, when the next one "
        "is due, how much one is, what a missed payment leads to, the grace period, whether the "
        "policy lapses, which mode a policy is paid by (年繳, 半年繳, 季繳, 月繳), how the modes "
        "differ, and a change of mode. Examples: 「我這期繳了嗎」「下次什麼時候繳」「我忘記繳了會怎樣」"
        "「寬限期還有多久」「我是月繳還是年繳」「可以改成年繳嗎」「這張改月繳」. "
        "A message that only names a policy number, after a payment question, continues that "
        "question in this scenario. "
        "The premium total for a year, and a comparison of product rates, belong to other scenarios."
    ),
    # Three sentences. The rows say what they mean (`status`, `mode`, `grace_days_left`),
    # the router's rules already cover promises, figures and filing, and the clauses say
    # the rest. What is left is the one state a row cannot answer for itself: a 催告 with
    # no arrival on record.
    #
    # Measured 2026-09-05 (validate-prompt-rules, Haiku, isolated, n=3 per arm). Before
    # `grace_days_left` existed, the control handed §116 verbatim computed 「還剩 19 天」
    # from the due date 3/3. With the figure on the row it quoted the figure 3/3 with no
    # rule. With the figure NULL and the status saying 尚無催告送達紀錄, the control
    # asserted 「尚未送出催告」 3/3 — a claim the desk cannot make — and the last sentence
    # below turned that into a question 3/3.
    injection=(
        "payment_state: an empty list means the customer holds no policy, and a row's status "
        "says where that policy stands. mode_change_rule holds what each contract says about "
        "how its premiums are paid, by policy_number; when no clause covers a change of mode, "
        "say the contract holds no such article.\n"
        "Cite grace_rule as its citation field is written, for example 〔保險法 第116條第1項〕, "
        "and a clause as [clause_id].\n"
        "When grace_days_left is empty, no 催告 arrival is on record, which is not the same as "
        "no 催告 sent: state that the thirty days in 保險法 §116 run from the day after it "
        "arrives, and ask the customer when it arrived."
    ),
    tools=("payment_state", "payment_history", "grace_rule", "mode_change_rule"),
    tools_module="policydesk.agent.scenarios.payment",
    params=(
        Param(
            name="policy_number",
            description="The policy the customer named, as they wrote it, for example CL9926-658746 or 658746.",
            example="CL9926-658746",
            when_unsaid="Empty when the customer named no policy.",
        ),
    ),
    # 我想知道下一期什麼時候繳？ was here, and the injection now requires every reply to
    # state `next_due_at` per policy — so the chip spent a tap on lines already on screen.
    quick_replies=("逾期了還能補繳嗎？", "可以改成年繳嗎？", "催告通知會寄到哪裡？"),
    transitions=("reinstate", "billing", "soothe"),
)
