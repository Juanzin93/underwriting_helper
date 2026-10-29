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

from uwhelper import __version__
from uwhelper.crm import service as crm
from uwhelper.crm.models import (
    PRODUCT_TYPES,
    ContactIn,
    ContactPatch,
    DealContactIn,
    DealIn,
    DealPatch,
    DealStatusIn,
    OrganizationIn,
    OrganizationPatch,
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


@router.get("/pipeline")
def pipeline(request: Request) -> JSONResponse:
    with get_db(request).session() as conn:
        return _json(crm.pipeline_summary(conn))
