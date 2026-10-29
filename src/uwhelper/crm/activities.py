"""Activities (notes, calls, meetings, manually logged emails) and the merged timeline.

Activities are append-only (database triggers). A correction is a new activity that points to the one it
corrects; the original is shown as superseded. Note text is stored in the database only: audit events record the
activity id, kind and links, never the subject or body, and nothing is written to the log file.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC
from typing import Any

from uwhelper.core.identity import current_actor
from uwhelper.crm.models import ActivityIn
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure

AUDIT_LABELS = {
    "deal_created": "Deal created", "deal_updated": "Deal details edited", "deal_contact_added": "Contact added",
    "deal_contact_removed": "Contact removed", "deal_document_attached": "Document attached",
    "deal_underwriting_file_created": "Underwriting file created", "deal_underwriting_file_linked":
    "Underwriting file linked", "document_requests_generated": "Document checklist generated",
    "document_request_added": "Document requested", "document_request_received": "Requested document received",
    "document_request_accepted": "Requested document accepted", "document_request_rejected":
    "Requested document rejected", "document_request_waived": "Requested document waived",
}


def _require(conn: sqlite3.Connection, table: str, id_: str | None, what: str) -> None:
    if id_ and not conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (id_,)).fetchone():
        raise ValidationFailure(f"{what} does not exist.")


def add_activity(conn: sqlite3.Connection, a: ActivityIn) -> str:
    if not (a.deal_id or a.contact_id or a.organization_id):
        raise ValidationFailure("Link the activity to a deal, contact or organization.")
    _require(conn, "deals", a.deal_id, "Deal")
    _require(conn, "contacts", a.contact_id, "Contact")
    _require(conn, "organizations", a.organization_id, "Organization")
    if a.corrects_activity_id:
        orig = conn.execute("SELECT * FROM activities WHERE id=?", (a.corrects_activity_id,)).fetchone()
        if not orig:
            raise ValidationFailure("The activity being corrected does not exist.")
        if conn.execute("SELECT 1 FROM activities WHERE corrects_activity_id=?", (a.corrects_activity_id,)).fetchone():
            raise ValidationFailure("That activity was already corrected; correct the latest version instead.")
    aid, now = new_id(), utcnow()
    # A time without a zone (HTML datetime-local) is local time on this PC; store UTC like every other timestamp.
    occurred = a.occurred_at.astimezone(UTC).replace(microsecond=0).isoformat() if a.occurred_at else now
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO activities(id, deal_id, contact_id, organization_id, kind, subject, body, occurred_at, actor,
                   corrects_activity_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, a.deal_id, a.contact_id, a.organization_id, a.kind, a.subject, a.body, occurred, current_actor(),
             a.corrects_activity_id, now))
        case_id = None
        if a.deal_id:
            case_id = conn.execute("SELECT loan_case_id FROM deals WHERE id=?", (a.deal_id,)).fetchone()[0]
        audit.record(conn, "activity_corrected" if a.corrects_activity_id else "activity_added", "activity", aid,
                     case_id, {"kind": a.kind, "deal_id": a.deal_id, "contact_id": a.contact_id,
                               "organization_id": a.organization_id, "corrects": a.corrects_activity_id})
    return aid


def get_activity(conn: sqlite3.Connection, activity_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
    if not row:
        raise NotFound("Activity not found")
    return dict(row)


def list_activities(conn: sqlite3.Connection, *, deal_id: str | None = None, contact_id: str | None = None,
                    organization_id: str | None = None, include_superseded: bool = True,
                    limit: int = 200) -> list[dict[str, Any]]:
    col, val = next(((c, v) for c, v in (("deal_id", deal_id), ("contact_id", contact_id),
                                          ("organization_id", organization_id)) if v), (None, None))
    if not col:
        raise ValidationFailure("Give a deal, contact or organization.")
    rows = [dict(r) for r in conn.execute(
        f"""SELECT a.*, EXISTS(SELECT 1 FROM activities n WHERE n.corrects_activity_id=a.id) AS superseded
            FROM activities a WHERE a.{col}=? ORDER BY a.occurred_at DESC, a.created_at DESC LIMIT ?""", (val, limit))]
    return rows if include_superseded else [r for r in rows if not r["superseded"]]


def deal_timeline(conn: sqlite3.Connection, deal_id: str, limit: int = 300) -> list[dict[str, Any]]:
    """Activities + status history + audited deal events + tasks, newest first."""
    items: list[dict[str, Any]] = []
    for a in list_activities(conn, deal_id=deal_id, limit=limit):
        items.append({"at": a["occurred_at"], "type": "activity", "kind": a["kind"], "title": a["subject"],
                      "body": a["body"], "actor": a["actor"], "id": a["id"], "superseded": bool(a["superseded"]),
                      "corrects": a["corrects_activity_id"]})
    for h in conn.execute("SELECT * FROM deal_status_history WHERE deal_id=? ORDER BY id DESC LIMIT ?", (deal_id, limit)):
        items.append({"at": h["occurred_at"], "type": "status", "kind": "status",
                      "title": f"{h['from_status'] or '—'} → {h['to_status']}", "body": h["reason"],
                      "actor": h["authorized_by"] or h["actor"]})
    for e in conn.execute(
            "SELECT occurred_at, action, actor FROM audit_events WHERE entity_type='deal' AND entity_id=? "
            "AND action NOT IN ('deal_status_changed','deal_status_synced','deal_created') ORDER BY id DESC LIMIT ?",
            (deal_id, limit)):
        items.append({"at": e["occurred_at"], "type": "event", "kind": "event",
                      "title": AUDIT_LABELS.get(e["action"], e["action"].replace("_", " ")), "body": None,
                      "actor": e["actor"]})
    for t in conn.execute("SELECT * FROM tasks WHERE deal_id=? ORDER BY created_at DESC LIMIT ?", (deal_id, limit)):
        items.append({"at": t["created_at"], "type": "task", "kind": "task", "title": f"Task: {t['title']}",
                      "body": None, "actor": t["created_by"]})
        if t["completed_at"]:
            items.append({"at": t["completed_at"], "type": "task", "kind": "task",
                          "title": f"Task {t['status']}: {t['title']}", "body": None, "actor": t["completed_by"]})
    items.sort(key=lambda i: i["at"] or "", reverse=True)
    return items[:limit]
