"""
Regressions for the back office markup and stylesheet.

The page is one file with no build step, so these read it as text. Each one guards a
defect that was visible on screen and invisible in the code.
"""

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

PAGE = Path("src/policydesk/web/static/index.html").read_text()


def test_hidden_beats_the_author_display_rules():
    """
    `.decide { display: flex }` outranks the UA rule for [hidden], so `.hidden = true`
    did nothing and the desk offered an approve button on a case at 詢問. The server
    refuses it, so the button could only ever fail — the fix is the page not offering it.
    """
    assert "[hidden] { display: none !important; }" in PAGE


def test_every_colour_token_is_defined_on_bare_root():
    """
    A token whose only definition sits inside a media query never applies in the
    un-stamped state, and the page then renders one theme's text on the other's ground.
    """
    root = PAGE[PAGE.index(":root {"):PAGE.index(':root:not([data-theme="light"]) { }')]
    dark = PAGE[PAGE.index(':root[data-theme="dark"] {'):]
    dark = dark[:dark.index("\n  }")]
    declared = {line.split(":")[0].strip() for line in root.splitlines() if line.strip().startswith("--")}
    for line in dark.splitlines():
        if (name := line.strip()).startswith("--"):
            assert name.split(":")[0].strip() in declared, f"{name} has no light-mode definition"


def test_the_body_paints_its_own_background():
    """The host paints its ground in its own theme, so a transparent body borrows it."""
    body = PAGE[PAGE.index("\n  body {"):PAGE.index("\n  :where(button")]
    assert "background: var(--ground)" in body


@pytest.mark.parametrize(
    "panel", ["保單清單", "應簽署文件", "身分驗證", "稽核軌跡", "案件佇列", "要保人 · 被保險人"]
)
def test_the_back_office_carries_the_panels_a_service_console_has(panel: str):
    assert panel in PAGE


def test_the_policy_table_reads_the_snapshot_policies():
    """The agent points a customer at this panel; it renders the member's own rows."""
    assert "renderPolicies(c.policies ?? []" in PAGE
    assert 'id="policyTable"' in PAGE


def test_status_is_never_carried_by_colour_alone():
    """Every pill ships a glyph beside its word, so it survives a colour-blind reader."""
    pills = PAGE[PAGE.index("const PILLS = {"):PAGE.index("function bubble(")]
    for kind in ("ok:", "no:", "wait:", "flag:"):
        line = pills[pills.index(kind):]
        assert "icon(" in line[:120], f"{kind} pill has no glyph"


def test_the_queue_answers_the_keyboard():
    """It is navigation, so it is reachable without a mouse."""
    assert 'role="button" tabindex="0"' in PAGE
    assert 'queueList").addEventListener("keydown"' in PAGE


def test_reduced_motion_is_honoured():
    assert "@media (prefers-reduced-motion: reduce)" in PAGE


def test_the_contract_routes_are_guarded_like_the_document_route():
    # 200 for a holding and 404 for a miss is an oracle: 48 members by 660 products maps
    # who holds what, from anywhere that can reach the port, with `?member=` as the only
    # authorisation and the requester choosing it. The document route already carried this
    # reasoning; these three did not.
    source = Path("src/policydesk/web/server.py").read_text()
    for route in ("clause_page", "contract", "contract_page", "download_document"):
        at = source.index(f"async def {route}(")
        body = source[at:source.index("\n@app.", at) if "\n@app." in source[at:] else len(source)]
        assert "_unauthorised(request" in body[:2000], f"{route} answers an id without a token"


def test_every_link_the_page_builds_carries_the_token():
    # A guarded route the page calls without the token is a feature that 403s in the demo.
    page = Path("src/policydesk/web/static/index.html").read_text()
    for link in ("/contract/${encodeURIComponent(p.product_id)}", "/clause/${encodeURIComponent(c.product_id)}"):
        at = page.index(link)
        assert "token=" in page[at:at + 260], f"{link} is built without a token"


