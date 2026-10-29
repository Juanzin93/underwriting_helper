"""Decimal parsing, rounding and formatting.

All financial arithmetic uses :class:`decimal.Decimal`. Floats are accepted
only at the edges and converted through ``str`` to avoid binary artefacts.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

MONEY_PLACES = Decimal("0.01")
RATIO_PLACES = Decimal("0.000001")

SCALES = {
    None: Decimal(1),
    "": Decimal(1),
    "units": Decimal(1),
    "thousands": Decimal(1000),
    "millions": Decimal(1_000_000),
    "billions": Decimal(1_000_000_000),
}


class InvalidAmount(ValueError):
    pass


_CLEAN_RE = re.compile(r"[\s$€£,]")
_NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")


def to_decimal(value: object, *, allow_none: bool = True) -> Decimal | None:
    """Strictly convert user / document input to Decimal.

    Accepts ``1,234.50``, ``$1,234``, ``(1,234)`` (accounting negative),
    ``-1234``, ``12.5%`` (returned as 0.125). Rejects NaN/Infinity and text.
    """
    if value is None:
        if allow_none:
            return None
        raise InvalidAmount("A value is required.")
    if isinstance(value, bool):
        raise InvalidAmount("Boolean is not a valid amount.")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, float):
        d = Decimal(str(value))
    else:
        text = str(value).strip()
        if text == "":
            if allow_none:
                return None
            raise InvalidAmount("A value is required.")
        negative = False
        if text.startswith("(") and text.endswith(")"):
            negative, text = True, text[1:-1]
        if text.endswith("-") and not text.startswith("-"):  # trailing minus (some ledgers)
            negative, text = True, text[:-1]
        percent = text.endswith("%")
        if percent:
            text = text[:-1]
        text = _CLEAN_RE.sub("", text)
        if not _NUMBER_RE.match(text):
            raise InvalidAmount(f"Not a valid number: {str(value)[:40]!r}")
        try:
            d = Decimal(text)
        except InvalidOperation as exc:  # pragma: no cover - regex guards this
            raise InvalidAmount(str(exc)) from exc
        if percent:
            d = d / Decimal(100)
        if negative:
            d = -abs(d)
    if not d.is_finite():
        raise InvalidAmount("Amount must be finite.")
    return d


def apply_scale(value: Decimal, scale: str | None) -> Decimal:
    key = (scale or "").strip().lower()
    if key not in SCALES:
        raise InvalidAmount(f"Unknown scale {scale!r}")
    return value * SCALES[key]


def q_money(d: Decimal | None) -> Decimal | None:
    return None if d is None else d.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def q_ratio(d: Decimal | None) -> Decimal | None:
    return None if d is None else d.quantize(RATIO_PLACES, rounding=ROUND_HALF_UP)


def round_to(d: Decimal, places: int) -> Decimal:
    return d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def dstr(d: Decimal | None) -> str | None:
    """Canonical storage string (no exponent)."""
    if d is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 40
        s = format(d, "f")
    return s


def fmt_money(d: Decimal | None, places: int = 0) -> str:
    if d is None:
        return "—"
    r = round_to(d, places)
    s = f"{abs(r):,.{places}f}"
    return f"-${s}" if r < 0 else f"${s}"


def fmt_ratio(d: Decimal | None, places: int = 2) -> str:
    if d is None:
        return "—"
    return f"{round_to(d, places):,.{places}f}x"


def fmt_pct(d: Decimal | None, places: int = 1) -> str:
    if d is None:
        return "—"
    return f"{round_to(d * 100, places):,.{places}f}%"


def fmt_value(d: Decimal | None, unit: str) -> str:
    if unit == "USD":
        return fmt_money(d)
    if unit == "ratio":
        return fmt_ratio(d)
    if unit == "percent":
        return fmt_pct(d)
    if unit == "years":
        return "—" if d is None else f"{d:.1f} years"
    if d is None:
        return "—"
    return f"{d:,}"
