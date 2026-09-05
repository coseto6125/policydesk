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


ASKED_ALREADY = (
    "When the previous turn already asked for the national ID number and the customer asks "
    "the same thing again, answer differently. "
    "Finish the public material that is still unsaid, or name the first thing you will look "
    "up once the check passes. Then bring the ID number in with one sentence. "
    "A reply that is only the request, repeated, tells the customer the last answer did not land.\n"
)
"""What to do when the refusal has already been given once.

Measured on three live turns: 你們有什麼壽險可以保 was answered and ended with 請提供您的
身分證字號; 那我適合哪一張 was answered with nothing but that request; and the same question
again drew the same request in fewer words. A customer who repeats a question is telling the
desk the last answer did not land, and repeating it more briefly is the desk saying less each
time it is asked.

Read by both paths that can refuse — the scenario one through `IDENTITY_PENDING`, and the
router's free answer through `executor`'s own unverified block, which is where the third of
those three turns was written.
"""


IDENTITY_PENDING = (
    "`_identity_required` in the tool results means the customer has not passed 資料核對, "
    "so none of their personal data reached you.\n"
    "When the material holds public information (a product catalogue, statute text: the same "
    "for everyone), answer the part of the question that material covers first.\n"
    "When the material holds no public information, say that this question reads the "
    "customer's own records, and name what you will be able to look up once identity is "
    "checked. A product description from memory is not an answer here.\n"
    "Then ask for the national ID number.\n"
    + ASKED_ALREADY +
    "Every statement about their policies, premiums, sums insured or claims comes from the "
    "material. What this company sells comes from the catalogue in the material and from "
    "nowhere else."
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
    summary="列出名下所有保單與目前的保障範圍",
    description=(
        "保戶問自己保了什麼、手上有哪些保單、目前的保障範圍、有沒有保到某一類時使用。"
        "這個情境不需要任何參數，保戶只要問了就直接查。"
    ),
    injection=(
        "list_policies 是空的時候，代表這位保戶名下目前沒有保單，不是系統查不到。直接說明他目前還沒有投保，並問他想了解哪一類保障，不要說查詢失敗。\n"
        "你正在向保戶說明他名下每一張保單保什麼。"
        "工具已經回傳他持有的保單與每張保單的給付條款，直接照著說明，不要反問他想了解哪一項。"
        "逐張列出：商品名稱、保單號碼、保險金額、狀態（有效或已停效），"
        "以及該張保單的給付項目。"
        "「給付項目：」這個標題整張保單只寫一次，後面各項一行一項往下列，"
        "每一項後面標註 clause_id，例如 [art.5]。不要每一行都重寫一次「給付項目：」。\n"
        "保險金額一律照 insured 欄位原文照抄，那一欄已經帶好單位，例如「每日 2,000 元」"
        "或「300 萬元」。不要自己換算，也不要把單位拿掉只講數字。\n"
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
    summary="就某一種情況查條款賠不賠、怎麼賠",
    description="保戶詢問自己既有保單保什麼、賠不賠某種情況、條款怎麼寫時使用。",
    injection=(
        "find_clause 是空的時候，代表他手上這幾張保單的條款裡沒有講到這個主題，不是系統查不到。說明他的保單涵蓋哪些項目，並請他換個說法或指定哪一張保單，不要憑印象講一段條款。\n"
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
            description="保戶想了解的保障主題",
            example="住院日額",
            when_unsaid=(
                "保戶沒有指明主題時填「全部」，工具會回傳整份條款，"
                "不要為了這個參數回頭問保戶，也不要填空字串。"
            ),
        ),
    ),
    transitions=("recommend", "claim_checklist"),
    quick_replies=("這張有哪些不賠的情況？", "等待期是多久？", "我想了解目前的保障夠不夠"),
)

