"""Covenants, covenant tests, rule-based alerts and the portfolio overview.

Alerts come only from explicit deterministic rules over recorded data (schedules, payments,
covenant tests, monitoring items). No model predictions are used.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.domain.models import _dec
from uwhelper.domain.money import dstr
from uwhelper.metrics.structuring import add_months
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure
from uwhelper.servicing.ledger import account_state, get_account, monitoring_items

COVENANT_METRICS = {
    "dscr": "Business DSCR", "global_dscr": "Global DSCR", "current_ratio": "Current ratio",
    "quick_ratio": "Quick ratio", "debt_to_equity": "Debt-to-equity", "debt_to_ebitda": "Funded debt / EBITDA",
    "working_capital": "Working capital ($)", "total_equity": "Total equity ($)", "ltv": "Loan-to-value",
    "manual": "Other (manual measurement)",
}
FREQ_MONTHS = {"quarterly": 3, "semiannual": 6, "annual": 12}
OPS = {">=": lambda a, b: a >= b, ">": lambda a, b: a > b, "<=": lambda a, b: a <= b, "<": lambda a, b: a < b}


class CovenantIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_\-]{1,30}$")
    description: str = Field(min_length=3, max_length=500)
    metric_key: str
    operator: Literal[">=", ">", "<=", "<"]
    threshold: Decimal
    frequency: Literal["quarterly", "semiannual", "annual"] = "annual"
    first_test_date: date
    reporting_days: int = Field(default=120, ge=0, le=365)

    @field_validator("metric_key")
    @classmethod
    def _m(cls, v: str) -> str:
        if v not in COVENANT_METRICS:
            raise ValueError("unknown covenant metric")
        return v

    @field_validator("threshold", mode="before")
    @classmethod
    def _t(cls, v: object) -> Decimal:
        d = _dec(v, required=True)
        assert d is not None
        return d


def add_covenant(conn: sqlite3.Connection, account_id: str, c: CovenantIn) -> str:
    acct = get_account(conn, account_id)
    if conn.execute("SELECT 1 FROM covenants WHERE account_id=? AND code=?", (account_id, c.code)).fetchone():
        raise ValidationFailure("A covenant with that code exists.")
    cid = new_id()
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO covenants(id, account_id, code, description, metric_key, operator, threshold, frequency,
                   first_test_date, reporting_days, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, account_id, c.code, c.description, c.metric_key, c.operator, dstr(c.threshold), c.frequency,
             c.first_test_date.isoformat(), c.reporting_days, utcnow()))
        audit.record(conn, "covenant_added", "covenant", cid, acct["case_id"],
                     {"code": c.code, "metric": c.metric_key, "operator": c.operator, "threshold": dstr(c.threshold)})
    return cid


def set_covenant_active(conn: sqlite3.Connection, account_id: str, covenant_id: str, active: bool, reason: str) -> None:
    acct = get_account(conn, account_id)
    if len((reason or "").strip()) < 10:
        raise ValidationFailure("Give a reason (at least 10 characters).")
    with Database.transaction(conn):
        cur = conn.execute("UPDATE covenants SET active=? WHERE id=? AND account_id=?", (int(active), covenant_id, account_id))
        if cur.rowcount == 0:
            raise NotFound("Covenant not found")
        audit.record(conn, "covenant_status_changed", "covenant", covenant_id, acct["case_id"],
                     {"active": active, "reason": reason.strip()})


def _covenant(conn, account_id: str, covenant_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM covenants WHERE id=? AND account_id=?", (covenant_id, account_id)).fetchone()
    if not row:
        raise NotFound("Covenant not found")
    return dict(row)


def evaluate_value(cov: dict[str, Any], value: Decimal | None) -> tuple[str, str]:
    if value is None:
        return "UNKNOWN", "No measurable value — information missing or not calculable. Not treated as a pass."
    ok = OPS[cov["operator"]](value, Decimal(cov["threshold"]))
    return ("PASS" if ok else "FAIL"), (f"Observed {dstr(value)} vs requirement {cov['operator']} {cov['threshold']}: "
                                        + ("met." if ok else "NOT met (breach — requires human review)."))


def test_from_run(conn: sqlite3.Connection, account_id: str, covenant_id: str, period_end: date, run_id: str) -> str:
    acct = get_account(conn, account_id)
    cov = _covenant(conn, account_id, covenant_id)
    if cov["metric_key"] == "manual":
        raise ValidationFailure("This covenant is measured manually.")
    run = conn.execute("SELECT * FROM underwriting_runs WHERE id=? AND case_id=?", (run_id, acct["case_id"])).fetchone()
    if not run:
        raise NotFound("Run not found")
    if run["mode"] != "verified_only" or run["scenario_name"]:
        raise ValidationFailure("Covenant tests must use a verified-only base underwriting run (not a preview or scenario).")
    m = conn.execute("SELECT * FROM run_metrics WHERE run_id=? AND metric_key=?", (run_id, cov["metric_key"])).fetchone()
    value = Decimal(m["value"]) if m and m["status"] == "computed" and m["value"] is not None else None
    result, expl = evaluate_value(cov, value)
    if value is None and m is not None:
        expl += f" Metric status: {m['status']}. {m['explanation'][:200]}"
    expl += f" Source: run #{run['run_number']} (base period {run['base_period_label'] or 'n/a'})."
    return _save_test(conn, acct, cov, period_end, value, "underwriting_run", run_id, None, result, expl)


def test_manual(conn: sqlite3.Connection, account_id: str, covenant_id: str, period_end: date, value: str | None,
                evidence_note: str) -> str:
    acct = get_account(conn, account_id)
    cov = _covenant(conn, account_id, covenant_id)
    if len((evidence_note or "").strip()) < 10:
        raise ValidationFailure("Describe the evidence for a manual covenant measurement (at least 10 characters).")
    v = _dec(value) if value not in (None, "") else None
    result, expl = evaluate_value(cov, v)
    return _save_test(conn, acct, cov, period_end, v, "manual", None, evidence_note.strip(), result, expl)


def _save_test(conn, acct, cov, period_end: date, value, source, run_id, evidence, result, expl) -> str:
    tid = new_id()
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO covenant_tests(id, covenant_id, test_period_end, observed_value, source, run_id, evidence_note,
                   result, explanation, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (tid, cov["id"], period_end.isoformat(), dstr(value), source, run_id, evidence, result, expl, utcnow()))
        audit.record(conn, "covenant_tested", "covenant_test", tid, acct["case_id"],
                     {"code": cov["code"], "period_end": period_end.isoformat(), "observed": dstr(value),
                      "result": result, "source": source})
    return tid


def test_periods(cov: dict[str, Any], as_of: date) -> list[date]:
    out, d, k = [], date.fromisoformat(cov["first_test_date"]), 0
    while d <= as_of and k < 400:
        out.append(d)
        k += 1
        d = add_months(date.fromisoformat(cov["first_test_date"]), FREQ_MONTHS[cov["frequency"]] * k)
    return out


def covenant_status(conn: sqlite3.Connection, account_id: str, as_of: date | None = None) -> list[dict[str, Any]]:
    as_of = as_of or date.today()
    out = []
    for c in conn.execute("SELECT * FROM covenants WHERE account_id=? ORDER BY code", (account_id,)):
        cov = dict(c)
        tests = [dict(t) for t in conn.execute("SELECT * FROM covenant_tests WHERE covenant_id=? "
                                               "ORDER BY test_period_end, created_at", (cov["id"],))]
        latest_by_period: dict[str, dict] = {}
        for t in tests:
            latest_by_period[t["test_period_end"]] = t
        due_missing, due_soon = [], []
        if cov["active"]:
            for pe in test_periods(cov, as_of + timedelta(days=60)):
                deadline = pe + timedelta(days=cov["reporting_days"])
                if pe.isoformat() in latest_by_period:
                    continue
                if deadline < as_of:
                    due_missing.append({"period_end": pe.isoformat(), "deadline": deadline.isoformat()})
                elif deadline <= as_of + timedelta(days=60):
                    due_soon.append({"period_end": pe.isoformat(), "deadline": deadline.isoformat()})
        latest = tests[-1] if tests else None
        values = [Decimal(t["observed_value"]) for t in latest_by_period.values() if t["observed_value"] is not None]
        deterioration = None
        if len(values) >= 2 and values[-2] != 0:
            change = (values[-1] - values[-2]) / abs(values[-2])
            worse = change < 0 if cov["operator"] in (">=", ">") else change > 0
            if worse and abs(change) >= Decimal("0.15"):
                deterioration = f"{cov['metric_key']} moved {change * 100:+.1f}% versus the prior test (rule: ≥15% adverse)."
        cov.update(tests=tests, latest=latest, missing=due_missing, due_soon=due_soon, deterioration=deterioration,
                   metric_label=COVENANT_METRICS.get(cov["metric_key"], cov["metric_key"]))
        out.append(cov)
    return out


def alerts(conn: sqlite3.Connection, account_id: str, as_of: date | None = None) -> list[dict[str, Any]]:
    as_of = as_of or date.today()
    acct = get_account(conn, account_id)
    if acct["status"] != "ACTIVE":
        return []
    st = account_state(conn, account_id, as_of)
    out: list[dict[str, Any]] = []

    def add(key: str, rule: str, severity: str, message: str) -> None:
        out.append({"key": key, "rule": rule, "severity": severity, "message": message})

    if st.past_due_count:
        oldest = next(i["due_date"] for i in st.installments if i["past_due"])
        sev = "high" if st.days_past_due > 30 else ("medium" if st.days_past_due > int(acct["grace_days"]) else "low")
        add(f"past_due:{oldest}", "PAYMENT_PAST_DUE", sev,
            f"{st.past_due_count} installment(s) past due totalling ${st.past_due_amount:,.2f}; oldest due {oldest} "
            f"({st.days_past_due} days, {st.delinquency}). Based on recorded payments only.")
    if st.maturity_date:
        days = (date.fromisoformat(st.maturity_date) - as_of).days
        if 0 <= days <= 90:
            add(f"maturity:{st.maturity_date}", "MATURITY_WITHIN_90_DAYS", "medium", f"Loan matures {st.maturity_date}.")
    if st.balloon:
        days = (date.fromisoformat(st.balloon["due_date"]) - as_of).days
        if 0 <= days <= 180:
            add(f"balloon:{st.balloon['due_date']}", "BALLOON_WITHIN_180_DAYS", "high",
                f"Balloon payment ${Decimal(st.balloon['payment']):,.2f} due {st.balloon['due_date']} — refinancing plan needed.")
    for cov in covenant_status(conn, account_id, as_of):
        if not cov["active"]:
            continue
        if cov["latest"] and cov["latest"]["result"] == "FAIL":
            add(f"breach:{cov['code']}:{cov['latest']['test_period_end']}", "COVENANT_BREACH", "high",
                f"Covenant {cov['code']} failed for period ending {cov['latest']['test_period_end']}: "
                f"{cov['latest']['explanation']}")
        if cov["latest"] and cov["latest"]["result"] == "UNKNOWN":
            add(f"unknown:{cov['code']}:{cov['latest']['test_period_end']}", "COVENANT_UNKNOWN", "medium",
                f"Covenant {cov['code']} could not be measured for {cov['latest']['test_period_end']}.")
        for miss in cov["missing"]:
            add(f"missing:{cov['code']}:{miss['period_end']}", "COVENANT_TEST_MISSING", "medium",
                f"No {cov['code']} test recorded for period ending {miss['period_end']} (reporting deadline {miss['deadline']}).")
        if cov["deterioration"]:
            add(f"deteriorating:{cov['code']}:{cov['latest']['test_period_end']}", "METRIC_DETERIORATION", "medium",
                cov["deterioration"])
    for item in monitoring_items(conn, account_id):
        if item["status"] == "OPEN" and item["due_date"] < as_of.isoformat():
            add(f"item:{item['id']}", "MONITORING_ITEM_OVERDUE", "medium",
                f"{item['description']} was due {item['due_date']} and is not marked received.")
    return out


def refresh_events(conn: sqlite3.Connection, account_id: str, as_of: date | None = None) -> int:
    """Persist newly observed alerts as monitoring events (de-duplicated by key)."""
    acct = get_account(conn, account_id)
    new = 0
    with Database.transaction(conn):
        for a in alerts(conn, account_id, as_of):
            cur = conn.execute("INSERT OR IGNORE INTO monitoring_events(account_id, event_key, rule, severity, message, "
                               "first_seen_at) VALUES (?,?,?,?,?,?)",
                               (account_id, a["key"], a["rule"], a["severity"], a["message"], utcnow()))
            if cur.rowcount:
                new += 1
                audit.record(conn, "monitoring_event", "servicing_account", account_id, acct["case_id"],
                             {"rule": a["rule"], "key": a["key"], "severity": a["severity"]})
    return new


def acknowledge_event(conn: sqlite3.Connection, account_id: str, event_id: int, note: str) -> None:
    acct = get_account(conn, account_id)
    if len((note or "").strip()) < 5:
        raise ValidationFailure("Add a short note describing the follow-up.")
    with Database.transaction(conn):
        cur = conn.execute("UPDATE monitoring_events SET acknowledged_at=?, acknowledged_note=? WHERE id=? AND "
                           "account_id=? AND acknowledged_at IS NULL", (utcnow(), note.strip(), event_id, account_id))
        if cur.rowcount == 0:
            raise NotFound("Event not found or already acknowledged")
        audit.record(conn, "monitoring_event_acknowledged", "monitoring_event", str(event_id), acct["case_id"],
                     {"note": note.strip()})


def events(conn: sqlite3.Connection, account_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM monitoring_events WHERE account_id=? ORDER BY "
                                          "acknowledged_at IS NOT NULL, id DESC", (account_id,))]


def portfolio(conn: sqlite3.Connection, as_of: date | None = None) -> dict[str, Any]:
    as_of = as_of or date.today()
    rows = []
    totals = {"funded": Decimal(0), "outstanding": Decimal(0), "past_due": Decimal(0), "accounts": 0, "delinquent": 0}
    by_type: dict[str, Decimal] = {}
    upcoming: list[dict[str, Any]] = []
    for r in conn.execute("SELECT id FROM servicing_accounts ORDER BY loan_number"):
        acct = get_account(conn, r["id"])
        st = account_state(conn, r["id"], as_of)
        al = alerts(conn, r["id"], as_of)
        rows.append({"account": acct, "state": st, "alerts": al,
                     "covenants": covenant_status(conn, r["id"], as_of)})
        if acct["status"] == "ACTIVE":
            totals["accounts"] += 1
            totals["funded"] += st.funded_principal
            totals["outstanding"] += st.outstanding_principal
            totals["past_due"] += st.past_due_amount
            totals["delinquent"] += int(st.days_past_due > int(acct["grace_days"]))
            by_type[acct["loan_type"]] = by_type.get(acct["loan_type"], Decimal(0)) + st.outstanding_principal
            for i in st.installments:
                if i["status"] != "PAID" and as_of.isoformat() <= i["due_date"] <= (as_of + timedelta(days=30)).isoformat():
                    upcoming.append({"loan_number": acct["loan_number"], "account_id": acct["id"],
                                     "case_id": acct["case_id"], "due_date": i["due_date"], "amount": i["remaining"]})
    upcoming.sort(key=lambda x: x["due_date"])
    share = {k: (v / totals["outstanding"] if totals["outstanding"] else Decimal(0)) for k, v in by_type.items()}
    return {"as_of": as_of.isoformat(), "rows": rows, "totals": totals, "by_type": by_type, "share": share,
            "upcoming": upcoming}
