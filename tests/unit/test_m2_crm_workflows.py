"""Milestone 2: pipeline consistency, borrower entities, actor identity, tasks, document requests, activities,
search/import and the API surface. All tests use temporary databases and no network."""

from datetime import date, timedelta

import pytest

from tests.lifecycle_helpers import APPROVED, advance, satisfy
from uwhelper.bootstrap import initialise
from uwhelper.core import identity
from uwhelper.crm import service as crm
from uwhelper.crm.models import (
    ContactIn,
    DealContactIn,
    DealIn,
    DealStatusIn,
    OrganizationIn,
)
from uwhelper.db.connection import Database
from uwhelper.db.migrate import (
    MIGRATIONS_DIR,
    MigrationError,
    apply_migrations,
    current_version,
    discover,
)
from uwhelper.domain.models import BorrowerIn, CaseIn
from uwhelper.repo import cases
from uwhelper.repo.audit import list_events, verify_chain
from uwhelper.repo.cases import ValidationFailure
from uwhelper.workflow import closing as CL
from uwhelper.workflow import decisions as D
from uwhelper.workflow import lifecycle as L
from uwhelper.workflow.lifecycle import TransitionRefused

LATEST = max(m.version for m in discover())


def _org(conn, name="Harbor Freight Partners LLC (FICTIONAL)", org_type="borrower_business"):
    return crm.create_organization(conn, OrganizationIn(name=name, org_type=org_type))


def _deal(conn, org_id=None, **kw):
    data = {"name": "Equipment line", "deal_type": "small_business", "product_type": "equipment",
            "requested_amount": "480000", "borrower_organization_id": org_id or _org(conn)} | kw
    return crm.create_deal(conn, DealIn(**data))


def _move(conn, did, *statuses, **extra):
    for s in statuses:
        crm.transition_deal(conn, did, DealStatusIn(to_status=s, reason=f"Moving the deal to {s}", **extra))


def _fund(conn, cid, days_ago=10):
    CL.save_closing(conn, cid, CL.ClosingIn(scheduled_closing_date=date.today().isoformat()))
    D.record_executed_terms(conn, cid, D.TermSetIn(**APPROVED))
    satisfy(conn, cid, "CLOSING")
    fd = date.today() - timedelta(days=days_ago)
    return CL.confirm_funding(conn, cid, CL.FundingIn(funded_amount="480000", funding_date=fd,
                                                      first_payment_date=fd + timedelta(days=30),
                                                      confirmed_by="Ops Officer", confirmation_text=CL.FUNDING_PHRASE))


# ------------------------------------------------------------------------------ 2.1 one pipeline truth
def test_new_deal_statuses_and_transitions(conn):
    did = _deal(conn)
    _move(conn, did, "UNDERWRITING", "CREDIT_REVIEW", "STRUCTURING", "LENDER_SUBMISSION")
    crm.transition_deal(conn, did, DealStatusIn(to_status="APPROVED", reason="Lender approved the request",
                                                authorized_by="Pat Broker", confirm=True))
    _move(conn, did, "DUE_DILIGENCE", "CLOSING")
    crm.transition_deal(conn, did, DealStatusIn(to_status="FUNDED", reason="Lender wired the loan proceeds",
                                                authorized_by="Pat Broker", confirm=True))
    _move(conn, did, "SERVICING")
    d = crm.get_deal(conn, did)
    assert d["status"] == "SERVICING" and d["allowed_transitions"] == []
    with pytest.raises(ValidationFailure, match="closed for edits"):
        crm.update_deal(conn, did, crm.DealPatch(name="renamed"))


def test_case_funding_syncs_deal_with_authorization(conn, settings):
    out = advance(conn, settings, "CLOSING")
    cid = out["case_id"]
    did = _deal(conn)
    crm.link_underwriting_file(conn, did, cid)
    _fund(conn, cid)
    d = crm.get_deal(conn, did)
    assert d["status"] == "FUNDED"
    last = d["history"][-1]
    assert last["authorized_by"] == "Ops Officer" and "Synchronized from underwriting file" in last["reason"]
    from uwhelper.servicing.ledger import OpenAccountIn, open_account

    open_account(conn, cid, OpenAccountIn())
    assert crm.get_deal(conn, did)["status"] == "SERVICING"
    assert any(e["action"] == "deal_status_synced" for e in list_events(conn, limit=500))
    assert verify_chain(conn)["ok"]


def test_case_decline_syncs_deal_with_decider(conn, settings):
    out = advance(conn, settings, "CREDIT_REVIEW")
    cid = out["case_id"]
    did = _deal(conn)
    crm.link_underwriting_file(conn, did, cid)
    _move(conn, did, "UNDERWRITING")
    D.record_decision(conn, cid, D.DecisionIn(decision="DECLINE", decided_by="Credit Officer",
                                              rationale="Repayment capacity insufficient after review.",
                                              confirm_human=True))
    L.transition(conn, cid, "DECLINED", "Declined by credit officer")
    d = crm.get_deal(conn, did)
    assert d["status"] == "DECLINED" and d["history"][-1]["authorized_by"] == "Credit Officer"


