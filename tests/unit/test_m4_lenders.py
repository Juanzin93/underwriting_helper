"""Milestone 4: lender programs, versioned criteria, explainable matching, freshness, catalog import, UI and API.
Temporary databases only; no network. All lenders are fictional."""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.lifecycle_helpers import advance
from uwhelper.crm import service as crm
from uwhelper.crm.models import DealIn, OrganizationIn
from uwhelper.db.migrate import (
    MIGRATIONS_DIR,
    apply_migrations,
    current_version,
    discover,
)
from uwhelper.lenders import importer as LI
from uwhelper.lenders import matching as M
from uwhelper.lenders import service as L
from uwhelper.lenders.models import (
    CriteriaVersionIn,
    Criterion,
    ProgramIn,
    ProgramPatch,
)
from uwhelper.repo.audit import list_events, verify_chain
from uwhelper.repo.cases import ValidationFailure

SAMPLE = Path(__file__).resolve().parents[2] / "samples" / "lender_catalog_example.yaml"
TODAY = date.today()


def _lender(conn, name="Test Bank (FICTIONAL)"):
    return crm.create_organization(conn, OrganizationIn(name=name, org_type="lender"))


def _crit(**kw):
    return {"id": "DSCR_MIN", "description": "DSCR at least 1.25x", "subject": "metric:dscr", "op": ">=",
            "value": "1.25"} | kw


def _version(criteria, verified_at=None, **kw):
    return CriteriaVersionIn(criteria=criteria, source="Fictional sheet", verified_at=verified_at or TODAY,
                             verified_by="Pat Broker", **kw)


def _program(conn, org=None, **kw):
    return L.create_program(conn, ProgramIn(organization_id=org or _lender(conn), name=kw.pop("name", "Term loans"), **kw))


def _deal(conn, state="TX", product="equipment", amount="480000", case_id=None):
    org = crm.create_organization(conn, OrganizationIn(name=f"Borrower {state}{product} (FICTIONAL)",
                                                       org_type="borrower_business", state=state))
    did = crm.create_deal(conn, DealIn(name="Deal", deal_type="small_business", product_type=product,
                                       requested_amount=amount, borrower_organization_id=org))
    if case_id:
        crm.link_underwriting_file(conn, did, case_id)
    return did


# ------------------------------------------------------------------------------ 4.1 programs and versions
def test_program_rules(conn):
    org = _lender(conn)
    borrower = crm.create_organization(conn, OrganizationIn(name="Not A Lender", org_type="borrower_business"))
    with pytest.raises(ValidationFailure, match="not a lender"):
        _program(conn, borrower)
    pid = _program(conn, org, states=["tx"], product_types=["equipment"], min_amount="100000")
    with pytest.raises(ValidationFailure, match="already has"):
        _program(conn, org, name="term LOANS")
    with pytest.raises(ValidationError, match="larger"):
        ProgramIn(organization_id=org, name="x1", min_amount="5", max_amount="1")
    with pytest.raises(ValidationFailure, match="larger"):
        L.update_program(conn, pid, ProgramPatch(max_amount="50000"))
    assert L.update_program(conn, pid, ProgramPatch(states=["OK", "tx"])) == ["states"]
    assert L.get_program(conn, pid)["states"] == ["OK", "TX"]
    assert L.update_program(conn, pid, ProgramPatch(states=["TX", "OK"])) == []


def test_criterion_validation():
    with pytest.raises(ValidationError, match="unknown metric"):
        Criterion(**_crit(subject="metric:made_up"))
    with pytest.raises(ValidationError, match="subject must look like"):
        Criterion(**_crit(subject="dscr"))
    with pytest.raises(ValidationError, match="needs a number"):
        Criterion(**_crit(value="high"))
    with pytest.raises(ValidationError, match="needs a list"):
        Criterion(**_crit(subject="deal:product_type", op="in", value=[]))
    assert Criterion(**_crit(subject="deal:product_type", op="in", value="equipment | term_loan")).value == \
        ["equipment", "term_loan"]
    assert Criterion(**_crit(op="is_known", value="ignored")).value is None
    with pytest.raises(ValidationError, match="duplicate"):
        _version([_crit(), _crit()])
    with pytest.raises(ValidationError, match="future"):
        _version([_crit()], verified_at=TODAY + timedelta(days=1))


