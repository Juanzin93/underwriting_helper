"""Milestone 1: settings, database, migrations, CRM entities, API errors, audit, files, health, offline, bind."""

import logging
import socket
import sqlite3
from pathlib import Path

import httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient

from tests.conftest import ExternalNetworkBlocked
from uwhelper.bootstrap import initialise
from uwhelper.config import ConfigError, Settings, load_settings, read_env_file
from uwhelper.db.connection import Database
from uwhelper.db.migrate import (
    MIGRATIONS_DIR,
    apply_migrations,
    current_version,
    discover,
)
from uwhelper.integrations import (
    IntegrationDisabled,
    IntegrationSpec,
    is_enabled,
    register,
    require_enabled,
)
from uwhelper.repo.audit import list_events, verify_chain
from uwhelper.web.app import NO_TELEMETRY, create_app

LATEST = max(m.version for m in discover())


# ------------------------------------------------------------------------------------------ fixtures
@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def api(app, client):
    token = client.get("/api/v1/session").json()["csrf_token"]

    class Api:
        def get(self, url, **kw):
            return client.get(url, **kw)

        def post(self, url, json=None, **kw):
            return client.post(url, json=json, headers={"X-CSRF-Token": token, **kw.pop("headers", {})}, **kw)

        def patch(self, url, json=None):
            return client.patch(url, json=json, headers={"X-CSRF-Token": token})

        def put(self, url, json=None):
            return client.put(url, json=json, headers={"X-CSRF-Token": token})

        def delete(self, url):
            return client.delete(url, headers={"X-CSRF-Token": token})
    return Api()


def pdf_bytes(text="Revenue 1,000") -> bytes:
    d = pymupdf.open()
    d.new_page().insert_text((72, 72), text)
    return d.tobytes()


