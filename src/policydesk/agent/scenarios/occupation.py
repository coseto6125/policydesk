"""
OCCUPATION: 職業或職務變更的通知義務.

「我換工作了要跟你們說嗎」「我現在做工地會不會影響理賠」「換工作保費會變嗎」share no
character with the clause that answers them — 職業或職務變更的通知義務 — which is why
BM25 alone cannot reach it and why this scenario exists to prove the embedding channel
does. Checked against the live corpus before writing this module: `我現在做工地會不會影響
理賠` and `換工作保費會變嗎`, searched with no anchor at all, both rank a jurisdiction
clause (管轄法院) above the occupation clause. `_ANCHOR` below, prepended to every search,
is what puts the occupation clause first for all three of the customer's own sentences —
the same fix `disclosure.py` already applies to the statutory search, extended here to the
clause search `disclosure.py` never needed.

## Where the clause actually lives

23 of 660 products carry a clause headed 職業或職務變更的通知義務 — every one of them
`kind = 'procedure'`, across both `accident` and `life` lines, checked against the live
corpus before writing this module. A customer holding none of those 23 products gets no
clause row back, which is the correct answer, not a failure: most policies in this book
carry no occupation-sensitivity at all, and the statutory floor still applies to every one
of them regardless.

## The duty runs both ways, in the same paragraph the customer never asks about

保險法 §59 has four 項. I-III are the increase side: notify, and notify promptly when the
increase is the customer's own doing. IV is `危險減少時，被保險人得請求保險人重新核定保
費` — the customer may ask for the premium to be re-rated down. A desk that quotes I-III
and stops has told the customer only the half that sounds like a threat; `occupation_duty`
brings all four back together in one `search_statute` call, the same `siblings=True`
mechanism `disclosure.py` uses for §64, so there is no ranking step that could return I-III
without IV.

The contract clause states the same asymmetry in its own words, more concretely: a
lower-risk move gets an automatic pro-rata refund of unearned premium; a higher-risk move
gets a pro-rata surcharge, or — for a move into class 5-6 or 拒保 — the insurer may
terminate. `occupation_clause` returns whichever of those paragraphs the customer's own
product states; the injection requires both directions be read out, never only the one
that sounds punitive.

## What this scenario refuses to do

**No verdict.** Whether a given move triggers 加費, 退費 or termination is underwriting's
reading of the customer's own case, not a comparison this desk runs and announces.
`member_occupation` hands the model the member's current `occupation_class` and each of
their in-force policies' `max_occupation` — facts, not a verdict — so the model can point
at *which* policy a move might concern without declaring what happens to it.

**No premium.** `quote` exists to price a policy; this scenario states the duty and what
the clause says follows from it, and stops. A number produced here would be this desk's
own arithmetic pretending to be the underwriter's.

**No occupation the customer never gave.** `occupation_classes` is the same catalogue
`policydesk.synthetic.person.occupation_catalogue` used to build every synthetic member —
not what the model may declare an occupation to be, but what it may check a customer's own
words against. An occupation absent from it is not evidence of a class; it is a case for
查證, never a guess dressed as one.

## Identity

`occupation_duty` reads `statute_article`; `occupation_classes` reads a Python literal
constructed by `policydesk.synthetic.person`. Neither touches a member row, so neither
carries `@requires_identity`. `member_occupation` reads the member's own `occupation` and
`occupation_class`, and the `max_occupation` of the products they actually hold — both
gated. `occupation_clause` searches those same held products' own clauses — gated for the
same reason `find_clause` already carries the mark in `agent.tools`. An unverified
customer still gets the full §59 text and the public occupation table; asked for an ID to
see how either applies to a policy that is theirs.
"""

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from policydesk.agent import statute, tools
from policydesk.agent.scenario_base import Param, Scenario, gather_tools
from policydesk.agent.tools import requires_identity
from policydesk.synthetic.person import OccupationClass, occupation_catalogue

if TYPE_CHECKING:
    from policydesk.core.db import Database

STATUTE_SCOPE: list[str] = ["insurance_act"]
"""§59-61 are 保險法's own general duty on a change in risk; nothing in the other two
statutes this corpus carries bears on an occupation change."""

