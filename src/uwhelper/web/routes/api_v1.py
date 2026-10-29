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
from uwhelper.brokerage.fees import FeeAgreementIn, LedgerEntryIn
from uwhelper.brokerage.submissions import DecisionIn as OfferDecisionIn
from uwhelper.brokerage.submissions import OfferIn, SubmissionIn, SubmissionStatusIn
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
from uwhelper.lenders.models import CriteriaVersionIn, ProgramIn, ProgramPatch
from uwhelper.origination.closing import ClosingIn, FundingConfirmIn
from uwhelper.origination.conditions import (
    ConditionIn,
    ConditionUpdateIn,
    DDItemIn,
    DDUpdateIn,
)
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


@router.post("/deals/{deal_id}/memos")
def deal_memo_create(request: Request, deal_id: str) -> JSONResponse:
    from uwhelper.reports import deal_memo

    with get_db(request).session() as conn:
        return _json(deal_memo.generate(conn, deal_id), 201)


@router.get("/deals/{deal_id}/memos")
def deal_memo_list(request: Request, deal_id: str) -> JSONResponse:
    from uwhelper.reports import deal_memo

    with get_db(request).session() as conn:
        crm.get_deal(conn, deal_id)
        return _json({"items": deal_memo.list_memos(conn, deal_id)})


@router.get("/deals/{deal_id}/memos/{memo_id}/verify")
def deal_memo_verify(request: Request, deal_id: str, memo_id: str) -> JSONResponse:
    from uwhelper.reports import deal_memo

    with get_db(request).session() as conn:
        return _json(deal_memo.verify(conn, deal_id, memo_id))


@router.get("/underwriting-files/{case_id}/runs/{run_id}/verify")
def run_verify(request: Request, case_id: str, run_id: str) -> JSONResponse:
    from uwhelper.analysis.reproduce import verify_run

    with get_db(request).session() as conn:
        return _json(verify_run(conn, case_id, run_id))


@router.get("/underwriting-files/{case_id}/runs/{run_id}/traceability")
def run_trace(request: Request, case_id: str, run_id: str) -> JSONResponse:
    from uwhelper.analysis.evidence import run_traceability

    with get_db(request).session() as conn:
        return _json(run_traceability(conn, case_id, run_id))


# ---------------------------------------------------------------------------------------- lenders
@router.get("/lender-programs")
def lender_programs(request: Request, organization_id: str | None = None, include_inactive: bool = False) -> JSONResponse:
    from uwhelper.lenders import service as L

    with get_db(request).session() as conn:
        return _json({"items": L.list_programs(conn, organization_id=organization_id,
                                               include_inactive=include_inactive)})


@router.post("/lender-programs")
def lender_program_create(request: Request, body: ProgramIn) -> JSONResponse:
    from uwhelper.lenders import service as L

    with get_db(request).session() as conn:
        return _json(L.get_program(conn, L.create_program(conn, body)), 201)


@router.get("/lender-programs/{program_id}")
def lender_program(request: Request, program_id: str) -> JSONResponse:
    from uwhelper.lenders import service as L

    with get_db(request).session() as conn:
        return _json(L.get_program(conn, program_id))


@router.patch("/lender-programs/{program_id}")
def lender_program_patch(request: Request, program_id: str, body: ProgramPatch) -> JSONResponse:
    from uwhelper.lenders import service as L

    with get_db(request).session() as conn:
        changed = L.update_program(conn, program_id, body)
        return _json({"program": L.get_program(conn, program_id), "changed": changed})


@router.post("/lender-programs/{program_id}/versions")
def lender_program_version(request: Request, program_id: str, body: CriteriaVersionIn) -> JSONResponse:
    from uwhelper.lenders import service as L

    with get_db(request).session() as conn:
        res = L.add_version(conn, program_id, body)
        return _json(res, 201 if res["created"] else 200)


@router.post("/lender-catalog/import")
def lender_catalog_import(request: Request, file: UploadFile = File(...), dry_run: bool = True) -> JSONResponse:
    from uwhelper.core.csvutil import decode
    from uwhelper.lenders import importer as LI

    text = decode(file.file.read(1_000_001))
    with get_db(request).session() as conn:
        if dry_run:
            return _json({"dry_run": True, "programs": [
                {"lender": p.lender, "program": p.program, "lender_exists": p.lender_exists,
                 "program_exists": bool(p.program_id), "criteria_version": p.version_action, "errors": p.errors}
                for p in LI.plan_yaml(conn, text)]})
        return _json({"dry_run": False, **LI.commit_yaml(conn, text)}, 201)


@router.post("/deals/{deal_id}/matches")
def deal_matches_run(request: Request, deal_id: str) -> JSONResponse:
    from uwhelper.lenders import matching

    with get_db(request).session() as conn:
        mid = matching.run_matching(conn, deal_id, max_age_days=request.app.state.settings.criteria_max_age_days)
        return _json(matching.get_match_run(conn, deal_id, mid), 201)


