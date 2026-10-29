import zipfile

import pytest

from uwhelper.analysis.underwriting import run_underwriting
from uwhelper.backup import BackupError, create_backup, restore_backup
from uwhelper.config import load_settings
from uwhelper.documents.pdf import page_count, text_of
from uwhelper.llm.narrative import Narrative
from uwhelper.repo import cases
from uwhelper.reports.builder import build_context, generate_report, render_html
from uwhelper.sample_data import load_sample_case


@pytest.fixture
def sample(conn, settings):
    return load_sample_case(conn, settings)


def test_report_flags_unverified_missing_and_contradictions(conn, settings, sample):
    run = run_underwriting(conn, sample)
    html = render_html(build_context(conn, sample, run.run_id, 1))
    assert "NOT A CREDIT DECISION" in html and "ILLUSTRATIVE" in html
    assert "Unverified facts (2)" in html  # tax-return revenue and legal-settlement add-back
    assert "Revenue / net sales" in html and "unresolved; no value was selected automatically" in html
    assert "Questions requiring follow-up" in html and "Resolve conflicting values" in html
    assert "evidence is insufficient" in html  # INCOMPLETE run says so explicitly
    assert "sample_pnl_fy2025.pdf" in html and "p.1" in html  # source references
    assert "Human review checklist" in html


def test_report_files_and_versions(conn, settings, sample):
    run = run_underwriting(conn, sample, mode="include_unverified")
    out1 = generate_report(conn, settings, sample, run.run_id)
    out2 = generate_report(conn, settings, sample, run.run_id, ("pdf",))
    assert {o["format"] for o in out1} == {"html", "pdf"} and out2[0]["version"] == 2
    data = (settings.reports_dir / sample / "report_v1.pdf").read_bytes()
    text = text_of(data)
    assert page_count(data) > 2 and "PRELIMINARY SCREENING" in text and "Preliminary Credit Analysis" in text


def test_report_escapes_html_from_data(conn, settings):
    from uwhelper.domain.models import BorrowerIn, CaseIn

    cid = cases.create_case(conn, CaseIn(title="<script>alert(1)</script>", loan_type="equipment"),
                            BorrowerIn(legal_name="<img src=x onerror=alert(1)>"))
    run = run_underwriting(conn, cid)
    html = render_html(build_context(conn, cid, run.run_id, 1))
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "<img src=x" not in html


def test_narrative_section_labelled(conn, settings, sample):
    run = run_underwriting(conn, sample)
    n = Narrative("Summary text 99.", "qwen3:8b", unsupported_numbers=["99"], decision_language=["approved"])
    html = render_html(build_context(conn, sample, run.run_id, 1, n))
    assert "MODEL-GENERATED narrative (not verified)" in html and "numbers not found" in html
    assert "decision" in html


def test_backup_and_restore_roundtrip(conn, settings, sample, tmp_path):
    run = run_underwriting(conn, sample)
    generate_report(conn, settings, sample, run.run_id, ("html",))
    zpath = create_backup(settings, tmp_path / "b.zip")
    with zipfile.ZipFile(zpath) as zf:
        names = zf.namelist()
    assert "manifest.json" in names and "uwhelper.db" in names and any(n.startswith("documents/") for n in names)
    target = load_settings(data_dir=tmp_path / "restored")
    info = restore_backup(target, zpath)
    assert info["files"] >= 6
    from uwhelper.db.connection import Database

    with Database(target.db_path).session() as c2:
        assert c2.execute("SELECT COUNT(*) FROM loan_cases").fetchone()[0] == 1
    with pytest.raises(BackupError, match="already contains"):
        restore_backup(target, zpath)
    assert restore_backup(target, zpath, force=True)["files"] == info["files"]


def test_restore_rejects_tampered_or_unsafe(settings, tmp_path, conn):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("manifest.json", '{"app": "uwhelper", "files": {"../evil.txt": "x"}}')
    with pytest.raises(BackupError, match="Unsafe"):
        restore_backup(load_settings(data_dir=tmp_path / "t"), bad)
    z = create_backup(settings, tmp_path / "ok.zip")
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(z) as src, zipfile.ZipFile(tampered, "w") as dst:
        for item in src.namelist():
            data = src.read(item)
            dst.writestr(item, data + b"x" if item == "uwhelper.db" else data)
    with pytest.raises(BackupError, match="Checksum"):
        restore_backup(load_settings(data_dir=tmp_path / "t2"), tampered)


def test_purge_case_deletes_data(conn, settings, sample):
    run_underwriting(conn, sample)
    assert list(settings.documents_dir.rglob("*.pdf"))
    cases.purge_case(conn, sample, settings.documents_dir, settings.reports_dir)
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert not list(settings.documents_dir.rglob("*.pdf"))
    tomb = conn.execute("SELECT action, case_id FROM audit_events WHERE action='case_purged'").fetchone()
    assert tomb["case_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM audit_events WHERE action != 'case_purged' AND entity_type != 'policy' "
                        "AND action != 'document_processed'").fetchone()[0] == 0