def test_versions_are_immutable_and_only_added_on_change(conn):
    pid = _program(conn)
    v1 = L.add_version(conn, pid, _version([_crit()]))
    assert v1 == {"id": v1["id"], "version": 1, "created": True}
    assert not L.add_version(conn, pid, _version([_crit()]))["created"]  # identical → no new version
    v2 = L.add_version(conn, pid, _version([_crit(value="1.30")]))
    assert v2["version"] == 2 and L.current_version(conn, pid)["criteria"][0]["value"] == "1.30"
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE program_criteria_versions SET source='x' WHERE id=?", (v1["id"],))
    with pytest.raises(sqlite3.IntegrityError, match="kept for history"):
        conn.execute("DELETE FROM program_criteria_versions WHERE id=?", (v1["id"],))
    # A future-dated version does not apply yet.
    L.add_version(conn, pid, _version([_crit(value="1.50")], effective_date=TODAY + timedelta(days=30)))
    assert L.current_version(conn, pid)["version"] == 2
    assert L.current_version(conn, pid, TODAY + timedelta(days=30))["version"] == 3
    assert verify_chain(conn)["ok"]


# ------------------------------------------------------------------------------ 4.3 / 4.4 matching
def test_matching_explains_every_check(conn, settings):
    out = advance(conn, settings, "CREDIT_REVIEW")
    did = _deal(conn, case_id=out["case_id"])
    a = _program(conn, _lender(conn, "Alpha (FICTIONAL)"), name="Fits", deal_types=["small_business"],
                 product_types=["equipment"], states=["TX"], min_amount="100000", max_amount="1000000")
    L.add_version(conn, a, _version([_crit(), _crit(id="LEV", description="Leverage at most 3.5x",
                                                    subject="metric:debt_to_ebitda", op="<=", value="3.5"),
                                     _crit(id="SEC", description="Secured", subject="case:is_secured", op="==",
                                           value=True)]))
    b = _program(conn, _lender(conn, "Beta (FICTIONAL)"), name="Too strict")
    L.add_version(conn, b, _version([_crit(value="5.0")]))
    c = _program(conn, _lender(conn, "Gamma (FICTIONAL)"), name="Optional only")
    L.add_version(conn, c, _version([_crit(value="5.0", required=False),
                                     _crit(id="GDSCR", description="Global DSCR", subject="metric:global_dscr",
                                           value="1.0")]))
    d = _program(conn, _lender(conn, "Delta (FICTIONAL)"), name="Elsewhere", states=["CA"])
    res = M.compute_matches(conn, did)
    by = {p["program"]: p for p in res["programs"]}
    assert by["Fits"]["fit"] == "meets_all" and by["Fits"]["counts"] == {"PASS": 8, "FAIL": 0, "UNKNOWN": 0}
    assert by["Too strict"]["fit"] == "not_a_fit" and by["Too strict"]["required_failures"] == 1
    assert by["Optional only"]["fit"] == "meets_all" and by["Optional only"]["counts"]["FAIL"] == 1
    assert by["Elsewhere"]["fit"] == "not_a_fit" and by["Elsewhere"]["no_criteria"]
    dscr = next(x for x in by["Too strict"]["checks"] if x["id"] == "DSCR_MIN")
    assert dscr["result"] == "FAIL" and "NOT met" in dscr["explanation"] and dscr["observed"] and dscr["source"] == "criteria v1"
    order = [p["program"] for p in res["programs"]]
    assert order.index("Fits") < order.index("Too strict") and order.index("Optional only") < order.index("Too strict")
    assert res["run"]["run_number"] and res["summary"] == {"meets_all": 2, "possible": 0, "not_a_fit": 2}
    assert d


def test_unknown_is_never_a_pass(conn):
    did = _deal(conn, state="")  # no underwriting file, no borrower state
    pid = _program(conn, states=["TX"])
    L.add_version(conn, pid, _version([_crit(), _crit(id="NEED", description="Has DSCR", op="is_known", value=None),
                                       _crit(id="NODSCR", description="DSCR unknown", op="is_unknown", value=None)]))
    p = M.compute_matches(conn, did)["programs"][0]
    res = {c["id"]: (c["result"], c["explanation"]) for c in p["checks"]}
    assert res["DSCR_MIN"] == ("UNKNOWN", "no underwriting file linked")
    assert res["NEED"][0] == "UNKNOWN" and res["NODSCR"][0] == "PASS"
    assert res["ELIG-STATE"] == ("UNKNOWN", "borrower organization has no state")
    assert p["fit"] == "possible" and p["counts"]["UNKNOWN"] == 3


def test_freshness_boundary(conn):
    did = _deal(conn)
    org = _lender(conn)
    fresh, old = _program(conn, org, name="Fresh"), _program(conn, org, name="Old")
    L.add_version(conn, fresh, _version([_crit()], verified_at=TODAY - timedelta(days=180)))
    L.add_version(conn, old, _version([_crit()], verified_at=TODAY - timedelta(days=181)))
    by = {p["program"]: p for p in M.compute_matches(conn, did, max_age_days=180)["programs"]}
    assert by["Fresh"]["age_days"] == 180 and not by["Fresh"]["stale"]
    assert by["Old"]["age_days"] == 181 and by["Old"]["stale"]


