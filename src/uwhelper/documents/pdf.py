"""Reading uploaded documents: PDFs with pypdfium2 (PDFium, BSD-3/Apache-2.0), images with Pillow (MIT-CMU).

This is the only module that opens uploaded PDFs/images. It exposes a tiny interface so the rest of the app never
depends on a particular library:

    with open_document(path, ext, password=None) as doc:
        doc.page_count
        doc.page_text(i)          # text layer of page i (0-based); "" for images
        doc.render_png(i, dpi, grayscale=False)

Errors: ``PasswordRequired`` (encrypted PDF without the right password), ``DocumentOpenError`` (malformed/unsupported).
Document content is untrusted: nothing here executes it; PDFium only parses and renders.
"""

from __future__ import annotations

import io
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pypdfium2 as pdfium
from PIL import Image, ImageSequence

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff")
PDFIUM_PASSWORD_ERROR = 4
MAX_IMAGE_PIXELS = 120_000_000  # refuse decompression bombs (≈ 11,000 × 11,000)


class DocumentOpenError(Exception):
    pass


class PasswordRequired(DocumentOpenError):
    pass


class _PdfDoc:
    def __init__(self, pdf: pdfium.PdfDocument):
        self._pdf = pdf

    @property
    def page_count(self) -> int:
        return len(self._pdf)

    def page_text(self, index: int) -> str:
        page = self._pdf[index]
        try:
            tp = page.get_textpage()
            try:
                return tp.get_text_range().replace("\r\n", "\n").replace("\r", "\n")
            finally:
                tp.close()
        finally:
            page.close()

    def render_png(self, index: int, dpi: int = 110, grayscale: bool = False) -> bytes:
        page = self._pdf[index]
        try:
            bitmap = page.render(scale=dpi / 72, grayscale=grayscale)
            img = bitmap.to_pil()
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue()
        finally:
            page.close()

    def close(self) -> None:
        self._pdf.close()


class _ImageDoc:
    def __init__(self, img: Image.Image):
        self._img = img
        self._frames = sum(1 for _ in ImageSequence.Iterator(img))

    @property
    def page_count(self) -> int:
        return self._frames

    def page_text(self, index: int) -> str:
        return ""

    def render_png(self, index: int, dpi: int = 110, grayscale: bool = False) -> bytes:
        self._img.seek(index)
        frame = self._img.convert("L" if grayscale else "RGB")
        buf = io.BytesIO()
        frame.save(buf, "PNG")
        return buf.getvalue()

    def close(self) -> None:
        self._img.close()


@contextmanager
def open_document(path: Path | str | bytes, ext: str, password: str | None = None) -> Iterator[_PdfDoc | _ImageDoc]:
    ext = ext.lower()
    if ext in IMAGE_EXTS:
        try:
            img = Image.open(io.BytesIO(path) if isinstance(path, bytes) else path)  # reads the header only
            if img.width * img.height > MAX_IMAGE_PIXELS:
                img.close()
                raise DocumentOpenError("image too large")
            img.load()
        except (OSError, Image.DecompressionBombError, ValueError) as exc:
            raise DocumentOpenError(exc.__class__.__name__) from None
        doc: _PdfDoc | _ImageDoc = _ImageDoc(img)
    else:
        try:
            pdf = pdfium.PdfDocument(path if isinstance(path, bytes) else str(path), password=password)
        except pdfium.PdfiumError as exc:
            if getattr(exc, "err_code", None) == PDFIUM_PASSWORD_ERROR:
                raise PasswordRequired("password required or incorrect") from None
            raise DocumentOpenError("malformed or unsupported PDF") from None
        doc = _PdfDoc(pdf)
    try:
        yield doc
    finally:
        doc.close()


def page_count(data: bytes, ext: str = ".pdf") -> int:
    with open_document(data, ext) as d:
        return d.page_count


def text_of(data: bytes, ext: str = ".pdf") -> str:
    """All page text (for tests and checks)."""
    with open_document(data, ext) as d:
        return "\n".join(d.page_text(i) for i in range(d.page_count))
