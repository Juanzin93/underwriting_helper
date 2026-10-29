"""Formatting helpers shared by the web UI and reports (Jinja filters)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from jinja2 import Environment

from uwhelper.domain.enums import RESULT_SYMBOL
from uwhelper.domain.money import fmt_money, fmt_pct, fmt_ratio, fmt_value, to_decimal

FLAG_LABELS = {
    "unverified": "UNVERIFIED",
    "annualized": "ANNUALIZED",
    "stale": "STALE",
    "estimate": "ESTIMATE",
    "conflict": "CONFLICT",
    "scenario_adjusted": "SCENARIO",
    "reported_not_recomputed": "AS REPORTED",
    "period_mismatch": "PERIOD MISMATCH",
    "variable_rate": "VARIABLE RATE",
    "assumption_cogs_variable": "ASSUMPTION",
    "unverified_excluded": "UNVERIFIED (EXCLUDED)",
}

STATUS_TEXT = {
    "computed": "Calculated",
    "not_computable": "Not calculated — missing/contradictory inputs",
    "not_meaningful": "Not meaningful",
    "not_applicable": "Not applicable",
}

SOURCE_LABELS = {
    "extracted": "AI-extracted (untrusted until verified)",
    "user_entered": "User-entered",
    "user_corrected": "User-corrected",
}


def _d(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return to_decimal(v)
    except Exception:
        return None


def money(v: Any, places: int = 0) -> str:
    return fmt_money(_d(v), places)


def ratio(v: Any) -> str:
    return fmt_ratio(_d(v))


def pct(v: Any, places: int = 1) -> str:
    return fmt_pct(_d(v), places)


def value(v: Any, unit: str = "USD") -> str:
    if unit == "text":
        return str(v) if v is not None else "—"
    return fmt_value(_d(v), unit)


def rate(v: Any) -> str:
    d = _d(v)
    if d is None:
        return "—"
    return f"{d * 100:.3f}".rstrip("0").rstrip(".") + "%"


def result_label(r: str | None) -> str:
    return RESULT_SYMBOL.get(r or "", r or "—")


def result_class(r: str | None) -> str:
    return {"PASS": "res-pass", "FAIL": "res-fail", "UNKNOWN": "res-unknown", "NOT_APPLICABLE": "res-na",
            "REVIEW_REQUIRED": "res-review"}.get(r or "", "")


def flag_label(f: str) -> str:
    return FLAG_LABELS.get(f, f.replace("_", " ").upper())


def fact_value(f: dict[str, Any]) -> str:
    if f.get("value_numeric") is not None:
        return value(f["value_numeric"], f.get("unit") or "USD")
    return f.get("value_text") or "—"


def register_filters(env: Environment) -> None:
    env.filters.update(money=money, ratio=ratio, pct=pct, fmtval=value, rate=rate, result_label=result_label,
                       result_class=result_class, flag_label=flag_label, fact_value=fact_value)
    env.globals.update(STATUS_TEXT=STATUS_TEXT, SOURCE_LABELS=SOURCE_LABELS)
