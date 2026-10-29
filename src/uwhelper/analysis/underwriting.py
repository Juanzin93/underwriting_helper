"""Orchestrates an underwriting run and saves it atomically.

A run snapshot stores: the policy version id, every input (with provenance),
every metric (formula + inputs), every rule evaluation, evidence references
and findings. Runs are immutable; re-running creates a new run number.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from uwhelper import ENGINE_VERSION
from uwhelper.analysis.resolver import (
    build_inputs,
    find_contradictions,
    missing_checklist,
)
from uwhelper.analysis.scenarios import apply_scenario, sensitivity_grid
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.domain.enums import OVERALL_STATUS_TEXT
from uwhelper.modules import get_module
from uwhelper.policy.engine import EvaluationContext, PolicyEngine, overall_status
from uwhelper.policy.loader import PolicyError, resolve_policy
from uwhelper.repo import audit
from uwhelper.repo import cases as cases_repo


class RunError(RuntimeError):
    pass


@dataclass
class RunResult:
    run_id: str
    run_number: int
    overall_status: str


def default_policy_key(loan_type: str) -> str:
    return "demo_cre" if loan_type == "commercial_real_estate" else "demo_commercial_general"


def compute_run(conn: sqlite3.Connection, case_id: str, *, mode: str = "verified_only",
                scenario: dict[str, Any] | None = None, as_of: date | None = None) -> dict[str, Any]:
    """Compute everything for a run without saving (pure w.r.t. the database)."""
    if mode not in ("verified_only", "include_unverified"):
        raise RunError("invalid run mode")
    as_of = as_of or date.today()
    case = cases_repo.get_case(conn, case_id)
    module = get_module(case["module_key"])
    policy_key = case.get("policy_key") or default_policy_key(case["loan_type"])
    try:
        policy_id, policy = resolve_policy(conn, policy_key, case.get("policy_version"))
    except PolicyError as exc:
        raise RunError(str(exc)) from exc
    if case["loan_type"] not in policy.loan_types:
        raise RunError(f"Policy {policy.policy_key} v{policy.version} does not cover loan type {case['loan_type']}.")

    base_inputs, findings = build_inputs(conn, case_id, mode, as_of)
    params = policy.parameters.model_dump()
    inputs = apply_scenario(base_inputs, scenario["adjustments"]) if scenario else base_inputs
    metrics = module.compute_metrics(inputs, params)

    contradictions = find_contradictions(conn, case_id)
    for c in contradictions:
        findings.append({"finding_type": "contradiction", "severity": "high", "field_key": c["field_key"],
                         "period_label": c["period_label"],
                         "message": f"{c['field_label']}: sources disagree ({' vs '.join(c['values'])}).",
                         "details": {"fact_ids": [f["id"] for f in c["facts"]]}})
    for item in missing_checklist(conn, case_id, mode):
        if item["status"] in ("missing", "unverified"):
            findings.append({"finding_type": "missing" if item["status"] == "missing" else "unverified",
                             "severity": "medium", "field_key": item["key"], "period_label": inputs.period_label,
                             "message": f"{item['label']}: " + "; ".join(item["details"])})
    doc_types = {r["doc_type"] for r in conn.execute("SELECT doc_type FROM case_documents WHERE case_id=?", (case_id,))}
    guarantors = conn.execute("SELECT COUNT(*) AS n FROM parties WHERE case_id=? AND is_guarantor=1",
                              (case_id,)).fetchone()["n"]
    ctx = EvaluationContext(case["loan_type"], inputs.terms.is_secured, metrics, inputs, doc_types,
                            contradictions, as_of, guarantors)
    evaluations = PolicyEngine(policy).evaluate(ctx)
    status = overall_status(evaluations, contradictions, mode)
    sensitivity = sensitivity_grid(module.compute_metrics, inputs, params,
                                   policy.parameters.sensitivity_revenue_shocks,
                                   policy.parameters.sensitivity_rate_shocks_bps,
                                   policy.parameters.sensitivity_expense_shocks)
    return {
        "case": case, "policy_id": policy_id, "policy": policy, "inputs": inputs, "metrics": metrics,
        "evaluations": evaluations, "findings": findings, "contradictions": contradictions,
        "overall_status": status, "sensitivity": sensitivity, "mode": mode, "as_of": as_of,
        "doc_types": doc_types, "guarantor_count": guarantors,
    }


def evaluation_context(r: dict[str, Any]) -> dict[str, Any]:
    return {"as_of": r["as_of"].isoformat(), "document_types": sorted(r["doc_types"]),
            "contradictions": sorted(({"field_key": c["field_key"], "period_label": c.get("period_label")}
                                      for c in r["contradictions"]), key=lambda c: (c["field_key"], c["period_label"] or "")),
            "guarantor_count": r["guarantor_count"]}


def inputs_digest(inputs_json: str) -> str:
    return hashlib.sha256(inputs_json.encode("utf-8")).hexdigest()


def run_underwriting(conn: sqlite3.Connection, case_id: str, *, mode: str = "verified_only",
                     scenario_id: str | None = None, as_of: date | None = None) -> RunResult:
    scenario = None
    if scenario_id:
        row = conn.execute("SELECT * FROM scenarios WHERE id=? AND case_id=?", (scenario_id, case_id)).fetchone()
        if not row:
            raise RunError("scenario not found")
        scenario = {"id": row["id"], "name": row["name"], "adjustments": json.loads(row["adjustments_json"])}
    r = compute_run(conn, case_id, mode=mode, scenario=scenario, as_of=as_of)
    inputs_json = json.dumps(r["inputs"].to_dict(), sort_keys=True)
    summary = {
        "overall_status_text": OVERALL_STATUS_TEXT[r["overall_status"]],
        "policy": {"key": r["policy"].policy_key, "version": r["policy"].version, "name": r["policy"].name,
                   "illustrative": r["policy"].illustrative, "disclaimer": r["policy"].disclaimer},
        "as_of": r["as_of"].isoformat(),
        "result_counts": _counts(r["evaluations"]),
        "sensitivity": r["sensitivity"],
        "scenario": scenario,
        # Everything besides the inputs that rules look at, so rule results can be re-checked later.
        "evaluation_context": evaluation_context(r),
    }
    run_id = new_id()
    with Database.transaction(conn):
        row = conn.execute("SELECT COALESCE(MAX(run_number),0) AS n FROM underwriting_runs WHERE case_id=?",
                           (case_id,)).fetchone()
        number = int(row["n"]) + 1
        conn.execute(
            """INSERT INTO underwriting_runs(id, case_id, run_number, policy_id, scenario_id, scenario_name, mode,
                   base_period_label, engine_version, inputs_json, inputs_sha256, overall_status, summary_json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, case_id, number, r["policy_id"], scenario_id, scenario["name"] if scenario else None, mode,
             r["inputs"].period_label, ENGINE_VERSION, inputs_json, inputs_digest(inputs_json),
             str(r["overall_status"]), json.dumps(summary, sort_keys=True, default=str), utcnow()),
        )
        for m in r["metrics"].values():
            d = m.to_dict()
            conn.execute(
                """INSERT INTO run_metrics(run_id, metric_key, label, value, unit, status, formula, inputs_json,
                       explanation, flags_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (run_id, m.key, m.label, d["value"], m.unit, m.status, m.formula, json.dumps(d["inputs"]),
                 m.explanation, json.dumps(d["flags"])),
            )
            for src in _metric_sources(m):
                conn.execute(
                    """INSERT INTO evidence_refs(run_id, metric_key, fact_id, document_id, page_number, description)
                       VALUES (?,?,?,?,?,?)""",
                    (run_id, m.key, src.get("fact_id"), src.get("document_id"), src.get("page_number"),
                     src.get("input") or m.label),
                )
        for e in r["evaluations"]:
            cur = conn.execute(
                """INSERT INTO rule_evaluations(run_id, rule_id, rule_version, description, severity, result,
                       indicative_result, subject, observed_value, operator, threshold, explanation, requires_review,
                       policy_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, e.rule_id, e.rule_version, e.description, e.severity, str(e.result),
                 str(e.indicative_result) if e.indicative_result else None, e.subject, e.observed_value, e.operator,
                 e.threshold, e.explanation, int(e.requires_review), e.policy_source),
            )
            for src in e.evidence:
                conn.execute(
                    """INSERT INTO evidence_refs(run_id, rule_evaluation_id, fact_id, document_id, page_number, description)
                       VALUES (?,?,?,?,?,?)""",
                    (run_id, cur.lastrowid, src.get("fact_id"), src.get("document_id"), src.get("page_number"),
                     src.get("input") or e.rule_id),
                )
        for f in r["findings"]:
            conn.execute(
                """INSERT INTO run_findings(run_id, finding_type, severity, field_key, period_label, message, details_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (run_id, f["finding_type"], f["severity"], f.get("field_key"), f.get("period_label"), f["message"],
                 json.dumps(f.get("details") or {})),
            )
        cases_repo.touch(conn, case_id)
        audit.record(conn, "underwriting_run_saved", "underwriting_run", run_id, case_id,
                     {"run_number": number, "mode": mode, "policy": f"{r['policy'].policy_key} v{r['policy'].version}",
                      "scenario_id": scenario_id, "overall_status": str(r["overall_status"]),
                      "inputs_sha256": inputs_digest(inputs_json)})
    return RunResult(run_id, number, str(r["overall_status"]))


def _metric_sources(m) -> list[dict[str, Any]]:
    seen, out = set(), []
    for i in m.inputs:
        for s in i.sources:
            key = (s.get("document_id"), s.get("page_number"), i.label)
            if key in seen or (not s.get("fact_id") and not s.get("document_id")):
                continue
            seen.add(key)
            out.append({**s, "input": i.label})
    return out


def _counts(evals) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in evals:
        counts[str(e.result)] = counts.get(str(e.result), 0) + 1
    return counts


def get_run(conn: sqlite3.Connection, run_id: str, case_id: str | None = None) -> dict[str, Any]:
    row = conn.execute("SELECT r.*, p.policy_key, p.version AS policy_version, p.name AS policy_name, "
                       "p.illustrative FROM underwriting_runs r LEFT JOIN policies p ON p.id=r.policy_id WHERE r.id=?",
                       (run_id,)).fetchone()
    if not row or (case_id and row["case_id"] != case_id):
        raise cases_repo.NotFound("run not found")
    run = dict(row)
    run["summary"] = json.loads(row["summary_json"])
    run["inputs"] = json.loads(row["inputs_json"])
    run["metrics"] = []
    for m in conn.execute("SELECT * FROM run_metrics WHERE run_id=? ORDER BY id", (run_id,)):
        d = dict(m)
        d["inputs"] = json.loads(m["inputs_json"])
        d["flags"] = json.loads(m["flags_json"])
        run["metrics"].append(d)
    run["metrics_by_key"] = {m["metric_key"]: m for m in run["metrics"]}
    run["evaluations"] = []
    for e in conn.execute("SELECT * FROM rule_evaluations WHERE run_id=? ORDER BY id", (run_id,)):
        d = dict(e)
        d["evidence"] = [dict(x) for x in conn.execute(
            """SELECT ev.*, doc.original_filename FROM evidence_refs ev LEFT JOIN documents doc ON doc.id=ev.document_id
               WHERE ev.rule_evaluation_id=?""", (e["id"],))]
        run["evaluations"].append(d)
    run["findings"] = [dict(f) for f in conn.execute("SELECT * FROM run_findings WHERE run_id=? ORDER BY id", (run_id,))]
    run["metric_evidence"] = [dict(x) for x in conn.execute(
        """SELECT ev.*, doc.original_filename FROM evidence_refs ev LEFT JOIN documents doc ON doc.id=ev.document_id
           WHERE ev.run_id=? AND ev.metric_key IS NOT NULL""", (run_id,))]
    return run


def list_runs(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT r.id, r.run_number, r.mode, r.scenario_name, r.base_period_label, r.overall_status, r.created_at,
                  r.inputs_sha256, r.engine_version, p.policy_key, p.version AS policy_version
           FROM underwriting_runs r LEFT JOIN policies p ON p.id=r.policy_id WHERE r.case_id=?
           ORDER BY r.run_number DESC""", (case_id,)).fetchall()
    return [dict(r) for r in rows]


