"""Pydantic models validating every user-supplied input before it reaches SQL."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from uwhelper.domain.money import InvalidAmount, to_decimal

_STR = dict(str_strip_whitespace=True, extra="forbid")


def _dec(v: object, *, allow_negative: bool = True, required: bool = False) -> Decimal | None:
    try:
        d = to_decimal(v)
    except InvalidAmount as exc:
        raise ValueError(str(exc)) from exc
    if d is None:
        if required:
            raise ValueError("value is required")
        return None
    if not allow_negative and d < 0:
        raise ValueError("must not be negative")
    if abs(d) >= Decimal("1e15"):
        raise ValueError("value is implausibly large")
    return d


def _rate(v: object) -> Decimal | None:
    """Interest rate: accepts 8.25, '8.25%' or 0.0825 -> 0.0825."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    raw = str(v).strip()
    d = _dec(raw.rstrip("%"), allow_negative=False)
    if d is None:
        return None
    if raw.endswith("%") or d >= 1:
        d = d / 100
    if d >= Decimal("0.5"):
        raise ValueError("interest rate above 50% looks like an input error")
    return d


def _iso_date(v: object) -> str | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, date):
        return v.isoformat()
    try:
        return date.fromisoformat(str(v).strip()[:10]).isoformat()
    except ValueError as exc:
        raise ValueError("dates must be YYYY-MM-DD") from exc


