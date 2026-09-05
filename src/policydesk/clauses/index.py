"""
Turn a policy PDF into an enumerated clause index.

Deterministic on purpose. The index is the only thing allowed to produce a `Citation`,
so no model ever authors contract text — it may select a `clause_id` that exists here
and nothing else. That is what makes a hallucinated citation structurally impossible
rather than merely unlikely.

Two traps this parser exists to catch, both found in the first real contract we indexed
(國泰人壽全心住院日額健康保險附約, 16pp, 38,657 chars, 25 articles):

1. The waiting period is never called 等待期. Searching the full text for that phrase
   returns zero hits. The 30-day wait is written into the cover-page definition of
   疾病 — "自本附約生效日起持續有效三十日以後". A retriever looking for the label passes
   a claim that the contract refuses.
2. Exclusions carve back inside a single sentence. Article 17 excludes 美容手術、外科整型
   and then restores 為重建其基本功能所作之必要整型. Matching either half alone gives the
   wrong answer in opposite directions.
"""

import bisect
import re
from hashlib import blake2b
from typing import TYPE_CHECKING
from unicodedata import normalize

from msgspec import Struct
from pypdf import PdfReader

from policydesk.core.models import Citation, Clause, ClauseKind, DocumentKind

if TYPE_CHECKING:
    from pathlib import Path

_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# "但為重建其基本功能所作之必要整型，不在此限" — the restoring half of an exclusion.
_CARVE_BACK = re.compile(r"但[^。；]{4,80}?(?:不在此限|除外|不適用)")

# "自本附約生效日起持續有效三十日以後" — a waiting period wearing a definition's clothes.
_WAITING = re.compile(r"(?:生效日|復效日)起持續有效([一二三四五六七八九十百]+)日")

# Justified CJK lines come out of extraction with a space between every glyph:
# "國 泰 人 壽新憶樂活認 知 功 能 障 礙終身健 康 保 險". Token matching then fails on every
# term, which is how 80 real contracts ended up classified as "other". Latin runs keep
# their spaces, so "真大心 PLUS 住院醫療" and "第 31 日" survive intact.
#
# The class covers CJK punctuation and fullwidth forms as well as the ideographs, because
# the gap lands on either side of a comma just as readily: 醫療診斷書須列明手術名稱 、 部位
# 及方式 reached a customer verbatim, and a class of ideographs alone leaves every 、。（）
# stranded between spaces.
_CJK_GAP = re.compile(r"(?<=[　-〿㐀-鿿＀-￯])[ \t　]+(?=[　-〿㐀-鿿＀-￯])")

_IDEOGRAPH_MAP = {
    code: normalized for code in range(0xF900, 0xFB00)
    if (normalized := normalize("NFKC", chr(code))) != chr(code)
}


def normalize_ideographs(text: str) -> str:
    """Normalize compatibility ideographs while preserving fullwidth punctuation."""
    return text.translate(_IDEOGRAPH_MAP)


def _tidy(text: str) -> str:
    r"""
    Close the gaps a PDF text layer leaves inside a line, and trim what it leaves at the end.

    Args:
        text: Extracted text, one clause body or one line.

    Returns:
        The same text with intra-CJK spacing closed and trailing spaces removed.

    Applied to what gets stored, never to the page the offsets are measured in. Two
    things break when a page is normalised before it is scanned: `page_ends` then
    addresses text that no longer exists at those offsets, and the article scanner
    loses the whitespace separating a clause number from its heading.

    The cost is the one row of a benefit table that still had its columns: 項目 給付條件
    給付金額 becomes 項目給付條件給付金額. That table is already unreadable in the text
    layer — its body cells each sit on their own line — and the table this desk quotes
    from is the pdfplumber extraction in `benefit`, not this text. Measured on the live
    corpus: 10,726 of 11,741 clauses change, 612,593 characters of stray spacing removed.
    """
    return "\n".join(_CJK_GAP.sub("", line).rstrip() for line in normalize_ideographs(text).splitlines())

_HEADING_KIND = (
    ("名詞定義", ClauseKind.DEFINITION),
    ("除外責任", ClauseKind.EXCLUSION),
    ("批註", ClauseKind.ENDORSEMENT),
    ("保險範圍", ClauseKind.GRANT),
    ("保險金", ClauseKind.GRANT),
    ("優惠", ClauseKind.LIMIT),
    ("申領", ClauseKind.PROCEDURE),
    ("通知", ClauseKind.PROCEDURE),
)


