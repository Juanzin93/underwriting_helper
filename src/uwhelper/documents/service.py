"""Document import, de-duplication, processing and lookup."""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from uwhelper.config import Settings
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.documents.extract import extract_document
from uwhelper.documents.storage import StagedFile, stage_upload, stored_name
from uwhelper.modules import get_module
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure, touch


@dataclass
class ImportResult:
    document_id: str
    duplicate: bool  # identical content already stored (by SHA-256)
    already_linked: bool  # already attached to this case
    status: str


def _doc_type_ok(doc_type: str) -> str:
    types = get_module("commercial").document_types()
    if doc_type not in types:
        raise ValidationFailure(f"Unknown document type {doc_type!r}")
    return doc_type


def import_document(conn: sqlite3.Connection, settings: Settings, case_id: str, stream: BinaryIO,
                    filename: str | None, doc_type: str = "other", period_label: str | None = None,
                    document_date: str | None = None, password: str | None = None,
                    process: bool = True) -> ImportResult:
    _doc_type_ok(doc_type)
    if not conn.execute("SELECT 1 FROM loan_cases WHERE id=?", (case_id,)).fetchone():
        raise NotFound("Loan case not found")
    staged = stage_upload(stream, filename, settings.documents_dir / ".incoming", settings.max_upload_bytes)
    try:
        existing = conn.execute("SELECT * FROM documents WHERE sha256=?", (staged.sha256,)).fetchone()
        if existing:
            linked = conn.execute("SELECT 1 FROM case_documents WHERE case_id=? AND document_id=?",
                                  (case_id, existing["id"])).fetchone()
            if not linked:
                with Database.transaction(conn):
                    conn.execute(
                        "INSERT INTO case_documents(case_id, document_id, doc_type, period_label, document_date, added_at) "
                        "VALUES (?,?,?,?,?,?)", (case_id, existing["id"], doc_type, period_label, document_date, utcnow()))
                    touch(conn, case_id)
                    audit.record(conn, "document_linked_duplicate", "document", existing["id"], case_id,
                                 {"sha256": staged.sha256})
            return ImportResult(existing["id"], True, bool(linked), existing["processing_status"])
        doc_id = _store_new(conn, settings, case_id, staged, doc_type, period_label, document_date)
    finally:
        staged.path.unlink(missing_ok=True)
    status = "pending"
    if process:
        status = process_document(conn, settings, doc_id, password=password)
    return ImportResult(doc_id, False, False, status)


def _store_new(conn: sqlite3.Connection, settings: Settings, case_id: str, staged: StagedFile, doc_type: str,
               period_label: str | None, document_date: str | None) -> str:
    name = stored_name(staged.sha256, staged.ext)
    dest = settings.documents_dir / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(staged.path, dest)
    doc_id, now = new_id(), utcnow()
    try:
        with Database.transaction(conn):
            conn.execute(
                """INSERT INTO documents(id, sha256, original_filename, stored_name, file_ext, size_bytes,
                       processing_status, imported_at) VALUES (?,?,?,?,?,?, 'pending', ?)""",
                (doc_id, staged.sha256, staged.display_name, name, staged.ext, staged.size, now),
            )
            conn.execute(
                "INSERT INTO case_documents(case_id, document_id, doc_type, period_label, document_date, added_at) "
                "VALUES (?,?,?,?,?,?)", (case_id, doc_id, doc_type, period_label, document_date, now))
            touch(conn, case_id)
            audit.record(conn, "document_imported", "document", doc_id, case_id,
                         {"sha256": staged.sha256, "size": staged.size, "ext": staged.ext, "doc_type": doc_type})
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return doc_id


def process_document(conn: sqlite3.Connection, settings: Settings, document_id: str, password: str | None = None) -> str:
    doc = get_document(conn, document_id)
    path = settings.documents_dir / doc["stored_name"]
    if not path.is_file():
        outcome_status, error, pages, count, ocr_pages = "failed", "Stored file is missing from the data directory.", [], 0, 0
    else:
        outcome = extract_document(
            path, doc["file_ext"], tesseract_cmd=settings.tesseract_cmd, ocr_lang=settings.tesseract_lang,
            ocr_dpi=settings.ocr_dpi, ocr_timeout=settings.ocr_timeout_seconds,
            min_text_chars=settings.ocr_min_text_chars, max_pages=settings.max_pages, password=password)
        outcome_status, error, pages, count, ocr_pages = (outcome.status, outcome.error, outcome.pages,
                                                          outcome.page_count, outcome.ocr_pages)
    flagged = sum(1 for p in pages if p.injection_patterns)
    with Database.transaction(conn):
        conn.execute("DELETE FROM document_pages WHERE document_id=?", (document_id,))
        for p in pages:
            conn.execute(
                """INSERT INTO document_pages(document_id, page_number, text_source, text, char_count, ocr_status,
                       injection_suspected, injection_patterns) VALUES (?,?,?,?,?,?,?,?)""",
                (document_id, p.page_number, p.text_source, p.text, len(p.text), p.ocr_status,
                 int(bool(p.injection_patterns)), ",".join(p.injection_patterns) or None),
            )
        conn.execute(
            """UPDATE documents SET processing_status=?, processing_error=?, page_count=?, ocr_pages=?,
                   injection_flags=?, processed_at=? WHERE id=?""",
            (outcome_status, error, count, ocr_pages, flagged, utcnow(), document_id),
        )
        audit.record(conn, "document_processed", "document", document_id, None,
                     {"status": outcome_status, "pages": count, "ocr_pages": ocr_pages, "injection_flags": flagged})
    return outcome_status