_ANCHOR = "職業或職務變更 危險增加 危險減少 通知本公司 終止契約 增收保險費 退還保險費 重新核定保費"
"""The clause's and §59's own vocabulary, prepended to the customer's words before every
search — clause and statute both, since neither ranks first on 換工作 alone.

Checked against the live corpus before writing this module: `換工作保費會變嗎` with no
anchor ranks `管轄法院` (a jurisdiction clause) above the occupation clause in the clause
search, and pulls in an unrelated article in the statute search. Prepended with this
anchor, all three of the brief's own example sentences — 我換工作了要通知嗎, 我現在做工地
會不會影響理賠, 換工作保費會變嗎 — rank the occupation clause first and bring back all
four paragraphs of §59. The customer's own words still ride along after the anchor and
still count toward the ranking when they overlap the clause's own vocabulary; they are
never load-bearing on their own.
"""


async def occupation_duty(
    db: Database, concern: str, *, limit: int = 2, retriever: Any | None = None
) -> list[dict[str, Any]]:
    """
    Find §59's duty to notify a change in risk, both directions together.

    Args:
        db: The database.
        concern: What the customer described, in their own words.
        limit: Most provisions to rank, before `search_statute` fills in each hit's
            neighbouring 項. Kept small: raising it past 2 starts pulling in §64 (the
            health-declaration duty) on a generic concern, which is a different scenario's
            subject.
        retriever: The shared BM25/embedding index, when one is open. None falls back to
            the SQL ranking, which is worse and still answers.

    Returns:
        Provision rows with their verbatim text, ready to be quoted.

    Public: reads `statute_article`, the same text anyone can read at 全國法規資料庫, so
    this carries no `requires_identity` mark.

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


async def occupation_classes() -> list[dict[str, object]]:
    """
    List the occupations this insurer classifies, with the class each carries.

    Returns:
        One entry per occupation: its name, its numeric class, and the class's Chinese
        label — `policydesk.synthetic.person.occupation_catalogue`, unfiltered.

    Public: a fixed classification table, the same for every customer and every product,
    so this carries no `requires_identity` mark. An occupation the customer names that is
    not in this list is not evidence of any particular class — the injection is written to
    say so, never to guess one from a nearby-sounding entry.

    """
    return occupation_catalogue()


@requires_identity
async def member_occupation(db: Database, member_id: int, *, today: date) -> dict[str, Any]:
    """
    Read this member's own occupation and class, and the ceiling of each policy they hold.

    Args:
        db: The database.
        member_id: Whose record.
        today: The date to judge policy currency against.

    Returns:
        `occupation`, `occupation_class` and its label as recorded on the member; and
        `policies`, one entry per in-force policy naming the product and its
        `max_occupation` — the class beyond which that product will not carry the risk.
        Empty dict when the member row does not exist.

    Gated: reads the member's own row and, through `list_policies`, their own book — a
    class number alone means nothing without knowing which of the customer's own products
    it is being weighed against.

    """
    row = await db.fetch_one(
        "SELECT occupation, occupation_class FROM member WHERE member_id = $1::bigint", [member_id]
    )
    if row is None:
        return {}
    active = [p for p in await tools.list_policies(db, member_id, today=today) if not p["is_lapsed"]]
    ceilings: dict[str, int] = {}
    if active:
        rows = await db.fetch(
            "SELECT product_id, max_occupation FROM catalog_entry WHERE product_id = ANY($1::text[])",
            [[p["product_id"] for p in active]],
        )
        ceilings = {r["product_id"]: r["max_occupation"] for r in rows}
    cls = row["occupation_class"]
    return {
        "occupation": row["occupation"],
        "occupation_class": cls,
        "occupation_class_label": OccupationClass(cls).label if cls is not None else None,
        "policies": [
            {
                "product_id": p["product_id"],
                "product_name": p["product_name"],
                "max_occupation": ceilings.get(p["product_id"]),
            }
            for p in active
        ],
    }


@requires_identity
async def occupation_clause(
    db: Database, product_ids: list[str], concern: str, *, limit: int = 4, retriever: Any | None = None
) -> list[dict[str, Any]]:
    """
    Find the member's own products' clause on changing occupation, by meaning.

    Args:
        db: The database.
        product_ids: The products behind the member's in-force policies.
        concern: What the customer described, in their own words.
        limit: Most clauses to return.
        retriever: The shared index. None falls back to `find_clause`'s ILIKE ranking,
            which will not reach this clause on the customer's own wording — the ILIKE
            fallback needs the customer's literal words as a substring, and 換工作 shares
            no character with 職業或職務變更. It still answers with whatever else the
            fallback's `kind IN (exclusion, carve_back, waiting)` net catches, which is
            this clause's `kind = 'procedure'` never.

    Returns:
        Clause rows from the member's own held products, best first.

    Gated: scoped to `product_ids`, which come from the member's own policies —
    `find_clause` itself already carries `@requires_identity` in `agent.tools`.

    """
    if not product_ids:
        return []
    topic = f"{_ANCHOR} {concern}".strip()
    return await tools.find_clause(db, product_ids, topic, limit=limit, index=retriever)


TOOLS: dict[str, Any] = {
    "occupation_duty": occupation_duty,
    "occupation_classes": occupation_classes,
    "member_occupation": member_occupation,
    "occupation_clause": occupation_clause,
}
"""The scenario's tools, for the executor's dispatch.

