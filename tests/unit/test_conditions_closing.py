import sqlite3
from datetime import date, timedelta

import pytest

from tests.lifecycle_helpers import APPROVED, advance, satisfy
from uwhelper.repo.cases import ValidationFailure
from uwhelper.workflow import closing as CL
from uwhelper.workflow import conditions as C
from uwhelper.workflow import decisions as D
from uwhelper.workflow import lifecycle as L
from uwhelper.workflow.lifecycle import TransitionRefused


@pytest.fixture
def dd(conn, settings):
    return advance(conn, settings, "DUE_DILIGENCE")["case_id"]


def cond(conn, cid, code):
    return next(c for c in C.list_conditions(conn, cid) if c["code"] == code)


def test_policy_defines_applicable_conditions(conn, dd):
    codes = {c["code"] for c in C.list_conditions(conn, dd)}
    assert "DD-EQUIP-INVOICE" in codes and "DD-PURCHASE-AGMT" not in codes  # equipment loan, not acquisition
    assert "CL-SIGNATURES" in codes
    again = C.generate_from_policy(conn, dd)
    assert again["created"] == 0 and again["existing"] > 0  # idempotent
    assert cond(conn, dd, "DD-BANK-STMTS")["required"] == 0  # optional per policy


def test_status_machine_and_evidence(conn, dd):
    c = cond(conn, dd, "DD-FORMATION")
    with pytest.raises(ValidationFailure, match="not an allowed"):
        C.set_status(conn, dd, c["id"], "SATISFIED")
    C.set_status(conn, dd, c["id"], "REQUESTED")
    C.set_status(conn, dd, c["id"], "RECEIVED")
    C.set_status(conn, dd, c["id"], "UNDER_REVIEW")
    with pytest.raises(ValidationFailure, match="evidence"):
        C.set_status(conn, dd, c["id"], "SATISFIED", verified=True)
    with pytest.raises(ValidationFailure, match="verified"):
        C.set_status(conn, dd, c["id"], "SATISFIED", evidence_note="Certificate dated 2026-09-01")
    C.set_status(conn, dd, c["id"], "SATISFIED", evidence_note="Certificate dated 2026-09-01", verified=True)
    c = cond(conn, dd, "DD-FORMATION")
    assert c["verification_status"] == "verified" and c["completed_at"]
    with pytest.raises(ValidationFailure, match="reason"):
        C.set_status(conn, dd, c["id"], "UNDER_REVIEW")
    C.set_status(conn, dd, c["id"], "UNDER_REVIEW", note="Certificate expired; need new one")
    hist = [e["to_status"] for e in C.condition_history(conn, dd, c["id"])]
    assert hist == ["NOT_STARTED", "REQUESTED", "RECEIVED", "UNDER_REVIEW", "SATISFIED", "UNDER_REVIEW"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE condition_events SET note='x'")


def test_rejection_requires_note(conn, dd):
    c = cond(conn, dd, "DD-FIN-STMTS")
    C.set_status(conn, dd, c["id"], "RECEIVED")
    with pytest.raises(ValidationFailure):
        C.set_status(conn, dd, c["id"], "REJECTED")
    C.set_status(conn, dd, c["id"], "REJECTED", note="Interim statements missing")


def test_waiver_rules(conn, dd):
    c = cond(conn, dd, "DD-BENEFICIAL-OWNERS")  # non-waivable per policy
    with pytest.raises(ValidationFailure, match="non-waivable"):
        C.waive(conn, dd, c["id"], waived_by="Officer", reason="x" * 30, confirm=True)
    c = cond(conn, dd, "DD-LICENSES")
    with pytest.raises(ValidationFailure, match="Confirm"):
        C.waive(conn, dd, c["id"], waived_by="Officer", reason="x" * 30, confirm=False)
    with pytest.raises(ValidationFailure, match="20 characters"):
        C.waive(conn, dd, c["id"], waived_by="Officer", reason="too short", confirm=True)
    with pytest.raises(ValidationFailure, match="waiver form"):
        C.set_status(conn, dd, c["id"], "WAIVED")
    C.waive(conn, dd, c["id"], waived_by="Senior Officer", reason="No license required for this activity per counsel",
            confirm=True)
    c = cond(conn, dd, "DD-LICENSES")
    assert c["status"] == "WAIVED" and c["waived_by"] == "Senior Officer" and c["waiver_reason"]
    C.set_status(conn, dd, c["id"], "NOT_STARTED", note="Waiver revoked after new information")
    assert cond(conn, dd, "DD-LICENSES")["waiver_reason"] is None


def test_required_conditions_block_closing_optional_do_not(conn, dd):
    with pytest.raises(TransitionRefused, match="required due-diligence"):
        L.transition(conn, dd, "CLOSING", "Ready to close this loan", override=True)
    for c in C.list_conditions(conn, dd, "DUE_DILIGENCE"):
        if c["required"]:
            C.set_status(conn, dd, c["id"], "RECEIVED")
            C.set_status(conn, dd, c["id"], "UNDER_REVIEW")
            C.set_status(conn, dd, c["id"], "SATISFIED", evidence_note="ok", verified=True)
    assert any(not c["required"] and c["status"] == "NOT_STARTED" for c in C.list_conditions(conn, dd, "DUE_DILIGENCE"))
    L.transition(conn, dd, "CLOSING", "All required due diligence satisfied")


def test_manual_and_decision_conditions(conn, settings):
    out = advance(conn, settings, "CREDIT_REVIEW")
    cid = out["case_id"]
    D.record_decision(conn, cid, D.DecisionIn(decision="APPROVE_WITH_CONDITIONS", decided_by="Officer",
                                              rationale="Approve subject to guaranty and inspection.",
                                              conditions_text="- Personal guaranty of owner\n- Site inspection",
                                              confirm_human=True), D.TermSetIn(**APPROVED))
    codes = [c["code"] for c in C.list_conditions(conn, cid) if c["source"] == "credit_decision"]
    assert codes == ["DEC-01", "DEC-02"]
    C.add_manual(conn, cid, C.ConditionIn(code="X-ENV", description="Environmental questionnaire", category="other"))
    with pytest.raises(ValidationFailure):
        C.add_manual(conn, cid, C.ConditionIn(code="X-ENV", description="dup", category="other"))


def test_closing_blockers_and_funding_confirmation(conn, settings):
    out = advance(conn, settings, "CLOSING")
    cid = out["case_id"]
    codes = {b.code for b in L.funding_blockers(conn, cid)}
    assert {"closing_items", "executed_terms"} <= codes
    with pytest.raises(TransitionRefused, match="confirmation page"):
        L.transition(conn, cid, "FUNDED", "funded today by the bank")
    D.record_executed_terms(conn, cid, D.TermSetIn(**{**APPROVED, "principal": "500000"}))
    assert any(b.code == "executed_exceeds_approved" and b.hard for b in L.funding_blockers(conn, cid))
    D.record_executed_terms(conn, cid, D.TermSetIn(**{**APPROVED, "annual_rate": "8.5"}))
    satisfy(conn, cid, "CLOSING")
    blk = L.funding_blockers(conn, cid)
    assert {b.code for b in blk} == {"executed_differs", "closing_record"} and not any(b.hard for b in blk)
    fd = date.today() - timedelta(days=1)
    f = dict(funded_amount="480000", funding_date=fd, first_payment_date=fd + timedelta(days=30),
             confirmed_by="Ops Person", confirmation_text="yes")
    with pytest.raises(ValidationFailure, match="Type exactly"):
        CL.confirm_funding(conn, cid, CL.FundingIn(**f))
    f["confirmation_text"] = CL.FUNDING_PHRASE
    with pytest.raises(TransitionRefused, match="override"):
        CL.confirm_funding(conn, cid, CL.FundingIn(**f))
    with pytest.raises(ValidationFailure, match="future"):
        CL.confirm_funding(conn, cid, CL.FundingIn(**{**f, "funding_date": date.today() + timedelta(days=3),
                                                      "first_payment_date": date.today() + timedelta(days=40),
                                                      "override_reason": "x" * 30}))
    with pytest.raises(ValidationFailure, match="exceeds"):
        CL.confirm_funding(conn, cid, CL.FundingIn(**{**f, "funded_amount": "999999", "override_reason": "x" * 30}))
    CL.confirm_funding(conn, cid, CL.FundingIn(**{**f, "funded_amount": "470000",
                                                  "override_reason": "Rate changed per signed rate-lock addendum"}))
    from uwhelper.repo.cases import get_case

    assert get_case(conn, cid)["stage"] == "FUNDED"
    fund = CL.get_funding(conn, cid)
    assert fund["funded_amount"] == "470000"
    terms = {t["term_type"] for t in D.term_sets(conn, cid)}
    assert terms == {"approved", "executed"}  # requested terms remain in loan_terms; layers distinct
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE fundings SET funded_amount='1'")


def test_closing_record_audit_hides_instructions(conn, settings):
    from uwhelper.repo.audit import list_events

    out = advance(conn, settings, "CLOSING")
    CL.save_closing(conn, out["case_id"], CL.ClosingIn(disbursement_instructions="Wire to acct 123456789"))
    ev = [e for e in list_events(conn, out["case_id"]) if e["action"] == "closing_updated"][0]
    assert "123456789" not in ev["details_json"]
