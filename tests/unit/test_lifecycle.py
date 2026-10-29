import sqlite3

import pytest

from tests.conftest import make_case
from tests.lifecycle_helpers import APPROVED, advance, clean_sample
from uwhelper.analysis.underwriting import run_underwriting
from uwhelper.domain.models import CorrectionIn, TermsIn
from uwhelper.repo import cases, facts
from uwhelper.workflow import decisions as D
from uwhelper.workflow import lifecycle as L
from uwhelper.workflow.currency import run_status
from uwhelper.workflow.lifecycle import TransitionRefused


def test_new_case_starts_in_intake_with_history(conn):
    cid = make_case(conn)
    case = cases.get_case(conn, cid)
    assert case["stage"] == "INTAKE"
    h = L.history(conn, cid)
    assert [(x["from_stage"], x["to_stage"]) for x in h] == [(None, "INTAKE")]


def test_disallowed_and_system_only_transitions(conn):
    cid = make_case(conn)
    with pytest.raises(TransitionRefused, match="not an allowed"):
        L.transition(conn, cid, "CLOSING", "skipping ahead to closing")
    with pytest.raises(TransitionRefused, match="reason"):
        L.transition(conn, cid, "UNDERWRITING", "short")


def test_soft_blocker_needs_documented_override(conn):
    cid = make_case(conn)  # no documents -> soft blocker
    with pytest.raises(TransitionRefused, match="Outstanding items"):
        L.transition(conn, cid, "UNDERWRITING", "start underwriting now")
    with pytest.raises(TransitionRefused, match="documented reason"):
        L.transition(conn, cid, "UNDERWRITING", "start it now pls", override=True)
    L.transition(conn, cid, "UNDERWRITING", "Borrower will send documents this week; start spreading", override=True)
    last = L.history(conn, cid)[-1]
    assert last["is_override"] == 1 and last["blockers"][0]["code"] == "documents"


def test_hard_blocker_cannot_be_overridden(conn, settings):
    cid = clean_sample(conn, settings)
    L.transition(conn, cid, "UNDERWRITING", "Documents received for underwriting")
    with pytest.raises(TransitionRefused, match="No verified-only underwriting run"):
        L.transition(conn, cid, "CREDIT_REVIEW", "Please let me through without a run", override=True)


def test_material_change_requires_new_run(conn, settings):
    cid = clean_sample(conn, settings)
    L.transition(conn, cid, "UNDERWRITING", "Documents received for underwriting")
    run_underwriting(conn, cid)
    assert run_status(conn, cid)["current"]
    ni = next(f for f in facts.list_facts(conn, cid, statuses=("verified",)) if f["field_key"] == "net_income")
    facts.correct_fact(conn, cid, ni["id"], CorrectionIn(new_value="200000", reason="restated by CPA"))
    rs = run_status(conn, cid)
    assert not rs["current"] and "out of date" in rs["reason"]
    with pytest.raises(TransitionRefused, match="out of date"):
        L.transition(conn, cid, "CREDIT_REVIEW", "Screening complete and reviewed")
    run_underwriting(conn, cid)
    L.transition(conn, cid, "CREDIT_REVIEW", "Screening complete and reviewed")


def test_terms_change_invalidates_decision(conn, settings):
    out = advance(conn, settings, "STRUCTURING")
    cid = out["case_id"]
    cases.save_terms(conn, cid, TermsIn(requested_amount="600000", interest_rate="8.25", amortization_months=84,
                                        term_months=84, project_total_cost="600000"))
    blockers = L.evaluate(conn, cid, "DUE_DILIGENCE")
    assert any(b.code == "decision_stale" and b.hard for b in blockers)


def test_decision_requires_human_confirmation_and_current_run(conn, settings):
    out = advance(conn, settings, "CREDIT_REVIEW")
    cid = out["case_id"]
    with pytest.raises(Exception, match="responsible human"):
        D.DecisionIn(decision="APPROVE", decided_by="X Y", rationale="r" * 25, confirm_human=False)
    with pytest.raises(Exception, match="approved terms"):
        D.record_decision(conn, cid, D.DecisionIn(decision="APPROVE", decided_by="X Y", rationale="r" * 25,
                                                  confirm_human=True))


def test_decline_path_and_reopen(conn, settings):
    out = advance(conn, settings, "CREDIT_REVIEW")
    cid = out["case_id"]
    with pytest.raises(TransitionRefused, match="DECLINE decision"):
        L.transition(conn, cid, "DECLINED", "Cash flow insufficient for request")
    D.record_decision(conn, cid, D.DecisionIn(decision="DECLINE", decided_by="Credit Officer",
                                              rationale="Repayment capacity insufficient after review.",
                                              confirm_human=True))
    L.transition(conn, cid, "DECLINED", "Declined by credit officer")
    with pytest.raises(TransitionRefused):
        L.transition(conn, cid, "UNDERWRITING", "try again")
    L.transition(conn, cid, "INTAKE", "Borrower returned with a smaller request")
    assert cases.get_case(conn, cid)["stage"] == "INTAKE"
    # Old decisions are preserved, never rewritten.
    assert len(D.list_decisions(conn, cid)) == 1


def test_hold_and_resume_returns_to_prior_stage(conn, settings):
    out = advance(conn, settings, "UNDERWRITING")
    cid = out["case_id"]
    L.transition(conn, cid, "ON_HOLD", "Borrower travelling for a month")
    assert cases.get_case(conn, cid)["hold_return_stage"] == "UNDERWRITING"
    assert L.transition(conn, cid, "RESUME", "Borrower back; continuing") == "UNDERWRITING"


def test_full_lifecycle_to_servicing(conn, settings):
    out = advance(conn, settings, "SERVICING")
    stages = [h["to_stage"] for h in L.history(conn, out["case_id"])]
    assert stages == ["INTAKE", "UNDERWRITING", "CREDIT_REVIEW", "STRUCTURING", "DUE_DILIGENCE", "CLOSING", "FUNDED",
                      "SERVICING"]


def test_transitions_append_only(conn, settings):
    out = advance(conn, settings, "UNDERWRITING")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE workflow_transitions SET reason='x' WHERE case_id=?", (out["case_id"],))


def test_credit_decisions_immutable(conn, settings):
    advance(conn, settings, "STRUCTURING")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE credit_decisions SET decision='DECLINE'")


def test_approved_terms_layers_never_overwrite_requested(conn, settings):
    out = advance(conn, settings, "STRUCTURING")
    cid = out["case_id"]
    before = cases.get_case(conn, cid)["terms"]
    D.record_approved_terms(conn, cid, D.TermSetIn(**{**APPROVED, "principal": "450000"}))
    assert cases.get_case(conn, cid)["terms"] == before
    approved = D.term_sets(conn, cid, "approved")
    assert [t["revision"] for t in approved] == [2, 1] and approved[0]["principal"] == "450000"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE term_sets SET principal='1' WHERE id=?", (approved[0]["id"],))


def test_funded_case_cannot_be_purged(conn, settings):
    out = advance(conn, settings, "FUNDED")
    with pytest.raises(Exception, match="cannot be purged"):
        cases.purge_case(conn, out["case_id"], settings.documents_dir, settings.reports_dir)
