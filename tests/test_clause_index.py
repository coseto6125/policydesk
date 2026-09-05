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
    """Approval references are not positive evidence of a product name."""
    from policydesk.clauses.index import _title_of

    title = "國泰人壽新實全心意PLUS住院醫療健康保險附約"
    assert _title_of(f"{line}\n{title}") == title


@pytest.mark.parametrize("furniture", [
    "本商品說明書僅供參考，詳細內容請以保險單條款為準",
    "碳標字第R2316510002號", "健康促進保費折減", "保險單年度末年齡",
])
def test_title_of_furniture_before_printed_product_uses_positive_evidence(furniture):
    from policydesk.clauses.index import _title_of

    assert _title_of(f"{furniture}\n• 國泰人壽鑫月享加鑫外幣變額壽險") == "國泰人壽鑫月享加鑫外幣變額壽險"


def test_title_of_wrapped_product_preserves_fullwidth_qualifiers():
    from policydesk.clauses.index import _title_of

    assert _title_of("國 泰 人 壽\n新守護久久長期照顧\n終身健康保險\n（實物給付型保險商品）") == (
        "國泰人壽新守護久久長期照顧終身健康保險（實物給付型保險商品）"
    )


def test_title_of_two_main_products_is_unresolved():
    from policydesk.clauses.index import _title_of

    assert _title_of("國泰人壽安家一年期保險費豁免附約\n國泰人壽安護一年期保險費豁免附約") == ""


def test_title_of_second_product_without_repeated_insurer_is_unresolved():
    from policydesk.clauses.index import _title_of

    assert _title_of("國泰人壽安家一年期保險費豁免附約\n安護一年期保險費豁免附約") == ""


@pytest.mark.parametrize("definition", ["(以下簡稱本商品)", "（以下簡稱本商品）"])
def test_title_of_disclosure_names_its_own_product_not_the_document_heading(definition):
    from policydesk.clauses.index import _title_of

    title = "國泰人壽泰享富貴投資鏈結型保險"
    page = "國泰人壽保險股份有限公司-基金通路報酬揭露聲明書\n"
    assert _title_of(page + f"本公司『{title}』{definition}，提供連結之基金。") == title


def test_title_of_wrapped_self_definition_preserves_product_qualifier():
    from policydesk.clauses.index import _title_of

    assert _title_of("本公司『國泰人壽安心終身\n保險（甲型）』(以下簡稱本商品)，提供服務。") == (
        "國泰人壽安心終身保險（甲型）"
    )


@pytest.mark.parametrize("page", [
    "本公司『國泰人壽安心終身保險』(以下簡稱其他商品)，僅供比較。",
    "投保範例：本公司『國泰人壽安心終身保險』(以下簡稱本商品)。",
    "本公司『合作廠商資料及查閱方式』(以下簡稱本商品)。",
    "本公司『國泰人壽甲種終身保險』(以下簡稱本商品)。\n"
    "本公司『國泰人壽乙種終身保險』(以下簡稱本商品)。",
])
def test_title_of_reference_or_conflicting_self_definitions_is_unresolved(page):
    from policydesk.clauses.index import _title_of

    assert _title_of(page) == ""


