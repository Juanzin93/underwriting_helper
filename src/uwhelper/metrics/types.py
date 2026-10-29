"""Value objects passed between fact resolution, metrics and the policy engine."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from uwhelper.domain.money import dstr

InputState = Literal["ok", "missing", "conflict", "not_meaningful"]
MetricStatus = Literal["computed", "not_computable", "not_meaningful", "not_applicable"]

# Flags that make a value less than fully reliable. The policy engine never
# lets a rule PASS when an input carries one of these.
QUALITY_FLAGS = {"unverified", "annualized", "stale", "estimate", "reported_not_recomputed", "period_mismatch"}


@dataclass
class InputValue:
    name: str
    label: str
    value: Decimal | None = None
    state: InputState = "missing"
    verified: bool = False
    flags: set[str] = field(default_factory=set)
    fact_ids: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.state == "ok" and self.value is not None

    @classmethod
    def missing(cls, name: str, label: str, note: str = "") -> "InputValue":
        return cls(name=name, label=label, state="missing", note=note or "No value available.")

    @classmethod
    def assumed(cls, name: str, label: str, value: Decimal, note: str, flags: set[str] | None = None) -> "InputValue":
        """A user-entered structural assumption (e.g. proposed loan terms)."""
        return cls(name=name, label=label, value=value, state="ok", verified=True,
                   flags=set(flags or {"proposed_terms"}), note=note)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "value": dstr(self.value),
            "state": self.state,
            "verified": self.verified,
            "flags": sorted(self.flags),
            "fact_ids": list(self.fact_ids),
            "sources": list(self.sources),
            "note": self.note,
        }


@dataclass
class MetricResult:
    key: str
    label: str
    unit: str
    status: MetricStatus
    value: Decimal | None
    formula: str
    inputs: list[InputValue] = field(default_factory=list)
    explanation: str = ""
    flags: set[str] = field(default_factory=set)

    @property
    def computed(self) -> bool:
        return self.status == "computed" and self.value is not None

    @property
    def verified(self) -> bool:
        return all(i.verified for i in self.inputs)

    def as_input(self, label: str | None = None) -> InputValue:
        """Use this metric as an input to another metric, carrying provenance."""
        fact_ids: list[str] = []
        sources: list[dict[str, Any]] = []
        for i in self.inputs:
            fact_ids.extend(x for x in i.fact_ids if x not in fact_ids)
            sources.extend(s for s in i.sources if s not in sources)
        state: InputState
        if self.status == "computed":
            state = "ok"
        elif self.status == "not_meaningful":
            state = "not_meaningful"
        else:
            state = "missing"
        return InputValue(
            name=self.key, label=label or self.label, value=self.value if state == "ok" else None,
            state=state, verified=self.verified, flags=set(self.flags) | {"derived"},
            fact_ids=fact_ids, sources=sources,
            note=("not meaningful" if state == "not_meaningful" else "not calculated — see that metric")
            if state != "ok" else f"Derived: {self.formula}",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "unit": self.unit,
            "status": self.status,
            "value": dstr(self.value),
            "formula": self.formula,
            "inputs": [i.to_dict() for i in self.inputs],
            "explanation": self.explanation,
            "flags": sorted(self.flags),
        }


@dataclass
class DebtInput:
    id: str
    creditor: str
    balance: InputValue
    annual_payment: InputValue
    to_be_refinanced: bool
    interest_rate: Decimal | None = None
    maturity_date: str | None = None


@dataclass
class CollateralInput:
    id: str
    description: str
    collateral_type: str
    value: InputValue
    valuation_date: str | None
    valuation_method: str | None
    prior_lien_balance: Decimal
    lien_position: int = 1


@dataclass
class GuarantorInput:
    id: str
    name: str
    personal_income: InputValue
    living_expenses: InputValue
    personal_debt_service: InputValue


@dataclass
class LoanTermsInput:
    requested_amount: Decimal | None = None
    interest_rate: Decimal | None = None  # annual, decimal (0.0825 = 8.25%)
    term_months: int | None = None
    amortization_months: int | None = None
    repayment_type: str = "amortizing"
    rate_type: str = "fixed"
    project_total_cost: Decimal | None = None
    is_secured: bool = True
    loan_purpose: str | None = None


@dataclass
class AnalysisInputs:
    loan_type: str
    period_label: str | None = None
    period_end: str | None = None
    period_months: int | None = None
    balance_sheet_label: str | None = None
    values: dict[str, InputValue] = field(default_factory=dict)
    adjustments: list[InputValue] = field(default_factory=list)
    excluded_adjustments: list[InputValue] = field(default_factory=list)
    debts: list[DebtInput] = field(default_factory=list)
    debt_schedule_confirmed: bool = False
    collateral: list[CollateralInput] = field(default_factory=list)
    guarantors: list[GuarantorInput] = field(default_factory=list)
    terms: LoanTermsInput = field(default_factory=LoanTermsInput)
    scenario: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def get(self, key: str, label: str | None = None) -> InputValue:
        iv = self.values.get(key)
        if iv is None:
            return InputValue.missing(key, label or key)
        return iv

    def clone(self) -> "AnalysisInputs":
        return copy.deepcopy(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "loan_type": self.loan_type,
            "period_label": self.period_label,
            "period_end": self.period_end,
            "period_months": self.period_months,
            "balance_sheet_label": self.balance_sheet_label,
            "values": {k: v.to_dict() for k, v in sorted(self.values.items())},
            "adjustments": [a.to_dict() for a in self.adjustments],
            "excluded_adjustments": [a.to_dict() for a in self.excluded_adjustments],
            "debts": [
                {"id": d.id, "creditor": d.creditor, "balance": d.balance.to_dict(),
                 "annual_payment": d.annual_payment.to_dict(), "to_be_refinanced": d.to_be_refinanced,
                 "interest_rate": dstr(d.interest_rate), "maturity_date": d.maturity_date}
                for d in self.debts
            ],
            "debt_schedule_confirmed": self.debt_schedule_confirmed,
            "collateral": [
                {"id": c.id, "description": c.description, "collateral_type": c.collateral_type,
                 "value": c.value.to_dict(), "valuation_date": c.valuation_date,
                 "valuation_method": c.valuation_method, "prior_lien_balance": dstr(c.prior_lien_balance),
                 "lien_position": c.lien_position}
                for c in self.collateral
            ],
            "guarantors": [
                {"id": g.id, "name": g.name, "personal_income": g.personal_income.to_dict(),
                 "living_expenses": g.living_expenses.to_dict(),
                 "personal_debt_service": g.personal_debt_service.to_dict()}
                for g in self.guarantors
            ],
            "terms": {
                "requested_amount": dstr(self.terms.requested_amount),
                "interest_rate": dstr(self.terms.interest_rate),
                "term_months": self.terms.term_months,
                "amortization_months": self.terms.amortization_months,
                "repayment_type": self.terms.repayment_type,
                "rate_type": self.terms.rate_type,
                "project_total_cost": dstr(self.terms.project_total_cost),
                "is_secured": self.terms.is_secured,
                "loan_purpose": self.terms.loan_purpose,
            },
            "scenario": self.scenario,
            "notes": list(self.notes),
        }
