"""Explainable matching of a deal against lender programs.

For every active program, each check is PASS, FAIL or UNKNOWN with the observed value and a reason:
* built-in eligibility from the program record (deal type, product, borrower state, amount range);
* the criteria of the version in effect today (each cites the criteria version).

Values come from the deal record and from the linked underwriting file's latest **verified-only** run (metrics,
inputs, proposed terms). UNKNOWN is never counted as PASS. Programs are grouped — "meets all checks", "possible
(unknowns)", "not a fit (a required check fails)" — and ordered only by those visible counts; there is no hidden
score. Criteria confirmed more than ``criteria_max_age_days`` ago are flagged stale.

Each matching is saved immutably (results JSON + SHA-256) so a later submission can cite exactly what was shown.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from uwhelper.core.identity import current_actor
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.lenders import service as lenders
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound

FIT_ORDER = {"meets_all": 0, "possible": 1, "not_a_fit": 2}
FIT_TEXT = {"meets_all": "Meets every recorded check", "possible": "Possible — some checks could not be evaluated",
            "not_a_fit": "Not a fit — a required check fails"}


def _dec(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return Decimal(str(v).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


class _Facts:
    """Values available to criteria for one deal."""

    def __init__(self, conn: sqlite3.Connection, deal: dict[str, Any]):
        from uwhelper.workflow.currency import latest_base_run, run_status

        self.deal = deal
        org = conn.execute("SELECT state FROM organizations WHERE id=?",
                           (deal["borrower_organization_id"],)).fetchone() if deal["borrower_organization_id"] else None
        self.borrower_state = (org["state"] or "").strip().upper() or None if org else None
        self.run = latest_base_run(conn, deal["loan_case_id"]) if deal["loan_case_id"] else None
        self.run_current = run_status(conn, deal["loan_case_id"])["current"] if self.run else None
        self.metrics: dict[str, dict[str, Any]] = {}
        self.inputs: dict[str, Any] = {}
        if self.run:
            for r in conn.execute("SELECT metric_key, label, value, unit, status, flags_json FROM run_metrics "
                                  "WHERE run_id=?", (self.run["id"],)):
                d = dict(r)
                d["flags"] = json.loads(d.pop("flags_json"))
                self.metrics[d["metric_key"]] = d
            self.inputs = json.loads(self.run["inputs_json"])

    def value(self, subject: str) -> tuple[Any, str | None, list[str]]:
        """(value, reason-if-unknown, flags)"""
        kind, name = subject.split(":", 1)
        run_no = f"run #{self.run['run_number']}" if self.run else None
        if kind == "deal":
            if name == "borrower_state":
                return (self.borrower_state, None if self.borrower_state else "borrower organization has no state", [])
            v = self.deal.get(name)
            return v, None if v not in (None, "") else f"deal {name.replace('_', ' ')} not entered", []
        if not self.run:
            why = "no underwriting file linked" if not self.deal["loan_case_id"] else "no verified-only screening run yet"
            return None, why, []
        if kind == "metric":
            m = self.metrics.get(name)
            if not m or m["status"] != "computed" or m["value"] is None:
                label = m["label"] if m else name
                return None, f"{label} not calculated in {run_no}", []
            return Decimal(m["value"]), None, (["unverified input"] if "unverified" in m["flags"] else []) + \
                (["stale input"] if "stale" in m["flags"] else [])
        if kind == "input":
            iv = (self.inputs.get("values") or {}).get(name)
            if not iv or iv.get("state") != "ok" or iv.get("value") is None:
                return None, f"{name.replace('_', ' ')} not available in {run_no}", []
            return Decimal(iv["value"]), None, [] if iv.get("verified") else ["unverified input"]
        if kind == "terms":
            v = (self.inputs.get("terms") or {}).get(name)
            return v, None if v is not None else f"terms {name.replace('_', ' ')} not entered", []
        if kind == "case":
            if name == "loan_type":
                return self.inputs.get("loan_type"), None, []
            if name == "is_secured":
                return (self.inputs.get("terms") or {}).get("is_secured"), None, []
        return None, "unsupported subject", []


def _norm(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v).strip().lower()


def evaluate(subject_value: Any, op: str, target: Any) -> tuple[str, str]:
    """(result, explanation) for a known value."""
    if op == "is_known":
        return "PASS", "value is available"
    if op == "is_unknown":
        return "FAIL", "value is available but the criterion expects it to be unknown"
    if op in (">=", ">", "<=", "<"):
        a, b = _dec(subject_value), _dec(target)
        if a is None or b is None:
            return "UNKNOWN", "not a number"
        ok = {">=": a >= b, ">": a > b, "<=": a <= b, "<": a < b}[op]
        return ("PASS" if ok else "FAIL"), f"{a} {op} {b} is {'met' if ok else 'NOT met'}"
    if op in ("==", "!="):
        a, b = _dec(subject_value), _dec(target)
        same = (a == b) if a is not None and b is not None else _norm(subject_value) == _norm(target)
        ok = same if op == "==" else not same
        return ("PASS" if ok else "FAIL"), f"{_norm(subject_value)} {op} {_norm(target)} is {'met' if ok else 'NOT met'}"
    if op in ("in", "not_in"):
        inside = _norm(subject_value) in {_norm(x) for x in target}
        ok = inside if op == "in" else not inside
        return ("PASS" if ok else "FAIL"), f"{subject_value} {'is' if inside else 'is not'} in {', '.join(map(str, target))}"
    return "UNKNOWN", f"unsupported operator {op}"


def _check(cid: str, description: str, subject: str, op: str, target: Any, required: bool, facts: _Facts,
           source: str) -> dict[str, Any]:
    v, why, flags = facts.value(subject)
    if op == "is_unknown" and v is None:
        result, expl = "PASS", "value is not available, as expected"
    elif v is None:
        result, expl = "UNKNOWN", why or "value not available"
    else:
        result, expl = evaluate(v, op, target)
    return {"id": cid, "description": description, "subject": subject, "op": op, "target": target,
            "required": required, "observed": None if v is None else (str(v) if not isinstance(v, bool) else _norm(v)),
            "result": result, "explanation": expl, "flags": flags, "source": source}


def _program_checks(p: dict[str, Any], facts: _Facts) -> list[dict[str, Any]]:
    out = []
    if p["deal_types"]:
        out.append(_check("ELIG-DEAL-TYPE", "Deal type offered by the program", "deal:deal_type", "in",
                          p["deal_types"], True, facts, "program"))
    if p["product_types"]:
        out.append(_check("ELIG-PRODUCT", "Product offered by the program", "deal:product_type", "in",
                          p["product_types"], True, facts, "program"))
    if p["states"]:
        out.append(_check("ELIG-STATE", "Borrower state in the program's footprint", "deal:borrower_state", "in",
                          p["states"], True, facts, "program"))
    if p["min_amount"] is not None:
        out.append(_check("ELIG-MIN-AMOUNT", "Requested amount at or above the program minimum",
                          "deal:requested_amount", ">=", p["min_amount"], True, facts, "program"))
    if p["max_amount"] is not None:
        out.append(_check("ELIG-MAX-AMOUNT", "Requested amount at or below the program maximum",
                          "deal:requested_amount", "<=", p["max_amount"], True, facts, "program"))
    return out


def compute_matches(conn: sqlite3.Connection, deal_id: str, *, max_age_days: int = 180,
                    today: date | None = None) -> dict[str, Any]:
    from uwhelper.crm import service as crm

    today = today or date.today()
    deal = crm.get_deal(conn, deal_id)
    facts = _Facts(conn, deal)
    programs = []
    for p in lenders.list_programs(conn):
        ver = lenders.current_version(conn, p["id"], today)
        checks = _program_checks(p, facts)
        if ver:
            src = f"criteria v{ver['version']}"
            checks += [_check(c["id"], c["description"], c["subject"], c["op"], c["value"], c["required"], facts, src)
                       for c in ver["criteria"]]
        counts = {"PASS": 0, "FAIL": 0, "UNKNOWN": 0}
        for c in checks:
            counts[c["result"]] += 1
        req_fail = sum(1 for c in checks if c["result"] == "FAIL" and c["required"])
        fit = "not_a_fit" if req_fail else ("possible" if counts["UNKNOWN"] or not ver else "meets_all")
        age = lenders.age_days(ver["verified_at"], today) if ver else None
        programs.append({
            "program_id": p["id"], "program": p["name"], "lender_id": p["organization_id"], "lender": p["lender_name"],
            "criteria_version": ver["version"] if ver else None, "criteria_sha256": ver["criteria_sha256"] if ver else None,
            "verified_at": ver["verified_at"] if ver else None, "verified_by": ver["verified_by"] if ver else None,
            "criteria_source": ver["source"] if ver else None, "age_days": age,
            "stale": age is not None and age > max_age_days, "no_criteria": ver is None,
            "counts": counts, "required_failures": req_fail, "fit": fit, "fit_text": FIT_TEXT[fit], "checks": checks,
        })
    programs.sort(key=lambda r: (FIT_ORDER[r["fit"]], r["required_failures"], r["counts"]["UNKNOWN"],
                                 -r["counts"]["PASS"], r["lender"].lower(), r["program"].lower()))
    return {"deal_id": deal_id, "deal_number": deal["deal_number"], "as_of": today.isoformat(),
            "max_age_days": max_age_days,
            "run": None if not facts.run else {"run_number": facts.run["run_number"], "created_at": facts.run["created_at"],
                                               "current": facts.run_current, "inputs_sha256": facts.run["inputs_sha256"]},
            "programs": programs,
            "summary": {k: sum(1 for p in programs if p["fit"] == k) for k in FIT_ORDER}}


def run_matching(conn: sqlite3.Connection, deal_id: str, *, max_age_days: int = 180,
                 today: date | None = None) -> str:
    res = compute_matches(conn, deal_id, max_age_days=max_age_days, today=today)
    deal = conn.execute("SELECT loan_case_id FROM deals WHERE id=?", (deal_id,)).fetchone()
    run_id = None
    if res["run"]:
        r = conn.execute("SELECT id FROM underwriting_runs WHERE case_id=? AND run_number=?",
                         (deal["loan_case_id"], res["run"]["run_number"])).fetchone()
        run_id = r["id"] if r else None
    body = json.dumps(res, sort_keys=True, default=str)
    mid, now = new_id(), utcnow()
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO match_runs(id, deal_id, case_id, run_id, run_number, run_current, max_age_days, results_json,
                   results_sha256, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, deal_id, deal["loan_case_id"], run_id, res["run"]["run_number"] if res["run"] else None,
             None if not res["run"] else int(bool(res["run"]["current"])), max_age_days, body,
             hashlib.sha256(body.encode()).hexdigest(), current_actor(), now))
        audit.record(conn, "lender_matching_run", "deal", deal_id, deal["loan_case_id"],
                     {"match_id": mid, "programs": len(res["programs"]), **res["summary"],
                      "run_number": res["run"]["run_number"] if res["run"] else None,
                      "criteria_versions": {p["program_id"]: p["criteria_version"] for p in res["programs"]}})
    return mid


def get_match_run(conn: sqlite3.Connection, deal_id: str, match_id: str) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM match_runs WHERE id=? AND deal_id=?", (match_id, deal_id)).fetchone()
    if not r:
        raise NotFound("Matching result not found")
    d = dict(r)
    d["results"] = json.loads(d.pop("results_json"))
    d["intact"] = hashlib.sha256(json.dumps(d["results"], sort_keys=True, default=str).encode()).hexdigest() \
        == d["results_sha256"]
    return d


def list_match_runs(conn: sqlite3.Connection, deal_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT id, run_number, run_current, created_at, created_by FROM match_runs WHERE deal_id=? "
        "ORDER BY created_at DESC, rowid DESC", (deal_id,))]