def test_build_index_unresolved_title_is_explicit_not_a_filename(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from policydesk.clauses import index as parser

    monkeypatch.setattr(parser, "PdfReader", lambda _: SimpleNamespace(
        pages=[SimpleNamespace(extract_text=lambda: "合作廠商資料及查閱方式")],
    ))
    pdf = tmp_path / "wrong-product-name.pdf"
    pdf.write_bytes(b"fixture")
    assert build_index(pdf).title == "商品名稱待核對"


def test_title_of_wrapped_benefit_enumeration_is_not_a_second_product():
    from policydesk.clauses.index import _title_of

    title = "國泰人壽真心康愛防癌終身健康保險附約"
    assert _title_of(title + "\n裝設保險金、癌症門診醫療保險金、癌症放射線治療保險\n金") == title


def test_title_of_repeated_main_and_endorsement_uses_main():
    from policydesk.clauses.index import _title_of

    assert _title_of("國泰人壽真簡單愛變額萬能壽險\n國泰人壽真簡單愛變額萬能壽險\n"
                     "國泰人壽意外生活照護保險金傷害失能保險附加條款") == "國泰人壽真簡單愛變額萬能壽險"


def test_title_of_structured_product_retains_printed_index_and_currency():
    from policydesk.clauses.index import _title_of

    title = "澳幣計價股票指數連結結構型商品(無擔保) 【鏈結指數為韓國KOSPI 200指數(KOSPI 200 Index)】"
    assert _title_of("商品說明書\n" + title) == title


def test_title_of_inline_benefits_does_not_promote_related_endorsement():
    from policydesk.clauses.index import _title_of

    assert _title_of("■ 國泰人壽超月月澳利外幣變額壽險（給付項目：祝壽保險金、身故保險金）\n"
                     "國泰人壽委託投資帳戶投資標的批註條款(二)") == "國泰人壽超月月澳利外幣變額壽險"


def test_title_of_benefit_list_without_label_is_not_a_name_qualifier():
    from policydesk.clauses.index import _title_of

    title = "國泰人壽全心住院日額健康保險附約"
    assert _title_of(title + "\n（住院日額醫療、出院療養、手術醫療、加護病房保險金）") == title


def test_title_of_slogan_is_not_joined_to_next_line_as_product():
    from policydesk.clauses.index import _title_of

    assert _title_of("國泰人壽大特點去哪都安心\n新旅行平安保險") == ""


@pytest.mark.parametrize(("prefix", "expected"), [
    ("034bbaa688b2", "國泰人壽鑫月享加鑫外幣變額壽險"),
    ("11762fabf871", "國泰人壽真安宜保險費豁免附約"),
    ("8b9740dc6bc1", ""),
    ("c0cb926dd8a3", ""),
    ("b91c3821e306", "國泰人壽自由配一年定期初次罹患癌症健康保險附約（外溢型）"),
    ("16152d032eb1", "國泰人壽鑫飛揚變額年金保險"),
    ("3108edad1fc2", "國泰人壽泰享富貴投資鏈結型保險"),
    ("3a75b8a48821", ""),
])
def test_title_of_local_pdf_matches_printed_source(prefix, expected):
    from pypdf import PdfReader

    from policydesk.clauses.index import _title_of

    paths = list((FIXTURE.parent.parent / "cathay").glob(f"{prefix}*.pdf"))
    if not paths:
        pytest.skip("local Cathay PDF corpus is unavailable")
    assert len(paths) == 1
    assert _title_of(PdfReader(paths[0]).pages[0].extract_text()) == expected


@pytest.mark.parametrize("page", [
    "合作廠商資料及查閱方式\n合作廠商網站", "健康促進保費折減\n保障每一天",
    "國泰人壽保險股份有限公司-基金通路報酬揭露聲明書",
    "國泰人壽商品\n免費提供保險", "國泰人壽安心保險。", "國泰人壽安心\n\n健康保險",
])
def test_title_of_no_complete_printed_title_remains_unresolved(page):
    from policydesk.clauses.index import _title_of

    assert _title_of(page) == ""


@pytest.mark.parametrize(
    ("line", "readable"),
    [
        ("國泰人壽新實全心意PLUS住院醫療健康保險附約（外溢型）", True),
        ("真月月康利變額壽險", False),
        ("保障內容(請詳閱條款)", False),
        # A real product whose own prospectus prints this much Latin and this many
        # digits. The first version of the rule asked for half the characters to be CJK
        # and rejected it — a ratio punishes the honest name for the company it keeps.
        ("澳幣計價股票指數連結結構型商品(無擔保) 【鏈結指數為韓國KOSPI 200指數(KOSPI 200 Index)】", True),
        ("ॆᔊఊฌᜊᕘຬঐྪᎈ", False),
        ("Cathay Life Insurance Co Ltd", False),
    ],
)
def test_a_title_is_text_a_person_could_read(line: str, readable: bool):
    """Readable text alone is insufficient, but Latin in a real title is retained."""
    from policydesk.clauses.index import _title_of

    assert bool(_title_of(line)) is readable


@needs_pdf
def test_no_contract_is_named_after_a_broken_font():
    index = build_index(FIXTURE)
    assert index.title == "國泰人壽全心住院日額健康保險附約"


@needs_pdf
def test_build_index_title_selection_does_not_change_clause_text_or_pages(monkeypatch):
    from policydesk.clauses import index as parser

    before = build_index(FIXTURE)
    monkeypatch.setattr(parser, "_title_of", lambda _: "核對用名稱")
    after = build_index(FIXTURE)
    assert after.title == "核對用名稱"
    assert before.title != after.title
    assert after.clauses == before.clauses
    assert after.doc_id == before.doc_id
    assert after.document_kind == before.document_kind
