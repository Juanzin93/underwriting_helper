from decimal import Decimal as D

import pytest

from uwhelper.domain.money import InvalidAmount, apply_scale, fmt_money, fmt_pct, fmt_ratio, round_to, to_decimal
from uwhelper.metrics.finance import (
    LoanMathError,
    amortization_schedule,
    annual_debt_service,
    annualize_payment,
    monthly_payment,
)


@pytest.mark.parametrize("raw,expected", [
    ("1,234.50", D("1234.50")), ("$1,234", D("1234")), ("(12,500)", D("-12500")), ("-7", D("-7")),
    ("12.5%", D("0.125")), (" 1 000 ", D("1000")), ("500-", D("-500")), (0, D(0)), (1.1, D("1.1")),
])
def test_to_decimal_parses_accounting_formats(raw, expected):
    assert to_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "1.2.3", "NaN", "Infinity", "1e5x", "--5"])
def test_to_decimal_rejects_invalid(raw):
    with pytest.raises(InvalidAmount):
        to_decimal(raw)


def test_to_decimal_none_and_required():
    assert to_decimal(None) is None and to_decimal("") is None
    with pytest.raises(InvalidAmount):
        to_decimal("", allow_none=False)
    with pytest.raises(InvalidAmount):
        to_decimal(True)


def test_scale_and_rounding():
    assert apply_scale(D("1,250".replace(",", "")), "thousands") == D("1250000")
    assert apply_scale(D("2.5"), "millions") == D("2500000")
    with pytest.raises(InvalidAmount):
        apply_scale(D(1), "dozens")
    assert round_to(D("1.245"), 2) == D("1.25")  # half-up, not banker's rounding
    assert round_to(D("1.2449"), 2) == D("1.24")
    assert fmt_money(D("-1234.5")) == "-$1,235" and fmt_ratio(D("1.2549")) == "1.25x" and fmt_pct(D("0.8")) == "80.0%"


def test_monthly_payment_known_answer():
    # $500,000, 8.5%, 84 months -> $7,918.24 (standard annuity formula)
    assert monthly_payment(D(500000), D("0.085"), 84).quantize(D("0.01")) == D("7918.24")
    assert annual_debt_service(D(500000), D("0.085"), 84).quantize(D("0.01")) == D("95018.91")


def test_zero_rate_and_interest_only():
    assert monthly_payment(D(120000), D(0), 120) == D(1000)
    assert annual_debt_service(D(1000000), D("0.07"), None, "interest_only") == D(70000)
    assert monthly_payment(D(0), D("0.05"), 60) == 0


@pytest.mark.parametrize("args", [(D(-1), D("0.05"), 60), (D(1000), D("-0.01"), 60), (D(1000), D("0.05"), 0),
                                  (D(1000), D("8.5"), 60)])
def test_loan_math_rejects_invalid(args):
    with pytest.raises(LoanMathError):
        monthly_payment(*args)


def test_annualize_payment_frequencies():
    assert annualize_payment(D(1000), "monthly") == D(12000)
    assert annualize_payment(D(1000), "quarterly") == D(4000)
    assert annualize_payment(D(1000), "semiannual") == D(2000)
    assert annualize_payment(D(1000), "annual") == D(1000)
    with pytest.raises(LoanMathError):
        annualize_payment(D(1000), "weekly")
    with pytest.raises(LoanMathError):
        annualize_payment(D(-1), "monthly")


def test_schedule_fully_amortizes_and_balloon():
    rows = amortization_schedule(D(100000), D("0.06"), 60)
    assert len(rows) == 60 and rows[-1].balance == 0
    assert sum(r.principal for r in rows) == D(100000)
    balloon = amortization_schedule(D(100000), D("0.06"), 300, term_months=60)
    assert len(balloon) == 60 and balloon[-1].balance == 0 and balloon[-1].principal > 80000
    io = amortization_schedule(D(100000), D("0.06"), 12, 12, "interest_only")
    assert io[0].payment == D("500.00") and io[-1].principal == D(100000)
