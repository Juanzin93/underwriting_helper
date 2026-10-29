"""Milestone 5: submission packages, submissions and offers, fee agreements and the commission ledger.
Temporary databases only; no network; all parties fictional."""

import io
import json
import sqlite3
import zipfile
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.lifecycle_helpers import advance
from uwhelper.brokerage import fees as F
from uwhelper.brokerage import packages as P
from uwhelper.brokerage import submissions as S
from uwhelper.crm import service as crm
from uwhelper.crm.models import ContactIn, DealIn, DealStatusIn, OrganizationIn
from uwhelper.db.migrate import (
    MIGRATIONS_DIR,
    apply_migrations,
    current_version,
    discover,
)
from uwhelper.lenders import importer as LI
from uwhelper.lenders import matching as M
from uwhelper.lenders import service as L
from uwhelper.metrics.structuring import StructureInputs, analyse
from uwhelper.repo.audit import list_events, verify_chain
from uwhelper.repo.cases import ValidationFailure
from uwhelper.reports import deal_memo

SAMPLE = Path(__file__).resolve().parents[2] / "samples" / "lender_catalog_example.yaml"
TODAY = date.today()


def _setup(conn, settings, with_file=True):
    LI.commit_yaml(conn, SAMPLE.read_text(encoding="utf-8"))
    program = next(p for p in L.list_programs(conn) if p["name"] == "Equipment term loans")
    org = crm.create_organization(conn, OrganizationIn(name="Broker Test Borrower (FICTIONAL)",
                                                       org_type="borrower_business", state="TX"))
    did = crm.create_deal(conn, DealIn(name="CNC line", deal_type="small_business", product_type="equipment",
                                       requested_amount="480000", borrower_organization_id=org))
    doc = crm.attach_document(conn, settings, did, io.BytesIO(b"Equipment quote $480,000 (FICTIONAL)"), "quote.txt",
                              "equipment_quote")
    case_id = None
    if with_file:
        case_id = advance(conn, settings, "CREDIT_REVIEW")["case_id"]
        crm.link_underwriting_file(conn, did, case_id)
    return {"deal": did, "program": program["id"], "doc": doc["document_id"], "case": case_id}


def _move(conn, did, *statuses, **kw):
    for s in statuses:
        crm.transition_deal(conn, did, DealStatusIn(to_status=s, reason=f"Moving to {s} now", **kw))


def _submitted(conn, ctx):
    sid = S.create_submission(conn, ctx["deal"], S.SubmissionIn(program_id=ctx["program"]))
    S.transition(conn, ctx["deal"], sid, S.SubmissionStatusIn(to_status="submitted", submitted_on=TODAY,
                                                              submitted_via="email"))
    return sid


def _offer(**kw):
    return S.OfferIn(**({"received_on": TODAY, "amount": "480000", "annual_rate": "7.25", "term_months": 84,
                         "amortization_months": 84, "origination_fee": "4800"} | kw))


