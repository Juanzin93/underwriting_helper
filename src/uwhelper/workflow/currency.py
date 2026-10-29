"""Is the latest underwriting run still current?

A run is current only if re-building the verified-only inputs today produces exactly the
same input snapshot (same SHA-256) and the same policy version. Any material change —
corrected or newly verified facts, revised loan terms, debts, collateral, guarantors,
or a new policy version — makes it stale and a new run is required.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from uwhelper.analysis.resolver import build_inputs
from uwhelper.analysis.underwriting import default_policy_key
from uwhelper.policy.loader import PolicyError, resolve_policy
from uwhelper.repo import cases as cases_repo


def latest_base_run(conn: sqlite3.Connection, case_id: str, mode: str = "verified_only") -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT * FROM underwriting_runs WHERE case_id=? AND mode=? AND scenario_id IS NULL AND scenario_name IS NULL
           ORDER BY run_number DESC LIMIT 1""", (case_id, mode)).fetchone()
    return dict(row) if row else None


def current_fingerprint(conn: sqlite3.Connection, case_id: str) -> tuple[str, str | None]:
    inputs, _ = build_inputs(conn, case_id, "verified_only")
    digest = hashlib.sha256(json.dumps(inputs.to_dict(), sort_keys=True).encode("utf-8")).hexdigest()
    case = cases_repo.get_case(conn, case_id)
    try:
        policy_id, _ = resolve_policy(conn, case.get("policy_key") or default_policy_key(case["loan_type"]),
                                      case.get("policy_version"))
    except PolicyError:
        policy_id = None
    return digest, policy_id


def run_status(conn: sqlite3.Connection, case_id: str) -> dict[str, Any]:
    run = latest_base_run(conn, case_id)
    if run is None:
        return {"run": None, "current": False, "reason": "No verified-only underwriting run has been saved yet."}
    digest, policy_id = current_fingerprint(conn, case_id)
    reasons = []
    if digest != run["inputs_sha256"]:
        reasons.append("financial facts, loan terms, debts, collateral or guarantor data changed since the run")
    if policy_id != run["policy_id"]:
        reasons.append("a different policy version now applies")
    return {"run": run, "current": not reasons,
            "reason": ("Run #%d is current." % run["run_number"]) if not reasons
            else f"Run #{run['run_number']} is out of date: " + "; ".join(reasons) + ". Re-run the screening."}