def test_deal_withdrawal_closes_prefunding_file(conn, settings):
    out = advance(conn, settings, "UNDERWRITING")
    did = _deal(conn)
    crm.link_underwriting_file(conn, did, out["case_id"])
    _move(conn, did, "WITHDRAWN")
    case = cases.get_case(conn, out["case_id"])
    assert case["stage"] == "WITHDRAWN"
    hist = L.history(conn, out["case_id"])
    assert "Synchronized from deal" in hist[-1]["reason"]
    # No echo back: exactly one WITHDRAWN row on the deal.
    assert [h["to_status"] for h in crm.get_deal(conn, did)["history"]].count("WITHDRAWN") == 1


def test_lender_decline_does_not_touch_file_but_shows_notice(conn, settings):
    out = advance(conn, settings, "UNDERWRITING")
    did = _deal(conn)
    crm.link_underwriting_file(conn, did, out["case_id"])
    crm.transition_deal(conn, did, DealStatusIn(to_status="DECLINED", reason="All lenders declined the request",
                                                authorized_by="Pat Broker", confirm=True))
    assert cases.get_case(conn, out["case_id"])["stage"] == "UNDERWRITING"
    assert any("still open" in n for n in crm.get_deal(conn, did)["alignment_notes"])


def test_guards_prevent_contradictory_terminal_states(conn, settings):
    # A file cannot be funded while its deal is declined.
    out = advance(conn, settings, "CLOSING")
    cid = out["case_id"]
    did = _deal(conn)
    crm.link_underwriting_file(conn, did, cid)
    crm.transition_deal(conn, did, DealStatusIn(to_status="DECLINED", reason="Borrower rejected the offer terms",
                                                authorized_by="Pat Broker", confirm=True))
    with pytest.raises(TransitionRefused, match="Reopen the deal"):
        _fund(conn, cid)
    # A file cannot be withdrawn/declined once its deal is funded by an outside lender.
    out2 = advance(conn, settings, "UNDERWRITING")
    did2 = _deal(conn, org_id=_org(conn, "Second Borrower Inc (FICTIONAL)"))
    crm.link_underwriting_file(conn, did2, out2["case_id"])
    _move(conn, did2, "UNDERWRITING", "STRUCTURING", "LENDER_SUBMISSION")
    for s in ("APPROVED",):
        crm.transition_deal(conn, did2, DealStatusIn(to_status=s, reason="Lender approved the request",
                                                     authorized_by="Pat Broker", confirm=True))
    _move(conn, did2, "DUE_DILIGENCE", "CLOSING")
    crm.transition_deal(conn, did2, DealStatusIn(to_status="FUNDED", reason="Outside lender funded the loan",
                                                 authorized_by="Pat Broker", confirm=True))
    with pytest.raises(TransitionRefused, match="funded deal"):
        L.transition(conn, out2["case_id"], "WITHDRAWN", "Borrower no longer needs the file")
    # A deal cannot be funded while its file is withdrawn.
    out3 = advance(conn, settings, "UNDERWRITING")
    did3 = _deal(conn, org_id=_org(conn, "Third Borrower Inc (FICTIONAL)"))
    crm.link_underwriting_file(conn, did3, out3["case_id"])
    L.transition(conn, out3["case_id"], "WITHDRAWN", "Borrower withdrew from internal review")
    assert crm.get_deal(conn, did3)["status"] == "WITHDRAWN"  # synced
    _move(conn, did3, "INTAKE", "UNDERWRITING", "STRUCTURING", "LENDER_SUBMISSION")
    crm.transition_deal(conn, did3, DealStatusIn(to_status="APPROVED", reason="Lender approved the request",
                                                 authorized_by="Pat Broker", confirm=True))
    _move(conn, did3, "DUE_DILIGENCE", "CLOSING")
    with pytest.raises(ValidationFailure, match="Reopen it"):
        crm.transition_deal(conn, did3, DealStatusIn(to_status="FUNDED", reason="Lender funded the loan",
                                                     authorized_by="Pat Broker", confirm=True))


def test_link_existing_file_rules(conn, settings):
    cid = cases.create_case(conn, CaseIn(title="Existing file", loan_type="equipment"),
                            BorrowerIn(legal_name="Linked Co (FICTIONAL)"))
    d1, d2 = _deal(conn), _deal(conn, org_id=_org(conn, "Other Co (FICTIONAL)"))
    assert any(f["id"] == cid for f in crm.unlinked_underwriting_files(conn))
    crm.link_underwriting_file(conn, d1, cid)
    with pytest.raises(ValidationFailure, match="already linked"):
        crm.link_underwriting_file(conn, d2, cid)
    with pytest.raises(ValidationFailure, match="already has"):
        crm.link_underwriting_file(conn, d1, cid)


