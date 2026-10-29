"""Append-only audit log.

Details hold identifiers, field keys and statuses — never document text.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from uwhelper.db.connection import utcnow


def record(
    conn: sqlite3.Connection,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    case_id: str | None = None,
    details: dict[str, Any] | None = None,
    actor: str = "local_user",
) -> None:
    conn.execute(
        "INSERT INTO audit_events(occurred_at, case_id, entity_type, entity_id, action, details_json, actor) "
        "VALUES (?,?,?,?,?,?,?)",
        (utcnow(), case_id, entity_type, entity_id, action, json.dumps(details or {}, default=str, sort_keys=True), actor),
    )


def list_events(conn: sqlite3.Connection, case_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    if case_id:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE case_id = ? ORDER BY id DESC LIMIT ?", (case_id, limit)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["details"] = json.loads(r["details_json"] or "{}")
        out.append(d)
    return out
