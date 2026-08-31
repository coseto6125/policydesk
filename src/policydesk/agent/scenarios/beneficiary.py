"""
BENEFICIARY: 受益人變更.

「我要改受益人」「受益人可以改成誰」「我離婚了要換人」 — all three ask the same thing:
what the customer can do, and what makes it real. This scenario answers both halves and
executes neither. It reads the member's current designation and their policies so the
desk can say what would change; it never writes to `member`, because a change spoken at
the counter and a change registered with the insurer are different events, and telling a
customer the first happened is how the second stops mattering to them.

## What the retrieval found, checked against the live corpus rather than assumed

保險法 §110-§114 all exist in `statute_article`. But the provision that actually answers
「什麼時候對保險人生效」is **§111 II** — 要保人行使前項處分權，非經通知，不得對抗保險
人 — not §114, which governs something adjacent: a *beneficiary's* transfer of their own
right to someone else (受益人非經要保人之同意…不得將其利益轉讓他人), the reverse
direction from a 要保人 changing who the beneficiary is. This module never states which
article answers which question from memory; `designation_rules` retrieves §110-111
together (`search_statute`'s default `siblings=True` keeps I and II of the same article
paired), and whichever doc_id the tool returns is the one the model is told to cite.

§112 and §113 are the other half: what happens when nobody was named. §113 — 死亡保險契
約未指定受益人者，其保險金額作為被保險人之遺產 — is the statutory backing for
`member.beneficiary_relation = 'legal_heir'`
(`policydesk.synthetic.person.BeneficiaryRelation.LEGAL_HEIR`, "the default when the
applicant names nobody"), so a customer whose record already reads 法定繼承人 can be told
why in the Act's own words.

`search_statute` was queried directly against every candidate concern here before this
module was written, including 「受益人 未成年」 and 「受益人 法定繼承人」. The 未成年 side
turned up nothing in `insurance_act` about a beneficiary who is a minor — the corpus's
only minor-related hits are about a minor *insured*, an unrelated question (§107, §16-1).
So the injection names 未成年受益人 only as a paperwork point (a legal representative's
ID goes with the form), never as a statutory claim, and never with a citation attached.

## What it may not do

**It prepares, it does not execute.** No write to `member` anywhere in this module. The
injection is explicit that the change takes effect only once the insurer registers it,
never at the counter.

**No statute the retrieval did not return**, same discipline as `soothe.py` and
`disclosure.py`.

## Identity

`current_beneficiary` reads the designation on each contract and `list_policies`
(reused from `policydesk.agent.tools`, already `@requires_identity`) reads their book, so
both carry the mark. `designation_rules` and `undesignated_fallback` read only
`statute_article` and carry no mark. The executor derives `allowed` from these marks per
tool (`policydesk.agent.tools.permitted`) and hands `gather` the set of names that may
run this turn; `gather` runs the two statute searches regardless and runs the two member
reads only when their names are in `allowed`. An unconfirmed customer therefore still
gets the designation rule, the notice requirement and the undesignated-estate rule
explained, with the request for an ID attached to the missing half — his own current
record and his policies — rather than standing in for the whole answer.
"""

import json
from typing import TYPE_CHECKING, Any

from policydesk.agent import statute, tools
from policydesk.agent.scenario_base import Param, Scenario, gather_tools
from policydesk.agent.tools import requires_identity

if TYPE_CHECKING:
    from datetime import date

    from policydesk.core.db import Database

STATUTE_SCOPE: list[str] = ["insurance_act"]
"""受益人的指定與變更 is squarely 保險法 §110-114; nothing outside that Act bears on it."""

_DESIGNATION_ANCHOR = "要保人得指定變更受益人 未經通知不得對抗保險人"
"""§110-111's own vocabulary. Checked against the live corpus before use: this phrase
ranks §110 I/II and §111 I/II as the top four hits and nothing else, with or without a
customer's own words appended — unlike a search built from 改受益人 alone, which ranked
an unrelated debtor-substitution provision (§123-2) above them."""

_UNDESIGNATED = 113
"""保險法 §113 — an undesignated death benefit becomes the estate."""

_DESIGNATED = 112
"""保險法 §112 — a designated one does not. One word apart in the text, opposite in
effect, and both returned by the same query."""

_FALLBACK_QUERY = "死亡保險契約未指定受益人者其保險金額作為被保險人之遺產"
"""§112-113's own vocabulary, kept as a separate query rather than merged into
`_DESIGNATION_ANCHOR`: merging the two pulled §110/§111 out of the top ranks entirely
(the rarer 遺產 vocabulary dominates the score), so nobody asking to change a beneficiary
would see the designation rule and nobody asking what happens with none named would see
the fallback rule. Two small, precise queries instead of one broad, noisy one."""

