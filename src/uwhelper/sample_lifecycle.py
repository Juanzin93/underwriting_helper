"""Synthetic lifecycle demo: the fictional sample case driven to any stage (used by tests and the CLI)."""

from __future__ import annotations

from datetime import date, timedelta

from uwhelper.analysis.resolver import find_contradictions
from uwhelper.analysis.underwriting import run_underwriting
from uwhelper.repo import cases, facts
from uwhelper.reports.memo import generate_memo
from uwhelper.sample_data import load_sample_case
from uwhelper.workflow import closing as CL
from uwhelper.workflow import conditions as C
from uwhelper.workflow import decisions as D
from uwhelper.workflow import lifecycle as L

APPROVED = dict(principal="480000", annual_rate="8.25", term_months=84, amortization_months=84,
                entered_by="Test Credit Officer")
STAGES = ["UNDERWRITING", "CREDIT_REVIEW", "STRUCTURING", "DUE_DILIGENCE", "CLOSING", "FUNDED", "SERVICING"]


def clean_sample(conn, settings) -> str:
    """Sample case with its contradiction resolved and everything verified."""
    cid = load_sample_case(conn, settings)
    c = find_contradictions(conn, cid)[0]
    keep = next(f for f in c["facts"] if f["status"] == "verified")
    facts.resolve_contradiction(conn, cid, keep["id"], [f["id"] for f in c["facts"] if f["id"] != keep["id"]],
                                "P&L reconciled to deposits")
    for f in facts.list_facts(conn, cid, statuses=("candidate",)):
        facts.verify_fact(conn, cid, f["id"])
    for d in cases.list_debts(conn, cid):
        cases.set_debt_status(conn, cid, d["id"], "verified")
    return cid


def satisfy(conn, cid, phase):
    for cond in C.list_conditions(conn, cid, phase):
        if cond["status"] in ("SATISFIED", "WAIVED"):
            continue
        C.set_status(conn, cid, cond["id"], "RECEIVED")
        C.set_status(conn, cid, cond["id"], "UNDER_REVIEW")
        C.set_status(conn, cid, cond["id"], "SATISFIED", evidence_note="Original reviewed", verified=True)


def advance(conn, settings, to: str, funding_days_ago: int = 200) -> dict:
    cid = clean_sample(conn, settings)
    out = {"case_id": cid}
    target = STAGES.index(to)
    L.transition(conn, cid, "UNDERWRITING", "Documents received for underwriting")
    if target < 1:
        return out
    run = run_underwriting(conn, cid)
    out["run_id"] = run.run_id
    L.transition(conn, cid, "CREDIT_REVIEW", "Verified screening complete")
    if target < 2:
        return out
    out["memo"] = generate_memo(conn, settings, cid, run.run_id, pdf=False)
    D.record_decision(conn, cid, D.DecisionIn(decision="APPROVE", decided_by="Test Credit Officer",
                                              rationale="Coverage adequate for the request; human review done.",
                                              confirm_human=True), D.TermSetIn(**APPROVED))
    L.transition(conn, cid, "STRUCTURING", "Approved by credit officer")
    if target < 3:
        return out
    C.generate_from_policy(conn, cid)
    L.transition(conn, cid, "DUE_DILIGENCE", "Terms approved; collect conditions")
    if target < 4:
        return out
    satisfy(conn, cid, "DUE_DILIGENCE")
    L.transition(conn, cid, "CLOSING", "Due diligence complete")
    if target < 5:
        return out
    CL.save_closing(conn, cid, CL.ClosingIn(scheduled_closing_date=date.today().isoformat()))
    D.record_executed_terms(conn, cid, D.TermSetIn(**APPROVED))
    satisfy(conn, cid, "CLOSING")
    fd = date.today() - timedelta(days=funding_days_ago)
    out["funding_date"] = fd
    CL.confirm_funding(conn, cid, CL.FundingIn(funded_amount="480000", funding_date=fd,
                                               first_payment_date=fd + timedelta(days=30), confirmed_by="Test Ops",
                                               confirmation_text=CL.FUNDING_PHRASE))
    if target < 6:
        return out
    from uwhelper.servicing.ledger import OpenAccountIn, open_account

    out["account_id"] = open_account(conn, cid, OpenAccountIn())
    return out


def load_lifecycle_demo(conn, settings) -> dict[str, str]:
    """Create one fictional case per lifecycle stage, plus a serviced loan with payments and a covenant."""
    from uwhelper import structuring as S
    from uwhelper.servicing import ledger as LG
    from uwhelper.servicing import monitoring as MO

    created: dict[str, str] = {}
    for stage in ("UNDERWRITING", "CREDIT_REVIEW", "STRUCTURING", "DUE_DILIGENCE", "CLOSING"):
        created[stage] = advance(conn, settings, stage)["case_id"]
    S.baseline_from_requested(conn, created["CREDIT_REVIEW"])
    S.create_structure(conn, created["CREDIT_REVIEW"], S.StructureIn(
        name="IO 12 then 72-month amortization", principal="480000", annual_rate="8.25", term_months=84,
        amortization_months=72, interest_only_months=12))
    out = advance(conn, settings, "SERVICING", funding_days_ago=200)
    acct = out["account_id"]
    for i in LG.effective_installments(conn, acct)[:4]:
        LG.record_payment(conn, acct, LG.PaymentIn(received_date=i["due_date"], amount=i["payment"],
                                                   reference="DEMO", note="Synthetic demo payment"))
    MO.add_covenant(conn, acct, MO.CovenantIn(code="MIN-DSCR", description="Minimum DSCR 1.25x (illustrative)",
                                              metric_key="dscr", operator=">=", threshold="1.25",
                                              first_test_date=out["funding_date"]))
    created["SERVICING"] = out["case_id"]
    return created