def make_org(api, name="Acme Fabrication LLC (FICTIONAL)", org_type="borrower_business"):
    r = api.post("/api/v1/organizations", {"name": name, "org_type": org_type, "city": "Springfield"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_deal(api, org_id=None, **kw):
    body = {"name": "Equipment line", "deal_type": "small_business", "product_type": "equipment",
            "requested_amount": "250000", "borrower_organization_id": org_id, **kw}
    r = api.post("/api/v1/deals", body)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------------------------------ settings
def test_default_settings_are_local_and_closed(tmp_path):
    s = load_settings(data_dir=tmp_path)
    assert s.host == "127.0.0.1" and s.ollama_url.startswith("http://127.0.0.1")
    assert s.external_integrations_allowed is False and s.integration_names == frozenset()
    assert s.db_path == tmp_path / "uwhelper.db" and s.documents_dir == tmp_path / "documents"


def test_env_file_and_precedence(tmp_path, monkeypatch):
    env = tmp_path / "local.env"
    env.write_text("# comment\nUWH_PORT=8800\nUWH_OLLAMA_MODEL='qwen3:4b'\nOTHER_SECRET=x\n"
                   f"UWH_DOCUMENTS_PATH={tmp_path / 'docs'}\nUWH_DB_FILE={tmp_path / 'db' / 'main.db'}\n",
                   encoding="utf-8")
    assert "OTHER_SECRET" not in read_env_file(env)
    monkeypatch.setenv("UWH_ENV_FILE", str(env))
    monkeypatch.setenv("UWH_PORT", "8900")  # real environment beats the .env file
    s = load_settings(data_dir=tmp_path)
    assert s.port == 8900 and s.ollama_model == "qwen3:4b"
    assert s.documents_dir == tmp_path / "docs" and s.db_path == tmp_path / "db" / "main.db"
    s.ensure_dirs()
    assert (tmp_path / "db").is_dir()


@pytest.mark.parametrize("kw,msg", [
    (dict(enabled_integrations="bureau"), "external_integrations_allowed"),
    (dict(host="0.0.0.0"), "Refusing to bind"),
    (dict(ollama_url="http://192.168.1.5:11434"), "non-loopback"),
    (dict(db_busy_timeout_ms=5), "db_busy_timeout_ms"),
    (dict(log_level="TRACE"), "log_level"),
])
def test_unsafe_settings_refused(tmp_path, kw, msg):
    with pytest.raises(ConfigError, match=msg):
        load_settings(data_dir=tmp_path, **kw)


def test_data_dir_inside_static_refused():
    from uwhelper.config import PACKAGE_DIR

    with pytest.raises(ConfigError, match="static"):
        load_settings(data_dir=PACKAGE_DIR / "web" / "static" / "data")


def test_integrations_disabled_unless_both_flags(tmp_path):
    register(IntegrationSpec("demo_provider", "Test-only provider", "nothing (test)"))
    off = load_settings(data_dir=tmp_path)
    assert not is_enabled(off, "demo_provider")
    with pytest.raises(IntegrationDisabled):
        require_enabled(off, "demo_provider")
    on = load_settings(data_dir=tmp_path, external_integrations_allowed=True, enabled_integrations="demo_provider")
    assert is_enabled(on, "demo_provider")
    with pytest.raises(IntegrationDisabled, match="not registered"):
        require_enabled(on, "unknown")


# ------------------------------------------------------------------------------------------ database
def test_clean_database_initialises_from_migrations(settings):
    db, report = initialise(settings)
    assert not report.errors
    with db.session() as c:
        assert current_version(c) == LATEST
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == settings.db_busy_timeout_ms
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"organizations", "contacts", "deals", "deal_status_history", "deal_contacts", "deal_documents",
                "loan_cases", "audit_events"} <= tables
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upgrade_from_previous_schema_preserves_data(tmp_path, settings):
    """A database at schema 6 (previous release) with real records upgrades to the latest without data loss."""
    from uwhelper.domain.models import FactIn, TermsIn
    from uwhelper.repo import cases, facts

    old_dir = tmp_path / "m6"
    old_dir.mkdir()
    for m in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(m.name[:4]) <= 6:
            (old_dir / m.name).write_text(m.read_text(encoding="utf-8"), encoding="utf-8")
    db = Database(settings.db_path)
    settings.ensure_dirs()
    with db.session() as c:
        assert apply_migrations(c, old_dir) == [1, 2, 3, 4, 5, 6]
        # Seed rows the way the previous release wrote them (today's code expects newer tables).
        cid, now = "c" * 32, "2026-01-01T00:00:00+00:00"
        c.execute("INSERT INTO loan_cases(id, case_number, title, loan_type, stage, stage_entered_at, created_at, "
                  "updated_at) VALUES (?, 'UW-2026-0001', 'Pre-upgrade', 'equipment', 'INTAKE', ?, ?, ?)",
                  (cid, now, now, now))
        c.execute("INSERT INTO borrowers(id, case_id, legal_name, created_at, updated_at) VALUES ('b1', ?, 'Old Co', ?, ?)",
                  (cid, now, now))
        cases.save_terms(c, cid, TermsIn(requested_amount="100000", interest_rate="8", amortization_months=60))
        facts.add_user_fact(c, cid, FactIn(field_key="revenue", value="900000", period_end="2025-12-31",
                                           period_months=12, mark_verified=True))
        before = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("loan_cases", "facts", "audit_events", "workflow_transitions", "loan_terms")}
    db2, _ = initialise(settings)  # what the app does on start
    with db2.session() as c:
        assert current_version(c) == LATEST
        after = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
        assert after["loan_cases"] == before["loan_cases"] and after["facts"] == before["facts"]
        assert after["loan_terms"] == before["loan_terms"]
        assert c.execute("SELECT title FROM loan_cases WHERE id=?", (cid,)).fetchone()[0] == "Pre-upgrade"
        assert verify_chain(c)["ok"]
        assert apply_migrations(c) == []  # idempotent


