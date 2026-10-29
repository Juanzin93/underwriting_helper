from decimal import Decimal as D

import pytest

from tests.conftest import add_verified_financials, make_case
from uwhelper.analysis.resolver import (
    build_inputs,
    find_contradictions,
    missing_checklist,
)
from uwhelper.domain.models import CorrectionIn, FactIn, PartyIn
from uwhelper.repo import cases, facts
from uwhelper.repo.cases import ValidationFailure


def fact(conn, cid, key="revenue", value="1000", verified=False, months=12, end="2025-12-31", **kw):
    return facts.add_user_fact(conn, cid, FactIn(field_key=key, value=value, period_end=end, period_months=months,
                                                 mark_verified=verified, **kw))


def test_correction_keeps_history(conn):
    cid = make_case(conn)
    f1 = fact(conn, cid, value="1,000")
    f2 = facts.correct_fact(conn, cid, f1, CorrectionIn(new_value="1,100", reason="misread digit"))
    old, new = facts.get_fact(conn, cid, f1), facts.get_fact(conn, cid, f2)
    assert old["status"] == "superseded" and old["value_numeric"] == "1000"
    assert new["value_numeric"] == "1100" and new["status"] == "verified" and new["source_type"] == "user_corrected"
    hist = facts.fact_history(conn, cid, f2)
    assert [v["id"] for v in hist["versions"]] == [f1, f2]
    assert hist["corrections"][0]["reason"] == "misread digit"
    with pytest.raises(ValidationFailure):
        facts.correct_fact(conn, cid, f1, CorrectionIn(new_value="5", reason="again"))


def test_reject_requires_reason_and_restore(conn):
    cid = make_case(conn)
    f1 = fact(conn, cid)
    with pytest.raises(ValidationFailure):
        facts.reject_fact(conn, cid, f1, "")
    facts.reject_fact(conn, cid, f1, "wrong column")
    facts.restore_fact(conn, cid, f1)
    events = [e["event_type"] for e in facts.fact_history(conn, cid, f1)["versions"][0]["events"]]
    assert events == ["created", "rejected", "restored"]


def test_invalid_fact_inputs(conn):
    cid = make_case(conn)
    with pytest.raises(ValidationFailure):
        fact(conn, cid, key="no_such_field")
    with pytest.raises(ValidationFailure):
        fact(conn, cid, value="twelve")
    with pytest.raises(ValidationFailure):
        facts.add_user_fact(conn, cid, FactIn(field_key="revenue", value="1"))  # no period
    with pytest.raises(ValidationFailure):
        fact(conn, cid, key="ebitda_adjustment")  # needs item label
    with pytest.raises(ValidationFailure):
        fact(conn, cid, key="guarantor_personal_income")  # needs a guarantor


def test_balance_sheet_fact_is_point_in_time(conn):
    cid = make_case(conn)
    fid = fact(conn, cid, key="cash", months=12)
    f = facts.list_facts(conn, cid)[0]
    assert f["id"] == fid and f["period_months"] == 0


def test_contradiction_detected_both_values_kept_none_chosen(conn):
    cid = make_case(conn)
    add_verified_financials(conn, cid)
    other = fact(conn, cid, value="2350000")  # unverified, disagrees with verified 2,400,000
    items = find_contradictions(conn, cid)
    assert len(items) == 1 and set(items[0]["values"]) == {"2400000", "2350000"}
    inputs, _ = build_inputs(conn, cid, "verified_only")
    assert inputs.values["revenue"].state == "conflict" and inputs.values["revenue"].value is None
    # Resolution needs a reason and keeps the rejected fact.
    verified_id = next(f["id"] for f in items[0]["facts"] if f["id"] != other)
    with pytest.raises(ValidationFailure):
        facts.resolve_contradiction(conn, cid, verified_id, [other], "")
    facts.resolve_contradiction(conn, cid, verified_id, [other], "P&L reconciled to bank statements")
    assert find_contradictions(conn, cid) == []
    assert facts.get_fact(conn, cid, other)["status"] == "rejected"


def test_rounding_tolerance(conn):
    cid = make_case(conn)
    fact(conn, cid, value="1000000.40", verified=True)
    fact(conn, cid, value="1000000")
    assert find_contradictions(conn, cid) == []
    fact(conn, cid, value="1000005")
    assert len(find_contradictions(conn, cid)) == 1


def test_verified_only_excludes_candidates(conn):
    cid = make_case(conn)
    fact(conn, cid, value="5000")
    v, _ = build_inputs(conn, cid, "verified_only")
    assert v.values["revenue"].state == "missing" and "unverified_excluded" in v.values["revenue"].flags
    p, _ = build_inputs(conn, cid, "include_unverified")
    assert p.values["revenue"].value == D(5000) and not p.values["revenue"].verified


def test_annualization_of_interim_period(conn):
    cid = make_case(conn)
    fact(conn, cid, value="600000", months=6, end="2026-06-30", verified=True)
    period = facts.list_periods(conn, cid)[0]
    cases.set_base_period(conn, cid, period["id"])
    inputs, findings = build_inputs(conn, cid)
    assert inputs.values["revenue"].value == D(1200000) and "annualized" in inputs.values["revenue"].flags
    assert any(f["finding_type"] == "assumption" for f in findings)


def test_balance_sheet_period_mismatch_flagged(conn):
    cid = make_case(conn)
    fact(conn, cid, verified=True)
    fact(conn, cid, key="cash", value="10", end="2025-06-30", verified=True)
    inputs, findings = build_inputs(conn, cid)
    assert "period_mismatch" in inputs.values["cash"].flags
    assert any("No balance sheet dated" in f["message"] for f in findings)


def test_guarantor_facts(conn):
    cid = make_case(conn)
    gid = cases.add_party(conn, cid, PartyIn(name="G", is_guarantor=True))
    fact(conn, cid, verified=True)
    facts.add_user_fact(conn, cid, FactIn(field_key="guarantor_personal_income", value="90000", period_end="2025-12-31",
                                          period_months=12, entity_ref=gid, mark_verified=True))
    inputs, _ = build_inputs(conn, cid)
    assert inputs.guarantors[0].personal_income.value == D(90000)
    assert inputs.guarantors[0].living_expenses.state == "missing"


def test_missing_checklist_identifies_gaps(conn):
    cid = make_case(conn, loan_type="working_capital")
    items = {i["key"]: i for i in missing_checklist(conn, cid)}
    assert items["base_period"]["status"] == "missing"
    assert items["debt_schedule"]["status"] == "missing"
    assert items["collateral"]["status"] == "missing"
    assert "project_cost" not in items
