"""Lender conditions on a deal (from the commitment / accepted offer) and the due-diligence tracker.

Conditions: open → received → satisfied (a named person), or waived (a named person and a reason — normally the lender
waives); a received item can be returned to open with a reason. Satisfied and waived are final.

Due diligence (appraisal, environmental, title, insurance, …): not ordered → ordered (vendor, ordered/due dates) →
received → reviewed (a named person); any open item can be waived with a reason; a received item can be re-ordered.

Every change writes an append-only event and an audit record. Overdue items (due date passed, not finished) are listed
on the Tasks page.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from uwhelper.core.identity import current_actor
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure

COND_DONE = ("satisfied", "waived")
DD_DONE = ("reviewed", "waived")
DD_TYPES = ("appraisal", "environmental", "title", "insurance", "survey", "ucc_lien_search", "background_check", "flood",
            "zoning", "other")


def _blank(v: object) -> object:
    return None if isinstance(v, str) and not v.strip() else v


class ConditionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    description: str = Field(min_length=3, max_length=500)
    source: Literal["lender", "broker", "other"] = "lender"
    category: str = Field(default="other", max_length=60)
    phase: Literal["due_diligence", "closing", "post_closing"] = "closing"
    required: bool = True
    responsible: str | None = Field(default=None, max_length=120)
    due_on: date | None = None
    offer_id: str | None = None

    _b = field_validator("responsible", "due_on", "offer_id", mode="before")(_blank)


class ConditionUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    to_status: Literal["open", "received", "satisfied", "waived"]
    by: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=1000)
    document_id: str | None = None

    _b = field_validator("by", "note", "document_id", mode="before")(_blank)


class DDItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    item_type: Literal["appraisal", "environmental", "title", "insurance", "survey", "ucc_lien_search",
                       "background_check", "flood", "zoning", "other"]
    description: str | None = Field(default=None, max_length=500)
    vendor_organization_id: str | None = None
    vendor_name: str | None = Field(default=None, max_length=200)
    required: bool = True
    due_on: date | None = None

    _b = field_validator("description", "vendor_organization_id", "vendor_name", "due_on", mode="before")(_blank)


class DDUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    to_status: Literal["ordered", "received", "reviewed", "waived"]
    on: date | None = None
    due_on: date | None = None
    by: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=1000)
    document_id: str | None = None
    vendor_name: str | None = Field(default=None, max_length=200)

    _b = field_validator("on", "due_on", "by", "note", "document_id", "vendor_name", mode="before")(_blank)


def _deal(conn: sqlite3.Connection, deal_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT id, loan_case_id, status FROM deals WHERE id=?", (deal_id,)).fetchone()
    if not row:
        raise NotFound("Deal not found")
    return row


def _doc_on_deal(conn: sqlite3.Connection, deal_id: str, document_id: str | None) -> None:
    if document_id and not conn.execute("SELECT 1 FROM deal_documents WHERE deal_id=? AND document_id=?",
                                        (deal_id, document_id)).fetchone():
        raise ValidationFailure("That document is not attached to this deal.")


# ------------------------------------------------------------------------------------------ conditions
def add_condition(conn: sqlite3.Connection, deal_id: str, c: ConditionIn) -> str:
    deal = _deal(conn, deal_id)
    if c.offer_id and not conn.execute("SELECT 1 FROM offers WHERE id=? AND deal_id=?", (c.offer_id, deal_id)).fetchone():
        raise ValidationFailure("That offer does not belong to this deal.")
    cid, now, actor = new_id(), utcnow(), current_actor()
    with Database.transaction(conn):
        conn.execute("""INSERT INTO deal_conditions(id, deal_id, offer_id, source, description, category, phase, required,
                            responsible, due_on, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (cid, deal_id, c.offer_id, c.source, c.description, c.category, c.phase, int(c.required),
                      c.responsible, c.due_on.isoformat() if c.due_on else None, actor, now, now))
        conn.execute("INSERT INTO deal_condition_events(condition_id, from_status, to_status, note, actor, occurred_at) "
                     "VALUES (?, NULL, 'open', 'Condition recorded', ?, ?)", (cid, actor, now))
        audit.record(conn, "deal_condition_added", "deal", deal_id, deal["loan_case_id"],
                     {"condition_id": cid, "source": c.source, "phase": c.phase, "required": c.required})
    return cid


def add_conditions_from_text(conn: sqlite3.Connection, deal_id: str, text: str, *, offer_id: str | None = None,
                             phase: str = "closing") -> int:
    """One condition per non-empty line (e.g. pasted from a commitment letter)."""
    lines = [ln.strip(" -•\t") for ln in (text or "").splitlines() if ln.strip(" -•\t")]
    if not lines:
        raise ValidationFailure("Paste at least one condition (one per line).")
    with Database.transaction(conn):
        for ln in lines:
            add_condition(conn, deal_id, ConditionIn(description=ln[:500], offer_id=offer_id, phase=phase))  # type: ignore[arg-type]
    return len(lines)


