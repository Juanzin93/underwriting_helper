"""Milestone 6: lender conditions, due diligence, deal gates, closing record, funding confirmation, readiness.
Temporary databases only; no network; all parties fictional."""

import io
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.deal_helpers import fund_deal
from uwhelper.brokerage import fees as F
from uwhelper.brokerage import submissions as S
from uwhelper.crm import service as crm
from uwhelper.crm.models import DealIn, DealStatusIn, OrganizationIn
from uwhelper.db.migrate import (
    MIGRATIONS_DIR,
    apply_migrations,
    current_version,
    discover,
)
from uwhelper.lenders import importer as LI
from uwhelper.lenders import service as L
from uwhelper.origination import closing as CL
from uwhelper.origination import conditions as C
from uwhelper.repo.audit import list_events, verify_chain
from uwhelper.repo.cases import ValidationFailure

SAMPLE = Path(__file__).resolve().parents[2] / "samples" / "lender_catalog_example.yaml"
TODAY = date.today()
OVR = "Documented exception approved by the lender by email"


def _approved_deal(conn, settings, *, accept=True, amount="480000"):
    """A deal at APPROVED with an accepted offer (or at LENDER_SUBMISSION without one)."""
    if not L.list_programs(conn):
        LI.commit_yaml(conn, SAMPLE.read_text(encoding="utf-8"))
    program = next(p for p in L.list_programs(conn) if p["name"] == "Equipment term loans")
    n = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
    org = crm.create_organization(conn, OrganizationIn(name=f"Closing Borrower {n} (FICTIONAL)",
                                                       org_type="borrower_business", state="TX"))
    did = crm.create_deal(conn, DealIn(name="CNC line", deal_type="small_business", product_type="equipment",
                                       requested_amount=amount, borrower_organization_id=org))
    doc = crm.attach_document(conn, settings, did, io.BytesIO(f"Appraisal report {n} (FICTIONAL)".encode()),
                              "appraisal.txt", "appraisal")["document_id"]
    for s in ("UNDERWRITING", "STRUCTURING"):
        crm.transition_deal(conn, did, DealStatusIn(to_status=s, reason=f"Moving to {s} now"))
    sid = S.create_submission(conn, did, S.SubmissionIn(program_id=program["id"]))
    S.transition(conn, did, sid, S.SubmissionStatusIn(to_status="submitted", submitted_on=TODAY, submitted_via="email"))
    oid = S.add_offer(conn, did, sid, S.OfferIn(received_on=TODAY, amount=amount, annual_rate="7.25", term_months=84,
                                                amortization_months=84))
    if accept:
        S.accept_offer(conn, did, oid, S.DecisionIn(decided_by="Pat Broker", confirm=True))
    return did, oid, doc


def _to(conn, did, status, **kw):
    crm.transition_deal(conn, did, DealStatusIn(to_status=status, reason=f"Moving to {status} now", **kw))


def _fund_in(**kw):
    return CL.FundingConfirmIn(**({"funded_amount": "480000", "funding_date": TODAY, "confirmed_by": "Ops Person",
                                   "confirmation_text": CL.FUNDING_PHRASE} | kw))


# ------------------------------------------------------------------------------ 6.1 conditions
def test_condition_lifecycle(conn, settings):
    did, oid, doc = _approved_deal(conn, settings)
    cid = C.add_condition(conn, did, C.ConditionIn(description="Evidence of hazard insurance", offer_id=oid))
    n = C.add_conditions_from_text(conn, did, "- Signed guaranty\n\n• UCC search clear\n", offer_id=oid)
    assert n == 2 and len(C.list_conditions(conn, did)) == 3
    with pytest.raises(ValidationFailure, match="cannot go from open to satisfied"):
        C.update_condition(conn, did, cid, C.ConditionUpdateIn(to_status="satisfied", by="Pat"))
    C.update_condition(conn, did, cid, C.ConditionUpdateIn(to_status="received", document_id=doc))
    with pytest.raises(ValidationFailure, match="name"):
        C.update_condition(conn, did, cid, C.ConditionUpdateIn(to_status="satisfied"))
    with pytest.raises(ValidationFailure, match="why"):
        C.update_condition(conn, did, cid, C.ConditionUpdateIn(to_status="open"))
    C.update_condition(conn, did, cid, C.ConditionUpdateIn(to_status="satisfied", by="Pat Broker"))
    c = C.get_condition(conn, did, cid)
    assert c["status"] == "satisfied" and c["resolved_by"] == "Pat Broker" and c["document_id"] == doc
    other = next(x for x in C.list_conditions(conn, did) if x["description"] == "Signed guaranty")
    with pytest.raises(ValidationFailure, match="reason"):
        C.update_condition(conn, did, other["id"], C.ConditionUpdateIn(to_status="waived", by="Pat", note="ok"))
    C.update_condition(conn, did, other["id"], C.ConditionUpdateIn(to_status="waived", by="Pat Broker",
                                                                    note="Lender waived by email on the 3rd"))
    with pytest.raises(ValidationFailure, match="cannot go from"):
        C.update_condition(conn, did, cid, C.ConditionUpdateIn(to_status="open", note="reopen please"))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE deal_condition_events SET note='x'")
    ucc = next(x for x in C.list_conditions(conn, did) if x["description"] == "UCC search clear")
    with pytest.raises(ValidationFailure, match="not attached"):
        C.update_condition(conn, did, ucc["id"], C.ConditionUpdateIn(to_status="received", document_id="nope"))
    assert [e["to_status"] for e in C.list_conditions(conn, did)[0]["events"]][:1] == ["open"]


