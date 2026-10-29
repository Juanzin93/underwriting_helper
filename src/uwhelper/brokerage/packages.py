"""Submission packages: a manifest of exactly what would be shared with one lender program.

1. Build: choose the lender program, the deal documents and (optionally) a deal memo version. The manifest lists
   every file with its SHA-256, the figures that would be quoted (from the linked run), the matching result for that
   program and the disclosure text. It is stored immutably *before* anything is exported.
2. Export: after a named person confirms the listed contents, a ZIP is written to the local reports folder. Each
   document is re-hashed first; if a stored file no longer matches the manifest, the export is refused.

The app never sends the package anywhere. Delivering it to a lender is the user's own action, outside the app.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from uwhelper.config import Settings
from uwhelper.core.identity import current_actor
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure

FORMAT = "uwhelper-submission/1"
KEY_METRICS = ("dscr", "global_dscr", "dscr_property", "debt_yield", "ltv", "ltc", "debt_to_ebitda", "noi", "cfads",
               "occupancy_physical", "breakeven_occupancy")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:40] or "lender"


def build_package(conn: sqlite3.Connection, settings: Settings, deal_id: str, program_id: str,
                  document_ids: list[str], *, memo_id: str | None = None, match_run_id: str | None = None,
                  include_figures: bool = True) -> str:
    from uwhelper.crm import service as crm
    from uwhelper.lenders import service as lenders
    from uwhelper.workflow.currency import latest_base_run, run_status

    deal = crm.get_deal(conn, deal_id)
    program = lenders.get_program(conn, program_id)
    attached = {d["id"]: d for d in deal["documents"]}
    unknown = [d for d in document_ids if d not in attached]
    if unknown:
        raise ValidationFailure("Only documents attached to this deal can be included.")
    if not document_ids and not memo_id:
        raise ValidationFailure("Choose at least one document or a deal memo to include.")
    docs = []
    for did in sorted(set(document_ids), key=lambda x: attached[x]["original_filename"].lower()):
        d = attached[did]
        docs.append({"document_id": did, "filename": d["original_filename"], "doc_type": d["doc_type"],
                     "sha256": d["sha256"], "size_bytes": d["size_bytes"]})
    memo = None
    if memo_id:
        row = conn.execute("SELECT id, version, html_sha256 FROM deal_memos WHERE id=? AND deal_id=?",
                           (memo_id, deal_id)).fetchone()
        if not row:
            raise ValidationFailure("That deal memo does not belong to this deal.")
        memo = {"memo_id": row["id"], "version": row["version"], "html_sha256": row["html_sha256"]}
    match = None
    if match_run_id:
        from uwhelper.lenders.matching import get_match_run

        mr = get_match_run(conn, deal_id, match_run_id)
        entry = next((p for p in mr["results"]["programs"] if p["program_id"] == program_id), None)
        match = {"match_run_id": match_run_id, "created_at": mr["created_at"],
                 "fit": entry["fit"] if entry else None, "counts": entry["counts"] if entry else None,
                 "criteria_version": entry["criteria_version"] if entry else None}
    figures = None
    if include_figures and deal["loan_case_id"]:
        run = latest_base_run(conn, deal["loan_case_id"])
        if run:
            ms = {r["metric_key"]: r for r in conn.execute(
                "SELECT metric_key, label, value, unit, status FROM run_metrics WHERE run_id=?", (run["id"],))}
            figures = {"run_number": run["run_number"], "run_created_at": run["created_at"],
                       "inputs_sha256": run["inputs_sha256"], "run_current": run_status(conn, deal["loan_case_id"])["current"],
                       "metrics": [{"key": k, "label": ms[k]["label"], "value": ms[k]["value"], "unit": ms[k]["unit"]}
                                   for k in KEY_METRICS if k in ms and ms[k]["status"] == "computed"]}
    version = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM submission_packages WHERE deal_id=? AND program_id=?",
                           (deal_id, program_id)).fetchone()[0]
    manifest = {
        "format": FORMAT, "version": version, "created_at": utcnow(), "created_by": current_actor(),
        "deal": {k: deal[k] for k in ("deal_number", "name", "deal_type", "product_type", "requested_amount",
                                      "loan_purpose")} | {"borrower": deal["borrower_name"]},
        "lender": program["lender_name"], "program": program["name"], "program_id": program_id,
        "criteria_version": program["current"]["version"] if program["current"] else None,
        "match": match, "documents": docs, "memo": memo, "figures": figures,
        "disclosure": settings.broker_disclosure,
    }
    body = json.dumps(manifest, sort_keys=True, default=str)
    pid = new_id()
    with Database.transaction(conn):
        conn.execute("""INSERT INTO submission_packages(id, deal_id, program_id, match_run_id, version, manifest_json,
                            manifest_sha256, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                     (pid, deal_id, program_id, match_run_id, version, body, _sha(body.encode()), manifest["created_by"],
                      manifest["created_at"]))
        audit.record(conn, "submission_package_built", "deal", deal_id, deal["loan_case_id"],
                     {"package_id": pid, "program_id": program_id, "version": version,
                      "documents": [d["sha256"][:16] for d in docs], "memo_version": memo["version"] if memo else None,
                      "figures": bool(figures)})
    return pid


