"""Tasks and follow-ups linked to deals, contacts and/or organizations.

Dates are local calendar dates (``date.today()`` on this PC); "overdue" means an open task whose due date is before
today. Every change is audited (titles only — descriptions are recorded as "changed").
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from uwhelper.core.identity import current_actor
from uwhelper.crm.models import TaskIn, TaskPatch
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure

VIEWS = ("open", "overdue", "today", "week", "done", "all")


def _require(conn: sqlite3.Connection, table: str, id_: str | None, what: str) -> None:
    if id_ and not conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (id_,)).fetchone():
        raise ValidationFailure(f"{what} does not exist.")


def create_task(conn: sqlite3.Connection, t: TaskIn) -> str:
    _require(conn, "deals", t.deal_id, "Deal")
    _require(conn, "contacts", t.contact_id, "Contact")
    _require(conn, "organizations", t.organization_id, "Organization")
    tid, now = new_id(), utcnow()
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO tasks(id, title, description, deal_id, contact_id, organization_id, due_date, priority,
                   assignee, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tid, t.title, t.description, t.deal_id, t.contact_id, t.organization_id,
             t.due_date.isoformat() if t.due_date else None, t.priority, t.assignee, current_actor(), now, now))
        audit.record(conn, "task_created", "task", tid, None,
                     {"title": t.title, "deal_id": t.deal_id, "contact_id": t.contact_id,
                      "organization_id": t.organization_id, "due_date": t.due_date, "priority": t.priority})
    return tid


def get_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    row = conn.execute(
        """SELECT t.*, d.deal_number, d.name AS deal_name, c.first_name || ' ' || c.last_name AS contact_name,
                  o.name AS organization_name
           FROM tasks t LEFT JOIN deals d ON d.id=t.deal_id LEFT JOIN contacts c ON c.id=t.contact_id
           LEFT JOIN organizations o ON o.id=t.organization_id WHERE t.id=?""", (task_id,)).fetchone()
    if not row:
        raise NotFound("Task not found")
    return dict(row)


def list_tasks(conn: sqlite3.Connection, *, view: str = "open", deal_id: str | None = None,
               contact_id: str | None = None, organization_id: str | None = None, assignee: str | None = None,
               today: date | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    if view not in VIEWS:
        raise ValidationFailure(f"view must be one of {', '.join(VIEWS)}")
    if not (1 <= limit <= 500) or offset < 0:
        raise ValidationFailure("limit must be 1-500 and offset >= 0")
    today = today or date.today()
    sql = """SELECT t.*, d.deal_number, d.name AS deal_name, c.first_name || ' ' || c.last_name AS contact_name,
                    o.name AS organization_name
             FROM tasks t LEFT JOIN deals d ON d.id=t.deal_id LEFT JOIN contacts c ON c.id=t.contact_id
             LEFT JOIN organizations o ON o.id=t.organization_id WHERE 1=1"""
    params: list[Any] = []
    if view in ("open", "overdue", "today", "week"):
        sql += " AND t.status='open'"
    if view == "overdue":
        sql += " AND t.due_date IS NOT NULL AND t.due_date < ?"
        params.append(today.isoformat())
    elif view == "today":
        sql += " AND t.due_date IS NOT NULL AND t.due_date <= ?"
        params.append(today.isoformat())
    elif view == "week":
        sql += " AND t.due_date IS NOT NULL AND t.due_date <= ?"
        params.append((today + timedelta(days=7)).isoformat())
    elif view == "done":
        sql += " AND t.status IN ('done','cancelled')"
    for col, val in (("deal_id", deal_id), ("contact_id", contact_id), ("organization_id", organization_id),
                     ("assignee", assignee)):
        if val:
            sql += f" AND t.{col}=?"
            params.append(val)
    sql += (" ORDER BY CASE t.status WHEN 'open' THEN 0 ELSE 1 END, t.due_date IS NULL, t.due_date,"
            " CASE t.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, t.created_at LIMIT ? OFFSET ?")
    rows = [dict(r) for r in conn.execute(sql, (*params, limit, offset))]
    for r in rows:
        r["overdue"] = r["status"] == "open" and bool(r["due_date"]) and r["due_date"] < today.isoformat()
    return rows


def task_counts(conn: sqlite3.Connection, today: date | None = None) -> dict[str, int]:
    today = today or date.today()
    row = conn.execute(
        """SELECT SUM(status='open') AS open_, SUM(status='open' AND due_date IS NOT NULL AND due_date < ?) AS overdue,
                  SUM(status='open' AND due_date = ?) AS due_today FROM tasks""",
        (today.isoformat(), today.isoformat())).fetchone()
    return {"open": row["open_"] or 0, "overdue": row["overdue"] or 0, "due_today": row["due_today"] or 0}


def update_task(conn: sqlite3.Connection, task_id: str, p: TaskPatch) -> list[str]:
    before = get_task(conn, task_id)
    patch = p.model_dump(exclude_unset=True)
    if "title" in patch and patch["title"] is None:
        raise ValidationFailure("title cannot be cleared")
    values = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in patch.items()}
    changed = [k for k, v in values.items() if before.get(k) != v]
    if not changed:
        return []
    with Database.transaction(conn):
        conn.execute(f"UPDATE tasks SET {', '.join(f'{k}=?' for k in changed)}, updated_at=? WHERE id=?",
                     (*[values[k] for k in changed], utcnow(), task_id))
        audit.record(conn, "task_updated", "task", task_id, None,
                     {"changed": {k: ("(changed)" if k == "description" else [before.get(k), values[k]]) for k in changed}})
    return changed


def set_task_status(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    if status not in ("open", "done", "cancelled"):
        raise ValidationFailure("status must be open, done or cancelled")
    before = get_task(conn, task_id)
    if before["status"] == status:
        return
    now = utcnow()
    done = status in ("done", "cancelled")
    with Database.transaction(conn):
        conn.execute("UPDATE tasks SET status=?, completed_at=?, completed_by=?, updated_at=? WHERE id=?",
                     (status, now if done else None, current_actor() if done else None, now, task_id))
        audit.record(conn, f"task_{'reopened' if status == 'open' else status}", "task", task_id, None,
                     {"title": before["title"], "deal_id": before["deal_id"]})