# ------------------------------------------------------------------------------ 6.2 due diligence
def test_due_diligence_tracker_and_overdue(conn, settings):
    did, _, doc = _approved_deal(conn, settings)
    iid = C.add_dd_item(conn, did, C.DDItemIn(item_type="appraisal", vendor_name="Valuers Inc (FICTIONAL)",
                                              due_on=TODAY - timedelta(days=2)))
    C.add_dd_item(conn, did, C.DDItemIn(item_type="flood", required=False))
    over = C.overdue_items(conn)
    assert [i["id"] for i in over["dd_items"]] == [iid]
    C.update_dd_item(conn, did, iid, C.DDUpdateIn(to_status="ordered", on=TODAY - timedelta(days=5),
                                                  due_on=TODAY + timedelta(days=10)))
    assert C.overdue_items(conn)["dd_items"] == []
    with pytest.raises(ValidationFailure, match="future"):
        C.update_dd_item(conn, did, iid, C.DDUpdateIn(to_status="received", on=TODAY + timedelta(days=1)))
    C.update_dd_item(conn, did, iid, C.DDUpdateIn(to_status="received", document_id=doc))
    with pytest.raises(ValidationFailure, match="re-ordered"):
        C.update_dd_item(conn, did, iid, C.DDUpdateIn(to_status="ordered"))
    with pytest.raises(ValidationFailure, match="name"):
        C.update_dd_item(conn, did, iid, C.DDUpdateIn(to_status="reviewed"))
    C.update_dd_item(conn, did, iid, C.DDUpdateIn(to_status="reviewed", by="Pat Broker"))
    item = C.get_dd_item(conn, did, iid)
    assert item["status"] == "reviewed" and item["document_id"] == doc and item["ordered_on"]
    with pytest.raises(ValidationFailure, match="cannot go from"):
        C.update_dd_item(conn, did, iid, C.DDUpdateIn(to_status="waived", by="Pat", note="x" * 12))
    events = [e["action"] for e in list_events(conn, limit=50)]
    assert events.count("dd_item_updated") == 3


# ------------------------------------------------------------------------------ 6.4 gates and readiness
def test_closing_gate_soft_blockers_need_override(conn, settings):
    did, oid, _ = _approved_deal(conn, settings)
    _to(conn, did, "DUE_DILIGENCE")  # accepted offer exists → no blocker
    cid = C.add_condition(conn, did, C.ConditionIn(description="Payoff letter", phase="due_diligence"))
    C.add_dd_item(conn, did, C.DDItemIn(item_type="title"))
    C.add_condition(conn, did, C.ConditionIn(description="Nice to have", required=False))
    rd = {r["to_status"]: r for r in CL.readiness(conn, did)}
    codes = {b["code"] for b in rd["CLOSING"]["blockers"]}
    assert codes == {"conditions", "due_diligence"} and not any(b["hard"] for b in rd["CLOSING"]["blockers"])
    with pytest.raises(ValidationFailure, match="Outstanding items"):
        _to(conn, did, "CLOSING")
    with pytest.raises(ValidationFailure, match="at least 20"):
        _to(conn, did, "CLOSING", override_reason="too short")
    _to(conn, did, "CLOSING", override_reason=OVR)
    h = crm.get_deal(conn, did)["history"][-1]
    assert h["to_status"] == "CLOSING" and "Override: " + OVR in h["reason"]
    ev = next(e for e in list_events(conn, limit=20) if e["action"] == "deal_status_changed")
    assert "due_diligence" in ev["details_json"] and "conditions" in ev["details_json"]
    assert cid


