"""Controlled loan lifecycle with gate checks and an append-only transition history.

Forward moves are validated by gate checks. Each blocker is HARD (cannot be bypassed) or
SOFT (may be overridden by a documented human reason, recorded with the blockers that were
overridden). Backward moves, holds and terminal exits need a reason but no gates.
FUNDED, SERVICING and PAID_OFF are entered only through their own confirmation actions.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Callable

from uwhelper.db.connection import Database, utcnow
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure, get_case

PIPELINE = ["INTAKE", "UNDERWRITING", "CREDIT_REVIEW", "STRUCTURING", "DUE_DILIGENCE", "CLOSING", "FUNDED",
            "SERVICING", "PAID_OFF"]
EXCEPTION_STAGES = ["ON_HOLD", "WITHDRAWN", "DECLINED", "CANCELLED"]
TERMINAL = {"WITHDRAWN", "DECLINED", "CANCELLED", "PAID_OFF"}
PRE_FUNDING = PIPELINE[:6]
SYSTEM_ONLY = {"FUNDED", "SERVICING", "PAID_OFF"}

STAGE_LABELS = {
    "INTAKE": "Intake", "UNDERWRITING": "Underwriting", "CREDIT_REVIEW": "Credit review", "STRUCTURING": "Structuring",
    "DUE_DILIGENCE": "Due diligence", "CLOSING": "Closing", "FUNDED": "Funded", "SERVICING": "Servicing",
    "PAID_OFF": "Paid off", "ON_HOLD": "On hold", "WITHDRAWN": "Withdrawn", "DECLINED": "Declined",
    "CANCELLED": "Cancelled",
}

# Allowed targets from each stage (documented in docs/LIFECYCLE.md).
TRANSITIONS: dict[str, list[str]] = {
    "INTAKE": ["UNDERWRITING", "ON_HOLD", "WITHDRAWN", "CANCELLED"],
    "UNDERWRITING": ["CREDIT_REVIEW", "INTAKE", "ON_HOLD", "WITHDRAWN", "CANCELLED"],
    "CREDIT_REVIEW": ["STRUCTURING", "UNDERWRITING", "DECLINED", "ON_HOLD", "WITHDRAWN", "CANCELLED"],
    "STRUCTURING": ["DUE_DILIGENCE", "CREDIT_REVIEW", "UNDERWRITING", "ON_HOLD", "WITHDRAWN", "CANCELLED"],
    "DUE_DILIGENCE": ["CLOSING", "STRUCTURING", "CREDIT_REVIEW", "ON_HOLD", "WITHDRAWN", "CANCELLED"],
    "CLOSING": ["FUNDED", "DUE_DILIGENCE", "CREDIT_REVIEW", "ON_HOLD", "WITHDRAWN", "CANCELLED"],
    "FUNDED": ["SERVICING"],
    "SERVICING": ["PAID_OFF"],
    "PAID_OFF": [],
    "ON_HOLD": ["RESUME", "WITHDRAWN", "CANCELLED"],
    "WITHDRAWN": ["INTAKE"],
    "DECLINED": ["INTAKE"],
    "CANCELLED": ["INTAKE"],
}

MIN_REASON = 10
MIN_OVERRIDE_REASON = 20


@dataclass
class Blocker:
    code: str
    message: str
    hard: bool
    link: str | None = None


class TransitionRefused(ValidationFailure):
    def __init__(self, message: str, blockers: list[Blocker] | None = None):
        super().__init__(message)
        self.blockers = blockers or []


# --------------------------------------------------------------------------- gate checks
def _count(conn: sqlite3.Connection, sql: str, *params: Any) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def _gate_underwriting(conn, case) -> list[Blocker]:
    b: list[Blocker] = []
    cid = case["id"]
    if not (case.get("borrower") or {}).get("legal_name"):
        b.append(Blocker("borrower", "Borrower legal name is missing.", True, f"/cases/{cid}/edit"))
    t = case.get("terms") or {}
    if not t.get("requested_amount"):
        b.append(Blocker("requested_terms", "Requested loan amount is not entered.", True, f"/cases/{cid}"))
    if _count(conn, "SELECT COUNT(*) FROM case_documents WHERE case_id=?", cid) == 0:
        b.append(Blocker("documents", "No documents uploaded yet.", False, f"/cases/{cid}/documents"))
    return b


def _gate_credit_review(conn, case) -> list[Blocker]:
    from uwhelper.analysis.resolver import find_contradictions
    from uwhelper.workflow.currency import run_status

    cid = case["id"]
    b: list[Blocker] = []
    rs = run_status(conn, cid)
    if not rs["current"]:
        b.append(Blocker("current_run", rs["reason"], True, f"/cases/{cid}/findings"))
    n = len(find_contradictions(conn, cid))
    if n:
        b.append(Blocker("contradictions", f"{n} unresolved contradiction(s) between sources.", False,
                         f"/cases/{cid}/contradictions"))
    n = _count(conn, "SELECT COUNT(*) FROM facts WHERE case_id=? AND status='candidate'", cid)
    if n:
        b.append(Blocker("unverified", f"{n} unverified candidate fact(s) not yet reviewed.", False, f"/cases/{cid}/review"))
    if rs["run"] and rs["run"]["overall_status"] == "INCOMPLETE":
        b.append(Blocker("incomplete_run", "The latest screening is INCOMPLETE (missing or unknown items).", False,
                         f"/cases/{cid}/missing"))
    return b


def _latest_decision(conn, case_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM credit_decisions WHERE case_id=? ORDER BY decided_at DESC, rowid DESC LIMIT 1",
                       (case_id,)).fetchone()
    return dict(row) if row else None


def _gate_structuring(conn, case) -> list[Blocker]:
    from uwhelper.workflow.currency import run_status

    cid = case["id"]
    b: list[Blocker] = []
    d = _latest_decision(conn, cid)
    if d is None or d["decision"] not in ("APPROVE", "APPROVE_WITH_CONDITIONS"):
        b.append(Blocker("decision", "No human credit decision approving the request is recorded"
                         + (f" (latest decision: {d['decision']})." if d else "."), True, f"/cases/{cid}/decision"))
    else:
        rs = run_status(conn, cid)
        if not rs["run"] or rs["run"]["id"] != d["run_id"] or not rs["current"]:
            b.append(Blocker("decision_stale", "The credit decision was based on an underwriting run that is no longer "
                             "current. Re-run the screening and record a new decision.", True, f"/cases/{cid}/decision"))
        if _count(conn, "SELECT COUNT(*) FROM credit_memos WHERE case_id=? AND run_id=?", cid, d["run_id"]) == 0:
            b.append(Blocker("memo", "No credit memo was generated from the run the decision relied on.", False,
                             f"/cases/{cid}/memo"))
    return b


def _gate_declined(conn, case) -> list[Blocker]:
    d = _latest_decision(conn, case["id"])
    if d is None or d["decision"] != "DECLINE":
        return [Blocker("decision", "A human DECLINE decision must be recorded first.", True,
                        f"/cases/{case['id']}/decision")]
    return []


def _approved_terms(conn, case_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM term_sets WHERE case_id=? AND term_type='approved' ORDER BY revision DESC LIMIT 1",
                       (case_id,)).fetchone()
    return dict(row) if row else None


def _gate_due_diligence(conn, case) -> list[Blocker]:
    cid = case["id"]
    b = [x for x in _gate_structuring(conn, case) if x.code != "memo"]
    at = _approved_terms(conn, cid)
    d = _latest_decision(conn, cid)
    if at is None:
        b.append(Blocker("approved_terms", "Human-approved terms are not recorded.", True, f"/cases/{cid}/decision"))
    elif d and at["decision_id"] != d["id"]:
        b.append(Blocker("approved_terms_stale", "Approved terms belong to an earlier credit decision; record approved "
                         "terms under the latest decision.", True, f"/cases/{cid}/decision"))
    if _count(conn, "SELECT COUNT(*) FROM loan_conditions WHERE case_id=?", cid) == 0:
        b.append(Blocker("conditions", "No due-diligence conditions have been generated or added.", False,
                         f"/cases/{cid}/conditions"))
    return b


def open_required_conditions(conn, case_id: str, phase: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM loan_conditions WHERE case_id=? AND phase=? AND required=1 AND status NOT IN ('SATISFIED','WAIVED') "
        "ORDER BY code", (case_id, phase))]


def _gate_closing(conn, case) -> list[Blocker]:
    cid = case["id"]
    b = _gate_due_diligence(conn, case)
    b = [x for x in b if x.code != "conditions"]
    open_dd = open_required_conditions(conn, cid, "DUE_DILIGENCE")
    if open_dd:
        b.append(Blocker("dd_conditions", f"{len(open_dd)} required due-diligence condition(s) not satisfied or waived: "
                         + ", ".join(c["code"] for c in open_dd[:8]), True, f"/cases/{cid}/conditions"))
    return b


def funding_blockers(conn, case_id: str) -> list[Blocker]:
    """Checks for the funding confirmation (CLOSING -> FUNDED)."""
    case = get_case(conn, case_id)
    b = [x for x in _gate_closing(conn, case)]
    open_cl = open_required_conditions(conn, case_id, "CLOSING")
    if open_cl:
        b.append(Blocker("closing_items", f"{len(open_cl)} required closing item(s) open: "
                         + ", ".join(c["code"] for c in open_cl[:8]), True, f"/cases/{case_id}/closing"))
    ex = conn.execute("SELECT * FROM term_sets WHERE case_id=? AND term_type='executed' ORDER BY revision DESC LIMIT 1",
                      (case_id,)).fetchone()
    at = _approved_terms(conn, case_id)
    if ex is None:
        b.append(Blocker("executed_terms", "Final executed terms are not recorded.", True, f"/cases/{case_id}/closing"))
    elif at is not None:
        from decimal import Decimal

        if Decimal(ex["principal"]) > Decimal(at["principal"]):
            b.append(Blocker("executed_exceeds_approved", "Executed principal exceeds the human-approved principal. "
                             "Record a new credit decision first.", True, f"/cases/{case_id}/decision"))
        diffs = [f for f in ("annual_rate", "term_months", "amortization_months", "interest_only_months",
                             "payment_frequency", "rate_type") if str(ex[f]) != str(at[f])]
        if diffs:
            b.append(Blocker("executed_differs", "Executed terms differ from approved terms in: " + ", ".join(diffs)
                             + ". Confirm this was approved.", False, f"/cases/{case_id}/closing"))
    cl = conn.execute("SELECT * FROM closings WHERE case_id=?", (case_id,)).fetchone()
    if cl is None or not cl["scheduled_closing_date"]:
        b.append(Blocker("closing_record", "Closing date / closing record not entered.", False, f"/cases/{case_id}/closing"))
    from uwhelper.crm.pipeline import case_transition_conflict

    deal_conflict = case_transition_conflict(conn, case_id, "FUNDED")
    if deal_conflict:
        b.append(Blocker("linked_deal", deal_conflict, True, None))
    return b


GATES: dict[str, Callable[[sqlite3.Connection, dict], list[Blocker]]] = {
    "UNDERWRITING": _gate_underwriting,
    "CREDIT_REVIEW": _gate_credit_review,
    "STRUCTURING": _gate_structuring,
    "DUE_DILIGENCE": _gate_due_diligence,
    "CLOSING": _gate_closing,
    "DECLINED": _gate_declined,
}


def is_forward(from_stage: str, to_stage: str) -> bool:
    return (from_stage in PIPELINE and to_stage in PIPELINE and PIPELINE.index(to_stage) > PIPELINE.index(from_stage)) \
        or to_stage == "DECLINED"


def evaluate(conn: sqlite3.Connection, case_id: str, to_stage: str) -> list[Blocker]:
    case = get_case(conn, case_id)
    target = case["hold_return_stage"] if to_stage == "RESUME" else to_stage
    if to_stage == "RESUME" or not is_forward(case["stage"], target):
        return []
    gate = GATES.get(target)
    return gate(conn, case) if gate else []


def available_actions(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    case = get_case(conn, case_id)
    actions = []
    for target in TRANSITIONS.get(case["stage"], []):
        if target in SYSTEM_ONLY:
            continue
        blockers = evaluate(conn, case_id, target)
        label_target = case["hold_return_stage"] if target == "RESUME" else target
        actions.append({
            "to_stage": target,
            "label": ("Resume to " + STAGE_LABELS.get(label_target or "", "?")) if target == "RESUME"
            else ("Advance to " if is_forward(case["stage"], target) and target != "DECLINED" else "Move to ")
            + STAGE_LABELS[target],
            "forward": is_forward(case["stage"], target),
            "blockers": [asdict(b) for b in blockers],
            "hard_blocked": any(b.hard for b in blockers),
            "soft_only": bool(blockers) and not any(b.hard for b in blockers),
        })
    return actions


def _write(conn, case, to_stage: str, reason: str, override: bool, blockers: list[Blocker],
           actor: str | None) -> None:
    from uwhelper.core.identity import current_actor

    actor = actor or current_actor()
    now = utcnow()
    hold_return = case["stage"] if to_stage == "ON_HOLD" else None
    conn.execute("UPDATE loan_cases SET stage=?, stage_entered_at=?, hold_return_stage=?, updated_at=? WHERE id=?",
                 (to_stage, now, hold_return, now, case["id"]))
    conn.execute(
        "INSERT INTO workflow_transitions(case_id, from_stage, to_stage, reason, is_override, blockers_json, actor, "
        "occurred_at) VALUES (?,?,?,?,?,?,?,?)",
        (case["id"], case["stage"], to_stage, reason, int(override), json.dumps([asdict(b) for b in blockers]), actor, now))
    audit.record(conn, "workflow_transition", "loan_case", case["id"], case["id"],
                 {"from": case["stage"], "to": to_stage, "override": override, "reason": reason,
                  "overridden_blockers": [b.code for b in blockers]}, actor)
    from uwhelper.crm.pipeline import on_case_stage

    on_case_stage(conn, case["id"], to_stage, reason, actor)


def transition(conn: sqlite3.Connection, case_id: str, to_stage: str, reason: str, *, override: bool = False,
               actor: str | None = None, _system: bool = False) -> str:
    """Move a case to another stage. Returns the new stage."""
    case = get_case(conn, case_id)
    reason = (reason or "").strip()
    current = case["stage"]
    if to_stage not in TRANSITIONS.get(current, []):
        raise TransitionRefused(f"{STAGE_LABELS.get(current, current)} → {to_stage} is not an allowed transition.")
    if to_stage in SYSTEM_ONLY and not _system:
        raise TransitionRefused(f"{STAGE_LABELS[to_stage]} can only be entered through its confirmation page.")
    if len(reason) < MIN_REASON:
        raise TransitionRefused(f"Give a reason for this transition (at least {MIN_REASON} characters).")
    target = to_stage
    if to_stage == "RESUME":
        target = case["hold_return_stage"] or "INTAKE"
    from uwhelper.crm.pipeline import case_transition_conflict

    deal_conflict = case_transition_conflict(conn, case_id, target)
    if deal_conflict:
        raise TransitionRefused(deal_conflict)
    blockers = evaluate(conn, case_id, to_stage)
    hard = [b for b in blockers if b.hard]
    if hard:
        raise TransitionRefused("Blocked: " + " ".join(b.message for b in hard), blockers)
    if blockers and not override:
        raise TransitionRefused("Outstanding items: " + " ".join(b.message for b in blockers)
                                + " Resolve them, or override with a documented reason.", blockers)
    if override and blockers and len(reason) < MIN_OVERRIDE_REASON:
        raise TransitionRefused(f"An override needs a documented reason of at least {MIN_OVERRIDE_REASON} characters.",
                                blockers)
    with Database.transaction(conn):
        _write(conn, case, target, reason, bool(override and blockers), blockers if override else [], actor)
    return target


def history(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute("SELECT * FROM workflow_transitions WHERE case_id=? ORDER BY id", (case_id,)):
        d = dict(r)
        d["blockers"] = json.loads(r["blockers_json"] or "[]")
        out.append(d)
    return out


def stage_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    counts = {r["stage"]: r["n"] for r in conn.execute("SELECT stage, COUNT(*) AS n FROM loan_cases GROUP BY stage")}
    return [{"stage": s, "label": STAGE_LABELS[s], "count": counts.get(s, 0)} for s in PIPELINE + EXCEPTION_STAGES]


def require_stage(case: dict[str, Any], allowed: tuple[str, ...], action: str) -> None:
    if case["stage"] not in allowed:
        raise ValidationFailure(f"{action} is only available in stage(s): "
                                + ", ".join(STAGE_LABELS[s] for s in allowed)
                                + f". This case is in {STAGE_LABELS.get(case['stage'], case['stage'])}.")


def ensure_case(conn: sqlite3.Connection, case_id: str) -> dict[str, Any]:
    try:
        return get_case(conn, case_id)
    except NotFound:
        raise
