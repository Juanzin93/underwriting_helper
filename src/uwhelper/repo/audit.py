"""Append-only, hash-chained audit log.

Details hold identifiers, field names, statuses and amounts the user entered — never document text.

Tamper evidence (migration 0006): every event stores ``prev_hash`` and
``event_hash = SHA-256(prev_hash | occurred_at | case_id | entity_type | entity_id | action | details | actor)``.
:func:`verify_chain` recomputes the chain and reports breaks.

Limitations (honest): SQLite is an ordinary file. Anyone who can write to it can delete
the triggers, rewrite events and recompute every hash. The chain only detects edits made
without recomputing it (accidental changes, naive tampering, partial restores). For stronger
evidence, periodically write down or print the head hash shown on the System page and keep
backups on separate media.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from uwhelper.db.connection import Database, utcnow

GENESIS = "0" * 64


def _event_hash(prev_hash: str, occurred_at: str, case_id: str | None, entity_type: str, entity_id: str | None,
                action: str, details_json: str, actor: str) -> str:
    payload = "\x1f".join([prev_hash, occurred_at, case_id or "", entity_type, entity_id or "", action,
                           details_json, actor])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _head(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT event_hash FROM audit_events WHERE event_hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    return row["event_hash"] if row else GENESIS


def record(
    conn: sqlite3.Connection,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    case_id: str | None = None,
    details: dict[str, Any] | None = None,
    actor: str | None = None,
) -> None:
    from uwhelper.core.identity import current_actor

    actor = actor or current_actor()
    details_json = json.dumps(details or {}, default=str, sort_keys=True)
    occurred = utcnow()
    with Database.transaction(conn):  # write lock held while reading the chain head
        prev = _head(conn)
        h = _event_hash(prev, occurred, case_id, entity_type, entity_id, action, details_json, actor)
        conn.execute(
            "INSERT INTO audit_events(occurred_at, case_id, entity_type, entity_id, action, details_json, actor, "
            "prev_hash, event_hash) VALUES (?,?,?,?,?,?,?,?,?)",
            (occurred, case_id, entity_type, entity_id, action, details_json, actor, prev, h),
        )


def backfill_chain(conn: sqlite3.Connection) -> int:
    """Hash events created before migration 0006 (one-time, in id order)."""
    rows = conn.execute("SELECT * FROM audit_events WHERE event_hash IS NULL ORDER BY id").fetchall()
    if not rows:
        return 0
    with Database.transaction(conn):
        for r in rows:
            prev_row = conn.execute("SELECT event_hash FROM audit_events WHERE id < ? AND event_hash IS NOT NULL "
                                    "ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
            prev = prev_row["event_hash"] if prev_row else GENESIS
            h = _event_hash(prev, r["occurred_at"], r["case_id"], r["entity_type"], r["entity_id"], r["action"],
                            r["details_json"], r["actor"])
            conn.execute("UPDATE audit_events SET prev_hash=?, event_hash=? WHERE id=?", (prev, h, r["id"]))
    return len(rows)


def preserve_hashes_before_purge(conn: sqlite3.Connection, case_id: str) -> int:
    """Keep only id + hashes (no content) of events about to be deleted, so the chain stays verifiable."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO audit_purged_hashes(event_id, prev_hash, event_hash, purged_at) "
        "SELECT id, prev_hash, event_hash, ? FROM audit_events WHERE case_id=?", (utcnow(), case_id))
    return cur.rowcount


def verify_chain(conn: sqlite3.Connection) -> dict[str, Any]:
    events = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM audit_events ORDER BY id")}
    purged = {r["event_id"]: dict(r) for r in conn.execute("SELECT * FROM audit_purged_hashes")}
    problems: list[str] = []
    prev = GENESIS
    checked = 0
    for eid in sorted(set(events) | set(purged)):
        if eid in events:
            e = events[eid]
            if e["event_hash"] is None:
                problems.append(f"event {eid}: no hash")
                continue
            expected = _event_hash(e["prev_hash"] or "", e["occurred_at"], e["case_id"], e["entity_type"],
                                   e["entity_id"], e["action"], e["details_json"], e["actor"])
            if expected != e["event_hash"]:
                problems.append(f"event {eid}: content does not match its hash (modified)")
            if e["prev_hash"] != prev:
                problems.append(f"event {eid}: chain link broken (an earlier event was removed or altered)")
            prev = e["event_hash"]
            checked += 1
        else:
            p = purged[eid]
            if p["prev_hash"] != prev:
                problems.append(f"purged event {eid}: chain link broken")
            prev = p["event_hash"]
    return {"ok": not problems, "checked": checked, "purged_placeholders": len(purged), "head": prev,
            "problems": problems[:50]}


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