def test_due_diligence_gate_without_accepted_offer(conn, settings):
    did, _, _ = _approved_deal(conn, settings, accept=False)
    crm.transition_deal(conn, did, DealStatusIn(to_status="APPROVED", reason="Lender approved by phone",
                                                authorized_by="Pat Broker", confirm=True))
    with pytest.raises(ValidationFailure, match="accepted lender offer"):
        _to(conn, did, "DUE_DILIGENCE")
    _to(conn, did, "DUE_DILIGENCE", override_reason=OVR)


# ------------------------------------------------------------------------------ 6.3 closing and funding
def test_funding_confirmation_is_the_only_way_to_funded(conn, settings):
    did, oid, _ = _approved_deal(conn, settings)
    _to(conn, did, "DUE_DILIGENCE")
    _to(conn, did, "CLOSING")
    with pytest.raises(ValidationFailure, match="Confirm funding"):
        crm.transition_deal(conn, did, DealStatusIn(to_status="FUNDED", reason="Lender wired the money",
                                                    authorized_by="Pat", confirm=True))
    with pytest.raises(ValidationFailure, match="Closing date"):
        CL.confirm_funding(conn, did, _fund_in())
    CL.save_closing(conn, did, CL.ClosingIn(closing_date=TODAY, final_amount="480,000", final_annual_rate="7.25",
                                            final_term_months=84, final_amortization_months=84))
    cid = C.add_condition(conn, did, C.ConditionIn(description="Signed note"))
    with pytest.raises(ValidationFailure, match="still open"):
        CL.confirm_funding(conn, did, _fund_in(override_reason=OVR))  # hard: cannot be overridden
    C.update_condition(conn, did, cid, C.ConditionUpdateIn(to_status="received"))
    C.update_condition(conn, did, cid, C.ConditionUpdateIn(to_status="satisfied", by="Pat Broker"))
    with pytest.raises(ValidationFailure, match="Type exactly"):
        CL.confirm_funding(conn, did, _fund_in(confirmation_text="yes"))
    with pytest.raises(ValidationFailure, match="future"):
        CL.confirm_funding(conn, did, _fund_in(funding_date=TODAY + timedelta(days=1)))
    fid = CL.confirm_funding(conn, did, _fund_in())
    deal = crm.get_deal(conn, did)
    assert deal["status"] == "FUNDED" and deal["history"][-1]["authorized_by"] == "Ops Person"
    f = CL.get_funding(conn, did)
    assert f["id"] == fid and f["differences"] == [] and f["offer_id"] == oid
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE deal_fundings SET funded_amount='1' WHERE id=?", (fid,))
    with pytest.raises(sqlite3.IntegrityError, match="locked"):
        conn.execute("UPDATE deal_closings SET notes='x' WHERE deal_id=?", (did,))
    with pytest.raises(ValidationFailure, match="locked|approved"):
        CL.save_closing(conn, did, CL.ClosingIn(closing_date=TODAY))
    assert F.funded_amount(conn, did) == (Decimal(480000), "funding confirmation on the deal by Ops Person")
    assert verify_chain(conn)["ok"]


def test_differences_from_offer_are_flagged_and_need_override(conn, settings):
    did, oid, _ = _approved_deal(conn, settings)
    _to(conn, did, "DUE_DILIGENCE")
    _to(conn, did, "CLOSING")
    CL.save_closing(conn, did, CL.ClosingIn(closing_date=TODAY, final_annual_rate="7.5", final_term_months=84))
    with pytest.raises(ValidationFailure, match="annual rate 7.25% → 7.5%"):
        CL.confirm_funding(conn, did, _fund_in(funded_amount="475000"))
    CL.confirm_funding(conn, did, _fund_in(funded_amount="475000", override_reason=OVR))
    f = CL.get_funding(conn, did)
    assert {d["item"] for d in f["differences"]} == {"annual rate", "funded amount vs offer"}
    assert f["override_reason"] == OVR


def test_funding_only_from_closing_and_pipeline_guard(conn, settings):
    did, _, _ = _approved_deal(conn, settings)
    with pytest.raises(ValidationFailure, match="only in CLOSING"):
        CL.confirm_funding(conn, did, _fund_in())
    assert fund_deal  # shared helper used by earlier milestones' tests


# ------------------------------------------------------------------------------ migration
def test_upgrade_from_schema11(tmp_path, settings):
    from uwhelper.bootstrap import initialise
    from uwhelper.db.connection import Database

    old = tmp_path / "m11"
    old.mkdir()
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(f.name[:4]) <= 11:
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