`occupation_duty` and `occupation_classes` are unmarked public reference material.
`member_occupation` is marked directly. `occupation_clause` inherits its mark from
`find_clause`'s own `@requires_identity`, carried through unchanged rather than
redeclared — a second mark on the same fact would be one more place a fix could miss.
"""


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
        params: What the router collected. `concern` is the job change the customer
            described, in their own words.
        member_id: Whose occupation and clauses to read. Ignored when `member_occupation`
            is not in `allowed`.
        today: The date to judge policy currency against. Defaults to today.
        retriever: The shared index, passed straight through to both searches.
        allowed: Which tool names may run this turn, from `policydesk.agent.tools
            .permitted`. None runs every tool — the shape a direct call gets.

    Returns:
        Material for the injection, by tool name. `occupation_clause` runs only once
        `member_occupation` has actually returned this member's held products — the two
        cannot go in the same concurrent batch, since one needs the other's output —
        so the identity-gated pair costs one extra round trip rather than a second
        gather. `_allowed_clauses` names exactly the `clause_id`s `occupation_clause`
        returned; never sets `_identity_required` itself, since the executor already
        knows what it withheld.

    """
    today = today or datetime.now(UTC).date()
    concern = params.get("concern", "一般說明")
    factories: dict[str, Any] = {
        "occupation_classes": occupation_classes,
        "occupation_duty": lambda: occupation_duty(db, concern, retriever=retriever),
    }
    if member_id is not None:
        factories["member_occupation"] = lambda: member_occupation(db, member_id, today=today)
    facts = await gather_tools(factories, allowed=allowed)
    facts.setdefault("_allowed_clauses", frozenset())

    # Falsy, not `is None`. `member_occupation` returns `{}` for a member_id with no row
    # — its own docstring says so — and `{}` is not None, so the guard passed and
    # `member["policies"]` raised KeyError on the next line.
    member = facts.get("member_occupation")
    if not member or (allowed is not None and "occupation_clause" not in allowed):
        return facts

    product_ids = sorted({p["product_id"] for p in member["policies"]})
    clauses = await occupation_clause(db, product_ids, concern, retriever=retriever)
    facts["occupation_clause"] = clauses
    facts["_allowed_clauses"] = frozenset(row["clause_id"] for row in clauses)
    return facts