@router.get("/deals/{deal_id}/matches")
def deal_matches(request: Request, deal_id: str) -> JSONResponse:
    from uwhelper.lenders import matching

    with get_db(request).session() as conn:
        crm.get_deal(conn, deal_id)
        return _json({"items": matching.list_match_runs(conn, deal_id)})


@router.get("/deals/{deal_id}/matches/{match_id}")
def deal_match(request: Request, deal_id: str, match_id: str) -> JSONResponse:
    from uwhelper.lenders import matching

    with get_db(request).session() as conn:
        return _json(matching.get_match_run(conn, deal_id, match_id))


# ---------------------------------------------------------------------------------------- brokerage
class _PackageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    program_id: str
    document_ids: list[str] = Field(default_factory=list)
    memo_id: str | None = None
    match_run_id: str | None = None
    include_figures: bool = True


class _ExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed_by: str
    confirm: bool = False


@router.post("/deals/{deal_id}/packages")
def package_build(request: Request, deal_id: str, body: _PackageIn) -> JSONResponse:
    from uwhelper.brokerage import packages as P

    with get_db(request).session() as conn:
        pid = P.build_package(conn, request.app.state.settings, deal_id, body.program_id, body.document_ids,
                              memo_id=body.memo_id, match_run_id=body.match_run_id, include_figures=body.include_figures)
        return _json(P.get_package(conn, deal_id, pid), 201)


@router.get("/deals/{deal_id}/packages/{package_id}")
def package_get(request: Request, deal_id: str, package_id: str) -> JSONResponse:
    from uwhelper.brokerage import packages as P

    with get_db(request).session() as conn:
        return _json(P.get_package(conn, deal_id, package_id))


@router.post("/deals/{deal_id}/packages/{package_id}/exports")
def package_export(request: Request, deal_id: str, package_id: str, body: _ExportIn) -> JSONResponse:
    from uwhelper.brokerage import packages as P

    with get_db(request).session() as conn:
        res = P.export_package(conn, request.app.state.settings, deal_id, package_id, confirmed_by=body.confirmed_by,
                               confirm=body.confirm)
        return _json({k: v for k, v in res.items() if k != "path"}, 201)


@router.get("/deals/{deal_id}/submissions")
def submissions_list(request: Request, deal_id: str) -> JSONResponse:
    from uwhelper.brokerage import submissions as S

    with get_db(request).session() as conn:
        crm.get_deal(conn, deal_id)
        return _json({"items": S.list_submissions(conn, deal_id)})


@router.post("/deals/{deal_id}/submissions")
def submission_create(request: Request, deal_id: str, body: SubmissionIn) -> JSONResponse:
    from uwhelper.brokerage import submissions as S

    with get_db(request).session() as conn:
        return _json(S.get_submission(conn, deal_id, S.create_submission(conn, deal_id, body)), 201)


@router.get("/deals/{deal_id}/submissions/{submission_id}")
def submission_get(request: Request, deal_id: str, submission_id: str) -> JSONResponse:
    from uwhelper.brokerage import submissions as S

    with get_db(request).session() as conn:
        return _json(S.get_submission(conn, deal_id, submission_id))


@router.post("/deals/{deal_id}/submissions/{submission_id}/status")
def submission_status(request: Request, deal_id: str, submission_id: str, body: SubmissionStatusIn) -> JSONResponse:
    from uwhelper.brokerage import submissions as S

    with get_db(request).session() as conn:
        return _json(S.transition(conn, deal_id, submission_id, body))


@router.post("/deals/{deal_id}/submissions/{submission_id}/offers")
def offer_create(request: Request, deal_id: str, submission_id: str, body: OfferIn) -> JSONResponse:
    from uwhelper.brokerage import submissions as S

    with get_db(request).session() as conn:
        return _json(S.get_offer(conn, deal_id, S.add_offer(conn, deal_id, submission_id, body)), 201)


@router.get("/deals/{deal_id}/offers")
def offers_compare(request: Request, deal_id: str) -> JSONResponse:
    from uwhelper.brokerage import submissions as S

    with get_db(request).session() as conn:
        return _json(S.compare_offers(conn, deal_id))


@router.post("/deals/{deal_id}/offers/{offer_id}/{decision}")
def offer_decide(request: Request, deal_id: str, offer_id: str, decision: Literal["accept", "decline"],
                 body: OfferDecisionIn) -> JSONResponse:
    from uwhelper.brokerage import submissions as S

    with get_db(request).session() as conn:
        if decision == "accept":
            res = S.accept_offer(conn, deal_id, offer_id, body)
        else:
            S.decline_offer(conn, deal_id, offer_id, body)
            res = {}
        return _json({"offer": S.get_offer(conn, deal_id, offer_id), **res})