# ------------------------------------------------------------------------------ 5.1 packages
def test_package_manifest_lists_exactly_what_is_shared(conn, settings):
    ctx = _setup(conn, settings)
    memo = deal_memo.generate(conn, ctx["deal"])
    mid = M.run_matching(conn, ctx["deal"])
    pid = P.build_package(conn, settings, ctx["deal"], ctx["program"], [ctx["doc"]], memo_id=memo["id"], match_run_id=mid)
    pkg = P.get_package(conn, ctx["deal"], pid)
    m = pkg["manifest"]
    sha = conn.execute("SELECT sha256 FROM documents WHERE id=?", (ctx["doc"],)).fetchone()[0]
    assert [d["sha256"] for d in m["documents"]] == [sha] and m["memo"]["version"] == 1
    assert m["match"]["fit"] == "meets_all" and m["criteria_version"] == 1
    assert m["figures"]["run_number"] and {x["key"] for x in m["figures"]["metrics"]} >= {"dscr"}
    assert m["disclosure"] == settings.broker_disclosure and pkg["intact"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE submission_packages SET manifest_json='{}' WHERE id=?", (pid,))
    with pytest.raises(ValidationFailure, match="attached to this deal"):
        P.build_package(conn, settings, ctx["deal"], ctx["program"], ["not-a-doc"])
    with pytest.raises(ValidationFailure, match="at least one"):
        P.build_package(conn, settings, ctx["deal"], ctx["program"], [])
    assert P.build_package(conn, settings, ctx["deal"], ctx["program"], [ctx["doc"]]) != pid
    assert P.get_package(conn, ctx["deal"], P.list_packages(conn, ctx["deal"])[0]["id"])["version"] == 2


def test_package_export_requires_confirmation_and_verifies_files(conn, settings):
    ctx = _setup(conn, settings, with_file=False)
    pid = P.build_package(conn, settings, ctx["deal"], ctx["program"], [ctx["doc"]])
    with pytest.raises(ValidationFailure, match="Confirm"):
        P.export_package(conn, settings, ctx["deal"], pid, confirmed_by="Pat Broker", confirm=False)
    with pytest.raises(ValidationFailure, match="name"):
        P.export_package(conn, settings, ctx["deal"], pid, confirmed_by="", confirm=True)
    r1 = P.export_package(conn, settings, ctx["deal"], pid, confirmed_by="Pat Broker", confirm=True)
    with zipfile.ZipFile(r1["path"]) as z:
        names = z.namelist()
        assert {"manifest.json", "README.txt", "DISCLOSURE.txt"} <= set(names)
        doc_name = next(n for n in names if n.startswith("documents/"))
        assert z.read(doc_name) == b"Equipment quote $480,000 (FICTIONAL)"
        assert "confirmed by Pat Broker" in z.read("README.txt").decode()
        assert json.loads(z.read("manifest.json"))["documents"][0]["document_id"] == ctx["doc"]
    r2 = P.export_package(conn, settings, ctx["deal"], pid, confirmed_by="Pat Broker", confirm=True)
    assert r2["zip_sha256"] == r1["zip_sha256"]  # deterministic archive
    stored = conn.execute("SELECT stored_name FROM documents WHERE id=?", (ctx["doc"],)).fetchone()[0]
    (settings.documents_dir / stored).write_bytes(b"tampered")
    with pytest.raises(ValidationFailure, match="no longer matches"):
        P.export_package(conn, settings, ctx["deal"], pid, confirmed_by="Pat Broker", confirm=True)
    assert len(P.get_package(conn, ctx["deal"], pid)["exports"]) == 2
    assert sum(e["action"] == "submission_package_exported" for e in list_events(conn, limit=50)) == 2


# ------------------------------------------------------------------------------ 5.2 submissions
def test_submission_lifecycle_and_deal_sync(conn, settings):
    ctx = _setup(conn, settings, with_file=False)
    _move(conn, ctx["deal"], "UNDERWRITING", "STRUCTURING")
    sid = S.create_submission(conn, ctx["deal"], S.SubmissionIn(program_id=ctx["program"]))
    with pytest.raises(ValidationFailure, match="already has a submission"):
        S.create_submission(conn, ctx["deal"], S.SubmissionIn(program_id=ctx["program"]))
    with pytest.raises(ValidationFailure, match="when and how"):
        S.transition(conn, ctx["deal"], sid, S.SubmissionStatusIn(to_status="submitted"))
    with pytest.raises(ValidationFailure, match="future"):
        S.transition(conn, ctx["deal"], sid, S.SubmissionStatusIn(to_status="submitted", submitted_via="email",
                                                                  submitted_on=TODAY + timedelta(days=1)))
    res = S.transition(conn, ctx["deal"], sid, S.SubmissionStatusIn(to_status="submitted", submitted_on=TODAY,
                                                                    submitted_via="portal"))
    assert res["deal_status"] == "LENDER_SUBMISSION"
    assert crm.get_deal(conn, ctx["deal"])["history"][-1]["reason"].startswith("Submitted to Riverstone")
    with pytest.raises(ValidationFailure, match="cannot go from"):
        S.transition(conn, ctx["deal"], sid, S.SubmissionStatusIn(to_status="accepted"))
    with pytest.raises(ValidationFailure, match="reason"):
        S.transition(conn, ctx["deal"], sid, S.SubmissionStatusIn(to_status="declined"))
    S.transition(conn, ctx["deal"], sid, S.SubmissionStatusIn(to_status="declined", reason="Outside appetite"))
    S.transition(conn, ctx["deal"], sid, S.SubmissionStatusIn(to_status="draft", reason="Resubmitting"))
    sub = S.get_submission(conn, ctx["deal"], sid)
    assert [h["to_status"] for h in sub["history"]] == ["draft", "submitted", "declined", "draft"]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE submission_status_history SET reason='x' WHERE submission_id=?", (sid,))


def test_submission_does_not_skip_deal_stages(conn, settings):
    ctx = _setup(conn, settings, with_file=False)
    _move(conn, ctx["deal"], "UNDERWRITING")
    assert _submitted(conn, ctx) and crm.get_deal(conn, ctx["deal"])["status"] == "UNDERWRITING"


# ------------------------------------------------------------------------------ 5.3 offers
def test_offers_compare_with_structuring_engine(conn, settings):
    ctx = _setup(conn, settings)
    sid = _submitted(conn, ctx)
    with pytest.raises(ValidationError, match="interest-only"):
        _offer(interest_only_months=84)
    o1 = S.add_offer(conn, ctx["deal"], sid, _offer())
    assert S.get_submission(conn, ctx["deal"], sid)["status"] == "term_sheet"
    S.add_offer(conn, ctx["deal"], sid, _offer(annual_rate="0.0695", amortization_months=120, term_months=60,
                                               origination_fee="9600", other_fees="1,250"))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE offers SET amount='1' WHERE id=?", (o1,))
    cmp = S.compare_offers(conn, ctx["deal"])
    assert cmp["basis"]["cash_flow_label"] == "CFADS" and len(cmp["rows"]) == 2
    run = conn.execute("SELECT id FROM underwriting_runs WHERE case_id=? ORDER BY run_number DESC LIMIT 1",
                       (ctx["case"],)).fetchone()[0]
    ms = {r[0]: Decimal(r[1]) for r in conn.execute(
        "SELECT metric_key, value FROM run_metrics WHERE run_id=? AND metric_key IN ('cfads','existing_debt_service')",
        (run,))}
    b = cmp["rows"][1]["analysis"]
    ref = analyse(StructureInputs(principal=Decimal(480000), annual_rate=Decimal("0.0695"), term_months=60,
                                  amortization_months=120, origination_fee=Decimal(9600), other_costs=Decimal(1250),
                                  cfads=ms["cfads"], existing_debt_service=ms["existing_debt_service"]))
    assert b["level_payment"] == ref["level_payment"] and b["balloon_payment"] == ref["balloon_payment"] > 0
    assert b["total_cost"] == ref["total_interest"] + Decimal(10850)
    assert b["dscr_year1"] == (ms["cfads"] / (ms["existing_debt_service"] + ref["year1_debt_service"])).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP)


