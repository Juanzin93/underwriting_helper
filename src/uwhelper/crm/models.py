"""Pydantic input models for CRM entities (create = full model, update = all-optional patch)."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from uwhelper.domain.models import _dec

OrgType = Literal["borrower_business", "lender", "referral_partner", "brokerage", "law_firm", "title_company",
                  "accounting_firm", "insurance_agency", "appraiser", "other"]
ContactType = Literal["borrower", "guarantor", "referral_partner", "lender_contact", "attorney", "accountant", "other"]
DealType = Literal["small_business", "cre"]
PRODUCT_TYPES = {
    "small_business": ("term_loan", "line_of_credit", "equipment", "business_acquisition", "sba_7a", "sba_504",
                       "working_capital", "other"),
    "cre": ("cre_purchase", "cre_refinance", "cre_cash_out", "construction", "bridge", "owner_occupied_cre", "other"),
}
_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
_PHONE = re.compile(r"^[0-9+()\-. xext]{7,30}$")
_CFG = dict(str_strip_whitespace=True, extra="forbid")


def _blank(v: object) -> object:
    return None if isinstance(v, str) and not v.strip() else v


def _email(v: str | None) -> str | None:
    if v is not None and not _EMAIL.match(v):
        raise ValueError("not a valid email address")
    return v.lower() if v else v


def _phone(v: str | None) -> str | None:
    if v is not None and not _PHONE.match(v):
        raise ValueError("not a valid phone number")
    return v


class OrganizationIn(BaseModel):
    model_config = ConfigDict(**_CFG)
    name: str = Field(min_length=1, max_length=200)
    org_type: OrgType
    website: str | None = Field(default=None, max_length=200)
    phone: str | None = None
    email: str | None = Field(default=None, max_length=200)
    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=50)
    postal_code: str | None = Field(default=None, max_length=20)
    country: str = Field(default="US", min_length=2, max_length=2)
    industry: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=5000)

    _b = field_validator("website", "phone", "email", "address_line1", "address_line2", "city", "state",
                         "postal_code", "industry", "notes", mode="before")(_blank)
    _e = field_validator("email")(_email)
    _p = field_validator("phone")(_phone)

    @field_validator("website")
    @classmethod
    def _w(cls, v: str | None) -> str | None:
        if v and not re.match(r"^(https?://)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/.*)?$", v):
            raise ValueError("not a valid website")
        return v


class OrganizationPatch(OrganizationIn):
    name: str | None = Field(default=None, min_length=1, max_length=200)  # type: ignore[assignment]
    org_type: OrgType | None = None  # type: ignore[assignment]
    country: str | None = Field(default=None, min_length=2, max_length=2)  # type: ignore[assignment]
    archived: bool | None = None


class ContactIn(BaseModel):
    model_config = ConfigDict(**_CFG)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = None
    title: str | None = Field(default=None, max_length=120)
    contact_type: ContactType = "other"
    notes: str | None = Field(default=None, max_length=5000)
    organization_id: str | None = None
    organization_role: str | None = Field(default=None, max_length=60)

    _b = field_validator("email", "phone", "title", "notes", "organization_id", "organization_role",
                         mode="before")(_blank)
    _e = field_validator("email")(_email)
    _p = field_validator("phone")(_phone)


class ContactPatch(BaseModel):
    model_config = ConfigDict(**_CFG)
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = None
    title: str | None = Field(default=None, max_length=120)
    contact_type: ContactType | None = None
    notes: str | None = Field(default=None, max_length=5000)
    archived: bool | None = None

    _e = field_validator("email")(_email)
    _p = field_validator("phone")(_phone)


class DealIn(BaseModel):
    model_config = ConfigDict(**_CFG)
    name: str = Field(min_length=1, max_length=200)
    deal_type: DealType
    product_type: str
    loan_purpose: str | None = Field(default=None, max_length=1000)
    requested_amount: Decimal | None = None
    collateral_summary: str | None = Field(default=None, max_length=2000)
    borrower_organization_id: str | None = None
    primary_contact_id: str | None = None
    referral_source_contact_id: str | None = None
    target_close_date: date | None = None
    notes: str | None = Field(default=None, max_length=5000)

    _b = field_validator("loan_purpose", "collateral_summary", "borrower_organization_id", "primary_contact_id",
                         "referral_source_contact_id", "target_close_date", "notes", mode="before")(_blank)

    @field_validator("requested_amount", mode="before")
    @classmethod
    def _amt(cls, v: object) -> Decimal | None:
        d = _dec(_blank(v), allow_negative=False)
        if d is not None and d <= 0:
            raise ValueError("requested amount must be greater than zero")
        return d

    def check_product(self) -> None:
        if self.product_type not in PRODUCT_TYPES[self.deal_type]:
            raise ValueError(f"product_type must be one of {', '.join(PRODUCT_TYPES[self.deal_type])} for {self.deal_type}")


class DealPatch(BaseModel):
    model_config = ConfigDict(**_CFG)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    product_type: str | None = None
    loan_purpose: str | None = Field(default=None, max_length=1000)
    requested_amount: Decimal | None = None
    collateral_summary: str | None = Field(default=None, max_length=2000)
    borrower_organization_id: str | None = None
    primary_contact_id: str | None = None
    referral_source_contact_id: str | None = None
    target_close_date: date | None = None
    notes: str | None = Field(default=None, max_length=5000)
    archived: bool | None = None

    @field_validator("requested_amount", mode="before")
    @classmethod
    def _amt(cls, v: object) -> Decimal | None:
        d = _dec(_blank(v), allow_negative=False)
        if d is not None and d <= 0:
            raise ValueError("requested amount must be greater than zero")
        return d


class DealStatusIn(BaseModel):
    model_config = ConfigDict(**_CFG)
    to_status: str
    reason: str = Field(min_length=10, max_length=2000)
    authorized_by: str | None = Field(default=None, max_length=120)
    confirm: bool = False

    _b = field_validator("authorized_by", mode="before")(_blank)


class DealContactIn(BaseModel):
    model_config = ConfigDict(**_CFG)
    contact_id: str
    role: ContactType
