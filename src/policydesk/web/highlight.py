"""
Render the contract page a citation points at, with the cited lines picked out.

A clause id in a reply is a promise: *this sentence came from that contract, on that
page*. The promise is only checkable if the reader can get to the page. So `[art.16]`
opens the page it names, rendered from the insurer's own PDF, with the lines the clause
occupies highlighted — the same way a person reading a contract runs a marker over the
part that answers the question.

Two libraries, one page, both already here for the ingest:

- **pypdfium2** renders the page to a bitmap. It is what pdfplumber already depends on,
  so this costs no new wheel.
- **pdfplumber** returns the text boxes. On this corpus a "word" is a whole line, which
  is the granularity a highlight wants anyway — a marker over half a line reads as a
  mistake.

Matching is done on whitespace-stripped text. The ingest strips the CJK inter-character
spacing the PDFs carry, so the clause text in the database and the line text on the page
differ in exactly that, and normalising both is what makes them comparable.
"""

import io
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import pdfplumber
import pypdfium2 as pdfium
from PIL import Image, ImageDraw

from policydesk.bootloader import logger

if TYPE_CHECKING:
    from pathlib import Path

SCALE = 2.0
"""Render scale. At 2.0 a page is about 1200x1700, which is legible on a laptop without
sending a megabyte per citation."""

FILL = (255, 235, 59, 90)
"""Marker yellow, translucent, so the text stays readable underneath it."""

EDGE = (250, 179, 0, 200)

MIN_LINE = 4
"""Shorter lines are page furniture — numbers, headers — and matching on them highlights
half the page."""


def _flat(text: str) -> str:
    """
    Strip every space so a page line and a stored clause can be compared.

    Args:
        text: Either side's text.

    Returns:
        The text with all whitespace removed.

    """
    return "".join(text.split())


def _wanted(lines: list[dict[str, Any]], needle: str) -> list[dict[str, Any]]:
    """
    Pick the page lines the clause occupies.

    Args:
        lines: Text boxes from pdfplumber.
        needle: The clause text as stored.

    Returns:
        The boxes whose text belongs to the clause. A line counts when the clause
        contains it — not the reverse — because the stored clause is the longer text and
        the page splits it across lines.

    """
    flat = _flat(needle)
    if not flat:
        return []
    return [
        line
        for line in lines
        if len(clean := _flat(line["text"])) >= MIN_LINE and clean in flat
    ]


def page_count(pdf: Path) -> int:
    """
    Count a contract's pages.

    Args:
        pdf: The contract file.

    Returns:
        How many pages, zero when the file cannot be opened.

    """
    try:
        document = pdfium.PdfDocument(str(pdf))
    except (OSError, pdfium.PdfiumError):
        return 0
    try:
        return len(document)
    finally:
        document.close()


@lru_cache(maxsize=96)
def page_image(pdf: Path, page: int, needle: str) -> bytes | None:
    """
    Render one page with the cited lines highlighted.

    Args:
        pdf: The contract file.
        page: Which page, 1-based, as the clause index records it.
        needle: The clause text to mark. Empty renders the page unmarked, which is what
            reading a contract straight through wants.

    Returns:
        PNG bytes, or None when the file or the page is not there. None is a degradation
        the caller answers with the clause text alone — a citation whose page will not
        render is still a citation whose words can be read.

    Cached, because a render plus a box extraction is about a quarter of a second and the
    arguments are a page of a file that does not change. Sixty-four entries is a demo's
    worth of citations at roughly half a megabyte each.

    """
    try:
        document = pdfium.PdfDocument(str(pdf))
    except (OSError, pdfium.PdfiumError) as exc:
        logger.warning("clause_page_unreadable", pdf=str(pdf), error=str(exc))
        return None

    try:
        if not 1 <= page <= len(document):
            return None
        image = document[page - 1].render(scale=SCALE).to_pil().convert("RGBA")
    finally:
        document.close()

    try:
        with pdfplumber.open(pdf) as plumbed:
            lines = plumbed.pages[page - 1].extract_words()
    except (OSError, IndexError, ValueError) as exc:
        logger.warning("clause_boxes_unreadable", pdf=str(pdf), page=page, error=str(exc))
        lines = []

    marks = _wanted(lines, needle)
    if marks:
        # Drawn on its own layer and composited, so the fill is translucent over the
        # glyphs rather than painted on top of them.
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        pen = ImageDraw.Draw(layer)
        for line in marks:
            box = (line["x0"] * SCALE, line["top"] * SCALE, line["x1"] * SCALE, line["bottom"] * SCALE)
            pen.rectangle(box, fill=FILL, outline=EDGE, width=2)
        image = Image.alpha_composite(image, layer)

    buffer = io.BytesIO()
    # `optimize=True` costs 212 ms against 53 ms here and returns 877,765 bytes against
    # 893,707 — 68% of this function's latency for 1.8% of its size, on the path a
    # customer waits on after clicking a citation. Measured on a real corpus page.
    image.convert("RGB").save(buffer, format="PNG")
    logger.info("clause_page_rendered", pdf=pdf.name, page=page, marked=len(marks))
    return buffer.getvalue()