def test_accepting_an_offer_needs_a_person_and_moves_the_deal(conn, settings):
    ctx = _setup(conn, settings, with_file=False)
    _move(conn, ctx["deal"], "UNDERWRITING", "STRUCTURING")
    sid = _submitted(conn, ctx)
    o1 = S.add_offer(conn, ctx["deal"], sid, _offer())
    o2 = S.add_offer(conn, ctx["deal"], sid, _offer(annual_rate="8"))
    old = S.add_offer(conn, ctx["deal"], sid, _offer(received_on=TODAY - timedelta(days=40),
                                                     expires_on=TODAY - timedelta(days=10)))
    with pytest.raises(ValidationError):
        S.DecisionIn(decided_by="", confirm=True)
    with pytest.raises(ValidationFailure, match="Confirm"):
        S.accept_offer(conn, ctx["deal"], o1, S.DecisionIn(decided_by="Pat Broker"))
    with pytest.raises(ValidationFailure, match="expired"):
        S.accept_offer(conn, ctx["deal"], old, S.DecisionIn(decided_by="Pat Broker", confirm=True))
    res = S.accept_offer(conn, ctx["deal"], o1, S.DecisionIn(decided_by="Pat Broker", confirm=True))
    assert res["deal_status"] == "APPROVED"
    h = crm.get_deal(conn, ctx["deal"])["history"][-1]
    assert h["to_status"] == "APPROVED" and h["authorized_by"] == "Pat Broker"
    assert S.get_submission(conn, ctx["deal"], sid)["status"] == "accepted"
    with pytest.raises(ValidationFailure, match="already accepted"):
        S.accept_offer(conn, ctx["deal"], o2, S.DecisionIn(decided_by="Pat Broker", confirm=True))
    with pytest.raises(ValidationFailure, match="reason"):
        S.decline_offer(conn, ctx["deal"], o2, S.DecisionIn(decided_by="Pat Broker"))
    S.decline_offer(conn, ctx["deal"], o2, S.DecisionIn(decided_by="Pat Broker", reason="Rate too high"))
    with pytest.raises(sqlite3.IntegrityError, match="decided offer"):
        conn.execute("UPDATE offers SET status='open' WHERE id=?", (o1,))
    assert verify_chain(conn)["ok"]