_RELATION_LABEL: dict[str, str] = {
    "self": "本人",
    "spouse": "配偶",
    "child": "子女",
    "parent": "父母",
    "sibling": "手足",
    "legal_heir": "法定繼承人",
}
"""Chinese labels for `member.beneficiary_relation`
(`policydesk.synthetic.person.BeneficiaryRelation`). Nothing else in the codebase
translates these — `web/server.py` and the console render the raw code."""




def _as_rows(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Shape `search_statute` rows into what the injection quotes from.

    Args:
        hits: Rows from `search_statute`.

    Returns:
        Each hit's citation, statute name, doc_id and verbatim text.

    """
    return [
        {"citation": statute.citation(row), "statute": row["statute_name"], "doc_id": row["doc_id"], "verbatim": row["verbatim"]}
        for row in hits
    ]


async def designation_rules(
    db: Database, concern: str, *, limit: int = 2, retriever: Any | None = None
) -> list[dict[str, Any]]:
    """
    Find §110-111's provisions on naming and changing a beneficiary.

    Args:
        db: The database.
        concern: What the customer said they want to do, in their own words.
        limit: Most provisions to rank, before siblings are added.
        retriever: The shared BM25 index, when one is open.

    Returns:
        Provision rows, best first — in practice §110 I/II and §111 I/II, the right to
        name a beneficiary and the notice that makes a change bind the insurer.

    """
    topic = f"{_DESIGNATION_ANCHOR} {concern}".strip()
    return _as_rows(await statute.search_statute(db, topic, STATUTE_SCOPE, limit=limit, retriever=retriever))


async def undesignated_fallback(db: Database, *, retriever: Any | None = None) -> list[dict[str, Any]]:
    """
    Find §112-113's provisions on what happens when nobody was named.

    Args:
        db: The database.
        retriever: The shared BM25 index, when one is open.

    Returns:
        The provision for the undesignated case only — §113, an undesignated death benefit
        becomes 遺產, which is the statutory backing for a `legal_heir` record.

    A fixed query, not the customer's words: this half is a background fact the desk
    supplies context with, not something a customer phrases their own way.

    **§112 is filtered out here on purpose.** The query returns both, because they are
    written as mirror images of each other — §112 is 指定了受益人的保險金不得作為遺產,
    §113 is 未指定的作為遺產. Handed to the model under one key called
    `undesignated_fallback`, the second one reads as more support for the first, and a
    model being thorough cites 〔保險法 第112條第1項〕 for a claim that provision denies.
    The citation checker cannot catch that: §112 exists, so the citation resolves. A real
    provision cited for the opposite of what it says is the failure this scenario was
    written to avoid, arriving through the material rather than through the model.

    """
    hits = await statute.search_statute(db, _FALLBACK_QUERY, STATUTE_SCOPE, limit=4, retriever=retriever)
    return _as_rows([hit for hit in hits if hit["article"] == _UNDESIGNATED])


async def designated_protection(db: Database, *, retriever: Any | None = None) -> list[dict[str, Any]]:
    """
    Find §112's provision on what designating a beneficiary protects.

    Args:
        db: The database.
        retriever: The shared index, when one is open.

    Returns:
        §112's rows: a death benefit payable to a named beneficiary is not part of the
        estate.

    Its own tool rather than a second entry under the undesignated one, because it is the
    other half of the same fact and the two are one word apart in the text. Separated here,
    the model is handed each with the condition it applies to attached; merged, it had to
    infer the condition from provisions that differ by 未.

    """
    hits = await statute.search_statute(db, _FALLBACK_QUERY, STATUTE_SCOPE, limit=4, retriever=retriever)
    return _as_rows([hit for hit in hits if hit["article"] == _DESIGNATED])


@requires_identity
async def current_beneficiary(db: Database, member_id: int) -> list[dict[str, Any]]:
    """
    Read who is designated on each of this member's contracts.

    Args:
        db: The database.
        member_id: Whose book.

    Returns:
        One row per policy: its number, the product, and who is named on it with their
        share. A policy with nobody named comes back with an empty `beneficiaries` list,
        which is 保險法 §113 and the case this scenario spends most of its words on.

    **Per contract, not per person.** This used to read `member.beneficiary_relation`, one
    code on the customer — and a beneficiary is not a property of a person. It is a
    designation on a contract, there can be several with shares between them, and §110 to
    §113 are entirely about that designation. A scenario built on those provisions was
    answering from a field that could not express what any of them describe.

    Reads one member's own record, so this carries `@requires_identity`. It only ever
    reads: this scenario prepares a change and never executes one.

    """
    rows = await db.fetch(
        """SELECT po.policy_id, po.policy_number, pr.name AS product_name,
                  (po.lapsed_at IS NOT NULL) AS is_lapsed,
                  coalesce(
                      json_agg(json_build_object('name', pb.display_name, 'relation', pb.relation,
                                                 'share', pb.share)
                               ORDER BY pb.share DESC)
                      FILTER (WHERE pb.beneficiary_id IS NOT NULL),
                      '[]'::json
                  ) AS beneficiaries
           FROM policy po
           JOIN product pr USING (product_id)
           LEFT JOIN policy_beneficiary pb ON pb.policy_id = po.policy_id
           WHERE po.member_id = $1::bigint
           GROUP BY po.policy_id, po.policy_number, pr.name, po.lapsed_at
           ORDER BY po.effective_at DESC""",
        [member_id],
    )
    for row in rows:
        named = row["beneficiaries"]
        row["beneficiaries"] = json.loads(named) if isinstance(named, str) else named
        for who in row["beneficiaries"]:
            who["label"] = _RELATION_LABEL.get(who["relation"], who["relation"])
    return rows


TOOLS: dict[str, Any] = {
    "current_beneficiary": current_beneficiary,
    "list_policies": tools.list_policies,
    "designation_rules": designation_rules,
    "undesignated_fallback": undesignated_fallback,
    "designated_protection": designated_protection,
}
"""The scenario's tools, for the executor's dispatch.

`list_policies` is reused from `policydesk.agent.tools` rather than redefined, so its
existing `@requires_identity` mark and its query travel with it — a second copy here
would be the same query maintained twice. Two of the four are marked, so
`reads_identity` reports True for this scenario.
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
        params: What the router collected. `concern` is what the customer wants to
            change, in their own words.
        member_id: Whose current designation and policies to read. Ignored when neither
            member-reading tool is in `allowed`.
        today: The date to judge policy currency against, passed to `list_policies`.
        retriever: The shared index, passed straight through to the statute searches.
        allowed: Which tool names may run this turn, from `policydesk.agent.tools
            .permitted`. None runs every tool — the shape a direct call (e.g. a test)
            gets, since nothing has withheld anything.

    Returns:
        Material for the injection, by tool name. Never sets `_identity_required`
        itself: the executor sets that flag whenever it withheld a tool, so a module
        that forgot to could not hand the model a partial answer that reads as a whole
        one.

    Two statute searches run first regardless of identity, since both are public; the
    two member reads join them only once verification has passed. Sent together with
    `asyncio.gather` rather than one round trip at a time, the same reasoning as the
    executor's own default gather step.

    """
    factories: dict[str, Any] = {
        "designation_rules": lambda: designation_rules(db, params.get("concern", ""), retriever=retriever),
        "designated_protection": lambda: designated_protection(db, retriever=retriever),
        "undesignated_fallback": lambda: undesignated_fallback(db, retriever=retriever),
    }
    if member_id is not None:
        factories["current_beneficiary"] = lambda: current_beneficiary(db, member_id)
        factories["list_policies"] = lambda: tools.list_policies(db, member_id, today=today)
    return await gather_tools(factories, allowed=allowed)


BENEFICIARY = Scenario(
    name="beneficiary",
    display_name="受益人變更",
    summary="準備變更受益人所需的資訊與法定限制",
    description=(
        "保戶要改受益人、問受益人可以填誰、或因為離婚、家庭狀況改變要換受益人時使用。"
        "這個情境準備變更所需的資訊，不會在這裡直接把受益人改掉。"
    ),
    injection=(
        "current_beneficiary 是空的時候，代表這位保戶名下沒有保單，不是系統查不到。\n"
        "current_beneficiary 是逐張保單列的，每一張的 beneficiaries 是那張契約上指定的人，"
        "帶 label（稱謂）與 share（比例）。回答時逐張講，不要把不同保單的受益人混在一起說——"
        "受益人是契約上的指定，不是這個人身上的一個屬性，同一位保戶不同保單指定不同人是常見的。\n"
        "某一張的 beneficiaries 是空陣列時，代表那張契約沒有指定受益人，"
        "照 undesignated_fallback 說明保險金將作為被保險人的遺產，並說明要指定的話要辦什麼手續。\n"
        "保戶要準備變更受益人，你的任務是說清楚規則、目前紀錄，以及接下來要怎麼送出正式變更——"
        "這個情境本身不會把受益人改掉。\n"
        "先說明保戶（要保人）有權指定或變更受益人，但引用工具回傳的條文並照 citation 欄位"
        "一字不差標註，例如〔保險法 第111條第2項〕；工具沒有回傳「通知才對保險人生效」這一條，"
        "就不要自己補一句進去，改說這部分需要查證。\n"
        "工具回傳保戶目前的受益人紀錄與名下保單時，先照原樣說一次目前紀錄上的受益人關係，"
        "以及有哪幾張保單會受這次變更影響。\n"
        "再說明怎麼正式送出：要保人本人要在「應簽署文件」完成受益人變更申請書的簽名，"
        "不得由他人代簽；新受益人如果是未成年人，申請書要一併附上法定代理人的身分證明，"
        "這是作業上的要求，不要幫這句話掛條號。文件送出、保險公司登記後才對保險人生效，"
        "不是講完這段對話就已經改好了，不可以說「已經幫您改好了」或類似的話。\n"
        "工具回傳的兩條遺產規定分別放在兩個欄位，是因為它們講的是相反的情形，"
        "不可以互相支援：undesignated_fallback 講的是沒有指定受益人時保險金歸入遺產，"
        "designated_protection 講的是有指定受益人時保險金不歸入遺產。"
        "說明哪一種情形，就只引用對應那個欄位裡的條號，"
        "拿另一條去佐證同一句話會引到一條說相反內容的真實條文。\n"
        "**§112 講的是身故保險金。** 它說的是死亡給付給指定受益人時不列入遺產，"
        "不是「有指定受益人的保單都不列入遺產」。住院日額、醫療實支實付這種被保險人生前領的"
        "給付根本不是遺產問題，不要把它們蓋進去——保戶可能拿這句去做遺產規劃。\n"
        "**只有寫著他要換掉的那個人的保單會被影響。** 他因為離婚要改，受影響的就是"
        "current_beneficiary 裡寫「配偶」的那幾張；寫法定繼承人的、沒有指定的，"
        "都不在這次變更範圍內。逐張講清楚哪幾張要改、哪幾張不用，"
        "不要說「本次變更會影響上述N張保單」把全部算進去。\n"
        "**簽署方式、代簽、未成年受益人要附什麼證明，這些工具沒有回傳。** "
        "不要自己列辦理須知——不是只有條號不能編，程序要求一樣不能編。"
        "需要講到怎麼辦時，說明會由公司提供變更申請書並告知應備文件。\n"
        "判斷用哪一條，看的是 current_beneficiary 有沒有回傳列，不是看那一列寫誰："
        "有列就是有指定，即使寫的是「法定繼承人」也是一種指定，要用 designated_protection；"
        "完全沒有列才是沒有指定，才用 undesignated_fallback。"
        "不要主動勸他要不要改指定別人，他問到才說明。\n"
        "不可以說變更一定會核准，不可以承認公司過去做錯，不可以講任何金額——"
        "金額由計算工具產生，這個情境沒有那個工具。資格是否符合、變更何時登記完成，"
        "由保險公司核定。\n"
        "不可以引用工具沒有回傳的條文，想不起來就說這部分需要查證，寫錯的條號比不寫更傷害保戶。\n"
        "工具回傳 _identity_required 時，表示保戶尚未完成身分核對，這次拿不到他目前的受益人紀錄"
        "與名下保單。此時照工具回傳的公開規定，把受益人可以怎麼指定或變更、通知才生效、"
        "以及沒有指定時的法定結果完整說一次——這部分不需要身分就能答，先把能答的答完，"
        "不要因為缺一半資料就整段都不答。答完再說明：要看他目前紀錄上的受益人是誰、"
        "以及會影響哪幾張保單，需要先核對身分，請他提供身分證字號。"
        "不要憑空講任何關於他保單或受益人的內容。"
    ),
    tools=(
        "current_beneficiary",
        "list_policies",
        "designation_rules",
        "undesignated_fallback",
        "designated_protection",
    ),
    tools_module="policydesk.agent.scenarios.beneficiary",
    params=(
        Param(
            name="concern",
            description=(
                "保戶想變更受益人的理由或情境，用他自己的話摘要成一句話，"
                "例如「離婚要換人」「原本受益人過世了」"
            ),
            example="我離婚了，想把受益人換掉",
            when_unsaid="他只是問一般規則、沒有提到具體理由時填「一般說明」，不要填空字串。",
        ),
    ),
    # Two of the three asked what the reply had just said: the injection lists the current
    # designations and explains the §113 fallback in every reply this scenario writes.
    quick_replies=("這個要準備什麼文件？", "受益人可以填未成年的孩子嗎？", "變更後從什麼時候生效？"),
    transitions=("policy_overview", "soothe"),
)
