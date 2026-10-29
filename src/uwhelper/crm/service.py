"""Organizations, contacts and deals: repository + service functions.

* All writes run in a transaction and record an audit event (changed field names with before/after values;
  free-text notes are recorded only as "changed").
* Deals follow an explicit status machine with append-only history (``deal_status_history``).
  Milestone 1 enforces valid transitions, reasons and human authorization for APPROVED / FUNDED / DECLINED;
  stage gates (submissions, offers, conditions) arrive with the brokerage milestone.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any, BinaryIO

from uwhelper.config import Settings
from uwhelper.crm.models import (
    PRODUCT_TYPES,
    ContactIn,
    ContactPatch,
    DealContactIn,
    DealIn,
    DealPatch,
    DealStatusIn,
    OrganizationIn,
    OrganizationPatch,
)
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.domain.money import dstr
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure

MAX_PAGE = 200
TEXT_FIELDS = {"notes", "collateral_summary", "loan_purpose"}

DEAL_STATUSES = ("INTAKE", "UNDERWRITING", "STRUCTURING", "LENDER_SUBMISSION", "APPROVED", "DUE_DILIGENCE",
                 "CLOSING", "FUNDED", "DECLINED", "WITHDRAWN", "ON_HOLD", "CANCELLED")
_EXITS = ["ON_HOLD", "WITHDRAWN", "CANCELLED"]
DEAL_TRANSITIONS: dict[str, list[str]] = {
    "INTAKE": ["UNDERWRITING", "DECLINED", *_EXITS],
    "UNDERWRITING": ["STRUCTURING", "INTAKE", "DECLINED", *_EXITS],
    "STRUCTURING": ["LENDER_SUBMISSION", "UNDERWRITING", "DECLINED", *_EXITS],
    "LENDER_SUBMISSION": ["APPROVED", "STRUCTURING", "DECLINED", *_EXITS],
    "APPROVED": ["DUE_DILIGENCE", "LENDER_SUBMISSION", *_EXITS],
    "DUE_DILIGENCE": ["CLOSING", "APPROVED", *_EXITS],
    "CLOSING": ["FUNDED", "DUE_DILIGENCE", *_EXITS],
    "FUNDED": [],
    "ON_HOLD": ["RESUME", "WITHDRAWN", "CANCELLED"],
    "DECLINED": ["INTAKE"],
    "WITHDRAWN": ["INTAKE"],
    "CANCELLED": ["INTAKE"],
}
NEEDS_AUTHORIZATION = {"APPROVED", "FUNDED", "DECLINED"}


def _page(limit: int, offset: int) -> tuple[int, int]:
    if not (1 <= limit <= MAX_PAGE) or offset < 0:
        raise ValidationFailure(f"limit must be 1-{MAX_PAGE} and offset >= 0")
    return limit, offset


def _changes(before: dict[str, Any], patch: dict[str, Any]) -> dict[str, list]:
    out = {}
    for k, v in patch.items():
        old = before.get(k)
        new = int(v) if isinstance(v, bool) else (dstr(v) if isinstance(v, Decimal) else
                                                  (v.isoformat() if isinstance(v, date) else v))
        if old != new:
            out[k] = ["(changed)", "(changed)"] if k in TEXT_FIELDS else [old, new]
    return out


def _unique_violation(exc: sqlite3.IntegrityError, what: str) -> ValidationFailure:
    if "UNIQUE" in str(exc):
        return ValidationFailure(f"A matching {what} already exists.")
    raise exc


# ------------------------------------------------------------------------------------------ organizations
def create_organization(conn: sqlite3.Connection, o: OrganizationIn) -> str:
    oid, now = new_id(), utcnow()
    data = o.model_dump()
    try:
        with Database.transaction(conn):
            cols = ", ".join(data)
            conn.execute(f"INSERT INTO organizations(id, {cols}, created_at, updated_at) "
                         f"VALUES (?, {', '.join('?' * len(data))}, ?, ?)", (oid, *data.values(), now, now))
            audit.record(conn, "organization_created", "organization", oid, None,
                         {"org_type": o.org_type, "name": o.name})
    except sqlite3.IntegrityError as exc:
        raise _unique_violation(exc, "organization (same name and type)") from None
    return oid


def get_organization(conn: sqlite3.Connection, org_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM organizations WHERE id=?", (org_id,)).fetchone()
    if not row:
        raise NotFound("Organization not found")
    org = dict(row)
    org["contacts"] = [dict(r) for r in conn.execute(
        """SELECT c.*, oc.role, oc.is_primary FROM organization_contacts oc JOIN contacts c ON c.id=oc.contact_id
           WHERE oc.organization_id=? ORDER BY oc.is_primary DESC, c.last_name""", (org_id,))]
    org["deals"] = [dict(r) for r in conn.execute(
        "SELECT id, deal_number, name, status FROM deals WHERE borrower_organization_id=? ORDER BY created_at DESC",
        (org_id,))]
    return org


def list_organizations(conn: sqlite3.Connection, *, q: str | None = None, org_type: str | None = None,
                       include_archived: bool = False, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    limit, offset = _page(limit, offset)
    sql, params = "SELECT * FROM organizations WHERE 1=1", []
    if not include_archived:
        sql += " AND archived=0"
    if org_type:
        sql += " AND org_type=?"
        params.append(org_type)
    if q:
        sql += " AND name LIKE ? ESCAPE '\\'"
        params.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    sql += " ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?"
    return [dict(r) for r in conn.execute(sql, (*params, limit, offset))]


def update_organization(conn: sqlite3.Connection, org_id: str, p: OrganizationPatch) -> dict[str, list]:
    before = get_organization(conn, org_id)
    patch = p.model_dump(exclude_unset=True)
    changes = _changes(before, patch)
    if not changes:
        return {}
    values = {k: (int(v) if isinstance(v, bool) else v) for k, v in patch.items() if k in changes}
    try:
        with Database.transaction(conn):
            conn.execute(f"UPDATE organizations SET {', '.join(f'{k}=?' for k in values)}, updated_at=? WHERE id=?",
                         (*values.values(), utcnow(), org_id))
            audit.record(conn, "organization_updated", "organization", org_id, None, {"changed": changes})
    except sqlite3.IntegrityError as exc:
        raise _unique_violation(exc, "organization (same name and type)") from None
    return changes


# ------------------------------------------------------------------------------------------ contacts
def _require(conn, table: str, id_: str | None, what: str) -> None:
    if id_ and not conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (id_,)).fetchone():
        raise ValidationFailure(f"{what} {id_[:8]}… does not exist.")


def create_contact(conn: sqlite3.Connection, c: ContactIn) -> str:
    _require(conn, "organizations", c.organization_id, "Organization")
    cid, now = new_id(), utcnow()
    data = c.model_dump(exclude={"organization_id", "organization_role"})
    try:
        with Database.transaction(conn):
            conn.execute(f"INSERT INTO contacts(id, {', '.join(data)}, created_at, updated_at) "
                         f"VALUES (?, {', '.join('?' * len(data))}, ?, ?)", (cid, *data.values(), now, now))
            if c.organization_id:
                conn.execute("INSERT INTO organization_contacts(organization_id, contact_id, role, is_primary, created_at) "
                             "VALUES (?,?,?,?,?)", (c.organization_id, cid, c.organization_role or "member", 0, now))
            audit.record(conn, "contact_created", "contact", cid, None,
                         {"contact_type": c.contact_type, "organization_id": c.organization_id})
    except sqlite3.IntegrityError as exc:
        raise _unique_violation(exc, "contact (same email)") from None
    return cid


def get_contact(conn: sqlite3.Connection, contact_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
    if not row:
        raise NotFound("Contact not found")
    c = dict(row)
    c["organizations"] = [dict(r) for r in conn.execute(
        """SELECT o.id, o.name, o.org_type, oc.role, oc.is_primary FROM organization_contacts oc
           JOIN organizations o ON o.id=oc.organization_id WHERE oc.contact_id=? ORDER BY o.name""", (contact_id,))]
    c["deals"] = [dict(r) for r in conn.execute(
        """SELECT d.id, d.deal_number, d.name, d.status, dc.role FROM deal_contacts dc JOIN deals d ON d.id=dc.deal_id
           WHERE dc.contact_id=? ORDER BY d.created_at DESC""", (contact_id,))]
    return c


def list_contacts(conn: sqlite3.Connection, *, q: str | None = None, contact_type: str | None = None,
                  include_archived: bool = False, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    limit, offset = _page(limit, offset)
    sql, params = """SELECT c.*, (SELECT o.name FROM organization_contacts oc JOIN organizations o ON o.id=oc.organization_id
                      WHERE oc.contact_id=c.id ORDER BY oc.is_primary DESC LIMIT 1) AS organization_name
                     FROM contacts c WHERE 1=1""", []
    if not include_archived:
        sql += " AND c.archived=0"
    if contact_type:
        sql += " AND c.contact_type=?"
        params.append(contact_type)
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql += " AND (c.first_name LIKE ? ESCAPE '\\' OR c.last_name LIKE ? ESCAPE '\\' OR c.email LIKE ? ESCAPE '\\')"
        params += [like, like, like]
    sql += " ORDER BY c.last_name COLLATE NOCASE, c.first_name COLLATE NOCASE LIMIT ? OFFSET ?"
    return [dict(r) for r in conn.execute(sql, (*params, limit, offset))]


def update_contact(conn: sqlite3.Connection, contact_id: str, p: ContactPatch) -> dict[str, list]:
    before = get_contact(conn, contact_id)
    patch = p.model_dump(exclude_unset=True)
    for k in ("first_name", "last_name", "contact_type"):
        if k in patch and patch[k] is None:
            raise ValidationFailure(f"{k} cannot be cleared")
    changes = _changes(before, patch)
    if not changes:
        return {}
    values = {k: (int(v) if isinstance(v, bool) else v) for k, v in patch.items() if k in changes}
    try:
        with Database.transaction(conn):
            conn.execute(f"UPDATE contacts SET {', '.join(f'{k}=?' for k in values)}, updated_at=? WHERE id=?",
                         (*values.values(), utcnow(), contact_id))
            audit.record(conn, "contact_updated", "contact", contact_id, None, {"changed": changes})
    except sqlite3.IntegrityError as exc:
        raise _unique_violation(exc, "contact (same email)") from None
    return changes


def link_contact_organization(conn: sqlite3.Connection, contact_id: str, org_id: str, role: str = "member",
                              is_primary: bool = False) -> None:
    get_contact(conn, contact_id)
    get_organization(conn, org_id)
    with Database.transaction(conn):
        conn.execute("INSERT OR REPLACE INTO organization_contacts(organization_id, contact_id, role, is_primary, created_at) "
                     "VALUES (?,?,?,?,?)", (org_id, contact_id, (role or "member")[:60], int(is_primary), utcnow()))
        audit.record(conn, "contact_linked_to_organization", "contact", contact_id, None,
                     {"organization_id": org_id, "role": role})


# ------------------------------------------------------------------------------------------ deals
def _next_deal_number(conn: sqlite3.Connection) -> str:
    prefix = f"DL-{date.today().year}-"
    row = conn.execute("SELECT deal_number FROM deals WHERE deal_number LIKE ? ORDER BY deal_number DESC LIMIT 1",
                       (prefix + "%",)).fetchone()
    n = int(row["deal_number"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}{n:04d}"


def _check_refs(conn, borrower_org: str | None, primary: str | None, referral: str | None) -> None:
    _require(conn, "organizations", borrower_org, "Borrower organization")
    _require(conn, "contacts", primary, "Primary contact")
    _require(conn, "contacts", referral, "Referral contact")


def create_deal(conn: sqlite3.Connection, d: DealIn) -> str:
    try:
        d.check_product()
    except ValueError as exc:
        raise ValidationFailure(str(exc)) from None
    _check_refs(conn, d.borrower_organization_id, d.primary_contact_id, d.referral_source_contact_id)
    did, now = new_id(), utcnow()
    with Database.transaction(conn):
        number = _next_deal_number(conn)
        conn.execute(
            """INSERT INTO deals(id, deal_number, name, deal_type, product_type, loan_purpose, requested_amount,
                   collateral_summary, borrower_organization_id, primary_contact_id, referral_source_contact_id,
                   target_close_date, notes, status, status_changed_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'INTAKE', ?,?,?)""",
            (did, number, d.name, d.deal_type, d.product_type, d.loan_purpose, dstr(d.requested_amount),
             d.collateral_summary, d.borrower_organization_id, d.primary_contact_id, d.referral_source_contact_id,
             d.target_close_date.isoformat() if d.target_close_date else None, d.notes, now, now, now))
        conn.execute("INSERT INTO deal_status_history(deal_id, from_status, to_status, reason, occurred_at) "
                     "VALUES (?, NULL, 'INTAKE', 'Deal created', ?)", (did, now))
        for cid, role in ((d.primary_contact_id, "borrower"), (d.referral_source_contact_id, "referral_partner")):
            if cid:
                conn.execute("INSERT OR IGNORE INTO deal_contacts(deal_id, contact_id, role, created_at) VALUES (?,?,?,?)",
                             (did, cid, role, now))
        audit.record(conn, "deal_created", "deal", did, None,
                     {"deal_number": number, "deal_type": d.deal_type, "product_type": d.product_type,
                      "requested_amount": dstr(d.requested_amount)})
    return did


def get_deal(conn: sqlite3.Connection, deal_id: str) -> dict[str, Any]:
    row = conn.execute(
        """SELECT d.*, o.name AS borrower_name, pc.first_name || ' ' || pc.last_name AS primary_contact_name,
                  lc.case_number, lc.stage AS case_stage
           FROM deals d LEFT JOIN organizations o ON o.id=d.borrower_organization_id
           LEFT JOIN contacts pc ON pc.id=d.primary_contact_id LEFT JOIN loan_cases lc ON lc.id=d.loan_case_id
           WHERE d.id=?""", (deal_id,)).fetchone()
    if not row:
        raise NotFound("Deal not found")
    deal = dict(row)
    deal["contacts"] = [dict(r) for r in conn.execute(
        """SELECT c.id, c.first_name, c.last_name, c.email, c.phone, dc.role FROM deal_contacts dc
           JOIN contacts c ON c.id=dc.contact_id WHERE dc.deal_id=? ORDER BY dc.role, c.last_name""", (deal_id,))]
    deal["documents"] = [dict(r) for r in conn.execute(
        """SELECT doc.id, doc.original_filename, doc.size_bytes, doc.sha256, doc.page_count, doc.processing_status,
                  dd.doc_type, dd.added_at FROM deal_documents dd JOIN documents doc ON doc.id=dd.document_id
           WHERE dd.deal_id=? ORDER BY dd.added_at""", (deal_id,))]
    deal["history"] = [dict(r) for r in conn.execute(
        "SELECT * FROM deal_status_history WHERE deal_id=? ORDER BY id", (deal_id,))]
    deal["allowed_transitions"] = [t for t in DEAL_TRANSITIONS.get(deal["status"], [])]
    return deal


def list_deals(conn: sqlite3.Connection, *, status: str | None = None, deal_type: str | None = None,
               q: str | None = None, include_archived: bool = False, limit: int = 50,
               offset: int = 0) -> list[dict[str, Any]]:
    limit, offset = _page(limit, offset)
    sql = """SELECT d.*, o.name AS borrower_name, lc.case_number, lc.stage AS case_stage FROM deals d
             LEFT JOIN organizations o ON o.id=d.borrower_organization_id
             LEFT JOIN loan_cases lc ON lc.id=d.loan_case_id WHERE 1=1"""
    params: list[Any] = []
    if not include_archived:
        sql += " AND d.archived=0"
    if status:
        if status not in DEAL_STATUSES:
            raise ValidationFailure("unknown status")
        sql += " AND d.status=?"
        params.append(status)
    if deal_type:
        sql += " AND d.deal_type=?"
        params.append(deal_type)
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql += " AND (d.name LIKE ? ESCAPE '\\' OR d.deal_number LIKE ? ESCAPE '\\' OR o.name LIKE ? ESCAPE '\\')"
        params += [like, like, like]
    sql += " ORDER BY d.updated_at DESC LIMIT ? OFFSET ?"
    return [dict(r) for r in conn.execute(sql, (*params, limit, offset))]


def update_deal(conn: sqlite3.Connection, deal_id: str, p: DealPatch) -> dict[str, list]:
    before = get_deal(conn, deal_id)
    if before["status"] == "FUNDED":
        raise ValidationFailure("Funded deals are closed for edits; record changes in servicing.")
    patch = p.model_dump(exclude_unset=True)
    if patch.get("name", "x") is None:
        raise ValidationFailure("name cannot be cleared")
    if patch.get("product_type") and patch["product_type"] not in PRODUCT_TYPES[before["deal_type"]]:
        raise ValidationFailure(f"product_type must be one of {', '.join(PRODUCT_TYPES[before['deal_type']])}")
    _check_refs(conn, patch.get("borrower_organization_id"), patch.get("primary_contact_id"),
                patch.get("referral_source_contact_id"))
    changes = _changes(before, patch)
    if not changes:
        return {}
    values = {}
    for k in changes:
        v = patch[k]
        values[k] = int(v) if isinstance(v, bool) else (dstr(v) if isinstance(v, Decimal) else
                                                       (v.isoformat() if isinstance(v, date) else v))
    with Database.transaction(conn):
        conn.execute(f"UPDATE deals SET {', '.join(f'{k}=?' for k in values)}, updated_at=? WHERE id=?",
                     (*values.values(), utcnow(), deal_id))
        audit.record(conn, "deal_updated", "deal", deal_id, before.get("loan_case_id"), {"changed": changes})
    return changes


def transition_deal(conn: sqlite3.Connection, deal_id: str, s: DealStatusIn) -> str:
    deal = get_deal(conn, deal_id)
    current = deal["status"]
    if s.to_status not in DEAL_TRANSITIONS.get(current, []):
        raise ValidationFailure(f"{current} → {s.to_status} is not an allowed deal transition.")
    target = (deal["hold_return_status"] or "INTAKE") if s.to_status == "RESUME" else s.to_status
    if target in NEEDS_AUTHORIZATION and s.to_status != "RESUME":  # resuming restores an already-authorized status
        if not s.authorized_by or len(s.authorized_by) < 2:
            raise ValidationFailure(f"{target} requires the name of the person who authorized it.")
        if not s.confirm:
            raise ValidationFailure(f"Confirm that {target} reflects a human decision recorded outside the software.")
    if target == "UNDERWRITING" and not (deal["borrower_organization_id"] or deal["primary_contact_id"]):
        raise ValidationFailure("Add a borrower organization or primary contact before underwriting.")
    if target == "UNDERWRITING" and not deal["requested_amount"]:
        raise ValidationFailure("Enter the requested amount before underwriting.")
    now = utcnow()
    with Database.transaction(conn):
        conn.execute("UPDATE deals SET status=?, status_changed_at=?, hold_return_status=?, updated_at=? WHERE id=?",
                     (target, now, current if target == "ON_HOLD" else None, now, deal_id))
        conn.execute("INSERT INTO deal_status_history(deal_id, from_status, to_status, reason, authorized_by, occurred_at) "
                     "VALUES (?,?,?,?,?,?)", (deal_id, current, target, s.reason, s.authorized_by, now))
        audit.record(conn, "deal_status_changed", "deal", deal_id, deal.get("loan_case_id"),
                     {"from": current, "to": target, "reason": s.reason, "authorized_by": s.authorized_by})
    return target


def add_deal_contact(conn: sqlite3.Connection, deal_id: str, dc: DealContactIn) -> None:
    deal = get_deal(conn, deal_id)
    get_contact(conn, dc.contact_id)
    with Database.transaction(conn):
        cur = conn.execute("INSERT OR IGNORE INTO deal_contacts(deal_id, contact_id, role, created_at) VALUES (?,?,?,?)",
                           (deal_id, dc.contact_id, dc.role, utcnow()))
        if cur.rowcount:
            audit.record(conn, "deal_contact_added", "deal", deal_id, deal.get("loan_case_id"),
                         {"contact_id": dc.contact_id, "role": dc.role})


def remove_deal_contact(conn: sqlite3.Connection, deal_id: str, contact_id: str, role: str) -> None:
    deal = get_deal(conn, deal_id)
    with Database.transaction(conn):
        cur = conn.execute("DELETE FROM deal_contacts WHERE deal_id=? AND contact_id=? AND role=?",
                           (deal_id, contact_id, role))
        if not cur.rowcount:
            raise NotFound("That contact/role is not on this deal")
        audit.record(conn, "deal_contact_removed", "deal", deal_id, deal.get("loan_case_id"),
                     {"contact_id": contact_id, "role": role})


def attach_document(conn: sqlite3.Connection, settings: Settings, deal_id: str, stream: BinaryIO,
                    filename: str | None, doc_type: str = "other") -> dict[str, Any]:
    from uwhelper.documents.service import ingest_file
    from uwhelper.modules import get_module

    deal = get_deal(conn, deal_id)
    if doc_type not in get_module("commercial").document_types():
        raise ValidationFailure(f"Unknown document type {doc_type!r}")
    res = ingest_file(conn, settings, stream, filename)
    with Database.transaction(conn):
        cur = conn.execute("INSERT OR IGNORE INTO deal_documents(deal_id, document_id, doc_type, added_at) VALUES (?,?,?,?)",
                           (deal_id, res["document_id"], doc_type, utcnow()))
        audit.record(conn, "deal_document_attached", "deal", deal_id, deal.get("loan_case_id"),
                     {"document_id": res["document_id"], "doc_type": doc_type, "duplicate": res["duplicate"],
                      "already_attached": not cur.rowcount})
    return {**res, "already_attached": not cur.rowcount}


LOAN_TYPE_FOR_PRODUCT = {
    "equipment": "equipment", "business_acquisition": "business_acquisition", "working_capital": "working_capital",
    "line_of_credit": "working_capital", "term_loan": "general_commercial", "sba_7a": "general_commercial",
    "sba_504": "general_commercial", "other": "general_commercial",
}


def start_underwriting_file(conn: sqlite3.Connection, deal_id: str, loan_type: str | None = None) -> str:
    """Create a linked underwriting file (loan case) from the deal; copies, never moves, deal data."""
    from uwhelper.domain.models import BorrowerIn, CaseIn, TermsIn
    from uwhelper.repo.cases import create_case

    deal = get_deal(conn, deal_id)
    if deal["loan_case_id"]:
        raise ValidationFailure("This deal already has an underwriting file.")
    if not deal["borrower_name"]:
        raise ValidationFailure("Set the borrower organization first (its name becomes the borrower legal name).")
    lt = loan_type or ("commercial_real_estate" if deal["deal_type"] == "cre"
                       else LOAN_TYPE_FOR_PRODUCT.get(deal["product_type"], "general_commercial"))
    terms = TermsIn(requested_amount=deal["requested_amount"], loan_purpose=deal["loan_purpose"]) \
        if deal["requested_amount"] else None
    with Database.transaction(conn):
        case_id = create_case(conn, CaseIn(title=f"{deal['deal_number']} {deal['name']}"[:200], loan_type=lt,
                                           notes=f"Created from deal {deal['deal_number']}."),
                              BorrowerIn(legal_name=deal["borrower_name"]), terms)
        conn.execute("UPDATE deals SET loan_case_id=?, updated_at=? WHERE id=?", (case_id, utcnow(), deal_id))
        for d in deal["documents"]:
            conn.execute("INSERT OR IGNORE INTO case_documents(case_id, document_id, doc_type, added_at) VALUES (?,?,?,?)",
                         (case_id, d["id"], d["doc_type"], utcnow()))
        audit.record(conn, "deal_underwriting_file_created", "deal", deal_id, case_id,
                     {"loan_type": lt, "documents_linked": len(deal["documents"])})
    return case_id


def pipeline_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    totals: dict[str, Decimal] = {}
    for r in conn.execute("SELECT status, requested_amount FROM deals WHERE archived=0"):
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r["requested_amount"]:
            totals[r["status"]] = totals.get(r["status"], Decimal(0)) + Decimal(r["requested_amount"])
    return [{"status": s, "count": counts.get(s, 0), "requested_total": totals.get(s, Decimal(0))}
            for s in DEAL_STATUSES]
