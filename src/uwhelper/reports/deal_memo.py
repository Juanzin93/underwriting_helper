"""Credit memo per deal (brokerage view): deal, borrower, people, document checklist, underwriting results.

* Every figure comes from stored records: the deal, its checklist, and the linked file's saved underwriting run
  (with its policy version and input hash). Nothing is calculated here beyond counting.
* The full render context is stored with the memo (immutable, versioned), so any version can be re-rendered and
  checked byte-for-byte. The memo states plainly that it is preliminary and not an approval.
* No language model is used.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from uwhelper import ENGINE_VERSION, __version__
from uwhelper.core.identity import current_actor
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound

TEMPLATE_VERSION = "deal-memo/1.0.0"
KEY_METRICS = ["dscr", "global_dscr", "dscr_property", "dscr_property_stressed", "debt_yield", "ltv", "cltv", "ltc",
               "collateral_coverage", "debt_to_ebitda", "current_ratio", "noi", "cfads", "total_debt_service",
               "breakeven_occupancy", "occupancy_physical", "occupancy_economic", "rollover_12m", "walt_years"]
# Business-cash-flow metrics that say nothing about a property loan (shown for business loans only).
BUSINESS_ONLY = {"dscr", "global_dscr", "debt_to_ebitda", "current_ratio", "cfads", "total_debt_service"}
STATUS_TEXT = {"INCOMPLETE": "Incomplete — information missing or unknown",
               "REVIEW_REQUIRED": "Review required — exceptions or review items identified",
               "NO_EXCEPTIONS_IDENTIFIED": "No policy exceptions identified — NOT a credit approval"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_context(conn: sqlite3.Connection, deal_id: str, version: int) -> dict[str, Any]:
    from uwhelper.crm import requests as docreq
    from uwhelper.crm import service as crm
    from uwhelper.crm import tasks as task_svc
    from uwhelper.workflow.currency import latest_base_run, run_status

    deal = crm.get_deal(conn, deal_id)
    org = crm.get_organization(conn, deal["borrower_organization_id"]) if deal["borrower_organization_id"] else None
    reqs = docreq.list_requests(conn, deal_id)
    ctx: dict[str, Any] = {
        "version": version, "generated_at": utcnow(), "generated_by": current_actor(), "app_version": __version__,
        "engine_version": ENGINE_VERSION, "template_version": TEMPLATE_VERSION,
        "deal": {k: deal[k] for k in ("deal_number", "name", "deal_type", "product_type", "requested_amount",
                                      "loan_purpose", "collateral_summary", "target_close_date", "status",
                                      "status_changed_at")},
        "status_history": [{k: h[k] for k in ("occurred_at", "from_status", "to_status", "reason", "authorized_by")}
                           for h in deal["history"][-8:]],
        "borrower": None if not org else {k: org.get(k) for k in ("name", "org_type", "industry", "city", "state")},
        "people": [{"name": f"{c['first_name']} {c['last_name']}", "role": c["role"]} for c in deal["contacts"]],
        "checklist": {
            "summary": docreq.summary(conn, deal_id),
            "versions": sorted({f"{r['checklist_key']} {r['checklist_version']}" for r in reqs if r["checklist_version"]}),
            "rows": [{"label": r["label"], "required": bool(r["required"]), "status": r["status"],
                       "reviewed_by": r["reviewed_by"], "file": r["original_filename"]} for r in reqs],
        },
        "open_tasks": len(task_svc.list_tasks(conn, view="open", deal_id=deal_id)),
        "alignment_notes": deal["alignment_notes"],
        "underwriting": None,
    }
    case_id = deal["loan_case_id"]
    if case_id:
        case = conn.execute("SELECT case_number, stage, loan_type FROM loan_cases WHERE id=?", (case_id,)).fetchone()
        uw: dict[str, Any] = {"case_number": case["case_number"], "stage": case["stage"], "loan_type": case["loan_type"],
                              "run": None}
        run = latest_base_run(conn, case_id)
        if run:
            pol = conn.execute("SELECT policy_key, version, illustrative FROM policies WHERE id=?",
                               (run["policy_id"],)).fetchone()
            metrics = {r["metric_key"]: r for r in conn.execute(
                "SELECT metric_key, label, value, unit, status FROM run_metrics WHERE run_id=?", (run["id"],))}
            evals = [dict(e) for e in conn.execute(
                "SELECT rule_id, description, result, observed_value, operator, threshold, explanation, severity "
                "FROM rule_evaluations WHERE run_id=? ORDER BY id", (run["id"],))]
            counts: dict[str, int] = {}
            for e in evals:
                counts[e["result"]] = counts.get(e["result"], 0) + 1
            memo = conn.execute("SELECT version FROM credit_memos WHERE run_id=? ORDER BY version DESC LIMIT 1",
                                (run["id"],)).fetchone()
            decision = conn.execute("SELECT decision, decided_by, decided_at FROM credit_decisions WHERE case_id=? "
                                    "ORDER BY decided_at DESC, rowid DESC LIMIT 1", (case_id,)).fetchone()
            uw["run"] = {
                "run_number": run["run_number"], "created_at": run["created_at"], "overall_status": run["overall_status"],
                "overall_status_text": STATUS_TEXT.get(run["overall_status"], run["overall_status"]),
                "inputs_sha256": run["inputs_sha256"], "base_period": run["base_period_label"],
                "policy": f"{pol['policy_key']} v{pol['version']}" if pol else None,
                "policy_illustrative": bool(pol["illustrative"]) if pol else None,
                "current": run_status(conn, case_id)["current"],
                "metrics": [dict(metrics[k]) for k in KEY_METRICS if k in metrics and metrics[k]["status"] != "not_applicable"
                            and not (case["loan_type"] == "commercial_real_estate" and k in BUSINESS_ONLY)],
                "result_counts": counts,
                "exceptions": [e for e in evals if e["result"] in ("FAIL", "REVIEW_REQUIRED", "UNKNOWN")],
                "case_memo_version": memo["version"] if memo else None,
                "decision": dict(decision) if decision else None,
            }
        ctx["underwriting"] = uw
    return ctx


def render(ctx: dict[str, Any]) -> str:
    from uwhelper.reports.memo import _env

    return _env().get_template("deal_memo.html").render(m=ctx)


def generate(conn: sqlite3.Connection, deal_id: str) -> dict[str, Any]:
    exists = conn.execute("SELECT loan_case_id FROM deals WHERE id=?", (deal_id,)).fetchone()
    if not exists:
        raise NotFound("Deal not found")
    row = conn.execute("SELECT COALESCE(MAX(version),0) AS v FROM deal_memos WHERE deal_id=?", (deal_id,)).fetchone()
    version = int(row["v"]) + 1
    ctx = build_context(conn, deal_id, version)
    ctx_json = json.dumps(ctx, sort_keys=True, default=str)
    ctx = json.loads(ctx_json)  # render exactly what is stored
    html = render(ctx)
    run = (ctx.get("underwriting") or {}).get("run")
    run_id = None
    if run:
        r = conn.execute("SELECT id FROM underwriting_runs WHERE case_id=? AND run_number=?",
                         (exists["loan_case_id"], run["run_number"])).fetchone()
        run_id = r["id"] if r else None
    mid = new_id()
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO deal_memos(id, deal_id, version, case_id, run_id, context_json, context_sha256, html, html_sha256,
                   template_version, generated_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, deal_id, version, exists["loan_case_id"], run_id, ctx_json, _sha(ctx_json), html, _sha(html),
             TEMPLATE_VERSION, ctx["generated_by"], ctx["generated_at"]))
        audit.record(conn, "deal_memo_generated", "deal", deal_id, exists["loan_case_id"],
                     {"version": version, "memo_id": mid, "run_number": run["run_number"] if run else None,
                      "checklist_versions": ctx["checklist"]["versions"], "html_sha256": _sha(html)})
    return {"id": mid, "version": version}


def list_memos(conn: sqlite3.Connection, deal_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT id, version, created_at, generated_by, html_sha256, template_version FROM deal_memos WHERE deal_id=? "
        "ORDER BY version DESC", (deal_id,))]


def get_memo(conn: sqlite3.Connection, deal_id: str, memo_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM deal_memos WHERE id=? AND deal_id=?", (memo_id, deal_id)).fetchone()
    if not row:
        raise NotFound("Deal memo not found")
    return dict(row)


def verify(conn: sqlite3.Connection, deal_id: str, memo_id: str) -> dict[str, Any]:
    memo = get_memo(conn, deal_id, memo_id)
    ctx_ok = _sha(memo["context_json"]) == memo["context_sha256"]
    html_ok = _sha(render(json.loads(memo["context_json"]))) == memo["html_sha256"]
    stored_ok = _sha(memo["html"]) == memo["html_sha256"]
    return {"context_intact": ctx_ok, "html_reproduced": html_ok, "stored_html_intact": stored_ok,
            "template_version": memo["template_version"]}
