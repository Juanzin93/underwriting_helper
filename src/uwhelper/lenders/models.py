"""Input models for lender programs and their criteria.

A criterion uses the same small language as policy conditions: ``subject`` ``op`` ``value``.
Subjects:
* ``metric:<key>``  — a metric of the deal's underwriting run (e.g. ``metric:dscr``)
* ``input:<field>`` — a resolved financial input of that run (e.g. ``input:revenue``)
* ``terms:<field>`` — proposed loan terms of that run (requested_amount, interest_rate, amortization_months, …)
* ``case:<loan_type|is_secured>``
* ``deal:<deal_type|product_type|requested_amount|borrower_state>`` — from the deal record
Operators: >=, >, <=, <, ==, !=, in, not_in, is_known, is_unknown.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from uwhelper.crm.models import PRODUCT_TYPES

Op = Literal[">=", ">", "<=", "<", "==", "!=", "in", "not_in", "is_known", "is_unknown"]
TERMS_FIELDS = ("requested_amount", "interest_rate", "term_months", "amortization_months", "project_total_cost",
                "is_secured", "repayment_type", "rate_type")
CASE_FIELDS = ("loan_type", "is_secured")
DEAL_FIELDS = ("deal_type", "product_type", "requested_amount", "borrower_state")
_SUBJECT = re.compile(r"^(metric|input|terms|case|deal):[a-z0-9_]+$")
_US_STATE = re.compile(r"^[A-Z]{2}$")


def known_subjects() -> dict[str, set[str]]:
    from uwhelper.modules import get_module

    mod = get_module("commercial")
    return {"metric": set(mod.metric_catalog()), "input": {k for k, f in mod.field_catalog().items()
                                                          if not f.party_scoped and f.unit != "text"},
            "terms": set(TERMS_FIELDS), "case": set(CASE_FIELDS), "deal": set(DEAL_FIELDS)}


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.\-]{0,59}$")
    description: str = Field(min_length=3, max_length=300)
    subject: str
    op: Op
    value: Any = None
    required: bool = True  # a FAIL on a non-required criterion is shown but does not rule the program out

    @field_validator("subject")
    @classmethod
    def _subject(cls, v: str) -> str:
        if not _SUBJECT.match(v):
            raise ValueError("subject must look like metric:dscr, terms:requested_amount, deal:product_type …")
        kind, name = v.split(":", 1)
        if name not in known_subjects()[kind]:
            raise ValueError(f"unknown {kind} {name!r}")
        return v

    @model_validator(mode="after")
    def _value(self) -> Criterion:
        if self.op in ("is_known", "is_unknown"):
            self.value = None
        elif self.op in ("in", "not_in"):
            if isinstance(self.value, str):
                self.value = [x.strip() for x in self.value.split("|") if x.strip()]
            if not isinstance(self.value, list) or not self.value:
                raise ValueError(f"criterion {self.id}: '{self.op}' needs a list of values (or a|b|c)")
            self.value = [str(x) for x in self.value]
        elif self.op in (">=", ">", "<=", "<"):
            try:
                self.value = str(Decimal(str(self.value).replace(",", "").strip()))
            except (InvalidOperation, ValueError):
                raise ValueError(f"criterion {self.id}: '{self.op}' needs a number") from None
        elif self.value is None or str(self.value).strip() == "":
            raise ValueError(f"criterion {self.id}: '{self.op}' needs a value")
        else:
            self.value = str(self.value).strip().lower() if isinstance(self.value, bool) else str(self.value).strip()
        return self


def _check_products(v: list[str]) -> list[str]:
    allowed = {p for ps in PRODUCT_TYPES.values() for p in ps}
    bad = [p for p in v if p not in allowed]
    if bad:
        raise ValueError(f"unknown product type(s): {', '.join(bad)}")
    return sorted(set(v))


def _check_states(v: list[str]) -> list[str]:
    out = [s.strip().upper() for s in v if s.strip()]
    bad = [s for s in out if not _US_STATE.match(s)]
    if bad:
        raise ValueError(f"states must be two-letter codes: {', '.join(bad)}")
    return sorted(set(out))


class ProgramIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    organization_id: str
    name: str = Field(min_length=2, max_length=200)
    deal_types: list[Literal["small_business", "cre"]] = Field(default_factory=list)
    product_types: list[str] = Field(default_factory=list)
    states: list[str] = Field(default_factory=list)
    min_amount: Decimal | None = Field(default=None, ge=0)
    max_amount: Decimal | None = Field(default=None, gt=0)
    notes: str | None = Field(default=None, max_length=5000)

    _p = field_validator("product_types")(lambda v: _check_products(v))
    _s = field_validator("states")(lambda v: _check_states(v))

    @model_validator(mode="after")
    def _range(self) -> ProgramIn:
        if self.min_amount is not None and self.max_amount is not None and self.min_amount > self.max_amount:
            raise ValueError("min_amount is larger than max_amount")
        return self


class ProgramPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=2, max_length=200)
    deal_types: list[Literal["small_business", "cre"]] | None = None
    product_types: list[str] | None = None
    states: list[str] | None = None
    min_amount: Decimal | None = Field(default=None, ge=0)
    max_amount: Decimal | None = Field(default=None, gt=0)
    active: bool | None = None
    notes: str | None = Field(default=None, max_length=5000)

    _p = field_validator("product_types")(lambda v: None if v is None else _check_products(v))
    _s = field_validator("states")(lambda v: None if v is None else _check_states(v))


class CriteriaVersionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    criteria: list[Criterion] = Field(min_length=1, max_length=100)
    source: str = Field(min_length=3, max_length=500)
    verified_at: date
    verified_by: str = Field(min_length=2, max_length=120)
    effective_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _checks(self) -> CriteriaVersionIn:
        ids = [c.id for c in self.criteria]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate criterion id(s): {', '.join(dupes)}")
        if self.verified_at > date.today():
            raise ValueError("verified_at cannot be in the future")
        return self
