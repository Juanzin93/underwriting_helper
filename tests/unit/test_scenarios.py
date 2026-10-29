from decimal import Decimal as D

from tests.conftest import base_inputs
from uwhelper.analysis.scenarios import apply_scenario, sensitivity_grid
from uwhelper.metrics.types import InputValue
from uwhelper.modules import get_module

M = get_module("commercial")


def test_scenario_does_not_mutate_base():
    base = base_inputs()
    before = base.to_dict()
    s = apply_scenario(base, {"revenue_change_pct": "-0.10", "rate_change_bps": 200})
    assert base.to_dict() == before
    assert s.values["revenue"].value == D(2160000) and s.terms.interest_rate == D("0.105")
    # Δ = -240,000 revenue + 144,000 COGS = -96,000 flows to net income
    assert s.values["net_income"].value == D(134000)
    assert "scenario_adjusted" in s.values["net_income"].flags


def test_revenue_shock_needs_cogs():
    s = apply_scenario(base_inputs(cost_of_goods_sold=None), {"revenue_change_pct": "-0.10"})
    assert M.compute_metrics(s, {})["dscr"].status == "not_computable"
    conflict = InputValue("revenue", "Revenue", None, "conflict", False, {"conflict"})
    s = apply_scenario(base_inputs(revenue=conflict), {"revenue_change_pct": "-0.10"})
    assert M.compute_metrics(s, {})["dscr"].status == "not_computable"


def test_expense_shock_and_overrides():
    s = apply_scenario(base_inputs(), {"opex_change_pct": "0.10", "loan_amount_override": "400000",
                                       "amortization_months_override": 120})
    assert s.values["net_income"].value == D(170000)
    assert s.terms.requested_amount == D(400000) and s.terms.amortization_months == 120


def test_sensitivity_grid_monotonic():
    g = sensitivity_grid(M.compute_metrics, base_inputs(), {}, [D(0), D("-0.1"), D("-0.2")], [0, 200], [D(0), D("0.1")])
    vals = [D(c["dscr"]) for row in g["revenue_rows"] for c in row["cells"]]
    assert vals[0] == D("2.683676")
    assert vals[0] > vals[2] > vals[4] and vals[0] > vals[1]
    assert g["expense_rows"][1]["dscr"] is not None and D(g["expense_rows"][1]["dscr"]) < vals[0]
