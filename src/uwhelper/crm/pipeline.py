"""One pipeline truth: how a deal's status and its linked underwriting file's stage stay consistent.

Ownership
* The **deal** owns the brokerage pipeline (INTAKE … LENDER_SUBMISSION … FUNDED → SERVICING).
* The **underwriting file** (loan case) owns the internal credit lifecycle, with its gates and human decisions.

Automatic, recorded synchronisation (never silent: each one writes deal/case history and an audit event)
* Case → deal: FUNDED, SERVICING, DECLINED, WITHDRAWN, CANCELLED are copied to an open deal. Funding carries the
  funding confirmation's "confirmed by"; a decline carries the credit decision's "decided by".
* Deal → case: WITHDRAWN and CANCELLED close a pre-funding underwriting file (the borrower walked away).
* A lender decline (deal DECLINED) does **not** change the file; the deal page shows a notice instead.

Guards (so terminal states never contradict)
* A file cannot be funded while its deal is DECLINED / WITHDRAWN / CANCELLED.
* A file cannot be declined / withdrawn / cancelled while its deal is FUNDED or SERVICING.
* A deal cannot become FUNDED / SERVICING while its file is DECLINED / WITHDRAWN / CANCELLED.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from uwhelper.db.connection import Database, utcnow
from uwhelper.repo import audit

CASE_TO_DEAL = {"FUNDED": "FUNDED", "SERVICING": "SERVICING", "DECLINED": "DECLINED", "WITHDRAWN": "WITHDRAWN",
                "CANCELLED": "CANCELLED"}
DEAL_TO_CASE = {"WITHDRAWN": "WITHDRAWN", "CANCELLED": "CANCELLED"}
DEAL_CLOSED = {"FUNDED", "SERVICING", "DECLINED", "WITHDRAWN", "CANCELLED"}
DEAL_LOST = {"DECLINED", "WITHDRAWN", "CANCELLED"}
DEAL_FUNDED = {"FUNDED", "SERVICING"}
CASE_LOST = {"DECLINED", "WITHDRAWN", "CANCELLED"}
CASE_FUNDED = {"FUNDED", "SERVICING", "PAID_OFF"}
CASE_PRE_FUNDING = {"INTAKE", "UNDERWRITING", "CREDIT_REVIEW", "STRUCTURING", "DUE_DILIGENCE", "CLOSING", "ON_HOLD"}


def deal_for_case(conn: sqlite3.Connection, case_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM deals WHERE loan_case_id=?", (case_id,)).fetchone()
    return dict(row) if row else None


def case_transition_conflict(conn: sqlite3.Connection, case_id: str, target: str) -> str | None:
    """Reason the case may not move to ``target`` because of its deal, else None."""
    deal = deal_for_case(conn, case_id)
    if not deal:
        return None
    if target in CASE_LOST and deal["status"] in DEAL_FUNDED:
        return (f"Linked deal {deal['deal_number']} is {deal['status']}; a funded deal's underwriting file cannot be "
                f"{target.lower()}.")
    if target == "FUNDED" and deal["status"] in DEAL_LOST:
        return (f"Linked deal {deal['deal_number']} is {deal['status']}. Reopen the deal before confirming funding.")
    return None


def deal_transition_conflict(conn: sqlite3.Connection, deal: dict[str, Any], target: str) -> str | None:
    if not deal.get("loan_case_id") or target not in DEAL_FUNDED:
        return None
    row = conn.execute("SELECT case_number, stage FROM loan_cases WHERE id=?", (deal["loan_case_id"],)).fetchone()
    if row and row["stage"] in CASE_LOST:
        return (f"The linked underwriting file {row['case_number']} is {row['stage']}. Reopen it before marking the "
                f"deal {target.lower()}.")
    return None


def _authorizer(conn: sqlite3.Connection, case_id: str, stage: str) -> str | None:
    if stage == "FUNDED":
        r = conn.execute("SELECT confirmed_by FROM fundings WHERE case_id=?", (case_id,)).fetchone()
        return r["confirmed_by"] if r else None
    if stage == "DECLINED":
        r = conn.execute("SELECT decided_by FROM credit_decisions WHERE case_id=? AND decision='DECLINE' "
                         "ORDER BY decided_at DESC, rowid DESC LIMIT 1", (case_id,)).fetchone()
        return r["decided_by"] if r else None
    return None


def on_case_stage(conn: sqlite3.Connection, case_id: str, stage: str, reason: str, actor: str) -> str | None:
    """Called inside the case-transition transaction. Returns the deal's new status if it changed."""
    target = CASE_TO_DEAL.get(stage)
    deal = deal_for_case(conn, case_id) if target else None
    if not deal or deal["status"] == target:
        return None
    if deal["status"] in DEAL_CLOSED and not (deal["status"] == "FUNDED" and target == "SERVICING"):
        return None  # guards above prevent contradictions; a closed deal is never reopened automatically
    case_number = conn.execute("SELECT case_number FROM loan_cases WHERE id=?", (case_id,)).fetchone()[0]
    now = utcnow()
    note = f"Synchronized from underwriting file {case_number}: {reason}"[:2000]
    authorized_by = _authorizer(conn, case_id, stage)
    with Database.transaction(conn):
        conn.execute("UPDATE deals SET status=?, status_changed_at=?, hold_return_status=NULL, updated_at=? WHERE id=?",
                     (target, now, now, deal["id"]))
        conn.execute("INSERT INTO deal_status_history(deal_id, from_status, to_status, reason, authorized_by, actor, "
                     "occurred_at) VALUES (?,?,?,?,?,?,?)",
                     (deal["id"], deal["status"], target, note, authorized_by, actor, now))
        audit.record(conn, "deal_status_synced", "deal", deal["id"], case_id,
                     {"from": deal["status"], "to": target, "source": "underwriting_file",
                      "authorized_by": authorized_by}, actor)
    return target


def sync_case_from_deal(conn: sqlite3.Connection, deal: dict[str, Any], target: str, reason: str) -> str | None:
    """Called inside the deal-transition transaction. Closes a pre-funding file when the deal is withdrawn/cancelled."""
    to_stage = DEAL_TO_CASE.get(target)
    if not to_stage or not deal.get("loan_case_id"):
        return None
    row = conn.execute("SELECT stage FROM loan_cases WHERE id=?", (deal["loan_case_id"],)).fetchone()
    if not row or row["stage"] not in CASE_PRE_FUNDING:
        return None
    from uwhelper.workflow import lifecycle

    note = f"Synchronized from deal {deal['deal_number']}: {reason}"[:2000]
    return lifecycle.transition(conn, deal["loan_case_id"], to_stage, note)


def alignment_notes(deal: dict[str, Any]) -> list[str]:
    """Human-readable notices when deal and file differ in a way that needs a person's attention."""
    stage, status = deal.get("case_stage"), deal["status"]
    if not stage:
        return []
    notes = []
    if status in DEAL_LOST and stage in CASE_PRE_FUNDING:
        notes.append(f"The deal is {status} but its underwriting file is still open ({stage}). Close the file "
                     "(withdraw/cancel) or reopen the deal.")
    if status in DEAL_FUNDED and stage in CASE_PRE_FUNDING:
        notes.append("The deal is marked funded (e.g. by an outside lender) but the underwriting file was not funded "
                     "in this app; servicing here starts only from a confirmed funding on the file.")
    if stage in CASE_LOST and status not in DEAL_CLOSED:
        notes.append(f"The underwriting file is {stage} but the deal is open ({status}).")
    return notes
