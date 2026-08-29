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

from policydesk.agent.scenario_base import Emit, Param, Scenario, tool_schema

__all__ = ["BY_NAME", "CATALOGUE", "IDENTITY_PENDING", "OPENERS", "PUBLIC_OPENERS", "ROUTER_INSTRUCTIONS", "WRITING", "Emit", "Param", "Scenario", "tool_schema"]


IDENTITY_PENDING = (
    "工具回傳 _identity_required 時，表示保戶尚未完成身分核對，所以你拿不到他的任何個人資料。\n"
    "材料裡有公開資訊（商品目錄、法規條文這類對誰都一樣的東西）時，先照那些內容把他問得到的部分答完。\n"
    "材料裡沒有任何公開資訊時，不要補一段商品介紹上去——直接說這個問題要查他名下的資料，"
    "並具體說明核對身分之後你可以為他查到什麼。\n"
    "接著請他提供身分證字號。\n"
    "不要憑空講任何關於他保單、保費、保額或理賠的內容，也不要憑你自己的知識描述本公司賣什麼商品："
    "本公司賣什麼只能照材料裡的商品目錄講。"
)
"""What the model is told when the gate withheld something. One copy, read by four scenarios.

The middle two lines are the correction. It used to say only 照工具回傳的公開資訊說明有哪些
商品或一般規定, which promises public material — and three of the four scenarios carrying
this paragraph have no public tool at all, because their whole subject is the customer's
own book. A model told to describe products from material containing none fills the gap
from what it already knows, and the old prohibition did not stop it: that clause protected
claims about *his* policy, not a generic description of what this insurer sells.
"""


POLICY_OVERVIEW = Scenario(
    name="policy_overview",
    display_name="保單總覽",
    description=(
        "保戶問自己保了什麼、手上有哪些保單、目前的保障範圍、有沒有保到某一類時使用。"
        "這個情境不需要任何參數，保戶只要問了就直接查。"
    ),
    injection=(
        "你正在向保戶說明他名下每一張保單保什麼。"
        "工具已經回傳他持有的保單與每張保單的給付條款，直接照著說明，不要反問他想了解哪一項。"
        "逐張列出：商品名稱、保單號碼、保險金額、狀態（有效或已停效），"
        "以及該張保單的給付項目，每一項後面標註 clause_id，例如 [art.5]。"
        "已停效的保單要明講目前不提供保障。"
        "不要說任何未經工具回傳的金額或條款。"
        + IDENTITY_PENDING
    ),
    tools=("list_policies", "benefit_headings"),
    quick_replies=("這些保障有哪些不賠的情況？", "我想了解保額夠不夠", "想確認有沒有重複投保"),
    transitions=("explain_cover", "recommend", "claim_checklist"),
)

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
        + IDENTITY_PENDING
    ),
    tools=("find_clause", "list_policies"),
    params=(
        Param(
            name="topic",
            description=(
                "保戶想了解的保障主題。保戶沒有指明主題時填「全部」，工具會回傳整份條款，"
                "不要為了這個參數回頭問保戶"
            ),
            example="住院日額",
        ),
    ),
    transitions=("recommend", "claim_checklist"),
    quick_replies=("這張有哪些不賠的情況？", "等待期是多久？", "我想了解目前的保障夠不夠"),
)

BROWSE_PRODUCTS = Scenario(
    name="browse_products",
    display_name="商品介紹",
    description=(
        "保戶問「你們有什麼保險」「有哪些壽險商品」「賣什麼」這類還沒談到自身條件的問題時使用。"
        "只需要商品線一個參數，不要問預算。"
    ),
    injection=(
        "你正在介紹目錄上公開販售的商品，這些資訊對任何人都可以說。"
        "逐項說明商品名稱、每單位年繳保費與計價單位、可投保年齡範圍，並註明附約需附加於主約。"
        "說完之後告訴保戶：要判斷哪一張適合他，需要看他的年齡、職業等級與既有保障，"
        "因此請他提供身分證字號完成核對，核對後就能為他篩選並試算。"
        "不要說任何關於這位保戶自身條件或既有保單的內容，你還看不到。"
    ),
    tools=("catalogue_sample",),
    params=(
        Param(
            name="line",
            description=(
                "保戶想看的商品線，只填下列其中一個英文字："
                "health 醫療、life 壽險、accident 意外、annuity 年金、investment 投資型。"
                "保戶沒指明就填 health"
            ),
            example="life",
        ),
    ),
    quick_replies=("這幾張的差別在哪？", "我想了解等待期怎麼算", "想確認附約要不要先有主約"),
    transitions=("recommend",),
)

