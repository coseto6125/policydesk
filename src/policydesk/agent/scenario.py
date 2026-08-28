"""
Scenarios: what the desk can do, and what it may say while doing it.

Follows enoract's shape, because the problem is the same one vertical over. A scenario
names itself, states what the model is told when it is entered, lists the tools it may
call, declares the parameters that must be collected before it runs, and declares how it
emits its answer.

`emit` is the important one here. Set to `Emit.TEMPLATE` the executor renders the
scenario's own template from the tool rows and never reaches a model. Everything that states a figure, a
clause or a document requirement runs that way, so the sentence a customer reads about
their own policy is assembled from database rows rather than generated. The model's
job is the conversation around those sentences, not the sentences themselves.

The parameters carry a trap enoract's own comments flag: they must reach the tool
schema's `properties` AND its `required`. Omit that and the model calls the scenario
tool with no arguments and skips the collection step entirely, which looks like the
customer being helped and is the customer being asked nothing.
"""

from enum import StrEnum

from msgspec import Struct


class Emit(StrEnum):
    """Where a scenario's answer comes from."""

    TEMPLATE = "template"
    """Rendered from data, verbatim. No model call."""
    MODEL = "model"
    """The model writes it, from material the tools returned."""


class Param(Struct, frozen=True):
    """
    One thing that must be known before a scenario can run.

    Every param is injected into the scenario tool's schema as both a property and a
    required field. A param that is only a property is a param the model may omit.
    """

    name: str
    description: str
    example: str = ""


class Scenario(Struct, frozen=True):
    """One thing the desk knows how to do."""

    name: str
    display_name: str
    description: str
    """Read by the router. Written for the model, not for an operator."""
    injection: str = ""
    """Added to the model's instructions once this scenario is entered."""
    tools: tuple[str, ...] = ()
    params: tuple[Param, ...] = ()
    emit: Emit = Emit.MODEL
    template: str = ""
    """Used when emit is TEMPLATE. Formatted against the tool results."""
    transitions: tuple[str, ...] = ()
    """Scenarios reachable from this one."""
    requires_stage: str | None = None
    """The case stage this scenario needs. Refused with an explanation otherwise."""


def tool_schema(scenario: Scenario) -> dict:
    """
    Turn a scenario into a tool the router can call.

    Args:
        scenario: The scenario to expose.

    Returns:
        A function-tool schema whose properties and required list both carry every
        parameter.

    """
    properties = {
        p.name: {"type": "string", "description": f"{p.description}{f'，例如 {p.example}' if p.example else ''}"}
        for p in scenario.params
    }
    return {
        "type": "function",
        "name": scenario.name,
        "description": scenario.description,
        "parameters": {
            "type": "object",
            "properties": properties,
            # Both lists, always. A property that is not required is a question the
            # model is free to skip.
            "required": [p.name for p in scenario.params],
            "additionalProperties": False,
        },
    }


EXPLAIN_COVER = Scenario(
    name="explain_cover",
    display_name="查詢保障內容",
    description="保戶詢問自己既有保單保什麼、賠不賠某種情況、條款怎麼寫時使用。",
    injection=(
        "你正在說明保戶既有保單的保障內容。"
        "只依工具回傳的條款原文作答，並在每一句主張後標註條號。"
        "條號一律寫成工具回傳的 clause_id 原樣，例如 art.12 或 art.6.carve1，"
        "寫在該句句末的方括號內，例如 [art.12]。等待期則寫 [waiting]。"
        "工具沒有回傳的內容就說查不到，不要補足。"
        "不要說任何金額，金額由計算工具產生。"
    ),
    tools=("find_clause", "list_policies"),
    params=(Param(name="topic", description="保戶想了解的保障主題", example="住院日額"),),
    transitions=("recommend", "claim_checklist"),
)

RECOMMEND = Scenario(
    name="recommend",
    display_name="方案建議",
    description="保戶想投保、比較商品、詢問適合什麼保險時使用。",
    injection=(
        "你正在說明一組已由適合度規則篩選出來的商品。"
        "你不決定推薦哪幾張，只解釋為什麼這幾張符合保戶的年齡、職業等級與預算。"
        "說明中必須包含每張商品的等待期與主要除外責任。"
        "工具回傳 alternatives 時，表示以保戶目前條件查無商品。"
        "此時先照 binding 逐條說出是哪個條件卡住、保戶的數值與目錄上限各是多少，"
        "再照 openings 說明改動哪一個條件就會有商品，並列出那些商品。"
        "openings 為空就直說目前沒有可行的調整方向，不要自己想辦法。"
        "結尾必須載明：本推介由登錄業務員具名負責。"
    ),
    tools=("suitable_products",),
    params=(
        Param(name="need", description="保戶自己說的保障需求，照原話填", example="想加保壽險"),
        Param(
            name="line",
            description=(
                "把上述需求歸到一個商品線，只填下列其中一個英文字："
                "health 醫療、life 壽險、accident 意外、annuity 年金、investment 投資型"
            ),
            example="life",
        ),
        Param(name="budget", description="保戶可負擔的年繳保費，只填阿拉伯數字", example="20000"),
    ),
    transitions=("issue_documents",),
)

