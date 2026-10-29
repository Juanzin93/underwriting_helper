"""Milestone 7: brokered-loan key dates and reminders, portfolio views and CSV, watchlist, payoff quotes.
Temporary databases only; no network; all parties fictional."""

import sqlite3
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest

from tests.lifecycle_helpers import advance
from tests.unit.test_m6_closing import _approved_deal, _to
from uwhelper.brokerage import fees as F
from uwhelper.crm import service as crm
from uwhelper.crm.models import ContactIn
from uwhelper.crm.tasks import list_tasks
from uwhelper.db.migrate import (
    MIGRATIONS_DIR,
    apply_migrations,
    current_version,
    discover,
)
from uwhelper.metrics.structuring import StructureInputs, add_months, analyse
from uwhelper.origination import closing as CL
from uwhelper.portfolio import monitoring as MON
from uwhelper.portfolio import payoff as PO
from uwhelper.portfolio import reports as REP
from uwhelper.repo.cases import ValidationFailure
from uwhelper.servicing.ledger import PaymentIn, account_state, record_payment

TODAY = date.today()
OVR = "Documented exception approved by the lender by email"


def _funded(conn, settings, *, term=60, amort=120, io=0, rate_type="variable", reset=12, amount="480000",
            funding_date=None):
    """Fund a deal through the M6 path with chosen final terms."""
    from uwhelper.brokerage import submissions as S

    did, oid, _ = _approved_deal(conn, settings, accept=False, amount=amount)
    sid = conn.execute("SELECT id FROM submissions WHERE deal_id=?", (did,)).fetchone()[0]
    oid2 = S.add_offer(conn, did, sid, S.OfferIn(received_on=TODAY, amount=amount, annual_rate="7", rate_type=rate_type,
                                                 term_months=term, amortization_months=amort, interest_only_months=io))
    S.decline_offer(conn, did, oid, S.DecisionIn(decided_by="Pat Broker", reason="Superseded by revised offer"))
    S.accept_offer(conn, did, oid2, S.DecisionIn(decided_by="Pat Broker", confirm=True))
    _to(conn, did, "DUE_DILIGENCE")
    _to(conn, did, "CLOSING")
    CL.save_closing(conn, did, CL.ClosingIn(closing_date=TODAY, rate_reset_months=reset))
    CL.confirm_funding(conn, did, CL.FundingConfirmIn(funded_amount=amount, funding_date=funding_date or TODAY,
                                                      confirmed_by="Ops Person", confirmation_text=CL.FUNDING_PHRASE))
    return did


# ------------------------------------------------------------------------------ 7.1 key dates and reminders
def test_key_dates_hand_worked(conn, settings):
    did = _funded(conn, settings, term=60, amort=120, io=6, reset=12)
    kd = MON.key_dates(conn, did)
    by = {}
    for k in kd:
        by.setdefault(k["kind"], []).append(k)
    assert by["interest_only_end"][0]["date"] == add_months(TODAY, 6)
    assert [k["date"] for k in by["rate_reset"]] == [add_months(TODAY, m) for m in (12, 24, 36, 48)]
    assert by["maturity"][0]["date"] == add_months(TODAY, 60) == by["balloon"][0]["date"]
    ref = analyse(StructureInputs(principal=Decimal(480000), annual_rate=Decimal("0.07"), term_months=60,
                                  amortization_months=120, interest_only_months=6))
    assert by["balloon"][0]["amount"] == ref["balloon_payment"] > 0
    fixed = _funded(conn, settings, term=84, amort=84, rate_type="fixed")
    assert [k["kind"] for k in MON.key_dates(conn, fixed)] == ["maturity"]  # no balloon, no resets


def test_reminders_created_once(conn, settings):
    did = _funded(conn, settings, term=60, amort=120, io=6, reset=12)
    n = conn.execute("SELECT COUNT(*) FROM deal_reminders WHERE deal_id=?", (did,)).fetchone()[0]
    assert n == 1 + 4 + 2 + 3  # IO end + 4 resets + maturity (2 leads) + balloon (3 leads), created at funding
    assert MON.sync_reminders(conn, did) == 0 and MON.sync_all_reminders(conn) == 0
    tasks = list_tasks(conn, view="all", deal_id=did)
    assert len(tasks) == 10 and len({t["title"] for t in tasks}) == 10  # each lead time has its own title
    bal = [t for t in tasks if t["title"].startswith("Balloon")]
    assert sorted(t["due_date"] for t in bal) == sorted(
        max(date.fromordinal(add_months(TODAY, 60).toordinal() - lead), TODAY).isoformat() for lead in (365, 180, 90))
    assert all(t["priority"] == "high" for t in bal)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO deal_reminders(deal_id, kind, event_date, lead_days, created_at) "
                     "SELECT deal_id, kind, event_date, lead_days, created_at FROM deal_reminders LIMIT 1")


