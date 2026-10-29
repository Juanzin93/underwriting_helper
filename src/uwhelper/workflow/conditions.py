"""Due-diligence and closing conditions.

Rules
* Which items exist and whether they are required comes from the case's policy (``conditions:``),
  from the human credit decision, or from manual entry — never invented by the software.
* Status changes follow an explicit state machine; every change is an append-only event.
* SATISFIED requires evidence (a linked document or an evidence note) and the reviewer's verification.
* WAIVED requires a recorded human decision: name, explanation, and explicit confirmation.
  Items marked non-waivable (e.g. legal documents) cannot be waived at all.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from uwhelper.analysis.underwriting import default_policy_key
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.policy.engine import EvaluationContext, evaluate_condition
from uwhelper.policy.loader import resolve_policy
from uwhelper.policy.schema import CONDITION_CATEGORIES
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure, get_case

STATUSES = ("NOT_STARTED", "REQUESTED", "RECEIVED", "UNDER_REVIEW", "SATISFIED", "WAIVED", "REJECTED")
ALLOWED: dict[str, tuple[str, ...]] = {
    "NOT_STARTED": ("REQUESTED", "RECEIVED"),
    "REQUESTED": ("RECEIVED", "NOT_STARTED"),
    "RECEIVED": ("UNDER_REVIEW", "REJECTED"),
    "UNDER_REVIEW": ("SATISFIED", "REJECTED", "RECEIVED"),
    "REJECTED": ("REQUESTED", "RECEIVED"),
    "SATISFIED": ("UNDER_REVIEW",),  # re-open, with a reason
    "WAIVED": ("NOT_STARTED",),  # revoke a waiver, with a reason
}
CLOSED = ("SATISFIED", "WAIVED")


class ConditionIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_\-]{1,40}$")
    description: str = Field(min_length=3, max_length=1000)
    category: str
    phase: Literal["DUE_DILIGENCE", "CLOSING"] = "DUE_DILIGENCE"
    required: bool = True
    waivable: bool = True
    responsible_party: str = Field(default="borrower", max_length=60)
    due_date: str | None = None

    @field_validator("category")
    @classmethod
    def _cat(cls, v: str) -> str:
        if v not in CONDITION_CATEGORIES:
            raise ValueError("unknown category")
        return v

    @field_validator("due_date", mode="before")
    @classmethod
    def _d(cls, v: object) -> str | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        try:
            return date.fromisoformat(str(v)[:10]).isoformat()
        except ValueError as exc:
            raise ValueError("due date must be YYYY-MM-DD") from exc


def _event(conn, cid: str, frm: str | None, to: str, note: str | None, details: dict | None = None) -> None:
    conn.execute("INSERT INTO condition_events(condition_id, from_status, to_status, note, details_json, occurred_at) "
                 "VALUES (?,?,?,?,?,?)", (cid, frm, to, note, json.dumps(details or {}, sort_keys=True), utcnow()))


def _insert(conn, case_id: str, c: ConditionIn, source: str, policy_id: str | None = None) -> str | None:
    if conn.execute("SELECT 1 FROM loan_conditions WHERE case_id=? AND code=?", (case_id, c.code)).fetchone():
        return None
    cid, now = new_id(), utcnow()
    conn.execute(
        """INSERT INTO loan_conditions(id, case_id, code, phase, source, policy_id, description, category, required,
               waivable, responsible_party, due_date, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, case_id, c.code, c.phase, source, policy_id, c.description, c.category, int(c.required), int(c.waivable),
         c.responsible_party, c.due_date, now, now))
    _event(conn, cid, None, "NOT_STARTED", f"Created ({source})")
    audit.record(conn, "condition_created", "condition", cid, case_id,
                 {"code": c.code, "source": source, "required": c.required, "phase": c.phase})
    return cid


