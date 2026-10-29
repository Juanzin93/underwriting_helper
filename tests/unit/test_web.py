import re

import pytest
from fastapi.testclient import TestClient

from uwhelper.documents.pdfwrite import lines_pdf
from uwhelper.web.app import create_app


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    return TestClient(app)


def post(client, app, url, data=None, **kw):
    return client.post(url, data={**(data or {}), "csrf_token": app.state.csrf_token}, follow_redirects=False, **kw)


def new_case(client, app):
    r = post(client, app, "/cases", {"title": "Web test", "loan_type": "equipment", "b_legal_name": "Web Co",
                                      "t_requested_amount": "250,000", "t_interest_rate": "7.5",
                                      "t_amortization_months": "60", "is_secured": "on"})
    assert r.status_code == 303
    return r.headers["location"].split("?")[0].rsplit("/", 1)[1]


def test_security_headers_and_local_assets(client):
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "same-origin"
    assert "http://" not in re.sub(r"http://127\.0\.0\.1:\d+", "", r.text).replace("http://127.0.0.1", "")
    assert client.get("/docs").status_code == 404  # API docs (which load CDN assets) disabled


def test_csrf_origin_and_host_checks(client, app):
    assert client.post("/cases", data={"title": "x"}).status_code == 403
    assert post(client, app, "/cases", {"title": "x"}, headers={"origin": "https://evil.example"}).status_code == 403
    assert post(client, app, "/cases", {"title": "x"}, headers={"origin": "null"}).status_code == 403
    assert client.get("/", headers={"host": "attacker.example"}).status_code == 400


def test_create_case_and_validation_errors(client, app):
    cid = new_case(client, app)
    assert "Web Co" in client.get(f"/cases/{cid}").text
    r = post(client, app, "/cases", {"title": "", "loan_type": "equipment", "b_legal_name": "x"})
    assert r.status_code == 400 and "title" in r.text
    r = post(client, app, "/cases", {"title": "x", "loan_type": "sba_7a", "b_legal_name": "x"})
    assert r.status_code == 400 and "not implemented" in r.text
    r = post(client, app, f"/cases/{cid}/terms", {"requested_amount": "abc"})
    assert r.status_code == 400


def test_full_workflow_upload_verify_run_report(client, app):
    cid = new_case(client, app)
    files = {"files": ("is.pdf", lines_pdf(["Revenue 1,000,000"]), "application/pdf")}
    r = client.post(f"/cases/{cid}/documents?csrf_token={app.state.csrf_token}", data={"doc_type": "income_statement"},
                    files=files, follow_redirects=False)
    assert r.status_code == 303 and "imported" in r.headers["location"]
    r = client.post(f"/cases/{cid}/documents?csrf_token={app.state.csrf_token}", files=files, follow_redirects=False)
    assert "SHA-256" in r.headers["location"] or "identical" in r.headers["location"].replace("+", " ")
    doc_id = re.search(rf"/cases/{cid}/documents/([0-9a-f]{{32}})", client.get(f"/cases/{cid}/documents").text).group(1)
    assert client.get(f"/cases/{cid}/documents/{doc_id}/pages/1.png").headers["content-type"] == "image/png"
    assert client.get(f"/cases/{cid}/documents/{doc_id}/file").headers["content-type"] == "application/octet-stream"

    r = post(client, app, f"/cases/{cid}/facts", {"field_key": "revenue", "value": "1,000,000", "period_end": "2025-12-31",
                                                 "period_months": "12", "document_id": doc_id, "page_number": "1"})
    assert r.status_code == 303
    page = client.get(f"/cases/{cid}/review").text
    fid = re.search(rf"/cases/{cid}/facts/([0-9a-f]{{32}})/history", page).group(1)
    assert "UNVERIFIED" in page
    assert post(client, app, f"/cases/{cid}/facts/{fid}/correct", {"new_value": "1,100,000", "reason": "x"}).status_code == 400
    assert post(client, app, f"/cases/{cid}/facts/{fid}/correct",
                {"new_value": "1,100,000", "reason": "misread", "mark_verified": "on"}).status_code == 303
    hist = client.get(f"/cases/{cid}/facts/{fid}/history").text
    assert "Version 1" in hist and "Version 2" in hist and "misread" in hist

    assert post(client, app, f"/cases/{cid}/run", {"mode": "verified_only"}).status_code == 303
    for tab in ("metrics", "findings", "missing", "contradictions", "scenarios", "runs", "reports", "audit", "financials"):
        assert client.get(f"/cases/{cid}/{tab}").status_code == 200, tab
    assert "INCOMPLETE" in client.get(f"/cases/{cid}/findings").text
    assert post(client, app, f"/cases/{cid}/reports", {"format": "html"}).status_code == 303
    rid = re.search(rf"/cases/{cid}/reports/([0-9a-f]{{32}})", client.get(f"/cases/{cid}/reports").text).group(1)
    rep = client.get(f"/cases/{cid}/reports/{rid}")
    assert "Preliminary Credit Analysis" in rep.text and "default-src 'none'" in rep.headers["content-security-policy"]


def test_delete_requires_typed_case_number(client, app):
    cid = new_case(client, app)
    assert post(client, app, f"/cases/{cid}/delete", {"confirm_case_number": "wrong"}).status_code == 400
    number = re.search(r"UW-\d{4}-\d{4}", client.get(f"/cases/{cid}").text).group(0)
    assert post(client, app, f"/cases/{cid}/delete", {"confirm_case_number": number}).status_code == 303
    assert client.get(f"/cases/{cid}").status_code == 404


def test_errors_do_not_leak_content(client, app, monkeypatch):
    import uwhelper.web.routes.cases as routes

    def boom(*a, **k):
        raise RuntimeError("SECRET BORROWER REVENUE 123")
    monkeypatch.setattr(routes.repo, "list_cases", boom)
    r = TestClient(app, raise_server_exceptions=False).get("/")
    assert r.status_code == 500 and "SECRET" not in r.text
