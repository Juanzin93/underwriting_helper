"""HTML → PDF for the app's own report templates, using ReportLab (BSD licence, pure Python).

Supports the subset the templates use: h1–h3, p, div (``banner``/``box``/``narrative`` get a frame), ul/ol/li,
tables (th/td, ``colspan``, ``class="num"`` right-aligned, header rows repeated on each page), b/strong, i/em,
br, span/a/label (``small``/``muted`` shrink the text). head/style/script/title are ignored. Any other tag's text is
kept as plain text, so nothing is silently dropped.

Fonts: a Unicode TrueType font found on this PC (Arial or Segoe UI on Windows, DejaVu/Liberation on Linux, Arial on
macOS) is embedded so characters like ≥, →, ✔ and × print correctly; if none is found, the built-in Helvetica is used
and those characters are replaced with ASCII equivalents. Output is deterministic (``invariant``: no timestamps or
random IDs), so the same HTML gives byte-identical PDFs. Nothing is fetched from the network.
"""

from __future__ import annotations

import html
import io
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

FOOTER = "PRELIMINARY SCREENING — NOT A CREDIT DECISION — page {page} of {total}"
FONT_CANDIDATES = [
    (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
    (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\segoeuib.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]
ASCII_FALLBACK = {"≥": ">=", "≤": "<=", "→": "->", "←": "<-", "✔": "v", "✖": "x", "≠": "!=", "⚠": "!", "▶": ">",
                  "✓": "v", "…": "...", "−": "-", "≈": "~"}

_fonts: tuple[str, str, bool] | None = None


def fonts() -> tuple[str, str, bool]:
    """(regular, bold, unicode_capable) — registered once per process."""
    global _fonts
    if _fonts is None:
        _fonts = ("Helvetica", "Helvetica-Bold", False)
        for regular, bold in FONT_CANDIDATES:
            if Path(regular).is_file() and Path(bold).is_file():
                try:
                    pdfmetrics.registerFont(TTFont("UWH-Regular", regular))
                    pdfmetrics.registerFont(TTFont("UWH-Bold", bold))
                    pdfmetrics.registerFontFamily("UWH", normal="UWH-Regular", bold="UWH-Bold",
                                                  italic="UWH-Regular", boldItalic="UWH-Bold")
                    _fonts = ("UWH-Regular", "UWH-Bold", True)
                    break
                except Exception:
                    continue
    return _fonts


def _clean(text: str, unicode_ok: bool) -> str:
    if not unicode_ok:
        for k, v in ASCII_FALLBACK.items():
            text = text.replace(k, v)
        text = text.encode("cp1252", "replace").decode("cp1252")
    return text


def _para(markup: str, style: ParagraphStyle, **kw) -> Paragraph:
    """A paragraph; if the markup is not valid for ReportLab, fall back to its plain text (never fail the PDF)."""
    try:
        return Paragraph(markup, style, **kw)
    except ValueError:
        plain = html.escape(html.unescape(re.sub(r"<[^>]+>", " ", markup)), quote=False)
        return Paragraph(re.sub(r"\s+", " ", plain).strip() or "&nbsp;", style, **kw)


@dataclass
class _Cell:
    markup: str = ""
    header: bool = False
    right: bool = False
    colspan: int = 1


@dataclass
class _TableCtx:
    rows: list[list[_Cell]] = field(default_factory=list)
    cell: _Cell | None = None


class _Converter(HTMLParser):
    BLOCK = frozenset({"h1", "h2", "h3", "h4", "p", "div", "li", "pre", "blockquote"})
    SKIP = frozenset({"head", "style", "script", "title"})

    def __init__(self, styles: dict[str, ParagraphStyle], unicode_ok: bool, width: float):
        super().__init__(convert_charrefs=True)
        self.styles, self.unicode_ok, self.width = styles, unicode_ok, width
        self.flow: list = []
        self.buf: list[str] = []
        self.style = "body"
        self.skip = 0
        self.tables: list[_TableCtx] = []
        self.inline: list[str] = []
        self.list_depth = 0
        self.block_stack: list[str] = []

    # ------------------------------------------------------------------ helpers
    def _emit(self, markup: str) -> None:
        if self.tables and self.tables[-1].cell is not None:
            self.tables[-1].cell.markup += markup
        else:
            self.buf.append(markup)

    def _flush(self) -> None:
        text = "".join(self.buf).strip()
        self.buf = []
        if text and re.sub(r"<[^>]+>", "", text).strip():
            if self.style == "li":
                self.flow.append(_para(text, self.styles["li"], bulletText="•"))
            else:
                self.flow.append(_para(text, self.styles[self.style]))

    # ------------------------------------------------------------------ parser events
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "table":
            self._flush()
            self.tables.append(_TableCtx())
        elif tag == "tr" and self.tables:
            self.tables[-1].rows.append([])
        elif tag in ("td", "th") and self.tables:
            t = self.tables[-1]
            if not t.rows:
                t.rows.append([])
            try:
                span = max(1, min(int(a.get("colspan") or 1), 20))
            except ValueError:
                span = 1
            t.cell = _Cell(header=tag == "th", right="num" in cls, colspan=span)
        elif tag in self.BLOCK and not (self.tables and self.tables[-1].cell is not None):
            parent = self.block_stack[-1] if self.block_stack else "body"
            self._flush()
            if tag in ("h1", "h2", "h3", "h4"):
                style = "h3" if tag == "h4" else tag
            elif tag == "li":
                style = "li"
            elif set(cls) & {"banner", "box", "narrative"}:
                style = "banner"
            elif "small" in cls or "muted" in cls:
                style = "small"
            else:
                style = parent if parent in ("banner", "small") else "body"
            self.block_stack.append(style)
            self.style = style
        elif tag in ("ul", "ol"):
            self._flush()
            self.list_depth += 1
        elif tag in ("b", "strong", "th"):
            self._emit("<b>")
            self.inline.append("</b>")
        elif tag in ("i", "em"):
            self._emit("<i>")
            self.inline.append("</i>")
        elif tag in ("span", "a", "label", "small"):
            if "small" in cls or "muted" in cls or tag == "small":
                self._emit('<font size="7.5" color="#444444">')
                self.inline.append("</font>")
            else:
                self.inline.append("")
        elif tag == "br":
            self._emit("<br/>")
        elif tag in ("p", "div", "li") and self.tables and self.tables[-1].cell is not None:
            if self.tables[-1].cell.markup.strip():
                self._emit("<br/>")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag == "br" and not self.skip:
            self._emit("<br/>")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ("td", "th") and self.tables and self.tables[-1].cell is not None:
            t = self.tables[-1]
            t.rows[-1].append(t.cell)
            t.cell = None
        elif tag == "table" and self.tables:
            t = self.tables.pop()
            if self.tables and self.tables[-1].cell is not None:  # nested table: flatten as text
                for row in t.rows:
                    self._emit(" · ".join(c.markup for c in row) + "<br/>")
            else:
                self._table(t)
        elif tag in self.BLOCK and not (self.tables and self.tables[-1].cell is not None):
            self._flush()
            if self.block_stack:
                self.block_stack.pop()
            self.style = self.block_stack[-1] if self.block_stack else "body"
        elif tag in ("ul", "ol"):
            self._flush()
            self.list_depth = max(0, self.list_depth - 1)
        elif tag in ("b", "strong", "i", "em", "span", "a", "label", "small") and self.inline:
            self._emit(self.inline.pop())

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        text = re.sub(r"\s+", " ", data)
        if not text.strip() and not (self.buf or (self.tables and self.tables[-1].cell)):
            return
        self._emit(html.escape(_clean(text, self.unicode_ok), quote=False))

    def close(self) -> None:
        super().close()
        while self.inline:
            self._emit(self.inline.pop())
        self._flush()

    # ------------------------------------------------------------------ tables
    def _table(self, t: _TableCtx) -> None:
        rows = [r for r in t.rows if r]
        if not rows:
            return
        ncols = max(sum(c.colspan for c in r) for r in rows)
        data, spans, styles = [], [], []
        weights = [1.0] * ncols
        for ri, r in enumerate(rows):
            line, ci = [], 0
            for c in r:
                st = self.styles["cell_r" if c.right else ("cell_h" if c.header else "cell")]
                line.append(_para(c.markup.strip() or "&nbsp;", st))
                if c.colspan > 1:
                    spans.append(("SPAN", (ci, ri), (ci + c.colspan - 1, ri)))
                    line.extend([""] * (c.colspan - 1))
                else:
                    plain = re.sub(r"<[^>]+>", "", c.markup)
                    weights[ci] = max(weights[ci], min(len(plain), 60) ** 0.8)
                if c.header:
                    styles.append(("BACKGROUND", (ci, ri), (ci + c.colspan - 1, ri), colors.HexColor("#e8e8e8")))
                ci += c.colspan
            line.extend([""] * (ncols - len(line)))
            data.append(line)
        # Minimum width per column = its longest word (so words are not broken), then share the rest by content.
        mins = [0.35 * inch] * ncols
        for r in rows:
            ci = 0
            for c in r:
                if c.colspan == 1:
                    plain = html.unescape(re.sub(r"<[^>]+>", " ", c.markup))
                    font = self.styles["cell_h" if c.header else "cell"].fontName
                    longest = max((pdfmetrics.stringWidth(w, font, 8) for w in plain.split()), default=0)
                    mins[ci] = max(mins[ci], min(longest + 8, 1.6 * inch))
                ci += c.colspan
        spare = max(0.0, self.width - sum(mins))
        total = sum(weights)
        widths = [m + spare * w / total for m, w in zip(mins, weights)]
        scale = self.width / sum(widths)
        widths = [w * scale for w in widths]
        header_rows = 1 if all(c.header for c in rows[0]) and len(rows) > 1 else 0
        table = Table(data, colWidths=widths, repeatRows=header_rows, hAlign="LEFT")
        table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                                   ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
                                   *spans, *styles]))
        self.flow.append(table)
        self.flow.append(Spacer(1, 5))