def test_every_link_the_viewer_page_builds_carries_the_token():
    # The page images were updated when `contract` gained its guard and the download link
    # was not, so the one control on the viewer that opens the actual file 403'd. A guard
    # is only finished when every caller of the guarded route passes through it.
    source = Path("src/policydesk/web/server.py").read_text()
    body = source[source.index("def _render_contract("):]
    for link in ("/page/", "download=1"):
        at = body.index(link)
        window = body[max(0, at - 220):at + 60]
        assert "token=" in window, f"the viewer builds {link} without a token"


def test_the_desk_addresses_the_customer_and_not_a_third_party():
    """
    「這位保戶目前紀錄為商業潛水員」 reached a customer in a live run.

    The material's own section header is 這位保戶的現況 — a label written for the model, and
    the model read it as the way to refer to the person it was talking to. A desk that says
    這位保戶 to someone's face is a desk discussing them with somebody else.
    """
    from policydesk.agent.scenario import WRITING

    assert "in the second person" in WRITING
    assert "這位保戶" in WRITING, "the wrong form must be named, not only the right one"
    assert "not for the customer" in WRITING, "and the reason — the labels are for the model"


def test_a_folded_card_keeps_its_header_on_screen():
    """
    The card's collapsed state was called `folded`, which is also the enrolment card's
    summary line — a class that hides itself. Collapsing 保單清單 made the whole card,
    header included, `display: none`, and a caseworker had no chevron left to reopen it.
    """
    assert ".card.shut > .body { grid-template-rows: 0fr; }" in PAGE
    assert 'classList.toggle("folded"' not in PAGE
    assert ".folded { display: none;" in PAGE, "the enrolment summary line still owns that name"


