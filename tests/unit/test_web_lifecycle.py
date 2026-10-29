"""Drive the full loan lifecycle through the web forms (synthetic sample data, no network, no LLM)."""

import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.lifecycle_helpers import clean_sample
from uwhelper.web.app import create_app

PAGES = ["", "/workflow", "/memo", "/structuring", "/decision", "/conditions", "/closing", "/funding", "/servicing",
         "/audit", "/documents", "/review", "/financials", "/metrics", "/findings", "/missing", "/contradictions",
         "/scenarios", "/runs", "/reports"]


@pytest.fixture
def web(settings):
    app = create_app(settings)
    return app, TestClient(app)


def post(client, app, url, data=None, expect=303):
    r = client.post(url, data={**(data or {}), "csrf_token": app.state.csrf_token}, follow_redirects=False)
    err = re.search(r'<div class="panel"><h1>.*?</div>', r.text, re.S)
    assert r.status_code == expect, (url, r.status_code, err.group(0) if err else r.text[-800:])
    return r


def msg(r) -> str:
    return r.headers.get("location", "").replace("+", " ")


def all_pages_ok(client, cid):
    for p in PAGES:
        r = client.get(f"/cases/{cid}{p}")
        assert r.status_code == 200, (p, r.text[-2000:])


def test_full_lifecycle_through_web(web, conn, settings):
    app, client = web
    cid = clean_sample(conn, settings)
    all_pages_ok(client, cid)
    page = client.get(f"/cases/{cid}/workflow").text
    assert "Stage: Intake" in page and "Advance to Underwriting" in page

    # Missing reason is refused with a message, not a crash.
    r = post(client, app, f"/cases/{cid}/workflow/transition", {"to_stage": "UNDERWRITING", "reason": "x"})
    assert "level=error" in r.headers["location"]
    post(client, app, f"/cases/{cid}/workflow/transition", {"to_stage": "UNDERWRITING", "reason": "Docs in; underwriting"})
    r = post(client, app, f"/cases/{cid}/workflow/transition", {"to_stage": "CREDIT_REVIEW", "reason": "Trying without run"})
    assert "No verified-only underwriting run" in msg(r)
    post(client, app, f"/cases/{cid}/run", {"mode": "verified_only"})
    post(client, app, f"/cases/{cid}/workflow/transition", {"to_stage": "CREDIT_REVIEW", "reason": "Verified screening done"})
    all_pages_ok(client, cid)

    run_id = re.search(r'<option value="([0-9a-f]{32})">#1', client.get(f"/cases/{cid}/memo").text).group(1)
    post(client, app, f"/cases/{cid}/memo", {"run_id": run_id, "analyst_industry_risks": "Two customers = 45% of sales.",
                                             "include_pdf": "on"})
    memo_page = client.get(f"/cases/{cid}/memo").text
    mid = re.search(rf"/cases/{cid}/memo/([0-9a-f]{{32}})/html", memo_page).group(1)
    html = client.get(f"/cases/{cid}/memo/{mid}/html")
    assert "Preliminary Credit Memo" in html.text and "Two customers" in html.text
    assert "default-src 'none'" in html.headers["content-security-policy"]
    assert client.get(f"/cases/{cid}/memo/{mid}/pdf").headers["content-type"] == "application/pdf"
    assert "intact" in msg(client.get(f"/cases/{cid}/memo/{mid}/verify", follow_redirects=False))

    post(client, app, f"/cases/{cid}/structuring/baseline")
    post(client, app, f"/cases/{cid}/structuring", {"name": "IO then amortize", "principal": "480000",
                                                    "annual_rate": "8.25", "term_months": "84",
                                                    "amortization_months": "72", "interest_only_months": "12",
                                                    "payment_frequency": "monthly", "cfads_source": "run"})
    sp = client.get(f"/cases/{cid}/structuring").text
    sid = re.findall(rf"/cases/{cid}/structuring/([0-9a-f]{{32}})\"", sp)[-1]
    assert "IO then amortize" in sp and "DSCR_MIN" in sp
    assert client.get(f"/cases/{cid}/structuring/{sid}").status_code == 200
    post(client, app, f"/cases/{cid}/structuring/{sid}/proposed")
    assert client.get(f"/cases/{cid}/decision?from_structure={sid}").status_code == 200

    decision = {"decision": "APPROVE_WITH_CONDITIONS", "decided_by": "Web Officer", "rationale": "Coverage adequate; "
                "guarantee required.", "conditions_text": "Personal guaranty", "t_principal": "480000",
                "t_annual_rate": "8.25", "t_term_months": "84", "t_amortization_months": "72",
                "t_interest_only_months": "12", "t_payment_frequency": "monthly", "t_source_structure_id": sid}
    r = post(client, app, f"/cases/{cid}/decision", decision, expect=400)
    assert "responsible human" in r.text
    post(client, app, f"/cases/{cid}/decision", {**decision, "confirm_human": "on"})
    post(client, app, f"/cases/{cid}/workflow/transition", {"to_stage": "STRUCTURING", "reason": "Approved by officer"})
    post(client, app, f"/cases/{cid}/conditions/generate")
    post(client, app, f"/cases/{cid}/workflow/transition", {"to_stage": "DUE_DILIGENCE", "reason": "Terms approved"})
    all_pages_ok(client, cid)

    # Satisfy / waive all required DD conditions through the forms.
    from uwhelper.workflow import conditions as C

    for c in C.list_conditions(conn, cid, "DUE_DILIGENCE"):
        if not c["required"]:
            continue
        if c["code"] == "DEC-01":
            post(client, app, f"/cases/{cid}/conditions/{c['id']}/waive",
                 {"waived_by": "Senior Officer", "reason": "Guaranty signed at closing instead", "confirm": "on"})
            continue
        for st in ("RECEIVED", "UNDER_REVIEW"):
            post(client, app, f"/cases/{cid}/conditions/{c['id']}/status", {"to_status": st})
        post(client, app, f"/cases/{cid}/conditions/{c['id']}/status",
             {"to_status": "SATISFIED", "evidence_note": "Original reviewed", "verified": "on"})
    post(client, app, f"/cases/{cid}/workflow/transition", {"to_stage": "CLOSING", "reason": "Due diligence complete"})

    post(client, app, f"/cases/{cid}/closing", {"scheduled_closing_date": date.today().isoformat(),
                                                "disbursement_status": "INSTRUCTIONS_RECORDED",
                                                "disbursement_instructions": "Wire per signed letter"})
    post(client, app, f"/cases/{cid}/closing/executed-terms", {k: v for k, v in decision.items() if k.startswith("t_")}
         | {"t_entered_by": "Closer"})
    for c in C.list_conditions(conn, cid, "CLOSING"):
        for st in ("RECEIVED", "UNDER_REVIEW"):
            post(client, app, f"/cases/{cid}/conditions/{c['id']}/status", {"to_status": st, "next": f"/cases/{cid}/closing"})
        post(client, app, f"/cases/{cid}/conditions/{c['id']}/status",
             {"to_status": "SATISFIED", "evidence_note": "Signed", "verified": "on", "next": f"/cases/{cid}/closing"})
    all_pages_ok(client, cid)
    fd = date.today() - timedelta(days=95)
    funding = {"funded_amount": "480000", "funding_date": fd.isoformat(),
               "first_payment_date": (fd + timedelta(days=30)).isoformat(), "confirmed_by": "Ops"}
    r = post(client, app, f"/cases/{cid}/funding", {**funding, "confirmation_text": "ok"}, expect=400)
    assert "Type exactly" in r.text
    post(client, app, f"/cases/{cid}/funding", {**funding, "confirmation_text": "I CONFIRM THIS LOAN WAS FUNDED"})
    assert "Open servicing account" in client.get(f"/cases/{cid}/servicing").text
    post(client, app, f"/cases/{cid}/servicing/open", {"grace_days": "10", "add_default_monitoring": "on"})
    all_pages_ok(client, cid)

    sv = client.get(f"/cases/{cid}/servicing").text
    assert "PAYMENT_PAST_DUE" in sv  # nothing recorded yet -> past due, not assumed paid
    due = re.findall(r"<td>(\d{4}-\d{2}-\d{2})</td><td class=\"num\">\$([\d,]+\.\d\d)", sv)
    first_due, amount = due[0][0], due[0][1].replace(",", "")
    post(client, app, f"/cases/{cid}/servicing/payment", {"received_date": first_due, "amount": amount, "reference": "ACH1"})
    csv = f"date,amount,reference\n{due[1][0]},{due[1][1].replace(',', '')},ACH2\n".encode()
    r = client.post(f"/cases/{cid}/servicing/import?csrf_token={app.state.csrf_token}",
                    files={"file": ("p.csv", csv, "text/csv")}, follow_redirects=False)
    assert r.status_code == 303 and "Imported 1" in msg(r)
    post(client, app, f"/cases/{cid}/servicing/covenant", {"code": "MIN-DSCR", "description": "Minimum DSCR",
                                                           "metric_key": "dscr", "operator": ">=", "threshold": "1.25",
                                                           "frequency": "annual", "first_test_date": "2025-12-31",
                                                           "reporting_days": "120"})
    from uwhelper.servicing.ledger import account_for_case

    acct = account_for_case(conn, cid)
    cov_id = conn.execute("SELECT id FROM covenants WHERE account_id=?", (acct["id"],)).fetchone()[0]
    post(client, app, f"/cases/{cid}/servicing/covenant-test", {"covenant_id": cov_id, "period_end": "2025-12-31",
                                                                "run_id": run_id})
    post(client, app, f"/cases/{cid}/servicing/refresh")
    sv = client.get(f"/cases/{cid}/servicing").text
    assert "ACH1" in sv and "PASS" in sv
    portfolio = client.get("/portfolio")
    assert portfolio.status_code == 200 and acct["loan_number"] in portfolio.text
    audit = client.get("/audit")
    assert "Hash chain: intact" in audit.text and "funding confirmed" in audit.text
    dash = client.get("/")
    assert "Servicing: <b>1</b>" in dash.text
