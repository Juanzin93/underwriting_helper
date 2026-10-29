"""Commercial real estate metrics beyond NOI / property DSCR / debt yield.

Uses the same builder as the commercial metrics: explicit formulas, Decimal arithmetic, provenance on every input,
no zero-filling. Policy parameters (cap rate, stress) are labelled assumptions. Rent-roll aggregates come from a
reconciled rent roll (uwhelper.cre.rent_roll) and carry its verification status.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from uwhelper.metrics.finance import annual_debt_service
from uwhelper.metrics.types import AnalysisInputs, InputValue

if TYPE_CHECKING:  # pragma: no cover
    from uwhelper.metrics.commercial import _Builder

CRE_METRICS = ("dscr_property_stressed", "breakeven_occupancy", "income_approach_value", "ltv_income_value",
               "occupancy_physical", "occupancy_economic", "rollover_12m", "rollover_24m", "walt_years")
RENT_ROLL_METRICS = ("occupancy_physical", "occupancy_economic", "rollover_12m", "rollover_24m", "walt_years")


def _param(params: dict, key: str, label: str, note: str) -> InputValue:
    raw = params.get(key)
    if raw is None:
        return InputValue.missing(key, label, f"Policy parameter '{key}' is not set.")
    return InputValue.assumed(key, label, Decimal(str(raw)), note, {"policy_parameter"})


def add_cre_metrics(b: _Builder, inputs: AnalysisInputs, params: dict, *, noi_in: InputValue,
                    proposed_in: InputValue, amt_in: InputValue, rate_in: InputValue) -> None:
    from uwhelper.metrics.commercial import _ratio, _terms_input

    t = inputs.terms
    v = inputs.get

    # Stressed DSCR: same structure, higher rate.
    stress = _param(params, "cre_stress_rate_bps", "Policy rate stress (bps)", "Rate shock defined by the policy.")
    if t.repayment_type == "interest_only":
        stressed_ds = lambda a, r, s: annual_debt_service(a, r + s / Decimal(10000), None, "interest_only")
        ds_inputs = [amt_in, rate_in, stress]
    else:
        amort_in = _terms_input("amortization_months", "Amortization (months)", t.amortization_months)
        stressed_ds = lambda a, r, s, n: annual_debt_service(a, r + s / Decimal(10000), int(n), "amortizing")
        ds_inputs = [amt_in, rate_in, stress, amort_in]
    b.add("dscr_property_stressed", [noi_in, *ds_inputs],
          lambda n, *xs: _ratio(n, stressed_ds(*xs), "Stressed debt service"))

    # Break-even occupancy.
    b.add("breakeven_occupancy",
          [v("property_operating_expenses", "Property operating expenses"), proposed_in,
           v("gross_potential_rent", "Gross potential rent"), v("other_property_income", "Other property income")],
          lambda opex, ds, gpr, other: _ratio(opex + ds, gpr + other, "Gross potential rent + other income"))

    # Income-approach cross-check.
    cap = _param(params, "cre_cap_rate", "Policy capitalization rate", "Illustrative cap rate defined by the policy.")
    value = b.add("income_approach_value", [noi_in, cap], lambda n, c: _ratio(n, c, "Capitalization rate"),
                  note="A policy-rate cross-check, not an appraisal.")
    b.add("ltv_income_value", [amt_in, value.as_input("Income-approach value")],
          lambda a, val: _ratio(a, val, "Income-approach value"))

    # Rent roll.
    rr = inputs.rent_roll
    if rr is None:
        for k in RENT_ROLL_METRICS:
            b.add(k, [InputValue.missing("rent_roll", "Rent roll",
                                         "No usable rent roll on this file (verified-only runs need a verified one)."), ], lambda x: x)
        return
    g = rr.get
    in_place = g("in_place_rent", "In-place annual rent")
    b.add("occupancy_physical", [g("units_occupied", "Occupied units"), g("units_total", "Total units")],
          lambda o, n: _ratio(o, n, "Total units"))
    b.add("occupancy_economic", [in_place, g("vacant_market_rent", "Market rent of vacant units")],
          lambda r, m: _ratio(r, r + m, "Potential rent"))
    b.add("rollover_12m", [g("rent_expiring_12m", "Rent expiring within 12 months"), in_place],
          lambda e, r: _ratio(e, r, "In-place rent"))
    b.add("rollover_24m", [g("rent_expiring_24m", "Rent expiring within 24 months"), in_place],
          lambda e, r: _ratio(e, r, "In-place rent"))
    b.add("walt_years", [g("walt_weighted", "Σ rent × remaining years"), g("walt_rent", "Rent with a known lease end")],
          lambda w, r: _ratio(w, r, "Rent with a known lease end"))