OCCUPATION = Scenario(
    name="occupation",
    display_name="職業變更通知",
    summary="說明換工作的通知義務與職業等級的影響",
    description=(
        "保戶問換工作、職業或職務有變動要不要通知、現在做的工作會不會影響理賠或保費時使用。"
        "這個情境不判斷會不會加費、退費或終止契約，只說明通知義務本身、契約與保險法怎麼規定，"
        "以及保戶自己名下保單的職業等級上限。"
    ),
    injection=(
        "member_occupation 某一列的 max_occupation 是空的時候，代表那張商品沒有登記職業等級上限，"
        "不是他不受限制，也不是系統查不到。這種保單只說明通知義務本身，"
        "不要推論他的職業等級在那張保單上可不可以。\n"
        "member_occupation 顯示他目前的職業等級已經高於某張保單的 max_occupation 時，"
        "那正是這個情境存在的情形：照條款說明公司在這種情況下可以做什麼（加費、終止、"
        "或理賠時按保費比例折算），但不要說他的保單已經失效或理賠一定會被打折——"
        "那是核保理賠人員依個案認定的，不是這裡能下的結論。\n"
        "occupation_clause 是空的時候，代表保戶名下目前持有的保單裡沒有找到職業或職務變更的"
        "書面條款，不是系統查不到——多數保單本來就沒有這條，這時候只照 occupation_duty 的"
        "保險法規定說明即可，不要說查詢失敗，也不要假裝有一條条款卻講不出內容。\n"
        "member_occupation 是空字典時，代表查無這位會員的紀錄，一樣不要假裝查得到他的職業資料。\n\n"
        "你正在說明職業或職務變更的通知義務，你不是核保或理賠人員，不能替保戶判斷改行之後"
        "會不會加費、退費、被終止契約或影響理賠。你能做的是把規定攤開，讓保戶自己知道接下來"
        "該做什麼、可能碰到什麼情況。\n\n"
        "先講清楚這個義務是雙向的，不是只有變壞的一半：\n"
        "職業或職務的危險性提高時，要保人或被保險人應該通知本公司，本公司可能按差額比率"
        "增收保費，如果變更後的職業依照本公司職業分類屬於第五、六類或拒保範圍，本公司於接到"
        "通知後得終止契約——這是'得'終止，不是'一定'終止，不要講成保戶一定會被解約。\n"
        "職業或職務的危險性降低時，依照保險法第59條第4項，被保險人得請求保險人重新核定保費；"
        "保戶自己保單的條款如果另外約定危險降低時自動退還未到期保險費差額，也要照 verbatim"
        "一併講出來，不要只講對公司有利的加費、終止那一半。\n\n"
        "occupation_duty 回傳的是保險法第59條的規定，引用時照 citation 欄位一字不差標註，"
        "例如〔保險法 第59條第4項〕；occupation_clause 回傳的是保戶自己保單的條款，"
        "引用時照 clause_id 原樣標註在句末，例如 [art.20]，不同商品的條號可能不一樣，"
        "不要假設每張保單都用同一個號碼。\n\n"
        "member_occupation 回傳保戶目前紀錄上的職業等級，以及他名下每張在效保單所屬商品的"
        "職業等級上限 max_occupation。這兩個數字只能拿來說明「這張保單最高承保到第幾類」，"
        "不能拿來算保費、不能宣告哪張保單一定會怎樣——保戶要換的新職業屬於第幾類、換了之後"
        "會不會超過某張保單的上限，需要核保人員實際核定，你只負責把等級數字攤開讓他自己對照。\n"
        "occupation_classes 回傳本公司的職業分類對照表，保戶說的職業如果剛好在表列名稱裡，"
        "可以照表講出對應等級；表裡沒有的職業，不要用相近的職業硬套一個等級，"
        "要說明實際等級由本公司核定，建議保戶直接告知確切職稱以便查證。\n\n"
        "保費會怎麼變、要繳多少，一律請保戶到保費試算的情境查，這裡不計算也不估算任何金額。\n"
        "不可以說某次職業變更一定要通知或一定不用通知，不可以說一定會加費、退費、終止契約或"
        "影響理賠，不可以承認公司過去的認定有錯，不可以保證契約一定會維持或一定會被終止——"
        "這些判斷都屬於核保與理賠人員的權責。\n"
        "不可以引用工具沒有回傳的條文或條款，想不起來就說這部分需要查證，寫錯的依據比不寫更"
        "傷害保戶。\n\n"
        "工具回傳 _identity_required 時，表示保戶尚未完成身分核對，這次只有 occupation_duty"
        "與 occupation_classes 這兩項公開資訊可以用，拿不到他自己的職業紀錄與保單條款。"
        "此時先把保險法第59條雙向的規定（危險增加要通知、危險降低可以請求重新核定保費）"
        "完整說一次，職業分類對照表也可以先給他參考，不要因為缺一半資料就整段不答。"
        "答完再說明：要對照他自己名下保單的職業等級上限與條款內容，需要先核對身分，"
        "請他提供身分證字號。不要憑空講他的職業或任何一張保單的內容。"
    ),
    tools=("occupation_duty", "occupation_classes", "member_occupation", "occupation_clause"),
    tools_module="policydesk.agent.scenarios.occupation",
    params=(
        Param(
            name="concern",
            description=(
                "保戶描述的職業或職務變更情況，用他自己的話摘要成具體職業或情境，例如「要去做"
                "計程車司機」「調到工地現場」"
            ),
            example="我要換工作，去做計程車司機，不確定要不要跟你們說",
            when_unsaid="保戶只是籠統問要不要通知、沒有提到具體職業時填「一般說明」，不要填空字串。",
        ),
    ),
    # 我這張保單的職業等級上限是第幾類？ was here, and the reply lists the ceiling for every
    # policy the customer holds — the chip led back to the table directly above it.
    quick_replies=("要用什麼方式通知才算數？", "換工作後保費會怎麼算？", "萬一忘了通知會怎樣？"),
    transitions=("quote", "explain_cover"),
)
