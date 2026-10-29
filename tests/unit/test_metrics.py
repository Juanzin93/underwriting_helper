from decimal import Decimal as D

from tests.conftest import base_inputs, iv
from uwhelper.metrics.types import (
    CollateralInput,
    DebtInput,
    GuarantorInput,
    InputValue,
)
from uwhelper.modules import get_module

M = get_module("commercial")
PARAMS = {"collateral_advance_rates": {"equipment": D("0.6"), "real_estate": D("0.75")}}


def run(inputs):
    return M.compute_metrics(inputs, PARAMS)


def test_known_answer_dscr_and_components():
    m = run(base_inputs())
    assert m["ebitda"].value == D("360000.00")  # 230k + 40k + 30k + 60k + 0
    assert m["cfads"].value == D("255000.00")  # 360k - 25k - 30k - 50k
    assert m["proposed_debt_service"].value == D("95018.91")
    assert m["dscr"].value == D("2.683676")
    assert "CFADS" in m["dscr"].formula and m["dscr"].computed


def test_every_catalog_metric_present_once():
    assert set(run(base_inputs())) == set(M.metric_catalog())


def test_missing_input_means_not_computed_not_zero():
    m = run(base_inputs(amortization=None))
    assert m["ebitda"].status == "not_computable" and m["ebitda"].value is None
    assert "Amortization: missing" in m["ebitda"].explanation
    assert m["dscr"].status == "not_computable"
    # Unrelated metrics still compute.
    assert m["current_ratio"].computed


def test_conflicting_input_blocks_metric():
    conflict = InputValue("revenue", "Revenue", None, "conflict", False, {"conflict"}, note="Sources disagree")
    m = run(base_inputs(revenue=conflict))
    assert m["revenue"].status == "not_computable" and "contradiction" in m["revenue"].explanation
    assert m["gross_margin"].status == "not_computable"


def test_zero_and_negative_values():
    m = run(base_inputs(revenue=0))
    assert m["gross_margin"].status == "not_meaningful"
    m = run(base_inputs(net_income=-500000))  # loss: CFADS negative -> DSCR negative but computed
    assert m["cfads"].value < 0 and m["dscr"].computed and m["dscr"].value < 0
    m = run(base_inputs(total_liabilities=1500000))  # negative equity
    assert m["total_equity"].value == D("-100000.00") and m["debt_to_equity"].status == "not_meaningful"
    m = run(base_inputs(current_liabilities=0))
    assert m["current_ratio"].status == "not_meaningful"
    m = run(base_inputs(net_income=-400000))
    assert m["debt_to_ebitda"].status == "not_meaningful"


def test_debt_schedule_must_be_confirmed():
    i = base_inputs()
    i.debt_schedule_confirmed = False
    m = run(i)
    assert m["existing_debt_service"].status == "not_computable" and m["dscr"].status == "not_computable"


def test_existing_debts_and_refinance_exclusion():
    i = base_inputs()
    i.debts = [DebtInput("d1", "Bank A", iv("b", 100000), iv("p", 24000), False),
               DebtInput("d2", "Bank B", iv("b", 50000), iv("p", 12000), True)]
    m = run(i)
    assert m["existing_debt_service"].value == D("24000.00")
    assert m["funded_debt_pro_forma"].value == D("600000.00")
    assert m["total_debt_service"].value == D("119018.91")


def test_unverified_flag_propagates():
    m = run(base_inputs(net_income=iv("net_income", 230000, verified=False)))
    assert "unverified" in m["dscr"].flags and not m["dscr"].verified


def test_ltv_ltc_and_collateral_coverage():
    i = base_inputs()
    i.collateral = [CollateralInput("c", "CNC", "equipment", iv("v", 625000), "2026-01-01", "appraisal", D(0))]
    m = run(i)
    assert m["ltv"].value == D("0.800000") and m["ltc"].value == D("0.800000")
    assert m["discounted_collateral_value"].value == D("375000.00")
    assert m["collateral_coverage"].value == D("0.750000")
    i.collateral[0].prior_lien_balance = D(100000)
    m = run(i)
    assert m["cltv"].value == D("0.960000")
    assert m["discounted_collateral_value"].value == D("275000.00")


def test_collateral_missing_rate_and_unsecured():
    i = base_inputs()
    i.collateral = [CollateralInput("c", "Art", "other", iv("v", 1000), None, None, D(0))]
    assert run(i)["discounted_collateral_value"].status == "not_computable"
    i.terms.is_secured = False
    assert run(i)["ltv"].status == "not_applicable"


def test_global_dscr():
    i = base_inputs()
    i.guarantors = [GuarantorInput("g", "G", iv("i", 100000), iv("l", 60000), iv("d", 20000))]
    m = run(i)
    assert m["global_cash_flow"].value == D("295000.00")
    assert m["global_debt_service"].value == D("115018.91")
    assert m["global_dscr"].computed


def test_interest_only_and_variable_rate():
    i = base_inputs()
    i.terms.repayment_type, i.terms.rate_type = "interest_only", "variable"
    m = run(i)
    assert m["proposed_debt_service"].value == D("42500.00") and "variable_rate" in m["proposed_debt_service"].flags


def test_breakeven():
    m = run(base_inputs())
    # cushion = (255000 - 95018.91) / 0.4 = 399952.73 -> breakeven = 2,400,000 - 399,952.73
    assert m["breakeven_revenue"].value == D("2000047.28")


def test_cre_noi_and_debt_yield():
    i = base_inputs("commercial_real_estate", gross_potential_rent=200000, vacancy_credit_loss=10000,
                    other_property_income=5000, property_operating_expenses=60000)
    m = run(i)
    assert m["noi"].value == D("135000.00") and m["debt_yield"].value == D("0.270000")
    assert m["dscr_property"].computed
    assert run(base_inputs())["noi"].status == "not_applicable"


def test_reported_noi_fallback_is_flagged():
    i = base_inputs("commercial_real_estate", net_operating_income_reported=120000)
    m = run(i)
    assert m["noi"].value == D("120000.00") and "reported_not_recomputed" in m["noi"].flags


def test_deterministic_reproducibility():
    a = {k: v.to_dict() for k, v in run(base_inputs()).items()}
    b = {k: v.to_dict() for k, v in run(base_inputs()).items()}
    assert a == b
