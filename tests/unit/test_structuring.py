from decimal import Decimal as D

import pytest

from tests.lifecycle_helpers import advance
from uwhelper import structuring as S
from uwhelper.metrics.structuring import (
    RateStep,
    StructureError,
    StructureInputs,
    add_months,
    analyse,
    build_schedule,
    due_dates,
)


def si(**kw):
    base = dict(principal=D(100000), annual_rate=D("0.06"), term_months=60, amortization_months=60)
    base.update(kw)
    return StructureInputs(**base)


def test_level_payment_known_answer_and_exact_payoff():
    a = analyse(si())
    assert a["level_payment"] == D("1933.28")
    rows = a["schedule"]
    assert len(rows) == 60 and rows[-1].balance == 0
    assert sum(r.principal for r in rows) == D(100000)
    assert a["total_payments"] == a["total_interest"] + D(100000)


def test_quarterly_and_annual_frequency():
    q = analyse(si(annual_rate=D("0.08"), payment_frequency="quarterly"))
    assert q["level_payment"] == D("6115.67") and q["number_of_payments"] == 20  # r = 2% per quarter
    y = analyse(si(annual_rate=D("0.08"), payment_frequency="annual"))
    assert y["level_payment"] == D("25045.65") and y["number_of_payments"] == 5


def test_interest_only_period():
    a = analyse(si(principal=D(500000), annual_rate=D("0.08"), term_months=84, amortization_months=72,
                   interest_only_months=12, cfads=D(200000), existing_debt_service=D(0)))
    assert a["interest_only_payment"] == D("3333.33")
    assert all(r.principal == 0 for r in a["schedule"][:12])
    assert a["year1_debt_service"] == D("39999.96")
    assert a["dscr_year1"] > a["dscr_minimum"]  # IO year overstates coverage
    assert a["schedule"][-1].balance == 0 and len(a["schedule"]) == 84


def test_balloon_payment():
    a = analyse(si(principal=D(1000000), annual_rate=D("0.07"), term_months=60, amortization_months=300))
    assert a["level_payment"] == D("7067.79")
    assert a["balloon_payment"] == D("911621.64")
    assert a["schedule"][-1].kind == "balloon" and a["schedule"][-1].balance == 0
    assert a["max_annual_debt_service"] == D("84813.48")  # balloon excluded from annual debt service


def test_variable_rate_steps_reamortize():
    a = analyse(si(principal=D(500000), annual_rate=D("0.08"), rate_type="variable",
                   rate_steps=[RateStep(25, D("0.10"))]))
    rows = a["schedule"]
    assert rows[23].payment == D("10138.20") and rows[24].payment == D("10439.34")
    assert rows[24].annual_rate == D("0.10") and rows[-1].balance == 0


def test_zero_rate_and_rounding_to_cents():
    a = analyse(si(principal=D("1000.00"), annual_rate=D(0), term_months=3, amortization_months=3))
    assert [r.payment for r in a["schedule"]] == [D("333.33"), D("333.33"), D("333.34")]
    for r in analyse(si())["schedule"]:
        assert r.payment == r.payment.quantize(D("0.01")) and r.interest == r.interest.quantize(D("0.01"))


@pytest.mark.parametrize("kw,msg", [
    (dict(principal=D(0)), "greater than zero"), (dict(annual_rate=D("8.5")), "annual decimals"),
    (dict(interest_only_months=60), "shorter than the term"), (dict(term_months=61, payment_frequency="quarterly"), "multiple of 3"),
    (dict(rate_steps=[RateStep(13, D("0.07"))]), "variable-rate"), (dict(origination_fee=D(-1)), "negative"),
])
def test_invalid_structures(kw, msg):
    with pytest.raises(StructureError, match=msg):
        build_schedule(si(**kw))


def test_fees_financed_and_ltv_ltc():
    a = analyse(si(origination_fee=D(1000), other_costs=D(500), fees_financed=True, collateral_value=D(203000),
                   project_cost=D(125000)))
    assert a["financed_principal"] == D(101500) and a["net_proceeds"] == D(100000)
    assert a["ltv"] == D("0.500000") and a["ltc"] == D("0.812000")
    b = analyse(si(origination_fee=D(1000)))
    assert b["financed_principal"] == D(100000) and b["net_proceeds"] == D(99000)


def test_due_dates_clamp_month_end():
    from datetime import date

    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert due_dates(date(2026, 1, 31), 3, "monthly") == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]
    assert due_dates(date(2026, 1, 15), 2, "quarterly")[1] == date(2026, 4, 15)


def test_saved_structures_compare_and_never_touch_terms(conn, settings):
    from uwhelper.repo import cases

    out = advance(conn, settings, "CREDIT_REVIEW")
    cid = out["case_id"]
    before = cases.get_case(conn, cid)["terms"]
    base = S.baseline_from_requested(conn, cid)
    alt = S.create_structure(conn, cid, S.StructureIn(name="Longer amortization", principal="480000", annual_rate="8.25",
                                                       term_months=84, amortization_months=120))
    rows = {r["key"]: r for r in S.compare(S.list_structures(conn, cid))}
    assert rows["level_payment"]["cells"][1]["delta"] is not None and D(rows["level_payment"]["cells"][1]["delta"]) < 0
    alt_s = S.get_structure(conn, cid, alt)
    assert alt_s["results"]["balloon_payment"] != "0" and alt_s["calc"]["run_id"] == out["run_id"]
    assert {c["rule_id"] for c in alt_s["results"]["policy_checks"]} >= {"DSCR_MIN", "LTV_MAX_SECURED"}
    assert any("Balloon" in r for r in alt_s["results"]["risks"])
    assert cases.get_case(conn, cid)["terms"] == before
    with pytest.raises(Exception):
        conn.execute("UPDATE loan_structures SET inputs_json='{}' WHERE id=?", (base,))
    S.set_status(conn, cid, alt, "proposed")
    assert S.get_structure(conn, cid, alt)["status"] == "proposed"


def test_rate_steps_text_parsing():
    s = S.StructureIn(name="v", principal="100000", annual_rate="8", rate_type="variable", term_months=60,
                      amortization_months=60, rate_steps="25:9.5, 49:10")
    assert s.rate_steps == [(25, D("0.095")), (49, D("0.1"))]
