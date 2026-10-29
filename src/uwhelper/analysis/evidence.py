"""Evidence trails: from a metric in a saved run back to facts, pages, extraction runs and people.

Each metric input is classified as:
* ``fact``       — one or more facts (extracted or user-entered) with their document/page and verification history;
* ``record``     — a schedule entry (existing debt, collateral valuation, rent roll) with its own verification status;
* ``assumption`` — a user-entered structural assumption (proposed loan terms, prior liens) or a policy parameter;
* ``derived``    — another metric of the same run (traced recursively);
* ``missing``    — not available (the metric was then not calculated);
* ``untraced``   — a value with no recorded origin (should not happen; reported so it can be investigated).

A computed metric is *traced* when every input is a fact, record or assumption, or a derived metric that is itself
traced. All data is read from the saved run, so the trail shows what was used at the time, plus each fact's current
status for comparison.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from uwhelper.repo.cases import NotFound

ASSUMPTION_FLAGS = {"proposed_terms", "policy_parameter", "user_entered"}


def _run(conn: sqlite3.Connection, case_id: str, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM underwriting_runs WHERE id=? AND case_id=?", (run_id, case_id)).fetchone()
    if not row:
        raise NotFound("Run not found")
    return row


def _metrics(conn: sqlite3.Connection, run_id: str) -> dict[str, dict[str, Any]]:
    out = {}
    for r in conn.execute("SELECT * FROM run_metrics WHERE run_id=?", (run_id,)):
        d = dict(r)
        d["inputs"] = json.loads(d.pop("inputs_json"))
        d["flags"] = json.loads(d.pop("flags_json"))
        out[d["metric_key"]] = d
    return out


def classify(inp: dict[str, Any], metrics: dict[str, dict[str, Any]]) -> str:
    flags = set(inp.get("flags") or [])
    if inp.get("state") != "ok":
        return "missing"
    if "derived" in flags and inp.get("name") in metrics:
        return "derived"
    if inp.get("fact_ids"):
        return "fact"
    if flags & ASSUMPTION_FLAGS:
        return "assumption"
    if any(s.get("source_type") for s in inp.get("sources") or []):
        return "record"
    return "untraced"


def _traced(key: str, metrics: dict[str, dict[str, Any]], seen: set[str]) -> tuple[bool, list[str]]:
    if key in seen:  # defensive: metrics never depend on themselves
        return True, []
    seen = seen | {key}
    m = metrics[key]
    problems: list[str] = []
    for inp in m["inputs"]:
        kind = classify(inp, metrics)
        if kind == "untraced":
            problems.append(f"{inp['label']}: no recorded source")
        elif kind == "derived":
            ok, sub = _traced(inp["name"], metrics, seen)
            if not ok:
                problems.extend(f"{inp['label']} → {p}" for p in sub)
    return not problems, problems


def run_traceability(conn: sqlite3.Connection, case_id: str, run_id: str) -> dict[str, Any]:
    _run(conn, case_id, run_id)
    metrics = _metrics(conn, run_id)
    rows = []
    for key, m in metrics.items():
        if m["status"] != "computed":
            continue
        ok, problems = _traced(key, metrics, set())
        uses_unverified = "unverified" in m["flags"]
        rows.append({"metric_key": key, "label": m["label"], "traced": ok, "problems": problems,
                     "uses_unverified": uses_unverified})
    return {"computed": len(rows), "traced": sum(r["traced"] for r in rows),
            "untraced": [r for r in rows if not r["traced"]], "unverified": [r for r in rows if r["uses_unverified"]],
            "rows": rows}


def _fact_detail(conn: sqlite3.Connection, fact_id: str) -> dict[str, Any] | None:
    from uwhelper.llm.extraction import extraction_provenance

    row = conn.execute(
        """SELECT f.*, d.original_filename, p.label AS period_label FROM facts f
           LEFT JOIN documents d ON d.id=f.document_id LEFT JOIN financial_periods p ON p.id=f.period_id
           WHERE f.id=?""", (fact_id,)).fetchone()
    if not row:
        return None
    f = dict(row)
    f["events"] = [dict(e) for e in conn.execute(
        "SELECT event_type, from_status, to_status, note, actor, occurred_at FROM fact_events WHERE fact_id=? ORDER BY id",
        (fact_id,))]
    f["verified_by"] = next((e["actor"] for e in reversed(f["events"]) if e["to_status"] == "verified"), None)
    f["provenance"] = extraction_provenance(conn, f["extraction_run_id"])
    if f["supersedes_fact_id"]:
        f["corrects"] = conn.execute("SELECT id, value_numeric, value_text FROM facts WHERE id=?",
                                     (f["supersedes_fact_id"],)).fetchone()
        f["corrects"] = dict(f["corrects"]) if f["corrects"] else None
    return f


def metric_trail(conn: sqlite3.Connection, case_id: str, run_id: str, metric_key: str) -> dict[str, Any]:
    run = _run(conn, case_id, run_id)
    metrics = _metrics(conn, run_id)
    if metric_key not in metrics:
        raise NotFound("Metric not found in this run")
    m = metrics[metric_key]
    inputs = []
    for inp in m["inputs"]:
        kind = classify(inp, metrics)
        item = {**inp, "kind": kind, "facts": [], "records": []}
        if kind == "fact":
            item["facts"] = [f for f in (_fact_detail(conn, fid) for fid in inp["fact_ids"]) if f]
        elif kind == "record":
            item["records"] = inp.get("sources") or []
        elif kind == "derived":
            dm = metrics[inp["name"]]
            item["derived"] = {"metric_key": dm["metric_key"], "label": dm["label"], "value": dm["value"], "unit": dm["unit"],
                               "formula": dm["formula"], "traced": _traced(dm["metric_key"], metrics, set())[0]}
        inputs.append(item)
    traced, problems = _traced(metric_key, metrics, set()) if m["status"] == "computed" else (False, [])
    return {"run": {"id": run["id"], "run_number": run["run_number"], "mode": run["mode"],
                    "created_at": run["created_at"], "inputs_sha256": run["inputs_sha256"]},
            "metric": m, "inputs": inputs, "traced": traced, "problems": problems}
