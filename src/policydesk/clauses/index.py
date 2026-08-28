"""Turn a policy PDF into an enumerated clause index.

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
from pathlib import Path

from msgspec import Struct
from pypdf import PdfReader

from policydesk.core.models import Citation, Clause, ClauseKind

_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# "第十七條 除外責任" and "第 19 條 保險範圍" — the same insurer numbers its articles two
# ways, and which one you get depends on when the contract was approved: contracts filed
# under the older approvals spell the number (第十七條), those revised from 113 年 onward
# print it (第 19 條). A parser that reads only one system silently drops half the corpus,
# and the half it drops is the currently-sold products.
#
# Anchored to the start of a line, because article headings start one and cross-references
# never do. Without the anchor, the body of article 5 ("除第二條第一項及第九條之限制外")
# registers as a fresh article 2 and overwrites the real one — and the summary table on
# page one ("（一）附約的保證續保及限制（第 19 條）") would register as every article at once.
_ARTICLE = re.compile(r"^[ \t　]*第\s*([一二三四五六七八九十百]+|\d{1,3})\s*條\s+([^\n]{0,30})", re.MULTILINE)

# "但為重建其基本功能所作之必要整型，不在此限" — the restoring half of an exclusion.
_CARVE_BACK = re.compile(r"但[^。；]{4,80}?(?:不在此限|除外|不適用)")

# "自本附約生效日起持續有效三十日以後" — a waiting period wearing a definition's clothes.
_WAITING = re.compile(r"(?:生效日|復效日)起持續有效([一二三四五六七八九十百]+)日")

# Page furniture that sits above the product name on page one: "第1頁，共31頁",
# "Since2023", a core-approval reference number, a service hotline.
_FURNITURE = re.compile(r"^(?:第\s*\d+\s*頁|共\s*\d+\s*頁|Since\s*\d{4}|[\W\d_]+)$|頁，共|核准文號|免費申訴")

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
    """Read an article number written either way the contracts write them.

    Args:
        text: A numeral such as "十七", "二十三", or "19".

    Returns:
        Its integer value.
    """
    if text.isdigit():
        return int(text)
    if (tens := text.find("十")) == -1:
        return sum(_CN_DIGITS.get(c, 0) for c in text)
    head = text[:tens]
    tail = text[tens + 1 :]
    return (_CN_DIGITS.get(head, 1) if head else 1) * 10 + (_CN_DIGITS.get(tail, 0) if tail else 0)


def _title_of(first_page: str) -> str:
    """Read the product name off page one.

    The name is rarely the first line. Above it sit a page counter, a "Since2023"
    watermark, an approval reference, a complaints hotline — furniture that a naive
    "first non-empty line" picks up instead, which is how a corpus ends up with six
    documents all titled 第1頁，共31頁.

    Args:
        first_page: Extracted text of page one.

    Returns:
        The product name, or an empty string when the page has no line that reads
        like one.
    """
    for raw in first_page.splitlines():
        if len(line := raw.strip()) < 8 or _FURNITURE.search(line):
            continue
        return line
    return ""


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

    def cite(self, clause_id: str) -> Citation:
        """Build a citation for a clause that exists.

        Args:
            clause_id: An id from this index.

        Returns:
            A citation carrying the clause's verbatim text.

        Raises:
            KeyError: The id is not in this contract. This is the guard that turns a
                fabricated citation into a crash instead of a plausible-looking figure.
        """
        clause = self.clauses[clause_id]
        return Citation(doc_id=self.doc_id, clause_id=clause_id, page=clause.page, verbatim=clause.verbatim)


def _page_of(offset: int, page_ends: list[int]) -> int:
    """Map a character offset in the joined text back to its 1-based page.

    Args:
        offset: Character position within the joined document text.
        page_ends: Cumulative end offset of each page, in order.

    Returns:
        The page number as a reader would cite it, counting from 1.
    """
    return bisect.bisect_right(page_ends, offset) + 1


def build_index(pdf_path: Path) -> ClauseIndex:
    """Index one policy contract.

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
    for m in _ARTICLE.finditer(full):
        if (number := cn_to_int(m.group(1))) <= highest:
            continue
        highest = number
        marks.append((m.start(), m.end(), number, m.group(2).strip()))

    clauses: dict[str, Clause] = {}

    for i, (start, body_at, number, heading) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(full)
        body = full[body_at:end].strip()
        clause_id = f"art.{number}"
        kind = _kind_for(heading)
        clauses[clause_id] = Clause(
            clause_id=clause_id,
            kind=kind,
            heading=heading,
            verbatim=body,
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
                verbatim=carve.group(0),
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
            verbatim=verbatim.strip(),
            page=_page_of(wait.start(), page_ends),
        )

    return ClauseIndex(doc_id=doc_id, title=title, clauses=clauses)
