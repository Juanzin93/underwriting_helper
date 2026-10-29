"""Policies, system/local-only diagnostics and audit log."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request

from uwhelper.diagnostics import run_checks
from uwhelper.policy.loader import list_policies, policy_from_row, sync_policies_dir
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound
from uwhelper.web.app import get_db, redirect, render
from uwhelper.web.routes.cases import case_context

router = APIRouter()


@router.get("/policies")
async def policies(request: Request):
    with get_db(request).session() as conn:
        items = list_policies(conn)
        usage = {r["policy_id"]: r["n"] for r in conn.execute(
            "SELECT policy_id, COUNT(*) AS n FROM underwriting_runs GROUP BY policy_id")}
    return render(request, "policies.html", policies=items, usage=usage)


@router.get("/policies/{policy_id}")
async def policy_detail(request: Request, policy_id: str):
    with get_db(request).session() as conn:
        row = conn.execute("SELECT * FROM policies WHERE id=?", (policy_id,)).fetchone()
    if not row:
        raise NotFound("Policy not found")
    policy = policy_from_row(row)
    return render(request, "policy_detail.html", row=dict(row), policy=policy,
                  content=json.dumps(json.loads(row["content_json"]), indent=2))


@router.post("/policies/reload")
async def reload_policies(request: Request):
    with get_db(request).session() as conn:
        report = sync_policies_dir(conn, request.app.state.settings.policies_dir)
    request.app.state.policy_errors = report.errors
    msg = f"Loaded {len(report.loaded)} new version(s); {len(report.unchanged)} unchanged."
    if report.errors:
        msg += f" {len(report.errors)} error(s) — see the banner."
    return redirect("/policies", msg, "warn" if report.errors else "info")


@router.get("/system")
async def system(request: Request):
    checks = run_checks(request.app.state.settings)
    return render(request, "system.html", checks=checks)


@router.get("/cases/{case_id}/audit")
async def case_audit(request: Request, case_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        events = audit.list_events(conn, case_id, limit=1000)
    return render(request, "case/audit.html", active="audit", events=events, **ctx)
