"""Shared helpers for reading user-supplied CSV files (read in memory only; never stored by these helpers)."""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime

from uwhelper.repo.cases import ValidationFailure

MAX_BYTES = 1_000_000


def decode(raw: bytes, max_bytes: int = MAX_BYTES) -> str:
    """UTF-8 (with or without BOM), else Windows-1252 (Excel's default on Windows)."""
    if len(raw) > max_bytes:
        raise ValidationFailure(f"The CSV file is larger than {max_bytes // 1_000_000} MB; split it into smaller files.")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def norm_header(header: str) -> str:
    return "_".join(re.sub(r"[^a-z0-9 ]+", " ", header.strip().lower()).split())


def read_rows(text: str, max_rows: int = 5000) -> tuple[list[str], list[tuple[int, list[str]]]]:
    """Header (normalised) and non-blank rows with their 1-based line numbers."""
    reader = csv.reader(io.StringIO(text))
    try:
        header = [norm_header(h) for h in next(reader)]
    except StopIteration:
        raise ValidationFailure("The CSV file is empty.") from None
    rows = []
    for n, values in enumerate(reader, start=2):
        if not any(v.strip() for v in values):
            continue
        if len(rows) >= max_rows:
            raise ValidationFailure(f"More than {max_rows} rows; split the file.")
        rows.append((n, [v.strip() for v in values]))
    return header, rows


def parse_date(value: str) -> date | None:
    """ISO (2026-01-31) or US (1/31/2026, 01/31/26). Blank → None. Anything else raises ValueError."""
    v = (value or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {value!r}")
