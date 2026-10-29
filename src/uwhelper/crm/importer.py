"""CSV import of contacts and organizations from a local file: preview first, then an all-or-nothing commit.

* Idempotent: organizations match on name + type, contacts on email (or first + last name when there is no email);
  existing records are skipped, never overwritten. Repeated rows inside one file are reported as duplicates.
* Every row is validated with the same models as the forms/API. By default the commit is refused while any row is
  invalid; with ``skip_invalid`` only valid rows are imported. Either way the import runs in one transaction, so a
  failure leaves nothing half-imported.
* The file is read in memory only; it is not stored. Text is decoded as UTF-8 (with or without BOM) or, failing
  that, Windows-1252 (Excel's default on Windows).
"""

from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from uwhelper.crm.models import ContactIn, OrganizationIn
from uwhelper.db.connection import Database
from uwhelper.repo import audit
from uwhelper.repo.cases import ValidationFailure

Kind = Literal["contacts", "organizations"]
MAX_BYTES = 1_000_000
MAX_ROWS = 5000
ALIASES = {
    "first": "first_name", "firstname": "first_name", "given_name": "first_name",
    "last": "last_name", "lastname": "last_name", "surname": "last_name", "family_name": "last_name",
    "e_mail": "email", "email_address": "email", "phone_number": "phone", "telephone": "phone", "mobile": "phone",
    "company": "organization", "organisation": "organization", "organization_name": "organization",
    "type": "type", "zip": "postal_code", "zip_code": "postal_code", "postcode": "postal_code", "address": "address_line1",
    "job_title": "title", "company_name": "name", "organization_type": "organization_type",
}
CONTACT_COLS = {"first_name", "last_name", "email", "phone", "title", "contact_type", "notes", "organization",
                "organization_type"}
ORG_COLS = {"name", "org_type", "website", "phone", "email", "address_line1", "address_line2", "city", "state",
            "postal_code", "country", "industry", "notes"}


@dataclass
class RowResult:
    line: int
    data: dict[str, str]
    status: str  # new | existing | duplicate | invalid
    errors: list[str] = field(default_factory=list)
    model: Any = None


@dataclass
class Preview:
    kind: str
    columns: list[str]
    ignored_columns: list[str]
    rows: list[RowResult]

    @property
    def counts(self) -> dict[str, int]:
        out = {"new": 0, "existing": 0, "duplicate": 0, "invalid": 0}
        for r in self.rows:
            out[r.status] += 1
        return out


def decode(raw: bytes) -> str:
    if len(raw) > MAX_BYTES:
        raise ValidationFailure(f"The CSV file is larger than {MAX_BYTES // 1_000_000} MB; split it into smaller files.")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _norm(header: str) -> str:
    h = "_".join(header.strip().lower().replace("-", " ").split())
    return ALIASES.get(h, h)


def parse(conn: sqlite3.Connection, kind: Kind, text: str) -> Preview:
    if kind not in ("contacts", "organizations"):
        raise ValidationFailure("kind must be contacts or organizations")
    if len(text.encode("utf-8")) > MAX_BYTES * 2:
        raise ValidationFailure("The CSV text is too large.")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ValidationFailure("The CSV file is empty.") from None
    cols = [_norm(h) for h in header]
    allowed = CONTACT_COLS if kind == "contacts" else ORG_COLS
    if kind == "contacts":
        cols = ["contact_type" if c == "type" else c for c in cols]
        need = {"first_name", "last_name"}
    else:
        cols = ["org_type" if c == "type" else c for c in cols]
        need = {"name", "org_type"}
    missing = need - set(cols)
    if missing:
        raise ValidationFailure(f"Missing required column(s): {', '.join(sorted(missing))}. "
                                f"Found: {', '.join(cols) or 'none'}.")
    ignored = sorted({c for c in cols if c not in allowed})
    rows: list[RowResult] = []
    seen: set[tuple] = set()
    for n, values in enumerate(reader, start=2):
        if not any(v.strip() for v in values):
            continue
        if len(rows) >= MAX_ROWS:
            raise ValidationFailure(f"More than {MAX_ROWS} rows; split the file.")
        data = {c: (values[i].strip() if i < len(values) else "") for i, c in enumerate(cols) if c in allowed}
        rows.append(_check_row(conn, kind, n, data, seen))
    return Preview(kind, [c for c in cols if c in allowed], ignored, rows)


