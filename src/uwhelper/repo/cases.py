"""Loan cases, borrowers, parties, loan terms, debts and collateral."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.domain.models import BorrowerIn, CaseIn, CollateralIn, DebtIn, PartyIn, TermsIn, ValuationIn
from uwhelper.domain.money import dstr
from uwhelper.modules import get_module
from uwhelper.repo import audit


class NotFound(LookupError):
    pass


class ValidationFailure(ValueError):
    pass


def _next_case_number(conn: sqlite3.Connection) -> str:
    year = date.today().year
    prefix = f"UW-{year}-"
    row = conn.execute(
        "SELECT case_number FROM loan_cases WHERE case_number LIKE ? ORDER BY case_number DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    n = int(row["case_number"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}{n:04d}"


def _check_loan_type(module_key: str, loan_type: str) -> None:
    module = get_module(module_key)
    spec = module.loan_types.get(loan_type)
    if spec is None:
        raise ValidationFailure(f"Unknown loan type {loan_type!r}")
    if not spec.implemented:
        raise ValidationFailure(f"{spec.label} is a later extension and is not implemented yet.")


def create_case(conn: sqlite3.Connection, case: CaseIn, borrower: BorrowerIn, terms: TermsIn | None = None) -> str:
    _check_loan_type("commercial", case.loan_type)
    case_id, now = new_id(), utcnow()
    with Database.transaction(conn):
        number = _next_case_number(conn)
        conn.execute(
            """INSERT INTO loan_cases(id, case_number, title, module_key, loan_type, policy_key, policy_version,
                   is_synthetic, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (case_id, number, case.title, "commercial", case.loan_type, case.policy_key, case.policy_version,
             int(case.is_synthetic), case.notes, now, now),
        )
        conn.execute(
            """INSERT INTO borrowers(id, case_id, legal_name, dba_name, entity_type, industry, naics_code,
                   state_of_formation, year_established, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id(), case_id, borrower.legal_name, borrower.dba_name, borrower.entity_type, borrower.industry,
             borrower.naics_code, borrower.state_of_formation, borrower.year_established, now, now),
        )
        audit.record(conn, "case_created", "loan_case", case_id, case_id,
                     {"case_number": number, "loan_type": case.loan_type, "policy_key": case.policy_key})
        if terms is not None:
            save_terms(conn, case_id, terms)
    return case_id


def update_case(conn: sqlite3.Connection, case_id: str, case: CaseIn, borrower: BorrowerIn) -> None:
    _check_loan_type("commercial", case.loan_type)
    before = get_case(conn, case_id)
    now = utcnow()
    with Database.transaction(conn):
        conn.execute(
            """UPDATE loan_cases SET title=?, loan_type=?, policy_key=?, policy_version=?, notes=?, updated_at=?
               WHERE id=?""",
            (case.title, case.loan_type, case.policy_key, case.policy_version, case.notes, now, case_id),
        )
        conn.execute(
            """UPDATE borrowers SET legal_name=?, dba_name=?, entity_type=?, industry=?, naics_code=?,
                   state_of_formation=?, year_established=?, updated_at=? WHERE case_id=?""",
            (borrower.legal_name, borrower.dba_name, borrower.entity_type, borrower.industry, borrower.naics_code,
             borrower.state_of_formation, borrower.year_established, now, case_id),
        )
        changed = {k: [before[k], v] for k, v in case.model_dump().items() if k in before and before[k] != v}
        audit.record(conn, "case_updated", "loan_case", case_id, case_id, {"changed": changed})


def set_case_status(conn: sqlite3.Connection, case_id: str, status: str) -> None:
    if status not in ("open", "in_review", "archived"):
        raise ValidationFailure("invalid status")
    with Database.transaction(conn):
        conn.execute("UPDATE loan_cases SET status=?, updated_at=? WHERE id=?", (status, utcnow(), case_id))
        audit.record(conn, "case_status_changed", "loan_case", case_id, case_id, {"status": status})


def set_base_period(conn: sqlite3.Connection, case_id: str, period_id: str | None) -> None:
    if period_id:
        row = conn.execute("SELECT id FROM financial_periods WHERE id=? AND case_id=?", (period_id, case_id)).fetchone()
        if not row:
            raise NotFound("period not found")
    with Database.transaction(conn):
        conn.execute("UPDATE loan_cases SET base_period_id=?, updated_at=? WHERE id=?", (period_id, utcnow(), case_id))
        audit.record(conn, "base_period_set", "loan_case", case_id, case_id, {"period_id": period_id})


def get_case(conn: sqlite3.Connection, case_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM loan_cases WHERE id = ?", (case_id,)).fetchone()
    if not row:
        raise NotFound("Loan case not found")
    case = dict(row)
    b = conn.execute("SELECT * FROM borrowers WHERE case_id = ?", (case_id,)).fetchone()
    case["borrower"] = dict(b) if b else {}
    case["terms"] = current_terms(conn, case_id)
    return case


def list_cases(conn: sqlite3.Connection, include_archived: bool = True) -> list[dict[str, Any]]:
    sql = """SELECT c.*, b.legal_name,
                (SELECT requested_amount FROM loan_terms t WHERE t.case_id=c.id AND t.is_current=1) AS requested_amount,
                (SELECT COUNT(*) FROM case_documents d WHERE d.case_id=c.id) AS document_count,
                (SELECT overall_status FROM underwriting_runs r WHERE r.case_id=c.id AND r.scenario_id IS NULL
                   ORDER BY run_number DESC LIMIT 1) AS last_status,
                (SELECT COUNT(*) FROM facts f WHERE f.case_id=c.id AND f.status='candidate') AS pending_facts
             FROM loan_cases c LEFT JOIN borrowers b ON b.case_id = c.id"""
    if not include_archived:
        sql += " WHERE c.status != 'archived'"
    sql += " ORDER BY c.updated_at DESC"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def touch(conn: sqlite3.Connection, case_id: str) -> None:
    conn.execute("UPDATE loan_cases SET updated_at=? WHERE id=?", (utcnow(), case_id))


# ---------------------------------------------------------------- loan terms
def save_terms(conn: sqlite3.Connection, case_id: str, terms: TermsIn) -> str:
    if terms.repayment_type == "amortizing" and terms.amortization_months and terms.term_months \
            and terms.term_months > terms.amortization_months:
        raise ValidationFailure("Term cannot exceed amortization for an amortizing loan.")
    tid = new_id()
    with Database.transaction(conn):
        row = conn.execute("SELECT COALESCE(MAX(revision),0) AS r FROM loan_terms WHERE case_id=?", (case_id,)).fetchone()
        rev = int(row["r"]) + 1
        conn.execute("UPDATE loan_terms SET is_current=0 WHERE case_id=? AND is_current=1", (case_id,))
        conn.execute(
            """INSERT INTO loan_terms(id, case_id, revision, is_current, loan_purpose, requested_amount, term_months,
                   amortization_months, interest_rate, rate_type, repayment_type, project_total_cost,
                   requested_closing_date, is_secured, notes, created_at)
               VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tid, case_id, rev, terms.loan_purpose, dstr(terms.requested_amount), terms.term_months,
             terms.amortization_months, dstr(terms.interest_rate), terms.rate_type, terms.repayment_type,
             dstr(terms.project_total_cost), terms.requested_closing_date, int(terms.is_secured), terms.notes, utcnow()),
        )
        touch(conn, case_id)
        audit.record(conn, "loan_terms_revised", "loan_terms", tid, case_id,
                     {"revision": rev, **{k: str(v) if v is not None else None
                                          for k, v in terms.model_dump(exclude={"notes", "loan_purpose"}).items()}})
    return tid