def _styles(regular: str, bold: str) -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("body", fontName=regular, fontSize=9, leading=11.5, spaceAfter=3)
    return {
        "body": base,
        "small": ParagraphStyle("small", parent=base, fontSize=7.5, leading=9.5, textColor=colors.HexColor("#333333")),
        "h1": ParagraphStyle("h1", parent=base, fontName=bold, fontSize=16, leading=19, spaceAfter=4),
        "h2": ParagraphStyle("h2", parent=base, fontName=bold, fontSize=12, leading=15, spaceBefore=10, spaceAfter=4,
                             borderWidth=0, textColor=colors.HexColor("#1f3b63")),
        "h3": ParagraphStyle("h3", parent=base, fontName=bold, fontSize=10, leading=12.5, spaceBefore=6, spaceAfter=3),
        "li": ParagraphStyle("li", parent=base, leftIndent=12, bulletIndent=2),
        "banner": ParagraphStyle("banner", parent=base, fontName=bold, borderWidth=1.2, borderColor=colors.black,
                                 borderPadding=5, spaceBefore=10, spaceAfter=10, backColor=colors.HexColor("#fff7e6")),
        "cell": ParagraphStyle("cell", parent=base, fontSize=8, leading=9.6, spaceAfter=0),
        "cell_h": ParagraphStyle("cell_h", parent=base, fontName=bold, fontSize=8, leading=9.6, spaceAfter=0),
        "cell_r": ParagraphStyle("cell_r", parent=base, fontSize=8, leading=9.6, spaceAfter=0, alignment=TA_RIGHT),
    }


def _numbered_canvas(font: str, unicode_ok: bool):
    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages: list[dict] = []

        def showPage(self) -> None:
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self) -> None:
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self.setFont(font, 7)
                self.drawString(40, 25, _clean(FOOTER.format(page=self._pageNumber, total=total), unicode_ok))
                super().showPage()
            super().save()

    return NumberedCanvas


def render_pdf(html_text: str, *, title: str = "UW Helper report") -> bytes:
    regular, bold, unicode_ok = fonts()
    margin = 40
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=margin, rightMargin=margin, topMargin=margin,
                            bottomMargin=50, title=title, author="", creator="UW Helper (local)",
                            producer="UW Helper (local) / ReportLab", subject="", invariant=1)
    conv = _Converter(_styles(regular, bold), unicode_ok, letter[0] - 2 * margin)
    conv.feed(html_text)
    conv.close()
    story = conv.flow or [Paragraph("(empty)", conv.styles["body"])]
    if isinstance(story[0], Paragraph) and len(story) > 1:
        story[:2] = [KeepTogether(story[:2])]
    doc.build(story, canvasmaker=_numbered_canvas(regular, unicode_ok))
    return buf.getvalue()