# ------------------------------------------------------------------------------ 2.2 borrower entities
def test_case_borrower_links_to_organization(conn):
    org = _org(conn, "Riverside Bakery LLC (FICTIONAL)")
    c1 = cases.create_case(conn, CaseIn(title="A", loan_type="equipment"),
                           BorrowerIn(legal_name="riverside bakery llc (fictional)"))
    c2 = cases.create_case(conn, CaseIn(title="B", loan_type="equipment"), BorrowerIn(legal_name="New Borrower Co"))
    assert cases.get_case(conn, c1)["borrower"]["organization_id"] == org  # reused, case-insensitive
    new_org = cases.get_case(conn, c2)["borrower"]["organization_id"]
    assert crm.get_organization(conn, new_org)["org_type"] == "borrower_business"
    # Renaming the borrower re-links; the old organization is kept.
    cases.update_case(conn, c2, CaseIn(title="B", loan_type="equipment"), BorrowerIn(legal_name="Renamed Co"))
    assert cases.get_case(conn, c2)["borrower"]["organization_id"] != new_org
    crm.get_organization(conn, new_org)


def test_underwriting_file_from_deal_copies_guarantors_and_org(conn):
    org = _org(conn)
    gid = crm.create_contact(conn, ContactIn(first_name="Dana", last_name="Guarantor", contact_type="guarantor"))
    did = _deal(conn, org_id=org)
    crm.add_deal_contact(conn, did, DealContactIn(contact_id=gid, role="guarantor"))
    cid = crm.start_underwriting_file(conn, did)
    assert cases.get_case(conn, cid)["borrower"]["organization_id"] == org
    parties = cases.list_parties(conn, cid)
    assert parties[0]["contact_id"] == gid and parties[0]["is_guarantor"] == 1


def test_link_party_to_contact(conn):
    from uwhelper.domain.models import PartyIn

    cid = cases.create_case(conn, CaseIn(title="P", loan_type="equipment"), BorrowerIn(legal_name="Party Co"))
    pid = cases.add_party(conn, cid, PartyIn(name="Morgan Lee Owner", party_type="individual", is_guarantor=True,
                                              guarantee_type="unlimited"))
    contact_id = crm.link_party_contact(conn, cid, pid, create=True)
    c = crm.get_contact(conn, contact_id)
    assert (c["first_name"], c["last_name"], c["contact_type"]) == ("Morgan Lee", "Owner", "guarantor")
    assert cases.list_parties(conn, cid)[0]["contact_id"] == contact_id


def test_purge_removes_auto_created_org_but_keeps_crm_org(conn, settings):
    kept = _org(conn, "Kept Co (FICTIONAL)")
    c1 = cases.create_case(conn, CaseIn(title="1", loan_type="equipment"), BorrowerIn(legal_name="Kept Co (FICTIONAL)"))
    c2 = cases.create_case(conn, CaseIn(title="2", loan_type="equipment"), BorrowerIn(legal_name="Temp Co (FICTIONAL)"))
    temp = cases.get_case(conn, c2)["borrower"]["organization_id"]
    cases.purge_case(conn, c1, settings.documents_dir, settings.reports_dir)
    cases.purge_case(conn, c2, settings.documents_dir, settings.reports_dir)
    crm.get_organization(conn, kept)
    with pytest.raises(cases.NotFound):
        crm.get_organization(conn, temp)
    assert verify_chain(conn)["ok"]


# ------------------------------------------------------------------------------ migration 0008 upgrade
def _schema7_db(tmp_path, settings):
    old = tmp_path / "m7"
    old.mkdir()
    for m in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(m.name[:4]) <= 7:
            (old / m.name).write_text(m.read_text(encoding="utf-8"), encoding="utf-8")
    settings.ensure_dirs()
    db = Database(settings.db_path)
    now = "2026-01-01T00:00:00+00:00"
    with db.session() as c:
        assert apply_migrations(c, old)[-1] == 7
        c.execute("INSERT INTO organizations(id, name, org_type, created_at, updated_at) "
                  "VALUES ('org-beta', 'Beta Inc', 'borrower_business', ?, ?)", (now, now))
        c.execute("INSERT INTO contacts(id, first_name, last_name, created_at, updated_at) VALUES ('ct1','A','B',?,?)",
                  (now, now))
        for i, name in enumerate(["Acme LLC", " acme llc ", "BETA INC", "Gamma Co"]):
            c.execute("INSERT INTO loan_cases(id, case_number, title, loan_type, stage, stage_entered_at, created_at, "
                      "updated_at) VALUES (?, ?, 't', 'equipment', 'INTAKE', ?, ?, ?)", (f"case{i}", f"UW-{i}", now, now, now))
            c.execute("INSERT INTO borrowers(id, case_id, legal_name, created_at, updated_at) VALUES (?,?,?,?,?)",
                      (f"b{i}", f"case{i}", name, now, now))
        c.execute("""INSERT INTO deals(id, deal_number, name, deal_type, product_type, borrower_organization_id,
                        loan_case_id, status, status_changed_at, created_at, updated_at)
                     VALUES ('deal1','DL-2026-0001','D','small_business','equipment','org-beta','case2','UNDERWRITING',
                             ?,?,?)""", (now, now, now))
        c.execute("INSERT INTO deal_status_history(deal_id, from_status, to_status, reason, occurred_at) "
                  "VALUES ('deal1', NULL, 'INTAKE', 'Deal created', ?), ('deal1','INTAKE','UNDERWRITING','go now',?)",
                  (now, now))
        c.execute("INSERT INTO deal_contacts(deal_id, contact_id, role, created_at) VALUES ('deal1','ct1','borrower',?)",
                  (now,))
    return db


