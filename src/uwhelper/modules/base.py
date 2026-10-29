"""Extensible lending-module interface.

A lending module owns: a fact schema (field catalog), loan types, a metric
catalog and calculation function, a required-information checklist and its
document types. Common infrastructure (storage, extraction, policy engine,
reports, UI) is shared.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover
    from uwhelper.metrics.types import AnalysisInputs, MetricResult

Timing = Literal["duration", "instant", "static"]


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    category: str
    unit: str  # USD | text | date | percent | count
    timing: Timing
    description: str
    extractable: bool = True
    multi_item: bool = False  # several labelled items per period (e.g. EBITDA add-backs)
    party_scoped: bool = False  # belongs to an owner/guarantor (entity_ref)
    informational: bool = False  # never used directly in authoritative metrics


@dataclass(frozen=True)
class MetricSpec:
    key: str
    label: str
    unit: str  # USD | ratio | percent
    definition: str


@dataclass(frozen=True)
class LoanTypeSpec:
    key: str
    label: str
    implemented: bool = True
    note: str = ""


@dataclass(frozen=True)
class RequiredItem:
    key: str
    label: str
    kind: Literal["fact", "terms", "debt_schedule", "collateral", "guarantor", "document", "base_period"]
    fields: tuple[str, ...] = ()
    loan_types: tuple[str, ...] | None = None  # None = all
    why: str = ""


@dataclass
class LendingModule(ABC):
    key: str
    name: str
    loan_types: dict[str, LoanTypeSpec] = field(default_factory=dict)

    @abstractmethod
    def field_catalog(self) -> dict[str, FieldSpec]: ...

    @abstractmethod
    def metric_catalog(self) -> dict[str, MetricSpec]: ...

    @abstractmethod
    def compute_metrics(self, inputs: "AnalysisInputs", params: dict) -> dict[str, "MetricResult"]: ...

    @abstractmethod
    def required_items(self, loan_type: str) -> list[RequiredItem]: ...

    @abstractmethod
    def document_types(self) -> dict[str, str]: ...

    def implemented_loan_types(self) -> dict[str, LoanTypeSpec]:
        return {k: v for k, v in self.loan_types.items() if v.implemented}
