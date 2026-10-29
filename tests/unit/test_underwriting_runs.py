import json
import sqlite3

import pytest
import yaml

from tests.conftest import add_verified_financials, make_case
from uwhelper.analysis import underwriting as uw
from uwhelper.analysis.underwriting import RunError, get_run, run_underwriting
from uwhelper.config import PROJECT_ROOT
from uwhelper.domain.models import FactIn
from uwhelper.policy.loader import parse_policy_text, register_policy
from uwhelper.repo import audit, cases, facts


def test_complete_case_has_no_exceptions_but_never_says_approved(conn, full_case):
    from uwhelper.domain.models import PartyIn

    gid = cases.add_party(conn, full_case, PartyIn(name="G", is_guarantor=True))
    for k, v in {"guarantor_personal_income": "90000", "guarantor_living_expenses": "50000",
                 "guarantor_personal_debt_service": "12000"}.items():
        facts.add_user_fact(conn, full_case, FactIn(field_key=k, value=v, period_end="2025-12-31", period_months=12,
                                                    entity_ref=gid, mark_verified=True))
    for i, t in enumerate(("business_tax_return", "balance_sheet", "equipment_quote")):
        conn.execute("INSERT INTO documents(id, sha256, original_filename, stored_name, file_ext, size_bytes, imported_at) "
                     "VALUES (?, ?, 'x.pdf', 'x', '.pdf', 1, 't')", (f"d{i}", str(i) * 64))
        conn.execute("INSERT INTO case_documents(case_id, document_id, doc_type, added_at) VALUES (?,?,?,?)",
                     (full_case, f"d{i}", t, "t"))
    from datetime import date

    res = run_underwriting(conn, full_case, as_of=date(2026, 10, 1))
    run = get_run(conn, res.run_id)
    assert {e["result"] for e in run["evaluations"]} <= {"PASS", "NOT_APPLICABLE"}, [(e["rule_id"], e["result"], e["explanation"][:150]) for e in run["evaluations"] if e["result"] not in ("PASS", "NOT_APPLICABLE")]
    assert res.overall_status == "NO_EXCEPTIONS_IDENTIFIED"
    assert "NOT A CREDIT APPROVAL" in run["summary"]["overall_status_text"]


def test_run_saved_with_metrics_rules_evidence(conn, full_case):
    res = run_underwriting(conn, full_case)
    run = get_run(conn, res.run_id)
    assert run["metrics_by_key"]["dscr"]["value"] == "2.683676"
    assert run["metrics_by_key"]["dscr"]["formula"]
    assert run["evaluations"] and run["policy_version"] == "1.1.0"
    assert conn.execute("SELECT COUNT(*) FROM evidence_refs WHERE run_id=?", (res.run_id,)).fetchone()[0] > 0


def test_runs_are_immutable_and_numbered(conn, full_case):
    r1, r2 = run_underwriting(conn, full_case), run_underwriting(conn, full_case)
    assert (r1.run_number, r2.run_number) == (1, 2)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE underwriting_runs SET overall_status='X' WHERE id=?", (r1.run_id,))


def test_reproducible_inputs_hash(conn, full_case):
    a, b = get_run(conn, run_underwriting(conn, full_case).run_id), get_run(conn, run_underwriting(conn, full_case).run_id)
    assert a["inputs_sha256"] == b["inputs_sha256"]
    assert [m["value"] for m in a["metrics"]] == [m["value"] for m in b["metrics"]]


def test_transaction_failure_saves_nothing(conn, full_case, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(uw.audit, "record", boom)
    with pytest.raises(RuntimeError):
        run_underwriting(conn, full_case)
    assert conn.execute("SELECT COUNT(*) FROM underwriting_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM run_metrics").fetchone()[0] == 0


def test_correction_changes_next_run_but_not_history(conn, full_case):
    r1 = run_underwriting(conn, full_case)
    ni = next(f for f in facts.list_facts(conn, full_case) if f["field_key"] == "net_income")
    from uwhelper.domain.models import CorrectionIn

    facts.correct_fact(conn, full_case, ni["id"], CorrectionIn(new_value="30000", reason="restated"))
    r2 = run_underwriting(conn, full_case)
    d1 = get_run(conn, r1.run_id)["metrics_by_key"]["dscr"]["value"]
    d2 = get_run(conn, r2.run_id)["metrics_by_key"]["dscr"]["value"]
    assert d1 == "2.683676" and d2 != d1


def test_policy_version_change_preserved_in_history(conn, full_case):
    r1 = run_underwriting(conn, full_case)
    text = (PROJECT_ROOT / "policies" / "demo_commercial_general_v1.0.0.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    data["version"] = "9.9.0"
    next(r for r in data["rules"] if r["id"] == "DSCR_MIN")["threshold"] = 3.0
    register_policy(conn, parse_policy_text(yaml.safe_dump(data)))
    r2 = run_underwriting(conn, full_case)
    a, b = get_run(conn, r1.run_id), get_run(conn, r2.run_id)
    assert (a["policy_version"], b["policy_version"]) == ("1.1.0", "9.9.0")
    ea = next(e for e in a["evaluations"] if e["rule_id"] == "DSCR_MIN")
    eb = next(e for e in b["evaluations"] if e["rule_id"] == "DSCR_MIN")
    assert ea["result"] == "PASS" and eb["result"] == "FAIL"


def test_unverified_preview_never_passes(conn):
    cid = make_case(conn)
    for k, v in {"revenue": "2400000", "net_income": "230000"}.items():
        facts.add_user_fact(conn, cid, FactIn(field_key=k, value=v, period_end="2025-12-31", period_months=12))
    run = get_run(conn, run_underwriting(conn, cid, mode="include_unverified").run_id)
    assert run["overall_status"] != "NO_EXCEPTIONS_IDENTIFIED"
    for e in run["evaluations"]:
        if e["subject"] and e["subject"].startswith("metric:") and e["result"] == "PASS":
            m = run["metrics_by_key"][e["subject"].split(":")[1]]
            assert "unverified" not in m["flags"]


def test_scenario_run_keeps_base(conn, full_case):
    base = run_underwriting(conn, full_case)
    sid = uw.create_scenario(conn, full_case, "Down", None, {"revenue_change_pct": "-0.2", "rate_change_bps": 300})
    s = run_underwriting(conn, full_case, scenario_id=sid)
    assert uw.latest_run_id(conn, full_case) == base.run_id
    sd = float(get_run(conn, s.run_id)["metrics_by_key"]["dscr"]["value"])
    assert sd < 2.683676


def test_missing_policy_or_unsupported_type(conn, full_case):
    conn.execute("UPDATE loan_cases SET policy_key='nope' WHERE id=?", (full_case,))
    with pytest.raises(RunError):
        run_underwriting(conn, full_case)


def test_audit_records_run(conn, full_case):
    run_underwriting(conn, full_case)
    assert any(e["action"] == "underwriting_run_saved" for e in audit.list_events(conn, full_case))