def test_inactive_programs_left_out_and_match_runs_saved(conn):
    did = _deal(conn)
    org = _lender(conn)
    pid = _program(conn, org)
    L.add_version(conn, pid, _version([_crit()]))
    off = _program(conn, org, name="Paused")
    L.update_program(conn, off, ProgramPatch(active=False))
    mid = M.run_matching(conn, did)
    m = M.get_match_run(conn, did, mid)
    assert m["intact"] and [p["program"] for p in m["results"]["programs"]] == ["Term loans"]
    assert m["results"]["programs"][0]["criteria_version"] == 1
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE match_runs SET results_json='{}' WHERE id=?", (mid,))
    ev = next(e for e in list_events(conn, limit=20) if e["action"] == "lender_matching_run")
    assert '"meets_all"' in ev["details_json"]
    # Criteria change later: the saved matching still cites version 1.
    L.add_version(conn, pid, _version([_crit(value="9")]))
    assert M.get_match_run(conn, did, mid)["results"]["programs"][0]["criteria_version"] == 1


# ------------------------------------------------------------------------------ 4.2 import
def test_yaml_import_preview_commit_idempotent(conn):
    text = SAMPLE.read_text(encoding="utf-8")
    plan = LI.plan_yaml(conn, text)
    assert len(plan) == 3 and all(p.version_action == "new" and not p.errors for p in plan)
    assert LI.commit_yaml(conn, text) == {"lenders_created": 2, "programs_created": 3, "programs_updated": 0,
                                          "versions_added": 3}
    assert {p.version_action for p in LI.plan_yaml(conn, text)} == {"unchanged"}
    assert LI.commit_yaml(conn, text)["versions_added"] == 0
    changed = text.replace("value: 1.25}", "value: 1.30}")
    assert LI.commit_yaml(conn, changed)["versions_added"] == 1


def test_yaml_import_errors_import_nothing(conn):
    bad = SAMPLE.read_text(encoding="utf-8").replace('subject: "metric:dscr_property"', 'subject: "metric:nope"')
    plan = LI.plan_yaml(conn, bad)
    assert any("unknown metric" in e for p in plan for e in p.errors)
    with pytest.raises(ValidationFailure, match="nothing was imported"):
        LI.commit_yaml(conn, bad)
    assert L.list_programs(conn) == []
    with pytest.raises(ValidationFailure, match="lenders:"):
        LI.plan_yaml(conn, "programs: []")
    with pytest.raises(ValidationFailure, match="YAML"):
        LI.plan_yaml(conn, "lenders: [")


def test_criteria_csv():
    ok, errors = LI.parse_criteria_csv(
        "id,description,subject,op,value,required\n"
        "DSCR,DSCR min,metric:dscr,>=,1.25,yes\n"
        "PROD,Products,deal:product_type,in,equipment|term_loan,\n"
        "PREF,Preferred,metric:global_dscr,>=,1.1,no\n"
        "BAD,Bad,metric:zzz,>=,1,\n")
    assert [c.id for c in ok] == ["DSCR", "PROD", "PREF"] and not ok[2].required and ok[1].required
    assert len(errors) == 1 and "line 5" in errors[0]


