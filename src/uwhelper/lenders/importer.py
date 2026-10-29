"""Import lender programs and criteria from local files (preview first; all-or-nothing commit).

YAML (several lenders/programs at once)::

    lenders:
      - name: Example Bank (FICTIONAL)
        programs:
          - name: Equipment term loans
            deal_types: [small_business]
            product_types: [equipment, term_loan]
            states: [TX, OK]
            min_amount: 100000
            max_amount: 2000000
            criteria_version:
              source: "Program sheet dated 2026-09-01"
              verified_at: 2026-10-01
              verified_by: Pat Broker
              criteria:
                - {id: DSCR_MIN, description: "DSCR at least 1.25x", subject: "metric:dscr", op: ">=", value: 1.25}

CSV (criteria for one program): columns ``id, description, subject, op, value, required`` (``value`` lists use
``a|b|c``; ``required`` defaults to yes). The source / verified date / verified by are entered alongside.

Lenders are matched by name (organization type "lender"; created if missing), programs by name under the lender
(created, or their eligibility fields updated). A criteria version is added only if the criteria, source or verified
date differ from the latest version. Files are read in memory and not stored.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import yaml
from pydantic import ValidationError

from uwhelper.core.csvutil import read_rows
from uwhelper.db.connection import Database
from uwhelper.lenders import service as lenders
from uwhelper.lenders.models import (
    CriteriaVersionIn,
    Criterion,
    ProgramIn,
    ProgramPatch,
)
from uwhelper.repo import audit
from uwhelper.repo.cases import ValidationFailure

_TRUE = {"", "1", "y", "yes", "true", "required"}


@dataclass
class PlannedProgram:
    lender: str
    program: str
    lender_exists: bool
    program_id: str | None
    program_fields: dict[str, Any]
    version: CriteriaVersionIn | None
    version_action: str  # new | unchanged | none
    errors: list[str] = field(default_factory=list)


def _errors(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc']) or 'value'}: {e['msg']}" for e in exc.errors()]


def _version_action(conn: sqlite3.Connection, program_id: str | None, v: CriteriaVersionIn | None) -> str:
    if v is None:
        return "none"
    if not program_id:
        return "new"
    latest = lenders.current_version(conn, program_id, date.max)
    import hashlib

    digest = hashlib.sha256(lenders.canonical_criteria(v).encode()).hexdigest()
    same = latest and latest["criteria_sha256"] == digest and latest["verified_at"] == v.verified_at.isoformat() \
        and latest["source"] == v.source
    return "unchanged" if same else "new"


def plan_yaml(conn: sqlite3.Connection, text: str) -> list[PlannedProgram]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValidationFailure(f"Not valid YAML: {str(exc)[:200]}") from None
    if not isinstance(data, dict) or not isinstance(data.get("lenders"), list):
        raise ValidationFailure("The file must contain a top-level 'lenders:' list.")
    plan: list[PlannedProgram] = []
    for li, lender in enumerate(data["lenders"], start=1):
        if not isinstance(lender, dict) or not str(lender.get("name") or "").strip():
            raise ValidationFailure(f"Lender #{li} has no name.")
        lname = str(lender["name"]).strip()[:200]
        org = conn.execute("SELECT id FROM organizations WHERE org_type='lender' AND name=? COLLATE NOCASE",
                           (lname,)).fetchone()
        for prog in lender.get("programs") or []:
            pname = str((prog or {}).get("name") or "").strip()
            errs: list[str] = []
            fields = {k: prog.get(k) for k in ("deal_types", "product_types", "states", "min_amount", "max_amount",
                                                 "notes") if k in prog}
            try:
                ProgramIn(organization_id="x", name=pname, **fields)
            except ValidationError as exc:
                errs += _errors(exc)
            version = None
            if prog.get("criteria_version"):
                try:
                    version = CriteriaVersionIn(**prog["criteria_version"])
                except ValidationError as exc:
                    errs += [f"criteria: {e}" for e in _errors(exc)]
            pid = None
            if org:
                row = conn.execute("SELECT id FROM lender_programs WHERE organization_id=? AND name=? COLLATE NOCASE",
                                   (org["id"], pname)).fetchone()
                pid = row["id"] if row else None
            plan.append(PlannedProgram(lname, pname, bool(org), pid, fields, version,
                                       _version_action(conn, pid, version) if not errs else "none", errs))
    if not plan:
        raise ValidationFailure("No programs found in the file.")
    return plan


def commit_yaml(conn: sqlite3.Connection, text: str) -> dict[str, int]:
    from uwhelper.crm import service as crm
    from uwhelper.crm.models import OrganizationIn

    plan = plan_yaml(conn, text)
    bad = [p for p in plan if p.errors]
    if bad:
        raise ValidationFailure(f"{len(bad)} program(s) have errors; nothing was imported. First: "
                                f"{bad[0].lender} / {bad[0].program}: {bad[0].errors[0]}")
    out = {"lenders_created": 0, "programs_created": 0, "programs_updated": 0, "versions_added": 0}
    with Database.transaction(conn):
        for p in plan:
            org = conn.execute("SELECT id FROM organizations WHERE org_type='lender' AND name=? COLLATE NOCASE",
                               (p.lender,)).fetchone()
            org_id = org["id"] if org else crm.create_organization(conn, OrganizationIn(name=p.lender, org_type="lender"))
            out["lenders_created"] += 0 if org else 1
            row = conn.execute("SELECT id FROM lender_programs WHERE organization_id=? AND name=? COLLATE NOCASE",
                               (org_id, p.program)).fetchone()
            if row:
                pid = row["id"]
                if lenders.update_program(conn, pid, ProgramPatch(**p.program_fields)):
                    out["programs_updated"] += 1
            else:
                pid = lenders.create_program(conn, ProgramIn(organization_id=org_id, name=p.program, **p.program_fields))
                out["programs_created"] += 1
            if p.version and lenders.add_version(conn, pid, p.version)["created"]:
                out["versions_added"] += 1
        audit.record(conn, "lender_catalog_imported", "lender_program", None, None, out)
    return out


def parse_criteria_csv(text: str) -> tuple[list[Criterion], list[str]]:
    header, rows = read_rows(text, 200)
    need = {"id", "description", "subject", "op"}
    if not need <= set(header):
        raise ValidationFailure(f"Criteria CSV needs columns: {', '.join(sorted(need))} (plus value, required).")
    out, errors = [], []
    for line, values in rows:
        d = {h: (values[i] if i < len(values) else "") for i, h in enumerate(header)}
        try:
            out.append(Criterion(id=d["id"], description=d["description"], subject=d["subject"], op=d["op"],
                                 value=d.get("value") or None, required=d.get("required", "").strip().lower() in _TRUE))
        except ValidationError as exc:
            errors.append(f"line {line}: " + "; ".join(_errors(exc)))
    return out, errors
