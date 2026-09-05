"""Resolve a public catalogue product before retrieving its contract text."""

from typing import TYPE_CHECKING, Any
from unicodedata import normalize

from policydesk.agent import tools
from policydesk.agent.scenario_base import Param, Scenario, gather_tools

if TYPE_CHECKING:
    from policydesk.core.db import Database
    from policydesk.retrieval.base import Retriever


@tools.public
async def catalogue_clauses(
    db: Database, product: str, topic: str, *, index: Retriever | None = None,
) -> dict[str, Any]:
    """Search one uniquely identified public product, never a member's policy."""
    wanted = normalize("NFKC", product).replace(" ", "").strip()
    result: dict[str, Any] = {"status": "needs_product", "match_count": 0, "products": [], "clauses": []}
    if not wanted:
        return result
    rows = await db.fetch(
        """SELECT p.product_id, p.name, p.doc_sha, count(*) OVER () AS match_count
           FROM sale_catalog ce JOIN product p USING (product_id)
           WHERE ce.on_sale
             AND (p.product_id = $1::text
                  OR strpos(lower(replace(normalize(p.name, NFKC), ' ', '')), lower($1::text)) > 0)
           ORDER BY p.name, p.product_id
           LIMIT 5""",
        [wanted],
    )
    result["products"] = rows
    result["match_count"] = rows[0]["match_count"] if rows else 0
    if not rows:
        result["status"] = "not_found"
    elif result["match_count"] != 1:
        result["status"] = "ambiguous"
    elif not topic.strip():
        result["status"] = "needs_topic"
    else:
        result["status"] = "found"
        # Only the catalogue-resolved ID reaches the shared text lookup. No member
        # or policy identifier comes from the model or is read by this public tool.
        result["clauses"] = await tools.find_clause(db, [rows[0]["product_id"]], topic, index=index)
    return result


TOOLS = {"catalogue_clauses": catalogue_clauses}


async def gather(
    db: Database, params: dict[str, str], *, allowed: frozenset[str] | None = None,
    retriever: Retriever | None = None, **_: Any,
) -> dict[str, Any]:
    return await gather_tools({
        "catalogue_clauses": lambda: catalogue_clauses(
            db, params.get("product", ""), params.get("topic", ""), index=retriever,
        ),
    }, allowed=allowed)


PRODUCT_CLAUSES = Scenario(
    name="product_clauses",
    display_name="公開商品條款",
    summary="查詢指定公開商品的契約條款，不讀個人保單",
    description=(
        "查詢指定商品的公開契約條款，包括先前介紹或推薦、尚未投保的商品。"
        "不查持有狀態、個人保額、繳費或個人適合度；查自己既有保單時使用 explain_cover 等個人保單情境。"
    ),
    injection=(
        "catalogue_clauses 只查公開商品契約，不代表保戶已持有、符合投保資格或實際可獲理賠。"
        "status 為 needs_product 時請客戶指定商品；not_found 表示查無可辨識的公開目錄匹配，"
        "請核對名稱或版本，不可據此斷言客戶沒有保單或契約沒有該規定。"
        "ambiguous 表示多個商品或版本，先列出 products 的名稱與 product_id 請客戶確認，"
        "不可合併各版本條款；match_count 大於候選數時，說明候選僅列前幾項。"
        "needs_topic 時請客戶指定想了解的條款主題。"
        "found 時只依 clauses 說明指定商品，每項主張標註其 clause_id，保留除外責任及回復承保的條件。"
        "clauses 是空的時候，只能說現有證據不足以確認，不能視為整份契約沒有該規定。"
        "這是局部檢索，不可聲稱已逐條檢查完整契約。"
    ),
    tools=("catalogue_clauses",),
    tools_module="policydesk.agent.scenarios.product_clauses",
    params=(
        Param(name="product", description="對話中指定的商品名稱、可辨識名稱片段，或資料中已提供的 product_id"),
        Param(name="topic", description="客戶想了解的契約保障、條件或條款主題"),
    ),
    quick_replies=("這張商品有哪些除外責任？", "等待期的適用條件是什麼？"),
    transitions=("quote", "recommend"),
)