def get_document(conn: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
    if not row:
        raise NotFound("Document not found")
    return dict(row)


def get_case_document(conn: sqlite3.Connection, case_id: str, document_id: str) -> dict[str, Any]:
    row = conn.execute(
        """SELECT d.*, cd.doc_type, cd.period_label, cd.document_date, cd.added_at FROM documents d
           JOIN case_documents cd ON cd.document_id=d.id WHERE cd.case_id=? AND d.id=?""",
        (case_id, document_id)).fetchone()
    if not row:
        raise NotFound("Document not found on this case")
    return dict(row)


def list_case_documents(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT d.*, cd.doc_type, cd.period_label, cd.document_date, cd.added_at,
                  (SELECT COUNT(*) FROM facts f WHERE f.document_id=d.id AND f.case_id=cd.case_id) AS fact_count,
                  (SELECT status FROM extraction_runs e WHERE e.document_id=d.id AND e.case_id=cd.case_id
                     ORDER BY started_at DESC LIMIT 1) AS last_extraction_status
           FROM documents d JOIN case_documents cd ON cd.document_id=d.id WHERE cd.case_id=?
           ORDER BY cd.added_at""", (case_id,)).fetchall()
    return [dict(r) for r in rows]


def get_pages(conn: sqlite3.Connection, document_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM document_pages WHERE document_id=? ORDER BY page_number", (document_id,))]


def get_page(conn: sqlite3.Connection, document_id: str, page_number: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM document_pages WHERE document_id=? AND page_number=?",
                       (document_id, page_number)).fetchone()
    if not row:
        raise NotFound("Page not found")
    return dict(row)


def set_document_type(conn: sqlite3.Connection, case_id: str, document_id: str, doc_type: str,
                      period_label: str | None, document_date: str | None) -> None:
    _doc_type_ok(doc_type)
    with Database.transaction(conn):
        cur = conn.execute("UPDATE case_documents SET doc_type=?, period_label=?, document_date=? "
                           "WHERE case_id=? AND document_id=?",
                           (doc_type, period_label, document_date, case_id, document_id))
        if cur.rowcount == 0:
            raise NotFound("Document not found on this case")
        touch(conn, case_id)
        audit.record(conn, "document_classified", "document", document_id, case_id, {"doc_type": doc_type})


def remove_document(conn: sqlite3.Connection, settings: Settings, case_id: str, document_id: str) -> bool:
    """Detach a document from a case; delete the file if no other case uses it.

    Refused while active (candidate/verified) facts from it exist on the case:
    reject those facts first so the evidence trail stays coherent.
    """
    doc = get_case_document(conn, case_id, document_id)
    active = conn.execute("SELECT COUNT(*) AS n FROM facts WHERE case_id=? AND document_id=? AND status IN "
                          "('candidate','verified')", (case_id, document_id)).fetchone()["n"]
    if active:
        raise ValidationFailure(f"{active} active fact(s) cite this document. Reject them before removing it.")
    others = conn.execute("SELECT COUNT(*) AS n FROM case_documents WHERE document_id=? AND case_id!=?",
                          (document_id, case_id)).fetchone()["n"]
    with Database.transaction(conn):
        conn.execute("DELETE FROM case_documents WHERE case_id=? AND document_id=?", (case_id, document_id))
        if not others:
            conn.execute("DELETE FROM documents WHERE id=?", (document_id,))
        touch(conn, case_id)
        audit.record(conn, "document_removed", "document", document_id, case_id,
                     {"sha256": doc["sha256"], "file_deleted": not others})
    if not others:
        (settings.documents_dir / doc["stored_name"]).unlink(missing_ok=True)
    return not others


def document_path(settings: Settings, doc: dict[str, Any]) -> Path:
    path = (settings.documents_dir / doc["stored_name"]).resolve()
    if settings.documents_dir.resolve() not in path.parents:
        raise ValidationFailure("invalid stored path")
    return path
