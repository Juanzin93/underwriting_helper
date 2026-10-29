"""Preliminary credit memo generator.

* Built only from a saved underwriting run, verified facts, recorded debts/collateral/guarantors,
  saved structures, recorded conditions and analyst-entered commentary (labelled as such).
* Every figure in the memo is either a verified fact (with source document/page) or a deterministic
  calculation from the run (with formula); assumptions, estimates and model narrative are labelled.
* The memo's "preliminary analytical opinion" is derived mechanically from rule results. It is never an
  approval; only a recorded human credit decision can approve or decline.
* The full render context is stored as JSON with the memo, so a memo can be re-rendered and verified.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from uwhelper import ENGINE_VERSION, __version__
from uwhelper.analysis.resolver import find_contradictions, missing_checklist
from uwhelper.analysis.underwriting import get_run
from uwhelper.config import Settings
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.documents.service import list_case_documents
from uwhelper.llm.narrative import Narrative
from uwhelper.modules import get_module
from uwhelper.presentation import register_filters
from uwhelper.reports.builder import HUMAN_REVIEW_CHECKLIST, LIMITATIONS, render_pdf
from uwhelper.repo import audit
from uwhelper.repo import cases as cases_repo
from uwhelper.repo import facts as facts_repo

GENERATOR_VERSION = "credit-memo/1.0.0"
TEMPLATES = Path(__file__).parent / "templates"

OPINIONS = {
    "INSUFFICIENT_INFORMATION": "Insufficient information for a preliminary opinion. Required information is missing, "
                                "unverified or contradictory; see the follow-up questions.",
    "FURTHER_REVIEW_REQUIRED": "Preliminary analytical opinion: exceptions or review items were identified by the "
                               "configured screening rules. A human credit reviewer must evaluate them before any decision.",
    "NO_EXCEPTIONS_IDENTIFIED": "Preliminary analytical opinion: the configured screening rules identified no exceptions. "
                                "This is not an approval; the request still requires full human credit review.",
}

CATEGORY = {
    "dscr": "cash flow", "global_dscr": "cash flow", "dscr_property": "cash flow", "debt_yield": "cash flow",
    "debt_to_ebitda": "leverage", "debt_to_equity": "leverage", "total_equity": "leverage",
    "current_ratio": "liquidity", "quick_ratio": "liquidity", "working_capital": "liquidity",
    "ltv": "collateral", "cltv": "collateral", "collateral_coverage": "collateral", "ltc": "collateral",
}
HISTORY_FIELDS = ["revenue", "cost_of_goods_sold", "operating_expenses", "interest_expense", "depreciation",
                  "amortization", "income_tax_expense", "net_income", "distributions", "unfinanced_capex"]
KEY_METRICS = ["cfads", "existing_debt_service", "proposed_debt_service", "total_debt_service", "dscr", "global_dscr",
               "dscr_property", "ltv", "cltv", "ltc", "collateral_coverage", "debt_to_ebitda", "debt_to_equity",
               "current_ratio", "quick_ratio", "working_capital", "gross_margin", "ebitda_margin", "revenue_cushion"]


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]),
                      trim_blocks=True, lstrip_blocks=True)
    register_filters(env)
    return env


def _margin_ok(e: dict[str, Any]) -> bool:
    """A PASS is a 'strength' only if it clears the threshold by at least 20% (relative)."""
    try:
        obs, thr = Decimal(e["observed_value"]), Decimal(e["threshold"])
    except Exception:
        return False
    if thr == 0:
        return False
    if e["operator"] in (">=", ">"):
        return obs >= thr * Decimal("1.2")
    if e["operator"] in ("<=", "<"):
        return obs <= thr * Decimal("0.8")
    return False


def build_memo_context(conn: sqlite3.Connection, case_id: str, run_id: str, version: int,
                       analyst: dict[str, str] | None = None, narrative: Narrative | None = None) -> dict[str, Any]:
    run = get_run(conn, run_id, case_id)
    if run["scenario_name"]:
        raise cases_repo.ValidationFailure("Credit memos are generated from base-case runs, not stress scenarios.")
    case = cases_repo.get_case(conn, case_id)
    module = get_module(case["module_key"])
    catalog = module.field_catalog()
    m = run["metrics_by_key"]

    # Business financial history from VERIFIED facts only, by period, with sources.
    verified = facts_repo.list_facts(conn, case_id, statuses=("verified",))
    periods = sorted({(f["period_end"], f["period_months"], f["period_label"]) for f in verified
                      if f["field_key"] in HISTORY_FIELDS and f["period_months"]}, reverse=True)[:4]
    history = []
    for key in HISTORY_FIELDS:
        row = {"label": catalog[key].label, "cells": []}
        for pe, pm, _ in periods:
            f = next((x for x in verified if x["field_key"] == key and x["period_end"] == pe
                      and x["period_months"] == pm and not x["entity_ref"]), None)
            row["cells"].append({"value": f["value_numeric"] if f else None,
                                 "source": (f"{f.get('original_filename') or 'user entry'}"
                                            + (f" p.{f['page_number']}" if f and f.get("page_number") else ""))
                                 if f else None})
        if any(c["value"] for c in row["cells"]):
            history.append(row)

    evals = run["evaluations"]
    fails = [e for e in evals if e["result"] in ("FAIL", "REVIEW_REQUIRED")]
    unknown = [e for e in evals if e["result"] == "UNKNOWN"]
    metric_of = {e["rule_id"]: (e["subject"] or "").split(":", 1)[1] if (e["subject"] or "").startswith("metric:")
                 else None for e in evals}
    strengths = [f"{e['description']}: observed {e['observed_value']} vs requirement {e['operator']} {e['threshold']} "
                 f"(rule {e['rule_id']}, calculated)" for e in evals
                 if e["result"] == "PASS" and metric_of[e["rule_id"]] and _margin_ok(e)]
    fail_cats = {CATEGORY.get(metric_of[e["rule_id"]] or "", "other") for e in fails}
    mitigants = []
    if fails:
        mitigants = [f"{e['description']} passes ({e['observed_value']} vs {e['operator']} {e['threshold']}); may be "
                     f"relevant to the {', '.join(sorted(fail_cats))} exception(s) — for human evaluation only."
                     for e in evals if e["result"] == "PASS" and metric_of[e["rule_id"]]
                     and CATEGORY.get(metric_of[e["rule_id"]] or "") not in fail_cats]

    contradictions = find_contradictions(conn, case_id)
    checklist = missing_checklist(conn, case_id, "verified_only")
    questions = []
    for e in unknown:
        questions.append(f"[{e['rule_id']}] {e['explanation']}")
    for c in contradictions:
        refs = "; ".join(f"{f.get('original_filename') or f['source_type']} p.{f.get('page_number') or '?'} = "
                         f"{f.get('value_numeric') or f.get('value_text')}" for f in c["facts"])
        questions.append(f"Which figure is correct for {c['field_label']} ({c['period_label'] or 'n/a'})? {refs}")
    for item in checklist:
        if item["status"] in ("missing", "unverified"):
            questions.append(f"{item['label']}: {'; '.join(item['details'])}")

    suggestions = []
    for e in fails + unknown:
        subj = e["subject"] or ""
        if subj.startswith("documents:"):
            suggestions.append(f"Obtain and review: {subj.split(':', 1)[1].replace(',', ', ')} (rule {e['rule_id']}).")
        elif "valuation_age" in subj:
            suggestions.append(f"Obtain a current collateral valuation (rule {e['rule_id']}).")
        elif subj.startswith("metric:") and e["result"] == "FAIL":
            suggestions.append(f"Address {e['description'].lower()} — e.g. structure, amount, collateral or support "
                               f"(rule {e['rule_id']}); requires human judgement.")
    from uwhelper.policy.loader import policy_from_row

    prow = conn.execute("SELECT * FROM policies WHERE id=?", (run["policy_id"],)).fetchone()
    policy = policy_from_row(prow)
    for r in policy.rules:
        if r.type == "metric_threshold" and r.metric in ("dscr", "dscr_property") and \
                (r.applies_to == "all" or case["loan_type"] in r.applies_to):
            suggestions.append(f"Covenant: minimum {r.metric.upper()} of {r.threshold}x, tested annually on verified "
                               "financial statements (threshold from the case policy).")
    suggestions.append("Reporting: annual financial statements and tax returns within 120 days of fiscal year end.")
    open_conditions = [dict(r) for r in conn.execute(
        "SELECT code, description, status, required FROM loan_conditions WHERE case_id=? AND status NOT IN "
        "('SATISFIED','WAIVED') ORDER BY code", (case_id,))]

    proposed = []
    for s in conn.execute("SELECT id FROM loan_structures WHERE case_id=? AND status='proposed' ORDER BY created_at",
                          (case_id,)):
        from uwhelper.structuring import get_structure

        st = get_structure(conn, case_id, s["id"])
        proposed.append({"name": st["name"], "inputs": st["inputs"],
                         "results": {k: st["results"].get(k) for k in ("level_payment", "interest_only_payment",
                                                                       "year1_debt_service", "max_annual_debt_service",
                                                                       "balloon_payment", "dscr_minimum", "ltv",
                                                                       "total_interest")},
                         "policy_checks": st["results"].get("policy_checks", [])})

    status = run["overall_status"]
    opinion = {"INCOMPLETE": "INSUFFICIENT_INFORMATION", "REVIEW_REQUIRED": "FURTHER_REVIEW_REQUIRED"}.get(
        status, "NO_EXCEPTIONS_IDENTIFIED")
    unverified = facts_repo.list_facts(conn, case_id, statuses=("candidate",))
    for f in unverified:
        f["field_label"] = catalog[f["field_key"]].label if f["field_key"] in catalog else f["field_key"]
    docs = list_case_documents(conn, case_id)
    doc_types = module.document_types()
    required_docs = sorted({d for e in evals if (e["subject"] or "").startswith("documents:")
                            for d in e["subject"].split(":", 1)[1].split(",")})
    present = {d["doc_type"] for d in docs}

    def metric(k: str) -> dict[str, Any] | None:
        x = m.get(k)
        if not x or x["status"] == "not_applicable":
            return None
        return {"key": k, "label": x["label"], "value": x["value"], "unit": x["unit"], "status": x["status"],
                "formula": x["formula"], "flags": x["flags"], "explanation": x["explanation"],
                "inputs": [{"label": i["label"], "value": i["value"], "state": i["state"], "verified": i["verified"],
                            "sources": [f"{s.get('filename') or 'user entry'}"
                                        + (f" p.{s['page_number']}" if s.get("page_number") else "")
                                        for s in i["sources"]]} for i in x["inputs"]]}

    analyst = {k: (v or "").strip()[:4000] for k, v in (analyst or {}).items() if (v or "").strip()}
    ctx = {
        "generator_version": GENERATOR_VERSION, "app_version": __version__, "engine_version": ENGINE_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "memo_version": version,
        "case": {k: case[k] for k in ("id", "case_number", "title", "loan_type", "stage", "is_synthetic")},
        "loan_type_label": module.loan_types[case["loan_type"]].label,
        "borrower": {k: case["borrower"].get(k) for k in ("legal_name", "dba_name", "entity_type", "industry",
                                                           "naics_code", "state_of_formation", "year_established")},
        "terms": {k: (case["terms"] or {}).get(k) for k in ("revision", "requested_amount", "interest_rate", "rate_type",
                                                             "repayment_type", "term_months", "amortization_months",
                                                             "project_total_cost", "is_secured", "loan_purpose",
                                                             "requested_closing_date")},
        "run": {k: run[k] for k in ("id", "run_number", "created_at", "mode", "overall_status", "base_period_label",
                                    "inputs_sha256", "policy_key", "policy_version", "illustrative")},
        "policy_disclaimer": run["summary"]["policy"].get("disclaimer"),
        "opinion_code": opinion, "opinion_text": OPINIONS[opinion],
        "metrics": [x for x in (metric(k) for k in KEY_METRICS) if x],
        "history_periods": [lbl for _, _, lbl in periods], "history": history,
        "debts": [{k: d[k] for k in ("creditor", "description", "current_balance", "interest_rate", "payment_amount",
                                     "payment_frequency", "maturity_date", "to_be_refinanced", "verification_status")}
                  for d in cases_repo.list_debts(conn, case_id)],
        "debt_schedule_confirmed": bool(case["debt_schedule_confirmed"]),
        "collateral": [{"description": c["description"], "type": c["collateral_type"], "lien": c["lien_position"],
                        "prior_liens": c["prior_lien_balance"],
                        "value": c["current_valuation"]["value"] if c["current_valuation"] else None,
                        "date": c["current_valuation"]["valuation_date"] if c["current_valuation"] else None,
                        "method": c["current_valuation"]["valuation_method"] if c["current_valuation"] else None,
                        "status": c["current_valuation"]["verification_status"] if c["current_valuation"] else "missing"}
                       for c in cases_repo.list_collateral(conn, case_id)],
        "parties": [{k: p[k] for k in ("name", "party_type", "ownership_pct", "is_guarantor", "guarantee_type")}
                    for p in cases_repo.list_parties(conn, case_id)],
        "evaluations": [{k: e[k] for k in ("rule_id", "rule_version", "description", "severity", "result",
                                           "indicative_result", "observed_value", "operator", "threshold", "explanation")}
                        for e in evals],
        "exceptions": [{"rule_id": e["rule_id"], "result": e["result"], "description": e["description"],
                        "explanation": e["explanation"]} for e in fails + unknown],
        "strengths": strengths, "mitigants": mitigants,
        "contradictions": [{"field": c["field_label"], "period": c["period_label"],
                            "entries": [{"value": f.get("value_numeric") or f.get("value_text"),
                                        "source": f"{f.get('original_filename') or f['source_type']} p.{f.get('page_number') or '?'}",
                                        "status": f["status"]} for f in c["facts"]]} for c in contradictions],
        "unverified": [{"field": f["field_label"], "period": f["period_label"], "value": f["value_numeric"] or f["value_text"],
                        "source": f"{f.get('original_filename') or f['source_type']} p.{f.get('page_number') or '?'}"}
                       for f in unverified],
        "missing_docs": [doc_types.get(d, d) for d in required_docs if d not in present],
        "documents": [{"name": d["original_filename"], "type": doc_types.get(d["doc_type"], d["doc_type"]),
                       "sha256": d["sha256"][:16]} for d in docs],
        "checklist": checklist, "questions": questions, "suggested_conditions": suggestions,
        "open_conditions": open_conditions, "proposed_structures": proposed,
        "analyst": analyst,
        "findings": [{"type": f["finding_type"], "message": f["message"]} for f in run["findings"]
                     if f["finding_type"] in ("assumption", "data_quality", "stale")],
        "limitations": LIMITATIONS, "review_checklist": HUMAN_REVIEW_CHECKLIST,
        "narrative": ({"text": narrative.text, "model": narrative.model, "unsupported": narrative.unsupported_numbers,
                       "decision_language": narrative.decision_language, "error": narrative.error}
                      if narrative else None),
    }
    return json.loads(json.dumps(ctx, default=str))  # normalise to plain JSON types


def render_memo(ctx: dict[str, Any], *, for_pdf: bool = False) -> str:
    return _env().get_template("memo.html").render(m=ctx, for_pdf=for_pdf)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def generate_memo(conn: sqlite3.Connection, settings: Settings, case_id: str, run_id: str, *,
                  analyst: dict[str, str] | None = None, narrative: Narrative | None = None,
                  pdf: bool = True) -> dict[str, Any]:
    row = conn.execute("SELECT COALESCE(MAX(version),0) AS v FROM credit_memos WHERE case_id=?", (case_id,)).fetchone()
    version = int(row["v"]) + 1
    ctx = build_memo_context(conn, case_id, run_id, version, analyst, narrative)
    ctx_json = json.dumps(ctx, sort_keys=True)
    html = render_memo(ctx).encode("utf-8")
    out_dir = settings.reports_dir / case_id
    out_dir.mkdir(parents=True, exist_ok=True)
    html_name = f"credit_memo_v{version}.html"
    pdf_name = f"credit_memo_v{version}.pdf" if pdf else None
    pdf_bytes = render_pdf(render_memo(ctx, for_pdf=True)) if pdf else None
    written = []
    try:
        (out_dir / html_name).write_bytes(html)
        written.append(out_dir / html_name)
        if pdf_bytes is not None:
            (out_dir / pdf_name).write_bytes(pdf_bytes)  # type: ignore[operator]
            written.append(out_dir / pdf_name)  # type: ignore[operator]
        mid = new_id()
        with Database.transaction(conn):
            conn.execute(
                """INSERT INTO credit_memos(id, case_id, run_id, version, generator_version, context_json, context_sha256,
                       html_file, html_sha256, pdf_file, pdf_sha256, opinion_code, includes_narrative, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, case_id, run_id, version, GENERATOR_VERSION, ctx_json, _sha(ctx_json.encode()), html_name,
                 _sha(html), pdf_name, _sha(pdf_bytes) if pdf_bytes else None, ctx["opinion_code"],
                 int(narrative is not None), utcnow()))
            audit.record(conn, "credit_memo_generated", "credit_memo", mid, case_id,
                         {"version": version, "run_id": run_id, "opinion": ctx["opinion_code"],
                          "html_sha256": _sha(html), "narrative": narrative is not None})
    except Exception:
        for p in written:
            p.unlink(missing_ok=True)
        raise
    return {"id": mid, "version": version, "opinion_code": ctx["opinion_code"]}


