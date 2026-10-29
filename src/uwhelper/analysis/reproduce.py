"""Re-check a saved underwriting run.

From the run's stored input snapshot and the exact policy version it used (policies are stored with their full
content), recompute every metric and — for runs saved since Milestone 3, which also store the rule-evaluation
context — every rule result, and compare with what was saved. Nothing is written.

A difference means the calculation code changed since the run (see ``engine_version``) or stored data was altered.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from uwhelper import ENGINE_VERSION
from uwhelper.analysis.underwriting import inputs_digest
from uwhelper.metrics.types import AnalysisInputs
from uwhelper.modules import get_module
from uwhelper.policy.engine import EvaluationContext, PolicyEngine
from uwhelper.policy.schema import Policy
from uwhelper.repo.cases import NotFound


def verify_run(conn: sqlite3.Connection, case_id: str, run_id: str) -> dict[str, Any]:
    run = conn.execute("SELECT r.*, c.module_key FROM underwriting_runs r JOIN loan_cases c ON c.id=r.case_id "
                       "WHERE r.id=? AND r.case_id=?", (run_id, case_id)).fetchone()
    if not run:
        raise NotFound("Run not found")
    pol = conn.execute("SELECT policy_key, version, content_json FROM policies WHERE id=?", (run["policy_id"],)).fetchone()
    out: dict[str, Any] = {"run_number": run["run_number"], "engine_version_then": run["engine_version"],
                           "engine_version_now": ENGINE_VERSION,
                           "policy": f"{pol['policy_key']} v{pol['version']}" if pol else None,
                           "snapshot_intact": inputs_digest(run["inputs_json"]) == run["inputs_sha256"],
                           "metric_differences": [], "rule_differences": [], "rules_checked": False}
    if not pol:
        out["error"] = "The policy version used by this run is no longer stored."
        out["reproduced"] = False
        return out
    policy = Policy.model_validate(json.loads(pol["content_json"]))
    inputs = AnalysisInputs.from_dict(json.loads(run["inputs_json"]))
    metrics = get_module(run["module_key"]).compute_metrics(inputs, policy.parameters.model_dump())
    saved = {r["metric_key"]: r for r in conn.execute("SELECT * FROM run_metrics WHERE run_id=?", (run_id,))}
    for key, row in saved.items():
        m = metrics.get(key)
        now_value = None if m is None else (str(m.value) if m.value is not None else None)
        now_status = None if m is None else m.status
        if (row["value"], row["status"]) != (now_value, now_status):
            out["metric_differences"].append({"metric": key, "saved": [row["value"], row["status"]],
                                              "recomputed": [now_value, now_status]})
    out["metrics_checked"] = len(saved)
    summary = json.loads(run["summary_json"])
    ctx_data = summary.get("evaluation_context")
    if ctx_data:
        ctx = EvaluationContext(inputs.loan_type, inputs.terms.is_secured, metrics, inputs,
                                set(ctx_data["document_types"]), list(ctx_data["contradictions"]),
                                date.fromisoformat(ctx_data["as_of"]), int(ctx_data["guarantor_count"]))
        evals = {(e.rule_id, e.rule_version): e for e in PolicyEngine(policy).evaluate(ctx)}
        for row in conn.execute("SELECT * FROM rule_evaluations WHERE run_id=? ORDER BY id", (run_id,)):
            e = evals.get((row["rule_id"], row["rule_version"]))
            now = None if e is None else [str(e.result), e.observed_value]
            if [row["result"], row["observed_value"]] != now:
                out["rule_differences"].append({"rule": row["rule_id"], "saved": [row["result"], row["observed_value"]],
                                                "recomputed": now})
        out["rules_checked"] = True
    out["reproduced"] = out["snapshot_intact"] and not out["metric_differences"] and not out["rule_differences"]
    return out
