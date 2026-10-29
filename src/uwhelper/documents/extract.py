"""Page-level text extraction (PDF text layer via PDFium), falling back to local Tesseract OCR."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from uwhelper.documents.injection import scan_text
from uwhelper.documents.ocr import OCRFailed, OCRUnavailable, ocr_image_bytes
from uwhelper.documents.pdf import DocumentOpenError, PasswordRequired, open_document

log = logging.getLogger("uwhelper.documents")
MAX_PAGE_CHARS = 200_000
TXT_PAGE_CHARS = 4000


@dataclass
class PageText:
    page_number: int
    text: str
    text_source: str  # text_layer | ocr | none
    ocr_status: str = "not_needed"  # not_needed | done | unavailable | failed
    injection_patterns: list[str] = field(default_factory=list)


@dataclass
class ExtractionOutcome:
    status: str  # processed | partial | failed | encrypted | unsupported
    error: str | None = None
    pages: list[PageText] = field(default_factory=list)
    page_count: int = 0

    @property
    def ocr_pages(self) -> int:
        return sum(1 for p in self.pages if p.text_source == "ocr")


def _ocr_page(doc, index: int, tesseract_cmd: str, lang: str, dpi: int, timeout: float) -> tuple[str, str]:
    png = doc.render_png(index, dpi=dpi, grayscale=True)
    try:
        return ocr_image_bytes(png, tesseract_cmd, lang, timeout), "done"
    except OCRUnavailable:
        return "", "unavailable"
    except OCRFailed as exc:
        log.warning("OCR failed on page %s: %s", index + 1, exc)
        return "", "failed"


def extract_text_file(path: Path) -> ExtractionOutcome:
    raw = path.read_bytes().decode("utf-8", "replace")
    chunks = raw.split("\f") if "\f" in raw else [raw[i:i + TXT_PAGE_CHARS] for i in range(0, max(len(raw), 1), TXT_PAGE_CHARS)]
    pages = []
    for i, c in enumerate(chunks, start=1):
        c = c[:MAX_PAGE_CHARS]
        pages.append(PageText(i, c, "text_layer" if c.strip() else "none", injection_patterns=scan_text(c)))
    return ExtractionOutcome("processed", None, pages, len(pages))


def extract_document(path: Path, ext: str, *, tesseract_cmd: str = "tesseract", ocr_lang: str = "eng",
                     ocr_dpi: int = 300, ocr_timeout: float = 120.0, min_text_chars: int = 25,
                     max_pages: int = 300, password: str | None = None) -> ExtractionOutcome:
    if ext == ".txt":
        return extract_text_file(path)
    try:
        with open_document(path, ext, password) as doc:
            return _extract_pages(doc, tesseract_cmd=tesseract_cmd, ocr_lang=ocr_lang, ocr_dpi=ocr_dpi,
                                  ocr_timeout=ocr_timeout, min_text_chars=min_text_chars, max_pages=max_pages)
    except PasswordRequired:
        return ExtractionOutcome("encrypted", "The PDF is password-protected. Supply the password when "
                                              "re-processing, or remove the protection and upload again.")
    except DocumentOpenError as exc:
        log.info("Document open failed: %s", exc)
        return ExtractionOutcome("failed", "The file could not be opened (malformed or corrupted).")


def _extract_pages(doc, *, tesseract_cmd: str, ocr_lang: str, ocr_dpi: int, ocr_timeout: float, min_text_chars: int,
                   max_pages: int) -> ExtractionOutcome:
    if doc.page_count == 0:
        return ExtractionOutcome("failed", "The document has no pages.")
    if doc.page_count > max_pages:
        return ExtractionOutcome("unsupported", f"The document has {doc.page_count} pages; the limit is {max_pages}.",
                                 page_count=doc.page_count)
    pages: list[PageText] = []
    failures = 0
    for i in range(doc.page_count):
        n = i + 1
        try:
            text = doc.page_text(i)
        except Exception as exc:
            log.warning("Text extraction failed on page %s: %s", n, exc.__class__.__name__)
            text, failures = "", failures + 1
        if len(text.strip()) >= min_text_chars:
            pt = PageText(n, text[:MAX_PAGE_CHARS], "text_layer")
        else:
            try:
                ocr_text, ocr_status = _ocr_page(doc, i, tesseract_cmd, ocr_lang, ocr_dpi, ocr_timeout)
            except Exception as exc:
                log.warning("Render failed on page %s: %s", n, exc.__class__.__name__)
                ocr_text, ocr_status = "", "failed"
            if ocr_text.strip():
                pt = PageText(n, ocr_text[:MAX_PAGE_CHARS], "ocr", ocr_status)
            elif text.strip():
                pt = PageText(n, text[:MAX_PAGE_CHARS], "text_layer", ocr_status)
            else:
                pt = PageText(n, "", "none", ocr_status)
                if ocr_status != "done":
                    failures += 1
        pt.injection_patterns = scan_text(pt.text)
        pages.append(pt)
    status = "processed" if failures == 0 else ("partial" if failures < len(pages) else "failed")
    err = None
    if failures:
        unavailable = any(p.ocr_status == "unavailable" for p in pages)
        err = (f"{failures} page(s) have no extractable text"
               + (" — Tesseract OCR is not installed/configured." if unavailable else "."))
    return ExtractionOutcome(status, err, pages, len(pages))
