"""Pure, deterministic loan math (Decimal only)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from uwhelper.domain.money import q_money

PERIODS_PER_YEAR = {"monthly": 12, "quarterly": 4, "semiannual": 2, "annual": 1}


class LoanMathError(ValueError):
    pass


def monthly_payment(principal: Decimal, annual_rate: Decimal, amortization_months: int) -> Decimal:
    """Level monthly payment for a fully amortizing loan (unrounded).

    payment = P * r / (1 - (1 + r) ** -n), with r = annual_rate / 12.
    A 0% rate gives P / n.
    """
    if principal is None or annual_rate is None or amortization_months is None:
        raise LoanMathError("principal, rate and amortization are required")
    if principal < 0:
        raise LoanMathError("principal must not be negative")
    if annual_rate < 0:
        raise LoanMathError("interest rate must not be negative")
    if annual_rate >= 1:
        raise LoanMathError("interest rate must be an annual decimal (e.g. 0.0825), not a percent")
    if amortization_months <= 0:
        raise LoanMathError("amortization months must be positive")
    if principal == 0:
        return Decimal(0)
    with localcontext() as ctx:
        ctx.prec = 34
        n = Decimal(amortization_months)
        if annual_rate == 0:
            return principal / n
        r = annual_rate / Decimal(12)
        factor = (Decimal(1) + r) ** -amortization_months
        return principal * r / (Decimal(1) - factor)


def annual_debt_service(
    principal: Decimal,
    annual_rate: Decimal,
    amortization_months: int | None,
    repayment_type: str = "amortizing",
) -> Decimal:
    """Annual debt service for the proposed loan (unrounded)."""
    if repayment_type == "interest_only":
        if principal is None or annual_rate is None:
            raise LoanMathError("principal and rate are required")
        if annual_rate < 0 or annual_rate >= 1:
            raise LoanMathError("interest rate must be an annual decimal between 0 and 1")
        return principal * annual_rate
    if repayment_type != "amortizing":
        raise LoanMathError(f"unsupported repayment type {repayment_type!r}")
    return monthly_payment(principal, annual_rate, amortization_months) * 12  # type: ignore[arg-type]


def annualize_payment(amount: Decimal, frequency: str) -> Decimal:
    if frequency not in PERIODS_PER_YEAR:
        raise LoanMathError(f"unknown payment frequency {frequency!r}")
    if amount < 0:
        raise LoanMathError("payment amount must not be negative")
    return amount * PERIODS_PER_YEAR[frequency]


@dataclass(frozen=True)
class ScheduleRow:
    month: int
    payment: Decimal
    interest: Decimal
    principal: Decimal
    balance: Decimal


def amortization_schedule(
    principal: Decimal,
    annual_rate: Decimal,
    amortization_months: int,
    term_months: int | None = None,
    repayment_type: str = "amortizing",
) -> list[ScheduleRow]:
    """Monthly schedule rounded to cents; a balloon is paid at maturity if term < amortization."""
    term = term_months or amortization_months
    if term <= 0:
        raise LoanMathError("term must be positive")
    r = annual_rate / Decimal(12)
    rows: list[ScheduleRow] = []
    balance = principal
    if repayment_type == "interest_only":
        pay = q_money(principal * r)
        for m in range(1, term + 1):
            interest = q_money(balance * r)
            prin = balance if m == term else Decimal(0)
            rows.append(ScheduleRow(m, q_money(interest + prin), interest, q_money(prin), q_money(balance - prin)))
            balance -= prin
        return rows
    pay = q_money(monthly_payment(principal, annual_rate, amortization_months))
    for m in range(1, term + 1):
        interest = q_money(balance * r)
        prin = pay - interest
        if m == term or prin > balance:
            prin = balance  # final payment / balloon clears the balance
        balance = q_money(balance - prin)
        rows.append(ScheduleRow(m, q_money(interest + prin), interest, q_money(prin), balance))
        if balance <= 0:
            break
    return rows
