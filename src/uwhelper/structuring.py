"""Loan structuring simulator: saved alternative structures for a case.

Structures never change requested terms, underwriting runs, approved or executed terms.
Each structure's inputs are immutable; every (re)calculation is stored as a snapshot with the
underwriting run and policy version it used.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from uwhelper.analysis.underwriting import default_policy_key
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.domain.models import _dec, _rate
from uwhelper.domain.money import dstr, round_to
from uwhelper.metrics.structuring import (
    ENGINE_VERSION,
    RateStep,
    StructureError,
    StructureInputs,
    analyse,
    sensitivity,
    serialise,
    validate,
)
from uwhelper.policy.loader import PolicyError, resolve_policy
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure, get_case
from uwhelper.workflow.currency import latest_base_run

COMPARE_KEYS = [
    ("financed_principal", "Financed principal", "USD"), ("level_payment", "Regular payment", "USD"),
    ("interest_only_payment", "Interest-only payment", "USD"), ("year1_debt_service", "Year-1 debt service", "USD"),
    ("max_annual_debt_service", "Highest annual debt service", "USD"), ("total_interest", "Total interest", "USD"),
    ("total_payments", "Total scheduled payments", "USD"), ("balloon_payment", "Balloon payment", "USD"),
    ("balance_after_60_months", "Balance after 60 months", "USD"), ("total_fees", "Fees and costs", "USD"),
    ("dscr_year1", "DSCR (year 1)", "ratio"), ("dscr_minimum", "DSCR (lowest year)", "ratio"),
    ("ltv", "LTV", "percent"), ("ltc", "LTC", "percent"),
]


class StructureIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    principal: Decimal
    annual_rate: Decimal
    rate_type: Literal["fixed", "variable"] = "fixed"
    rate_steps: list[tuple[int, Decimal]] = Field(default_factory=list)
    term_months: int = Field(ge=1, le=600)
    amortization_months: int = Field(ge=1, le=600)
    interest_only_months: int = Field(default=0, ge=0, le=600)
    payment_frequency: Literal["monthly", "quarterly", "semiannual", "annual"] = "monthly"
    origination_fee: Decimal = Decimal(0)
    other_costs: Decimal = Decimal(0)
    fees_financed: bool = False
    cfads_source: Literal["run", "manual"] = "run"
    cfads_manual: Decimal | None = None
    existing_debt_service_manual: Decimal | None = None
    collateral_value: Decimal | None = None
    project_cost: Decimal | None = None

    @field_validator("principal", mode="before")
    @classmethod
    def _p(cls, v: object) -> Decimal:
        d = _dec(v, allow_negative=False, required=True)
        if d is None or d <= 0:
            raise ValueError("principal must be greater than zero")
        return d

    @field_validator("annual_rate", mode="before")
    @classmethod
    def _r(cls, v: object) -> Decimal:
        r = _rate(v)
        if r is None:
            raise ValueError("rate is required")
        return r

    @field_validator("origination_fee", "other_costs", mode="before")
    @classmethod
    def _f(cls, v: object) -> Decimal:
        return _dec(v, allow_negative=False) or Decimal(0)

    @field_validator("cfads_manual", "existing_debt_service_manual", mode="before")
    @classmethod
    def _m(cls, v: object) -> Decimal | None:
        return _dec(v)

    @field_validator("collateral_value", "project_cost", mode="before")
    @classmethod
    def _c(cls, v: object) -> Decimal | None:
        return _dec(v, allow_negative=False)

    @field_validator("description", mode="before")
    @classmethod
    def _b(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("rate_steps", mode="before")
    @classmethod
    def _steps(cls, v: object) -> list:
        if isinstance(v, str):
            out = []
            for part in [p.strip() for p in v.replace(";", ",").split(",") if p.strip()]:
                if ":" not in part:
                    raise ValueError("rate steps look like '25:9.5, 49:10.25' (month:rate%)")
                m, r = part.split(":", 1)
                out.append((int(m), _rate(r)))
            return out
        return v or []

    @model_validator(mode="after")
    def _v(self) -> "StructureIn":
        if self.cfads_source == "manual" and self.cfads_manual is None:
            raise ValueError("enter the manual cash flow available for debt service, or use the latest run")
        try:
            validate(self.to_inputs(None, None))
        except StructureError as exc:
            raise ValueError(str(exc)) from exc
        return self

    def to_inputs(self, cfads: Decimal | None, existing: Decimal | None) -> StructureInputs:
        return StructureInputs(
            principal=self.principal, annual_rate=self.annual_rate, term_months=self.term_months,
            amortization_months=self.amortization_months, interest_only_months=self.interest_only_months,
            payment_frequency=self.payment_frequency, rate_type=self.rate_type,
            rate_steps=[RateStep(m, r) for m, r in self.rate_steps], origination_fee=self.origination_fee,
            other_costs=self.other_costs, fees_financed=self.fees_financed, cfads=cfads,
            existing_debt_service=existing, collateral_value=self.collateral_value, project_cost=self.project_cost)

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True)


def _run_values(conn, run_id: str | None) -> dict[str, Any]:
    vals: dict[str, Any] = {"run_id": run_id}
    if not run_id:
        return vals
    run = conn.execute("SELECT run_number, overall_status FROM underwriting_runs WHERE id=?", (run_id,)).fetchone()
    vals.update(run_number=run["run_number"], overall_status=run["overall_status"])
    for k in ("cfads", "existing_debt_service", "collateral_value"):
        m = conn.execute("SELECT value, status, flags_json FROM run_metrics WHERE run_id=? AND metric_key=?",
                         (run_id, k)).fetchone()
        vals[k] = Decimal(m["value"]) if m and m["status"] == "computed" and m["value"] is not None else None
        vals[f"{k}_flags"] = json.loads(m["flags_json"]) if m else []
    return vals


def policy_checks(conn, case: dict[str, Any], results: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        _, policy = resolve_policy(conn, case.get("policy_key") or default_policy_key(case["loan_type"]),
                                   case.get("policy_version"))
    except PolicyError:
        return [{"rule_id": "-", "result": "UNKNOWN", "explanation": "No policy resolved for this case."}]
    mapping = {"dscr": ("dscr_minimum", "lowest-year DSCR"), "dscr_property": ("dscr_minimum", "lowest-year DSCR"),
               "ltv": ("ltv", "LTV"), "ltc": ("ltc", "LTC")}
    out = []
    for rule in policy.rules:
        if rule.type != "metric_threshold" or rule.metric not in mapping:
            continue
        if rule.applies_to != "all" and case["loan_type"] not in rule.applies_to:
            continue
        key, label = mapping[rule.metric]
        v = results.get(key)
        if v is None:
            out.append({"rule_id": rule.id, "result": "UNKNOWN", "observed": None, "requirement": f"{rule.operator} {rule.threshold}",
                        "explanation": f"{label} not calculable (missing cash flow, collateral or cost)."})
            continue
        val = Decimal(v)
        cmp = round_to(val, rule.round_to) if rule.round_to is not None else val
        ok = {">=": cmp >= rule.threshold, ">": cmp > rule.threshold, "<=": cmp <= rule.threshold,
              "<": cmp < rule.threshold, "==": cmp == rule.threshold, "!=": cmp != rule.threshold}[rule.operator]
        out.append({"rule_id": rule.id, "result": "PASS" if ok else "FAIL", "observed": dstr(cmp),
                    "requirement": f"{rule.operator} {rule.threshold}",
                    "explanation": f"{label} {dstr(cmp)} vs {rule.operator} {rule.threshold} "
                                   f"(policy {policy.policy_key} v{policy.version}, illustrative={policy.illustrative})."})
    return out


def risks_and_limits(s: StructureIn, results: dict[str, Any], src: dict[str, Any]) -> list[str]:
    notes = []
    if s.interest_only_months:
        notes.append(f"Interest-only for {s.interest_only_months} months: year-1 coverage overstates coverage once "
                     "amortization starts — the lowest-year DSCR is used for policy checks.")
    if results.get("balloon_payment") and Decimal(results["balloon_payment"]) > 0:
        notes.append(f"Balloon of ${Decimal(results['balloon_payment']):,.2f} at maturity requires refinancing or "
                     "sale proceeds; refinancing risk is not modelled.")
    if s.rate_type == "variable":
        notes.append("Variable rate: the rate path entered is an assumption, not a forecast. See the rate sensitivity.")
    if s.fees_financed and (s.origination_fee or s.other_costs):
        notes.append("Fees are financed: they increase principal and debt service.")
    if s.cfads_source == "manual":
        notes.append("Cash flow available for debt service was entered manually for this simulation (assumption).")
    elif src.get("cfads") is None:
        notes.append("No verified-only underwriting run with calculable CFADS: DSCR cannot be computed.")
    if results.get("ltv") is None:
        notes.append("No collateral value available: LTV not computed.")
    notes.append("Payment estimates use a nominal periodic rate and cent rounding; they are not a lender quote.")
    return notes


def _calculate(conn, case: dict[str, Any], s: StructureIn) -> dict[str, Any]:
    run = latest_base_run(conn, case["id"])
    src = _run_values(conn, run["id"] if run else None)
    if s.cfads_source == "manual":
        cfads = s.cfads_manual
        existing = s.existing_debt_service_manual if s.existing_debt_service_manual is not None else src.get(
            "existing_debt_service")
    else:
        cfads, existing = src.get("cfads"), src.get("existing_debt_service")
    collateral = s.collateral_value if s.collateral_value is not None else src.get("collateral_value")
    si = s.to_inputs(cfads, existing)
    si.collateral_value = collateral
    if si.project_cost is None and case.get("terms") and case["terms"].get("project_total_cost"):
        si.project_cost = Decimal(case["terms"]["project_total_cost"])
    results = serialise(analyse(si))
    results["sensitivity"] = sensitivity(si)
    results["inputs_used"] = {"cfads": dstr(cfads), "existing_debt_service": dstr(existing),
                              "collateral_value": dstr(collateral), "project_cost": dstr(si.project_cost),
                              "cfads_source": s.cfads_source, "run_number": src.get("run_number"),
                              "cfads_flags": src.get("cfads_flags", [])}
    results["policy_checks"] = policy_checks(conn, case, results)
    results["risks"] = risks_and_limits(s, results, {"cfads": cfads})
    policy_id = None
    try:
        policy_id, _ = resolve_policy(conn, case.get("policy_key") or default_policy_key(case["loan_type"]),
                                      case.get("policy_version"))
    except PolicyError:
        pass
    return {"results": results, "run_id": src.get("run_id"), "policy_id": policy_id}


def create_structure(conn: sqlite3.Connection, case_id: str, s: StructureIn, *, baseline: bool = False) -> str:
    case = get_case(conn, case_id)
    if case["stage"] in ("FUNDED", "SERVICING", "PAID_OFF"):
        raise ValidationFailure("Loan is funded: use servicing what-if tools instead of origination structures.")
    calc = _calculate(conn, case, s)
    sid = new_id()
    payload = s.to_json()
    with Database.transaction(conn):
        conn.execute("INSERT INTO loan_structures(id, case_id, name, description, is_baseline, inputs_json, inputs_sha256, "
                     "created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (sid, case_id, s.name, s.description, int(baseline), payload,
                      hashlib.sha256(payload.encode()).hexdigest(), utcnow()))
        conn.execute("INSERT INTO structure_calculations(structure_id, run_id, policy_id, engine_version, results_json, "
                     "computed_at) VALUES (?,?,?,?,?,?)",
                     (sid, calc["run_id"], calc["policy_id"], ENGINE_VERSION, json.dumps(calc["results"], sort_keys=True),
                      utcnow()))
        audit.record(conn, "structure_created", "loan_structure", sid, case_id,
                     {"name": s.name, "baseline": baseline, "principal": dstr(s.principal), "rate": dstr(s.annual_rate),
                      "term": s.term_months, "amortization": s.amortization_months, "io": s.interest_only_months,
                      "frequency": s.payment_frequency})
    return sid


def baseline_from_requested(conn: sqlite3.Connection, case_id: str) -> str:
    case = get_case(conn, case_id)
    t = case.get("terms") or {}
    if not (t.get("requested_amount") and t.get("interest_rate")):
        raise ValidationFailure("Enter requested amount and rate before creating the baseline.")
    io = t.get("repayment_type") == "interest_only"
    term = t.get("term_months") or t.get("amortization_months") or 12
    s = StructureIn(name=f"Baseline: requested terms (rev. {t['revision']})",
                    description="Copied from the requested loan terms; requested terms are not modified.",
                    principal=t["requested_amount"], annual_rate=t["interest_rate"], rate_type=t.get("rate_type", "fixed"),
                    term_months=term, amortization_months=t.get("amortization_months") or term,
                    interest_only_months=(term - 1) if io and term > 1 else 0,
                    project_cost=t.get("project_total_cost"))
    return create_structure(conn, case_id, s, baseline=True)


def recalculate(conn: sqlite3.Connection, case_id: str, structure_id: str) -> None:
    case = get_case(conn, case_id)
    row = _row(conn, case_id, structure_id)
    s = StructureIn.model_validate(json.loads(row["inputs_json"]))
    calc = _calculate(conn, case, s)
    with Database.transaction(conn):
        conn.execute("INSERT INTO structure_calculations(structure_id, run_id, policy_id, engine_version, results_json, "
                     "computed_at) VALUES (?,?,?,?,?,?)",
                     (structure_id, calc["run_id"], calc["policy_id"], ENGINE_VERSION,
                      json.dumps(calc["results"], sort_keys=True), utcnow()))
        audit.record(conn, "structure_recalculated", "loan_structure", structure_id, case_id, {"run_id": calc["run_id"]})


def set_status(conn: sqlite3.Connection, case_id: str, structure_id: str, status: str) -> None:
    if status not in ("draft", "proposed", "archived"):
        raise ValidationFailure("invalid status")
    row = _row(conn, case_id, structure_id)
    with Database.transaction(conn):
        conn.execute("UPDATE loan_structures SET status=? WHERE id=?", (status, structure_id))
        audit.record(conn, "structure_status_changed", "loan_structure", structure_id, case_id,
                     {"from": row["status"], "to": status, "name": row["name"]})


def _row(conn, case_id: str, structure_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM loan_structures WHERE id=? AND case_id=?", (structure_id, case_id)).fetchone()
    if not row:
        raise NotFound("Structure not found")
    return dict(row)


def get_structure(conn: sqlite3.Connection, case_id: str, structure_id: str) -> dict[str, Any]:
    row = _row(conn, case_id, structure_id)
    row["inputs"] = json.loads(row["inputs_json"])
    calc = conn.execute("SELECT c.*, r.run_number FROM structure_calculations c LEFT JOIN underwriting_runs r "
                        "ON r.id=c.run_id WHERE c.structure_id=? ORDER BY c.id DESC LIMIT 1", (structure_id,)).fetchone()
    row["calc"] = dict(calc) if calc else None
    row["results"] = json.loads(calc["results_json"]) if calc else {}
    return row


def list_structures(conn: sqlite3.Connection, case_id: str, include_archived: bool = False) -> list[dict[str, Any]]:
    ids = [r["id"] for r in conn.execute("SELECT id FROM loan_structures WHERE case_id=? ORDER BY is_baseline DESC, "
                                         "created_at", (case_id,))]
    out = [get_structure(conn, case_id, i) for i in ids]
    return [s for s in out if include_archived or s["status"] != "archived"]


def compare(structures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    base = next((s for s in structures if s["is_baseline"]), structures[0] if structures else None)
    rows = []
    for key, label, unit in COMPARE_KEYS:
        cells = []
        bval = base["results"].get(key) if base else None
        for s in structures:
            v = s["results"].get(key)
            delta = None
            if v is not None and bval is not None and s is not base and Decimal(v) != Decimal(bval):
                delta = dstr(Decimal(v) - Decimal(bval))
            cells.append({"value": v, "delta": delta})
        rows.append({"key": key, "label": label, "unit": unit, "cells": cells})
    return rows
