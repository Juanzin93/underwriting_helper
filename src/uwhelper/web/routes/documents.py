"""Upload, inspect, (re)process and AI-extract documents."""

from __future__ import annotations

import logging

import pymupdf
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import FileResponse, Response
from starlette.datastructures import UploadFile

from uwhelper.config import Settings
from uwhelper.db.connection import Database
from uwhelper.documents import service
from uwhelper.documents.storage import UploadRejected
from uwhelper.llm.client import client_from_settings
from uwhelper.llm.extraction import extract_document_facts, list_extraction_runs
from uwhelper.modules import get_module
from uwhelper.repo import facts as facts_repo
from uwhelper.repo.cases import ValidationFailure, get_case
from uwhelper.web.app import get_db, redirect, render
from uwhelper.web.forms import form_dict
from uwhelper.web.routes.cases import case_context

router = APIRouter()
log = logging.getLogger("uwhelper.web.documents")


@router.get("/cases/{case_id}/documents")
async def documents_page(request: Request, case_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        ctx["documents"] = service.list_case_documents(conn, case_id)
        ctx["extraction_runs"] = list_extraction_runs(conn, case_id)[:20]
    running = any(r["status"] == "running" for r in ctx["extraction_runs"])
    return render(request, "case/documents.html", active="documents", auto_refresh=running, **ctx)


@router.post("/cases/{case_id}/documents")
async def upload(request: Request, case_id: str):
    form = await request.form()
    files = [f for f in form.getlist("files") if isinstance(f, UploadFile) and f.filename]
    if not files:
        raise ValidationFailure("Choose at least one file to upload.")
    doc_type = str(form.get("doc_type") or "other")
    period_label = (str(form.get("period_label") or "").strip() or None)
    password = str(form.get("password") or "") or None  # used once, never stored
    settings: Settings = request.app.state.settings
    messages = []
    with get_db(request).session() as conn:
        get_case(conn, case_id)
        for f in files[:20]:
            try:
                res = service.import_document(conn, settings, case_id, f.file, f.filename, doc_type, period_label,
                                              password=password)
            except UploadRejected as exc:
                messages.append(f"{f.filename}: rejected — {exc}")
                continue
            if res.duplicate:
                messages.append(f"{f.filename}: identical file already stored (SHA-256 match) — "
                                + ("already on this case." if res.already_linked else "linked without reprocessing."))
            else:
                messages.append(f"{f.filename}: imported ({res.status}).")
    level = "warn" if any("rejected" in m for m in messages) else "info"
    return redirect(f"/cases/{case_id}/documents", " ".join(messages), level)


@router.get("/cases/{case_id}/documents/{document_id}")
async def document_detail(request: Request, case_id: str, document_id: str):
    page = int(request.query_params.get("page", "1") or 1)
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        doc = service.get_case_document(conn, case_id, document_id)
        pages = service.get_pages(conn, document_id)
        current = next((p for p in pages if p["page_number"] == page), pages[0] if pages else None)
        facts = facts_repo.list_facts(conn, case_id, document_id=document_id)
    catalog = get_module("commercial").field_catalog()
    for f in facts:
        f["field_label"] = catalog[f["field_key"]].label if f["field_key"] in catalog else f["field_key"]
    page_facts = [f for f in facts if current and f["page_number"] == current["page_number"]]
    return render(request, "case/document_detail.html", active="documents", doc=doc, pages=pages, current=current,
                  facts=facts, page_facts=page_facts, **ctx)


@router.get("/cases/{case_id}/documents/{document_id}/pages/{page_number}.png")
async def page_image(request: Request, case_id: str, document_id: str, page_number: int):
    settings: Settings = request.app.state.settings
    with get_db(request).session() as conn:
        doc = service.get_case_document(conn, case_id, document_id)
    if doc["file_ext"] == ".txt" or doc["processing_status"] in ("encrypted", "failed", "unsupported"):
        return Response(status_code=404)
    path = service.document_path(settings, doc)
    try:
        with pymupdf.open(path) as pdf:
            if not 1 <= page_number <= pdf.page_count:
                return Response(status_code=404)
            png = pdf[page_number - 1].get_pixmap(dpi=110).tobytes("png")
    except Exception:
        return Response(status_code=404)
    return Response(png, media_type="image/png")


@router.get("/cases/{case_id}/documents/{document_id}/file")
async def download(request: Request, case_id: str, document_id: str):
    settings: Settings = request.app.state.settings
    with get_db(request).session() as conn:
        doc = service.get_case_document(conn, case_id, document_id)
    # Served as an attachment so the browser never renders document content inside the app.
    return FileResponse(service.document_path(settings, doc), media_type="application/octet-stream",
                        filename=doc["original_filename"])


@router.post("/cases/{case_id}/documents/{document_id}/classify")
async def classify(request: Request, case_id: str, document_id: str):
    data = await form_dict(request)
    with get_db(request).session() as conn:
        service.set_document_type(conn, case_id, document_id, data.get("doc_type", "other"),
                                  data.get("period_label") or None, data.get("document_date") or None)
    return redirect(f"/cases/{case_id}/documents/{document_id}", "Document classification saved.")


@router.post("/cases/{case_id}/documents/{document_id}/reprocess")
async def reprocess(request: Request, case_id: str, document_id: str):
    data = await form_dict(request)
    with get_db(request).session() as conn:
        service.get_case_document(conn, case_id, document_id)
        status = service.process_document(conn, request.app.state.settings, document_id, data.get("password") or None)
    return redirect(f"/cases/{case_id}/documents/{document_id}", f"Re-processed: {status}.")


@router.post("/cases/{case_id}/documents/{document_id}/remove")
async def remove(request: Request, case_id: str, document_id: str):
    with get_db(request).session() as conn:
        deleted = service.remove_document(conn, request.app.state.settings, case_id, document_id)
    return redirect(f"/cases/{case_id}/documents",
                    "Document removed from case" + (" and file deleted." if deleted else " (still used by another case)."))


def _extract_job(db: Database, settings: Settings, case_id: str, document_ids: list[str]) -> None:
    with db.session() as conn, client_from_settings(settings) as client:
        for doc_id in document_ids:
            try:
                extract_document_facts(conn, client, case_id, doc_id, max_chars=settings.extraction_max_chars_per_chunk,
                                       retries=settings.ollama_retries)
            except Exception as exc:  # never crash the worker; details stay in the local log
                log.error("Extraction job failed for a document: %s", exc.__class__.__name__, exc_info=exc)


@router.post("/cases/{case_id}/extract")
async def extract(request: Request, case_id: str, background: BackgroundTasks):
    data = await form_dict(request)
    with get_db(request).session() as conn:
        get_case(conn, case_id)
        docs = service.list_case_documents(conn, case_id)
    wanted = data.get("document_id")
    ids = [d["id"] for d in docs if (not wanted or d["id"] == wanted)
           and d["processing_status"] in ("processed", "partial")]
    if not ids:
        raise ValidationFailure("No processed documents with text to extract from.")
    background.add_task(_extract_job, request.app.state.db, request.app.state.settings, case_id, ids)
    return redirect(f"/cases/{case_id}/documents",
                    f"Local extraction started for {len(ids)} document(s) with {request.app.state.settings.ollama_model}. "
                    "This page refreshes until it finishes; results appear in Extraction review as UNVERIFIED candidates.")