def test_upgrade_from_schema7_backfills_and_preserves(tmp_path, settings):
    db = _schema7_db(tmp_path, settings)
    with db.session() as c:
        before = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("loan_cases", "borrowers", "deals", "deal_status_history", "deal_contacts", "contacts")}
    db2, _ = initialise(settings)
    with db2.session() as c:
        assert current_version(c) == LATEST
        after = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
        assert after == before  # rebuild of deals did not cascade-delete history/contacts
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert c.execute("SELECT COUNT(*) FROM borrowers WHERE organization_id IS NULL").fetchone()[0] == 0
        names = sorted((r[0] for r in c.execute("SELECT name FROM organizations WHERE org_type='borrower_business'")),
                       key=str.lower)
        assert names == ["Acme LLC", "Beta Inc", "Gamma Co"]  # no duplicates; Beta reused
        assert c.execute("SELECT organization_id FROM borrowers WHERE id='b2'").fetchone()[0] == "org-beta"
        c.execute("UPDATE deals SET status='CREDIT_REVIEW' WHERE id='deal1'")  # new status accepted
        assert verify_chain(c)["ok"]


def test_foreign_keys_off_migration_rolls_back_on_violation(tmp_path, conn):
    d = tmp_path / "bad"
    d.mkdir()
    for m in sorted(MIGRATIONS_DIR.glob("*.sql")):
        (d / m.name).write_text(m.read_text(encoding="utf-8"), encoding="utf-8")
    (d / "9999_bad.sql").write_text("-- uwh:foreign_keys=off\nINSERT INTO deal_contacts(deal_id, contact_id, role, "
                                    "created_at) VALUES ('nope','nope','other','t');\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="foreign key violations"):
        apply_migrations(conn, d)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM deal_contacts").fetchone()[0] == 0
    assert current_version(conn) == LATEST


# ------------------------------------------------------------------------------ 2.3 actor identity
def test_actor_is_os_user_or_configured_name(conn, monkeypatch):
    monkeypatch.setattr(identity, "os_user", lambda: "jdoe")
    identity.set_actor(None)
    did = _deal(conn)
    ev = [e for e in list_events(conn, limit=50) if e["action"] == "deal_created"][0]
    assert ev["actor"] == "jdoe"
    identity.set_actor("Jane Broker; DROP")
    try:
        _move(conn, did, "UNDERWRITING")
        assert crm.get_deal(conn, did)["history"][-1]["actor"] == "Jane Broker DROP"
        ev = [e for e in list_events(conn, limit=50) if e["action"] == "deal_status_changed"][0]
        assert ev["actor"] == "Jane Broker DROP"
    finally:
        identity.set_actor(None)
    assert verify_chain(conn)["ok"]


def test_actor_setting_loaded(settings, monkeypatch):
    from uwhelper.config import load_settings

    monkeypatch.setenv("UWH_ACTOR_NAME", "Desk 2")
    s = load_settings(data_dir=settings.data_dir)
    try:
        initialise(s)
        assert identity.current_actor() == "Desk 2"
    finally:
        identity.set_actor(None)


# ------------------------------------------------------------------------------ 2.4 tasks
def test_tasks_crud_overdue_and_audit(conn):
    from uwhelper.crm import tasks as T
    from uwhelper.crm.models import TaskIn, TaskPatch

    did = _deal(conn)
    today = date(2026, 3, 1)
    t_over = T.create_task(conn, TaskIn(title="Call borrower", deal_id=did, due_date=date(2026, 2, 28)))
    t_today = T.create_task(conn, TaskIn(title="Order appraisal", deal_id=did, due_date=today, priority="high"))
    T.create_task(conn, TaskIn(title="Follow up next month", deal_id=did, due_date=date(2026, 4, 1)))
    T.create_task(conn, TaskIn(title="Someday", deal_id=did))
    assert [t["id"] for t in T.list_tasks(conn, view="overdue", today=today)] == [t_over]
    assert {t["id"] for t in T.list_tasks(conn, view="today", today=today)} == {t_over, t_today}
    # Boundary: the same task is not overdue on its due date, overdue the day after.
    assert t_today not in [t["id"] for t in T.list_tasks(conn, view="overdue", today=today)]
    assert t_today in [t["id"] for t in T.list_tasks(conn, view="overdue", today=today + timedelta(days=1))]
    assert len(T.list_tasks(conn, view="week", today=today)) == 2
    assert T.task_counts(conn, today) == {"open": 4, "overdue": 1, "due_today": 1}
    T.update_task(conn, t_over, TaskPatch(due_date=date(2026, 3, 5), description="Ask about the second location"))
    T.set_task_status(conn, t_over, "done")
    done = T.get_task(conn, t_over)
    assert done["status"] == "done" and done["completed_at"] and done["completed_by"]
    T.set_task_status(conn, t_over, "open")
    assert T.get_task(conn, t_over)["completed_at"] is None
    actions = [e["action"] for e in list_events(conn, limit=50)]
    assert {"task_created", "task_updated", "task_done", "task_reopened"} <= set(actions)
    upd = next(e for e in list_events(conn, limit=50) if e["action"] == "task_updated")
    assert "second location" not in str(upd["details_json"])  # descriptions are not copied into the audit log
    with pytest.raises(ValidationFailure):
        T.create_task(conn, TaskIn(title="Bad link", deal_id="missing"))


def test_tasks_survive_deal_archive(conn):
    from uwhelper.crm import tasks as T
    from uwhelper.crm.models import DealPatch, TaskIn

    did = _deal(conn)
    tid = T.create_task(conn, TaskIn(title="Keep me", deal_id=did))
    crm.update_deal(conn, did, DealPatch(archived=True))
    assert crm.list_deals(conn) == [] and len(crm.list_deals(conn, include_archived=True)) == 1
    assert T.get_task(conn, tid)["deal_id"] == did
    assert [t["id"] for t in T.list_tasks(conn, view="open")] == [tid]


# ------------------------------------------------------------------------------ 2.5 document requests
def test_checklist_is_versioned_and_valid():
    from uwhelper.crm import requests as R

    cl = R.load_checklist()
    assert cl.version and cl.key == "deal_documents"
    keys = [i.key for i in R.checklist_items("small_business", "equipment")]
    assert "equipment_quote" in keys and "business_tax_returns" in keys and len(keys) == len(set(keys))
    assert "rent_roll" in [i.key for i in R.checklist_items("cre", "cre_purchase")]


def test_document_request_lifecycle_and_carry_over(conn, settings):
    import io

    from uwhelper.crm import requests as R
    from uwhelper.crm.models import DocumentRequestIn, DocumentReviewIn

    did = _deal(conn)
    n = R.generate_requests(conn, did)
    assert n >= 5 and R.generate_requests(conn, did) == 0  # idempotent
    items = {r["item_key"]: r for r in R.list_requests(conn, did)}
    quote = items["equipment_quote"]
    assert quote["status"] == "requested" and quote["checklist_version"] == R.load_checklist().version
    with pytest.raises(ValidationFailure, match="received"):
        R.review_request(conn, did, quote["id"], DocumentReviewIn(decision="accepted", reviewed_by="Pat"))
    R.fulfil_with_upload(conn, settings, did, quote["id"], io.BytesIO(b"Quote: CNC lathe $480,000 (FICTIONAL)"),
                         "quote.txt")
    assert R.get_request(conn, did, quote["id"])["status"] == "received"
    with pytest.raises(ValidationFailure, match="why"):
        R.review_request(conn, did, quote["id"], DocumentReviewIn(decision="rejected", reviewed_by="Pat"))
    R.review_request(conn, did, quote["id"], DocumentReviewIn(decision="rejected", reviewed_by="Pat",
                                                              note="Quote is unsigned"))
    R.fulfil_with_upload(conn, settings, did, quote["id"], io.BytesIO(b"Signed quote $480,000 (FICTIONAL)"),
                         "quote-signed.txt")
    R.review_request(conn, did, quote["id"], DocumentReviewIn(decision="accepted", reviewed_by="Pat Broker"))
    acc = R.get_request(conn, did, quote["id"])
    assert acc["status"] == "accepted" and acc["reviewed_by"] == "Pat Broker"
    with pytest.raises(ValidationFailure, match="already accepted"):
        R.fulfil_with_upload(conn, settings, did, quote["id"], io.BytesIO(b"again"), "again.txt")
    # Waiver needs a reason.
    bank = items["bank_statements"]
    with pytest.raises(ValidationFailure, match="reason"):
        R.review_request(conn, did, bank["id"], DocumentReviewIn(decision="waived", reviewed_by="Pat", note="no"))
    R.review_request(conn, did, bank["id"], DocumentReviewIn(decision="waived", reviewed_by="Pat",
                                                             note="Not needed for an equipment loan this size"))
    rid = R.add_request(conn, did, DocumentRequestIn(label="Insurance certificate", doc_type="other"))
    assert R.get_request(conn, did, rid)["item_key"].startswith("manual_insurance_certificate")
    s = R.summary(conn, did)
    assert s["accepted"] == 1 and s["waived"] == 1 and s["required_outstanding"] == s["requested"]
    # Carry over: files received before and after the underwriting file exists end up on the file.
    cid = crm.start_underwriting_file(conn, did)
    tax = items["business_tax_returns"]
    R.fulfil_with_upload(conn, settings, did, tax["id"], io.BytesIO(b"Form 1120S 2025 (FICTIONAL)"), "1120s.txt")
    case_docs = {r[0] for r in conn.execute("SELECT document_id FROM case_documents WHERE case_id=?", (cid,))}
    deal_docs = {r[0] for r in conn.execute("SELECT document_id FROM deal_documents WHERE deal_id=?", (did,))}
    assert deal_docs and deal_docs <= case_docs
    assert verify_chain(conn)["ok"]


def test_request_link_existing_document(conn, settings):
    import io

    from uwhelper.crm import requests as R

    did = _deal(conn)
    R.generate_requests(conn, did)
    req = R.list_requests(conn, did)[0]
    res = crm.attach_document(conn, settings, did, io.BytesIO(b"Business return 2024 (FICTIONAL)"), "r.txt",
                              "business_tax_return")
    R.fulfil_with_existing(conn, did, req["id"], res["document_id"])
    assert R.get_request(conn, did, req["id"])["document_id"] == res["document_id"]
    with pytest.raises(ValidationFailure, match="not attached"):
        R.fulfil_with_existing(conn, did, req["id"], "nope")


# ------------------------------------------------------------------------------ 2.6 activities / timeline
def test_activities_append_only_with_corrections(conn, caplog):
    import sqlite3 as _sqlite3

    from uwhelper.crm import activities as A
    from uwhelper.crm.models import ActivityIn

    did = _deal(conn)
    secret = "Borrower disclosed a pending lawsuit worth 1.2M"
    aid = A.add_activity(conn, ActivityIn(kind="call", subject="Intro call", body=secret, deal_id=did))
    with pytest.raises(_sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE activities SET body='x' WHERE id=?", (aid,))
    with pytest.raises(_sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM activities WHERE id=?", (aid,))
    fix = A.add_activity(conn, ActivityIn(kind="call", subject="Intro call (corrected)", body="Lawsuit settled",
                                          deal_id=did, corrects_activity_id=aid))
    with pytest.raises(ValidationFailure, match="already corrected"):
        A.add_activity(conn, ActivityIn(subject="again", deal_id=did, corrects_activity_id=aid))
    rows = {r["id"]: r for r in A.list_activities(conn, deal_id=did)}
    assert rows[aid]["superseded"] and not rows[fix]["superseded"]
    assert [r["id"] for r in A.list_activities(conn, deal_id=did, include_superseded=False)] == [fix]
    # Note text never reaches the audit log or the log file.
    for e in list_events(conn, limit=50):
        assert "lawsuit" not in e["details_json"].lower()
    assert "lawsuit" not in caplog.text.lower()
    with pytest.raises(ValidationFailure, match="Link"):
        A.add_activity(conn, ActivityIn(subject="Orphan note"))


def test_deal_timeline_orders_all_sources(conn):
    from uwhelper.crm import activities as A
    from uwhelper.crm import tasks as T
    from uwhelper.crm.models import ActivityIn, TaskIn

    did = _deal(conn)
    A.add_activity(conn, ActivityIn(subject="Old meeting", kind="meeting", deal_id=did,
                                    occurred_at="2020-01-02T10:00:00+00:00"))
    _move(conn, did, "UNDERWRITING")
    T.create_task(conn, TaskIn(title="Collect returns", deal_id=did))
    tl = A.deal_timeline(conn, did)
    kinds = {i["kind"] for i in tl}
    assert {"meeting", "status", "task"} <= kinds
    assert tl[-1]["title"] == "Old meeting"  # oldest last
    assert [i["at"] for i in tl] == sorted([i["at"] for i in tl], reverse=True)


# ------------------------------------------------------------------------------ 2.7 search / import
CONTACTS_CSV = (
    "First Name,Last Name,Email,Phone,Company,Type,Favourite colour\n"
    "Ana,Silva,ana@example.com,555-010-2000,Silva Bakery LLC (FICTIONAL),borrower,blue\n"
    "Ben,Ortiz,,555-010-2001,,referral_partner,\n"
    "Ana,Silva,ANA@example.com,,,,\n"
    "Bad,Email,not-an-email,,,,\n"
)


def test_csv_import_preview_commit_idempotent(conn):
    from uwhelper.crm import importer

    p = importer.parse(conn, "contacts", CONTACTS_CSV)
    assert p.counts == {"new": 2, "existing": 0, "duplicate": 1, "invalid": 1}
    assert p.ignored_columns == ["favourite_colour"]
    assert "email" in p.rows[3].errors[0]
    with pytest.raises(ValidationFailure, match="invalid"):
        importer.commit(conn, "contacts", CONTACTS_CSV)
    assert crm.list_contacts(conn) == []  # nothing partially imported
    res = importer.commit(conn, "contacts", CONTACTS_CSV, skip_invalid=True, source_name="contacts.csv")
    assert res == {"created": 2, "organizations_created": 1, "skipped_existing": 0, "duplicates": 1,
                   "invalid_skipped": 1}
    ana = next(c for c in crm.list_contacts(conn) if c["first_name"] == "Ana")
    assert ana["organization_name"] == "Silva Bakery LLC (FICTIONAL)"
    again = importer.commit(conn, "contacts", CONTACTS_CSV, skip_invalid=True)
    assert again["created"] == 0 and again["skipped_existing"] == 2
    assert len(crm.list_contacts(conn)) == 2


def test_csv_import_organizations_and_limits(conn):
    from uwhelper.crm import importer

    csv_text = "name,org_type,city\nAcme (FICTIONAL),lender,Austin\nAcme (FICTIONAL),law_firm,Austin\n"
    assert importer.commit(conn, "organizations", csv_text)["created"] == 2  # same name, different type
    assert importer.commit(conn, "organizations", csv_text)["created"] == 0
    with pytest.raises(ValidationFailure, match="Missing required"):
        importer.parse(conn, "organizations", "city\nAustin\n")
    with pytest.raises(ValidationFailure, match="larger than"):
        importer.decode(b"x" * (importer.MAX_BYTES + 1))
    assert importer.decode("Café".encode("cp1252")) == "Café"
    assert importer.decode("﻿name".encode()) == "name"


def test_csv_import_1000_rows_is_fast(conn):
    import time

    from uwhelper.crm import importer

    rows = "\n".join(f"First{i},Last{i},person{i}@example.com,,Org {i % 50} (FICTIONAL),borrower" for i in range(1000))
    text = "first_name,last_name,email,phone,organization,contact_type\n" + rows + "\n"
    t0 = time.perf_counter()
    res = importer.commit(conn, "contacts", text)
    assert res["created"] == 1000 and res["organizations_created"] == 50
    assert time.perf_counter() - t0 < 5.0


def test_search_filters_and_paging(conn):
    for i in range(7):
        crm.create_contact(conn, ContactIn(first_name=f"P{i}", last_name="Page", contact_type="borrower" if i % 2 else
                                           "attorney"))
    assert len(crm.list_contacts(conn, limit=5)) == 5 and len(crm.list_contacts(conn, limit=5, offset=5)) == 2
    assert len(crm.list_contacts(conn, contact_type="attorney")) == 4
    assert len(crm.list_contacts(conn, q="p1")) == 1
    assert crm.list_contacts(conn, q="100%_") == []  # LIKE wildcards are escaped


# ------------------------------------------------------------------------------ web UI + API
@pytest.fixture
def web(settings):
    from fastapi.testclient import TestClient

    from uwhelper.web.app import create_app

    app = create_app(settings)
    client = TestClient(app)
    token = client.get("/api/v1/session").json()["csrf_token"]
    return app, client, token


def test_web_pages_and_forms(web):
    import re

    app, client, token = web
    with app.state.db.session() as c:
        oid = _org(c)
        did = _deal(c, org_id=oid)
        cid = crm.create_contact(c, ContactIn(first_name="Web", last_name="Contact"))
    r = client.post(f"/deals/{did}/generate-requests", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and "checklist" in r.headers["location"]
    page = client.get(f"/deals/{did}").text
    assert "Document checklist" in page and "Equipment invoice" in page and "Timeline" in page
    rid = re.search(r"/deals/[0-9a-f]+/requests/([0-9a-f]+)/upload", page).group(1)
    r = client.post(f"/deals/{did}/requests/{rid}/upload?csrf_token={token}",
                    files={"file": ("q.txt", b"quote text (FICTIONAL)", "text/plain")}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#documents")
    r = client.post(f"/deals/{did}/requests/{rid}/review",
                    data={"csrf_token": token, "decision": "accepted", "reviewed_by": "Pat Broker"})
    assert r.status_code == 200
    client.post("/tasks", data={"csrf_token": token, "title": "Web task", "deal_id": did, "due_date": "2020-01-01",
                                "next": f"/deals/{did}"})
    r = client.post("/tasks", data={"csrf_token": token, "title": "Evil redirect", "next": "//evil.example"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/tasks")
    assert "OVERDUE" in client.get("/tasks?view=overdue").text
    client.post("/activities", data={"csrf_token": token, "kind": "note", "subject": "Web note", "body": "hello",
                                     "contact_id": cid, "next": f"/contacts/{cid}"})
    assert "Web note" in client.get(f"/contacts/{cid}").text
    client.post(f"/deals/{did}/archive", data={"csrf_token": token})
    assert "Equipment line" not in client.get("/deals").text
    assert "Equipment line" in client.get("/deals?archived=1").text
    for url in ("/tasks", "/import", "/import?kind=organizations", f"/organizations/{oid}", "/contacts?contact_type=x",
                "/organizations?archived=1&offset=50", "/deals?offset=abc"):
        assert client.get(url).status_code == 200, url
    # CSV import through the browser: preview, then commit.
    r = client.post(f"/import/preview?csrf_token={token}", data={"kind": "contacts"},
                    files={"file": ("c.csv", CONTACTS_CSV.encode(), "text/csv")})
    assert r.status_code == 200 and "Import 2 new contacts" in r.text
    r = client.post("/import/commit", data={"csrf_token": token, "kind": "contacts", "csv_text": CONTACTS_CSV,
                                            "skip_invalid": "on", "filename": "c.csv"}, follow_redirects=False)
    assert r.status_code == 303 and "Imported+2" in r.headers["location"]


def test_case_page_links_party_to_contact(web):
    from uwhelper.domain.models import PartyIn

    app, client, token = web
    with app.state.db.session() as c:
        case_id = cases.create_case(c, CaseIn(title="UI", loan_type="equipment"), BorrowerIn(legal_name="UI Co"))
        pid = cases.add_party(c, case_id, PartyIn(name="Lee Owner", party_type="individual"))
    page = client.get(f"/cases/{case_id}").text
    assert "UI Co" in page and "Not linked" in page
    client.post(f"/cases/{case_id}/parties/{pid}/contact", data={"csrf_token": token, "contact_id": "__new__"})
    assert "/contacts/" in client.get(f"/cases/{case_id}").text


def test_api_endpoints_end_to_end(web):
    app, client, token = web
    h = {"X-CSRF-Token": token}
    oid = client.post("/api/v1/organizations", json={"name": "API Co (FICTIONAL)", "org_type": "borrower_business"},
                      headers=h).json()["id"]
    did = client.post("/api/v1/deals", json={"name": "API deal", "deal_type": "cre", "product_type": "cre_purchase",
                                             "requested_amount": "2500000", "borrower_organization_id": oid},
                      headers=h).json()["id"]
    r = client.post(f"/api/v1/deals/{did}/document-requests/generate", headers=h)
    assert r.status_code == 201 and r.json()["added"] >= 4
    reqs = client.get(f"/api/v1/deals/{did}/document-requests").json()
    rid = reqs["items"][0]["id"]
    assert reqs["summary"]["requested"] == reqs["summary"]["total"]
    r = client.post(f"/api/v1/deals/{did}/document-requests/{rid}/upload", headers=h,
                    files={"file": ("rr.txt", b"Rent roll (FICTIONAL)", "text/plain")})
    assert r.status_code == 201 and r.json()["request"]["status"] == "received"
    r = client.post(f"/api/v1/deals/{did}/document-requests/{rid}/review", headers=h,
                    json={"decision": "accepted", "reviewed_by": "API Reviewer"})
    assert r.json()["status"] == "accepted"
    r = client.post(f"/api/v1/deals/{did}/document-requests", headers=h, json={"label": "Survey", "doc_type": "other"})
    assert r.status_code == 201
    doc_id = client.get(f"/api/v1/deals/{did}/documents").json()[0]["id"]
    rid2 = r.json()["id"]
    assert client.post(f"/api/v1/deals/{did}/document-requests/{rid2}/link", headers=h,
                       json={"document_id": doc_id}).json()["status"] == "received"
    # Tasks
    t = client.post("/api/v1/tasks", headers=h, json={"title": "API task", "deal_id": did, "due_date": "2000-01-01"})
    assert t.status_code == 201
    tid = t.json()["id"]
    assert client.get("/api/v1/tasks?view=overdue").json()["counts"]["overdue"] == 1
    assert client.patch(f"/api/v1/tasks/{tid}", json={"priority": "high"},
                        headers=h).json()["changed"] == ["priority"]
    assert client.post(f"/api/v1/tasks/{tid}/status", headers=h, json={"status": "done"}).json()["status"] == "done"
    assert client.get(f"/api/v1/tasks/{tid}").json()["status"] == "done"
    assert client.get("/api/v1/tasks?view=bogus").status_code == 422
    # Activities and timeline
    a = client.post("/api/v1/activities", headers=h, json={"subject": "API note", "deal_id": did})
    assert a.status_code == 201
    assert client.get(f"/api/v1/activities?deal_id={did}").json()["items"][0]["subject"] == "API note"
    assert any(i["type"] == "activity" for i in client.get(f"/api/v1/deals/{did}/timeline").json()["items"])
    # Linking an existing underwriting file
    with app.state.db.session() as c:
        case_id = cases.create_case(c, CaseIn(title="Existing", loan_type="commercial_real_estate"),
                                    BorrowerIn(legal_name="API Co (FICTIONAL)"))
    assert any(f["id"] == case_id for f in client.get("/api/v1/underwriting-files/unlinked").json()["items"])
    r = client.post(f"/api/v1/deals/{did}/link-underwriting-file", headers=h, json={"case_id": case_id})
    assert r.json()["loan_case_id"] == case_id
    # CSV import (dry run, then commit)
    r = client.post("/api/v1/import/contacts", headers=h, files={"file": ("c.csv", CONTACTS_CSV.encode(), "text/csv")})
    assert r.json()["dry_run"] and r.json()["counts"]["invalid"] == 1
    r = client.post("/api/v1/import/contacts?dry_run=false", headers=h,
                    files={"file": ("c.csv", CONTACTS_CSV.encode(), "text/csv")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    r = client.post("/api/v1/import/contacts?dry_run=false&skip_invalid=true", headers=h,
                    files={"file": ("c.csv", CONTACTS_CSV.encode(), "text/csv")})
    assert r.status_code == 201 and r.json()["created"] == 2
    # Writes without the CSRF header are refused.
    assert client.post("/api/v1/tasks", json={"title": "No token"}).status_code == 403
    # Deal statuses in meta include the new ones.
    assert {"CREDIT_REVIEW", "SERVICING"} <= set(client.get("/api/v1/meta").json()["deal_statuses"])


def test_offline_openapi_schema(web):
    _, client, _ = web
    r = client.get("/api/v1/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert spec["info"]["title"] == "UW Helper local API"
    paths = spec["paths"]
    assert "/api/v1/tasks" in paths and "/api/v1/deals/{deal_id}/document-requests" in paths and "/health" in paths
    assert not any(p.startswith("/cases") for p in paths)  # HTML routes are not part of the API schema
    text = r.text
    assert "cdn" not in text.lower() and "http://" not in text and "https://" not in text
    assert client.get("/docs").status_code == 404  # no interactive UI (it would load CDN assets)
