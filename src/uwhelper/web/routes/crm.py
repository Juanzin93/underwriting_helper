"""HTML pages for deals, contacts and organizations (thin layer over uwhelper.crm.service)."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from starlette.datastructures import UploadFile

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
from uwhelper.modules import get_module
from uwhelper.repo.cases import ValidationFailure
from uwhelper.web.app import get_db, redirect, render
from uwhelper.web.forms import checkbox, form_dict, read_form, validate

router = APIRouter()
ORG_TYPES = ["borrower_business", "lender", "referral_partner", "brokerage", "law_firm", "title_company",
             "accounting_firm", "insurance_agency", "appraiser", "other"]
CONTACT_TYPES = ["borrower", "guarantor", "referral_partner", "lender_contact", "attorney", "accountant", "other"]


PAGE = 50


def _clean(data: dict, allowed) -> dict:
    return {k: v for k, v in data.items() if k in allowed and v != ""}


def _next(value: str | None, default: str) -> str:
    """Only local paths are allowed as redirect targets."""
    v = (value or "").strip()
    return v if v.startswith("/") and not v.startswith("//") and "\\" not in v and "\n" not in v else default


def _paging(request: Request) -> tuple[int, bool]:
    try:
        offset = max(int(request.query_params.get("offset") or 0), 0)
    except ValueError:
        offset = 0
    return offset, request.query_params.get("archived") == "1"


def _query(**kw) -> str:
    return urlencode({k: v for k, v in kw.items() if v})


# ------------------------------------------------------------------------------------------ deals
@router.get("/deals")
async def deals_page(request: Request):
    status = request.query_params.get("status") or None
    deal_type = request.query_params.get("deal_type") or None
    q = request.query_params.get("q") or None
    offset, archived = _paging(request)
    with get_db(request).session() as conn:
        deals = crm.list_deals(conn, status=status, deal_type=deal_type if deal_type in PRODUCT_TYPES else None, q=q,
                               include_archived=archived, limit=PAGE, offset=offset)
        orgs = crm.list_organizations(conn, org_type="borrower_business", limit=200)
        contacts = crm.list_contacts(conn, limit=200)
        pipeline = crm.pipeline_summary(conn)
    return render(request, "crm/deals.html", deals=deals, orgs=orgs, contacts=contacts, pipeline=pipeline,
                  product_types=PRODUCT_TYPES, status=status, deal_type=deal_type, q=q or "", statuses=crm.DEAL_STATUSES,
                  archived=archived, limit=PAGE, offset=offset,
                  base_query=_query(q=q, status=status, deal_type=deal_type, archived="1" if archived else ""))


@router.post("/deals")
async def deals_create(request: Request):
    data = _clean(await form_dict(request), DealIn.model_fields)
    with get_db(request).session() as conn:
        did = crm.create_deal(conn, validate(DealIn, data))
    return redirect(f"/deals/{did}", "Deal created in INTAKE.")


@router.get("/deals/{deal_id}")
async def deal_detail(request: Request, deal_id: str):
    with get_db(request).session() as conn:
        deal = crm.get_deal(conn, deal_id)
        contacts = crm.list_contacts(conn, limit=200)
        orgs = crm.list_organizations(conn, org_type="borrower_business", limit=200)
        unlinked = [] if deal["loan_case_id"] else crm.unlinked_underwriting_files(conn)
        extra = {"requests": docreq.list_requests(conn, deal_id), "request_summary": docreq.summary(conn, deal_id),
                 "tasks": task_svc.list_tasks(conn, view="all", deal_id=deal_id),
                 "activities": acts.list_activities(conn, deal_id=deal_id),
                 "timeline": acts.deal_timeline(conn, deal_id, 100)}
        from uwhelper.reports import deal_memo

        extra["memos"] = deal_memo.list_memos(conn, deal_id)
        from uwhelper.lenders.matching import list_match_runs

        extra["match_runs"] = list_match_runs(conn, deal_id)
    return render(request, "crm/deal_detail.html", deal=deal, contacts=contacts, orgs=orgs, unlinked_files=unlinked,
                  **extra,
                  product_types=PRODUCT_TYPES, contact_types=CONTACT_TYPES,
                  doc_types=get_module("commercial").document_types(), needs_auth=crm.NEEDS_AUTHORIZATION)


@router.post("/deals/{deal_id}/{action}")
async def deal_action(request: Request, deal_id: str, action: str):
    if action == "documents":
        async with read_form(request) as form:
            f = form.get("file")
            if not isinstance(f, UploadFile) or not f.filename:
                raise ValidationFailure("Choose a file to upload.")
            with get_db(request).session() as conn:
                try:
                    res = crm.attach_document(conn, request.app.state.settings, deal_id, f.file, f.filename,
                                              str(form.get("doc_type") or "other"))
                except UploadRejected as exc:
                    return redirect(f"/deals/{deal_id}", f"Upload rejected: {exc}", "error")
        msg = "Identical file already stored; linked to this deal." if res["duplicate"] else f"Uploaded ({res['status']})."
        return redirect(f"/deals/{deal_id}", msg)
    data = await form_dict(request)
    with get_db(request).session() as conn:
        if action == "edit":
            patch = {k: (v if v != "" else None) for k, v in data.items() if k in DealPatch.model_fields}
            changes = crm.update_deal(conn, deal_id, validate(DealPatch, patch))
            msg = f"Saved ({len(changes)} field(s) changed)." if changes else "No changes."
        elif action == "status":
            checkbox(data, "confirm")
            new = crm.transition_deal(conn, deal_id, validate(DealStatusIn, _clean(data, DealStatusIn.model_fields)
                                                              | {"confirm": data["confirm"]}))
            msg = f"Deal moved to {new}."
        elif action == "contacts":
            crm.add_deal_contact(conn, deal_id, validate(DealContactIn, _clean(data, DealContactIn.model_fields)))
            msg = "Contact added to the deal."
        elif action == "remove-contact":
            crm.remove_deal_contact(conn, deal_id, data.get("contact_id", ""), data.get("role", ""))
            msg = "Contact removed from the deal."
        elif action in ("archive", "unarchive"):
            crm.update_deal(conn, deal_id, DealPatch(archived=action == "archive"))
            msg = "Deal archived (hidden from lists; nothing deleted)." if action == "archive" else "Deal restored."
        elif action == "memo":
            from uwhelper.reports import deal_memo

            out = deal_memo.generate(conn, deal_id)
            return redirect(f"/deals/{deal_id}#memos", f"Deal credit memo version {out['version']} generated.")
        elif action == "generate-requests":
            n = docreq.generate_requests(conn, deal_id)
            msg = f"{n} checklist item(s) added." if n else "All checklist items are already on the deal."
        elif action == "add-request":
            checkbox(data, "required")
            docreq.add_request(conn, deal_id, validate(DocumentRequestIn, _clean(data, DocumentRequestIn.model_fields)
                                                       | {"required": data["required"]}))
            msg = "Document requested."
        elif action == "link-underwriting-file":
            crm.link_underwriting_file(conn, deal_id, data.get("case_id", ""))
            msg = "Existing underwriting file linked; deal documents copied into it."
        elif action == "underwriting-file":
            case_id = crm.start_underwriting_file(conn, deal_id)
            return redirect(f"/cases/{case_id}", "Underwriting file created from the deal; deal documents linked.")
        else:
            raise ValidationFailure("Unknown action.")
    return redirect(f"/deals/{deal_id}", msg)


@router.get("/deals/{deal_id}/memos/{memo_id}/{fmt}")
async def deal_memo_file(request: Request, deal_id: str, memo_id: str, fmt: str):
    from fastapi.responses import HTMLResponse, Response

    from uwhelper.reports import deal_memo
    from uwhelper.reports.builder import render_pdf
    from uwhelper.web.routes.reports import REPORT_CSP

    with get_db(request).session() as conn:
        memo = deal_memo.get_memo(conn, deal_id, memo_id)
        if fmt == "verify":
            r = deal_memo.verify(conn, deal_id, memo_id)
            ok = r["context_intact"] and r["html_reproduced"] and r["stored_html_intact"]
            return redirect(f"/deals/{deal_id}#memos", f"Memo v{memo['version']}: " + (
                "stored context intact and re-rendering matches exactly." if ok else
                f"context {'intact' if r['context_intact'] else 'ALTERED'}; re-rendering "
                f"{'matches' if r['html_reproduced'] else 'differs (template changed since)'}."), "info" if ok else "warn")
    if fmt == "html":
        return HTMLResponse(memo["html"], headers={"Content-Security-Policy": REPORT_CSP})
    if fmt == "pdf":
        return Response(render_pdf(memo["html"]), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="deal_memo_v{memo["version"]}.pdf"'})
    raise ValidationFailure("Unknown format.")


@router.post("/deals/{deal_id}/requests/{request_id}/{action}")
async def request_action(request: Request, deal_id: str, request_id: str, action: str):
    back = f"/deals/{deal_id}#documents"
    if action == "upload":
        async with read_form(request) as form:
            f = form.get("file")
            if not isinstance(f, UploadFile) or not f.filename:
                raise ValidationFailure("Choose a file to upload.")
            with get_db(request).session() as conn:
                try:
                    docreq.fulfil_with_upload(conn, request.app.state.settings, deal_id, request_id, f.file, f.filename)
                except UploadRejected as exc:
                    return redirect(back, f"Upload rejected: {exc}", "error")
        return redirect(back, "Document received; review it to accept or reject.")
    data = await form_dict(request)
    with get_db(request).session() as conn:
        if action == "link":
            docreq.fulfil_with_existing(conn, deal_id, request_id, data.get("document_id", ""))
            msg = "Existing deal document linked; review it to accept or reject."
        elif action == "review":
            docreq.review_request(conn, deal_id, request_id, validate(DocumentReviewIn, _clean(data, DocumentReviewIn.model_fields)))
            msg = f"Item {data.get('decision')}."
        else:
            raise ValidationFailure("Unknown action.")
    return redirect(back, msg)


# ------------------------------------------------------------------------------------------ tasks & activities
@router.get("/tasks")
async def tasks_page(request: Request):
    view = request.query_params.get("view") or "open"
    view = view if view in task_svc.VIEWS else "open"
    assignee = request.query_params.get("assignee") or None
    with get_db(request).session() as conn:
        tasks = task_svc.list_tasks(conn, view=view, assignee=assignee, limit=300)
        counts = task_svc.task_counts(conn)
        deals = crm.list_deals(conn, limit=200)
    return render(request, "crm/tasks.html", tasks=tasks, counts=counts, view=view, views=task_svc.VIEWS, deals=deals,
                  assignee=assignee or "")


@router.post("/tasks")
async def tasks_create(request: Request):
    data = await form_dict(request)
    back = _next(data.get("next"), "/tasks")
    with get_db(request).session() as conn:
        task_svc.create_task(conn, validate(TaskIn, _clean(data, TaskIn.model_fields)))
    return redirect(back, "Task added.")


@router.post("/tasks/{task_id}/{action}")
async def task_action(request: Request, task_id: str, action: str):
    data = await form_dict(request)
    back = _next(data.get("next"), "/tasks")
    status = {"done": "done", "cancel": "cancelled", "reopen": "open"}.get(action)
    with get_db(request).session() as conn:
        if status:
            task_svc.set_task_status(conn, task_id, status)
            msg = {"done": "Task completed.", "cancelled": "Task cancelled.", "open": "Task reopened."}[status]
        elif action == "edit":
            task_svc.update_task(conn, task_id, validate(TaskPatch, {k: (v or None) for k, v in data.items()
                                                                     if k in TaskPatch.model_fields}))
            msg = "Task updated."
        else:
            raise ValidationFailure("Unknown action.")
    return redirect(back, msg)


@router.post("/activities")
async def activities_create(request: Request):
    data = await form_dict(request)
    back = _next(data.get("next"), "/deals")
    with get_db(request).session() as conn:
        acts.add_activity(conn, validate(ActivityIn, _clean(data, ActivityIn.model_fields)))
    return redirect(back, "Saved.")


# ------------------------------------------------------------------------------------------ CSV import
@router.get("/import")
async def import_page(request: Request):
    return render(request, "crm/import.html", preview=None, kind=request.query_params.get("kind") or "contacts",
                  max_rows=importer.MAX_ROWS)


@router.post("/import/preview")
async def import_preview(request: Request):
    async with read_form(request) as form:
        kind = str(form.get("kind") or "contacts")
        f = form.get("file")
        if not isinstance(f, UploadFile) or not f.filename:
            raise ValidationFailure("Choose a CSV file.")
        text = importer.decode(await f.read(importer.MAX_BYTES + 1))
        filename = f.filename
    with get_db(request).session() as conn:
        preview = importer.parse(conn, kind, text)  # type: ignore[arg-type]
    return render(request, "crm/import.html", preview=preview, kind=kind, csv_text=text, filename=filename,
                  max_rows=importer.MAX_ROWS)


@router.post("/import/commit")
async def import_commit(request: Request):
    data = checkbox(await form_dict(request), "skip_invalid")
    kind = data.get("kind", "contacts")
    with get_db(request).session() as conn:
        res = importer.commit(conn, kind, data.get("csv_text", ""), skip_invalid=data["skip_invalid"],  # type: ignore[arg-type]
                              source_name=data.get("filename"))
    return redirect(f"/{kind}", f"Imported {res['created']} {kind}; skipped {res['skipped_existing']} existing, "
                                f"{res['duplicates']} duplicate and {res['invalid_skipped']} invalid row(s).")


# ------------------------------------------------------------------------------------------ contacts
@router.get("/contacts")
async def contacts_page(request: Request):
    q = request.query_params.get("q") or None
    ctype = request.query_params.get("contact_type") or None
    offset, archived = _paging(request)
    with get_db(request).session() as conn:
        contacts = crm.list_contacts(conn, q=q, contact_type=ctype if ctype in CONTACT_TYPES else None,
                                     include_archived=archived, limit=PAGE, offset=offset)
        orgs = crm.list_organizations(conn, limit=200)
    return render(request, "crm/contacts.html", contacts=contacts, orgs=orgs, contact_types=CONTACT_TYPES, q=q or "",
                  contact_type=ctype, archived=archived, limit=PAGE, offset=offset,
                  base_query=_query(q=q, contact_type=ctype, archived="1" if archived else ""))


@router.post("/contacts")
async def contacts_create(request: Request):
    data = _clean(await form_dict(request), ContactIn.model_fields)
    with get_db(request).session() as conn:
        cid = crm.create_contact(conn, validate(ContactIn, data))
    return redirect(f"/contacts/{cid}", "Contact created.")


@router.get("/contacts/{contact_id}")
async def contact_detail(request: Request, contact_id: str):
    with get_db(request).session() as conn:
        c = crm.get_contact(conn, contact_id)
        orgs = crm.list_organizations(conn, limit=200)
        tasks = task_svc.list_tasks(conn, view="all", contact_id=contact_id)
        activities = acts.list_activities(conn, contact_id=contact_id)
    return render(request, "crm/contact_detail.html", c=c, orgs=orgs, contact_types=CONTACT_TYPES, tasks=tasks,
                  activities=activities)


@router.post("/contacts/{contact_id}/{action}")
async def contact_action(request: Request, contact_id: str, action: str):
    data = await form_dict(request)
    with get_db(request).session() as conn:
        if action == "edit":
            patch = {k: (v if v != "" else None) for k, v in data.items() if k in ContactPatch.model_fields}
            changes = crm.update_contact(conn, contact_id, validate(ContactPatch, patch))
            msg = f"Saved ({len(changes)} field(s) changed)." if changes else "No changes."
        elif action in ("archive", "unarchive"):
            crm.update_contact(conn, contact_id, ContactPatch(archived=action == "archive"))
            msg = "Contact archived (hidden from lists; nothing deleted)." if action == "archive" else "Contact restored."
        elif action == "link":
            checkbox(data, "is_primary")
            crm.link_contact_organization(conn, contact_id, data.get("organization_id", ""), data.get("role", "member"),
                                          data["is_primary"])
            msg = "Linked to organization."
        else:
            raise ValidationFailure("Unknown action.")
    return redirect(f"/contacts/{contact_id}", msg)


# ------------------------------------------------------------------------------------------ organizations
@router.get("/organizations")
async def orgs_page(request: Request):
    q = request.query_params.get("q") or None
    org_type = request.query_params.get("org_type") or None
    org_type = org_type if org_type in ORG_TYPES else None
    offset, archived = _paging(request)
    with get_db(request).session() as conn:
        orgs = crm.list_organizations(conn, q=q, org_type=org_type, include_archived=archived, limit=PAGE, offset=offset)
    return render(request, "crm/organizations.html", orgs=orgs, org_types=ORG_TYPES, q=q or "", org_type=org_type,
                  archived=archived, limit=PAGE, offset=offset,
                  base_query=_query(q=q, org_type=org_type, archived="1" if archived else ""))


@router.post("/organizations")
async def orgs_create(request: Request):
    data = _clean(await form_dict(request), OrganizationIn.model_fields)
    with get_db(request).session() as conn:
        oid = crm.create_organization(conn, validate(OrganizationIn, data))
    return redirect(f"/organizations/{oid}", "Organization created.")


@router.get("/organizations/{org_id}")
async def org_detail(request: Request, org_id: str):
    with get_db(request).session() as conn:
        o = crm.get_organization(conn, org_id)
        tasks = task_svc.list_tasks(conn, view="all", organization_id=org_id)
        activities = acts.list_activities(conn, organization_id=org_id)
        from uwhelper.lenders.service import list_programs

        programs = list_programs(conn, organization_id=org_id, include_inactive=True) if o["org_type"] == "lender" else []
        files = [dict(r) for r in conn.execute(
            """SELECT c.id, c.case_number, c.title, c.stage FROM borrowers b JOIN loan_cases c ON c.id=b.case_id
               WHERE b.organization_id=? ORDER BY c.created_at DESC""", (org_id,))]
    return render(request, "crm/organization_detail.html", o=o, org_types=ORG_TYPES, tasks=tasks,
                  activities=activities, files=files, programs=programs)


@router.post("/organizations/{org_id}/{action}")
async def org_edit(request: Request, org_id: str, action: str):
    if action in ("archive", "unarchive"):
        with get_db(request).session() as conn:
            crm.update_organization(conn, org_id, OrganizationPatch(archived=action == "archive"))
        return redirect(f"/organizations/{org_id}", "Organization archived (hidden from lists; nothing deleted)."
                        if action == "archive" else "Organization restored.")
    if action != "edit":
        raise ValidationFailure("Unknown action.")
    data = await form_dict(request)
    patch = {k: (v if v != "" else None) for k, v in data.items() if k in OrganizationPatch.model_fields}
    with get_db(request).session() as conn:
        changes = crm.update_organization(conn, org_id, validate(OrganizationPatch, patch))
    return redirect(f"/organizations/{org_id}", f"Saved ({len(changes)} field(s) changed)." if changes else "No changes.")
