"""
The scenario for a customer who wants a number: 這張要多少錢, 我這個年紀保費多少.

Two figures answer that, and they come from different places.

**The rate is public.** `unit_premium` and `unit_label` are the same numbers printed
on the catalogue any visitor can ask to see — this desk already answers 這個商品線多少
錢 to someone who has not proved who they are, in `browse_products`. Refusing the same
number here because the question is phrased as "for me" would be refusing the one part
of the answer that never was personal.

**The fit is not.** Whether *this* customer's age falls inside the band, and whether
their occupation clears the ceiling, needs their insurance age and occupation class —
`policydesk.agent.tools.member_underwriting`, already `@requires_identity`. So the one
new tool here is the rate lookup; the member read is borrowed rather than duplicated,
and it carries its mark with it wherever it is called from.

## Two traps this desk keeps failing

**A rider quoted alone.** `requires_main` true means the row has no standalone annual
premium — it is a percentage or a flat add-on that only means something on top of a
main contract. Multiplying its `unit_premium` by a desired amount and presenting the
result as *the* premium quotes a price nobody can actually pay, because there is no
policy to buy it on its own.

**足歲 where 保險年齡 belongs.** Every band here is written in 保險年齡
(`policydesk.synthetic.person.insurance_age`), the age the plain calendar gets wrong by
a year exactly at the point that matters — someone 34 years and 7 months old is 35 for
underwriting. `member_underwriting` already returns the right one; this module never
computes an age of its own.

## What this is not

試算, not 核保結果. Nothing here reads a health declaration or an underwriter's
judgment, both of which can move the number this scenario states. The injection says so
on every turn, because a figure headed 試算 and read as final is a complaint waiting to
happen.
"""

import re
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from policydesk.agent import tools
from policydesk.agent.scenario_base import Param, Scenario, gather_tools
from policydesk.agent.tools import LINES
from policydesk.bootloader import logger
from policydesk.skills.calculator import CalculationError, calculate

if TYPE_CHECKING:
    from policydesk.core.db import Database

_WAN = re.compile(r"([\d,]+)\s*萬元")
_YUAN = re.compile(r"([\d,]+)\s*元")


def _unit_base(label: str) -> int:
    """
    Read the NT dollar amount one `unit_premium` buys, out of the label's own text.

    Args:
        label: `catalog_entry.unit_label`, e.g. `每 10 萬元保額` or `每單位`.

    Returns:
        100_000 for 每 10 萬元保額, 1_000_000 for 每 100 萬元保額, 1_000 for 每日
        1,000 元住院日額, 1 for 每單位 — where the whole `unit_premium` is already
        the price and no amount scales it.

    萬 is tried before a bare 元, because 每 10 萬元保額 contains a 元 of its own
    right after 萬 — matching that one first would read the label as costing NT$1
    per unit and multiply a real rate by ten thousand.

    """
    if m := _WAN.search(label):
        return int(m.group(1).replace(",", "")) * 10_000
    if m := _YUAN.search(label):
        return int(m.group(1).replace(",", ""))
    return 1


def _as_amount(raw: str) -> int | None:
    """
    Read the coverage amount the router collected.

    Args:
        raw: What the model put in the `amount` parameter.

    Returns:
        The amount in NT dollars, or None when the customer named no figure. None
        means the rate is shown without an estimate, rather than against a guessed
        amount nobody asked to be quoted on.

    """
    try:
        value = int(raw.strip().replace(",", ""))
    except (AttributeError, ValueError):
        return None
    return value if value > 0 else None


