"""Document requests (needed-items checklist) per deal.

States: requested → received (a file is attached) → accepted / rejected (a named person reviews it);
any unfulfilled item can be waived with a reason. A rejected item goes back to "received" when a new file arrives.
Checklist items come from the versioned ``checklists/deal_documents.yaml``; each request remembers the checklist
version it was generated from. Files are stored through the normal document service and are also linked to the
deal's underwriting file (if any), so the file's document list stays complete.
"""

from __future__ import annotations

import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any, BinaryIO

import yaml
from pydantic import BaseModel, ConfigDict, Field

from uwhelper.config import Settings
from uwhelper.crm.models import DocumentRequestIn, DocumentReviewIn
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure

CHECKLIST_FILE = Path(__file__).parent / "checklists" / "deal_documents.yaml"
STATUSES = ("requested", "received", "accepted", "rejected", "waived")
OPEN = ("requested", "received", "rejected")


class ChecklistItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z0-9_]{2,60}$")
    label: str = Field(min_length=2, max_length=200)
    doc_type: str
    required: bool = True


class Checklist(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    version: str
    common: dict[str, list[ChecklistItem]]
    products: dict[str, list[ChecklistItem]] = {}


@lru_cache(maxsize=4)
def load_checklist(path: Path = CHECKLIST_FILE) -> Checklist:
    from uwhelper.modules import get_module

    cl = Checklist.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    known = get_module("commercial").document_types()
    for items in [*cl.common.values(), *cl.products.values()]:
        for it in items:
            if it.doc_type not in known:
                raise ValueError(f"checklist item {it.key}: unknown doc_type {it.doc_type!r}")
    return cl


def checklist_items(deal_type: str, product_type: str) -> list[ChecklistItem]:
    cl = load_checklist()
    items: dict[str, ChecklistItem] = {i.key: i for i in cl.common.get(deal_type, [])}
    for i in cl.products.get(product_type, []):
        items[i.key] = i  # product-specific entry wins
    return list(items.values())


def _deal(conn: sqlite3.Connection, deal_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT id, deal_number, deal_type, product_type, loan_case_id, status FROM deals WHERE id=?",
                       (deal_id,)).fetchone()
    if not row:
        raise NotFound("Deal not found")
    return dict(row)


def generate_requests(conn: sqlite3.Connection, deal_id: str) -> int:
    """Add the checklist items for this deal's type/product that are not on the deal yet. Returns how many."""
    deal = _deal(conn, deal_id)
    cl = load_checklist()
    now, added = utcnow(), []
    with Database.transaction(conn):
        for it in checklist_items(deal["deal_type"], deal["product_type"]):
            cur = conn.execute(
                """INSERT OR IGNORE INTO document_requests(id, deal_id, item_key, label, doc_type, required, checklist_key,
                       checklist_version, status, requested_at, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,'requested',?,?,?)""",
                (new_id(), deal_id, it.key, it.label, it.doc_type, int(it.required), cl.key, cl.version, now, now, now))
            if cur.rowcount:
                added.append(it.key)
        if added:
            audit.record(conn, "document_requests_generated", "deal", deal_id, deal["loan_case_id"],
                         {"checklist": cl.key, "version": cl.version, "items": added})
    return len(added)


def add_request(conn: sqlite3.Connection, deal_id: str, r: DocumentRequestIn) -> str:
    from uwhelper.modules import get_module

    deal = _deal(conn, deal_id)
    if r.doc_type not in get_module("commercial").document_types():
        raise ValidationFailure(f"Unknown document type {r.doc_type!r}")
    rid, now = new_id(), utcnow()
    key = "manual_" + (re.sub(r"[^a-z0-9]+", "_", r.label.lower()).strip("_")[:40] or "item") + "_" + rid[:6]
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO document_requests(id, deal_id, item_key, label, doc_type, required, status, note,
                   requested_at, created_at, updated_at) VALUES (?,?,?,?,?,?,'requested',?,?,?,?)""",
            (rid, deal_id, key, r.label, r.doc_type, int(r.required), r.note, now, now, now))
        audit.record(conn, "document_request_added", "deal", deal_id, deal["loan_case_id"],
                     {"request_id": rid, "label": r.label, "required": r.required})
    return rid


def get_request(conn: sqlite3.Connection, deal_id: str, request_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM document_requests WHERE id=? AND deal_id=?", (request_id, deal_id)).fetchone()
    if not row:
        raise NotFound("Document request not found")
    return dict(row)


def list_requests(conn: sqlite3.Connection, deal_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        """SELECT r.*, d.original_filename, d.sha256 FROM document_requests r LEFT JOIN documents d ON d.id=r.document_id
           WHERE r.deal_id=? ORDER BY r.required DESC,
                 CASE r.status WHEN 'requested' THEN 0 WHEN 'rejected' THEN 1 WHEN 'received' THEN 2 ELSE 3 END,
                 r.created_at, r.label""", (deal_id,))]


def summary(conn: sqlite3.Connection, deal_id: str) -> dict[str, int]:
    out = {s: 0 for s in STATUSES}
    for r in conn.execute("SELECT status, COUNT(*) AS n FROM document_requests WHERE deal_id=? GROUP BY status", (deal_id,)):
        out[r["status"]] = r["n"]
    out["required_outstanding"] = conn.execute(
        "SELECT COUNT(*) FROM document_requests WHERE deal_id=? AND required=1 AND status IN ('requested','received',"
        "'rejected')", (deal_id,)).fetchone()[0]
    out["total"] = sum(out[s] for s in STATUSES)
    return out


def _mark_received(conn: sqlite3.Connection, deal: dict[str, Any], req: dict[str, Any], document_id: str) -> None:
    if req["status"] in ("accepted", "waived"):
        raise ValidationFailure(f"This item is already {req['status']}; add a new request for another file.")
    now = utcnow()
    with Database.transaction(conn):
        conn.execute("UPDATE document_requests SET status='received', document_id=?, received_at=?, reviewed_by=NULL, "
                     "reviewed_at=NULL, updated_at=? WHERE id=?", (document_id, now, now, req["id"]))
        audit.record(conn, "document_request_received", "deal", deal["id"], deal["loan_case_id"],
                     {"request_id": req["id"], "item": req["item_key"], "document_id": document_id,
                      "previous_status": req["status"]})


def fulfil_with_upload(conn: sqlite3.Connection, settings: Settings, deal_id: str, request_id: str, stream: BinaryIO,
                       filename: str | None) -> dict[str, Any]:
    from uwhelper.crm.service import attach_document

    deal = _deal(conn, deal_id)
    req = get_request(conn, deal_id, request_id)
    if req["status"] in ("accepted", "waived"):
        raise ValidationFailure(f"This item is already {req['status']}; add a new request for another file.")
    res = attach_document(conn, settings, deal_id, stream, filename, req["doc_type"])
    _mark_received(conn, deal, req, res["document_id"])
    return res


def fulfil_with_existing(conn: sqlite3.Connection, deal_id: str, request_id: str, document_id: str) -> None:
    deal = _deal(conn, deal_id)
    req = get_request(conn, deal_id, request_id)
    if not conn.execute("SELECT 1 FROM deal_documents WHERE deal_id=? AND document_id=?", (deal_id, document_id)).fetchone():
        raise ValidationFailure("That document is not attached to this deal.")
    _mark_received(conn, deal, req, document_id)


def review_request(conn: sqlite3.Connection, deal_id: str, request_id: str, r: DocumentReviewIn) -> None:
    deal = _deal(conn, deal_id)
    req = get_request(conn, deal_id, request_id)
    if r.decision in ("accepted", "rejected") and req["status"] != "received":
        raise ValidationFailure("Only a received document can be accepted or rejected.")
    if r.decision == "rejected" and len((r.note or "").strip()) < 5:
        raise ValidationFailure("Say why the document was rejected (what is missing or wrong).")
    if r.decision == "waived":
        if req["status"] not in OPEN:
            raise ValidationFailure(f"This item is already {req['status']}.")
        if len((r.note or "").strip()) < 10:
            raise ValidationFailure("A waiver needs a reason of at least 10 characters.")
    now = utcnow()
    with Database.transaction(conn):
        conn.execute("UPDATE document_requests SET status=?, reviewed_by=?, reviewed_at=?, note=COALESCE(?, note), "
                     "updated_at=? WHERE id=?", (r.decision, r.reviewed_by, now, r.note, now, request_id))
        audit.record(conn, f"document_request_{r.decision}", "deal", deal_id, deal["loan_case_id"],
                     {"request_id": request_id, "item": req["item_key"], "reviewed_by": r.reviewed_by,
                      "document_id": req["document_id"]})