# ------------------------------------------------------------------------------ migration
def test_upgrade_from_schema9(tmp_path, settings):
    from uwhelper.bootstrap import initialise
    from uwhelper.db.connection import Database

    old = tmp_path / "m9"
    old.mkdir()
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(f.name[:4]) <= 9:
            (old / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    settings.ensure_dirs()
    with Database(settings.db_path).session() as c:
        apply_migrations(c, old)
        c.execute("INSERT INTO organizations(id, name, org_type, created_at, updated_at) "
                  "VALUES ('o1','Old Lender','lender','t','t')")
    db, _ = initialise(settings)
    with db.session() as c:
        assert current_version(c) == max(m.version for m in discover())
        assert c.execute("SELECT name FROM organizations WHERE id='o1'").fetchone()[0] == "Old Lender"
        pid = L.create_program(c, ProgramIn(organization_id="o1", name="New program"))
        assert L.get_program(c, pid)["lender_name"] == "Old Lender"


# ------------------------------------------------------------------------------ web + API
@pytest.fixture
def web(settings):
    from fastapi.testclient import TestClient

    from uwhelper.web.app import create_app

    app = create_app(settings)
    client = TestClient(app)
    return app, client, client.get("/api/v1/session").json()["csrf_token"]


def test_lender_pages_and_forms(web, settings):
    import re

    app, c, token = web
    assert "No lender programs yet" in c.get("/lenders").text
    r = c.post(f"/lenders/import/preview?csrf_token={token}",
               files={"file": ("cat.yaml", SAMPLE.read_bytes(), "text/yaml")})
    assert r.status_code == 200 and r.text.count("new version") == 3
    r = c.post("/lenders/import/commit", data={"csrf_token": token, "yaml_text": SAMPLE.read_text()},
               follow_redirects=False)
    assert "3+new" in r.headers["location"]
    page = c.get("/lenders").text
    assert "Equipment term loans" in page and "STALE" in page  # the CRE bridge sheet is from March 2026
    with app.state.db.session() as conn:
        org = _lender(conn, "Form Bank (FICTIONAL)")
        out = advance(conn, settings, "CREDIT_REVIEW")
        did = _deal(conn, case_id=out["case_id"])
    r = c.post("/lenders/programs", data={"csrf_token": token, "organization_id": org, "name": "Form program",
                                          "deal_types": "small_business", "product_types": ["equipment", "term_loan"],
                                          "states": "tx, ok", "min_amount": "50,000"}, follow_redirects=False)
    purl = r.headers["location"].split("?")[0]
    page = c.get(purl).text
    assert "No criteria in effect" in page and "OK, TX" in page
    crit_yaml = "- {id: DSCR_MIN, description: DSCR at least 1.2x, subject: 'metric:dscr', op: '>=', value: 1.2}\n"
    r = c.post(f"{purl}/version", data={"csrf_token": token, "criteria_yaml": crit_yaml, "source": "Call with lender",
                                        "verified_at": TODAY.isoformat(), "verified_by": "Pat Broker"},
               follow_redirects=False)
    assert "version+1+saved" in r.headers["location"]
    r = c.post(f"{purl}/version", data={"csrf_token": token, "criteria_yaml": crit_yaml.replace("dscr'", "zzz'"),
                                        "source": "x" * 5, "verified_at": TODAY.isoformat(), "verified_by": "Pat"})
    assert r.status_code == 400 and "unknown metric" in r.text
    csv_text = "id,description,subject,op,value\nDSCR_MIN,DSCR,metric:dscr,>=,1.3\n"
    r = c.post(f"{purl}/version-csv?csrf_token={token}", data={"source": "Updated sheet", "verified_at": TODAY.isoformat(),
                                                               "verified_by": "Pat Broker"},
               files={"file": ("c.csv", csv_text.encode(), "text/csv")}, follow_redirects=False)
    assert "version+2+saved" in r.headers["location"]
    assert "Form program" in c.get(f"/organizations/{org}").text
    r = c.post(f"/deals/{did}/matches/run", data={"csrf_token": token}, follow_redirects=False)
    mpage = c.get(r.headers["location"].split("?")[0]).text
    assert "Form program" in mpage and "criteria v2" in mpage and re.search(r"based on screening run <b>#\d+", mpage)
    assert "Lender matching" in c.get(f"/deals/{did}").text


def test_lender_api(web, settings):
    app, c, token = web
    h = {"X-CSRF-Token": token}
    r = c.post("/api/v1/lender-catalog/import", headers=h, files={"file": ("c.yaml", SAMPLE.read_bytes(), "text/yaml")})
    assert r.json()["dry_run"] and len(r.json()["programs"]) == 3
    r = c.post("/api/v1/lender-catalog/import?dry_run=false", headers=h,
               files={"file": ("c.yaml", SAMPLE.read_bytes(), "text/yaml")})
    assert r.status_code == 201 and r.json()["programs_created"] == 3
    items = c.get("/api/v1/lender-programs").json()["items"]
    pid = items[0]["id"]
    assert c.get(f"/api/v1/lender-programs/{pid}").json()["current"]["version"] == 1
    assert c.patch(f"/api/v1/lender-programs/{pid}", headers=h, json={"active": False}).json()["changed"] == ["active"]
    r = c.post(f"/api/v1/lender-programs/{pid}/versions", headers=h,
               json={"criteria": [_crit()], "source": "API", "verified_at": TODAY.isoformat(), "verified_by": "Pat"})
    assert r.status_code == 201 and r.json()["version"] == 2
    r = c.post(f"/api/v1/lender-programs/{pid}/versions", headers=h,
               json={"criteria": [_crit(subject="metric:nope")], "source": "API", "verified_at": TODAY.isoformat(),
                     "verified_by": "Pat"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    org = c.post("/api/v1/organizations", headers=h, json={"name": "API Lender (FICTIONAL)", "org_type": "lender"}).json()
    r = c.post("/api/v1/lender-programs", headers=h, json={"organization_id": org["id"], "name": "API program"})
    assert r.status_code == 201
    with app.state.db.session() as conn:
        did = _deal(conn)
    m = c.post(f"/api/v1/deals/{did}/matches", headers=h)
    assert m.status_code == 201 and m.json()["intact"]
    mid = c.get(f"/api/v1/deals/{did}/matches").json()["items"][0]["id"]
    assert c.get(f"/api/v1/deals/{did}/matches/{mid}").json()["results"]["deal_id"] == did
    assert "/api/v1/lender-programs" in c.get("/api/v1/openapi.json").json()["paths"]
