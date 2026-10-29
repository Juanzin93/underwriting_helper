"""Writing simple PDFs (synthetic sample documents, test fixtures) with ReportLab (BSD licence).

* ``text_pdf``  — pages of text with a real text layer
* ``scanned_pdf`` — pages that are only images of text (no text layer), to exercise OCR
* optional password protection (``password=``) to exercise encrypted-PDF handling
Output is deterministic (no timestamps).
"""

from __future__ import annotations

import io

from reportlab.lib import pdfencrypt
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

PAGE = (612, 792)  # US letter, points


def _canvas(buf: io.BytesIO, password: str | None) -> canvas.Canvas:
    enc = pdfencrypt.StandardEncryption(password, ownerPassword=password + "-owner", strength=128) if password else None
    return canvas.Canvas(buf, pagesize=PAGE, invariant=1, encrypt=enc)


def _draw_text_page(c: canvas.Canvas, title: str, lines: list[str]) -> None:
    y = PAGE[1] - 72
    c.setFont("Helvetica-Bold", 15)
    c.drawString(72, y, title)
    y -= 28
    c.setFont("Helvetica", 11)
    for line in lines:
        c.drawString(72, y, line)
        y -= 17


def text_pdf(pages: list[tuple[str, list[str]]], *, password: str | None = None) -> bytes:
    buf = io.BytesIO()
    c = _canvas(buf, password)
    for title, lines in pages:
        _draw_text_page(c, title, lines)
        c.showPage()
    c.save()
    return buf.getvalue()


def lines_pdf(lines: list[str], *, password: str | None = None) -> bytes:
    """One page of plain lines (no title) — handy for tests."""
    buf = io.BytesIO()
    c = _canvas(buf, password)
    c.setFont("Helvetica", 11)
    y = PAGE[1] - 72
    for line in lines:
        c.drawString(72, y, line)
        y -= 16
    c.showPage()
    c.save()
    return buf.getvalue()


def image_pdf(png_pages: list[bytes]) -> bytes:
    buf = io.BytesIO()
    c = _canvas(buf, None)
    for png in png_pages:
        c.drawImage(ImageReader(io.BytesIO(png)), 0, 0, width=PAGE[0], height=PAGE[1])
        c.showPage()
    c.save()
    return buf.getvalue()


def text_page_png(title: str, lines: list[str], dpi: int = 200) -> bytes:
    """A grayscale PNG picture of a text page (what a scanner would produce)."""
    from uwhelper.documents.pdf import open_document

    with open_document(text_pdf([(title, lines)]), ".pdf") as d:
        return d.render_png(0, dpi=dpi, grayscale=True)


def scanned_pdf(pages: list[tuple[str, list[str]]]) -> bytes:
    return image_pdf([text_page_png(t, ls) for t, ls in pages])
