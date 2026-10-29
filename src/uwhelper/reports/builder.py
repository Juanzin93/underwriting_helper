"""Preliminary credit analysis report (HTML and PDF).

The report is assembled only from stored, deterministic data: a saved
underwriting run plus the case's current facts/documents. Risks and
follow-up questions are derived mechanically from rule results and findings
(nothing is invented). An optional model narrative is clearly labelled and
number-checked.
"""

from __future__ import annotations

import hashlib
import io
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pymupdf
from jinja2 import Environment, FileSystemLoader, select_autoescape

from uwhelper import ENGINE_VERSION, __version__
from uwhelper.analysis.resolver import find_contradictions, missing_checklist
from uwhelper.analysis.underwriting import get_run, list_scenarios
from uwhelper.config import Settings
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.documents.service import list_case_documents
from uwhelper.domain.enums import OVERALL_STATUS_TEXT
from uwhelper.llm.narrative import Narrative
from uwhelper.modules import get_module
from uwhelper.presentation import register_filters
from uwhelper.repo import audit
from uwhelper.repo import cases as cases_repo
from uwhelper.repo import facts as facts_repo

TEMPLATES = Path(__file__).parent / "templates"
KEY_FIGURES = ["revenue", "gross_profit", "operating_income", "ebitda", "adjusted_ebitda", "cfads",
               "existing_debt_service", "proposed_debt_service", "total_debt_service", "working_capital",
               "total_equity", "funded_debt_pro_forma", "collateral_value", "noi"]
RATIOS = ["dscr", "global_dscr", "dscr_property", "debt_to_ebitda", "debt_to_equity", "current_ratio", "quick_ratio",
          "gross_margin", "operating_margin", "ebitda_margin", "net_margin", "ltv", "cltv", "ltc",
          "collateral_coverage", "debt_yield", "breakeven_revenue", "revenue_cushion"]

HUMAN_REVIEW_CHECKLIST = [
    "Every figure used in metrics was compared against the source document page shown.",
    "All contradictions were resolved with a documented reason (no value chosen because it looks better).",
    "Unverified AI-extracted values were either verified or excluded.",
    "Income-statement periods are comparable (fiscal year, TTM, interim) and annualization is acceptable.",
    "EBITDA add-backs are documented, non-recurring and supportable.",
    "Guarantor income excludes amounts paid by the borrowing business (no double counting).",
    "Existing debt schedule was reconciled to the balance sheet and credit report.",
    "Collateral valuations are current, from an acceptable source, and liens were verified.",
    "Policy thresholds used are your actual sourced policy (demo thresholds are illustrative only).",
    "Qualitative factors not modelled here (management, industry, character, legal, environmental) were reviewed.",
]

LIMITATIONS = [
    "This is a preliminary screening produced by a personal research tool. It is not a credit decision, an approval, "
    "a denial, or a commitment to lend.",
    "Calculated values are only as reliable as the verified inputs; AI-extracted values are untrusted until verified.",
    "Metrics are not calculated when prerequisites are missing or contradictory; such gaps are listed rather than estimated.",
    "Sensitivity analysis treats COGS as variable and all other costs as fixed; tax effects of shocks are ignored.",
    "Rate shocks apply to the proposed loan only; existing debt payments are held constant.",
    "Demo policy thresholds are illustrative and are not universal commercial lending standards.",
]


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]),
                      trim_blocks=True, lstrip_blocks=True)
    register_filters(env)
    return env


def build_context(conn: sqlite3.Connection, case_id: str, run_id: str, version: int,
                  narrative: Narrative | None = None) -> dict[str, Any]:
    run = get_run(conn, run_id, case_id)
    case = cases_repo.get_case(conn, case_id)
    module = get_module(case["module_key"])
    docs = list_case_documents(conn, case_id)
    facts = facts_repo.list_facts(conn, case_id, statuses=("candidate", "verified", "rejected"))
    catalog = module.field_catalog()
    for f in facts:
        f["field_label"] = catalog[f["field_key"]].label if f["field_key"] in catalog else f["field_key"]
    contradictions = find_contradictions(conn, case_id)
    checklist = missing_checklist(conn, case_id, run["mode"])
    present_types = {d["doc_type"] for d in docs}
    doc_types = module.document_types()
    required_docs = sorted({d for e in run["evaluations"] if e["subject"] and e["subject"].startswith("documents:")
                            for d in e["subject"].split(":", 1)[1].split(",")})
    missing_docs = [doc_types.get(d, d) for d in required_docs if d not in present_types]

    risks, questions = [], []
    for e in run["evaluations"]:
        if e["result"] in ("FAIL", "REVIEW_REQUIRED"):
            risks.append(f"[{e['rule_id']}] {e['description']}: {e['explanation']}")
        elif e["result"] == "UNKNOWN":
            questions.append(f"[{e['rule_id']}] Cannot evaluate — {e['explanation']}")
    for c in contradictions:
        refs = "; ".join(f"{f.get('original_filename') or f['source_type']} p.{f.get('page_number') or '?'} = "
                         f"{f.get('value_numeric') or f.get('value_text')}" for f in c["facts"])
        questions.append(f"Resolve conflicting values for {c['field_label']} ({c['period_label'] or 'n/a'}): {refs}")
    for item in checklist:
        if item["status"] in ("missing", "unverified"):
            questions.append(f"{item['label']}: {'; '.join(item['details'])}")

    scenarios = []
    for s in list_scenarios(conn, case_id):
        if s["last_run"]:
            srun = get_run(conn, s["last_run"]["id"], case_id)
            scenarios.append({"name": s["name"], "adjustments": s["adjustments"], "run_number": srun["run_number"],
                              "overall_status": srun["overall_status"],
                              "metrics": {k: srun["metrics_by_key"].get(k) for k in ("dscr", "global_dscr",
                                                                                     "dscr_property", "ltv",
                                                                                     "debt_to_ebitda")}})
    unverified = [f for f in facts if f["status"] == "candidate"]
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "report_version": version,
        "app_version": __version__,
        "engine_version": ENGINE_VERSION,
        "case": case,
        "loan_type_label": module.loan_types[case["loan_type"]].label,
        "parties": cases_repo.list_parties(conn, case_id),
        "debts": cases_repo.list_debts(conn, case_id),
        "collateral": cases_repo.list_collateral(conn, case_id),
        "run": run,
        "overall_text": OVERALL_STATUS_TEXT.get(run["overall_status"], run["overall_status"]),
        "documents": docs,
        "doc_types": doc_types,
        "missing_docs": missing_docs,
        "periods": facts_repo.list_periods(conn, case_id),
        "key_figures": [run["metrics_by_key"][k] for k in KEY_FIGURES if k in run["metrics_by_key"]
                        and run["metrics_by_key"][k]["status"] != "not_applicable"],
        "ratios": [run["metrics_by_key"][k] for k in RATIOS if k in run["metrics_by_key"]
                   and run["metrics_by_key"][k]["status"] != "not_applicable"],
        "verified_facts": [f for f in facts if f["status"] == "verified"],
        "unverified_facts": unverified,
        "rejected_facts": [f for f in facts if f["status"] == "rejected"],
        "contradictions": contradictions,
        "checklist": checklist,
        "risks": risks,
        "questions": questions,
        "scenarios": scenarios,
        "sensitivity": run["summary"].get("sensitivity"),
        "limitations": LIMITATIONS,
        "review_checklist": HUMAN_REVIEW_CHECKLIST,
        "narrative": narrative,
        "evidence_insufficient": run["overall_status"] == "INCOMPLETE",
    }


