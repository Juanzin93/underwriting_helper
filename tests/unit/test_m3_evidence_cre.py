"""Milestone 3: extraction provenance, evidence trails, run reproduction, CRE rent roll / T-12 / metrics,
policy versions and diff, deal memos. Temporary databases only; no network; the local model is faked."""

import json
import sqlite3
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest

from tests.lifecycle_helpers import advance
from tests.unit.test_llm import MODEL, FakeOllama, client, fact_json, pdf
from uwhelper.analysis import evidence as EV
from uwhelper.analysis.reproduce import verify_run
from uwhelper.analysis.resolver import find_contradictions
from uwhelper.analysis.underwriting import get_run, run_underwriting
from uwhelper.cre import rent_roll as RR
from uwhelper.cre import t12 as T12
from uwhelper.crm import service as crm
from uwhelper.crm.models import DealIn, OrganizationIn
from uwhelper.db.migrate import (
    MIGRATIONS_DIR,
    apply_migrations,
    current_version,
    discover,
)
from uwhelper.documents.service import import_document
from uwhelper.domain.models import BorrowerIn, CaseIn, FactIn, TermsIn
from uwhelper.llm.extraction import (
    extract_document_facts,
    extraction_provenance,
    prompt_fingerprint,
)
from uwhelper.metrics.finance import annual_debt_service
from uwhelper.policy.diff import diff_policies
from uwhelper.policy.loader import policy_from_row, resolve_policy
from uwhelper.repo import cases
from uwhelper.repo.audit import verify_chain
from uwhelper.repo.cases import ValidationFailure
from uwhelper.repo.facts import add_user_fact
from uwhelper.reports import deal_memo

R6 = Decimal("0.000001")


def q6(d: Decimal) -> Decimal:
    return d.quantize(R6, rounding=ROUND_HALF_UP)


