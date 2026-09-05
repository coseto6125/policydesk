"""
The two traps in section-one of the index module are the reason this file exists.

Both were found in a real contract, so both are asserted against that contract rather
than against a hand-written sample that would only prove the regex matches itself.
"""

import re
from pathlib import Path

import pytest
from msgspec import ValidationError  # noqa: F401  (kept: msgspec import proves env wiring)

from policydesk.clauses.index import build_index, cn_to_int
from policydesk.core.models import Citation, ClauseKind, Money, Stage, may_advance

FIXTURE = Path(__file__).parent.parent / "data" / "clauses" / "cathay-inpatient-daily.pdf"
_GAP = re.compile(r"(?<=[\u3000-\u303f\u3400-\u9fff\uff00-\uffef])[ \t\u3000]+(?=[\u3000-\u303f\u3400-\u9fff\uff00-\uffef])")

needs_pdf = pytest.mark.skipif(not FIXTURE.exists(), reason="run scripts/fetch_fixtures.sh first")


def test_tidy_compatibility_ideographs_preserves_fullwidth_punctuation():
    from policydesk.clauses.index import _tidy

    assert _tidy("受益人（保險年齡）：執行，申領；Ａ１。") == "受益人（保險年齡）：執行，申領；Ａ１。"


def test_tidy_normalizes_ideographs_before_closing_cjk_gaps():
    from policydesk.clauses.index import _tidy

    assert _tidy("受 " + chr(0xFA17) + " 人（ A B ）") == "受益人（ A B ）"


def test_normalize_ideographs_only_changes_compatibility_block():
    from unicodedata import normalize

    from policydesk.clauses.index import normalize_ideographs

    block = "".join(chr(code) for code in range(0xF900, 0xFB00))
    punctuation = "，：；（）Ａ１①㍻"
    expected = "".join(normalize("NFKC", char) for char in block) + punctuation
    assert normalize_ideographs(block + punctuation) == expected
    assert normalize_ideographs(expected) == expected


@pytest.mark.parametrize("number", ["第十九條", "第 19 條"])
def test_build_index_heading_before_number_keeps_first_body_sentence(tmp_path, monkeypatch, number):
    from types import SimpleNamespace

    from policydesk.clauses import index as parser

    page = "國泰人壽測試終身保險\n除外責任\n" + number + "\n第一句正文不得遺失。但有例外。\n"
    monkeypatch.setattr(parser, "PdfReader", lambda _: SimpleNamespace(
        pages=[SimpleNamespace(extract_text=lambda: page)],
    ))
    pdf = tmp_path / "contract.pdf"
    pdf.write_bytes(b"fixture")
    clause = build_index(pdf).clauses["art.19"]
    assert clause.heading == "除外責任"
    assert clause.verbatim == "第一句正文不得遺失。但有例外。"
    assert clause.kind is ClauseKind.EXCLUSION


