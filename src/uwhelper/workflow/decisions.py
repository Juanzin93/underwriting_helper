"""Human credit decisions and approved / executed term sets.

Term layers kept separate:
  requested  -> loan_terms (revisioned; drives underwriting runs)
  simulated  -> loan_structures (any number, never authoritative)
  proposed   -> a structure marked 'proposed'
  approved   -> term_sets(term_type='approved'), tied to a human credit decision
  executed   -> term_sets(term_type='executed'), entered at closing
  funded     -> fundings.funded_amount (actual amount reported funded)
Nothing in a later layer overwrites an earlier one; every layer is append-only.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.domain.models import _dec, _rate
from uwhelper.domain.money import dstr
from uwhelper.metrics.structuring import PERIODS, StructureError, StructureInputs, analyse, validate
from uwhelper.repo import audit
from uwhelper.repo.cases import ValidationFailure, get_case
from uwhelper.workflow.currency import run_status
from uwhelper.workflow.lifecycle import require_stage

APPROVALS = ("APPROVE", "APPROVE_WITH_CONDITIONS")


class TermSetIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    principal: Decimal
    annual_rate: Decimal
    rate_type: Literal["fixed", "variable"] = "fixed"
    rate_index_note: str | None = Field(default=None, max_length=300)
    term_months: int = Field(ge=1, le=600)
    amortization_months: int = Field(ge=1, le=600)
    interest_only_months: int = Field(default=0, ge=0, le=600)
    payment_frequency: Literal["monthly", "quarterly", "semiannual", "annual"] = "monthly"
    origination_fee: Decimal = Decimal(0)
    other_costs: Decimal = Decimal(0)
    entered_by: str = Field(min_length=2, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)
    source_structure_id: str | None = None

    @field_validator("principal", mode="before")
    @classmethod
    def _p(cls, v: object) -> Decimal:
        d = _dec(v, allow_negative=False, required=True)
        if d is None or d <= 0:
            raise ValueError("principal must be greater than zero")
        return d

    @field_validator("origination_fee", "other_costs", mode="before")
    @classmethod
    def _fees(cls, v: object) -> Decimal:
        return _dec(v, allow_negative=False) or Decimal(0)

    @field_validator("annual_rate", mode="before")
    @classmethod
    def _r(cls, v: object) -> Decimal:
        r = _rate(v)
        if r is None:
            raise ValueError("rate is required")
        return r

    @field_validator("rate_index_note", "notes", "source_structure_id", mode="before")
    @classmethod
    def _blank(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="after")
    def _check(self) -> "TermSetIn":
        try:
            validate(self.to_structure())
        except StructureError as exc:
            raise ValueError(str(exc)) from exc
        return self

    def to_structure(self, **extra: Any) -> StructureInputs:
        return StructureInputs(principal=self.principal, annual_rate=self.annual_rate, term_months=self.term_months,
                               amortization_months=self.amortization_months,
                               interest_only_months=self.interest_only_months,
                               payment_frequency=self.payment_frequency, rate_type=self.rate_type,
                               origination_fee=self.origination_fee, other_costs=self.other_costs, **extra)


class DecisionIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    decision: Literal["APPROVE", "APPROVE_WITH_CONDITIONS", "DECLINE", "DEFER"]
    decided_by: str = Field(min_length=2, max_length=120)
    authority_note: str | None = Field(default=None, max_length=500)
    rationale: str = Field(min_length=20, max_length=5000)
    conditions_text: str | None = Field(default=None, max_length=5000)
    confirm_human: bool

    @model_validator(mode="after")
    def _c(self) -> "DecisionIn":
        if not self.confirm_human:
            raise ValueError("confirm that this decision is made by a responsible human, not by the software")
        if self.decision == "APPROVE_WITH_CONDITIONS" and not self.conditions_text:
            raise ValueError("list the approval conditions")
        return self


def _cfads_and_existing(run: dict) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    metrics = {r["metric_key"]: r for r in run.get("_metrics", [])}

    def val(k: str) -> Decimal | None:
        m = metrics.get(k)
        return Decimal(m["value"]) if m and m["status"] == "computed" and m["value"] is not None else None
    return val("cfads"), val("existing_debt_service"), val("collateral_value")


def run_metrics(conn: sqlite3.Connection, run_id: str) -> dict:
    run = dict(conn.execute("SELECT * FROM underwriting_runs WHERE id=?", (run_id,)).fetchone())
    run["_metrics"] = [dict(r) for r in conn.execute("SELECT * FROM run_metrics WHERE run_id=?", (run_id,))]
    return run


def analyse_terms(conn: sqlite3.Connection, case_id: str, terms: TermSetIn, run_id: str | None) -> dict[str, Any]:
    """Deterministic metrics for a term set using CFADS / existing debt service from a saved run."""
    cfads = existing = collateral = None
    if run_id:
        cfads, existing, collateral = _cfads_and_existing(run_metrics(conn, run_id))
    case = get_case(conn, case_id)
    project = Decimal(case["terms"]["project_total_cost"]) if case.get("terms") and case["terms"].get("project_total_cost") else None
    a = analyse(terms.to_structure(cfads=cfads, existing_debt_service=existing, collateral_value=collateral,
                                   project_cost=project))
    keys = ("level_payment", "interest_only_payment", "year1_debt_service", "max_annual_debt_service", "balloon_payment",
            "total_interest", "dscr_year1", "dscr_minimum", "ltv", "ltc")
    return {k: dstr(a[k]) if isinstance(a[k], Decimal) else a[k] for k in keys} | {
        "run_id": run_id, "cfads_used": dstr(cfads), "existing_debt_service_used": dstr(existing)}


def record_decision(conn: sqlite3.Connection, case_id: str, d: DecisionIn, approved: TermSetIn | None = None) -> str:
    case = get_case(conn, case_id)
    require_stage(case, ("CREDIT_REVIEW", "STRUCTURING", "DUE_DILIGENCE", "CLOSING"), "Recording a credit decision")
    rs = run_status(conn, case_id)
    if not rs["current"]:
        raise ValidationFailure("A decision must rely on a current underwriting run. " + rs["reason"])
    if d.decision in APPROVALS and approved is None:
        raise ValidationFailure("An approval must include the human-approved terms.")
    if d.decision not in APPROVALS and approved is not None:
        approved = None
    memo = conn.execute("SELECT id FROM credit_memos WHERE case_id=? AND run_id=? ORDER BY version DESC LIMIT 1",
                        (case_id, rs["run"]["id"])).fetchone()
    snapshot: dict[str, Any] = {"run_number": rs["run"]["run_number"], "overall_status": rs["run"]["overall_status"],
                                "inputs_sha256": rs["run"]["inputs_sha256"]}
    if approved is not None:
        snapshot["approved_terms_analysis"] = analyse_terms(conn, case_id, approved, rs["run"]["id"])
    did = new_id()
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO credit_decisions(id, case_id, run_id, memo_id, decision, decided_by, authority_note, rationale,
                   conditions_text, analysis_snapshot_json, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (did, case_id, rs["run"]["id"], memo["id"] if memo else None, d.decision, d.decided_by, d.authority_note,
             d.rationale, d.conditions_text, json.dumps(snapshot, sort_keys=True), utcnow()))
        audit.record(conn, "credit_decision_recorded", "credit_decision", did, case_id,
                     {"decision": d.decision, "decided_by": d.decided_by, "run_number": rs["run"]["run_number"],
                      "memo_id": memo["id"] if memo else None})
        if approved is not None:
            _insert_terms(conn, case_id, "approved", approved, did)
        if d.conditions_text:
            from uwhelper.workflow.conditions import add_decision_conditions

            add_decision_conditions(conn, case_id, did, d.conditions_text)
    return did


def _insert_terms(conn: sqlite3.Connection, case_id: str, term_type: str, t: TermSetIn, decision_id: str | None) -> str:
    rev = conn.execute("SELECT COALESCE(MAX(revision),0)+1 FROM term_sets WHERE case_id=? AND term_type=?",
                       (case_id, term_type)).fetchone()[0]
    tid = new_id()
    conn.execute(
        """INSERT INTO term_sets(id, case_id, term_type, revision, decision_id, source_structure_id, principal, annual_rate,
               rate_type, rate_index_note, term_months, amortization_months, interest_only_months, payment_frequency,
               origination_fee, other_costs, entered_by, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tid, case_id, term_type, rev, decision_id, t.source_structure_id, dstr(t.principal), dstr(t.annual_rate),
         t.rate_type, t.rate_index_note, t.term_months, t.amortization_months, t.interest_only_months,
         t.payment_frequency, dstr(t.origination_fee), dstr(t.other_costs), t.entered_by, t.notes, utcnow()))
    audit.record(conn, f"{term_type}_terms_recorded", "term_set", tid, case_id,
                 {"revision": rev, "principal": dstr(t.principal), "rate": dstr(t.annual_rate),
                  "term_months": t.term_months, "amortization_months": t.amortization_months,
                  "io_months": t.interest_only_months, "frequency": t.payment_frequency, "entered_by": t.entered_by,
                  "decision_id": decision_id, "source_structure_id": t.source_structure_id})
    return tid