def generate_from_policy(conn: sqlite3.Connection, case_id: str, today: date | None = None) -> dict[str, int]:
    """Create the policy's applicable conditions (idempotent: existing codes are kept untouched)."""
    case = get_case(conn, case_id)
    policy_id, policy = resolve_policy(conn, case.get("policy_key") or default_policy_key(case["loan_type"]),
                                       case.get("policy_version"))
    if not policy.conditions:
        raise ValidationFailure(f"Policy {policy.policy_key} v{policy.version} defines no conditions. "
                                "Add them manually or use a policy version with a 'conditions' section.")
    from uwhelper.analysis.resolver import build_inputs

    inputs, _ = build_inputs(conn, case_id, "verified_only")
    ctx = EvaluationContext(case["loan_type"], inputs.terms.is_secured, {}, inputs)
    today = today or date.today()
    created = skipped = not_applicable = 0
    with Database.transaction(conn):
        for t in policy.conditions:
            if t.applies_to != "all" and case["loan_type"] not in t.applies_to:
                not_applicable += 1
                continue
            if any(evaluate_condition(c, ctx) is False for c in t.conditions):
                not_applicable += 1
                continue
            due = (today + timedelta(days=t.due_days)).isoformat() if t.due_days is not None else None
            cin = ConditionIn(code=t.code, description=t.description, category=t.category, phase=t.phase,
                              required=t.required, waivable=t.waivable, responsible_party=t.responsible_party,
                              due_date=due)
            if _insert(conn, case_id, cin, "policy", policy_id):
                created += 1
            else:
                skipped += 1
        audit.record(conn, "conditions_generated", "loan_case", case_id, case_id,
                     {"policy": f"{policy.policy_key} v{policy.version}", "created": created, "existing": skipped,
                      "not_applicable": not_applicable})
    return {"created": created, "existing": skipped, "not_applicable": not_applicable}


def add_manual(conn: sqlite3.Connection, case_id: str, c: ConditionIn) -> str:
    get_case(conn, case_id)
    with Database.transaction(conn):
        cid = _insert(conn, case_id, c, "manual")
    if cid is None:
        raise ValidationFailure(f"A condition with code {c.code} already exists on this case.")
    return cid


def add_decision_conditions(conn: sqlite3.Connection, case_id: str, decision_id: str, text: str) -> int:
    """Each non-empty line of the human decision's conditions becomes a required DD condition."""
    n = 0
    existing = conn.execute("SELECT COUNT(*) FROM loan_conditions WHERE case_id=? AND source='credit_decision'",
                            (case_id,)).fetchone()[0]
    for line in [x.strip(" -•\t") for x in text.splitlines()]:
        if len(line) < 3:
            continue
        n += 1
        code = f"DEC-{existing + n:02d}"
        _insert(conn, case_id, ConditionIn(code=code, description=line[:1000], category="other",
                                           responsible_party="borrower"), "credit_decision")
    return n


