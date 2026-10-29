"""Facts, reporting periods and correction history.

Facts are append-only. A user correction inserts a new fact (source
``user_corrected``) that supersedes the original; the original row, its value
and its status history remain in the database.
"""

from __future__ import annotations

import json
import sqlite3
from calendar import monthrange
from datetime import date
from decimal import Decimal
from typing import Any

from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.domain.models import CorrectionIn, FactIn
from uwhelper.domain.money import InvalidAmount, dstr, to_decimal
from uwhelper.modules import FieldSpec, get_module
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure, touch

ACTIVE_STATUSES = ("candidate", "verified")


def period_label(period_end: str, months: int) -> str:
    if months == 0:
        return f"As of {period_end}"
    if months == 12:
        return f"12 months ended {period_end}"
    return f"{months} months ended {period_end}"


def get_or_create_period(conn: sqlite3.Connection, case_id: str, period_end: str, months: int) -> str:
    try:
        d = date.fromisoformat(period_end)
    except ValueError as exc:
        raise ValidationFailure("period end must be a valid YYYY-MM-DD date") from exc
    if not 0 <= months <= 12:
        raise ValidationFailure("period months must be between 0 and 12")
    if d.year < 1950 or d.year > date.today().year + 5:
        raise ValidationFailure("period end year is implausible")
    row = conn.execute(
        "SELECT id FROM financial_periods WHERE case_id=? AND period_end=? AND period_months=?",
        (case_id, d.isoformat(), months),
    ).fetchone()
    if row:
        return row["id"]
    pid = new_id()
    conn.execute(
        "INSERT INTO financial_periods(id, case_id, period_end, period_months, label, created_at) VALUES (?,?,?,?,?,?)",
        (pid, case_id, d.isoformat(), months, period_label(d.isoformat(), months), utcnow()),
    )
    return pid


