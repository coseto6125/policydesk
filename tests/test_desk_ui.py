"""
Regressions for the back office markup and stylesheet.

The page is one file with no build step, so these read it as text. Each one guards a
defect that was visible on screen and invisible in the code.
"""

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

    assert "稱呼一律用「您」" in WRITING
    assert "這位保戶" in WRITING, "the wrong form must be named, not only the right one"
    assert "不是給他看的" in WRITING, "and the reason — the labels are for the model"