# ------------------------------------------------------------------------------ 5.4 fees
def test_fee_agreement_validation():
    ok = F.FeeAgreementIn(kind="broker_fee", basis="percent_of_funded", value="0.875%")
    assert ok.value == Decimal("0.00875")
    with pytest.raises(ValidationError, match="typo"):
        F.FeeAgreementIn(kind="broker_fee", basis="percent_of_funded", value="30%")
    with pytest.raises(ValidationError, match="referral partner"):
        F.FeeAgreementIn(kind="referral_split", basis="flat", value="500")
    with pytest.raises(ValidationError, match="splits only"):
        F.FeeAgreementIn(kind="broker_fee", basis="percent_of_broker_gross", value="10%")
    with pytest.raises(ValidationError, match="greater than zero"):
        F.FeeAgreementIn(kind="lender_compensation", basis="flat", value="0")


def test_fee_math_to_the_cent_and_funded_amount_precedence(conn, settings):
    ctx = _setup(conn, settings, with_file=False)
    did = ctx["deal"]
    ref = crm.create_contact(conn, ContactIn(first_name="Rita", last_name="Referrer", contact_type="referral_partner"))
    F.add_agreement(conn, did, F.FeeAgreementIn(kind="broker_fee", basis="percent_of_funded", value="0.875%"))
    F.add_agreement(conn, did, F.FeeAgreementIn(kind="lender_compensation", basis="flat", value="1,333.33"))
    F.add_agreement(conn, did, F.FeeAgreementIn(kind="referral_split", basis="percent_of_broker_gross",
                                                value="33.3333%", party_contact_id=ref))
    amount, basis = F.funded_amount(conn, did)
    assert amount == Decimal(480000) and "ESTIMATE" in basis
    calc = F.compute(F.list_agreements(conn, did), Decimal("480123.45"))
    assert [x["amount"] for x in calc["lines_in"]] == [Decimal("4201.08"), Decimal("1333.33")]
    assert calc["gross"] == Decimal("5534.41")
    assert calc["lines_out"][0]["amount"] == Decimal("1844.80")  # 5534.41 × 0.333333 = 1844.8018 → 1844.80
    assert calc["net"] == Decimal("3689.61")
    _move(conn, did, "UNDERWRITING", "STRUCTURING")
    sid = _submitted(conn, ctx)
    oid = S.add_offer(conn, did, sid, _offer(amount="450000"))
    S.accept_offer(conn, did, oid, S.DecisionIn(decided_by="Pat Broker", confirm=True))
    assert F.funded_amount(conn, did) == (Decimal(450000), "accepted offer from Riverstone Community Bank (FICTIONAL)")


# ------------------------------------------------------------------------------ 5.5 ledger
def _funded_deal(conn, settings, ctx=None):
    ctx = ctx or _setup(conn, settings, with_file=False)
    did = ctx["deal"]
    ref = crm.create_organization(conn, OrganizationIn(name="Referral Co (FICTIONAL)", org_type="referral_partner"))
    F.add_agreement(conn, did, F.FeeAgreementIn(kind="broker_fee", basis="percent_of_funded", value="1%"))
    F.add_agreement(conn, did, F.FeeAgreementIn(kind="lender_compensation", basis="flat", value="2500"))
    split = F.add_agreement(conn, did, F.FeeAgreementIn(kind="referral_split", basis="percent_of_broker_gross",
                                                        value="25%", party_organization_id=ref))
    _move(conn, did, "UNDERWRITING", "STRUCTURING")
    sid = _submitted(conn, ctx)
    S.accept_offer(conn, did, S.add_offer(conn, did, sid, _offer()), S.DecisionIn(decided_by="Pat", confirm=True))
    _move(conn, did, "DUE_DILIGENCE", "CLOSING")
    crm.transition_deal(conn, did, DealStatusIn(to_status="FUNDED", reason="Lender wired the proceeds",
                                                authorized_by="Pat Broker", confirm=True))
    return did, split