def list_periods(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM financial_periods WHERE case_id=? ORDER BY period_end DESC, period_months DESC", (case_id,))]


def field_spec(field_key: str, module_key: str = "commercial") -> FieldSpec:
    spec = get_module(module_key).field_catalog().get(field_key)
    if spec is None:
        raise ValidationFailure(f"Unknown field {field_key!r}")
    return spec


def normalize_value(spec: FieldSpec, raw: str) -> tuple[str | None, str | None]:
    """Return (value_numeric, value_text) for storage."""
    if spec.unit == "text":
        text = raw.strip()
        if not text:
            raise ValidationFailure("text value is empty")
        return None, text[:500]
    if spec.unit == "date":
        try:
            return None, date.fromisoformat(raw.strip()[:10]).isoformat()
        except ValueError as exc:
            raise ValidationFailure("date must be YYYY-MM-DD") from exc
    try:
        d = to_decimal(raw, allow_none=False)
    except InvalidAmount as exc:
        raise ValidationFailure(str(exc)) from exc
    assert d is not None
    if abs(d) >= Decimal("1e15"):
        raise ValidationFailure("value is implausibly large")
    return dstr(d), None


def _resolve_period(conn: sqlite3.Connection, case_id: str, spec: FieldSpec, period_end: str | None,
                    months: int | None) -> str | None:
    if spec.timing == "static":
        return None
    if not period_end:
        raise ValidationFailure(f"{spec.label} needs a period end date")
    if spec.timing == "instant":
        months = 0
    elif months is None or months == 0:
        raise ValidationFailure(f"{spec.label} covers a period: give the number of months (1-12)")
    return get_or_create_period(conn, case_id, period_end, int(months))


def _check_entity(conn: sqlite3.Connection, case_id: str, spec: FieldSpec, entity_ref: str | None) -> str | None:
    if spec.party_scoped:
        if not entity_ref:
            raise ValidationFailure(f"{spec.label} must be linked to an owner/guarantor")
        if not conn.execute("SELECT 1 FROM parties WHERE id=? AND case_id=?", (entity_ref, case_id)).fetchone():
            raise ValidationFailure("owner/guarantor not found on this case")
        return entity_ref
    return None


def _event(conn: sqlite3.Connection, fact_id: str, event_type: str, from_status: str | None, to_status: str | None,
           note: str | None = None) -> None:
    conn.execute(
        "INSERT INTO fact_events(fact_id, event_type, from_status, to_status, note, occurred_at) VALUES (?,?,?,?,?,?)",
        (fact_id, event_type, from_status, to_status, note, utcnow()),
    )


def insert_fact(conn: sqlite3.Connection, *, case_id: str, field_key: str, value_numeric: str | None,
                value_text: str | None, unit: str, source_type: str, period_id: str | None = None,
                entity_ref: str | None = None, item_label: str = "", currency: str | None = None,
                raw_value_text: str | None = None, scale: str | None = None, document_id: str | None = None,
                page_number: int | None = None, excerpt: str | None = None, extraction_run_id: str | None = None,
                model_name: str | None = None, model_confidence: float | None = None,
                evidence_check: dict | None = None, status: str = "candidate",
                supersedes_fact_id: str | None = None, note: str | None = None) -> str:
    fid = new_id()
    conn.execute(
        """INSERT INTO facts(id, case_id, field_key, period_id, entity_ref, item_label, value_numeric, value_text, unit,
               currency, raw_value_text, scale, source_type, document_id, page_number, excerpt, extraction_run_id,
               model_name, model_confidence, evidence_check_json, status, supersedes_fact_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fid, case_id, field_key, period_id, entity_ref, item_label or "", value_numeric, value_text, unit,
         currency, raw_value_text, scale, source_type, document_id, page_number, excerpt, extraction_run_id,
         model_name, model_confidence, json.dumps(evidence_check) if evidence_check is not None else None,
         status, supersedes_fact_id, utcnow()),
    )
    _event(conn, fid, "created", None, status, note)
    return fid


def add_user_fact(conn: sqlite3.Connection, case_id: str, f: FactIn) -> str:
    spec = field_spec(f.field_key)
    if spec.multi_item and not f.item_label:
        raise ValidationFailure(f"{spec.label} needs an item label describing it")
    if f.document_id and not conn.execute(
            "SELECT 1 FROM case_documents WHERE case_id=? AND document_id=?", (case_id, f.document_id)).fetchone():
        raise ValidationFailure("Referenced document is not attached to this case.")
    num, text = normalize_value(spec, f.value)
    with Database.transaction(conn):
        period_id = _resolve_period(conn, case_id, spec, f.period_end, f.period_months)
        entity = _check_entity(conn, case_id, spec, f.entity_ref)
        status = "verified" if f.mark_verified else "candidate"
        fid = insert_fact(
            conn, case_id=case_id, field_key=spec.key, value_numeric=num, value_text=text, unit=spec.unit,
            currency="USD" if spec.unit == "USD" else None, source_type="user_entered", period_id=period_id,
            entity_ref=entity, item_label=f.item_label if spec.multi_item else "", raw_value_text=f.value,
            document_id=f.document_id, page_number=f.page_number, status=status, note=f.note,
        )
        if f.mark_verified:
            _event(conn, fid, "verified", "candidate", "verified", "Entered and verified by user")
        touch(conn, case_id)
        audit.record(conn, "fact_entered", "fact", fid, case_id,
                     {"field_key": spec.key, "period_id": period_id, "status": status})
    return fid


def get_fact(conn: sqlite3.Connection, case_id: str, fact_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM facts WHERE id=? AND case_id=?", (fact_id, case_id)).fetchone()
    if not row:
        raise NotFound("fact not found")
    return dict(row)


def _set_status(conn: sqlite3.Connection, case_id: str, fact_id: str, to_status: str, event: str,
                allowed_from: tuple[str, ...], note: str | None) -> None:
    fact = get_fact(conn, case_id, fact_id)
    if fact["status"] not in allowed_from:
        raise ValidationFailure(f"Cannot change a {fact['status']} fact to {to_status}.")
    with Database.transaction(conn):
        conn.execute("UPDATE facts SET status=? WHERE id=?", (to_status, fact_id))
        _event(conn, fact_id, event, fact["status"], to_status, note)
        touch(conn, case_id)
        audit.record(conn, f"fact_{event}", "fact", fact_id, case_id,
                     {"field_key": fact["field_key"], "from": fact["status"], "to": to_status, "note": note})


def verify_fact(conn: sqlite3.Connection, case_id: str, fact_id: str, note: str | None = None) -> None:
    fact = get_fact(conn, case_id, fact_id)
    if fact["value_numeric"] is None and fact["value_text"] is None:
        raise ValidationFailure("A fact without a value cannot be verified; correct it first.")
    _set_status(conn, case_id, fact_id, "verified", "verified", ("candidate",), note)


def unverify_fact(conn: sqlite3.Connection, case_id: str, fact_id: str, note: str | None = None) -> None:
    _set_status(conn, case_id, fact_id, "candidate", "unverified", ("verified",), note)


def reject_fact(conn: sqlite3.Connection, case_id: str, fact_id: str, reason: str) -> None:
    if not reason or len(reason.strip()) < 3:
        raise ValidationFailure("A reason is required to reject a fact.")
    _set_status(conn, case_id, fact_id, "rejected", "rejected", ("candidate", "verified"), reason.strip())


def restore_fact(conn: sqlite3.Connection, case_id: str, fact_id: str, note: str | None = None) -> None:
    _set_status(conn, case_id, fact_id, "candidate", "restored", ("rejected",), note)


def correct_fact(conn: sqlite3.Connection, case_id: str, fact_id: str, c: CorrectionIn) -> str:
    old = get_fact(conn, case_id, fact_id)
    if old["status"] in ("superseded",):
        raise ValidationFailure("This fact was already superseded; correct the newer version.")
    spec = field_spec(old["field_key"])
    num, text = normalize_value(spec, c.new_value)
    with Database.transaction(conn):
        new_status = "verified" if c.mark_verified else "candidate"
        new_id_ = insert_fact(
            conn, case_id=case_id, field_key=old["field_key"], value_numeric=num, value_text=text, unit=old["unit"],
            currency=old["currency"], source_type="user_corrected", period_id=old["period_id"],
            entity_ref=old["entity_ref"], item_label=old["item_label"], raw_value_text=c.new_value,
            document_id=old["document_id"], page_number=old["page_number"], excerpt=old["excerpt"],
            status=new_status, supersedes_fact_id=fact_id, note=c.reason,
        )
        conn.execute("UPDATE facts SET status='superseded' WHERE id=?", (fact_id,))
        _event(conn, fact_id, "superseded", old["status"], "superseded", f"Corrected: {c.reason}")
        conn.execute(
            """INSERT INTO user_corrections(id, case_id, original_fact_id, corrected_fact_id, previous_value, new_value,
                   reason, corrected_at) VALUES (?,?,?,?,?,?,?,?)""",
            (new_id(), case_id, fact_id, new_id_, old["value_numeric"] if old["value_numeric"] is not None else old["value_text"],
             num if num is not None else text, c.reason, utcnow()),
        )
        touch(conn, case_id)
        audit.record(conn, "fact_corrected", "fact", new_id_, case_id,
                     {"field_key": old["field_key"], "supersedes": fact_id, "status": new_status})
    return new_id_


def resolve_contradiction(conn: sqlite3.Connection, case_id: str, keep_fact_id: str, reject_fact_ids: list[str],
                          reason: str) -> None:
    """Documented resolution: verify one value and reject the conflicting ones, with a reason."""
    if not reason or len(reason.strip()) < 5:
        raise ValidationFailure("Resolving a contradiction requires a documented reason.")
    keep = get_fact(conn, case_id, keep_fact_id)
    with Database.transaction(conn):
        for fid in reject_fact_ids:
            other = get_fact(conn, case_id, fid)
            if (other["field_key"], other["period_id"], other["entity_ref"], other["item_label"]) != \
                    (keep["field_key"], keep["period_id"], keep["entity_ref"], keep["item_label"]):
                raise ValidationFailure("Facts being resolved must describe the same field and period.")
            if other["status"] in ACTIVE_STATUSES:
                reject_fact(conn, case_id, fid, f"Contradiction resolved in favour of {keep_fact_id[:8]}: {reason}")
        if keep["status"] == "candidate":
            verify_fact(conn, case_id, keep_fact_id, f"Contradiction resolution: {reason}")
        audit.record(conn, "contradiction_resolved", "fact", keep_fact_id, case_id,
                     {"field_key": keep["field_key"], "rejected": reject_fact_ids, "reason": reason})


def list_facts(conn: sqlite3.Connection, case_id: str, *, statuses: tuple[str, ...] | None = None,
               document_id: str | None = None) -> list[dict[str, Any]]:
    sql = """SELECT f.*, p.period_end, p.period_months, p.label AS period_label, d.original_filename,
                    pa.name AS party_name
             FROM facts f
             LEFT JOIN financial_periods p ON p.id = f.period_id
             LEFT JOIN documents d ON d.id = f.document_id
             LEFT JOIN parties pa ON pa.id = f.entity_ref
             WHERE f.case_id = ?"""
    params: list[Any] = [case_id]
    if statuses:
        sql += f" AND f.status IN ({','.join('?' * len(statuses))})"
        params.extend(statuses)
    if document_id:
        sql += " AND f.document_id = ?"
        params.append(document_id)
    sql += " ORDER BY f.field_key, p.period_end DESC, f.created_at"
    out = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        d["evidence_check"] = json.loads(r["evidence_check_json"]) if r["evidence_check_json"] else None
        out.append(d)
    return out


def fact_history(conn: sqlite3.Connection, case_id: str, fact_id: str) -> dict[str, Any]:
    """Walk the supersession chain both ways and return all versions with their events."""
    get_fact(conn, case_id, fact_id)
    root = fact_id
    while True:
        row = conn.execute("SELECT supersedes_fact_id FROM facts WHERE id=?", (root,)).fetchone()
        if row is None or not row["supersedes_fact_id"]:
            break
        root = row["supersedes_fact_id"]
    chain, current = [], root
    while current:
        f = get_fact(conn, case_id, current)
        f["events"] = [dict(e) for e in conn.execute(
            "SELECT * FROM fact_events WHERE fact_id=? ORDER BY id", (current,))]
        chain.append(f)
        nxt = conn.execute("SELECT id FROM facts WHERE supersedes_fact_id=? ORDER BY created_at LIMIT 1",
                           (current,)).fetchone()
        current = nxt["id"] if nxt else None
    corrections = [dict(r) for r in conn.execute(
        "SELECT * FROM user_corrections WHERE original_fact_id IN ({}) ORDER BY corrected_at".format(
            ",".join("?" * len(chain))), [c["id"] for c in chain])]
    return {"versions": chain, "corrections": corrections}


def months_between(start: date, end: date) -> int:
    """Whole months from start (inclusive) to end (inclusive) for period inference."""
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day >= monthrange(end.year, end.month)[1] or end.day >= start.day:
        months += 1
    return months