# ------------------------------------------------------------------------------ 7.3 watchlist
def test_watchlist_combines_without_duplicates(conn, settings):
    soon = _funded(conn, settings, term=2, amort=2, rate_type="fixed")  # matures within 90 days
    later = _funded(conn, settings, term=120, amort=120, rate_type="fixed")
    out = advance(conn, settings, "SERVICING", funding_days_ago=200)  # in-app serviced loan, payments past due
    # A deal whose underwriting file is serviced in the app: shown once, via its servicing alerts.
    linked = _funded(conn, settings, term=2, amort=2, rate_type="fixed")
    # (funded deals are locked for edits, so the serviced file is linked directly for this test)
    conn.execute("UPDATE deals SET loan_case_id=? WHERE id=?", (out["case_id"], linked))
    items = MON.watchlist(conn)
    keys = [i["key"] for i in items]
    assert len(keys) == len(set(keys))
    rules = {(i["deal_id"], i["rule"]) for i in items}
    assert (soon, "MATURITY") in rules and not any(d == later for d, _ in rules)
    assert not any(d == linked and r == "MATURITY" for d, r in rules)  # no brokered duplicate for the serviced loan
    serv = [i for i in items if i["source"] == "servicing"]
    assert serv and all(i["deal_id"] == linked for i in serv)
    from uwhelper.servicing.monitoring import alerts

    assert len(serv) == len(alerts(conn, out["account_id"]))  # matches the per-file alerts
    assert MON.sync_reminders(conn, linked) == 0  # serviced loans get no brokered reminders


# ------------------------------------------------------------------------------ 7.2 portfolio
def test_portfolio_grouping_reconciles_and_csv_is_safe(conn, settings):
    ref = crm.create_contact(conn, ContactIn(first_name="=HYPERLINK(\"x\")", last_name="Ref", contact_type="referral_partner"))
    a = _funded(conn, settings, term=84, amort=84, rate_type="fixed", amount="480000")
    b = _funded(conn, settings, term=84, amort=84, rate_type="fixed", amount="250000")
    conn.execute("UPDATE deals SET referral_source_contact_id=? WHERE id=?", (ref, b))
    for did in (a, b):
        F.add_agreement(conn, did, F.FeeAgreementIn(kind="broker_fee", basis="percent_of_funded", value="1%"))
        F.record_expected(conn, did, TODAY)
    F.record_entry(conn, a, F.LedgerEntryIn(entry_type="received", amount="4800", occurred_on=TODAY))
    rows = REP.funded_deals(conn)
    assert {r["deal_id"] for r in rows} == {a, b} and not any(r["estimate"] for r in rows)
    t = REP.totals(rows)
    assert t["funded"] == Decimal(730000) and t["commission_expected"] == Decimal("7300.00")
    assert t["commission_received"] == F.ledger(conn, a)["received"] + F.ledger(conn, b)["received"]
    by_ref = {g["group"]: g for g in REP.grouped(rows, "referral_source")}
    assert by_ref["(none)"]["funded"] == Decimal(480000) and by_ref['=HYPERLINK("x") Ref']["deals"] == 1
    by_month = REP.grouped(rows, "month")
    assert by_month[0]["group"] == TODAY.isoformat()[:7] and by_month[0]["deals"] == 2
    assert by_month[0]["average"] == Decimal("365000.00")
    csv_text = REP.to_csv(REP.grouped(rows, "referral_source"), "referral_source")
    assert csv_text.splitlines()[0].startswith("Referral source,Deals,Funded amount")
    assert "'=HYPERLINK" in csv_text and ",=HYPERLINK" not in csv_text
    assert REP.funded_deals(conn, date_to=TODAY - timedelta(days=1)) == []
    assert REP.funded_deals(conn, deal_type="cre") == []