def test_closing_pages_and_forms(web, settings):
    import re

    app, c, token = web
    with app.state.db.session() as conn:
        did, oid, doc = _approved_deal(conn, settings)
    page = c.get(f"/deals/{did}").text
    assert "Conditions, due diligence, closing" in page and 'value="FUNDED"' not in page
    c.post(f"/deals/{did}/closing/conditions-paste", data={"csrf_token": token, "text": "Signed note\nInsurance binder",
                                                           "offer_id": oid, "phase": "closing"})
    c.post(f"/deals/{did}/closing/dd", data={"csrf_token": token, "item_type": "appraisal", "vendor_name": "Valuers",
                                             "due_on": (TODAY - timedelta(days=1)).isoformat(), "required": "on"})
    assert "Overdue due diligence" in c.get("/tasks").text
    cp = c.get(f"/deals/{did}/closing").text
    assert "Signed note" in cp and "OVERDUE" in cp and "What blocks the next step" in cp
    c.post(f"/deals/{did}/status", data={"csrf_token": token, "to_status": "DUE_DILIGENCE",
                                         "reason": "Commitment signed by borrower"})
    r = c.post(f"/deals/{did}/status", data={"csrf_token": token, "to_status": "CLOSING",
                                             "reason": "Ready to close the loan"})
    assert r.status_code == 400 and "Outstanding items" in r.text
    c.post(f"/deals/{did}/status", data={"csrf_token": token, "to_status": "CLOSING", "reason": "Ready to close now",
                                         "override_reason": OVR})
    for cid in re.findall(r"/conditions/([0-9a-f]+)/update", c.get(f"/deals/{did}/closing").text):
        c.post(f"/deals/{did}/conditions/{cid}/update", data={"csrf_token": token, "to_status": "received"})
        c.post(f"/deals/{did}/conditions/{cid}/update", data={"csrf_token": token, "to_status": "satisfied",
                                                              "by": "Pat Broker"})
    c.post(f"/deals/{did}/closing/record", data={"csrf_token": token, "closing_date": TODAY.isoformat(),
                                                 "final_amount": "480000", "final_annual_rate": "7.25",
                                                 "final_term_months": "84", "final_amortization_months": "84"})
    r = c.post(f"/deals/{did}/closing/fund", data={"csrf_token": token, "funded_amount": "480,000",
                                                   "funding_date": TODAY.isoformat(), "confirmed_by": "Ops Person",
                                                   "confirmation_text": CL.FUNDING_PHRASE.lower(),
                                                   "override_reason": OVR}, follow_redirects=False)
    assert r.status_code == 303 and "FUNDED" in r.headers["location"]
    assert "confirmed by <b>Ops Person</b>" in c.get(f"/deals/{did}/closing").text


def test_closing_api(web, settings):
    app, c, token = web
    h = {"X-CSRF-Token": token}
    with app.state.db.session() as conn:
        did, oid, doc = _approved_deal(conn, settings)
    cond = c.post(f"/api/v1/deals/{did}/conditions", headers=h, json={"description": "Signed note", "offer_id": oid})
    assert cond.status_code == 201
    dd = c.post(f"/api/v1/deals/{did}/diligence", headers=h, json={"item_type": "title"})
    assert dd.status_code == 201
    r = c.post(f"/api/v1/deals/{did}/diligence/{dd.json()['id']}", headers=h, json={"to_status": "ordered"})
    assert r.json()["status"] == "ordered"
    ready = c.get(f"/api/v1/deals/{did}/readiness").json()["items"]
    assert ready[0]["to_status"] == "DUE_DILIGENCE" and ready[0]["blockers"] == []
    c.post(f"/api/v1/deals/{did}/status", headers=h, json={"to_status": "DUE_DILIGENCE", "reason": "Commitment signed"})
    c.post(f"/api/v1/deals/{did}/status", headers=h, json={"to_status": "CLOSING", "reason": "Ready to close",
                                                           "override_reason": OVR})
    for to in ("received", "satisfied"):
        c.post(f"/api/v1/deals/{did}/conditions/{cond.json()['id']}", headers=h, json={"to_status": to, "by": "Pat Broker"})
    assert c.put(f"/api/v1/deals/{did}/closing/record", headers=h,
                 json={"closing_date": TODAY.isoformat()}).status_code == 200
    r = c.post(f"/api/v1/deals/{did}/funding", headers=h,
               json={"funded_amount": "480000", "funding_date": TODAY.isoformat(), "confirmed_by": "Ops Person",
                     "confirmation_text": CL.FUNDING_PHRASE, "override_reason": OVR})
    assert r.status_code == 201, r.text
    body = c.get(f"/api/v1/deals/{did}/closing").json()
    assert body["funding"]["confirmed_by"] == "Ops Person" and body["deal"]["status"] == "FUNDED"