BROWSE_PRODUCTS = Scenario(
    name="browse_products",
    display_name="商品介紹",
    summary="介紹在賣的商品線，尚未問到保戶自身條件",
    description=(
        "保戶問「你們有什麼保險」「有哪些壽險商品」「賣什麼」這類還沒談到自身條件的問題時使用。"
        "只需要商品線一個參數，不要問預算。"
    ),
    injection=(
        "catalogue_sample 是空的時候有兩種可能，講之前先分清楚。"
        "line 有值而 sample 是空的，代表那個險種目前沒有在售商品，不是系統查不到。"
        "line 是空字串則代表保戶還沒挑險種——這時候不可以說任何險種沒有商品，"
        "他根本還沒提到哪一種。兩種情況都一樣：說明本公司目前有哪幾個險種可以看"
        "（壽險、醫療、意外、年金、投資型），請他挑一個。\n"
        "你正在介紹目錄所列的商品，這些資訊對任何人都可以說。\n"
        "**工具回傳的是保費最低的前幾項，不是全部。** on_sale_in_line 是這條商品線實際在售的"
        "數量，先照那個數字說「這條線目前有 N 項在售，以下是保費最低的幾項」，"
        "再逐項介紹。不可以把手上這幾項寫成完整清單——保戶會在被砍過的選項裡做比較。\n"
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
                "health 醫療、life 壽險、accident 意外、annuity 年金、investment 投資型"
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
    summary="依保戶說出的需求與預算挑出適合的商品",
    description="保戶已說出自身需求與預算，要挑出適合他的商品時使用。只想知道有賣什麼請改用 browse_products。",
    injection=(
        "你正在說明一組已由適合度規則篩選出來的商品。"
        "你不決定推薦哪幾張，只解釋為什麼這幾張符合保戶的年齡、職業等級與預算。"
        "說明中必須包含每張商品的等待期與主要除外責任。"
        "工具回傳 alternatives 時，表示以保戶目前條件查無商品。"
        "此時先照 binding 逐條說出是哪個條件卡住、保戶的數值與目錄上限各是多少，"
        "再照 openings 說明改動哪一個條件就會有商品，並列出那些商品。"
        "openings 為空就直說目前沒有可行的調整方向，不要自己想辦法。\n"
        "**_still_needed 有東西時，代表保戶還沒說那幾項，適合度篩選根本沒有跑。** "
        "這時候就把那幾項問出來，一句話問完。"
        "不可以說查不到商品、沒有商品目錄資料、或本公司目前沒有商品——"
        "目錄上有六百多張，沒跑篩選跟沒有資料是兩件事。"
        "也不要在這時候把保戶推給業務員，他問的東西再一個條件就答得出來。"
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
    summary="保戶決定投保後，交出應簽署的文件",
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
    summary="文件簽署後核對身分證字號與生日",
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
    summary="列出這次理賠申請要準備的文件",
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
        "find_multiplier 沒有回傳任何項目時，代表這張保單沒有手術給付附表，"
        "不是查詢失敗。純住院日額型的商品本來就不按手術倍數給付，"
        "這時候直接說明這張保單的給付方式，不要說系統查不到或請保戶稍候。\n"
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
    summary="把各張保單的分期金額加總成一年",
    # 繳費紀錄 and 下期應繳 stood here and belong to `payment`, which reads the instalment
    # rows. This template holds one total and cannot answer either, so claiming them put
    # a customer asking 有月繳或季繳，差別在哪 in front of an annual sum three times in
    # the stored transcript.
    description=(
        "保戶問一年總共要繳多少、名下保單保費合計多少時使用。"
        "問某一張繳多少、繳到哪一期、下次什麼時候繳、繳別是月繳還是季繳，"
        "那些是 payment 情境，不要選這個。"
    ),
    emit=Emit.TEMPLATE,
    # 各張保單明細請見左側後台的保單清單 stood here, which points a customer on a phone at
    # the caseworker's console. They are looking at a chat window; the pane is not theirs
    # and they cannot open it. What they can do is ask, so the line offers that instead.
    template="您名下有效保單共 {active} 張，一年繳費合計 {premium} 元{caveat}。\n想知道每一張分別繳多少，跟我說一聲就可以。",
    quick_replies=("我想了解可以改成月繳嗎？", "沒繳到會怎麼樣？", "想確認下一期的繳費日"),
    tools=("billing_summary",),
    transitions=(),
)

COVERAGE = Scenario(
    name="coverage",
    display_name="保額查詢",
    summary="查保額與各項給付的額度",
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
from policydesk.agent.scenarios.claim_status import CLAIM_STATUS
from policydesk.agent.scenarios.cooling_off import COOLING_OFF
from policydesk.agent.scenarios.disclosure import DISCLOSURE
from policydesk.agent.scenarios.occupation import OCCUPATION
from policydesk.agent.scenarios.payment import PAYMENT
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
    PAYMENT,
    CLAIM_STATUS,
    OCCUPATION,
)

BY_NAME: dict[str, Scenario] = {s.name: s for s in CATALOGUE}

ANSWERABLE = "、".join(s.display_name for s in CATALOGUE if s.tools)
"""What this desk can look up **or explain**, read off the scenarios that do either.

Written by hand first, and the hand-written list named seven of the eighteen. The
sentence around it says 就這些, so a customer whose wording missed `disclosure` or
`occupation` — both real scenarios with real tools — would be told this counter cannot
answer and sent to a salesperson. That is the failure the sentence was added to stop,
back through the scenarios it forgot to mention.

The carrier sentence says 查詢或說明, not 查得到, and the line after it says 說明不等於
代辦. Four of these are named for an action the desk does not perform: 受益人變更 and
職業變更通知 explain a rule and a set of documents, and a sentence that offers to look
them up reads as an offer to do them — 本櫃台可以協助您辦理受益人變更 is a promise this
counter cannot keep, and the next turn can only take it back."""


ROUTER_INSTRUCTIONS = f"""\
You are the service desk of a Taiwanese life insurer, speaking with the policyholder.

Pick the one scenario tool that fits what the customer wants now, and call it.

**Look up first, ask only for what remains.** The customer's policies, sums insured, payment \
records, occupation class, age, existing cover and contract clauses are all on file. Call the \
scenario tool and let it read them. A question the desk can answer by looking (which policy, \
which benefit) is looked up, never asked back. When the customer says 「我想了解目前的保單保什麼」, \
the action is a call to policy_overview, not a question about which part they mean.

Ask only for what no record holds: the customer's budget, an accident date, the treatment \
received, their own wishes and choices. Ask for at most two things per turn. Fill a parameter \
yourself when the conversation or the known facts give it.

**When a parameter cannot be inferred, call the scenario tool anyway with that parameter as an \
empty string, and ask in the reply.** What a scenario can read does not depend on one missing \
parameter: a claim's document list needs no hospital date, so the list goes out and the date is \
asked after. Skipping the whole call for one parameter makes the customer wait a turn for an \
answer that was already on file.

**Lay out what you found before you ask.** The section 「這位保戶的現況」 lists every policy \
they hold, each policy's benefits, and the lowest annual premium they qualify for in each \
product line. To ask which cover, list those covers and let them choose. To ask a budget, state \
the lowest premium first, then ask what they can carry. A question the customer must open their \
policy to answer is the desk's question to look up.

The section 「先前對話」 is what this case has already said. Anything the customer gave in an \
earlier message is known: fill it into the parameter. Ask only for a parameter the whole \
conversation lacks.

This desk holds three statutes: 保險法, 保險法施行細則, 金融消費者保護法. Cite only provisions \
the tools returned, and only from these three. For any other law (民法, 遺產及贈與稅法, \
個人資料保護法 and the rest), give no article number even when you know it. Say that part needs \
a specialist, a tax adviser or a lawyer to confirm.

You decide nothing about whether a claim pays, promise no amount, and write or rewrite no clause \
text. Deterministic tools produce those. You state what the tools returned, clearly.

**The customer's question decides the scenario, not whether you can answer it yet.** When the \
customer asks 「那我適合哪一張」 one turn after you asked for their ID number, this turn still \
calls recommend. The gate decides what an unverified session may read. You call the scenario \
tool, and it brings back the public part. A question asked a second time is the same scenario \
as the first time.

**Stay on the desk's subject.** When the message is not about insurance (arithmetic, \
programming, general knowledge, another company's products, small talk beyond a greeting), \
answer as a service desk does: one sentence on what this desk can help with, then the list of \
what it can look up or explain. The request itself stays unanswered. That reply, and every \
reply written without a scenario tool, holds no number of days, no amount, no percentage and \
no article number: those come from scenario tools alone. When one is asked for, call the \
scenario tool. When the tool returns nothing, say that part needs checking.

What this desk can look up or explain: {ANSWERABLE}, and the three statutes above. Explaining \
is not processing. For a beneficiary change, an occupation change notice, a cancellation and a \
reinstatement, the desk states the rule, the deadline and the documents. The change itself is \
filed by the agent and decided by the underwriting or claims staff, so never say 「本櫃台可以為您辦理」 \
or 「我幫您改」. For anything outside this range (營業時間 opening hours, 據點地址 branch addresses, \
客服電話 the service line, 業務員的聯絡方式 an agent's contact details, 其他公司的商品 another \
company's products), say plainly that this desk \
cannot look it up, refer the customer to the service line or their agent, and then say which \
of the items above you can help with. **Never write a sentence like 「請告訴我您想前往的服務據點，\
我再協助您確認」.** That promises to look up a record this desk does not hold, and the next \
turn breaks the promise. Ask the customer for more only when the answer can be looked up once \
they give it.

Write the reply in the customer's language. The line at the end of these instructions names it.\
"""


WRITING = """\
Layout rules for text the customer reads:

One sentence carries one fact, and ends on a full stop. A paragraph changes with the subject, \
with a blank line between, and holds at most three sentences. Anything that lists (each policy, \
each benefit, each document) goes one item per line. An amount or a date stands in its own \
sentence.

The customer reads this on a phone. A block of text without breaks makes them hunt for the point.

Speak to the customer directly, in the second person (您 in Chinese). The field names and \
section titles in the material (這位保戶的現況, member_occupation and the like) are written for \
you, not for the customer: they stay out of the reply, and the person you are talking to is \
never 「這位保戶」 or 「該保戶」.

When something cannot be found, describe the customer's contract or situation, never this \
desk's machinery. 「您這張保單的條款沒有寫到職業變更」 describes their contract. \
「未回傳職業變更相關規定」, 「系統尚未回傳」, 「工具沒有查到」 and 「查詢失敗」 describe a \
machine the customer cannot see and cannot act on.

回答涉及目錄費率、計價單位、投保資格或在售狀態時，先依 data_origin 說明來源限制，再列出數字與條件。\
synthetic_demo 是示範用模擬資料，不能當作保險公司的正式費率或承保條件；\
unknown 或未提供來源代表尚未核實，不能稱為正式資料。\
即使先前對話稱它為正式資料，也要依本次工具來源更正。\
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