# ------------------------------------------------------------------------------ 7.4 payoff quotes
def test_payoff_quote_hand_worked_and_rechecked(conn, settings):
    out = advance(conn, settings, "SERVICING", funding_days_ago=200)
    acct = out["account_id"]
    st = account_state(conn, acct)
    first = st.installments[0]
    record_payment(conn, acct, PaymentIn(received_date=date.fromisoformat(first["due_date"]), amount=first["payment"]))
    st = account_state(conn, acct)
    principal = st.outstanding_principal
    gt = TODAY + timedelta(days=10)
    q365 = PO.get_quote(conn, acct, PO.create_quote(conn, acct, gt, "actual/365"))
    q360 = PO.get_quote(conn, acct, PO.create_quote(conn, acct, gt, "actual/360"))
    days = (gt - date.fromisoformat(first["due_date"])).days
    assert q365["paid_through"] == first["due_date"] and q365["days"] == days
    for q, basis in ((q365, 365), (q360, 360)):
        exact = principal * Decimal("0.0825") / basis
        assert Decimal(q["per_diem"]) == exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        acc = (exact * days).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        assert Decimal(q["accrued_interest"]) == acc and Decimal(q["total"]) == principal + acc
    assert Decimal(q360["total"]) > Decimal(q365["total"])
    assert PO.verify_quote(conn, acct, q365["id"]) == {"inputs_intact": True, "reproduced": True, "ledger_changed": []}
    record_payment(conn, acct, PaymentIn(received_date=TODAY - timedelta(days=1), amount="10000.00"))
    assert "principal" in PO.verify_quote(conn, acct, q365["id"])["ledger_changed"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE payoff_quotes SET total='1' WHERE id=?", (q365["id"],))
    with pytest.raises(ValidationFailure, match="within 60 days"):
        PO.create_quote(conn, acct, TODAY + timedelta(days=61))
    with pytest.raises(ValidationFailure, match="within 60 days"):
        PO.create_quote(conn, acct, TODAY - timedelta(days=1))
    assert PO.compute({"principal": "100000", "annual_rate": "0.06", "paid_through": "2026-01-01",
                       "good_through": "2026-01-31", "day_count": "actual/360", "unpaid_fees": "25"}) == \
        {"days": 30, "per_diem": Decimal("16.67"), "accrued_interest": Decimal("500.00"), "total": Decimal("100525.00")}


# ------------------------------------------------------------------------------ migration
def test_upgrade_from_schema12(tmp_path, settings):
    from uwhelper.bootstrap import initialise
    from uwhelper.db.connection import Database

    old = tmp_path / "m12"
    old.mkdir()
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(f.name[:4]) <= 12:
            (old / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    settings.ensure_dirs()
    with Database(settings.db_path).session() as c:
        apply_migrations(c, old)
    db, _ = initialise(settings)
    with db.session() as c:
        assert current_version(c) == max(m.version for m in discover())
        cols = {r[1] for r in c.execute("PRAGMA table_info(deal_closings)")}
        assert "rate_reset_months" in cols


# ------------------------------------------------------------------------------ web + API
@pytest.fixture
def web(settings):
    from fastapi.testclient import TestClient

    from uwhelper.web.app import create_app

    app = create_app(settings)
    client = TestClient(app)
    return app, client, client.get("/api/v1/session").json()["csrf_token"]


def test_portfolio_pages_and_api(web, settings):
    app, c, token = web
    h = {"X-CSRF-Token": token}
    with app.state.db.session() as conn:
        did = _funded(conn, settings, term=2, amort=2, rate_type="fixed")
        out = advance(conn, settings, "SERVICING", funding_days_ago=200)
    page = c.get(f"/deals/{did}").text
    assert "Loan monitoring" in page and "Maturity" in page
    assert "Funded deals" in c.get("/portfolio/deals?by=product").text
    r = c.get("/portfolio/deals?by=lender&format=csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"] and "Riverstone" in r.text
    w = c.get("/watchlist").text
    assert "Watchlist" in w and "Loan matures" in w and "servicing" in w
    assert c.get("/tasks").status_code == 200
    r = c.post(f"/cases/{out['case_id']}/servicing/payoff/quote",
               data={"csrf_token": token, "good_through": (TODAY + timedelta(days=5)).isoformat(),
                     "day_count": "actual/360"}, follow_redirects=False)
    assert r.status_code == 303 and "quote=" in r.headers["location"]
    sp = c.get(r.headers["location"].split("#")[0]).text
    assert "re-checked" in sp and "actual/360" in sp
    api = c.post(f"/api/v1/underwriting-files/{out['case_id']}/payoff-quotes", headers=h,
                 json={"good_through": (TODAY + timedelta(days=5)).isoformat()})
    assert api.status_code == 201
    assert c.get(f"/api/v1/underwriting-files/{out['case_id']}/payoff-quotes/{api.json()['id']}/verify").json()["reproduced"]
    assert c.get(f"/api/v1/deals/{did}/key-dates").json()["items"][0]["kind"] == "maturity"
    assert any(i["deal_id"] == did for i in c.get("/api/v1/watchlist").json()["items"])
    p = c.get("/api/v1/portfolio/deals?by=month").json()
    assert p["totals"]["deals"] == 1 and p["groups"][0]["group"] == TODAY.isoformat()[:7]
    assert c.post("/api/v1/reminders/sync", headers=h).json()["created"] == 0