@pytest.mark.parametrize("issued", [0, 9, 10])
@pytest.mark.parametrize("simulated", [False, True])
@pytest.mark.parametrize("completed", [False, True])
def test_render_case_required_missing_documents_remain_visible_and_pending(issued, simulated, completed):
    """Execute the page's renderers; a missing form must not turn nine signatures into 9/9."""
    node = shutil.which("node")
    assert node is not None, "UI renderer tests require Node.js on PATH"
    functions = []
    for name in ("renderBoard", "renderCase", "renderPolicies", "renderModalDocs", "updateDocumentDemoControls"):
        start = PAGE.index(f"function {name}(")
        end = PAGE.index("\n}\n", start) + len("\n}\n")
        functions.append(PAGE[start:end])
    signed = issued if completed else 0
    snapshot = {
        "case_id": 1, "case_version": 1, "stage": "verified", "policies": [],
        "documents": [
            {
                "document_id": index + 1, "kind": f"文件{index + 1}",
                "signed_at": "2026-09-05" if completed else None, "signature_simulated": simulated,
            }
            for index in range(issued)
        ],
        "document_status": {
            "signed": signed, "total": 10, "pending": 10 - signed,
            "unissued": [f"文件{index + 1}" for index in range(issued, 10)],
        },
    }
    harness = r"""
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const nodes = {};
const $ = id => nodes[id] ??= {classList: {toggle() {}}, dataset: {}, querySelectorAll: () => [], setAttribute() {}};
let caseId = null, lastSnapshot = null, lastStage = null, lastBoard = {};
let documentDemoAvailable = false, documentDemoRetry = false, documentDemoBusy = false;
const STAGES = ['inquiry','proposed','issued','signed','verified','review','approved','rejected'].map(s => [s, s]);
const STAGE_NAMES = Object.fromEntries(STAGES);
const TILE_GLYPH = {done:'',wait:'',flag:'',bad:'',open:''};
const PILLS = Object.fromEntries(['ok','no','wait','flag'].map(k => [k, s => s]));
const esc = s => String(s ?? '');
const icon = s => s;
const shortTime = s => s ?? '';
const money = n => String(n);
const countTo = (node, n) => { node.value = n; };
const flash = () => {}, resetDecide = () => {}, reveal = () => {}, markQueue = () => {};
eval(input.functions.join('\n') + '\nrenderCase(input.snapshot);');
process.stdout.write(JSON.stringify(nodes));
"""
    rendered = subprocess.run(  # noqa: S603 (only local source and generated fixtures reach Node)
        [node, "-e", harness], input=json.dumps({"functions": functions, "snapshot": snapshot}),
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert rendered.returncode == 0, rendered.stderr
    nodes = json.loads(rendered.stdout)
    assert nodes["docCount"]["textContent"] == f"{signed}/10"
    assert nodes["kpiTodo"]["value"] == 10 - signed
    if 0 < issued < 10:
        label = "模擬簽署" if simulated else "已簽署"
        assert f"{signed}/10 {label}" in nodes["board"]["innerHTML"]
    if signed:
        for panel in ("docTable", "modalDocs"):
            label = "模擬簽署完成" if simulated else "已簽署"
            assert nodes[panel]["innerHTML"].count(label) == issued
        if simulated:
            assert "模擬簽署" in nodes["board"]["innerHTML"]
    elif issued:
        label = "模擬簽署未齊" if simulated else "待模擬簽署"
        assert nodes["docTable"]["innerHTML"].count(label) == issued
        assert nodes["modalDocs"]["innerHTML"].count("正確示範文件") == issued
        assert nodes["modalDocs"]["innerHTML"].count("錯誤示範文件") == issued
        assert nodes["modalDocs"]["innerHTML"].count('data-sample="matching"') == issued
        assert nodes["modalDocs"]["innerHTML"].count('data-sample="mismatched"') == issued
        assert nodes["modalDocs"]["innerHTML"].count('aria-describedby="documentDemoNotice"') == issued * 2
        assert "簽署完成" not in nodes["modalDocs"]["innerHTML"]
    for name in snapshot["document_status"]["unissued"]:
        assert name in nodes["docTable"]["innerHTML"]
        assert name in nodes["modalDocs"]["innerHTML"]
    assert 'data-doc="undefined"' not in nodes["modalDocs"]["innerHTML"]
    assert 'data-doc="null"' not in nodes["modalDocs"]["innerHTML"]


def test_document_modal_explains_fixed_samples_and_future_local_verification():
    modal = PAGE[PAGE.index('<dialog id="docModal"'):PAGE.index("<script>", PAGE.index('<dialog id="docModal"'))]
    assert "示範流程" in modal
    assert "固定示範樣本" in modal
    assert "示範規則" in modal
    assert "不會上傳文件內容" in modal
    assert "不是真實簽署" in modal
    assert 'aria-describedby="documentDemoNotice"' in modal
    assert '<details class="document-demo-help">' in modal
    assert '<summary aria-label="正式場景驗證說明">' in modal
    assert 'aria-hidden="true">！</span>' in modal
    assert "正式場景規劃以地端模型檢查文件資料正確性" in modal
    assert "尚未執行地端模型驗證" in modal
    assert "資料檢查不等同身分或簽署效力認證" in modal
    assert "補齊文件、記錄模擬核對並送交人工審核" in modal
    assert "核准或退件仍由專人決定" in modal
    assert "不會直接送審" not in modal


@pytest.mark.parametrize("sample", ["matching", "mismatched"])
def test_document_sample_click_sends_choice_without_file_payload(sample):
    node = shutil.which("node")
    assert node is not None, "UI renderer tests require Node.js on PATH"
    start = PAGE.index('$("modalDocs").addEventListener("click",')
    end = PAGE.index("\n});", start) + len("\n});")
    harness = r"""
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let clicked;
const $ = id => ({addEventListener: (event, callback) => { clicked = callback; }});
const sent = [];
const customerWs = {readyState:1, send: value => sent.push(JSON.parse(value))};
const documentDemoAvailable = true, documentDemoBusy = false;
const waiting = () => {};
eval(input.listener);
const button = {dataset: {doc:'17', sample:input.sample}, classList: {contains: () => false}};
clicked({target:{closest: () => button}});
process.stdout.write(JSON.stringify(sent));
"""
    clicked = subprocess.run(  # noqa: S603 (only local source and fixed sample names reach Node)
        [node, "-e", harness], input=json.dumps({"listener": PAGE[start:end], "sample": sample}),
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert clicked.returncode == 0, clicked.stderr
    assert json.loads(clicked.stdout) == [{"type": "upload", "document_id": 17, "sample": sample}]


def test_document_demo_buttons_follow_test_order_with_complete_primary():
    class Buttons(HTMLParser):
        def __init__(self):
            super().__init__()
            self.buttons = []

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag == "button" and "data-document-demo" in attributes:
                self.buttons.append(attributes)

    parser = Buttons()
    parser.feed(PAGE)
    assert [button["data-document-demo"] for button in parser.buttons] == ["missing", "wrong", "complete"]
    assert [button.get("class", "") for button in parser.buttons] == ["", "", "primary"]
    for button in parser.buttons:
        assert "disabled" in button
        assert button["aria-describedby"] == "documentDemoNotice documentDemoStatus"
    for label in ("一鍵缺漏測試", "一鍵錯誤", "一鍵完整"):
        assert label in PAGE


@pytest.mark.parametrize("mode", ["missing", "wrong", "complete"])
@pytest.mark.parametrize(
    ("stage", "completed", "unissued", "expected_modes"),
    [
        ("issued", False, [], {"missing", "wrong", "complete"}),
        ("proposed", False, [], set()),
        ("verified", False, [], set()),
        ("issued", True, [], set()),
        ("signed", True, [], {"complete"}),
        ("verified", True, [], {"complete"}),
        ("signed", False, [], set()),
        ("signed", True, ["尚未交付"], set()),
        ("verified", True, ["尚未交付"], set()),
        ("review", True, [], set()),
        ("approved", True, [], set()),
        ("rejected", True, [], set()),
    ],
)
def test_document_demo_click_uses_current_stage_and_only_sends_mode(mode, stage, completed, unissued, expected_modes):
    node = shutil.which("node")
    assert node is not None, "UI renderer tests require Node.js on PATH"
    functions = []
    for name in ("renderModalDocs", "updateDocumentDemoControls", "waiting", "wsUrl", "connectCustomer"):
        start = PAGE.index(f"function {name}(")
        end = PAGE.index("\n}\n", start) + len("\n}\n")
        functions.append(PAGE[start:end])
    start = PAGE.index('$("documentDemoActions").addEventListener("click",')
    end = PAGE.index("\n});", start) + len("\n});")
    harness = r"""
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const nodes = {};
const uploads = [{disabled:false}];
const $ = id => nodes[id] ??= {
  querySelectorAll: () => uploads,
  setAttribute(key, value) {this[key] = value;},
  addEventListener(event, callback) {this.click = callback;},
  append() {},
  close() {this.closed = true;},
};
const document = {createElement: () => ({remove() {}})};
let pending = null, documentDemoAvailable = false, documentDemoRetry = false, documentDemoBusy = false, customerWs = null;
const sent = [];
const location = {host:'localhost'};
class WebSocket {
  readyState = 1;
  send(value) {sent.push(JSON.parse(value));}
}
const esc = value => String(value ?? '');
const icon = () => '';
const PILLS = {ok: text => text, wait: text => text};
const toBottom = () => {}, bubble = () => {}, renderChips = () => {}, renderCitations = () => {};
eval(input.functions.join('\n') + '\n' + input.listener + `
connectCustomer('demo');
renderModalDocs([{document_id: 17, kind:'文件', signed_at:input.completed ? 'date' : null}], input.unissued, input.stage);
const before = Object.fromEntries(['missing','wrong','complete'].map(mode => [mode, $('documentDemo' + mode).disabled]));
const button = $('documentDemo' + input.mode);
button.dataset = {documentDemo:input.mode};
$('documentDemoActions').click({target:{closest: () => button}});
$('documentDemoActions').click({target:{closest: () => button}});
customerWs.onmessage({data:JSON.stringify({type:'notice',text:'state result',pending_reply:true})});
const busy = {disabled:button.disabled, status:$('documentDemoStatus').textContent, uploadsDisabled:uploads[0].disabled};
customerWs.onmessage({data:JSON.stringify({type:'reply',text:'state guidance'})});
process.stdout.write(JSON.stringify({sent, before, busy, after:button.disabled, closed:!!$('docModal').closed}));
`);
"""
    clicked = subprocess.run(  # noqa: S603 (only local source and fixed demo choices reach Node)
        [node, "-e", harness],
        input=json.dumps({"functions": functions, "listener": PAGE[start:end], "mode": mode, "stage": stage, "completed": completed, "unissued": unissued}),
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert clicked.returncode == 0, clicked.stderr
    result = json.loads(clicked.stdout)
    allowed = mode in expected_modes
    assert result["before"] == {choice: choice not in expected_modes for choice in ("missing", "wrong", "complete")}
    assert result["sent"] == ([{"type": "document_demo", "mode": mode}] if allowed else [])
    assert result["busy"]["disabled"] is True
    assert result["busy"]["uploadsDisabled"] is True
    assert result["after"] is not allowed
    assert result["closed"] is allowed
    if allowed:
        assert result["busy"]["status"] == "示範處理中，請等候櫃台說明結果。"


def test_a_reply_with_another_one_coming_keeps_the_customer_waiting():
    """
    The pane reopens when the turn ends, not when the first reply lands.

    An identity check answers the question it interrupted and then speaks again about
    documents. Both arrived as plain replies and the pane released on the first, so a
    question typed into that gap was answered before the guidance already on its way —
    the guidance then appeared under the new question and read as a non-sequitur.
    Observed on case 7033: identity-correct, then claim-documents received
    document_progress and the two turns after it each carried the previous question's
    answer.

    `pending_reply` already existed on notices for this reason. This pins that replies
    carry it too, and that the pane reads it.
    """
    from pathlib import Path

    page = Path("src/policydesk/web/static/index.html").read_text()
    reply_branch = page[page.index('case "reply":'):page.index('case "notice":')]
    assert "if (!m.pending_reply) waiting(false);" in reply_branch, (
        "a reply that promises another one must not release the pane"
    )

    server = Path("src/policydesk/web/server.py").read_text()
    assert '"pending_reply": pending_reply,' in server, "the reply payload carries the flag"

    # The stage decides whether a second reply follows, so it must be read before the
    # first one is sent — after it, the pane has already reopened.
    handler = server[server.index("# Answer what they actually asked"):server.index('case "say" if case_id is not None:')]
    assert handler.index("documents_follow = stage in") < handler.index("turn = await _answer("), (
        "the stage read must precede the first answer"
    )
    assert "pending_reply=documents_follow," in handler


@pytest.mark.parametrize(("protocol", "scheme"), [("https:", "wss"), ("http:", "ws")])
def test_both_sockets_follow_the_page_scheme(protocol, scheme):
    """
    A `ws://` literal is a mixed-content socket on an https page, and the browser
    refuses it silently. Behind the demo tunnel both panes stayed at 尚未連線 and the
    composer kept 請先完成建檔, while http://localhost:8100 was fine — so the defect
    lived only where the QR code sends people.

    Run, not read. Asserting the source text says the literal is gone and nothing about
    what the page builds at either address.
    """
    node = shutil.which("node")
    assert node is not None, "UI renderer tests require Node.js on PATH"

    functions = []
    for name in ("wsUrl", "connectCustomer", "connectDesk"):
        start = PAGE.index(f"function {name}(")
        end = PAGE.index("\n}\n", start) + len("\n}\n")
        functions.append(PAGE[start:end])

    harness = r"""
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const opened = [];
const location = {protocol: input.protocol, host: 'desk.example'};
const DESK_TOKEN = 'tok en';
let customerWs = null, deskWs = null, memberId = 7;
class WebSocket {
  constructor(url) {opened.push(url);}
  set onopen(_) {}
  set onmessage(_) {}
  set onclose(_) {}
  send() {}
}
const setLink = () => {}, waiting = () => {}, bubble = () => {};
eval(input.functions.join('\n') + `
connectCustomer('demo');
connectDesk();
`);
process.stdout.write(JSON.stringify(opened));
"""
    payload = json.dumps({"functions": functions, "protocol": protocol})
    done = subprocess.run(  # noqa: S603 (only local source and the two fixed protocols reach Node)
        [node, "-e", harness], input=payload, capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, done.stderr
    customer, desk = json.loads(done.stdout)

    assert customer == f"{scheme}://desk.example/ws/customer"
    assert desk.startswith(f"{scheme}://desk.example/ws/desk?token=tok%20en&member=7")
    assert "ws://${location.host}" not in PAGE, "no socket rebuilds the URL by hand"