def render_html(context: dict[str, Any], *, for_pdf: bool = False) -> str:
    return _env().get_template("report.html").render(**context, for_pdf=for_pdf)


PDF_CSS = """
* { font-family: sans-serif; }
body { font-size: 9pt; color: #111; }
h1 { font-size: 16pt; margin: 0 0 4pt 0; }
h2 { font-size: 12pt; margin: 12pt 0 4pt 0; border-bottom: 1px solid #555; }
h3 { font-size: 10pt; margin: 8pt 0 3pt 0; }
table { border-collapse: collapse; width: 100%; margin-bottom: 6pt; }
th, td { border: 1px solid #999; padding: 2pt 3pt; font-size: 8pt; vertical-align: top; text-align: left; }
th { background-color: #e8e8e8; }
.banner { border: 2px solid #000; padding: 4pt; font-weight: bold; }
.small { font-size: 7.5pt; color: #333; }
"""


def render_pdf(html: str) -> bytes:
    buf = io.BytesIO()
    story = pymupdf.Story(html=html, user_css=PDF_CSS)
    writer = pymupdf.DocumentWriter(buf)
    mediabox = pymupdf.paper_rect("letter")
    where = mediabox + (40, 40, -40, -50)
    more = True
    while more:
        device = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()
    # Footer with page numbers and the non-decision disclaimer on every page.
    doc = pymupdf.open("pdf", buf.getvalue())
    total = doc.page_count
    for i, page in enumerate(doc, start=1):
        page.insert_text((40, page.rect.height - 25),
                         f"PRELIMINARY SCREENING — NOT A CREDIT DECISION — page {i} of {total}",
                         fontsize=7, fontname="helv")
    doc.set_metadata({"title": "Preliminary Credit Analysis", "producer": "UW Helper (local)", "creator": "UW Helper"})
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out


def generate_report(conn: sqlite3.Connection, settings: Settings, case_id: str, run_id: str,
                    formats: tuple[str, ...] = ("html", "pdf"), narrative: Narrative | None = None) -> list[dict[str, Any]]:
    row = conn.execute("SELECT COALESCE(MAX(version),0) AS v FROM reports WHERE case_id=?", (case_id,)).fetchone()
    version = int(row["v"]) + 1
    context = build_context(conn, case_id, run_id, version, narrative)
    out_dir = settings.reports_dir / case_id
    out_dir.mkdir(parents=True, exist_ok=True)
    produced: list[tuple[str, str, bytes]] = []
    if "html" in formats:
        produced.append(("html", f"report_v{version}.html", render_html(context).encode("utf-8")))
    if "pdf" in formats:
        produced.append(("pdf", f"report_v{version}.pdf", render_pdf(render_html(context, for_pdf=True))))
    written: list[Path] = []
    results = []
    try:
        for _, name, data in produced:
            p = out_dir / name
            p.write_bytes(data)
            written.append(p)
        with Database.transaction(conn):
            for fmt, name, data in produced:
                rid = new_id()
                digest = hashlib.sha256(data).hexdigest()
                conn.execute(
                    """INSERT INTO reports(id, case_id, run_id, version, format, file_name, content_sha256,
                           includes_narrative, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (rid, case_id, run_id, version, fmt, name, digest, int(narrative is not None), utcnow()))
                results.append({"id": rid, "format": fmt, "file_name": name, "sha256": digest, "version": version})
            audit.record(conn, "report_generated", "report", run_id, case_id,
                         {"version": version, "formats": list(formats), "narrative": narrative is not None})
    except Exception:
        for p in written:
            p.unlink(missing_ok=True)
        raise
    return results


def list_reports(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        """SELECT rp.*, r.run_number FROM reports rp JOIN underwriting_runs r ON r.id=rp.run_id
           WHERE rp.case_id=? ORDER BY rp.version DESC, rp.format""", (case_id,))]
