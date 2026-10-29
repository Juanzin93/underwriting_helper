"""Report preview, generation and download."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from uwhelper.analysis.underwriting import get_run, latest_run_id, list_runs
from uwhelper.llm.client import client_from_settings
from uwhelper.llm.narrative import generate_narrative
from uwhelper.reports.builder import build_context, generate_report, list_reports, render_html
from uwhelper.repo.cases import NotFound, ValidationFailure
from uwhelper.web.app import get_db, redirect, render
from uwhelper.web.forms import checkbox, form_dict
from uwhelper.web.routes.cases import case_context

router = APIRouter()
REPORT_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'"


@router.get("/cases/{case_id}/reports")
async def reports_page(request: Request, case_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        ctx["reports"] = list_reports(conn, case_id)
        ctx["runs"] = list_runs(conn, case_id)
    return render(request, "case/reports.html", active="reports", **ctx)


@router.get("/cases/{case_id}/reports/preview")
async def preview(request: Request, case_id: str):
    with get_db(request).session() as conn:
        run_id = request.query_params.get("run_id") or latest_run_id(conn, case_id)
        if not run_id:
            raise ValidationFailure("Run the underwriting screen before previewing a report.")
        row = conn.execute("SELECT COALESCE(MAX(version),0) AS v FROM reports WHERE case_id=?", (case_id,)).fetchone()
        html = render_html(build_context(conn, case_id, run_id, int(row["v"]) + 1))
    return HTMLResponse(html, headers={"Content-Security-Policy": REPORT_CSP})


@router.post("/cases/{case_id}/reports")
async def create_report(request: Request, case_id: str):
    data = checkbox(await form_dict(request), "include_narrative")
    fmt = data.get("format", "both")
    formats = {"html": ("html",), "pdf": ("pdf",)}.get(fmt, ("html", "pdf"))
    settings = request.app.state.settings
    with get_db(request).session() as conn:
        run_id = data.get("run_id") or latest_run_id(conn, case_id)
        if not run_id:
            raise ValidationFailure("Run the underwriting screen before generating a report.")
        narrative = None
        if data["include_narrative"]:
            with client_from_settings(settings) as client:
                narrative = generate_narrative(client, get_run(conn, run_id, case_id))
        produced = generate_report(conn, settings, case_id, run_id, formats, narrative)
    msg = f"Report version {produced[0]['version']} generated."
    if narrative and narrative.error:
        msg += f" Narrative unavailable ({narrative.error[:80]}); report generated without it."
    return redirect(f"/cases/{case_id}/reports", msg)


@router.get("/cases/{case_id}/reports/{report_id}")
async def get_report(request: Request, case_id: str, report_id: str):
    settings = request.app.state.settings
    with get_db(request).session() as conn:
        row = conn.execute("SELECT * FROM reports WHERE id=? AND case_id=?", (report_id, case_id)).fetchone()
    if not row:
        raise NotFound("Report not found")
    path = (settings.reports_dir / case_id / row["file_name"]).resolve()
    if settings.reports_dir.resolve() not in path.parents or not path.is_file():
        raise NotFound("Report file is missing from the data directory")
    if row["format"] == "html":
        return HTMLResponse(path.read_text(encoding="utf-8"), headers={"Content-Security-Policy": REPORT_CSP})
    disposition = "inline" if request.query_params.get("inline") else "attachment"
    return FileResponse(path, media_type="application/pdf", filename=row["file_name"], content_disposition_type=disposition)