def test_shutdown_checkpoints_wal(settings):
    db, _ = initialise(settings)
    with db.session() as c:
        c.execute("INSERT INTO organizations(id, name, org_type, created_at, updated_at) VALUES ('o','x','other','t','t')")
    db.shutdown()
    wal = Path(str(settings.db_path) + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_lifespan_shutdown_runs(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        assert c.get("/health?checks=basic").status_code == 200
    wal = Path(str(settings.db_path) + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_foreign_keys_and_relationship_rules(api, app):
    org = make_org(api)
    deal = make_deal(api, org)
    with app.state.db.session() as c:
        with pytest.raises(sqlite3.IntegrityError):  # RESTRICT: borrower org still referenced by a deal
            c.execute("DELETE FROM organizations WHERE id=?", (org,))
        with pytest.raises(sqlite3.IntegrityError):  # FK: unknown organization
            c.execute("UPDATE deals SET borrower_organization_id='nope' WHERE id=?", (deal["id"],))
        with pytest.raises(sqlite3.IntegrityError):  # history is append-only
            c.execute("UPDATE deal_status_history SET reason='x'")
        c.execute("DELETE FROM deals WHERE id=?", (deal["id"],))
        assert c.execute("SELECT COUNT(*) FROM deal_status_history WHERE deal_id=?", (deal["id"],)).fetchone()[0] == 0


def test_deleting_underwriting_file_unlinks_deal(api, app, settings):
    from uwhelper.repo.cases import purge_case

    deal = make_deal(api, make_org(api))
    case_id = api.post(f"/api/v1/deals/{deal['id']}/underwriting-file").json()["loan_case_id"]
    with app.state.db.session() as c:
        purge_case(c, case_id, settings.documents_dir, settings.reports_dir)
    assert api.get(f"/api/v1/deals/{deal['id']}").json()["loan_case_id"] is None


# ------------------------------------------------------------------------------------------ API + validation
def test_api_crud_organizations_contacts(api):
    org = make_org(api)
    r = api.post("/api/v1/contacts", {"first_name": "Dana", "last_name": "Example", "email": "Dana@Example.com",
                                      "contact_type": "borrower", "organization_id": org, "organization_role": "owner"})
    assert r.status_code == 201
    contact = r.json()
    assert contact["email"] == "dana@example.com" and contact["organizations"][0]["role"] == "owner"
    assert api.get("/api/v1/contacts?q=dana").json()["items"][0]["id"] == contact["id"]
    r = api.patch(f"/api/v1/contacts/{contact['id']}", {"title": "Owner", "phone": "555-010-0000"})
    assert sorted(r.json()["changed"]) == ["phone", "title"]
    r = api.patch(f"/api/v1/organizations/{org}", {"industry": "Fabrication"})
    assert r.json()["organization"]["industry"] == "Fabrication"
    assert api.get(f"/api/v1/organizations/{org}").json()["contacts"][0]["id"] == contact["id"]


def test_api_error_format(api, client):
    r = api.post("/api/v1/organizations", {"org_type": "lender", "ssn": "123-45-6789"})
    body = r.json()
    assert r.status_code == 422 and body["error"]["code"] == "validation_error"
    fields = {d["field"] for d in body["error"]["details"]}
    assert {"name", "ssn"} <= fields
    assert "123-45-6789" not in r.text  # submitted values are not echoed
    assert body["error"]["request_id"] == r.headers["X-Request-ID"]
    r = api.post("/api/v1/organizations", {"name": "x", "org_type": "not_a_type"})
    assert r.status_code == 422
    r = api.post("/api/v1/contacts", {"first_name": "A", "last_name": "B", "email": "not-an-email"})
    assert r.status_code == 422 and r.json()["error"]["details"][0]["field"] == "email"
    r = api.get("/api/v1/deals/doesnotexist")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = api.get("/api/v1/organizations?limit=5000")
    assert r.status_code == 422
    r = client.post("/api/v1/organizations", json={"name": "x", "org_type": "lender"})  # no CSRF header
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    r = client.get("/api/v1/session", headers={"host": "evil.example"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_host"
    r = api.get("/api/v1/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "http_404"


def test_duplicate_organization_is_validation_error(api):
    make_org(api)
    r = api.post("/api/v1/organizations", {"name": "ACME FABRICATION LLC (FICTIONAL)", "org_type": "borrower_business"})
    assert r.status_code == 422 and "already exists" in r.json()["error"]["message"]


def test_request_id_propagation(client):
    r = client.get("/health?checks=basic", headers={"X-Request-ID": "abc12345-test"})
    assert r.headers["X-Request-ID"] == "abc12345-test"
    r = client.get("/health?checks=basic", headers={"X-Request-ID": "bad id <script>"})
    assert r.headers["X-Request-ID"] != "bad id <script>" and len(r.headers["X-Request-ID"]) == 32


def test_deal_workflow_and_status_rules(api):
    contact = api.post("/api/v1/contacts", {"first_name": "Rae", "last_name": "Referrer",
                                            "contact_type": "referral_partner"}).json()["id"]
    r = api.post("/api/v1/deals", {"name": "x", "deal_type": "cre", "product_type": "equipment"})
    assert r.status_code == 422 and "product_type" in r.json()["error"]["message"]
    deal = make_deal(api, None, referral_source_contact_id=contact)
    assert deal["status"] == "INTAKE" and deal["deal_number"].startswith("DL-")
    assert deal["contacts"][0]["role"] == "referral_partner"
    did = deal["id"]
    r = api.post(f"/api/v1/deals/{did}/status", {"to_status": "UNDERWRITING", "reason": "Ready to underwrite"})
    assert r.status_code == 422 and "borrower" in r.json()["error"]["message"]
    api.patch(f"/api/v1/deals/{did}", {"borrower_organization_id": make_org(api)})
    r = api.post(f"/api/v1/deals/{did}/status", {"to_status": "FUNDED", "reason": "Skipping straight to funded"})
    assert r.status_code == 422 and "not an allowed" in r.json()["error"]["message"]
    for to in ("UNDERWRITING", "STRUCTURING", "LENDER_SUBMISSION"):
        assert api.post(f"/api/v1/deals/{did}/status", {"to_status": to, "reason": f"Moving to {to}"}).status_code == 200
    r = api.post(f"/api/v1/deals/{did}/status", {"to_status": "APPROVED", "reason": "Lender approval letter received"})
    assert r.status_code == 422 and "authorized" in r.json()["error"]["message"]
    r = api.post(f"/api/v1/deals/{did}/status", {"to_status": "APPROVED", "reason": "Lender approval letter received",
                                                 "authorized_by": "Pat Broker"})
    assert r.status_code == 422 and "Confirm" in r.json()["error"]["message"]
    api.post(f"/api/v1/deals/{did}/status", {"to_status": "APPROVED", "reason": "Lender approval letter received",
                                             "authorized_by": "Pat Broker", "confirm": True})
    api.post(f"/api/v1/deals/{did}/status", {"to_status": "ON_HOLD", "reason": "Borrower travelling"})
    r = api.post(f"/api/v1/deals/{did}/status", {"to_status": "RESUME", "reason": "Borrower back in town"})
    assert r.json().get("status") == "APPROVED", r.text
    r = api.post(f"/api/v1/deals/{did}/status", {"to_status": "DUE_DILIGENCE", "reason": "Moving to diligence"})
    assert r.status_code == 422 and "accepted lender offer" in r.json()["error"]["message"]  # M6 gate (soft)
    for to in ("DUE_DILIGENCE", "CLOSING"):
        assert api.post(f"/api/v1/deals/{did}/status", {
            "to_status": to, "reason": f"Moving to {to}",
            "override_reason": "Offer accepted by phone; terms to be recorded later"}).status_code == 200
    # Since M6 a deal becomes FUNDED only through the funding confirmation.
    r = api.post(f"/api/v1/deals/{did}/status", {"to_status": "FUNDED", "reason": "Lender confirmed funding",
                                                 "authorized_by": "Pat Broker", "confirm": True})
    assert r.status_code == 422 and "Confirm funding" in r.json()["error"]["message"]
    assert api.put(f"/api/v1/deals/{did}/closing/record", {"closing_date": "2026-01-15"}).status_code == 200
    r = api.post(f"/api/v1/deals/{did}/funding", {"funded_amount": "480000", "funding_date": "2026-01-15",
                                                  "confirmed_by": "Pat Broker",
                                                  "confirmation_text": "I confirm this loan was funded",
                                                  "override_reason": "Offer accepted by phone; terms to be recorded"})
    assert r.status_code == 201, r.text
    d = api.get(f"/api/v1/deals/{did}").json()
    assert [h["to_status"] for h in d["history"]] == ["INTAKE", "UNDERWRITING", "STRUCTURING", "LENDER_SUBMISSION",
                                                      "APPROVED", "ON_HOLD", "APPROVED", "DUE_DILIGENCE", "CLOSING",
                                                      "FUNDED"]
    assert d["allowed_transitions"] == ["SERVICING"]  # funded deals can only move on to servicing
    assert api.patch(f"/api/v1/deals/{did}", {"name": "changed"}).status_code == 422


def test_deal_contacts_and_underwriting_file(api, app):
    org = make_org(api)
    c = api.post("/api/v1/contacts", {"first_name": "G", "last_name": "Guarantor", "contact_type": "guarantor"}).json()
    deal = make_deal(api, org)
    r = api.post(f"/api/v1/deals/{deal['id']}/contacts", {"contact_id": c["id"], "role": "guarantor"})
    assert r.status_code == 201 and r.json()[0]["role"] == "guarantor"
    assert api.delete(f"/api/v1/deals/{deal['id']}/contacts/{c['id']}/guarantor").status_code == 200
    up = api.post(f"/api/v1/deals/{deal['id']}/documents", files={"file": ("pnl.pdf", pdf_bytes(), "application/pdf")},
                  data={"doc_type": "income_statement"})
    assert up.status_code == 201
    case = api.post(f"/api/v1/deals/{deal['id']}/underwriting-file").json()
    with app.state.db.session() as conn:
        row = conn.execute("SELECT loan_type, (SELECT COUNT(*) FROM case_documents WHERE case_id=?) AS docs "
                           "FROM loan_cases WHERE id=?", (case["loan_case_id"], case["loan_case_id"])).fetchone()
    assert row["loan_type"] == "equipment" and row["docs"] == 1
    assert api.post(f"/api/v1/deals/{deal['id']}/underwriting-file").status_code == 422


# ------------------------------------------------------------------------------------------ audit
def test_audit_events_for_crud_and_workflow(api, app):
    org = make_org(api)
    api.patch(f"/api/v1/organizations/{org}", {"notes": "Private note about the owner", "city": "Shelbyville"})
    deal = make_deal(api, org)
    api.post(f"/api/v1/deals/{deal['id']}/status", {"to_status": "UNDERWRITING", "reason": "Docs complete for review"})
    with app.state.db.session() as c:
        ev = list_events(c)
        actions = [e["action"] for e in ev]
        assert {"organization_created", "organization_updated", "deal_created", "deal_status_changed"} <= set(actions)
        upd = next(e for e in ev if e["action"] == "organization_updated")
        assert upd["details"]["changed"]["city"] == ["Springfield", "Shelbyville"]
        assert upd["details"]["changed"]["notes"] == ["(changed)", "(changed)"]  # free text not copied into audit
        assert "Private note" not in str(ev)
        st = next(e for e in ev if e["action"] == "deal_status_changed")
        assert st["details"]["from"] == "INTAKE" and st["details"]["to"] == "UNDERWRITING"
        assert verify_chain(c)["ok"]


# ------------------------------------------------------------------------------------------ files
def test_document_storage_identifiers_and_dedupe(api, app, settings):
    deal = make_deal(api, make_org(api))
    data = pdf_bytes("Balance sheet 2025")
    r1 = api.post(f"/api/v1/deals/{deal['id']}/documents", files={"file": ("../../evil name.pdf", data, "application/pdf")})
    r2 = api.post(f"/api/v1/deals/{deal['id']}/documents", files={"file": ("copy.pdf", data, "application/pdf")})
    assert r1.status_code == 201 and r2.json()["duplicate"] and r2.json()["already_attached"]
    with app.state.db.session() as c:
        doc = dict(c.execute("SELECT * FROM documents WHERE id=?", (r1.json()["document_id"],)).fetchone())
    assert doc["original_filename"] == "evil name.pdf"  # display metadata only, sanitised
    assert doc["stored_name"] == f"{doc['sha256'][:2]}/{doc['sha256']}.pdf"
    assert (settings.documents_dir / doc["stored_name"]).is_file()
    assert len(list(settings.documents_dir.rglob("*.pdf"))) == 1
    assert doc["processing_status"] == "processed" and doc["page_count"] == 1


def test_upload_limits_and_type_validation(tmp_path):
    s = load_settings(data_dir=tmp_path, max_upload_mb=1)
    app = create_app(s)
    c = TestClient(app)
    tok = c.get("/api/v1/session").json()["csrf_token"]
    h = {"X-CSRF-Token": tok}
    org = c.post("/api/v1/organizations", json={"name": "O", "org_type": "borrower_business"}, headers=h).json()["id"]
    did = c.post("/api/v1/deals", json={"name": "D", "deal_type": "cre", "product_type": "bridge",
                                        "borrower_organization_id": org}, headers=h).json()["id"]
    big = b"%PDF-1.4\n" + b"0" * (2 * 1024 * 1024)
    r = c.post(f"/api/v1/deals/{did}/documents", files={"file": ("big.pdf", big, "application/pdf")}, headers=h)
    assert r.status_code == 422 and "1 MB" in r.json()["error"]["message"]
    r = c.post(f"/api/v1/deals/{did}/documents", files={"file": ("x.exe", b"MZ\x90\x00", "application/octet-stream")},
               headers=h)
    assert r.status_code == 422 and "Unsupported" in r.json()["error"]["message"]
    r = c.post(f"/api/v1/deals/{did}/documents", files={"file": ("fake.pdf", b"<html>no</html>", "application/pdf")},
               headers=h)
    assert r.status_code == 422 and "does not match" in r.json()["error"]["message"]
    assert not [p for p in (s.documents_dir).rglob("*") if p.is_file()]  # nothing left behind


def test_sensitive_files_not_served_statically(api, client, settings):
    deal = make_deal(api, make_org(api))
    api.post(f"/api/v1/deals/{deal['id']}/documents", files={"file": ("s.pdf", pdf_bytes("SECRET-123"), "application/pdf")})
    stored = next(settings.documents_dir.rglob("*.pdf"))
    for path in ("/static/../uwhelper.db", "/static/%2e%2e/%2e%2e/uwhelper.db", f"/static/{stored.name}",
                 f"/documents/{stored.relative_to(settings.documents_dir).as_posix()}", "/uwhelper.db",
                 "/static/../../../../etc/passwd"):
        r = client.get(path)
        assert r.status_code in (400, 404), path
        assert b"SECRET-123" not in r.content and b"SQLite format" not in r.content
    assert client.get("/static/style.css").status_code == 200


def test_logs_do_not_contain_document_content(api, caplog):
    caplog.set_level(logging.DEBUG)
    deal = make_deal(api, make_org(api))
    api.post(f"/api/v1/deals/{deal['id']}/documents",
             files={"file": ("p.pdf", pdf_bytes("CONFIDENTIAL-REVENUE-9876"), "application/pdf")})
    api.post("/api/v1/contacts", {"first_name": "Secret", "last_name": "Person", "email": "secret@example.com"})
    assert "CONFIDENTIAL-REVENUE-9876" not in caplog.text and "secret@example.com" not in caplog.text


# ------------------------------------------------------------------------------------------ health / offline
def test_health_db_ok_and_ollama_missing_is_degraded_not_down(client, api):
    r = client.get("/health")  # nothing listens on 127.0.0.1:11434 in tests
    body = r.json()
    assert r.status_code == 200
    assert body["components"]["database"]["status"] == "ok"
    assert body["components"]["database"]["schema_version"] == LATEST
    assert body["components"]["ollama"]["status"] in ("unavailable", "model_missing", "timeout")
    assert body["components"]["ollama"]["optional"] is True and body["status"] == "degraded"
    assert body["components"]["integrations"]["enabled"] == []
    assert body["bind"]["host"] == "127.0.0.1"
    # Basic CRM still works with Ollama down.
    assert make_deal(api, make_org(api))["status"] == "INTAKE"
    assert client.get("/api/health?checks=basic").json()["components"]["ollama"]["status"] == "not_checked"


def test_health_reports_local_ollama_when_available(app, client):
    tags = {"models": [{"name": "qwen3:8b", "digest": "abc123", "details": {"quantization_level": "Q4_K_M"}}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=tags if request.url.path == "/api/tags" else {"details": {}})
    app.state.ollama_transport = httpx.MockTransport(handler)
    o = client.get("/health").json()["components"]["ollama"]
    assert o["status"] == "available" and o["quantization"] == "Q4_K_M"


def test_health_503_when_database_unavailable(app, client):
    class Broken:
        def check(self):
            raise sqlite3.OperationalError("unable to open database file /secret/path")
    app.state.db = Broken()
    r = client.get("/health?checks=basic")
    assert r.status_code == 503 and r.json()["status"] == "unavailable"
    assert "/secret/path" not in r.text


def test_offline_guard_blocks_external_network():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(ExternalNetworkBlocked):
        s.connect(("93.184.216.34", 443))
    s.close()
    with pytest.raises(ExternalNetworkBlocked):
        socket.getaddrinfo("example.com", 443)


def test_fastapi_native_telemetry_disabled(app):
    import os

    assert os.environ.get("OTEL_SDK_DISABLED") == "true"
    cfg = getattr(app, "_telemetry", None)
    if cfg is not None:  # FastAPI versions with native OpenTelemetry instrumentation
        assert all(cfg[k] is False for k in NO_TELEMETRY)


def test_cli_serve_binds_localhost(monkeypatch, tmp_path):
    import uvicorn

    from uwhelper import cli

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    assert cli.main(["--data-dir", str(tmp_path), "serve"]) == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == Settings().port
    assert cli.main(["--data-dir", str(tmp_path), "serve", "--port", "8811"]) == 0 and seen["port"] == 8811


# ------------------------------------------------------------------------------------------ HTML shell
def test_crm_pages_render(api, client, app):
    org = make_org(api)
    contact = api.post("/api/v1/contacts", {"first_name": "Pat", "last_name": "Owner", "organization_id": org}).json()
    deal = make_deal(api, org)
    for path in ("/deals", f"/deals/{deal['id']}", "/contacts", f"/contacts/{contact['id']}", "/organizations",
                 f"/organizations/{org}", "/", "/deals?status=INTAKE"):
        r = client.get(path)
        assert r.status_code == 200, path
    page = client.get("/").text
    assert 'href="/deals"' in page and 'href="/contacts"' in page
    tok = app.state.csrf_token
    r = client.post(f"/deals/{deal['id']}/status", data={"csrf_token": tok, "to_status": "UNDERWRITING",
                                                         "reason": "Ready for underwriting"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post(f"/deals/{deal['id']}/documents?csrf_token={tok}",
                    files={"file": ("a.pdf", pdf_bytes(), "application/pdf")}, data={"doc_type": "balance_sheet"},
                    follow_redirects=False)
    assert r.status_code == 303 and "Uploaded" in r.headers["location"].replace("+", " ")
    r = client.post("/organizations", data={"csrf_token": tok, "name": "", "org_type": "lender"})
    assert r.status_code == 400 and "name" in r.text