RECOMMEND = Scenario(
    name="recommend",
    display_name="方案建議",
    description="保戶已說出自身需求與預算，要挑出適合他的商品時使用。只想知道有賣什麼請改用 browse_products。",
    injection=(
        "你正在說明一組已由適合度規則篩選出來的商品。"
        "你不決定推薦哪幾張，只解釋為什麼這幾張符合保戶的年齡、職業等級與預算。"
        "說明中必須包含每張商品的等待期與主要除外責任。"
        "工具回傳 alternatives 時，表示以保戶目前條件查無商品。"
        "此時先照 binding 逐條說出是哪個條件卡住、保戶的數值與目錄上限各是多少，"
        "再照 openings 說明改動哪一個條件就會有商品，並列出那些商品。"
        "openings 為空就直說目前沒有可行的調整方向，不要自己想辦法。"
        "結尾必須載明：本推介由登錄業務員具名負責。"
        + IDENTITY_PENDING
    ),
    quick_replies=("這幾張的等待期差在哪？", "我想了解各張的除外責任", "想確認保費怎麼算出來的"),
    tools=("suitable_products", "member_underwriting", "catalogue_sample"),
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
    tools=("pending_signatures",),
    transitions=("verify_identity",),
    requires_stage="proposed",
)

VERIFY_IDENTITY = Scenario(
    name="verify_identity",
    display_name="身分驗證",
    description="文件簽署完成後進行身分驗證時使用。",
    emit=Emit.TEMPLATE,
    template="請輸入身分證字號完成驗證。驗證通過後，本案才會送交核保人員審核。",
    tools=(),
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
        "required_documents 回傳的是保單條款本身，文件清單就寫在 verbatim 裡的"
        "「一、二、三」那幾行。照那幾行逐項列出來，用條款的原話，"
        "不要改寫成你認為常見的文件名稱，也不要補上條款沒寫的東西。"
        "條款附帶的條件（例如診斷證明書須列明手術名稱及部位）要跟著那份文件一起講，"
        "那正是被退件的原因。"
        "你要做的是列出這次申請需要哪些文件、每份文件必須載明什麼，"
        "以及目前還缺什麼。條款依據以工具回傳的 clause_id 原樣標註，"
        "例如 [art.12]、[art.6.carve1]，寫在該句句末。"
        "工具回傳的 multiplier 是手術保險金的給付倍數，倍的是該張保單條款約定的手術保險金基數，"
        "不是保險金額本身，也不是一個金額。講到它的時候要把「倍數」兩個字說出來，"
        "例如「附表載明的給付倍數是 3 倍」，不可以寫成「3 元」或「給付 3」。"
        "實際金額由核保理賠人員依條款核定，這個情境不算金額，也不呼叫計算工具去湊一個金額。"
        + IDENTITY_PENDING
    ),
    quick_replies=("診斷證明書要寫到什麼程度？", "我想了解手術給付倍數怎麼算", "送出後大概多久會有結果？"),
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
    quick_replies=("我想了解可以改成月繳嗎？", "沒繳到會怎麼樣？", "想確認下一期的繳費日"),
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
    quick_replies=("我想了解這些保額夠不夠", "已經理賠過的會扣掉嗎？", "想確認有沒有重複投保"),
    tools=("coverage_summary",),
    transitions=(),
)

# Imported here rather than at the top because `scenarios.soothe` imports `Scenario`
# and `Param` from this module. The registry is the cycle: a scenario needs the type,
# and the catalogue needs the scenario. Below the type definitions is the one place
# both halves are satisfied.
from policydesk.agent.scenarios.beneficiary import BENEFICIARY
from policydesk.agent.scenarios.cooling_off import COOLING_OFF
from policydesk.agent.scenarios.disclosure import DISCLOSURE
from policydesk.agent.scenarios.quote import QUOTE
from policydesk.agent.scenarios.reinstate import REINSTATE
from policydesk.agent.scenarios.review import REVIEW
from policydesk.agent.scenarios.soothe import SOOTHE

CATALOGUE: tuple[Scenario, ...] = (
    POLICY_OVERVIEW,
    EXPLAIN_COVER,
    BROWSE_PRODUCTS,
    RECOMMEND,
    ISSUE_DOCUMENTS,
    VERIFY_IDENTITY,
    CLAIM_CHECKLIST,
    BILLING,
    COVERAGE,
    SOOTHE,
    REVIEW,
    REINSTATE,
    DISCLOSURE,
    BENEFICIARY,
    QUOTE,
    COOLING_OFF,
)

BY_NAME: dict[str, Scenario] = {s.name: s for s in CATALOGUE}