def test_ledger_expected_received_reversal_and_aging(conn, settings):
    ctx = _setup(conn, settings, with_file=False)
    with pytest.raises(ValidationFailure, match="once the deal is funded"):
        F.record_expected(conn, ctx["deal"], TODAY)
    did, split = _funded_deal(conn, settings, ctx)
    res = F.record_expected(conn, did, TODAY - timedelta(days=45))
    assert res["gross"] == Decimal("7300.00") and res["payouts"] == Decimal("1825.00")
    with pytest.raises(ValidationFailure, match="already recorded"):
        F.record_expected(conn, did, TODAY)
    lg = F.ledger(conn, did)
    assert lg["receivable"] == Decimal("7300.00") and lg["payable"] == Decimal("1825.00")
    assert lg["receivable_age_days"] == 45 and lg["bucket"] == "31-60"
    F.record_entry(conn, did, F.LedgerEntryIn(entry_type="received", amount="4,800", occurred_on=TODAY,
                                              counterparty="Riverstone", reference="WIRE-1"))
    wrong = F.record_entry(conn, did, F.LedgerEntryIn(entry_type="paid_out", amount="1852", occurred_on=TODAY,
                                                      fee_agreement_id=split))
    with pytest.raises(ValidationFailure, match="go against"):
        F.record_entry(conn, did, F.LedgerEntryIn(entry_type="received", amount="1", occurred_on=TODAY,
                                                  fee_agreement_id=split))
    with pytest.raises(ValidationFailure, match="future"):
        F.record_entry(conn, did, F.LedgerEntryIn(entry_type="received", amount="1", occurred_on=TODAY + timedelta(days=1)))
    F.reverse_entry(conn, did, wrong, "Typo in the amount")
    with pytest.raises(ValidationFailure, match="already reversed"):
        F.reverse_entry(conn, did, wrong, "Again please")
    F.record_entry(conn, did, F.LedgerEntryIn(entry_type="paid_out", amount="1825", occurred_on=TODAY,
                                              fee_agreement_id=split))
    lg = F.ledger(conn, did)
    assert (lg["received"], lg["receivable"], lg["paid_out"], lg["payable"]) == \
        (Decimal(4800), Decimal("2500.00"), Decimal(1825), Decimal("0.00"))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE commission_entries SET amount='1' WHERE deal_id=?", (did,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM commission_entries WHERE deal_id=?", (did,))
    with pytest.raises(ValidationFailure, match="reverse them first"):
        F.void_agreement(conn, did, split, "Partner agreement changed")
    rep = F.commissions_report(conn)
    assert rep["totals"]["receivable"] == Decimal("2500.00") and rep["buckets"]["31-60"] == Decimal("2500.00")


def test_recalculate_after_reversing_expected(conn, settings):
    did, split = _funded_deal(conn, settings)
    F.record_expected(conn, did, TODAY)
    for e in F.effective_entries(F.ledger(conn, did)["entries"]):
        F.reverse_entry(conn, did, e["id"], "Agreement corrected")
    F.void_agreement(conn, did, split, "Split renegotiated")
    F.add_agreement(conn, did, F.FeeAgreementIn(kind="referral_split", basis="flat", value="1000",
                                                party_organization_id=conn.execute(
                                                    "SELECT id FROM organizations WHERE org_type='referral_partner'").fetchone()[0]))
    res = F.record_expected(conn, did, TODAY)
    assert res["payouts"] == Decimal("1000.00") and F.ledger(conn, did)["payable"] == Decimal("1000.00")