def _check_row(conn: sqlite3.Connection, kind: str, line: int, data: dict[str, str], seen: set[tuple]) -> RowResult:
    clean = {k: v for k, v in data.items() if v != ""}
    try:
        if kind == "contacts":
            fields = {k: v for k, v in clean.items() if k not in ("organization", "organization_type")}
            fields.setdefault("contact_type", "other")
            model = ContactIn(**fields)
            key = ("email", model.email) if model.email else ("name", model.first_name.lower(), model.last_name.lower())
            if model.email:
                exists = conn.execute("SELECT 1 FROM contacts WHERE email=? COLLATE NOCASE", (model.email,)).fetchone()
            else:
                exists = conn.execute("SELECT 1 FROM contacts WHERE first_name=? COLLATE NOCASE AND last_name=? COLLATE "
                                      "NOCASE", (model.first_name, model.last_name)).fetchone()
            if clean.get("organization_type"):
                OrganizationIn(name=clean.get("organization") or "x", org_type=clean["organization_type"])
        else:
            model = OrganizationIn(**clean)
            key = ("org", model.name.lower(), model.org_type)
            exists = conn.execute("SELECT 1 FROM organizations WHERE name=? COLLATE NOCASE AND org_type=?",
                                  (model.name, model.org_type)).fetchone()
    except ValidationError as exc:
        errs = [f"{'.'.join(str(p) for p in e['loc']) or 'row'}: {e['msg']}" for e in exc.errors()]
        return RowResult(line, data, "invalid", errs)
    if key in seen:
        return RowResult(line, data, "duplicate", ["repeats an earlier row in this file"], model)
    seen.add(key)
    return RowResult(line, data, "existing" if exists else "new", [], model)


def commit(conn: sqlite3.Connection, kind: Kind, text: str, *, skip_invalid: bool = False,
           source_name: str | None = None) -> dict[str, Any]:
    from uwhelper.crm import service as crm

    preview = parse(conn, kind, text)
    counts = preview.counts
    if counts["invalid"] and not skip_invalid:
        raise ValidationFailure(f"{counts['invalid']} row(s) are invalid. Fix them, or choose to skip invalid rows.")
    created = orgs_created = 0
    with Database.transaction(conn):
        for r in preview.rows:
            if r.status != "new":
                continue
            if kind == "organizations":
                crm.create_organization(conn, r.model)
            else:
                org_name = r.data.get("organization", "").strip()
                if org_name:
                    row = conn.execute("SELECT id FROM organizations WHERE name=? COLLATE NOCASE ORDER BY "
                                       "org_type != ?, created_at LIMIT 1",
                                       (org_name, r.data.get("organization_type") or "borrower_business")).fetchone()
                    if row:
                        org_id = row["id"]
                    else:
                        org_id = crm.create_organization(conn, OrganizationIn(
                            name=org_name, org_type=r.data.get("organization_type") or "other"))
                        orgs_created += 1
                    r.model = r.model.model_copy(update={"organization_id": org_id})
                crm.create_contact(conn, r.model)
            created += 1
        audit.record(conn, "crm_import", kind, None, None,
                     {"source": (source_name or "")[:120], "created": created, "organizations_created": orgs_created,
                      "skipped_existing": counts["existing"], "duplicates": counts["duplicate"],
                      "invalid_skipped": counts["invalid"]})
    return {"created": created, "organizations_created": orgs_created, "skipped_existing": counts["existing"],
            "duplicates": counts["duplicate"], "invalid_skipped": counts["invalid"]}