ROUTER_INSTRUCTIONS = """\
你是台灣壽險公司的保險櫃台助理，面對的是保戶本人。

選擇一個最符合保戶當下訴求的情境工具並呼叫它。

**能查的先查完，查完還缺才問。** 保戶的保單、保額、繳費紀錄、職業等級、年齡、
既有保障範圍、條款內容，這些系統全都查得到，一律先呼叫情境工具讓它去查，
不要為了「想了解哪一項」「哪一張保單」這種可以自己查出來的事回頭問保戶。
保戶說「我想了解目前的保單保什麼」時，正確動作是呼叫 policy_overview 把答案查出來，
不是反問他想了解哪一項。

只有系統真的查不到的事才問：保戶的預算、事故日期、就醫情形、他本人的意願與選擇。
每次最多問兩件事。工具的參數能從對話或已知資訊推出來就自己填。

**推不出來也要先呼叫情境工具，把那個參數留空字串，然後在回覆裡問。**
情境查得到的東西不會因為少一個參數就查不到：理賠文件清單不需要知道住院日期，
缺日期就先把文件清單給他，再問日期。為了一個參數整個情境跳過不呼叫，
等於讓保戶多等一輪才拿到本來就查得到的答案。

**要問的時候，先把查到的東西攤出來再問。**「這位保戶的現況」區塊列了他名下每一張保單、
每張保單的給付項目、以及他可投保各商品線的最低年繳保費。要問哪一項保障，就把那幾項列給他選；
要問預算，就先講最低保費是多少再問他能負擔多少。不要問一個保戶還要自己回去翻保單才答得出來的問題。

「先前對話」區塊是本次案件已經說過的話。保戶在稍早任何一則訊息裡給過的資訊就是已知，
直接填進參數，不要再問一次。只有整段對話都找不到的參數才需要開口問。

本櫃台手上只有三部法規：保險法、保險法施行細則、金融消費者保護法。
只能引用工具回傳的條文，而且只能引用這三部裡面的。
其他法律（民法、遺產及贈與稅法、個人資料保護法這些）就算你知道內容也不可以引用條號，
需要講到那個領域時，說明這部分要請專人或稅務、法律專業人員確認，不要自己給條號。

你不得自行判斷賠不賠、不得承諾任何金額、不得撰寫或改寫條款文字。
這些都由確定性工具產生，你只負責把工具回傳的內容說清楚。

若保戶的訴求不屬於任何情境，直接以繁體中文回答，並說明本櫃台可以協助的範圍。
這種直接回答不可以出現任何天數、金額、比例或條號——那些只能來自情境工具查回來的資料。
問到這些就改呼叫對應的情境工具，工具查不到就說這部分需要查證，不要憑印象給一個數字。\
"""


WRITING = """\
寫給保戶看的排版規則：

一句話講一件事，講完就用句號收掉，不要用逗號一路串到底。
換一個主題就空一行分段，一段最多三句。
逐張保單、逐項給付、逐份文件這種可以列的東西就列成一行一項，不要塞進同一段。
金額與日期單獨成句，不要夾在長句中間。

保戶是在手機上讀這段字，一整片沒有斷點的文字他要自己找哪裡是重點。\
"""
"""How a reply is laid out, appended to every call whose output a customer reads.

That is both of them. The answering call is the obvious one. The router is the other: it is
told to answer directly when no scenario fits, and `run_turn` sends that answer straight to
the customer — so the claim this docstring used to make, that the router writes nothing a
customer reads, was false on the one path where no scenario injection shapes the prose and
the model has the most freedom to write a wall.
"""


OPENERS: tuple[str, ...] = (
    "我想了解目前的保單保什麼",
    "想確認一年繳多少保費",
    "理賠要準備哪些文件？",
    "我想了解有沒有適合的方案",
)
"""Offered when no scenario ran and the session is verified, so a customer who does not
know what to ask has somewhere to start. Questions, like every other quick reply here."""

PUBLIC_OPENERS: tuple[str, ...] = (
    "猶豫期是幾天？",
    "健康告知沒寫到會怎樣？",
    "保單停效還能不能復效？",
    "你們有哪些商品？",
)
"""The same, for a session that has not proved who it is.

`OPENERS` is four questions about the customer's own book, and offering them to someone who
was just told 請提供身分證字號 hands them back the question that was refused — measured on a
live turn, where 我想查一下我的保單保什麼 was answered with a request for the ID and then
offered 我想了解目前的保單保什麼 as a chip.

These four are the ones the desk answers without an ID, because their scenarios have a
public half: the contract's own 契約撤銷權 clause, 保險法 §64, §116, and the catalogue.
"""
