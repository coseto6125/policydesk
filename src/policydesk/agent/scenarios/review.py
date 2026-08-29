"""
The scenario for a customer who wants to know if their coverage has a hole in it.

「我的保障夠不夠」「有沒有缺什麼」「幫我看一下保單」 ask a question none of the desk's
other scenarios answer. `explain_cover` reads one topic in one contract; `policy_overview`
lists what each contract grants. Neither one compares what the customer holds against
what the insurer's whole catalog recognises as a category of event, which is the only way
to say "you are not covered for X" rather than "here is what your policies say".

## The categories are queried, not guessed — and not read from `benefit`

`benefit` looked like the corpus's vocabulary for what a policy pays for, but it holds
rows for exactly one of the 660 products here. Querying it literally would have told 659
products' holders they have no coverage at all, which is worse than the sales pitch this
scenario exists to avoid — a health check that invents a gap is not a safer failure than
one that recommends a product.

The real vocabulary lives in `clause` where `kind = 'grant'`: 338 products, 2,521 rows,
and a heading that regularly ends in `…OO保險金的申領` or `…OO保險金的給付`. `_categories_in`
reads the noun in front of `保險金` out of that heading. It is anchored to start at the
heading's own start or just past a separator (、，或及與之的) so a fragment split out of
running prose cannot form one, and it drops anything containing 給付／通知／公司／減少／
分期 — the five words that turned up in the middle of a sentence about *how* a benefit is
paid rather than *what* it is. Verified against the corpus: the surviving vocabulary is
身故, 完全失能, 祝壽, 滿期, 醫療, 住院醫療, 門診手術醫療, 住院手術醫療, 住院日額, 特定傷病,
意外事故失能, 各項癌症, 重大燒燙傷, 意外骨折 among the top entries, and none of 保險事故的
通知與, 本公司不負給付, 給付各項, 本公司按, 減少基本, 分期定期 survive — those six are the
fragments a naive `([一-龥]{2,8})保險金` match without the boundary and the block list would
have produced.

## A rider's cover goes with its main

`policy.main_policy_id IS NULL` marks a main policy. A rider whose own `lapsed_at` is
untouched still stops paying the moment its main does — 主契約停止效力時，本附約效力亦
同時停止 is not a special case, it is how every rider in this corpus is written. A health
check that lists a rider as "covered" because nobody re-read its own row is worse than one
that says nothing, because the customer now believes he has cover he does not.

## No product recommendation

A health check that ends in a sales pitch is the thing customers distrust about this
industry, and it is also the wrong tool for the job: naming a gap and reaching for a
product to fill it are two different judgements, and only one of them belongs to a desk
that is supposed to be checking the customer's own book rather than selling into it.
`recommend` is a transition offered afterward, not a step this scenario takes on the
customer's behalf.
"""

import re
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from policydesk.agent import tools
from policydesk.agent.scenario_base import Scenario
from policydesk.agent.tools import requires_identity

if TYPE_CHECKING:
    from policydesk.core.db import Database

_CATEGORY = re.compile(r"(?:^|[、，,/或及與之的\s])([一-龥]{2,8})保險金")
"""What a grant clause's heading calls the thing it pays for.

Anchored so a match cannot start mid-word: either the heading's own start, or right
after a separator a heading actually uses (、，或及與之的). Without the anchor, a heading
like 有下列情形之一者，本公司不負給付保險金的責任 would let the quantifier reach back
across 本公司不負給付 and hand back a sentence fragment as if it were a category.
"""

_FRAGMENT_MARKERS: tuple[str, ...] = ("給付", "通知", "公司", "減少", "分期")
"""Present in the middle of *how* a benefit is paid, never in the name of *what* it is.

Found by reading what the raw regex actually produced against this corpus: 保險事故的
通知與, 本公司不負給付, 給付各項, 本公司按, 減少基本, 分期定期 — six phrases the boundary
anchor alone still let through, because each one does sit at a heading's own start or
just past a separator. A real category name never contains any of these five words, so
dropping on that alone is safe in a way dropping on length or position is not.
"""


def _categories_in(heading: str) -> set[str]:
    """
    Read the benefit categories a grant clause's own heading actually names.

    Args:
        heading: One `clause.heading` where `kind = 'grant'`.

    Returns:
        Zero or more category names — a heading naming two benefits, e.g. 身故保險金或
        喪葬費用保險金的申領, returns both.

    """
    candidates = {match.group(1) for match in _CATEGORY.finditer(heading)}
    return {c for c in candidates if not any(marker in c for marker in _FRAGMENT_MARKERS)}


