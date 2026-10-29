"""JSON API v1 for the shared CRM foundation (local only).

Writes require the ``X-CSRF-Token`` header (token from ``GET /api/v1/session``); cross-origin pages cannot
read that token because no CORS headers are sent, and the Host/Origin checks still apply.
Errors use the format in ``uwhelper.core.errors``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from uwhelper import __version__
from uwhelper.crm import activities as acts
from uwhelper.crm import importer
from uwhelper.crm import requests as docreq
from uwhelper.crm import service as crm
from uwhelper.crm import tasks as task_svc
from uwhelper.crm.models import (
    PRODUCT_TYPES,
    ActivityIn,
    ContactIn,
    ContactPatch,
    DealContactIn,
    DealIn,
    DealPatch,
    DealStatusIn,
    DocumentRequestIn,
    DocumentReviewIn,
    OrganizationIn,
    OrganizationPatch,
    TaskIn,
    TaskPatch,
)
from uwhelper.documents.storage import UploadRejected
from uwhelper.repo.cases import ValidationFailure
from uwhelper.web.app import get_db

router = APIRouter(prefix="/api/v1")


def _json(data: Any, status: int = 200) -> JSONResponse:
    def conv(x: Any) -> Any:
        if isinstance(x, Decimal):
            return str(x)
        if isinstance(x, dict):
            return {k: conv(v) for k, v in x.items()}
        if isinstance(x, list):
            return [conv(v) for v in x]
        return x
    return JSONResponse(conv(data), status_code=status)


@router.get("/session")
def session(request: Request) -> JSONResponse:
    return _json({"csrf_token": request.app.state.csrf_token, "version": __version__,
                  "header": "X-CSRF-Token"})


class _CaseLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(min_length=1, max_length=64)


class _TaskStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["open", "done", "cancelled"]


class _DocLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str = Field(min_length=1, max_length=64)


class _PartyContact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact_id: str | None = None
    create: bool = False


@router.get("/openapi.json", include_in_schema=False)
def openapi(request: Request) -> JSONResponse:
    """Schema of the local JSON API (no interactive UI: that would load assets from a CDN)."""
    app = request.app
    schema = getattr(app.state, "api_v1_openapi", None)
    if schema is None:
        full = app.openapi()  # generated in-process; nothing is fetched
        schema = {**full, "paths": {p: v for p, v in full.get("paths", {}).items()
                                    if p.startswith(("/api/v1", "/health", "/api/health"))}}
        schema["info"] = {"title": "UW Helper local API", "version": __version__,
                          "description": "Local-only API (127.0.0.1). Writes need the X-CSRF-Token header from "
                                         "GET /api/v1/session. Errors: {error: {code, message, details, request_id}}."}
        app.state.api_v1_openapi = schema
    return JSONResponse(schema)


@router.get("/meta")
def meta() -> JSONResponse:
    return _json({"deal_statuses": list(crm.DEAL_STATUSES), "deal_transitions": crm.DEAL_TRANSITIONS,
                  "product_types": PRODUCT_TYPES})


# ---------------------------------------------------------------------------------------- organizations
@router.get("/organizations")
def list_orgs(request: Request, q: str | None = Query(None, max_length=100), org_type: str | None = None,
              include_archived: bool = False, limit: int = Query(50, ge=1, le=200),
              offset: int = Query(0, ge=0)) -> JSONResponse:
    with get_db(request).session() as conn:
        return _json({"items": crm.list_organizations(conn, q=q, org_type=org_type, include_archived=include_archived,
                                                      limit=limit, offset=offset), "limit": limit, "offset": offset})


@router.post("/organizations")
def create_org(request: Request, body: OrganizationIn) -> JSONResponse:
    with get_db(request).session() as conn:
        oid = crm.create_organization(conn, body)
        return _json(crm.get_organization(conn, oid), 201)


@router.get("/organizations/{org_id}")
def get_org(request: Request, org_id: str) -> JSONResponse:
    with get_db(request).session() as conn:
        return _json(crm.get_organization(conn, org_id))


@router.patch("/organizations/{org_id}")
def patch_org(request: Request, org_id: str, body: OrganizationPatch) -> JSONResponse:
    with get_db(request).session() as conn:
        changes = crm.update_organization(conn, org_id, body)
        return _json({"organization": crm.get_organization(conn, org_id), "changed": sorted(changes)})


# ---------------------------------------------------------------------------------------- contacts
@router.get("/contacts")
def list_contacts(request: Request, q: str | None = Query(None, max_length=100), contact_type: str | None = None,
                  include_archived: bool = False, limit: int = Query(50, ge=1, le=200),
                  offset: int = Query(0, ge=0)) -> JSONResponse:
    with get_db(request).session() as conn:
        return _json({"items": crm.list_contacts(conn, q=q, contact_type=contact_type,
                                                 include_archived=include_archived, limit=limit, offset=offset),
                      "limit": limit, "offset": offset})


@router.post("/contacts")
def create_contact(request: Request, body: ContactIn) -> JSONResponse:
    with get_db(request).session() as conn:
        cid = crm.create_contact(conn, body)
        return _json(crm.get_contact(conn, cid), 201)


@router.get("/contacts/{contact_id}")
def get_contact(request: Request, contact_id: str) -> JSONResponse:
    with get_db(request).session() as conn:
        return _json(crm.get_contact(conn, contact_id))


@router.patch("/contacts/{contact_id}")
def patch_contact(request: Request, contact_id: str, body: ContactPatch) -> JSONResponse:
    with get_db(request).session() as conn:
        changes = crm.update_contact(conn, contact_id, body)
        return _json({"contact": crm.get_contact(conn, contact_id), "changed": sorted(changes)})


# ---------------------------------------------------------------------------------------- deals
@router.get("/deals")
def list_deals(request: Request, status: str | None = None,
               deal_type: Literal["small_business", "cre"] | None = None, q: str | None = Query(None, max_length=100),
               include_archived: bool = False, limit: int = Query(50, ge=1, le=200),
               offset: int = Query(0, ge=0)) -> JSONResponse:
    with get_db(request).session() as conn:
        return _json({"items": crm.list_deals(conn, status=status, deal_type=deal_type, q=q,
                                              include_archived=include_archived, limit=limit, offset=offset),
                      "limit": limit, "offset": offset})


@router.post("/deals")
def create_deal(request: Request, body: DealIn) -> JSONResponse:
    with get_db(request).session() as conn:
        did = crm.create_deal(conn, body)
        return _json(crm.get_deal(conn, did), 201)


@router.get("/deals/{deal_id}")
def get_deal(request: Request, deal_id: str) -> JSONResponse:
    with get_db(request).session() as conn:
        return _json(crm.get_deal(conn, deal_id))


@router.patch("/deals/{deal_id}")
def patch_deal(request: Request, deal_id: str, body: DealPatch) -> JSONResponse:
    with get_db(request).session() as conn:
        changes = crm.update_deal(conn, deal_id, body)
        return _json({"deal": crm.get_deal(conn, deal_id), "changed": sorted(changes)})


@router.post("/deals/{deal_id}/status")
def deal_status(request: Request, deal_id: str, body: DealStatusIn) -> JSONResponse:
    with get_db(request).session() as conn:
        new = crm.transition_deal(conn, deal_id, body)
        return _json({"status": new, "deal": crm.get_deal(conn, deal_id)})


@router.post("/deals/{deal_id}/contacts")
def deal_add_contact(request: Request, deal_id: str, body: DealContactIn) -> JSONResponse:
    with get_db(request).session() as conn:
        crm.add_deal_contact(conn, deal_id, body)
        return _json(crm.get_deal(conn, deal_id)["contacts"], 201)


@router.delete("/deals/{deal_id}/contacts/{contact_id}/{role}")
def deal_remove_contact(request: Request, deal_id: str, contact_id: str, role: str) -> JSONResponse:
    with get_db(request).session() as conn:
        crm.remove_deal_contact(conn, deal_id, contact_id, role)
        return _json({"removed": True})


@router.post("/deals/{deal_id}/documents")
def deal_upload(request: Request, deal_id: str, file: UploadFile = File(...),
                doc_type: str = Form("other")) -> JSONResponse:
    with get_db(request).session() as conn:
        try:
            res = crm.attach_document(conn, request.app.state.settings, deal_id, file.file, file.filename, doc_type)
        except UploadRejected as exc:
            raise ValidationFailure(str(exc)) from None
        return _json(res, 201)


@router.get("/deals/{deal_id}/documents")
def deal_documents(request: Request, deal_id: str) -> JSONResponse:
    with get_db(request).session() as conn:
        return _json(crm.get_deal(conn, deal_id)["documents"])


@router.post("/deals/{deal_id}/underwriting-file")
def deal_underwriting(request: Request, deal_id: str) -> JSONResponse:
    with get_db(request).session() as conn:
        case_id = crm.start_underwriting_file(conn, deal_id)
        return _json({"loan_case_id": case_id, "url": f"/cases/{case_id}"}, 201)


@router.post("/deals/{deal_id}/link-underwriting-file")
def deal_link_underwriting(request: Request, deal_id: str, body: _CaseLink) -> JSONResponse:
    with get_db(request).session() as conn:
        crm.link_underwriting_file(conn, deal_id, body.case_id)
        return _json(crm.get_deal(conn, deal_id))


@router.get("/underwriting-files/unlinked")
def unlinked_files(request: Request) -> JSONResponse:
    with get_db(request).session() as conn:
        return _json({"items": crm.unlinked_underwriting_files(conn)})


@router.post("/underwriting-files/{case_id}/parties/{party_id}/contact")
def party_contact(request: Request, case_id: str, party_id: str, body: _PartyContact) -> JSONResponse:
    with get_db(request).session() as conn:
        cid = crm.link_party_contact(conn, case_id, party_id, body.contact_id, create=body.create)
        return _json({"contact_id": cid})


@router.get("/deals/{deal_id}/timeline")
def deal_timeline(request: Request, deal_id: str, limit: int = Query(200, ge=1, le=500)) -> JSONResponse:
    with get_db(request).session() as conn:
        crm.get_deal(conn, deal_id)
        return _json({"items": acts.deal_timeline(conn, deal_id, limit)})


# ---------------------------------------------------------------------------------------- document requests
@router.get("/deals/{deal_id}/document-requests")
def doc_requests(request: Request, deal_id: str) -> JSONResponse:
    with get_db(request).session() as conn:
        crm.get_deal(conn, deal_id)
        return _json({"items": docreq.list_requests(conn, deal_id), "summary": docreq.summary(conn, deal_id)})


@router.post("/deals/{deal_id}/document-requests/generate")
def doc_requests_generate(request: Request, deal_id: str) -> JSONResponse:
    with get_db(request).session() as conn:
        added = docreq.generate_requests(conn, deal_id)
        return _json({"added": added, "items": docreq.list_requests(conn, deal_id)}, 201 if added else 200)


@router.post("/deals/{deal_id}/document-requests")
def doc_request_add(request: Request, deal_id: str, body: DocumentRequestIn) -> JSONResponse:
    with get_db(request).session() as conn:
        rid = docreq.add_request(conn, deal_id, body)
        return _json(docreq.get_request(conn, deal_id, rid), 201)


@router.post("/deals/{deal_id}/document-requests/{request_id}/upload")
def doc_request_upload(request: Request, deal_id: str, request_id: str, file: UploadFile = File(...)) -> JSONResponse:
    with get_db(request).session() as conn:
        try:
            res = docreq.fulfil_with_upload(conn, request.app.state.settings, deal_id, request_id, file.file,
                                            file.filename)
        except UploadRejected as exc:
            raise ValidationFailure(str(exc)) from None
        return _json({**res, "request": docreq.get_request(conn, deal_id, request_id)}, 201)


@router.post("/deals/{deal_id}/document-requests/{request_id}/link")
def doc_request_link(request: Request, deal_id: str, request_id: str, body: _DocLink) -> JSONResponse:
    with get_db(request).session() as conn:
        docreq.fulfil_with_existing(conn, deal_id, request_id, body.document_id)
        return _json(docreq.get_request(conn, deal_id, request_id))


@router.post("/deals/{deal_id}/document-requests/{request_id}/review")
def doc_request_review(request: Request, deal_id: str, request_id: str, body: DocumentReviewIn) -> JSONResponse:
    with get_db(request).session() as conn:
        docreq.review_request(conn, deal_id, request_id, body)
        return _json(docreq.get_request(conn, deal_id, request_id))


# ---------------------------------------------------------------------------------------- tasks
@router.get("/tasks")
def list_tasks(request: Request, view: str = "open", deal_id: str | None = None, contact_id: str | None = None,
               organization_id: str | None = None, assignee: str | None = Query(None, max_length=120),
               limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)) -> JSONResponse:
    with get_db(request).session() as conn:
        return _json({"items": task_svc.list_tasks(conn, view=view, deal_id=deal_id, contact_id=contact_id,
                                                   organization_id=organization_id, assignee=assignee, limit=limit,
                                                   offset=offset),
                      "counts": task_svc.task_counts(conn), "limit": limit, "offset": offset})


@router.post("/tasks")
def create_task(request: Request, body: TaskIn) -> JSONResponse:
    with get_db(request).session() as conn:
        return _json(task_svc.get_task(conn, task_svc.create_task(conn, body)), 201)


@router.get("/tasks/{task_id}")
def get_task(request: Request, task_id: str) -> JSONResponse:
    with get_db(request).session() as conn:
        return _json(task_svc.get_task(conn, task_id))


@router.patch("/tasks/{task_id}")
def patch_task(request: Request, task_id: str, body: TaskPatch) -> JSONResponse:
    with get_db(request).session() as conn:
        changed = task_svc.update_task(conn, task_id, body)
        return _json({"task": task_svc.get_task(conn, task_id), "changed": changed})


@router.post("/tasks/{task_id}/status")
def task_status(request: Request, task_id: str, body: _TaskStatus) -> JSONResponse:
    with get_db(request).session() as conn:
        task_svc.set_task_status(conn, task_id, body.status)
        return _json(task_svc.get_task(conn, task_id))


# ---------------------------------------------------------------------------------------- activities
@router.get("/activities")
def list_activities(request: Request, deal_id: str | None = None, contact_id: str | None = None,
                    organization_id: str | None = None, limit: int = Query(200, ge=1, le=500)) -> JSONResponse:
    with get_db(request).session() as conn:
        return _json({"items": acts.list_activities(conn, deal_id=deal_id, contact_id=contact_id,
                                                    organization_id=organization_id, limit=limit)})


@router.post("/activities")
def add_activity(request: Request, body: ActivityIn) -> JSONResponse:
    with get_db(request).session() as conn:
        return _json(acts.get_activity(conn, acts.add_activity(conn, body)), 201)


# ---------------------------------------------------------------------------------------- import
@router.post("/import/{kind}")
def import_csv(request: Request, kind: Literal["contacts", "organizations"], file: UploadFile = File(...),
               dry_run: bool = True, skip_invalid: bool = False) -> JSONResponse:
    text = importer.decode(file.file.read(importer.MAX_BYTES + 1))
    with get_db(request).session() as conn:
        if dry_run:
            p = importer.parse(conn, kind, text)
            return _json({"dry_run": True, "counts": p.counts, "columns": p.columns, "ignored_columns": p.ignored_columns,
                          "rows": [{"line": r.line, "status": r.status, "errors": r.errors} for r in p.rows]})
        return _json({"dry_run": False, **importer.commit(conn, kind, text, skip_invalid=skip_invalid,
                                                         source_name=file.filename)}, 201)


@router.get("/pipeline")
def pipeline(request: Request) -> JSONResponse:
    with get_db(request).session() as conn:
        return _json(crm.pipeline_summary(conn))