def cn_to_int(text: str) -> int:
    """
    Read an article number written either way the contracts write them.

    Args:
        text: A numeral such as "十七", "二十三", or "19".

    Returns:
        Its integer value.

    """
    if text.isdigit():
        return int(text)
    text = text.replace("廿", "二十").replace("卅", "三十")
    if (tens := text.find("十")) == -1:
        return sum(_CN_DIGITS.get(c, 0) for c in text)
    head = text[:tens]
    tail = text[tens + 1 :]
    return (_CN_DIGITS.get(head, 1) if head else 1) * 10 + (_CN_DIGITS.get(tail, 0) if tail else 0)


_TITLE_PREFIX = "國泰人壽"
_TITLE_ENDINGS = ("保險", "壽險", "附約", "附加條款", "批註條款")
_TITLE_MAX_LINES = 4
_TITLE_MAX_CHARACTERS = 120
_TITLE_BRACKETS = {"（": "）", "(": ")", "【": "】"}
_TITLE_WRAP_TERMS = ("保險", "壽險", "健康", "醫療", "住院", "年金", "變額", "利率", "終身", "定期",
                    "附加", "批註", "外幣", "美元", "澳幣", "重大", "長期", "豁免")
_TITLE_FAMILY_PREFIXES = ("投資型", "利率變動型", "指數連結型", "一年", "人身", "股票")


def _title_base(text: str) -> str | None:
    """Separate complete title qualifiers without folding their printed punctuation."""
    base_end = len(text)
    closing: list[str] = []
    for position, char in enumerate(text):
        if char in _TITLE_BRACKETS:
            if not closing and base_end == len(text):
                base_end = position
            closing.append(_TITLE_BRACKETS[char])
        elif char in _TITLE_BRACKETS.values():
            if not closing or closing.pop() != char:
                return None
        elif not closing and position >= base_end and not char.isspace():
            return None
    return text[:base_end].rstrip() if not closing else None


def _title_of(first_page: str) -> str:
    """
    Select a printed product title, never the first readable line or PDF metadata.

    Require the insurer prefix and a complete insurance-title ending. Adjacent wrapped
    lines and parenthesized qualifiers retain their printed punctuation. A page with
    multiple main products is unresolved, even if one occurs first. Endorsement titles
    do not displace a unique main title. No product evidence returns an empty string;
    callers must retain that uncertainty rather than invent a product for a vendor list.

    This function changes title metadata only. It never changes clause text or offsets.
    """
    first_article = next((start for start, _, number, _ in _article_marks(first_page) if number == 1), None)
    cover = first_page[:first_article] if first_article is not None else first_page
    lines = [_tidy(raw).strip().lstrip("•●■ ") for raw in cover.splitlines()]
    candidates: dict[str, str] = {}
    for start, line in enumerate(lines):
        base = _title_base(line)
        if base and base.endswith("結構型商品") and "計價" in base:
            candidates[line] = base
            continue
        if not line.startswith(_TITLE_PREFIX):
            continue
        if line != _TITLE_PREFIX and not any(term in line.removeprefix(_TITLE_PREFIX) for term in _TITLE_WRAP_TERMS):
            continue
        candidate = ""
        for offset, part in enumerate(lines[start : start + _TITLE_MAX_LINES]):
            if not part or (offset and part.startswith(_TITLE_PREFIX)):
                break
            if part.endswith(("公司", "說明書", "商品")):
                break
            candidate += part
            # Inline benefits are a separate paragraph, not part of a title qualifier.
            for opening in _TITLE_BRACKETS:
                before, separator, after = candidate.partition(opening)
                if separator and before.rstrip().endswith(_TITLE_ENDINGS) and (":" in after or "：" in after):
                    candidate = before.rstrip()
                    break
            if len(candidate) > _TITLE_MAX_CHARACTERS or any(char in candidate for char in "，。；：,:;!?！？�"):
                break
            base = _title_base(candidate)
            if base is None or not base.endswith(_TITLE_ENDINGS):
                continue
            if len(base.removeprefix(_TITLE_PREFIX)) < 4:
                break
            # A following qualifier belongs to this name; a benefits paragraph does not.
            following = start + offset + 1
            if following < len(lines) and lines[following].startswith(tuple(_TITLE_BRACKETS)):
                extended = candidate + lines[following]
                qualifier = lines[following].rstrip("）)】")
                if (qualifier.endswith(("型", "保險商品", "無身故給付", "無擔保"))
                        and not any(char in extended for char in "，。；：,:;!?！？�")
                        and _title_base(extended) == base):
                    candidate = extended
            candidates[candidate] = base
            break
    main = {title: base for title, base in candidates.items() if not base.endswith(("附加條款", "批註條款"))}
    # A second product need not repeat the insurer. Generic family labels and wrapped
    # fragments already contained in the selected full name are not other products.
    if main:
        compact_titles = tuple("".join(title.split()) for title in main)
        for line in lines:
            base = _title_base(line)
            if (not base or base.startswith((_TITLE_PREFIX, *_TITLE_FAMILY_PREFIXES))
                    or not base.endswith(("保險", "壽險", "附約"))
                    or any(char in line for char in "、，。；：,:;!?！？�")):
                continue
            positions = [position for term in _TITLE_WRAP_TERMS if (position := base.find(term)) >= 0]
            if positions and min(positions) >= 2 and not any("".join(line.split()) in title for title in compact_titles):
                return ""
    selected = main or candidates
    # Repeated headers can differ only in extraction spacing. Keep printed spacing.
    selected = {"".join(title.split()): title for title in selected}
    return next(iter(selected.values())) if len(selected) == 1 else ""