def _effectively_covered(policy: dict[str, Any], by_number: dict[str, dict[str, Any]]) -> bool:
    """
    Say whether a policy is still paying for anything today.

    Args:
        policy: One row from `tools.list_policies`.
        by_number: The same member's policies, keyed by `policy_number`, so a rider's
            main policy can be looked up without a second query.

    Returns:
        False when the policy itself has lapsed, or when it rides on a main policy that
        has. A rider's own `lapsed_at` says nothing about this — the main's does.

    """
    if policy["is_lapsed"]:
        return False
    main = by_number.get(policy["main_policy_number"])
    return not (main is not None and main["is_lapsed"])


async def _grant_headings(db: Database, product_ids: list[str]) -> list[dict[str, Any]]:
    """
    Read the grant clauses of these products — the table the categories live in.

    Args:
        db: The database.
        product_ids: Which products. Empty for the corpus-wide catalog query.

    Returns:
        Product and heading pairs, one per grant clause.

    Shared by `held_categories` and `category_catalog`, which differ only in whether
    `product_ids` narrows the query to one member's book or leaves it empty for
    everything the insurer sells.

    """
    return await db.fetch(
        """SELECT c.product_id, p.name AS product_name, c.heading
           FROM clause c JOIN product p USING (product_id)
           WHERE c.kind = 'grant' AND ($1::text[] IS NULL OR c.product_id = ANY($1::text[]))""",
        [product_ids or None],
    )


@requires_identity
async def held_categories(db: Database, product_ids: list[str]) -> list[dict[str, Any]]:
    """
    List what a member's own contracts actually pay for.

    Args:
        db: The database.
        product_ids: The products behind the member's policies that are still
            effectively in force.

    Returns:
        One row per (product, category) pair actually granted.

    Filtered to the member's own products, so a category held on one contract and not
    another still counts as held; the customer's book is the union of every policy he
    holds, not any one of them read alone.

    """
    if not product_ids:
        return []
    rows = await _grant_headings(db, product_ids)
    return [
        {"product_id": row["product_id"], "product_name": row["product_name"], "name": category}
        for row in rows
        for category in sorted(_categories_in(row["heading"]))
    ]


CATALOG_FLOOR = 10
"""How many products must carry a category before it counts as one.

Without a floor the corpus yields 107 categories and 47 of them appear on exactly one
product — 罹癌基因檢測, 燒燙傷回診, 完全失能生活扶助. Every one is a real clause heading,
and listing all of them turns a 健檢 into a 90-line list of things the customer does not
have, which is not a finding but a wall. At ten the list is the nineteen a 保戶 would
recognise as categories, and a gap in it is a gap worth naming.

The floor is on product count rather than on a hand-written keep-list, so a reingest that
changes what this insurer sells changes the list with it.
"""