# ------------------------------------------------------------------------------ migration
def test_upgrade_from_schema10(tmp_path, settings):
    from uwhelper.bootstrap import initialise
    from uwhelper.db.connection import Database

    old = tmp_path / "m10"
    old.mkdir()
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(f.name[:4]) <= 10:
            (old / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    settings.ensure_dirs()
    with Database(settings.db_path).session() as c:
        apply_migrations(c, old)
        LI.commit_yaml(c, SAMPLE.read_text(encoding="utf-8"))
    db, _ = initialise(settings)
    with db.session() as c:
        assert current_version(c) == max(m.version for m in discover())
        assert len(L.list_programs(c)) == 3 and verify_chain(c)["ok"]


# ------------------------------------------------------------------------------ web + API
@pytest.fixture
def web(settings):
    from fastapi.testclient import TestClient

    from uwhelper.web.app import create_app

    app = create_app(settings)
    client = TestClient(app)
    return app, client, client.get("/api/v1/session").json()["csrf_token"]


def test_brokerage_pages_and_forms(web, settings):
    import re

    app, c, token = web
    with app.state.db.session() as conn:
        ctx = _setup(conn, settings, with_file=False)
        _move(conn, ctx["deal"], "UNDERWRITING", "STRUCTURING")
    did = ctx["deal"]
    page = c.get(f"/deals/{did}").text
    assert "Lender submissions" in page and "Fees and commissions" in page
    r = c.post(f"/deals/{did}/packages/build", data={"csrf_token": token, "program_id": ctx["program"],
                                                     "document_ids": ctx["doc"], "include_figures": "on"},
               follow_redirects=False)
    purl = r.headers["location"].split("?")[0]
    assert "quote.txt" in c.get(purl).text
    r = c.post(f"{purl}/export", data={"csrf_token": token, "confirmed_by": "Pat Broker"})
    assert r.status_code == 400 and "Confirm" in r.text
    c.post(f"{purl}/export", data={"csrf_token": token, "confirmed_by": "Pat Broker", "confirm": "on"})
    href = re.search(r'href="(/deals/[^"]+/exports/[^"]+)"', c.get(purl).text).group(1)
    z = c.get(href)
    assert z.status_code == 200 and z.content[:2] == b"PK"
    r = c.post(f"/deals/{did}/submissions/new", data={"csrf_token": token, "program_id": ctx["program"]},
               follow_redirects=False)
    surl = r.headers["location"].split("?")[0]
    c.post(f"{surl}/status", data={"csrf_token": token, "to_status": "submitted", "submitted_on": TODAY.isoformat(),
                                   "submitted_via": "email"})
    c.post(f"{surl}/offer", data={"csrf_token": token, "received_on": TODAY.isoformat(), "amount": "480,000",
                                  "annual_rate": "7.25", "term_months": "84", "amortization_months": "84",
                                  "origination_fee": "4800", "other_fees": "0", "interest_only_months": "0",
                                  "payment_frequency": "monthly", "rate_type": "fixed"})
    cmp_page = c.get(f"/deals/{did}/offers/compare").text
    assert "Interest + fees" in cmp_page and "Riverstone" in cmp_page
    oid = re.search(r"/offers/([0-9a-f]+)/accept", cmp_page).group(1)
    r = c.post(f"/deals/{did}/offers/{oid}/accept", data={"csrf_token": token, "decided_by": "Pat Broker",
                                                          "confirm": "on"}, follow_redirects=False)
    assert "APPROVED" in r.headers["location"]
    c.post(f"/deals/{did}/fees/agreement", data={"csrf_token": token, "kind": "broker_fee",
                                                 "basis": "percent_of_funded", "value": "1%"})
    assert "4,800.00" in c.get(f"/deals/{did}").text
    assert c.get("/commissions").status_code == 200


def test_brokerage_api(web, settings):
    app, c, token = web
    h = {"X-CSRF-Token": token}
    with app.state.db.session() as conn:
        ctx = _setup(conn, settings, with_file=False)
    did = ctx["deal"]
    r = c.post(f"/api/v1/deals/{did}/packages", headers=h, json={"program_id": ctx["program"], "document_ids": [ctx["doc"]]})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert c.post(f"/api/v1/deals/{did}/packages/{pid}/exports", headers=h,
                  json={"confirmed_by": "Pat", "confirm": False}).status_code == 422
    assert c.post(f"/api/v1/deals/{did}/packages/{pid}/exports", headers=h,
                  json={"confirmed_by": "Pat Broker", "confirm": True}).status_code == 201
    sid = c.post(f"/api/v1/deals/{did}/submissions", headers=h, json={"program_id": ctx["program"]}).json()["id"]
    c.post(f"/api/v1/deals/{did}/submissions/{sid}/status", headers=h,
           json={"to_status": "submitted", "submitted_on": TODAY.isoformat(), "submitted_via": "portal"})
    o = c.post(f"/api/v1/deals/{did}/submissions/{sid}/offers", headers=h,
               json={"received_on": TODAY.isoformat(), "amount": "480000", "annual_rate": "7.25", "term_months": 84,
                     "amortization_months": 84})
    assert o.status_code == 201
    assert len(c.get(f"/api/v1/deals/{did}/offers").json()["rows"]) == 1
    r = c.post(f"/api/v1/deals/{did}/offers/{o.json()['id']}/accept", headers=h,
               json={"decided_by": "Pat Broker", "confirm": True})
    assert r.json()["offer"]["status"] == "accepted"
    assert c.post(f"/api/v1/deals/{did}/fees/agreements", headers=h,
                  json={"kind": "lender_compensation", "basis": "flat", "value": "2500"}).status_code == 201
    fees = c.get(f"/api/v1/deals/{did}/fees").json()
    assert fees["calculation"]["gross"] == "2500.00" and fees["funded_basis"].startswith("accepted offer")
    assert c.post(f"/api/v1/deals/{did}/fees/expected", headers=h).status_code == 422  # not funded yet
    assert c.get("/api/v1/commissions").status_code == 200
    assert "/api/v1/deals/{deal_id}/submissions" in c.get("/api/v1/openapi.json").json()["paths"]