def _kind_for(heading: str) -> ClauseKind:
    """Classify an article by its printed heading, which contracts keep conventional."""
    for token, kind in _HEADING_KIND:
        if token in heading:
            return kind
    return ClauseKind.PROCEDURE


class ClauseIndex(Struct):
    """Every clause of one contract, addressable by id."""

    doc_id: str
    title: str
    clauses: dict[str, Clause]
    document_kind: DocumentKind = DocumentKind.UNKNOWN

    def cite(self, clause_id: str) -> Citation:
        """
        Build a citation for a clause that exists.

        Args:
            clause_id: An id from this index.

        Returns:
            A citation carrying the clause's verbatim text.

        Raises:
            KeyError: The id is not in this contract. This is the guard that turns a
                fabricated citation into a crash instead of a plausible-looking figure.
            ValueError: The source is not a contract document.

        """
        if self.document_kind is not DocumentKind.CONTRACT:
            raise ValueError(f"{self.document_kind.value} source is not a contract citation")
        clause = self.clauses[clause_id]
        return Citation(doc_id=self.doc_id, clause_id=clause_id, page=clause.page, verbatim=clause.verbatim)


def _page_of(offset: int, page_ends: list[int]) -> int:
    """
    Map a character offset in the joined text back to its 1-based page.

    Args:
        offset: Character position within the joined document text.
        page_ends: Cumulative end offset of each page, in order.

    Returns:
        The page number as a reader would cite it, counting from 1.

    """
    return bisect.bisect_right(page_ends, offset) + 1


def _article_marks(full: str) -> list[tuple[int, int, int, str]]:
    """Read printed article lines without consuming the following body as a heading."""
    marks = []
    offset = 0
    lines = []
    for raw in full.splitlines(keepends=True):
        if line := raw.strip():
            lines.append((offset, offset + len(raw), line))
        offset += len(raw)
    numerals = frozenset("一二三四五六七八九十百廿卅0123456789")
    for position, (start, body_at, line) in enumerate(lines):
        if line.startswith("第"):
            written, separator, rest = line[1:].partition("條")
            digits = "".join(written.split())
            if separator and digits and set(digits) <= numerals and (not rest or rest[0].isspace()):
                number = cn_to_int(digits)
                heading = rest.strip()
                if not heading:
                    # Some PDFs extract the left-margin article title after its
                    # number; summaries print the title before it. Neither format
                    # permits taking a punctuated body sentence as the heading.
                    for neighbor in (position + 1, position - 1):
                        if not 0 <= neighbor < len(lines):
                            continue
                        candidate_at, candidate_end, candidate = lines[neighbor]
                        if len(candidate) > 60 or any(c in candidate for c in "。；：，、"):
                            continue
                        heading = candidate
                        if neighbor < position:
                            start = candidate_at
                        else:
                            body_at = candidate_end
                        break
                marks.append((start, body_at, number, heading))
    return marks