def get_condition(conn: sqlite3.Connection, case_id: str, condition_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM loan_conditions WHERE id=? AND case_id=?", (condition_id, case_id)).fetchone()
    if not row:
        raise NotFound("Condition not found")
    return dict(row)


def set_status(conn: sqlite3.Connection, case_id: str, condition_id: str, to_status: str, *, note: str | None = None,
               document_id: str | None = None, evidence_note: str | None = None, verified: bool = False) -> None:
    c = get_condition(conn, case_id, condition_id)
    frm = c["status"]
    if to_status == "WAIVED":
        raise ValidationFailure("Use the waiver form: a waiver needs a recorded human decision.")
    if to_status not in ALLOWED.get(frm, ()):
        raise ValidationFailure(f"{frm} → {to_status} is not an allowed status change.")
    note = (note or "").strip() or None
    if frm in CLOSED and (not note or len(note) < 10):
        raise ValidationFailure("Re-opening a closed condition requires a reason (at least 10 characters).")
    if to_status == "REJECTED" and (not note or len(note) < 5):
        raise ValidationFailure("Explain why the submission was rejected.")
    if document_id and not conn.execute("SELECT 1 FROM case_documents WHERE case_id=? AND document_id=?",
                                        (case_id, document_id)).fetchone():
        raise ValidationFailure("The linked document is not attached to this case.")
    doc = document_id or c["document_id"]
    evidence = (evidence_note or "").strip() or c["evidence_note"]
    updates: dict[str, Any] = {"status": to_status, "document_id": doc, "evidence_note": evidence, "updated_at": utcnow()}
    if to_status == "SATISFIED":
        if not doc and not evidence:
            raise ValidationFailure("Link a document or describe the evidence before marking the condition satisfied.")
        if not verified:
            raise ValidationFailure("Confirm that you reviewed and verified the evidence.")
        updates.update(verification_status="verified", completed_at=utcnow(), reviewer_notes=note or c["reviewer_notes"])
    else:
        updates.update(verification_status="unverified", completed_at=None)
    if frm == "WAIVED":
        updates.update(waiver_reason=None, waived_by=None, waived_at=None)
    with Database.transaction(conn):
        sets = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE loan_conditions SET {sets} WHERE id=?", (*updates.values(), condition_id))
        _event(conn, condition_id, frm, to_status, note, {"document_id": doc, "evidence_note": evidence})
        audit.record(conn, "condition_status_changed", "condition", condition_id, case_id,
                     {"code": c["code"], "from": frm, "to": to_status, "note": note, "document_id": doc})


def waive(conn: sqlite3.Connection, case_id: str, condition_id: str, *, waived_by: str, reason: str,
          confirm: bool) -> None:
    c = get_condition(conn, case_id, condition_id)
    if not c["waivable"]:
        raise ValidationFailure(f"{c['code']} is marked non-waivable by policy (e.g. a legal requirement). "
                                "It must be satisfied.")
    if c["status"] in CLOSED:
        raise ValidationFailure("Condition is already closed.")
    if not confirm:
        raise ValidationFailure("Confirm that a responsible human approved this waiver.")
    if len((waived_by or "").strip()) < 2 or len((reason or "").strip()) < 20:
        raise ValidationFailure("A waiver needs the approver's name and an explanation of at least 20 characters.")
    now = utcnow()
    with Database.transaction(conn):
        conn.execute("UPDATE loan_conditions SET status='WAIVED', waiver_reason=?, waived_by=?, waived_at=?, updated_at=? "
                     "WHERE id=?", (reason.strip(), waived_by.strip(), now, now, condition_id))
        _event(conn, condition_id, c["status"], "WAIVED", reason.strip(), {"waived_by": waived_by.strip()})
        audit.record(conn, "condition_waived", "condition", condition_id, case_id,
                     {"code": c["code"], "waived_by": waived_by.strip(), "reason": reason.strip(),
                      "required": bool(c["required"])})


def update_details(conn: sqlite3.Connection, case_id: str, condition_id: str, *, due_date: str | None,
                   responsible_party: str | None, reviewer_notes: str | None) -> None:
    c = get_condition(conn, case_id, condition_id)
    if due_date:
        try:
            due_date = date.fromisoformat(due_date[:10]).isoformat()
        except ValueError as exc:
            raise ValidationFailure("due date must be YYYY-MM-DD") from exc
    new = {"due_date": due_date or None, "responsible_party": (responsible_party or c["responsible_party"])[:60],
           "reviewer_notes": (reviewer_notes or "").strip()[:2000] or None}
    changed = {k: [c[k], v] for k, v in new.items() if c[k] != v}
    if not changed:
        return
    with Database.transaction(conn):
        conn.execute("UPDATE loan_conditions SET due_date=?, responsible_party=?, reviewer_notes=?, updated_at=? WHERE id=?",
                     (new["due_date"], new["responsible_party"], new["reviewer_notes"], utcnow(), condition_id))
        _event(conn, condition_id, c["status"], c["status"], "Details updated", {"changed": changed})
        audit.record(conn, "condition_updated", "condition", condition_id, case_id, {"code": c["code"], "changed": changed})


def list_conditions(conn: sqlite3.Connection, case_id: str, phase: str | None = None) -> list[dict[str, Any]]:
    sql = """SELECT c.*, d.original_filename FROM loan_conditions c LEFT JOIN documents d ON d.id=c.document_id
             WHERE c.case_id=?"""
    params: list[Any] = [case_id]
    if phase:
        sql += " AND c.phase=?"
        params.append(phase)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY c.phase DESC, c.required DESC, c.code", params)]
    today = date.today().isoformat()
    for r in rows:
        r["overdue"] = bool(r["due_date"] and r["due_date"] < today and r["status"] not in CLOSED)
        r["allowed"] = list(ALLOWED.get(r["status"], ()))
    return rows


def condition_history(conn: sqlite3.Connection, case_id: str, condition_id: str) -> list[dict[str, Any]]:
    get_condition(conn, case_id, condition_id)
    out = []
    for r in conn.execute("SELECT * FROM condition_events WHERE condition_id=? ORDER BY id", (condition_id,)):
        d = dict(r)
        d["details"] = json.loads(r["details_json"])
        out.append(d)
    return out


def summary(conn: sqlite3.Connection, case_id: str) -> dict[str, Any]:
    rows = list_conditions(conn, case_id)
    out: dict[str, Any] = {}
    for phase in ("DUE_DILIGENCE", "CLOSING"):
        ph = [r for r in rows if r["phase"] == phase]
        out[phase] = {
            "total": len(ph),
            "closed": sum(r["status"] in CLOSED for r in ph),
            "required_open": [r for r in ph if r["required"] and r["status"] not in CLOSED],
            "optional_open": [r for r in ph if not r["required"] and r["status"] not in CLOSED],
            "overdue": [r for r in ph if r["overdue"]],
            "waived": [r for r in ph if r["status"] == "WAIVED"],
        }
    return out