def record_approved_terms(conn: sqlite3.Connection, case_id: str, t: TermSetIn) -> str:
    """Revise approved terms under the latest approving decision (e.g. from a structure)."""
    case = get_case(conn, case_id)
    require_stage(case, ("STRUCTURING",), "Revising approved terms")
    latest = latest_decision(conn, case_id)
    if not latest or latest["decision"] not in APPROVALS:
        raise ValidationFailure("Approved terms require a recorded human approval decision.")
    with Database.transaction(conn):
        return _insert_terms(conn, case_id, "approved", t, latest["id"])


def record_executed_terms(conn: sqlite3.Connection, case_id: str, t: TermSetIn) -> str:
    case = get_case(conn, case_id)
    require_stage(case, ("CLOSING",), "Recording executed terms")
    latest = latest_decision(conn, case_id)
    with Database.transaction(conn):
        return _insert_terms(conn, case_id, "executed", t, latest["id"] if latest else None)


def latest_decision(conn: sqlite3.Connection, case_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM credit_decisions WHERE case_id=? ORDER BY decided_at DESC, rowid DESC LIMIT 1",
                       (case_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["snapshot"] = json.loads(row["analysis_snapshot_json"])
    return d


def list_decisions(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute(
            """SELECT d.*, r.run_number FROM credit_decisions d JOIN underwriting_runs r ON r.id=d.run_id
               WHERE d.case_id=? ORDER BY d.decided_at DESC, d.rowid DESC""", (case_id,)):
        x = dict(r)
        x["snapshot"] = json.loads(r["analysis_snapshot_json"])
        out.append(x)
    return out


def term_sets(conn: sqlite3.Connection, case_id: str, term_type: str | None = None) -> list[dict[str, Any]]:
    sql, params = "SELECT * FROM term_sets WHERE case_id=?", [case_id]
    if term_type:
        sql += " AND term_type=?"
        params.append(term_type)
    return [dict(r) for r in conn.execute(sql + " ORDER BY term_type, revision DESC", params)]


def latest_terms(conn: sqlite3.Connection, case_id: str, term_type: str) -> dict[str, Any] | None:
    rows = term_sets(conn, case_id, term_type)
    return rows[0] if rows else None


def termset_to_input(row: dict[str, Any], entered_by: str | None = None) -> TermSetIn:
    return TermSetIn(principal=row["principal"], annual_rate=row["annual_rate"], rate_type=row["rate_type"],
                     rate_index_note=row.get("rate_index_note"), term_months=row["term_months"],
                     amortization_months=row["amortization_months"],
                     interest_only_months=row["interest_only_months"], payment_frequency=row["payment_frequency"],
                     origination_fee=row["origination_fee"], other_costs=row["other_costs"],
                     entered_by=entered_by or row["entered_by"])


assert set(PERIODS) == {"monthly", "quarterly", "semiannual", "annual"}