def document_kind(first_page: str, *, has_first_article: bool) -> DocumentKind:
    """Use the printed source label before interpreting its numbered content."""
    lines = {"".join(line.split()) for line in first_page.splitlines()}
    if "商品說明書" in lines or "重要條款摘要" in lines:
        return DocumentKind.BROCHURE
    return DocumentKind.CONTRACT if has_first_article else DocumentKind.UNKNOWN


def build_index(pdf_path: Path) -> ClauseIndex:
    """
    Index one policy contract.

    Args:
        pdf_path: A text-layer PDF. Taiwanese insurers publish these directly, so no OCR
            is involved on the contract side — the imaging problem lives on the medical
            receipt side instead.

    Returns:
        The contract's clause index, including the two synthetic clauses this parser
        derives: a `waiting` clause lifted out of a definition, and a `carve_back` for
        each exclusion that restores cover mid-sentence.

    """
    reader = PdfReader(pdf_path)
    pages = [p.extract_text() or "" for p in reader.pages]

    page_ends: list[int] = []
    running = 0
    for page in pages:
        running += len(page) + 1
        page_ends.append(running)
    full = "\n".join(pages)

    doc_id = blake2b(pdf_path.read_bytes(), digest_size=8).hexdigest()
    title = _title_of(pages[0]) or pdf_path.stem

    # Article numbers run 1, 2, 3… once. A match that does not advance the count is a
    # line that merely opens with a cross-reference, or a heading repeated in a running
    # header — either way it is not where the next article begins.
    marks: list[tuple[int, int, int, str]] = []
    highest = 0
    for mark in _article_marks(full):
        if (number := mark[2]) <= highest:
            continue
        highest = number
        marks.append(mark)

    clauses: dict[str, Clause] = {}

    for i, (start, body_at, number, heading) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(full)
        body = full[body_at:end].strip()
        clause_id = f"art.{number}"
        # Tidied before it is classified, not after. `_kind_for` looks for 保險金 in the
        # heading, and the text layer prints 癌症住院手術醫療保險 金 — so three cancer
        # surgery benefits were filed as `procedure`, and `benefit_headings` asks for
        # `grant`. Storing the clean heading while classifying the dirty one left a row
        # that reads correctly and is still invisible to the tool that needs it.
        heading = _tidy(heading)
        kind = _kind_for(heading)
        clauses[clause_id] = Clause(
            clause_id=clause_id,
            kind=kind,
            heading=heading,
            verbatim=_tidy(body),
            page=_page_of(start, page_ends),
        )

        if kind is not ClauseKind.EXCLUSION:
            continue
        for j, carve in enumerate(_CARVE_BACK.finditer(body), start=1):
            carve_id = f"{clause_id}.carve{j}"
            clauses[carve_id] = Clause(
                clause_id=carve_id,
                kind=ClauseKind.CARVE_BACK,
                heading=f"{heading}／回復承保",
                verbatim=_tidy(carve.group(0)),
                page=_page_of(body_at + carve.start(), page_ends),
                overrides=(clause_id,),
            )

    if wait := _WAITING.search(full):
        days = cn_to_int(wait.group(1))
        # Anchored to the sentence, not the article, because it is not in one.
        line_start = full.rfind("（", 0, wait.start())
        line_end = full.find("）", wait.end())
        verbatim = full[line_start if line_start != -1 else wait.start() : line_end + 1 if line_end != -1 else wait.end()]
        clauses["waiting"] = Clause(
            clause_id="waiting",
            kind=ClauseKind.WAITING,
            heading=f"等待期 {days} 日（載於「疾病」定義，非獨立條文）",
            verbatim=_tidy(verbatim).strip(),
            page=_page_of(wait.start(), page_ends),
        )

    return ClauseIndex(doc_id=doc_id, title=title, clauses=clauses,
                       document_kind=document_kind(pages[0], has_first_article="art.1" in clauses))