@router.get("/deals/{deal_id}/fees")
def fees_get(request: Request, deal_id: str) -> JSONResponse:
    from uwhelper.brokerage import fees as F

    with get_db(request).session() as conn:
        funded, basis = F.funded_amount(conn, deal_id)
        agreements = F.list_agreements(conn, deal_id)
        lg = F.ledger(conn, deal_id)
        lg["reversed_ids"] = sorted(lg["reversed_ids"])
        return _json({"agreements": agreements, "funded_amount": funded, "funded_basis": basis,
                      "calculation": F.compute([a for a in agreements if not a["voided"]], funded), "ledger": lg})


@router.post("/deals/{deal_id}/fees/agreements")
def fee_agreement_create(request: Request, deal_id: str, body: FeeAgreementIn) -> JSONResponse:
    from uwhelper.brokerage import fees as F

    with get_db(request).session() as conn:
        return _json({"id": F.add_agreement(conn, deal_id, body)}, 201)


@router.post("/deals/{deal_id}/fees/expected")
def fee_expected(request: Request, deal_id: str) -> JSONResponse:
    from datetime import date as _date

    from uwhelper.brokerage import fees as F

    with get_db(request).session() as conn:
        return _json(F.record_expected(conn, deal_id, _date.today()), 201)


@router.post("/deals/{deal_id}/fees/entries")
def fee_entry(request: Request, deal_id: str, body: LedgerEntryIn) -> JSONResponse:
    from uwhelper.brokerage import fees as F

    with get_db(request).session() as conn:
        return _json({"id": F.record_entry(conn, deal_id, body)}, 201)


@router.get("/commissions")
def commissions(request: Request) -> JSONResponse:
    from uwhelper.brokerage import fees as F

    with get_db(request).session() as conn:
        return _json(F.commissions_report(conn))


# ---------------------------------------------------------------------------------------- closing & funding
@router.get("/deals/{deal_id}/closing")
def closing_get(request: Request, deal_id: str) -> JSONResponse:
    from uwhelper.web.routes.closing import closing_context

    with get_db(request).session() as conn:
        ctx = closing_context(conn, deal_id)
        ctx.pop("vendors", None)
        ctx["deal"] = {k: ctx["deal"][k] for k in ("id", "deal_number", "name", "status")}
        return _json(ctx)


@router.post("/deals/{deal_id}/conditions")
def condition_create(request: Request, deal_id: str, body: ConditionIn) -> JSONResponse:
    from uwhelper.origination import conditions as C

    with get_db(request).session() as conn:
        return _json(C.get_condition(conn, deal_id, C.add_condition(conn, deal_id, body)), 201)


@router.post("/deals/{deal_id}/conditions/{condition_id}")
def condition_update(request: Request, deal_id: str, condition_id: str, body: ConditionUpdateIn) -> JSONResponse:
    from uwhelper.origination import conditions as C

    with get_db(request).session() as conn:
        C.update_condition(conn, deal_id, condition_id, body)
        return _json(C.get_condition(conn, deal_id, condition_id))


@router.post("/deals/{deal_id}/diligence")
def dd_create(request: Request, deal_id: str, body: DDItemIn) -> JSONResponse:
    from uwhelper.origination import conditions as C

    with get_db(request).session() as conn:
        return _json(C.get_dd_item(conn, deal_id, C.add_dd_item(conn, deal_id, body)), 201)


@router.post("/deals/{deal_id}/diligence/{item_id}")
def dd_update(request: Request, deal_id: str, item_id: str, body: DDUpdateIn) -> JSONResponse:
    from uwhelper.origination import conditions as C

    with get_db(request).session() as conn:
        C.update_dd_item(conn, deal_id, item_id, body)
        return _json(C.get_dd_item(conn, deal_id, item_id))


@router.put("/deals/{deal_id}/closing/record")
def closing_record(request: Request, deal_id: str, body: ClosingIn) -> JSONResponse:
    from uwhelper.origination import closing as CL

    with get_db(request).session() as conn:
        CL.save_closing(conn, deal_id, body)
        return _json(CL.get_closing(conn, deal_id))


@router.post("/deals/{deal_id}/funding")
def funding_confirm(request: Request, deal_id: str, body: FundingConfirmIn) -> JSONResponse:
    from uwhelper.origination import closing as CL

    with get_db(request).session() as conn:
        CL.confirm_funding(conn, deal_id, body)
        return _json(CL.get_funding(conn, deal_id), 201)


@router.get("/deals/{deal_id}/readiness")
def deal_readiness(request: Request, deal_id: str) -> JSONResponse:
    from uwhelper.origination.closing import readiness

    with get_db(request).session() as conn:
        return _json({"items": readiness(conn, deal_id)})


@router.get("/pipeline")
def pipeline(request: Request) -> JSONResponse:
    with get_db(request).session() as conn:
        return _json(crm.pipeline_summary(conn))