def current_terms(conn: sqlite3.Connection, case_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM loan_terms WHERE case_id=? AND is_current=1", (case_id,)).fetchone()
    return dict(row) if row else None


def terms_history(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM loan_terms WHERE case_id=? ORDER BY revision DESC", (case_id,)).fetchall()]


# ---------------------------------------------------------------- parties
def add_party(conn: sqlite3.Connection, case_id: str, p: PartyIn) -> str:
    pid, now = new_id(), utcnow()
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO parties(id, case_id, name, party_type, ownership_pct, is_guarantor, guarantee_type,
                   created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (pid, case_id, p.name, p.party_type, dstr(p.ownership_pct), int(p.is_guarantor), p.guarantee_type, now, now),
        )
        touch(conn, case_id)
        audit.record(conn, "party_added", "party", pid, case_id, {"is_guarantor": p.is_guarantor})
    return pid


def delete_party(conn: sqlite3.Connection, case_id: str, party_id: str) -> None:
    with Database.transaction(conn):
        cur = conn.execute("DELETE FROM parties WHERE id=? AND case_id=?", (party_id, case_id))
        if cur.rowcount == 0:
            raise NotFound("party not found")
        touch(conn, case_id)
        audit.record(conn, "party_removed", "party", party_id, case_id)


def list_parties(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM parties WHERE case_id=? ORDER BY created_at", (case_id,))]


# ---------------------------------------------------------------- debts
def _check_doc(conn: sqlite3.Connection, case_id: str, document_id: str | None) -> None:
    if document_id and not conn.execute(
            "SELECT 1 FROM case_documents WHERE case_id=? AND document_id=?", (case_id, document_id)).fetchone():
        raise ValidationFailure("Referenced document is not attached to this case.")


def add_debt(conn: sqlite3.Connection, case_id: str, d: DebtIn) -> str:
    _check_doc(conn, case_id, d.document_id)
    did, now = new_id(), utcnow()
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO existing_debts(id, case_id, creditor, description, debt_type, original_amount, current_balance,
                   interest_rate, payment_amount, payment_frequency, maturity_date, to_be_refinanced, document_id,
                   page_number, verification_status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (did, case_id, d.creditor, d.description, d.debt_type, dstr(d.original_amount), dstr(d.current_balance),
             dstr(d.interest_rate), dstr(d.payment_amount), d.payment_frequency, d.maturity_date, int(d.to_be_refinanced),
             d.document_id, d.page_number, d.verification_status, now, now),
        )
        touch(conn, case_id)
        audit.record(conn, "debt_added", "existing_debt", did, case_id,
                     {"balance": dstr(d.current_balance), "payment": dstr(d.payment_amount),
                      "frequency": d.payment_frequency, "status": d.verification_status})
    return did


def set_debt_status(conn: sqlite3.Connection, case_id: str, debt_id: str, status: str) -> None:
    if status not in ("unverified", "verified", "rejected"):
        raise ValidationFailure("invalid status")
    with Database.transaction(conn):
        cur = conn.execute("UPDATE existing_debts SET verification_status=?, updated_at=? WHERE id=? AND case_id=?",
                           (status, utcnow(), debt_id, case_id))
        if cur.rowcount == 0:
            raise NotFound("debt not found")
        touch(conn, case_id)
        audit.record(conn, "debt_status_changed", "existing_debt", debt_id, case_id, {"status": status})


def delete_debt(conn: sqlite3.Connection, case_id: str, debt_id: str) -> None:
    with Database.transaction(conn):
        row = conn.execute("SELECT * FROM existing_debts WHERE id=? AND case_id=?", (debt_id, case_id)).fetchone()
        if not row:
            raise NotFound("debt not found")
        conn.execute("DELETE FROM existing_debts WHERE id=?", (debt_id,))
        touch(conn, case_id)
        audit.record(conn, "debt_removed", "existing_debt", debt_id, case_id,
                     {"creditor": row["creditor"], "balance": row["current_balance"], "payment": row["payment_amount"]})


def list_debts(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM existing_debts WHERE case_id=? ORDER BY created_at", (case_id,))]


def set_debt_schedule_confirmed(conn: sqlite3.Connection, case_id: str, confirmed: bool) -> None:
    with Database.transaction(conn):
        conn.execute("UPDATE loan_cases SET debt_schedule_confirmed=?, updated_at=? WHERE id=?",
                     (int(confirmed), utcnow(), case_id))
        audit.record(conn, "debt_schedule_confirmation", "loan_case", case_id, case_id, {"confirmed": confirmed})


# ---------------------------------------------------------------- collateral
def add_collateral(conn: sqlite3.Connection, case_id: str, c: CollateralIn, valuation: ValuationIn | None = None) -> str:
    cid, now = new_id(), utcnow()
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO collateral(id, case_id, description, collateral_type, lien_position, prior_lien_balance,
                   created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
            (cid, case_id, c.description, c.collateral_type, c.lien_position, dstr(c.prior_lien_balance), now, now),
        )
        audit.record(conn, "collateral_added", "collateral", cid, case_id, {"type": c.collateral_type})
        if valuation is not None:
            add_valuation(conn, case_id, cid, valuation)
        touch(conn, case_id)
    return cid


def add_valuation(conn: sqlite3.Connection, case_id: str, collateral_id: str, v: ValuationIn) -> str:
    if not conn.execute("SELECT 1 FROM collateral WHERE id=? AND case_id=?", (collateral_id, case_id)).fetchone():
        raise NotFound("collateral not found")
    _check_doc(conn, case_id, v.document_id)
    vid = new_id()
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO collateral_valuations(id, collateral_id, value, valuation_date, valuation_method, document_id,
                   page_number, verification_status, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (vid, collateral_id, dstr(v.value), v.valuation_date, v.valuation_method, v.document_id, v.page_number,
             v.verification_status, utcnow()),
        )
        touch(conn, case_id)
        audit.record(conn, "valuation_added", "collateral_valuation", vid, case_id,
                     {"collateral_id": collateral_id, "value": dstr(v.value), "date": v.valuation_date,
                      "method": v.valuation_method, "status": v.verification_status})
    return vid


def set_valuation_status(conn: sqlite3.Connection, case_id: str, valuation_id: str, status: str) -> None:
    if status not in ("unverified", "verified", "rejected"):
        raise ValidationFailure("invalid status")
    with Database.transaction(conn):
        cur = conn.execute(
            """UPDATE collateral_valuations SET verification_status=? WHERE id=? AND collateral_id IN
                   (SELECT id FROM collateral WHERE case_id=?)""", (status, valuation_id, case_id))
        if cur.rowcount == 0:
            raise NotFound("valuation not found")
        touch(conn, case_id)
        audit.record(conn, "valuation_status_changed", "collateral_valuation", valuation_id, case_id, {"status": status})


def delete_collateral(conn: sqlite3.Connection, case_id: str, collateral_id: str) -> None:
    with Database.transaction(conn):
        cur = conn.execute("DELETE FROM collateral WHERE id=? AND case_id=?", (collateral_id, case_id))
        if cur.rowcount == 0:
            raise NotFound("collateral not found")
        touch(conn, case_id)
        audit.record(conn, "collateral_removed", "collateral", collateral_id, case_id)


def list_collateral(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    items = [dict(r) for r in conn.execute("SELECT * FROM collateral WHERE case_id=? ORDER BY created_at", (case_id,))]
    for c in items:
        c["valuations"] = [dict(r) for r in conn.execute(
            "SELECT * FROM collateral_valuations WHERE collateral_id=? ORDER BY COALESCE(valuation_date,'') DESC, created_at DESC",
            (c["id"],))]
        c["current_valuation"] = next((v for v in c["valuations"] if v["verification_status"] != "rejected"), None)
    return items


# ---------------------------------------------------------------- deletion
def purge_case(conn: sqlite3.Connection, case_id: str, documents_dir: Path, reports_dir: Path) -> dict[str, int]:
    """Permanently delete a case, its records, reports and any documents not used by another case."""
    case = get_case(conn, case_id)
    doc_rows = conn.execute(
        """SELECT d.id, d.stored_name FROM documents d JOIN case_documents cd ON cd.document_id=d.id
           WHERE cd.case_id=? AND NOT EXISTS (SELECT 1 FROM case_documents o WHERE o.document_id=d.id AND o.case_id!=?)""",
        (case_id, case_id),
    ).fetchall()
    report_files = [r["file_name"] for r in conn.execute("SELECT file_name FROM reports WHERE case_id=?", (case_id,))]
    with Database.transaction(conn):
        for d in doc_rows:
            conn.execute("DELETE FROM documents WHERE id=?", (d["id"],))
        # Deletion is a privacy operation: the case's audit trail (which may hold
        # amounts) goes too; one tombstone event without financial data remains.
        conn.execute("DELETE FROM audit_events WHERE case_id=?", (case_id,))
        conn.execute("DELETE FROM loan_cases WHERE id=?", (case_id,))
        audit.record(conn, "case_purged", "loan_case", case_id, None,
                     {"case_number": case["case_number"], "documents_deleted": len(doc_rows)})
    removed = 0
    for d in doc_rows:
        p = documents_dir / d["stored_name"]
        if p.is_file():
            p.unlink()
            removed += 1
    case_reports = reports_dir / case_id
    for name in report_files:
        p = case_reports / name
        if p.is_file():
            p.unlink()
    if case_reports.is_dir():
        shutil.rmtree(case_reports, ignore_errors=True)
    return {"documents_deleted": removed, "reports_deleted": len(report_files)}