def get_package(conn: sqlite3.Connection, deal_id: str, package_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM submission_packages WHERE id=? AND deal_id=?", (package_id, deal_id)).fetchone()
    if not row:
        raise NotFound("Submission package not found")
    d = dict(row)
    d["manifest"] = json.loads(d["manifest_json"])
    d["intact"] = _sha(d["manifest_json"].encode()) == d["manifest_sha256"]
    d["exports"] = [dict(r) for r in conn.execute(
        "SELECT * FROM package_exports WHERE package_id=? ORDER BY exported_at DESC", (package_id,))]
    return d


def list_packages(conn: sqlite3.Connection, deal_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        """SELECT p.id, p.version, p.created_at, p.created_by, lp.name AS program, o.name AS lender,
                  (SELECT COUNT(*) FROM package_exports e WHERE e.package_id=p.id) AS exports
           FROM submission_packages p JOIN lender_programs lp ON lp.id=p.program_id
           JOIN organizations o ON o.id=lp.organization_id WHERE p.deal_id=?
           ORDER BY p.created_at DESC, p.version DESC""", (deal_id,))]


def export_package(conn: sqlite3.Connection, settings: Settings, deal_id: str, package_id: str, *,
                   confirmed_by: str, confirm: bool) -> dict[str, Any]:
    pkg = get_package(conn, deal_id, package_id)
    if not confirm:
        raise ValidationFailure("Confirm that you reviewed the listed contents before exporting.")
    if len((confirmed_by or "").strip()) < 2:
        raise ValidationFailure("Enter the name of the person confirming the package contents.")
    if not pkg["intact"]:
        raise ValidationFailure("The stored manifest no longer matches its checksum; build a new package.")
    m = pkg["manifest"]
    files: list[tuple[str, bytes]] = []
    for d in m["documents"]:
        row = conn.execute("SELECT stored_name FROM documents WHERE id=?", (d["document_id"],)).fetchone()
        path = settings.documents_dir / row["stored_name"] if row else None
        if not path or not path.is_file():
            raise ValidationFailure(f"{d['filename']} is no longer in the document store.")
        data = path.read_bytes()
        if _sha(data) != d["sha256"]:
            raise ValidationFailure(f"{d['filename']} no longer matches its recorded SHA-256; export refused.")
        safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", d["filename"])[:120]
        files.append((f"documents/{d['sha256'][:8]}_{safe}", data))
    if m["memo"]:
        row = conn.execute("SELECT html, html_sha256 FROM deal_memos WHERE id=?", (m["memo"]["memo_id"],)).fetchone()
        if not row or _sha(row["html"].encode()) != m["memo"]["html_sha256"]:
            raise ValidationFailure("The deal memo no longer matches the manifest; export refused.")
        files.append((f"deal_memo_v{m['memo']['version']}.html", row["html"].encode()))
    readme = [f"Submission package v{m['version']} — {m['deal']['deal_number']} {m['deal']['name']}",
              f"Lender: {m['lender']} — program: {m['program']}",
              f"Prepared {m['created_at']} by {m['created_by']}; contents confirmed by {confirmed_by.strip()}",
              "", "Files (SHA-256):"]
    readme += [f"  {name}  {_sha(data)}" for name, data in files]
    files += [("manifest.json", json.dumps(m, indent=2, sort_keys=True, default=str).encode()),
              ("DISCLOSURE.txt", (m["disclosure"] + "\n").encode()), ("README.txt", ("\n".join(readme) + "\n").encode())]
    out_dir = settings.reports_dir / "submissions" / deal_id
    out_dir.mkdir(parents=True, exist_ok=True)
    n = len(pkg["exports"]) + 1
    name = f"{m['deal']['deal_number']}_{_slug(m['lender'])}_pkg{m['version']}_export{n}.zip"
    tmp = out_dir / (name + ".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for arc, data in files:
            info = zipfile.ZipInfo(arc, date_time=(2000, 1, 1, 0, 0, 0))  # fixed time → identical bytes on re-export
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    blob = tmp.read_bytes()
    final = out_dir / name
    tmp.replace(final)
    eid = new_id()
    try:
        with Database.transaction(conn):
            conn.execute("""INSERT INTO package_exports(id, package_id, confirmed_by, file_name, zip_sha256, size_bytes,
                                exported_at) VALUES (?,?,?,?,?,?,?)""",
                         (eid, package_id, confirmed_by.strip()[:120], name, _sha(blob), len(blob), utcnow()))
            audit.record(conn, "submission_package_exported", "deal", deal_id, None,
                         {"package_id": package_id, "export_id": eid, "confirmed_by": confirmed_by.strip()[:120],
                          "zip_sha256": _sha(blob), "files": len(files)})
    except Exception:
        final.unlink(missing_ok=True)
        raise
    return {"export_id": eid, "path": final, "file_name": name, "zip_sha256": _sha(blob)}


def export_path(conn: sqlite3.Connection, settings: Settings, deal_id: str, package_id: str, export_id: str) -> Path:
    row = conn.execute("""SELECT e.file_name FROM package_exports e JOIN submission_packages p ON p.id=e.package_id
                          WHERE e.id=? AND p.id=? AND p.deal_id=?""", (export_id, package_id, deal_id)).fetchone()
    if not row:
        raise NotFound("Export not found")
    path = settings.reports_dir / "submissions" / deal_id / row["file_name"]
    if not path.is_file():
        raise NotFound("The exported file is no longer on disk.")
    return path