class CaseIn(BaseModel):
    model_config = ConfigDict(**_STR)
    title: str = Field(min_length=1, max_length=200)
    loan_type: str = Field(min_length=1, max_length=64)
    policy_key: str | None = Field(default=None, max_length=64)
    policy_version: str | None = Field(default=None, max_length=20)
    notes: str | None = Field(default=None, max_length=5000)
    is_synthetic: bool = False

    @field_validator("policy_key", "policy_version", "notes", mode="before")
    @classmethod
    def _blank(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v


class BorrowerIn(BaseModel):
    model_config = ConfigDict(**_STR)
    legal_name: str = Field(min_length=1, max_length=200)
    dba_name: str | None = Field(default=None, max_length=200)
    entity_type: str | None = Field(default=None, max_length=60)
    industry: str | None = Field(default=None, max_length=120)
    naics_code: str | None = Field(default=None, pattern=r"^\d{2,6}$")
    state_of_formation: str | None = Field(default=None, max_length=40)
    year_established: int | None = Field(default=None, ge=1800, le=2100)

    @field_validator("dba_name", "entity_type", "industry", "naics_code", "state_of_formation", "year_established", mode="before")
    @classmethod
    def _blank(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v


class TermsIn(BaseModel):
    model_config = ConfigDict(**_STR)
    loan_purpose: str | None = Field(default=None, max_length=1000)
    requested_amount: Decimal | None = None
    term_months: int | None = Field(default=None, ge=1, le=600)
    amortization_months: int | None = Field(default=None, ge=1, le=600)
    interest_rate: Decimal | None = None
    rate_type: Literal["fixed", "variable"] = "fixed"
    repayment_type: Literal["amortizing", "interest_only"] = "amortizing"
    project_total_cost: Decimal | None = None
    requested_closing_date: str | None = None
    is_secured: bool = True
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("requested_amount", "project_total_cost", mode="before")
    @classmethod
    def _amount(cls, v: object) -> Decimal | None:
        d = _dec(v, allow_negative=False)
        if d is not None and d == 0:
            raise ValueError("must be greater than zero")
        return d

    @field_validator("interest_rate", mode="before")
    @classmethod
    def _r(cls, v: object) -> Decimal | None:
        return _rate(v)

    @field_validator("term_months", "amortization_months", "loan_purpose", "notes", mode="before")
    @classmethod
    def _blank(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("requested_closing_date", mode="before")
    @classmethod
    def _d(cls, v: object) -> str | None:
        return _iso_date(v)


class PartyIn(BaseModel):
    model_config = ConfigDict(**_STR)
    name: str = Field(min_length=1, max_length=200)
    party_type: Literal["individual", "entity"] = "individual"
    ownership_pct: Decimal | None = None
    is_guarantor: bool = False
    guarantee_type: Literal["none", "unlimited", "limited"] = "none"

    @field_validator("ownership_pct", mode="before")
    @classmethod
    def _pct(cls, v: object) -> Decimal | None:
        d = _dec(str(v).rstrip("%") if v is not None else None, allow_negative=False)
        if d is not None and d > 100:
            raise ValueError("ownership percentage must be between 0 and 100")
        return d

    @model_validator(mode="after")
    def _g(self) -> "PartyIn":
        if self.is_guarantor and self.guarantee_type == "none":
            self.guarantee_type = "unlimited"
        if not self.is_guarantor:
            self.guarantee_type = "none"
        return self


class DebtIn(BaseModel):
    model_config = ConfigDict(**_STR)
    creditor: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    debt_type: str = Field(default="term_loan", max_length=60)
    original_amount: Decimal | None = None
    current_balance: Decimal | None = None
    interest_rate: Decimal | None = None
    payment_amount: Decimal | None = None
    payment_frequency: Literal["monthly", "quarterly", "semiannual", "annual"] = "monthly"
    maturity_date: str | None = None
    to_be_refinanced: bool = False
    verification_status: Literal["unverified", "verified"] = "unverified"
    document_id: str | None = None
    page_number: int | None = Field(default=None, ge=1)

    @field_validator("original_amount", "current_balance", "payment_amount", mode="before")
    @classmethod
    def _amt(cls, v: object) -> Decimal | None:
        return _dec(v, allow_negative=False)

    @field_validator("interest_rate", mode="before")
    @classmethod
    def _r(cls, v: object) -> Decimal | None:
        return _rate(v)

    @field_validator("maturity_date", mode="before")
    @classmethod
    def _d(cls, v: object) -> str | None:
        return _iso_date(v)

    @field_validator("description", "document_id", "page_number", mode="before")
    @classmethod
    def _blank(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v


COLLATERAL_TYPES = ("real_estate", "equipment", "vehicles", "accounts_receivable", "inventory",
                    "cash_securities", "business_assets", "other")
VALUATION_METHODS = ("appraisal", "broker_opinion", "invoice", "book_value", "tax_assessment", "user_estimate", "other")


class CollateralIn(BaseModel):
    model_config = ConfigDict(**_STR)
    description: str = Field(min_length=1, max_length=300)
    collateral_type: Literal["real_estate", "equipment", "vehicles", "accounts_receivable", "inventory",
                             "cash_securities", "business_assets", "other"]
    lien_position: int = Field(default=1, ge=1, le=10)
    prior_lien_balance: Decimal = Decimal(0)

    @field_validator("prior_lien_balance", mode="before")
    @classmethod
    def _amt(cls, v: object) -> Decimal:
        return _dec(v, allow_negative=False) or Decimal(0)

    @model_validator(mode="after")
    def _lien(self) -> "CollateralIn":
        if self.lien_position == 1 and self.prior_lien_balance > 0:
            raise ValueError("a first-lien position cannot have prior lien balances")
        return self


class ValuationIn(BaseModel):
    model_config = ConfigDict(**_STR)
    value: Decimal
    valuation_date: str | None = None
    valuation_method: Literal["appraisal", "broker_opinion", "invoice", "book_value", "tax_assessment",
                              "user_estimate", "other"] = "user_estimate"
    verification_status: Literal["unverified", "verified"] = "unverified"
    document_id: str | None = None
    page_number: int | None = Field(default=None, ge=1)

    @field_validator("value", mode="before")
    @classmethod
    def _amt(cls, v: object) -> Decimal:
        d = _dec(v, allow_negative=False, required=True)
        assert d is not None
        return d

    @field_validator("valuation_date", mode="before")
    @classmethod
    def _d(cls, v: object) -> str | None:
        return _iso_date(v)

    @field_validator("document_id", "page_number", mode="before")
    @classmethod
    def _blank(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v


class FactIn(BaseModel):
    """A user-entered fact."""

    model_config = ConfigDict(**_STR)
    field_key: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=500)
    period_end: str | None = None
    period_months: int | None = Field(default=None, ge=0, le=12)
    entity_ref: str | None = None
    item_label: str = Field(default="", max_length=200)
    document_id: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    note: str | None = Field(default=None, max_length=1000)
    mark_verified: bool = False

    @field_validator("period_end", mode="before")
    @classmethod
    def _d(cls, v: object) -> str | None:
        return _iso_date(v)

    @field_validator("entity_ref", "document_id", "page_number", "period_months", "note", mode="before")
    @classmethod
    def _blank(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v


class CorrectionIn(BaseModel):
    model_config = ConfigDict(**_STR)
    new_value: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=3, max_length=1000)
    mark_verified: bool = True


class ScenarioIn(BaseModel):
    model_config = ConfigDict(**_STR)
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    revenue_change_pct: Decimal = Decimal(0)
    opex_change_pct: Decimal = Decimal(0)
    rate_change_bps: int = Field(default=0, ge=-1000, le=2000)
    loan_amount_override: Decimal | None = None
    amortization_months_override: int | None = Field(default=None, ge=1, le=600)
    interest_rate_override: Decimal | None = None

    @field_validator("revenue_change_pct", "opex_change_pct", mode="before")
    @classmethod
    def _pct(cls, v: object) -> Decimal:
        """Percent as entered by the user (-10 means -10%)."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return Decimal(0)
        d = _dec(str(v).rstrip("%"))
        assert d is not None
        if d < -100 or d > 500:
            raise ValueError("percent change must be between -100 and 500")
        return d / 100

    @field_validator("loan_amount_override", mode="before")
    @classmethod
    def _amt(cls, v: object) -> Decimal | None:
        d = _dec(v, allow_negative=False)
        if d is not None and d == 0:
            raise ValueError("loan amount must be greater than zero")
        return d

    @field_validator("interest_rate_override", mode="before")
    @classmethod
    def _r(cls, v: object) -> Decimal | None:
        return _rate(v)

    @field_validator("amortization_months_override", "description", mode="before")
    @classmethod
    def _blank(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    def adjustments(self) -> dict[str, str | int | None]:
        return {
            "revenue_change_pct": str(self.revenue_change_pct),
            "opex_change_pct": str(self.opex_change_pct),
            "rate_change_bps": self.rate_change_bps,
            "loan_amount_override": str(self.loan_amount_override) if self.loan_amount_override is not None else None,
            "amortization_months_override": self.amortization_months_override,
            "interest_rate_override": str(self.interest_rate_override) if self.interest_rate_override is not None else None,
        }
