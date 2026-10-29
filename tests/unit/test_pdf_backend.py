"""PDF/image backend (pypdfium2 + Pillow for reading, ReportLab for writing) — Milestone 8.2 licensing switch."""

import io
from pathlib import Path

import pytest
from PIL import Image

from uwhelper.documents import pdf as P
from uwhelper.documents import pdfwrite as W
from uwhelper.reports import pdfrender as R

SRC = Path(__file__).resolve().parents[2] / "src"


def test_no_agpl_pdf_library_is_imported():
    offenders = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
                 if "import pymupdf" in p.read_text(encoding="utf-8") or "import fitz" in p.read_text(encoding="utf-8")]
    assert offenders == []


def test_text_pdf_round_trip_and_rendering():
    data = W.text_pdf([("Title", ["Revenue 1,234,567", "Net income (230)"]), ("Second", ["Page two"])])
    with P.open_document(data, ".pdf") as d:
        assert d.page_count == 2
        assert "Revenue 1,234,567" in d.page_text(0) and "\r" not in d.page_text(0)
        png = d.render_png(1, dpi=72, grayscale=True)
    img = Image.open(io.BytesIO(png))
    assert img.mode == "L" and img.size == (612, 792)


def test_password_and_malformed_pdfs():
    locked = W.lines_pdf(["Secret"], password="pw")
    with pytest.raises(P.PasswordRequired):
        with P.open_document(locked, ".pdf"):
            pass
    with pytest.raises(P.PasswordRequired):
        with P.open_document(locked, ".pdf", password="wrong"):
            pass
    with P.open_document(locked, ".pdf", password="pw") as d:
        assert "Secret" in d.page_text(0)
    with pytest.raises(P.DocumentOpenError):
        with P.open_document(b"%PDF-1.4 this is not really a pdf", ".pdf"):
            pass


def test_images_including_multipage_tiff(tmp_path):
    frames = [Image.new("L", (200, 100), color=c) for c in (0, 128, 255)]
    tif = tmp_path / "scan.tif"
    frames[0].save(tif, save_all=True, append_images=frames[1:])
    with P.open_document(tif, ".tif") as d:
        assert d.page_count == 3 and d.page_text(0) == ""
        assert Image.open(io.BytesIO(d.render_png(2))).getpixel((5, 5)) == (255, 255, 255)
    with pytest.raises(P.DocumentOpenError):
        with P.open_document(b"not an image", ".png"):
            pass


def test_oversized_image_refused(monkeypatch):
    monkeypatch.setattr(P, "MAX_IMAGE_PIXELS", 1000)
    buf = io.BytesIO()
    Image.new("L", (100, 100)).save(buf, "PNG")
    with pytest.raises(P.DocumentOpenError, match="too large"):
        with P.open_document(buf.getvalue(), ".png"):
            pass


def test_scanned_pdf_has_no_text_layer():
    data = W.scanned_pdf([("Quote", ["Total price: $600,000"])])
    assert P.text_of(data).strip() == "" and P.page_count(data) == 1


def test_report_pdf_is_deterministic_paginated_and_robust():
    html = ("<html><head><style>p{}</style><title>x</title></head><body><p class='banner'>NOT A DECISION ≥ 1.25x</p>"
            "<table><tr><th>A</th><th class='num'>B</th></tr>"
            + "".join(f"<tr><td>row {i}</td><td class='num'>{i}</td></tr>" for i in range(120))
            + "<tr><td colspan='2'>Total</td></tr></table><p><b>unclosed <i>tags</p><ul><li>one</li></ul></body></html>")
    a, b = R.render_pdf(html), R.render_pdf(html)
    assert a == b  # byte-identical
    text = P.text_of(a)
    assert P.page_count(a) >= 3 and "row 119" in text and "page 1 of" in text and "Total" in text
    assert "≥" in text or ">=" in text


def test_ascii_fallback_without_unicode_font(monkeypatch):
    monkeypatch.setattr(R, "_fonts", ("Helvetica", "Helvetica-Bold", False))
    text = P.text_of(R.render_pdf("<p>DSCR ≥ 1.25x → PASS ✔ — ok</p>"))
    assert "DSCR >= 1.25x -> PASS v" in text and "—" in text