def test_build_index_inline_heading_keeps_cross_reference_in_body(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from policydesk.clauses import index as parser

    page = "國泰人壽測試終身保險\n第 1 條 契約的構成\n第一條所約定之事項仍適用。\n第 2 條 保險範圍\n保障正文。"
    monkeypatch.setattr(parser, "PdfReader", lambda _: SimpleNamespace(
        pages=[SimpleNamespace(extract_text=lambda: page)],
    ))
    pdf = tmp_path / "contract.pdf"
    pdf.write_bytes(b"fixture")
    indexed = build_index(pdf)
    assert indexed.clauses["art.1"].heading == "契約的構成"
    assert indexed.clauses["art.1"].verbatim == "第一條所約定之事項仍適用。"
    assert indexed.clauses["art.2"].verbatim == "保障正文。"


def test_build_index_heading_after_number_is_not_previous_section_heading(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from policydesk.clauses import index as parser

    page = "國泰人壽測試終身保險\n附約的解釋\n第 1 條\n附約的訂立及構成\n第一句正文不得遺失。"
    monkeypatch.setattr(parser, "PdfReader", lambda _: SimpleNamespace(
        pages=[SimpleNamespace(extract_text=lambda: page)],
    ))
    pdf = tmp_path / "contract.pdf"
    pdf.write_bytes(b"fixture")
    clause = build_index(pdf).clauses["art.1"]
    assert clause.heading == "附約的訂立及構成"
    assert clause.verbatim == "第一句正文不得遺失。"


@pytest.mark.skipif(not list((FIXTURE.parent.parent / "cathay").glob("346e81662019*.pdf")), reason="local corpus absent")
def test_build_index_real_summary_does_not_promote_body_to_heading():
    from policydesk.core.models import DocumentKind

    pdf = next((FIXTURE.parent.parent / "cathay").glob("346e81662019*.pdf"))
    indexed = build_index(pdf)
    clause = indexed.clauses["art.39"]
    assert clause.heading == "不分紅保單"
    assert clause.verbatim.startswith("本保險為不分紅保單，不參加紅利分配，並無紅利給付項目。")
    assert indexed.document_kind is DocumentKind.BROCHURE


@pytest.mark.parametrize(("cover", "has_first", "expected"), [
    ("國泰人壽測試保險\n商 品 說 明 書\n", True, "brochure"),
    ("商品說明書\n", False, "brochure"),
    ("重要條款摘要\n", True, "brochure"),
    ("國泰人壽測試保險\n", True, "contract"),
    ("僅有商品介紹\n", False, "unknown"),
])
def test_document_kind_printed_brochure_label_outranks_numbered_articles(cover, has_first, expected):
    from policydesk.clauses.index import document_kind

    assert document_kind(cover, has_first_article=has_first).value == expected


@pytest.mark.parametrize("kind", ["brochure", "unknown"])
def test_cite_non_contract_source_rejects_existing_article(kind):
    from policydesk.clauses.index import ClauseIndex
    from policydesk.core.models import Clause, DocumentKind

    clause = Clause(clause_id="art.1", kind=ClauseKind.GRANT, heading="保障範圍", verbatim="摘要內容", page=1)
    index = ClauseIndex(doc_id="source", title="來源", clauses={"art.1": clause}, document_kind=DocumentKind(kind))
    with pytest.raises(ValueError, match="not a contract citation"):
        index.cite("art.1")


def test_cn_to_int_units_returns_digit():
    assert cn_to_int("三") == 3


def test_cn_to_int_bare_ten_returns_ten():
    assert cn_to_int("十") == 10


def test_cn_to_int_teens_returns_ten_plus_unit():
    assert cn_to_int("十七") == 17


def test_cn_to_int_compound_tens_returns_full_value():
    assert cn_to_int("二十三") == 23


@pytest.mark.parametrize(("written", "number"), [("廿", 20), ("廿七", 27), ("卅", 30), ("卅六", 36)])
def test_cn_to_int_abbreviated_tens_returns_full_value(written, number):
    assert cn_to_int(written) == number


def test_build_index_real_abbreviated_articles_keep_document_lists_separate():
    pdf = FIXTURE.parent.parent / "cathay" / "f8e03e1a4a79bc03cf44ef1eb5b6a9c9.pdf"
    if not pdf.exists():
        pytest.skip("local corpus absent")
    indexed = build_index(pdf)
    assert {f"art.{number}" for number in range(1, 37)} <= indexed.clauses.keys()
    first = indexed.clauses["art.27"]
    second = indexed.clauses["art.28"]
    assert first.heading == "保險金的申領（一）"
    assert second.heading == "保險金的申領（二）"
    assert "病理切片檢查" in first.verbatim
    assert "癌症門診手術證明文件" in second.verbatim
    assert "保險金的申領" not in indexed.clauses["art.20"].verbatim
    assert "第廿八條" not in first.verbatim
    assert indexed.clauses["art.36"].heading == "管轄法院"


def test_money_without_citation_raises():
    with pytest.raises(ValueError, match="no citation"):
        Money(amount=8000, basis="日額 2,000 × 4 日", citations=())


def test_money_with_citation_keeps_amount():
    cite = Citation(doc_id="d", clause_id="art.11", page=5, verbatim="住院日額醫療保險金")
    assert Money(amount=8000, basis="日額 2,000 × 4 日", citations=(cite,)).amount == 8000


@needs_pdf
def test_build_index_real_contract_finds_every_article():
    index = build_index(FIXTURE)
    # The contract prints 24 articles; the parser also emits derived clauses.
    articles = [c for c in index.clauses if c.startswith("art.") and "." not in c[4:]]
    assert len(articles) >= 24


@needs_pdf
def test_build_index_classifies_exclusion_article():
    index = build_index(FIXTURE)
    assert index.clauses["art.17"].kind is ClauseKind.EXCLUSION
    assert "除外責任" in index.clauses["art.17"].heading


@needs_pdf
def test_build_index_finds_waiting_period_hidden_in_definition():
    """Trap 1: the contract never writes 等待期, so a label search finds nothing."""
    index = build_index(FIXTURE)
    raw = "".join(c.verbatim for c in index.clauses.values())
    assert "等待期" not in raw, "if the contract ever labels it, this test is testing the wrong thing"

    waiting = index.clauses["waiting"]
    assert waiting.kind is ClauseKind.WAITING
    assert "三十日" in waiting.verbatim


@needs_pdf
def test_build_index_splits_carve_back_out_of_exclusion():
    """Trap 2: art.17 excludes cosmetic surgery, then restores reconstructive surgery."""
    index = build_index(FIXTURE)
    carves = [c for c in index.clauses.values() if c.kind is ClauseKind.CARVE_BACK]
    assert carves, "an exclusion that carves back must not be indexed as one flat clause"

    reconstructive = next(c for c in carves if "重建" in c.verbatim)
    assert reconstructive.overrides == ("art.17",)


@needs_pdf
def test_cite_unknown_clause_raises_rather_than_inventing():
    index = build_index(FIXTURE)
    with pytest.raises(KeyError):
        index.cite("art.999")


def test_cn_to_int_arabic_returns_same_number():
    """113 年起修正的條款改印阿拉伯數字條號，兩套都要讀得懂。"""
    assert cn_to_int("19") == 19


def test_may_advance_one_step_forward_is_allowed():
    assert may_advance(Stage.INQUIRY, Stage.PROPOSED)


def test_may_advance_skipping_a_stage_is_refused():
    """驗證身分前不得送審，否則簽署不具本人親簽的推定效力。"""
    assert not may_advance(Stage.SIGNED, Stage.REVIEW)


def test_may_advance_backwards_is_refused():
    assert not may_advance(Stage.REVIEW, Stage.PROPOSED)


def test_may_advance_decision_requires_review():
    assert may_advance(Stage.REVIEW, Stage.APPROVED)
    assert not may_advance(Stage.VERIFIED, Stage.APPROVED)


def test_may_advance_from_decided_case_is_refused():
    assert not may_advance(Stage.REJECTED, Stage.INQUIRY)
    assert not may_advance(Stage.APPROVED, Stage.REVIEW)


@needs_pdf
def test_a_stored_clause_carries_no_pdf_spacing():
    """
    The text layer's spacing reached a customer verbatim.

    「三、 醫療診斷書及X光片 。 申請意外脫臼手術保險金者 ， 醫療診斷書須列明手術名稱 、
    部位及方式」 is what a replay of the real transcript put in front of someone asking
    what to bring to a claim. The gap comes from glyph positions in a justified line, and
    it sat in 10,726 of the corpus's 11,741 clauses.

    Asserted on the fixture contract rather than on a crafted string: a hand-written
    sample would only prove the regex matches itself, and the class this widened —
    CJK punctuation and fullwidth forms — is exactly what a crafted sample would omit.
    """
    index = build_index(FIXTURE)
    stray = [
        (cid, line)
        for cid, clause in index.clauses.items()
        for line in clause.verbatim.splitlines()
        if _GAP.search(line) or line != line.rstrip()
    ]
    assert not stray, f"{len(stray)} lines still carry the text layer's spacing: {stray[:3]}"


@needs_pdf
def test_a_number_keeps_the_spaces_around_it():
    """
    第 31 日 and PLUS 住院醫療 are printed with those spaces, not spaced by justification.

    The rule closes a gap only when both sides are CJK, so a Latin or digit neighbour
    holds its space. Without this the same pass would run 「持續有效第31日」 together and
    change how the waiting period reads.
    """
    index = build_index(FIXTURE)
    body = "\n".join(c.verbatim for c in index.clauses.values())
    assert re.search(r"[0-9A-Za-z] [一-鿿]|[一-鿿] [0-9A-Za-z]", body), (
        "every space beside a number or a Latin run was removed, which is not the rule"
    )


@pytest.mark.parametrize(
    "line",
    ["認證編號：0610132-31", "認証番号：  8811432-1 （計4ページ中1ページ目、 2025年9月版）",
     "核准文號：金管保壽字第10902號", "第 1 頁， 共 17 頁"],
)
def test_an_approval_line_is_not_a_product_name(line: str):
    """
    22 of 660 contracts were named after their approval number.

    `_title_of` takes the first line of page one that is long enough and is not
    furniture, and the filter knew 核准文號 but not 認證編號 — the same line, printed
    under a different label by a different insurer's template. 認證編號：0610132-31 is
    what a recommendation would have offered a customer as a product.
    """
    from policydesk.clauses.index import _FURNITURE, _title_of

    assert _FURNITURE.search(line) or len(line) < 8
    assert _title_of(f"{line}\n國泰人壽新實全心意PLUS住院醫療健康保險附約") != line


@pytest.mark.parametrize(
    ("line", "readable"),
    [
        ("國泰人壽新實全心意PLUS住院醫療健康保險附約（外溢型）", True),
        ("真月月康利變額壽險", True),
        ("保障內容(請詳閱條款)", True),
        # A real product whose own prospectus prints this much Latin and this many
        # digits. The first version of the rule asked for half the characters to be CJK
        # and rejected it — a ratio punishes the honest name for the company it keeps.
        ("澳幣計價股票指數連結結構型商品(無擔保) 【鏈結指數為韓國KOSPI 200指數(KOSPI 200 Index)】", True),
        ("ॆᔊఊฌᜊᕘຬঐྪᎈ", False),
        ("Cathay Life Insurance Co Ltd", False),
    ],
)
def test_a_title_is_text_a_person_could_read(line: str, readable: bool):
    r"""
    A subset-embedded CID font decodes to characters that are `\\w`.

    `_FURNITURE`'s `[\\W\\d_]+` arm rejects a line of punctuation and digits and admits
    ॆᔊఊฌᜊᕘຬঐྪᎈ, which became a product name the moment 保障您所愛 above it fell
    below the length floor — a floor the `.strip()` fix lowered it past. Measured across
    660 contracts, six titles move under that fix: five better, this one worse.

    The bar is a count and not a ratio: the mojibake holds zero ideographs (those are
    Devanagari, Telugu, Thai and Tibetan codepoints), while a structured product's real
    name carries as much Latin as it needs to.
    """
    from policydesk.clauses.index import _reads_as_chinese

    assert _reads_as_chinese(line) is readable


@needs_pdf
def test_no_contract_is_named_after_a_broken_font():
    # The whole corpus, because the mechanism is a short Chinese line above a mojibake
    # one and nothing says which document has that shape.
    from policydesk.clauses.index import _reads_as_chinese

    index = build_index(FIXTURE)
    assert _reads_as_chinese(index.title), f"the fixture is named {index.title!r}"
