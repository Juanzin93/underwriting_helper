"""Pydantic schema for versioned, validated policy files (YAML/JSON)."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Operator = Literal[">=", ">", "<=", "<", "==", "!="]
CondOperator = Literal[">=", ">", "<=", "<", "==", "!=", "in", "not_in", "is_known", "is_unknown"]
Severity = Literal["info", "low", "medium", "high", "critical"]
RuleType = Literal[
    "metric_threshold",
    "inputs_present",
    "documents_present",
    "valuation_age",
    "statement_age",
    "no_contradictions",
    "guarantor_present",
    "review_flag",
]
ReviewTrigger = Literal["unverified", "annualized", "stale", "estimate", "reported_not_recomputed", "period_mismatch"]

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_SUBJECT = re.compile(r"^(metric|input|terms|case):[a-z0-9_]+$")


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(description="metric:<key> | input:<field> | terms:<field> | case:<loan_type|is_secured>")
    op: CondOperator
    value: Any = None

    @field_validator("subject")
    @classmethod
    def _subject(cls, v: str) -> str:
        if not _SUBJECT.match(v):
            raise ValueError(f"invalid condition subject {v!r}")
        return v


class RuleException(BaseModel):
    """A documented exception. It may only soften FAIL into REVIEW_REQUIRED, never into PASS."""

    model_config = ConfigDict(extra="forbid")
    id: str
    when: list[Condition] = Field(min_length=1)
    result: Literal["REVIEW_REQUIRED"] = "REVIEW_REQUIRED"
    explanation: str


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Z][A-Z0-9_.\-]{1,63}$")
    version: int = Field(ge=1)
    description: str
    type: RuleType
    applies_to: list[str] | Literal["all"] = "all"
    metric: str | None = None
    operator: Operator | None = None
    threshold: Decimal | None = None
    round_to: int | None = Field(default=None, ge=0, le=6)
    required_inputs: list[str] = Field(default_factory=list)
    documents: list[str] = Field(default_factory=list)
    max_age_days: int | None = Field(default=None, ge=1)
    conditions: list[Condition] = Field(default_factory=list)
    exceptions: list[RuleException] = Field(default_factory=list)
    severity: Severity
    explanation: str = ""
    review_on: list[ReviewTrigger] = Field(
        default_factory=lambda: ["unverified", "annualized", "stale", "estimate", "reported_not_recomputed", "period_mismatch"]
    )
    source: str = ""
    effective_date: date | None = None

    @model_validator(mode="after")
    def _check_type_fields(self) -> "Rule":
        if self.type == "metric_threshold":
            if not (self.metric and self.operator and self.threshold is not None):
                raise ValueError(f"rule {self.id}: metric_threshold needs metric, operator and threshold")
        if self.type == "inputs_present" and not self.required_inputs:
            raise ValueError(f"rule {self.id}: inputs_present needs required_inputs")
        if self.type == "documents_present" and not self.documents:
            raise ValueError(f"rule {self.id}: documents_present needs documents")
        if self.type in ("valuation_age", "statement_age") and not self.max_age_days:
            raise ValueError(f"rule {self.id}: {self.type} needs max_age_days")
        return self


class PolicyParameters(BaseModel):
    model_config = ConfigDict(extra="allow")
    collateral_advance_rates: dict[str, Decimal] = Field(default_factory=dict)
    sensitivity_revenue_shocks: list[Decimal] = Field(default_factory=lambda: [Decimal(x) for x in ("0", "-0.05", "-0.10", "-0.15", "-0.20", "-0.25")])
    sensitivity_rate_shocks_bps: list[int] = Field(default_factory=lambda: [0, 100, 200, 300])
    sensitivity_expense_shocks: list[Decimal] = Field(default_factory=lambda: [Decimal(x) for x in ("0", "0.05", "0.10")])

    @field_validator("collateral_advance_rates")
    @classmethod
    def _rates(cls, v: dict[str, Decimal]) -> dict[str, Decimal]:
        for k, r in v.items():
            if r < 0 or r > 1:
                raise ValueError(f"advance rate for {k} must be between 0 and 1")
        return v


CONDITION_CATEGORIES = (
    "business_identity", "ownership", "financial_statements", "tax_returns", "bank_statements", "debt_and_liens",
    "collateral_valuation", "insurance", "guarantees", "title_and_liens", "licenses_permits", "loan_documents",
    "signatures", "funding_prerequisites", "final_review", "other",
)


class ConditionTemplate(BaseModel):
    """A due-diligence or closing requirement defined by policy (not by the software)."""

    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_\-]{1,40}$")
    description: str
    category: Literal[CONDITION_CATEGORIES]  # type: ignore[valid-type]
    phase: Literal["DUE_DILIGENCE", "CLOSING"] = "DUE_DILIGENCE"
    required: bool = True
    waivable: bool = True
    responsible_party: Literal["borrower", "lender", "guarantor", "third_party", "attorney", "title_company",
                               "insurance_agent", "appraiser"] = "borrower"
    due_days: int | None = Field(default=None, ge=0, le=365)
    applies_to: list[str] | Literal["all"] = "all"
    conditions: list[Condition] = Field(default_factory=list)


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_key: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    version: str
    name: str
    module: str
    loan_types: list[str] = Field(min_length=1)
    effective_date: date
    illustrative: bool
    disclaimer: str = ""
    source_description: str
    parameters: PolicyParameters = Field(default_factory=PolicyParameters)
    rules: list[Rule] = Field(min_length=1)
    conditions: list[ConditionTemplate] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not _SEMVER.match(v):
            raise ValueError("version must be semantic (MAJOR.MINOR.PATCH)")
        return v

    @model_validator(mode="after")
    def _checks(self) -> "Policy":
        ids = [r.id for r in self.rules]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate rule ids: {sorted(dupes)}")
        codes = [c.code for c in self.conditions]
        if len(codes) != len(set(codes)):
            raise ValueError("duplicate condition codes")
        if self.illustrative and "illustrative" not in self.disclaimer.lower():
            raise ValueError("illustrative policies must carry a disclaimer containing the word 'illustrative'")
        return self

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        a, b, c = self.version.split(".")
        return int(a), int(b), int(c)