def get_condition(conn: sqlite3.Connection, deal_id: str, condition_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM deal_conditions WHERE id=? AND deal_id=?", (condition_id, deal_id)).fetchone()
    if not row:
        raise NotFound("Condition not found")
    return dict(row)


_COND_NEXT = {"open": ("received", "waived"), "received": ("satisfied", "waived", "open"), "satisfied": (), "waived": ()}


def update_condition(conn: sqlite3.Connection, deal_id: str, condition_id: str, u: ConditionUpdateIn) -> None:
    deal = _deal(conn, deal_id)
    c = get_condition(conn, deal_id, condition_id)
    if u.to_status not in _COND_NEXT[c["status"]]:
        raise ValidationFailure(f"A condition cannot go from {c['status']} to {u.to_status}.")
    if u.to_status in COND_DONE and len(u.by or "") < 2:
        raise ValidationFailure(f"Enter the name of the person who {'satisfied' if u.to_status == 'satisfied' else 'waived'}"
                                " the condition.")
    if u.to_status == "waived" and len(u.note or "") < 10:
        raise ValidationFailure("A waiver needs a reason (at least 10 characters), e.g. who at the lender waived it.")
    if u.to_status == "open" and len(u.note or "") < 5:
        raise ValidationFailure("Say why the item was returned (at least 5 characters).")
    _doc_on_deal(conn, deal_id, u.document_id)
    now = utcnow()
    with Database.transaction(conn):
        conn.execute(
            """UPDATE deal_conditions SET status=?, document_id=COALESCE(?, document_id), resolved_by=?, resolved_at=?,
                   resolution_note=COALESCE(?, resolution_note), updated_at=? WHERE id=?""",
            (u.to_status, u.document_id, u.by if u.to_status in COND_DONE else None,
             now if u.to_status in COND_DONE else None, u.note, now, condition_id))
        conn.execute("INSERT INTO deal_condition_events(condition_id, from_status, to_status, note, actor, occurred_at) "
                     "VALUES (?,?,?,?,?,?)", (condition_id, c["status"], u.to_status, u.note, current_actor(), now))
        audit.record(conn, "deal_condition_updated", "deal", deal_id, deal["loan_case_id"],
                     {"condition_id": condition_id, "from": c["status"], "to": u.to_status, "by": u.by,
                      "document_id": u.document_id})


def list_conditions(conn: sqlite3.Connection, deal_id: str) -> list[dict[str, Any]]:
    rows = [dict(r) for r in conn.execute(
        """SELECT c.*, d.original_filename FROM deal_conditions c LEFT JOIN documents d ON d.id=c.document_id
           WHERE c.deal_id=? ORDER BY CASE c.phase WHEN 'due_diligence' THEN 0 WHEN 'closing' THEN 1 ELSE 2 END,
                 c.status IN ('satisfied','waived'), c.created_at""", (deal_id,))]
    today = date.today().isoformat()
    for r in rows:
        r["overdue"] = r["status"] not in COND_DONE and bool(r["due_on"]) and r["due_on"] < today
        r["events"] = [dict(e) for e in conn.execute(
            "SELECT * FROM deal_condition_events WHERE condition_id=? ORDER BY id", (r["id"],))]
    return rows


def open_required_conditions(conn: sqlite3.Connection, deal_id: str, phases: tuple[str, ...]) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        f"""SELECT * FROM deal_conditions WHERE deal_id=? AND required=1 AND status NOT IN ('satisfied','waived')
            AND phase IN ({','.join('?' * len(phases))})""", (deal_id, *phases))]