def q2(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ------------------------------------------------------------------------------ 3.1 extraction provenance
def test_extraction_run_records_full_provenance_and_is_repeatable(conn, settings):
    import io

    from tests.conftest import make_case

    cid = make_case(conn)
    data = pdf(["Income Statement FY2025 (in thousands)", "Revenue 2,400", "Net income (230)"])
    did = import_document(conn, settings, cid, io.BytesIO(data), "is.pdf", "income_statement").document_id
    runs = []
    for _ in range(2):
        fake = FakeOllama([json.dumps({"facts": [fact_json()]})])
        runs.append(extract_document_facts(conn, client(fake), cid, did).run_id)
    p1, p2 = (extraction_provenance(conn, r) for r in runs)
    doc_sha = conn.execute("SELECT sha256 FROM documents WHERE id=?", (did,)).fetchone()[0]
    for p in (p1, p2):
        assert p["prompt_sha256"] == prompt_fingerprint() and p["prompt_matches_current"]
        assert p["document_sha256"] == doc_sha and p["document_unchanged"]
        assert p["pages"] == [1] and p["ollama_version"] == "0.12.0" and p["model_name"] == MODEL
        assert p["options"]["temperature"] == 0 and p["model_digest"] == "abc123def456"
    keys = ("prompt_sha256", "document_sha256", "pages_json", "options_json", "model_name", "model_digest", "prompt_version")
    assert {k: p1[k] for k in keys} == {k: p2[k] for k in keys}  # same inputs → identical provenance


def test_prompt_fingerprint_changes_with_field_catalog():
    from uwhelper.llm.extraction import extractable_fields

    fields = extractable_fields()
    smaller = {k: v for k, v in fields.items() if k != "revenue"}
    assert prompt_fingerprint(fields) != prompt_fingerprint(smaller)


# ------------------------------------------------------------------------------ 3.2 evidence trails
def test_every_metric_of_sample_run_is_traced(conn, settings):
    out = advance(conn, settings, "CREDIT_REVIEW")
    rep = EV.run_traceability(conn, out["case_id"], out["run_id"])
    assert rep["computed"] > 20 and rep["traced"] == rep["computed"] and not rep["untraced"]
    trail = EV.metric_trail(conn, out["case_id"], out["run_id"], "revenue")
    fact_inputs = [i for i in trail["inputs"] if i["kind"] == "fact"]
    assert fact_inputs and fact_inputs[0]["facts"][0]["events"]
    assert fact_inputs[0]["facts"][0]["verified_by"]
    dscr = EV.metric_trail(conn, out["case_id"], out["run_id"], "dscr")
    assert {i["kind"] for i in dscr["inputs"]} == {"derived"} and dscr["traced"]


def test_untraced_input_is_reported():
    metrics = {"x": {"metric_key": "x", "label": "X", "status": "computed", "flags": [],
                     "inputs": [{"name": "mystery", "label": "Mystery", "state": "ok", "flags": [], "fact_ids": [],
                                 "sources": []}]}}
    assert EV.classify(metrics["x"]["inputs"][0], metrics) == "untraced"
    ok, problems = EV._traced("x", metrics, set())
    assert not ok and "no recorded source" in problems[0]
    assert EV.classify({"name": "requested_amount", "state": "ok", "flags": ["proposed_terms"]}, metrics) == "assumption"


# ------------------------------------------------------------------------------ 3.5 reproduction
def test_saved_run_reproduces_exactly(conn, settings):
    out = advance(conn, settings, "CREDIT_REVIEW")
    r = verify_run(conn, out["case_id"], out["run_id"])
    assert r["reproduced"] and r["rules_checked"] and r["snapshot_intact"] and r["metrics_checked"] > 30


def test_reproduction_detects_changed_calculation(conn, settings, monkeypatch):
    from uwhelper.modules.commercial import CommercialModule

    out = advance(conn, settings, "CREDIT_REVIEW")
    original = CommercialModule.compute_metrics

    def altered(self, inputs, params):
        res = original(self, inputs, params)
        res["dscr"].value = (res["dscr"].value or Decimal(0)) + Decimal("0.5")
        return res

    monkeypatch.setattr(CommercialModule, "compute_metrics", altered)
    r = verify_run(conn, out["case_id"], out["run_id"])
    assert not r["reproduced"] and [d["metric"] for d in r["metric_differences"]] == ["dscr"]


def test_old_run_without_context_checks_metrics_only(conn, settings, monkeypatch):
    import uwhelper.analysis.underwriting as U

    monkeypatch.setattr(U, "evaluation_context", lambda r: None)
    out = advance(conn, settings, "CREDIT_REVIEW")
    r = verify_run(conn, out["case_id"], out["run_id"])
    assert r["reproduced"] and not r["rules_checked"]


# ------------------------------------------------------------------------------ 3.3 / 3.4 CRE
RENT_ROLL = (
    "Unit,Tenant,SqFt,Status,Monthly Rent,Market Rent,Lease Start,Lease End\n"
    "101,Alpha LLC,1000,occupied,\"10,000\",10500,2024-01-01,2027-03-31\n"
    "102,Beta Inc,1500,occupied,15000,15000,06/01/2023,05/31/2029\n"
    "103,Gamma Co,800,occupied,8000,8000,2025-01-01,MTM\n"
    "104,,900,vacant,,9000,,\n"
)
AS_OF = date(2026, 9, 30)


def _cre_case(conn, version="1.2.0", **terms):
    t = {"requested_amount": "6000000", "interest_rate": "7", "amortization_months": 300, "term_months": 120,
         "project_total_cost": "8000000"} | terms
    cid = cases.create_case(conn, CaseIn(title="CRE", loan_type="commercial_real_estate", policy_key="demo_cre",
                                         policy_version=version),
                            BorrowerIn(legal_name="Harbor Plaza LLC (FICTIONAL)"), TermsIn(**t))
    for key, val in (("gross_potential_rent", "1200000"), ("vacancy_credit_loss", "60000"),
                     ("other_property_income", "40000"), ("property_operating_expenses", "480000")):
        add_user_fact(conn, cid, FactIn(field_key=key, value=val, period_end="2025-12-31", period_months=12,
                                        mark_verified=True))
    return cid


def _verified_roll(conn, cid, as_of=AS_OF):
    rid = RR.create_draft(conn, cid, RENT_ROLL, as_of=as_of, stated_annual_rent=Decimal("396000"), stated_unit_count=4)
    RR.verify(conn, cid, rid, "Pat Analyst")
    return rid


def test_rent_roll_parse_reconcile_verify(conn):
    cid = _cre_case(conn)
    p = RR.parse_rent_roll(RENT_ROLL)
    assert p.rent_basis == "monthly ×12" and not p.invalid and p.annual_rent == Decimal("396000")
    mtm = next(u for u in p.units if u.unit == "103")
    assert mtm.month_to_month and mtm.lease_end is None
    assert next(u for u in p.units if u.unit == "102").lease_end == date(2029, 5, 31)
    rid = RR.create_draft(conn, cid, RENT_ROLL, as_of=AS_OF, stated_annual_rent=Decimal("400000"))
    assert not RR.get_rent_roll(conn, cid, rid)["reconciled"]
    with pytest.raises(ValidationFailure, match="match within"):
        RR.verify(conn, cid, rid, "Pat")
    assert RR.set_stated_totals(conn, cid, rid, Decimal("396000.50"), 4)
    RR.verify(conn, cid, rid, "Pat Analyst")
    roll = RR.get_rent_roll(conn, cid, rid)
    assert roll["status"] == "verified" and roll["verified_by"] == "Pat Analyst"
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE rent_rolls SET computed_annual_rent='1' WHERE id=?", (rid,))
    with pytest.raises(sqlite3.IntegrityError, match="no longer a draft"):
        conn.execute("INSERT INTO rent_roll_units(rent_roll_id, line_number, unit, occupied) VALUES (?,99,'x',0)", (rid,))
    newer = _verified_roll(conn, cid, date(2026, 10, 15))
    assert RR.get_rent_roll(conn, cid, rid)["status"] == "superseded"
    assert RR.roll_for_run(conn, cid, "verified_only")["id"] == newer
    assert verify_chain(conn)["ok"]


def test_rent_roll_invalid_rows(conn):
    cid = _cre_case(conn)
    bad = "unit,monthly_rent,status,lease_end\nA,1000,occupied,2026-13-01\nA,500,occupied,\nB,,occupied,\n"
    p = RR.parse_rent_roll(bad)
    assert len(p.invalid) == 3
    errs = " ".join(e for u in p.invalid for e in u.errors)
    assert "lease end" in errs and "twice" in errs and "no rent" in errs
    with pytest.raises(ValidationFailure, match="invalid"):
        RR.create_draft(conn, cid, bad, as_of=AS_OF)
    with pytest.raises(ValidationFailure, match="needs a 'unit'"):
        RR.parse_rent_roll("tenant,rent\nx,1\n")


def test_cre_metrics_hand_worked(conn):
    cid = _cre_case(conn)
    _verified_roll(conn, cid)
    run = get_run(conn, run_underwriting(conn, cid).run_id)
    m = {k: Decimal(v["value"]) if v["value"] is not None else None for k, v in run["metrics_by_key"].items()}
    noi = Decimal(1200000) - Decimal(60000) + Decimal(40000) - Decimal(480000)
    ads = q2(annual_debt_service(Decimal(6000000), Decimal("0.07"), 300, "amortizing"))
    ads_stress = annual_debt_service(Decimal(6000000), Decimal("0.09"), 300, "amortizing")
    assert m["noi"] == noi
    assert m["dscr_property_stressed"] == q6(noi / ads_stress)
    assert m["breakeven_occupancy"] == q6((Decimal(480000) + ads) / Decimal(1240000))
    value = q2(noi / Decimal("0.075"))
    assert m["income_approach_value"] == value and m["ltv_income_value"] == q6(Decimal(6000000) / value)
    in_place = Decimal(396000)
    assert m["occupancy_physical"] == q6(Decimal(3) / Decimal(4))
    assert m["occupancy_economic"] == q6(in_place / (in_place + Decimal(108000)))
    assert m["rollover_12m"] == q6(Decimal(216000) / in_place) == m["rollover_24m"]
    walt = (Decimal(120000) * Decimal((date(2027, 3, 31) - AS_OF).days) / 365
            + Decimal(180000) * Decimal((date(2029, 5, 31) - AS_OF).days) / 365) / in_place
    assert m["walt_years"] == q6(walt)
    rules = {e["rule_id"]: e["result"] for e in run["evaluations"]}
    assert rules["PHYSICAL_OCCUPANCY_MIN"] == "FAIL" and rules["ROLLOVER_12M_MAX"] == "FAIL"
    assert run["policy_version"] == "1.2.0"
    assert verify_run(conn, cid, run["id"])["reproduced"]
    trail = EV.metric_trail(conn, cid, run["id"], "occupancy_physical")
    assert {i["kind"] for i in trail["inputs"]} == {"record"} and trail["traced"]


def test_verified_only_ignores_draft_roll_and_flags_stale(conn):
    cid = _cre_case(conn)
    RR.create_draft(conn, cid, RENT_ROLL, as_of=AS_OF, stated_annual_rent=Decimal("396000"))
    run = get_run(conn, run_underwriting(conn, cid).run_id)
    assert run["metrics_by_key"]["occupancy_physical"]["status"] == "not_computable"
    prev = get_run(conn, run_underwriting(conn, cid, mode="include_unverified").run_id)
    occ = prev["metrics_by_key"]["occupancy_physical"]
    assert occ["status"] == "computed" and "unverified" in occ["flags"]
    cid2 = _cre_case(conn)
    _verified_roll(conn, cid2, date(2025, 1, 31))
    stale = get_run(conn, run_underwriting(conn, cid2).run_id)
    assert "stale" in stale["metrics_by_key"]["occupancy_physical"]["flags"]
    assert any(f["field_key"] == "rent_roll" and f["finding_type"] == "stale" for f in stale["findings"])


def test_non_cre_runs_unchanged_by_cre_metrics(conn, settings):
    out = advance(conn, settings, "CREDIT_REVIEW")
    run = get_run(conn, out["run_id"])
    assert all(run["metrics_by_key"][k]["status"] == "not_applicable" for k in ("occupancy_physical", "walt_years"))
    assert "rent_roll" not in run["inputs"]


T12_CSV = (
    "Description,Jan,Feb,Mar,Apr,May,Jun,Jul,Aug,Sep,Oct,Nov,Dec\n"
    "INCOME,,,,,,,,,,,,\n"
    "Base rent," + ",".join(["100000"] * 12) + "\n"
    "Vacancy loss," + ",".join(["-5000"] * 12) + "\n"
    "Parking income," + ",".join(["2000"] * 12) + "\n"
    "Late fees," + ",".join(["1333.33"] * 11) + ",1333.37\n"
    "Total income," + ",".join(["98333.33"] * 11) + ",98333.37\n"
    "OPERATING EXPENSES,,,,,,,,,,,,\n"
    "Real estate taxes," + ",".join(["15000"] * 12) + "\n"
    "Insurance," + ",".join(["5000"] * 12) + "\n"
    "Management fees," + ",".join(["4000"] * 12) + "\n"
    "Repairs & maintenance," + ",".join(["16000"] * 12) + "\n"
    "Total operating expenses," + ",".join(["40000"] * 12) + "\n"
    "Net operating income," + ",".join(["58333.33"] * 11) + ",58333.37\n"
    "Mortgage interest," + ",".join(["20000"] * 12) + "\n"
    "Capital reserve," + ",".join(["1000"] * 12) + "\n"
)


def test_t12_mapping_reconcile_and_verified_facts(conn):
    cid = _cre_case(conn)
    st = T12.parse_statement(T12_CSV)
    sugg = {ln.label: ln.suggested for ln in st.lines}
    assert sugg["Base rent"] == "gross_potential_rent" and sugg["Vacancy loss"] == "vacancy_credit_loss"
    assert sugg["Parking income"] == "other_property_income" and sugg["Late fees"] == "other_property_income"
    assert sugg["Management fees"] == "property_operating_expenses"
    assert sugg["Total income"] == "ignore" and sugg["Mortgage interest"] == "ignore" and sugg["Capital reserve"] == "ignore"
    assert sugg["Net operating income"] == "net_operating_income_reported"
    t = T12.totals(st.lines, {})
    assert t["effective_gross_income"] == Decimal("1180000.00") and t["property_operating_expenses"] == Decimal(480000)
    with pytest.raises(ValidationFailure, match="do not reconcile"):
        T12.commit_statement(conn, cid, T12_CSV, period_end=date(2026, 6, 30), stated_income=Decimal("1100000"),
                             stated_expenses=Decimal("480000"), confirm_verified=True, confirmed_by="Pat")
    res = T12.commit_statement(conn, cid, T12_CSV, period_end=date(2026, 6, 30), stated_income=Decimal("1180000"),
                               stated_expenses=Decimal("480000"), confirm_verified=True, confirmed_by="Pat Analyst")
    assert res["verified"] and res["reconciled"] and len(res["fact_ids"]) == 5
    facts = {r["field_key"]: dict(r) for r in conn.execute(
        "SELECT * FROM facts WHERE id IN (%s)" % ",".join("?" * len(res["fact_ids"])), res["fact_ids"])}
    assert facts["gross_potential_rent"]["value_numeric"] == "1200000" and facts["gross_potential_rent"]["status"] == "verified"
    assert facts["vacancy_credit_loss"]["value_numeric"] == "60000"
    assert facts["net_operating_income_reported"]["status"] == "candidate"  # informational, never auto-verified
    imp = T12.get_import(conn, cid, res["import_id"])
    assert imp["verified"] and len(imp["lines"]) == 13
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE property_statement_imports SET verified=0 WHERE id=?", (res["import_id"],))


def test_t12_mapping_override_and_unverified_save_and_contradiction(conn):
    cid = _cre_case(conn)  # already has FY2025 facts
    st = T12.parse_statement(T12_CSV)
    late = next(ln.line for ln in st.lines if ln.label == "Late fees")
    res = T12.commit_statement(conn, cid, T12_CSV, period_end=date(2025, 12, 31), mapping={late: "ignore"})
    assert not res["verified"] and not res["reconciled"]
    assert res["totals"]["other_property_income"] == Decimal(24000)
    keys = {c["field_key"] for c in find_contradictions(conn, cid)}
    assert "other_property_income" in keys  # 24,000 vs the existing 40,000 — never silently overwritten


# ------------------------------------------------------------------------------ 3.5 policy versions
def test_policy_versions_effective_dates_and_diff(conn):
    rows = {(r["policy_key"], r["version"]): r for r in conn.execute("SELECT * FROM policies")}
    assert ("demo_cre", "1.2.0") in rows
    assert resolve_policy(conn, "demo_cre", as_of=date(2026, 10, 29))[1].version == "1.1.0"
    assert resolve_policy(conn, "demo_cre", as_of=date(2026, 11, 1))[1].version == "1.2.0"
    d = diff_policies(policy_from_row(rows[("demo_cre", "1.1.0")]), policy_from_row(rows[("demo_cre", "1.2.0")]))
    assert {r["id"] for r in d["rules_added"]} == {"PROPERTY_DSCR_STRESSED_MIN", "BREAKEVEN_OCCUPANCY_MAX",
                                                   "PHYSICAL_OCCUPANCY_MIN", "ROLLOVER_12M_MAX"}
    assert {c["field"] for c in d["parameters"]} == {"cre_cap_rate", "cre_stress_rate_bps"}
    assert not d["rules_removed"] and not d["rules_changed"]
    assert {c["field"] for c in d["header"]} == {"version", "effective_date"}


# ------------------------------------------------------------------------------ 3.6 deal memo
def _deal_with_run(conn, settings):
    out = advance(conn, settings, "CREDIT_REVIEW")
    org = crm.create_organization(conn, OrganizationIn(name="Memo Co (FICTIONAL)", org_type="borrower_business"))
    did = crm.create_deal(conn, DealIn(name="Memo deal", deal_type="small_business", product_type="equipment",
                                       requested_amount="480000", borrower_organization_id=org))
    crm.link_underwriting_file(conn, did, out["case_id"])
    from uwhelper.crm import requests as docreq

    docreq.generate_requests(conn, did)
    return did, out


def test_deal_memo_cites_run_and_checklist_and_reproduces(conn, settings):
    did, out = _deal_with_run(conn, settings)
    m1 = deal_memo.generate(conn, did)
    memo = deal_memo.get_memo(conn, did, m1["id"])
    ctx = json.loads(memo["context_json"])
    run_no = conn.execute("SELECT run_number FROM underwriting_runs WHERE id=?", (out["run_id"],)).fetchone()[0]
    assert ctx["underwriting"]["run"]["run_number"] == run_no and memo["run_id"] == out["run_id"]
    assert ctx["checklist"]["versions"] and ctx["checklist"]["versions"][0].startswith("deal_documents ")
    assert "NOT A CREDIT APPROVAL" in memo["html"] and f"#{run_no}" in memo["html"]
    v = deal_memo.verify(conn, did, m1["id"])
    assert v["context_intact"] and v["html_reproduced"] and v["stored_html_intact"]
    assert deal_memo.generate(conn, did)["version"] == 2
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE deal_memos SET html='x' WHERE id=?", (m1["id"],))
    assert verify_chain(conn)["ok"]


def test_deal_memo_without_file(conn):
    org = crm.create_organization(conn, OrganizationIn(name="Solo Co (FICTIONAL)", org_type="borrower_business"))
    did = crm.create_deal(conn, DealIn(name="No file", deal_type="cre", product_type="bridge",
                                       borrower_organization_id=org))
    m = deal_memo.generate(conn, did)
    assert "No underwriting file is linked" in deal_memo.get_memo(conn, did, m["id"])["html"]


# ------------------------------------------------------------------------------ migration 0009 upgrade
def test_upgrade_from_schema8_keeps_extraction_runs(tmp_path, settings):
    from uwhelper.bootstrap import initialise
    from uwhelper.db.connection import Database

    old = tmp_path / "m8"
    old.mkdir()
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(f.name[:4]) <= 8:
            (old / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    settings.ensure_dirs()
    db = Database(settings.db_path)
    now = "2026-01-01T00:00:00+00:00"
    with db.session() as c:
        apply_migrations(c, old)
        c.execute("INSERT INTO loan_cases(id, case_number, title, loan_type, stage, stage_entered_at, created_at, "
                  "updated_at) VALUES ('c1','UW-1','t','equipment','INTAKE',?,?,?)", (now, now, now))
        c.execute("INSERT INTO documents(id, sha256, original_filename, stored_name, file_ext, size_bytes, imported_at) "
                  "VALUES ('d1','abc','a.pdf','abc.pdf','.pdf',1,?)", (now,))
        c.execute("INSERT INTO extraction_runs(id, case_id, document_id, model_name, prompt_version, status, started_at) "
                  "VALUES ('e1','c1','d1','m','extract-v1.2','completed',?)", (now,))
    db2, _ = initialise(settings)
    with db2.session() as c:
        assert current_version(c) == max(m.version for m in discover())
        row = c.execute("SELECT * FROM extraction_runs WHERE id='e1'").fetchone()
        assert row["model_name"] == "m" and row["prompt_sha256"] is None
        assert extraction_provenance(c, "e1")["prompt_matches_current"] is None


# ------------------------------------------------------------------------------ web + API
@pytest.fixture
def web(settings):
    from fastapi.testclient import TestClient

    from uwhelper.web.app import create_app

    app = create_app(settings)
    client_ = TestClient(app)
    return app, client_, client_.get("/api/v1/session").json()["csrf_token"]


def test_property_pages_rent_roll_and_t12_through_forms(web):
    import re

    app, c, token = web
    with app.state.db.session() as conn:
        cid = _cre_case(conn)
        other = cases.create_case(conn, CaseIn(title="Biz", loan_type="equipment"), BorrowerIn(legal_name="Biz Co"))
    assert "Property (rent roll" in c.get(f"/cases/{cid}").text
    assert c.get(f"/cases/{other}/property").status_code == 400
    assert c.get(f"/cases/{cid}/property").status_code == 200
    r = c.post(f"/cases/{cid}/property/rent-roll/preview?csrf_token={token}",
               data={"as_of": "2026-09-30", "stated_annual_rent": "396,000", "stated_unit_count": "4"},
               files={"file": ("rr.csv", RENT_ROLL.encode(), "text/csv")})
    assert r.status_code == 200 and "matches the printed total" in r.text
    r = c.post(f"/cases/{cid}/property/rent-roll", data={"csrf_token": token, "csv_text": RENT_ROLL,
                                                         "as_of": "2026-09-30", "stated_annual_rent": "396000",
                                                         "stated_unit_count": "4", "filename": "rr.csv"},
               follow_redirects=False)
    roll_url = r.headers["location"].split("?")[0]
    assert "RECONCILED" in c.get(roll_url).text
    c.post(f"{roll_url}/verify", data={"csrf_token": token, "verified_by": "Pat Analyst"})
    assert "verified by Pat Analyst" in c.get(roll_url).text
    r = c.post(f"/cases/{cid}/property/t12/preview?csrf_token={token}",
               data={"period_end": "2026-06-30", "period_months": "12", "stated_income": "1180000",
                     "stated_expenses": "480000"}, files={"file": ("t12.csv", T12_CSV.encode(), "text/csv")})
    assert r.status_code == 200 and r.text.count("matches") >= 2
    maps = dict(re.findall(r'name="(map_\d+)"[^>]*>(?:.|\n)*?<option value="([a-z_]+)" selected', r.text))
    form = {"csrf_token": token, "csv_text": T12_CSV, "period_end": "2026-06-30", "period_months": "12",
            "stated_income": "1180000", "stated_expenses": "480000", "confirm_verified": "on",
            "confirmed_by": "Pat Analyst", **maps}
    r = c.post(f"/cases/{cid}/property/t12", data=form, follow_redirects=False)
    assert r.status_code == 303 and "VERIFIED" in r.headers["location"]


def test_evidence_policy_diff_and_deal_memo_pages(web, settings):
    app, c, token = web
    with app.state.db.session() as conn:
        did, out = _deal_with_run(conn, settings)
        ids = {(r["policy_key"], r["version"]): r["id"] for r in conn.execute("SELECT * FROM policies")}
    cid, rid = out["case_id"], out["run_id"]
    assert "Evidence trail" in c.get(f"/cases/{cid}/metrics?run_id={rid}").text
    page = c.get(f"/cases/{cid}/runs/{rid}/evidence").text
    assert "REPRODUCED" in page and "fully traced" in page
    assert "FULLY TRACED" in c.get(f"/cases/{cid}/runs/{rid}/metrics/dscr").text
    assert c.get(f"/cases/{cid}/runs/{rid}/metrics/nope").status_code == 404
    r = c.get(f"/policies/diff?a={ids[('demo_cre', '1.1.0')]}&b={ids[('demo_cre', '1.2.0')]}")
    assert "PHYSICAL_OCCUPANCY_MIN" in r.text and "ADDED" in r.text
    c.post(f"/deals/{did}/memo", data={"csrf_token": token})
    mid = c.get(f"/api/v1/deals/{did}/memos").json()["items"][0]["id"]
    r = c.get(f"/deals/{did}/memos/{mid}/html")
    assert r.status_code == 200 and "default-src 'none'" in r.headers["content-security-policy"]
    assert c.get(f"/deals/{did}/memos/{mid}/pdf").content[:4] == b"%PDF"
    r = c.get(f"/deals/{did}/memos/{mid}/verify", follow_redirects=False)
    assert "re-rendering+matches+exactly" in r.headers["location"]
    h = {"X-CSRF-Token": token}
    assert c.post(f"/api/v1/deals/{did}/memos", headers=h).json()["version"] == 2
    assert c.get(f"/api/v1/deals/{did}/memos/{mid}/verify").json()["html_reproduced"]
    assert c.get(f"/api/v1/underwriting-files/{cid}/runs/{rid}/verify").json()["reproduced"]
    tr = c.get(f"/api/v1/underwriting-files/{cid}/runs/{rid}/traceability").json()
    assert tr["traced"] == tr["computed"]


def test_policy_schema_change_keeps_existing_hashes():
    """Adding optional parameters must not change the canonical content (and hash) of earlier policy versions."""
    import yaml

    from uwhelper.config import PROJECT_ROOT
    from uwhelper.policy.loader import canonical_json
    from uwhelper.policy.schema import Policy

    pdir = PROJECT_ROOT / "policies"
    for name in ("demo_cre_v1.1.0.yaml", "demo_commercial_general_v1.1.0.yaml", "demo_cre_v1.0.0.yaml"):
        text = canonical_json(Policy.model_validate(yaml.safe_load((pdir / name).read_text(encoding="utf-8"))))
        assert "cre_cap_rate" not in text and "cre_stress_rate_bps" not in text
    new = yaml.safe_load((pdir / "demo_cre_v1.2.0.yaml").read_text(encoding="utf-8"))
    assert Policy.model_validate(new).parameters.model_dump()["cre_cap_rate"] == 0.075
    new["parameters"]["cre_cap_rate"] = 1.5
    with pytest.raises(ValueError, match="cre_cap_rate"):
        Policy.model_validate(new)