async def category_catalog(db: Database, *, floor: int | None = None) -> list[dict[str, Any]]:
    """
    List every benefit category this insurer's whole catalog recognises.

    Args:
        db: The database.
        floor: Fewest products a category must appear on. None reads `CATALOG_FLOOR` at
            call time rather than binding it as a default, so a test can move the floor
            on a stub corpus of two products without the module's real one changing.

    Returns:
        One row per distinct category, named after the grant clauses that actually use
        it, with a count of how many products carry it, commonest first.

    Ungated: this is what the insurer sells generally, not what any one customer holds,
    so it carries no `requires_identity` mark. What the customer holds is
    `held_categories`, and the gap between the two is the whole point of this scenario.

    """
    floor = CATALOG_FLOOR if floor is None else floor
    rows = await _grant_headings(db, [])
    products_by_category: dict[str, set[str]] = {}
    for row in rows:
        for category in _categories_in(row["heading"]):
            products_by_category.setdefault(category, set()).add(row["product_id"])
    return [
        {"name": name, "product_count": len(products)}
        for name, products in sorted(products_by_category.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        if len(products) >= floor
    ]


TOOLS: dict[str, Any] = {
    "list_policies": tools.list_policies,
    "held_categories": held_categories,
    "category_catalog": category_catalog,
}
"""The scenario's tools, for the executor's dispatch.

`list_policies` is reused from `agent.tools` rather than reimplemented — it already reads
every policy a member holds, main and rider alike, with the `lapsed_at` and
`main_policy_number` this scenario's gate and its main/rider check both need.
"""


async def gather(
    db: Database,
    params: dict[str, str],  # noqa: ARG001 -- unused: this scenario takes no params, but every module shares one gather() signature
    *,
    member_id: int | None = None,
    today: date | None = None,
    retriever: Any | None = None,  # noqa: ARG001 -- unused: this scenario cites no clauses, but every module accepts one
    allowed: frozenset[str] | None = None,
    **_: Any,
) -> dict[str, Any]:
    """
    Run this scenario's tools.

    Args:
        db: The database.
        params: What the router collected. This scenario takes none.
        member_id: Whose book to check.
        today: The date to judge currency against.
        retriever: Unused here — this scenario cites no clauses to rank — but named
            because every scenario module accepts it.
        allowed: Which tools may run. None runs all of them.

    Returns:
        Every policy the member holds, what his effectively-covered ones actually pay
        for, and the categories none of them do. Without `list_policies` it returns the
        catalogue alone.

    `today` defaults to now rather than being required, so a caller in a test can omit
    it the same way `soothe.gather` needs no date at all — the contract is one signature
    for every scenario module, not one a caller has to special-case per scenario.

    A gap is only meaningful against a book. Unverified, this returns what the company
    covers and nothing about what the customer does — because computing gaps from an
    empty policy list would tell every unverified customer they hold no cover at all,
    which is a false statement about them rather than a withheld one.

    """
    def can(name: str) -> bool:
        return allowed is None or name in allowed

    today = today or datetime.now(UTC).date()
    catalog = await category_catalog(db) if can("category_catalog") else []
    if not can("list_policies"):
        return {"category_catalog": catalog}

    policies = await tools.list_policies(db, member_id, today=today)
    by_number = {p["policy_number"]: p for p in policies}
    for policy in policies:
        policy["is_main"] = policy["main_policy_number"] is None
        policy["effectively_covered"] = _effectively_covered(policy, by_number)

    covered_product_ids = sorted({p["product_id"] for p in policies if p["effectively_covered"]})
    held = await held_categories(db, covered_product_ids) if can("held_categories") else []
    held_names = {b["name"] for b in held}
    gaps = [c for c in catalog if c["name"] not in held_names]

    return {"policies": policies, "held_benefits": held, "gaps": gaps, "category_catalog": catalog}


REVIEW = Scenario(
    name="review",
    display_name="保單健檢",
    description=(
        "保戶問我的保障夠不夠、有沒有缺什麼、想幫他看一下保單、想健檢保單時使用。"
        "這個情境不需要任何參數，保戶只要問了就直接查。"
    ),
    injection=(
        "你正在為保戶健檢名下的保障，不是在推銷商品。\n"
        "工具已經查出他名下每一張保單的狀態、他目前有效的保單實際涵蓋哪些保障類別、"
        "以及完全沒有任何一張有效保單涵蓋的類別，這些全部照工具回傳的資料講，不要用印象補內容。\n\n"
        "先逐張列出保單：商品名稱、保單號碼、是主約還是附約、目前狀態（有效或已停效）。\n"
        "附約要特別檢查：如果這張附約本身沒有停效，但它所屬的主約已經停效，"
        "一樣要明講這張附約現在不提供保障——主約停效，附約的效力就跟著停止，不是附約自己沒事就沒事。\n\n"
        "接著逐一列出他有效保單實際涵蓋的保障類別，並點名是哪一張保單提供的。\n"
        "再明講完全沒有涵蓋的類別：每一項缺口都要具體點名是哪個類別、"
        "講清楚「你名下沒有任何一張有效保單，在OO情況發生時會給付」，不要只講「保障不足」這種空話。\n\n"
        "你的任務到這裡結束。不建議該買哪一張商品、不比較商品、不推銷、不說「建議您加保」。"
        "保戶自己問要怎麼補這個缺口，就告訴他可以進一步了解方案，由他自己決定要不要繼續，不要你先開口推。\n"
        "不可以引用任何條款號碼，工具回傳的材料裡沒有附上可引用的條款號碼，只有保障類別名稱，講類別名稱就好。\n"
        "不可以判斷賠不賠，核保理賠人員才有權決定。\n"
        "工具回傳 _identity_required 時，表示保戶尚未完成身分核對，所以你拿不到他的任何個人資料。"
        "此時說明本櫃台可以幫他健檢保單，但需要先核對身分，請他提供身分證字號。"
        "不要憑空講任何關於他保單或保障缺口的內容。"
    ),
    tools=("list_policies", "held_categories", "category_catalog"),
    tools_module="policydesk.agent.scenarios.review",
    quick_replies=("我想了解怎麼補上這個缺口", "這些保障各自的除外責任是什麼", "我想確認保額夠不夠"),
    transitions=("recommend", "explain_cover"),
)
