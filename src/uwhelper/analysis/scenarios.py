"""Scenario and sensitivity analysis on copies of the inputs (the case is never modified).

Shock mechanics (stated assumptions):
* Revenue change r: revenue × (1+r); COGS × (1+r) (COGS treated as variable).
  EBITDA, operating income and net income change by Δrevenue − ΔCOGS.
  Income-tax effects of the shock are ignored (conservative for downside shocks).
* Operating-expense change e: operating expenses × (1+e); EBITDA etc. change by −Δopex.
* Rate change (bps): added to the proposed loan rate only. Existing debt payments unchanged.
* Overrides replace the proposed loan amount / rate / amortization.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from uwhelper.domain.money import dstr, q_ratio, to_decimal
from uwhelper.metrics.types import AnalysisInputs, InputValue

_FLOW_DOWN = ("net_income", "operating_expenses")


def _shift(iv: InputValue, delta: Decimal, why: str) -> None:
    if iv.ok and delta != 0:
        iv.value = iv.value + delta  # type: ignore[operator]
        iv.flags.add("scenario_adjusted")
        iv.note = (iv.note + " " if iv.note else "") + why


def apply_scenario(inputs: AnalysisInputs, adj: dict[str, Any]) -> AnalysisInputs:
    s = inputs.clone()
    r = to_decimal(adj.get("revenue_change_pct")) or Decimal(0)
    e = to_decimal(adj.get("opex_change_pct")) or Decimal(0)
    bps = int(adj.get("rate_change_bps") or 0)
    v = s.values
    delta = Decimal(0)
    if r != 0:
        rev, cogs = v.get("revenue"), v.get("cost_of_goods_sold")
        if rev is None or cogs is None or not (rev.ok and cogs.ok):
            # Cannot apply the shock without both components: make the result not computable.
            why = "Revenue shock cannot be applied: revenue and COGS are both required."
            v["revenue"] = InputValue.missing("revenue", "Revenue", why)
            v["net_income"] = InputValue.missing("net_income", "Net income (shocked)", why)
        else:
            d_rev, d_cogs = rev.value * r, cogs.value * r  # type: ignore[operator]
            _shift(rev, d_rev, f"Scenario revenue {r * 100:+.1f}%.")
            _shift(cogs, d_cogs, f"Scenario COGS {r * 100:+.1f}% (variable).")
            delta += d_rev - d_cogs
    if e != 0:
        opex = v.get("operating_expenses")
        if opex is None or not opex.ok:
            v["net_income"] = InputValue.missing("net_income", "Net income (shocked)",
                                                 "Expense shock cannot be applied: operating expenses are required.")
        else:
            d_opex = opex.value * e  # type: ignore[operator]
            _shift(opex, d_opex, f"Scenario operating expenses {e * 100:+.1f}%.")
            delta -= d_opex
    if delta != 0 and "net_income" in v:
        _shift(v["net_income"], delta, "Scenario flow-through to net income (pre-tax effect, taxes unchanged).")
    t = s.terms
    if adj.get("loan_amount_override") is not None:
        t.requested_amount = to_decimal(adj["loan_amount_override"])
    if adj.get("interest_rate_override") is not None:
        t.interest_rate = to_decimal(adj["interest_rate_override"])
    if adj.get("amortization_months_override"):
        t.amortization_months = int(adj["amortization_months_override"])
    if bps and t.interest_rate is not None:
        t.interest_rate = max(Decimal(0), t.interest_rate + Decimal(bps) / Decimal(10000))
    s.scenario = {k: (str(val) if isinstance(val, Decimal) else val) for k, val in adj.items()}
    return s


def sensitivity_grid(compute, inputs: AnalysisInputs, params: dict, revenue_shocks: list[Decimal],
                     rate_shocks_bps: list[int], expense_shocks: list[Decimal]) -> dict[str, Any]:
    """DSCR under revenue × rate shocks, plus expense shocks. ``compute`` = module.compute_metrics."""

    def _dscr(adj: dict[str, Any]) -> str | None:
        m = compute(apply_scenario(inputs, adj), params)
        key = "dscr_property" if inputs.loan_type == "commercial_real_estate" and m["dscr_property"].computed else "dscr"
        res = m[key]
        return dstr(q_ratio(res.value)) if res.computed else None

    grid = []
    for r in revenue_shocks:
        row = {"revenue_change_pct": dstr(r), "cells": []}
        for bps in rate_shocks_bps:
            row["cells"].append({"rate_change_bps": bps,
                                 "dscr": _dscr({"revenue_change_pct": r, "rate_change_bps": bps})})
        grid.append(row)
    expense = [{"opex_change_pct": dstr(e), "dscr": _dscr({"opex_change_pct": e})} for e in expense_shocks]
    return {
        "metric": "dscr_property" if inputs.loan_type == "commercial_real_estate" else "dscr",
        "rate_shocks_bps": list(rate_shocks_bps),
        "revenue_rows": grid,
        "expense_rows": expense,
        "assumptions": [
            "COGS moves with revenue; all other costs are fixed.",
            "Rate shocks apply to the proposed loan only; existing debt payments are unchanged.",
            "Income-tax effects of shocks are ignored.",
        ],
    }