def list_memos(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        """SELECT m.id, m.version, m.run_id, m.opinion_code, m.html_file, m.pdf_file, m.html_sha256, m.created_at,
                  m.includes_narrative, r.run_number FROM credit_memos m JOIN underwriting_runs r ON r.id=m.run_id
           WHERE m.case_id=? ORDER BY m.version DESC""", (case_id,))]


def get_memo(conn: sqlite3.Connection, case_id: str, memo_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM credit_memos WHERE id=? AND case_id=?", (memo_id, case_id)).fetchone()
    if not row:
        raise cases_repo.NotFound("Credit memo not found")
    return dict(row)


def verify_reproduction(conn: sqlite3.Connection, case_id: str, memo_id: str) -> dict[str, Any]:
    """Re-render the stored context and compare with the stored HTML hash."""
    memo = get_memo(conn, case_id, memo_id)
    ctx = json.loads(memo["context_json"])
    ctx_ok = _sha(memo["context_json"].encode()) == memo["context_sha256"]
    html_ok = _sha(render_memo(ctx).encode("utf-8")) == memo["html_sha256"]
    return {"context_intact": ctx_ok, "html_reproduced": html_ok,
            "note": "A mismatch after an application upgrade can come from template changes; the stored context "
                    "(inputs) is what must match."}
