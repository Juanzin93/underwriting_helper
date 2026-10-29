"""Deterministic loan-structure engine (used by the structuring simulator and servicing schedules).

Conventions (documented, tested):
* Nominal annual rate; periodic rate = annual rate / payments per year (no daily accrual, no 365/360).
* Interest each period = round_half_up(balance × periodic rate, cents).
* Level payment = round_half_up(annuity payment, cents) over the REMAINING amortization periods,
  recomputed when the rate changes (variable-rate projection) or when interest-only ends.
* The amortization period starts after any interest-only period.
* If the term ends before the loan amortizes, the remaining balance is a balloon added to the final payment.
* The last amortizing payment is adjusted so the balance reaches exactly 0.00 (no residual cents).
* Term, amortization, interest-only length and rate-step months must align with the payment frequency.
These are planning estimates, not lender quotes: real loans may use actual/360 day counts, rate floors,
caps, payment dates on business days, prepayment terms, etc.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from uwhelper.domain.money import dstr

ENGINE_VERSION = "structuring/1.0.0"
PERIODS = {"monthly": 12, "quarterly": 4, "semiannual": 2, "annual": 1}
CENT = Decimal("0.01")
ZERO = Decimal(0)


class StructureError(ValueError):
    pass


def q(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class RateStep:
    start_month: int  # 1-based loan month from which this annual rate applies
    annual_rate: Decimal


@dataclass
class StructureInputs:
    principal: Decimal
    annual_rate: Decimal
    term_months: int
    amortization_months: int
    interest_only_months: int = 0
    payment_frequency: str = "monthly"
    rate_type: str = "fixed"
    rate_steps: list[RateStep] = field(default_factory=list)
    origination_fee: Decimal = ZERO
    other_costs: Decimal = ZERO
    fees_financed: bool = False
    requested_principal: Decimal | None = None
    cfads: Decimal | None = None
    existing_debt_service: Decimal | None = None
    collateral_value: Decimal | None = None
    project_cost: Decimal | None = None

    @property
    def periods_per_year(self) -> int:
        return PERIODS[self.payment_frequency]

    @property
    def financed_principal(self) -> Decimal:
        fees = self.origination_fee + self.other_costs
        return self.principal + (fees if self.fees_financed else ZERO)


@dataclass
class Installment:
    seq: int
    month_end: int  # loan month in which the payment falls due
    annual_rate: Decimal
    payment: Decimal
    interest: Decimal
    principal: Decimal
    balance: Decimal
    kind: str  # interest_only | amortizing | balloon | final


def _months_to_periods(months: int, p: int, what: str) -> int:
    step = 12 // p
    if months % step:
        raise StructureError(f"{what} ({months} months) must be a multiple of {step} months for this payment frequency.")
    return months // step


def validate(si: StructureInputs) -> None:
    if si.payment_frequency not in PERIODS:
        raise StructureError("Unknown payment frequency.")
    if si.principal is None or si.principal <= 0:
        raise StructureError("Principal must be greater than zero.")
    for r in [si.annual_rate, *[s.annual_rate for s in si.rate_steps]]:
        if r is None or r < 0 or r >= 1:
            raise StructureError("Rates must be annual decimals between 0 and 1 (e.g. 0.0825).")
    if si.term_months <= 0 or si.amortization_months <= 0:
        raise StructureError("Term and amortization must be positive.")
    if si.interest_only_months < 0 or si.interest_only_months >= si.term_months:
        raise StructureError("Interest-only period must be shorter than the term.")
    if si.origination_fee < 0 or si.other_costs < 0:
        raise StructureError("Fees cannot be negative.")
    p = si.periods_per_year
    _months_to_periods(si.term_months, p, "Term")
    _months_to_periods(si.amortization_months, p, "Amortization")
    _months_to_periods(si.interest_only_months, p, "Interest-only period")
    for s in si.rate_steps:
        if s.start_month < 1 or s.start_month > si.term_months:
            raise StructureError("Rate step months must fall within the term.")
        if (s.start_month - 1) % (12 // p):
            raise StructureError("Rate steps must start on a payment-period boundary.")
    if si.rate_type == "fixed" and si.rate_steps:
        raise StructureError("Rate steps are only allowed for variable-rate structures.")


def _annuity(balance: Decimal, r: Decimal, n: int) -> Decimal:
    if n <= 0:
        return balance
    if r == 0:
        return balance / n
    with localcontext() as ctx:
        ctx.prec = 34
        return balance * r / (1 - (1 + r) ** -n)


def build_schedule(si: StructureInputs) -> list[Installment]:
    validate(si)
    p = si.periods_per_year
    step = 12 // p
    n_term = si.term_months // step
    n_io = si.interest_only_months // step
    n_am = si.amortization_months // step
    steps = sorted(si.rate_steps, key=lambda s: s.start_month)

    def annual_rate_for(k: int) -> Decimal:
        month = (k - 1) * step + 1
        rate = si.annual_rate
        for s in steps:
            if s.start_month <= month:
                rate = s.annual_rate
        return rate

    balance = si.financed_principal
    rows: list[Installment] = []
    level: Decimal | None = None
    prev_rate: Decimal | None = None
    for k in range(1, n_term + 1):
        ar = annual_rate_for(k)
        r = ar / p
        interest = q(balance * r)
        amort_index = k - n_io  # 1-based index within the amortizing phase
        if k <= n_io:
            principal, kind = ZERO, "interest_only"
        else:
            remaining = n_am - amort_index + 1
            if level is None or ar != prev_rate:
                level = q(_annuity(balance, r, remaining))
            principal = level - interest
            kind = "amortizing"
            if remaining <= 1 or principal >= balance:
                principal, kind = balance, "final"
        prev_rate = ar
        if k == n_term and balance - principal > 0:
            principal, kind = balance, "balloon"
        if principal < 0:  # cannot happen with level annuities; guard anyway
            raise StructureError("Payment does not cover interest (negative amortization).")
        balance = balance - principal
        rows.append(Installment(k, k * step, ar, interest + principal, interest, principal, balance, kind))
        if balance == 0:
            break
    return rows


def _sum(xs) -> Decimal:
    return sum(xs, ZERO)


def analyse(si: StructureInputs) -> dict[str, Any]:
    rows = build_schedule(si)
    p = si.periods_per_year
    balloon_row = rows[-1] if rows and rows[-1].kind == "balloon" else None
    balloon = ZERO
    if balloon_row:
        # The balloon is the principal beyond the regular level payment in the final period.
        prior_level = next((r.payment for r in reversed(rows[:-1]) if r.kind == "amortizing"), None)
        regular_principal = (prior_level - balloon_row.interest) if prior_level is not None else ZERO
        balloon = balloon_row.principal - max(ZERO, min(regular_principal, balloon_row.principal))
    io_payment = next((r.payment for r in rows if r.kind == "interest_only"), None)
    level_payment = next((r.payment for r in rows if r.kind == "amortizing"), None)
    if level_payment is None:
        level_payment = next((r.payment for r in rows if r.kind in ("final", "balloon") and r.month_end > si.interest_only_months), None)

    # Debt service by loan year, excluding the balloon amount (a refinancing event, not periodic service).
    years: dict[int, Decimal] = {}
    for r in rows:
        y = (r.month_end - 1) // 12 + 1
        pay = r.payment - (balloon if r is balloon_row else ZERO)
        years[y] = years.get(y, ZERO) + pay
    full_years = {y: v for y, v in years.items() if y * 12 <= si.term_months}
    year1 = years.get(1, ZERO)
    max_annual = max(full_years.values()) if full_years else year1
    steady = level_payment * p if level_payment is not None else None

    fees = si.origination_fee + si.other_costs
    out: dict[str, Any] = {
        "engine_version": ENGINE_VERSION,
        "financed_principal": si.financed_principal,
        "periods_per_year": p,
        "number_of_payments": len(rows),
        "interest_only_payment": io_payment,
        "level_payment": level_payment,
        "total_payments": _sum(r.payment for r in rows),
        "total_interest": _sum(r.interest for r in rows),
        "balloon_payment": balloon,
        "balance_before_balloon": (balloon_row.principal if balloon_row else ZERO),
        "year1_debt_service": year1,
        "steady_state_annual_debt_service": steady,
        "max_annual_debt_service": max_annual,
        "annual_debt_service_by_year": {str(k): v for k, v in sorted(years.items())},
        "total_fees": fees,
        "net_proceeds": si.principal - (ZERO if si.fees_financed else fees),
        "balance_after_12_months": balance_after(rows, 12, si.financed_principal),
        "balance_after_60_months": balance_after(rows, 60, si.financed_principal),
    }
    eds = si.existing_debt_service
    for key, ds in (("dscr_year1", year1), ("dscr_amortizing", steady), ("dscr_minimum", max_annual)):
        out[key] = (si.cfads / (eds + ds)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP) \
            if si.cfads is not None and eds is not None and ds and (eds + ds) > 0 else None
    out["ltv"] = (si.financed_principal / si.collateral_value).quantize(Decimal("0.000001")) \
        if si.collateral_value and si.collateral_value > 0 else None
    out["ltc"] = (si.financed_principal / si.project_cost).quantize(Decimal("0.000001")) \
        if si.project_cost and si.project_cost > 0 else None
    out["schedule"] = rows
    return out


def balance_after(rows: list[Installment], months: int, start: Decimal) -> Decimal:
    bal = start
    for r in rows:
        if r.month_end <= months:
            bal = r.balance
    return bal


def shocked(si: StructureInputs, rate_bps: int = 0, cfads_pct: Decimal = ZERO) -> StructureInputs:
    from dataclasses import replace

    add = Decimal(rate_bps) / Decimal(10000)
    return replace(
        si,
        annual_rate=max(ZERO, si.annual_rate + add),
        rate_steps=[RateStep(s.start_month, max(ZERO, s.annual_rate + add)) for s in si.rate_steps],
        cfads=None if si.cfads is None else si.cfads * (1 + cfads_pct),
    )


def sensitivity(si: StructureInputs, rate_shocks_bps=(0, 100, 200, 300),
                cfads_changes=(Decimal("0"), Decimal("-0.10"), Decimal("-0.20"))) -> dict[str, Any]:
    rows = []
    for c in cfads_changes:
        cells = []
        for b in rate_shocks_bps:
            a = analyse(shocked(si, b, c))
            cells.append({"rate_bps": b, "dscr_minimum": dstr(a["dscr_minimum"]),
                          "max_annual_debt_service": dstr(a["max_annual_debt_service"])})
        rows.append({"cfads_change": dstr(c), "cells": cells})
    return {"rate_shocks_bps": list(rate_shocks_bps), "rows": rows,
            "note": "Rate shocks are added to every period's rate (including fixed-rate structures, as a what-if "
                    "on the pricing). CFADS changes scale the cash flow available for debt service."}


def serialise(result: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in result.items():
        if k == "schedule":
            out[k] = [{"seq": r.seq, "month": r.month_end, "rate": dstr(r.annual_rate), "payment": dstr(r.payment),
                       "interest": dstr(r.interest), "principal": dstr(r.principal), "balance": dstr(r.balance),
                       "kind": r.kind} for r in v]
        elif isinstance(v, Decimal):
            out[k] = dstr(v)
        elif isinstance(v, dict):
            out[k] = {kk: dstr(vv) if isinstance(vv, Decimal) else vv for kk, vv in v.items()}
        else:
            out[k] = v
    return out


def add_months(d: date, months: int) -> date:
    """Calendar month addition, clamping to month end (Jan 31 + 1 month = Feb 28/29)."""
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def due_dates(first_payment: date, count: int, frequency: str) -> list[date]:
    step = 12 // PERIODS[frequency]
    return [add_months(first_payment, i * step) for i in range(count)]