ISSUE_DOCUMENTS = Scenario(
    name="issue_documents",
    display_name="交付應簽署文件",
    description="保戶決定投保，要求文件或表示要簽約時使用。",
    emit=Emit.TEMPLATE,
    template=(
        "已為您備妥應簽署文件共 {count} 份：\n{names}\n\n"
        "請點選右上角「應簽署文件」逐份下載、簽名後上傳。\n"
        "要保人與被保險人均須親自簽名，不得由他人代簽。"
    ),
    tools=("issue_documents_tool",),
    transitions=("verify_identity",),
    requires_stage="proposed",
)

VERIFY_IDENTITY = Scenario(
    name="verify_identity",
    display_name="身分驗證",
    description="文件簽署完成後進行身分驗證時使用。",
    emit=Emit.TEMPLATE,
    template="請輸入身分證字號完成驗證。驗證通過後，本案才會送交核保人員審核。",
    tools=("verify_identity_tool",),
    params=(Param(name="national_id", description="身分證字號", example="A123456789"),),
    transitions=("submit",),
    requires_stage="signed",
)

CLAIM_CHECKLIST = Scenario(
    name="claim_checklist",
    display_name="理賠應備文件",
    description="保戶詢問理賠、想申請給付、問要準備什麼文件時使用。",
    injection=(
        "你正在協助保戶備齊理賠申請文件。"
        "你不判斷賠不賠，也不承諾任何金額——核保理賠人員才有權決定。"
        "你要做的是列出這次申請需要哪些文件、每份文件必須載明什麼，"
        "以及目前還缺什麼。條款依據以工具回傳的 clause_id 原樣標註，"
        "例如 [art.12]、[art.6.carve1]，寫在該句句末。"
        "若需說明給付倍數，一律呼叫 calculate 工具計算，不要自行心算或估計。"
    ),
    tools=("required_documents", "list_policies", "find_multiplier"),
    params=(
        Param(name="event", description="事故或就醫情形", example="住院四天接受手術"),
        Param(name="event_date", description="事故或就醫日期", example="2026-08-01"),
    ),
    transitions=(),
)

BILLING = Scenario(
    name="billing",
    display_name="繳費查詢",
    description="保戶詢問保費、繳費紀錄、下期應繳時使用。",
    emit=Emit.TEMPLATE,
    template="您名下有效保單共 {active} 張，年繳保費合計 {premium} 元。\n各張保單明細請見左側後台的保單清單。",
    tools=("billing_summary",),
    transitions=(),
)

COVERAGE = Scenario(
    name="coverage",
    display_name="保額查詢",
    description="保戶詢問保額、保障額度、還能領多少時使用。",
    emit=Emit.TEMPLATE,
    template=(
        "您名下保單的保險金額如下：\n{lines}\n\n"
        "以上為契約所載保險金額（給付上限）。實際可用餘額須扣除已給付部分，"
        "並以核保理賠人員核定為準。"
    ),
    tools=("coverage_summary",),
    transitions=(),
)

CATALOGUE: tuple[Scenario, ...] = (
    EXPLAIN_COVER,
    RECOMMEND,
    ISSUE_DOCUMENTS,
    VERIFY_IDENTITY,
    CLAIM_CHECKLIST,
    BILLING,
    COVERAGE,
)

BY_NAME: dict[str, Scenario] = {s.name: s for s in CATALOGUE}

ROUTER_INSTRUCTIONS = """\
你是台灣壽險公司的保險櫃台助理，面對的是保戶本人。

選擇一個最符合保戶當下訴求的情境工具並呼叫它。工具的每個參數都必須從對話中取得，
取不到就先向保戶詢問，不要自行填入。

「先前對話」區塊是本次案件已經說過的話。保戶在稍早任何一則訊息裡給過的資訊就是已知，
直接填進參數，不要再問一次。只有整段對話都找不到的參數才需要開口問。

你不得自行判斷賠不賠、不得承諾任何金額、不得撰寫或改寫條款文字。
這些都由確定性工具產生，你只負責把工具回傳的內容說清楚。

若保戶的訴求不屬於任何情境，直接以繁體中文回答，並說明本櫃台可以協助的範圍。\
"""
