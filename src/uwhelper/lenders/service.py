"""Lender programs and immutable criteria versions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from typing import Any

from uwhelper.core.identity import current_actor
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.domain.money import dstr
from uwhelper.lenders.models import CriteriaVersionIn, ProgramIn, ProgramPatch
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure


def _lender_org(conn: sqlite3.Connection, org_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT id, name, org_type FROM organizations WHERE id=?", (org_id,)).fetchone()
    if not row:
        raise ValidationFailure("Lender organization does not exist.")
    if row["org_type"] != "lender":
        raise ValidationFailure(f"{row['name']} is not a lender organization (type {row['org_type']}).")
    return row


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    for k in ("deal_types", "product_types", "states"):
        d[k] = json.loads(d.pop(f"{k}_json"))
    return d


def create_program(conn: sqlite3.Connection, p: ProgramIn) -> str:
    _lender_org(conn, p.organization_id)
    pid, now = new_id(), utcnow()
    try:
        with Database.transaction(conn):
            conn.execute(
                """INSERT INTO lender_programs(id, organization_id, name, deal_types_json, product_types_json, states_json,
                       min_amount, max_amount, notes, created_by, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, p.organization_id, p.name, json.dumps(sorted(p.deal_types)), json.dumps(p.product_types),
                 json.dumps(p.states), dstr(p.min_amount), dstr(p.max_amount), p.notes, current_actor(), now, now))
            audit.record(conn, "lender_program_created", "lender_program", pid, None,
                         {"organization_id": p.organization_id, "name": p.name})
    except sqlite3.IntegrityError:
        raise ValidationFailure("This lender already has a program with that name.") from None
    return pid


def get_program(conn: sqlite3.Connection, program_id: str) -> dict[str, Any]:
    row = conn.execute("""SELECT p.*, o.name AS lender_name FROM lender_programs p JOIN organizations o
                          ON o.id=p.organization_id WHERE p.id=?""", (program_id,)).fetchone()
    if not row:
        raise NotFound("Lender program not found")
    p = _row(row)
    p["versions"] = list_versions(conn, program_id)
    p["current"] = current_version(conn, program_id)
    return p


def list_programs(conn: sqlite3.Connection, *, organization_id: str | None = None,
                  include_inactive: bool = False) -> list[dict[str, Any]]:
    sql = """SELECT p.*, o.name AS lender_name,
                    (SELECT MAX(version) FROM program_criteria_versions v WHERE v.program_id=p.id) AS latest_version,
                    (SELECT verified_at FROM program_criteria_versions v WHERE v.program_id=p.id
                     ORDER BY version DESC LIMIT 1) AS latest_verified_at
             FROM lender_programs p JOIN organizations o ON o.id=p.organization_id WHERE 1=1"""
    params: list[Any] = []
    if organization_id:
        sql += " AND p.organization_id=?"
        params.append(organization_id)
    if not include_inactive:
        sql += " AND p.active=1"
    sql += " ORDER BY o.name COLLATE NOCASE, p.name COLLATE NOCASE"
    return [_row(r) for r in conn.execute(sql, params)]


def update_program(conn: sqlite3.Connection, program_id: str, p: ProgramPatch) -> list[str]:
    before = get_program(conn, program_id)
    patch = p.model_dump(exclude_unset=True)
    if "name" in patch and not patch["name"]:
        raise ValidationFailure("name cannot be cleared")
    cols: dict[str, Any] = {}
    for k, v in patch.items():
        if k in ("deal_types", "product_types", "states"):
            v = sorted(v or [])
            if v != before[k]:
                cols[f"{k}_json"] = json.dumps(v)
        elif k in ("min_amount", "max_amount"):
            if dstr(v) != before[k]:
                cols[k] = dstr(v)
        elif k == "active":
            if int(v) != before["active"]:
                cols[k] = int(v)
        elif v != before[k]:
            cols[k] = v
    lo = cols.get("min_amount", before["min_amount"])
    hi = cols.get("max_amount", before["max_amount"])
    if lo is not None and hi is not None and float(lo) > float(hi):
        raise ValidationFailure("min_amount is larger than max_amount")
    if not cols:
        return []
    try:
        with Database.transaction(conn):
            conn.execute(f"UPDATE lender_programs SET {', '.join(f'{k}=?' for k in cols)}, updated_at=? WHERE id=?",
                         (*cols.values(), utcnow(), program_id))
            audit.record(conn, "lender_program_updated", "lender_program", program_id, None,
                         {"changed": sorted(k.removesuffix("_json") for k in cols)})
    except sqlite3.IntegrityError:
        raise ValidationFailure("This lender already has a program with that name.") from None
    return sorted(k.removesuffix("_json") for k in cols)


def canonical_criteria(v: CriteriaVersionIn) -> str:
    return json.dumps([c.model_dump() for c in v.criteria], sort_keys=True, separators=(",", ":"))


def add_version(conn: sqlite3.Connection, program_id: str, v: CriteriaVersionIn) -> dict[str, Any]:
    """Store a new immutable criteria version. Identical criteria with the same source and date are not duplicated."""
    get_program(conn, program_id)
    crit = canonical_criteria(v)
    digest = hashlib.sha256(crit.encode("utf-8")).hexdigest()
    latest = current_version(conn, program_id)
    if latest and latest["criteria_sha256"] == digest and latest["verified_at"] == v.verified_at.isoformat() \
            and latest["source"] == v.source:
        return {"id": latest["id"], "version": latest["version"], "created": False}
    vid, now = new_id(), utcnow()
    with Database.transaction(conn):
        n = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM program_criteria_versions WHERE program_id=?",
                         (program_id,)).fetchone()[0]
        conn.execute(
            """INSERT INTO program_criteria_versions(id, program_id, version, criteria_json, criteria_sha256, source,
                   verified_at, verified_by, effective_date, notes, created_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (vid, program_id, n, crit, digest, v.source, v.verified_at.isoformat(), v.verified_by,
             (v.effective_date or date.today()).isoformat(), v.notes, current_actor(), now))
        audit.record(conn, "lender_criteria_version_added", "lender_program", program_id, None,
                     {"version": n, "criteria": len(v.criteria), "criteria_sha256": digest, "source": v.source,
                      "verified_at": v.verified_at.isoformat(), "verified_by": v.verified_by,
                      "criteria_changed": not latest or latest["criteria_sha256"] != digest})
    return {"id": vid, "version": n, "created": True}


def _version_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["criteria"] = json.loads(d.pop("criteria_json"))
    return d


def list_versions(conn: sqlite3.Connection, program_id: str) -> list[dict[str, Any]]:
    return [_version_row(r) for r in conn.execute(
        "SELECT * FROM program_criteria_versions WHERE program_id=? ORDER BY version DESC", (program_id,))]


def current_version(conn: sqlite3.Connection, program_id: str, as_of: date | None = None) -> dict[str, Any] | None:
    """Latest version in effect on ``as_of`` (today by default)."""
    r = conn.execute("""SELECT * FROM program_criteria_versions WHERE program_id=? AND effective_date<=?
                        ORDER BY version DESC LIMIT 1""",
                     (program_id, (as_of or date.today()).isoformat())).fetchone()
    return _version_row(r) if r else None


def get_version(conn: sqlite3.Connection, program_id: str, version: int) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM program_criteria_versions WHERE program_id=? AND version=?",
                     (program_id, version)).fetchone()
    if not r:
        raise NotFound("Criteria version not found")
    return _version_row(r)


def age_days(verified_at: str, today: date | None = None) -> int:
    return ((today or date.today()) - date.fromisoformat(verified_at)).days