async def product_rate(
    db: Database, line: str, keyword: str = "", amount: int | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    """
    Find what an on-sale product costs, for anyone.

    Args:
        db: The database.
        line: Which product line, one of `policydesk.agent.tools.LINES`.
        keyword: A fragment of the product's own name, when the customer means one in
            particular. Empty matches every on-sale product on the line.
        amount: The coverage the customer wants, in NT dollars, at the product's own
            unit. None returns the published rate with no estimate attached.
        limit: Most products to return.

    Returns:
        Catalog rows carrying the issue-age band and occupation ceiling regardless of
        whether they fit this customer — a product outside the band still comes back,
        with the band that would have to change, rather than being dropped silently.
        `estimated_premium` and `estimated_basis` are added when `amount` is given and
        the row is not a rider, through `calculate`, so the figure is never one this
        module computed itself in prose.

    Public: the same status `policydesk.agent.tools.catalogue_sample` already has. A
    rate card an insurer publishes is not this customer's book, so a visitor who has
    not proved who they are can still be told what a product costs — what they cannot
    be told is whether it fits them, which is what `member_underwriting` is for.

    """
    if line not in LINES:
        logger.warning("unsellable_line", line=line)
        return []
    rows = await db.fetch(
        """SELECT p.product_id, p.name, p.line, p.attachment, ce.unit_premium, ce.unit_label,
                  ce.issue_age_min, ce.issue_age_max, ce.max_occupation, ce.requires_main
           FROM catalog_entry ce JOIN product p USING (product_id)
           WHERE ce.on_sale AND p.line = $1::text
             AND ($2::text = '' OR p.name ILIKE '%' || $2::text || '%')
           ORDER BY ce.unit_premium ASC
           LIMIT $3::int""",
        [line, keyword.strip(), limit],
    )
    if amount is None:
        return rows
    priced: list[dict[str, Any]] = []
    for row in rows:
        row = dict(row)
        # A rider has no standalone premium to estimate — attaching one here is the
        # exact thing this scenario exists to stop the model from doing on its own.
        if not row["requires_main"]:
            try:
                computed = calculate(f"{row['unit_premium']} * {amount} / {_unit_base(row['unit_label'])}")
            except CalculationError as exc:
                logger.warning("quote_estimate_failed", product_id=row["product_id"], error=str(exc))
            else:
                row["estimated_premium"] = computed.amount
                row["estimated_basis"] = computed.basis
        priced.append(row)
    return priced


TOOLS: dict[str, Any] = {"product_rate": product_rate, "member_underwriting": tools.member_underwriting}
"""The scenario's tools, for the executor's dispatch.

`member_underwriting` is `policydesk.agent.tools.member_underwriting` itself, not a
copy — the same function object the mark was set on, so `policydesk.agent.tools.permitted`
withholds it no matter which scenario calls it. `product_rate` carries no such mark: it
reads `catalog_entry`, a table with no member in it, so it is never withheld.
"""




async def gather(
    db: Database,
    params: dict[str, str],
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
        params: What the router collected. `line` and `keyword` pick the product;
            `amount` is the coverage the customer wants, when they gave one.
        member_id: Whose figures to rate the fit against.
        today: The date to age the member against.
        retriever: Unused here. Accepted because every scenario module is called the
            same way; a module ignoring an argument is not a special case in the
            caller.
        allowed: Which tool names the executor's identity gate permits this turn. None
            permits both — what a direct call gets. `member_underwriting` withheld here
            means the query never runs; this module never sets `_identity_required`
            itself, because the executor already knows it withheld something and a
            module that forgot the flag would hand back a partial answer that reads as
            a whole one.

    Returns:
        `product_rate` when `allowed` permits it — normally always, since the rate is
        public. `member_underwriting` when `allowed` permits it and a member_id was
        given, so the reply can compare the member's own age and occupation class
        against a band it already has, instead of being asked to guess whether they fit.

    """
    line = params.get("line", "health")
    keyword = params.get("keyword", "")
    amount = _as_amount(params.get("amount", ""))
    factories: dict[str, Any] = {"product_rate": lambda: product_rate(db, line, keyword, amount)}
    if member_id is not None:
        factories["member_underwriting"] = lambda: tools.member_underwriting(
            db, member_id, today=today or datetime.now(UTC).date()
        )
    return await gather_tools(factories, allowed=allowed)


QUOTE = Scenario(
    name="quote",
    display_name="保費試算",
    summary="依年齡與保額試算某張商品的保費",
    description=(
        "保戶問「這張要多少錢」「我這個年紀保費多少」「幫我算一下」這類想知道某張商品保費金額時使用。"
        "這是試算不是核保結果，不需要保戶先說預算或需求。"
        "保戶已經說出需求與預算、要挑適合方案時改用 recommend；只是想看目錄上有什麼時改用 browse_products。"
    ),
    injection=(
        "product_rate 是空的時候，代表本公司沒有符合這個名稱或這個保險種類的在售商品，"
        "不是系統查不到。照實說沒有這張，並請他改用險種（壽險、醫療、意外、年金、投資型）"
        "或換個關鍵字讓你再查一次。不要憑印象講一張你以為我們有的商品。\n"
        "你正在為保戶試算保費，這是試算，不是核保結果——"
        "職業等級、健康告知與核保結果都可能讓實際保費不同，這句話一定要說。\n\n"
        "product_rate 回傳的 unit_premium 與 unit_label 是這張商品的公開費率，"
        "estimated_premium（如果有）是照保戶想要的金額換算出來的試算金額，"
        "兩者都只能照工具回傳的數字說，不可以自己心算、估計或另外換算。\n\n"
        "requires_main 為 true 的商品是附約，沒有主約不能單獨投保，也沒有單獨的年繳保費。"
        "這種商品只能說明它的單位費率，不可以說它一年要繳多少錢，也不可以用 estimated_premium——"
        "這種商品的這個欄位一定是空的。\n\n"
        "issue_age_min 到 issue_age_max 是這張商品可以投保的保險年齡範圍，這裡的年齡一律是保險年齡，不是足歲。"
        "member_underwriting 回傳保戶的保險年齡與職業等級時，把它拿去跟每張商品的年齡範圍與 max_occupation 比較："
        "不符合的商品也要照樣列出，並照 issue_age_min、issue_age_max 或 max_occupation 說明差在哪裡，"
        "不要因為不符合就不提這張商品。\n\n"
        "工具回傳 _identity_required 時，表示保戶尚未完成身分核對，所以你拿不到他的保險年齡與職業等級。"
        "此時只照 product_rate 說明費率與可投保的保險年齡範圍，"
        "接著說明要判斷是否符合他本人的條件，需要先核對身分，請他提供身分證字號。"
        "不要憑空猜測他的年齡或職業等級。"
    ),
    tools=("product_rate", "member_underwriting"),
    tools_module="policydesk.agent.scenarios.quote",
    params=(
        Param(
            name="line",
            description=(
                "保戶問的商品線，只填下列其中一個英文字："
                "health 醫療、life 壽險、accident 意外、annuity 年金、investment 投資型。"
                "保戶沒指明就填 health"
            ),
            example="health",
        ),
        Param(
            name="keyword",
            description="保戶提到的商品名稱關鍵字，用他自己的說法或既有商品全名片段；沒有指明特定商品就留空",
            example="住院醫療",
        ),
        Param(
            name="amount",
            description="保戶想要的保額或保障金額，只填阿拉伯數字；保戶沒有提到具體金額就留空",
            example="500000",
        ),
    ),
    quick_replies=("我想調整保額再算一次", "這張的等待期是多久？", "這張有哪些不賠的情況？"),
    transitions=("explain_cover", "recommend"),
)