# ------------------------------------------------------------------------------------------ due diligence
def add_dd_item(conn: sqlite3.Connection, deal_id: str, d: DDItemIn) -> str:
    deal = _deal(conn, deal_id)
    if d.vendor_organization_id and not conn.execute("SELECT 1 FROM organizations WHERE id=?",
                                                     (d.vendor_organization_id,)).fetchone():
        raise ValidationFailure("Vendor organization does not exist.")
    iid, now, actor = new_id(), utcnow(), current_actor()
    with Database.transaction(conn):
        conn.execute("""INSERT INTO dd_items(id, deal_id, item_type, description, vendor_organization_id, vendor_name,
                            required, due_on, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                     (iid, deal_id, d.item_type, d.description, d.vendor_organization_id, d.vendor_name, int(d.required),
                      d.due_on.isoformat() if d.due_on else None, actor, now, now))
        conn.execute("INSERT INTO dd_item_events(dd_item_id, from_status, to_status, note, actor, occurred_at) "
                     "VALUES (?, NULL, 'not_ordered', 'Item added', ?, ?)", (iid, actor, now))
        audit.record(conn, "dd_item_added", "deal", deal_id, deal["loan_case_id"],
                     {"dd_item_id": iid, "item_type": d.item_type, "required": d.required})
    return iid


_DD_NEXT = {"not_ordered": ("ordered", "waived"), "ordered": ("received", "waived"),
            "received": ("reviewed", "ordered", "waived"), "reviewed": (), "waived": ()}


def get_dd_item(conn: sqlite3.Connection, deal_id: str, item_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM dd_items WHERE id=? AND deal_id=?", (item_id, deal_id)).fetchone()
    if not row:
        raise NotFound("Due-diligence item not found")
    return dict(row)


def update_dd_item(conn: sqlite3.Connection, deal_id: str, item_id: str, u: DDUpdateIn) -> None:
    deal = _deal(conn, deal_id)
    it = get_dd_item(conn, deal_id, item_id)
    if u.to_status not in _DD_NEXT[it["status"]]:
        raise ValidationFailure(f"A due-diligence item cannot go from {it['status'].replace('_', ' ')} to {u.to_status}.")
    today = date.today()
    if u.on and u.on > today:
        raise ValidationFailure("The date cannot be in the future.")
    cols: dict[str, Any] = {"status": u.to_status}
    if u.to_status == "ordered":
        cols["ordered_on"] = (u.on or today).isoformat()
        if u.due_on:
            cols["due_on"] = u.due_on.isoformat()
        if u.vendor_name:
            cols["vendor_name"] = u.vendor_name
        if it["status"] == "received" and len(u.note or "") < 5:
            raise ValidationFailure("Say why the item is being re-ordered (at least 5 characters).")
    elif u.to_status == "received":
        cols["received_on"] = (u.on or today).isoformat()
        _doc_on_deal(conn, deal_id, u.document_id)
        if u.document_id:
            cols["document_id"] = u.document_id
    elif u.to_status == "reviewed":
        if len(u.by or "") < 2:
            raise ValidationFailure("Enter the name of the person who reviewed the report.")
        cols["reviewed_by"] = u.by
    elif u.to_status == "waived":
        if len(u.by or "") < 2 or len(u.note or "") < 10:
            raise ValidationFailure("A waiver needs the person's name and a reason (at least 10 characters).")
        cols["reviewed_by"] = u.by
    if u.note:
        cols["note"] = u.note
    now = utcnow()
    with Database.transaction(conn):
        conn.execute(f"UPDATE dd_items SET {', '.join(f'{k}=?' for k in cols)}, updated_at=? WHERE id=?",
                     (*cols.values(), now, item_id))
        conn.execute("INSERT INTO dd_item_events(dd_item_id, from_status, to_status, note, actor, occurred_at) "
                     "VALUES (?,?,?,?,?,?)", (item_id, it["status"], u.to_status, u.note, current_actor(), now))
        audit.record(conn, "dd_item_updated", "deal", deal_id, deal["loan_case_id"],
                     {"dd_item_id": item_id, "from": it["status"], "to": u.to_status, "by": u.by})


def list_dd_items(conn: sqlite3.Connection, deal_id: str) -> list[dict[str, Any]]:
    rows = [dict(r) for r in conn.execute(
        """SELECT i.*, o.name AS vendor_org, d.original_filename FROM dd_items i
           LEFT JOIN organizations o ON o.id=i.vendor_organization_id LEFT JOIN documents d ON d.id=i.document_id
           WHERE i.deal_id=? ORDER BY i.status IN ('reviewed','waived'), i.due_on IS NULL, i.due_on, i.created_at""",
        (deal_id,))]
    today = date.today().isoformat()
    for r in rows:
        r["overdue"] = r["status"] in ("not_ordered", "ordered") and bool(r["due_on"]) and r["due_on"] < today
    return rows


def overdue_items(conn: sqlite3.Connection, today: date | None = None) -> dict[str, list[dict[str, Any]]]:
    """Across all deals: overdue due-diligence items and conditions (shown on the Tasks page)."""
    t = (today or date.today()).isoformat()
    dd = [dict(r) for r in conn.execute(
        """SELECT i.id, i.deal_id, i.item_type, i.status, i.due_on, i.vendor_name, d.deal_number, d.name AS deal_name
           FROM dd_items i JOIN deals d ON d.id=i.deal_id
           WHERE i.status IN ('not_ordered','ordered') AND i.due_on IS NOT NULL AND i.due_on < ? AND d.archived=0
           ORDER BY i.due_on""", (t,))]
    conds = [dict(r) for r in conn.execute(
        """SELECT c.id, c.deal_id, c.description, c.status, c.due_on, d.deal_number, d.name AS deal_name
           FROM deal_conditions c JOIN deals d ON d.id=c.deal_id
           WHERE c.status IN ('open','received') AND c.due_on IS NOT NULL AND c.due_on < ? AND d.archived=0
           ORDER BY c.due_on""", (t,))]
    return {"dd_items": dd, "conditions": conds}
