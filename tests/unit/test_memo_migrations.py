import json
import sqlite3

import pytest

from tests.lifecycle_helpers import advance, clean_sample
from uwhelper.analysis.underwriting import run_underwriting
from uwhelper.db.connection import Database
from uwhelper.db.migrate import apply_migrations
from uwhelper.reports.memo import build_memo_context, generate_memo, list_memos, render_memo, verify_reproduction
from uwhelper.repo.audit import backfill_chain, verify_chain
from uwhelper.sample_data import load_sample_case


def test_memo_with_missing_information(conn, settings):
    cid = load_sample_case(conn, settings)  # contradiction + unverified items remain
    run = run_underwriting(conn, cid)
    ctx = build_memo_context(conn, cid, run.run_id, 1)
    html = render_memo(ctx)
    assert ctx["opinion_code"] == "INSUFFICIENT_INFORMATION"
    assert "NOT A CREDIT DECISION OR APPROVAL" in html and "approved" not in ctx["opinion_text"].lower()
    assert ctx["contradictions"] and ctx["unverified"] and ctx["questions"]
    assert "No industry or market evidence is recorded" in html
    assert "No structure has been marked" in html
    assert "[SUGGESTION]" in html and ctx["suggested_conditions"]


def test_memo_traceability_and_labels(conn, settings):
    out = advance(conn, settings, "CREDIT_REVIEW")
    ctx = build_memo_context(conn, out["case_id"], out["run_id"], 1,
                             analyst={"industry_risks": "Customer concentration noted in interview."})
    dscr = next(x for x in ctx["metrics"] if x["key"] == "dscr")
    assert dscr["formula"] and dscr["inputs"] and dscr["status"] == "computed"
    srcs = [s for x in ctx["history"] for c in x["cells"] for s in [c["source"]] if s]
    assert srcs and all("sample_pnl_fy2025.pdf" in s or "user entry" in s for s in srcs)
    html = render_memo(ctx)
    assert '<span class="label">ANALYST</span> Customer concentration' in html
    assert ctx["run"]["id"] == out["run_id"] and ctx["run"]["policy_version"] == "1.1.0"


def test_memo_versions_reproducible_and_immutable(conn, settings):
    out = advance(conn, settings, "CREDIT_REVIEW")
    cid = out["case_id"]
    m1 = generate_memo(conn, settings, cid, out["run_id"], pdf=True)
    m2 = generate_memo(conn, settings, cid, out["run_id"], pdf=False)
    assert (m1["version"], m2["version"]) == (1, 2)
    assert verify_reproduction(conn, cid, m1["id"]) == {**verify_reproduction(conn, cid, m1["id"]),
                                                         "context_intact": True, "html_reproduced": True}
    assert (settings.reports_dir / cid / "credit_memo_v1.pdf").is_file()
    assert len(list_memos(conn, cid)) == 2
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE credit_memos SET opinion_code='X'")


def test_memo_refuses_scenario_runs(conn, settings):
    from uwhelper.analysis.underwriting import create_scenario

    out = advance(conn, settings, "CREDIT_REVIEW")
    sid = create_scenario(conn, out["case_id"], "Down", None, {"revenue_change_pct": "-0.1"})
    srun = run_underwriting(conn, out["case_id"], scenario_id=sid)
    with pytest.raises(Exception, match="base-case"):
        build_memo_context(conn, out["case_id"], srun.run_id, 1)


def test_migration_from_v1_database_preserves_records(tmp_path, settings):
    """Simulate a database created by the previous release (migration 0001 only) and upgrade it."""
    from uwhelper.db.migrate import MIGRATIONS_DIR, discover

    path = tmp_path / "old.db"
    old_dir = tmp_path / "old_migrations"
    old_dir.mkdir()
    (old_dir / "0001_initial.sql").write_text((MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8"),
                                              encoding="utf-8")
    db = Database(path)
    with db.session() as c:
        apply_migrations(c, old_dir)
        c.execute("INSERT INTO loan_cases(id, case_number, title, loan_type, created_at, updated_at) "
                  "VALUES ('c1','UW-2025-0001','Old case','equipment','2025-01-01','2025-01-01')")
        c.execute("INSERT INTO audit_events(occurred_at, case_id, entity_type, entity_id, action) "
                  "VALUES ('2025-01-01', 'c1', 'loan_case', 'c1', 'case_created')")
    with db.session() as c:
        applied = apply_migrations(c)
        assert applied == [m.version for m in discover()][1:]
        row = c.execute("SELECT stage, title FROM loan_cases WHERE id='c1'").fetchone()
        assert tuple(row) == ("INTAKE", "Old case")
        hist = c.execute("SELECT from_stage, to_stage FROM workflow_transitions WHERE case_id='c1'").fetchall()
        assert [tuple(h) for h in hist] == [(None, "INTAKE")]
        assert backfill_chain(c) == 1 and verify_chain(c)["ok"]
        assert apply_migrations(c) == []


def test_audit_chain_detects_tampering(conn, settings):
    out = advance(conn, settings, "UNDERWRITING")
    assert verify_chain(conn)["ok"]
    conn.execute("DROP TRIGGER trg_audit_no_update")  # simulates someone editing the file directly
    conn.execute("UPDATE audit_events SET details_json='{\"tampered\": true}' WHERE id=(SELECT MAX(id) FROM audit_events)")
    v = verify_chain(conn)
    assert not v["ok"] and "modified" in v["problems"][0]


def test_audit_chain_survives_purge(conn, settings):
    from uwhelper.repo.cases import purge_case

    cid = clean_sample(conn, settings)
    clean_sample(conn, settings)
    purge_case(conn, cid, settings.documents_dir, settings.reports_dir)
    v = verify_chain(conn)
    assert v["ok"] and v["purged_placeholders"] > 0