def latest_run_id(conn: sqlite3.Connection, case_id: str, include_scenarios: bool = False) -> str | None:
    sql = "SELECT id FROM underwriting_runs WHERE case_id=?"
    if not include_scenarios:
        sql += " AND scenario_id IS NULL AND scenario_name IS NULL"
    row = conn.execute(sql + " ORDER BY run_number DESC LIMIT 1", (case_id,)).fetchone()
    return row["id"] if row else None


def create_scenario(conn: sqlite3.Connection, case_id: str, name: str, description: str | None,
                    adjustments: dict[str, Any]) -> str:
    sid = new_id()
    with Database.transaction(conn):
        conn.execute("INSERT INTO scenarios(id, case_id, name, description, adjustments_json, created_at) VALUES (?,?,?,?,?,?)",
                     (sid, case_id, name, description, json.dumps(adjustments, sort_keys=True), utcnow()))
        audit.record(conn, "scenario_created", "scenario", sid, case_id, {"name": name, "adjustments": adjustments})
    return sid


def list_scenarios(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute("SELECT * FROM scenarios WHERE case_id=? ORDER BY created_at", (case_id,)):
        d = dict(r)
        d["adjustments"] = json.loads(r["adjustments_json"])
        last = conn.execute("SELECT id, run_number, overall_status FROM underwriting_runs WHERE scenario_id=? "
                            "ORDER BY run_number DESC LIMIT 1", (r["id"],)).fetchone()
        d["last_run"] = dict(last) if last else None
        out.append(d)
    return out
