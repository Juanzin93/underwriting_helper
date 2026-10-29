"""HTML pages for deals, contacts and organizations (thin layer over uwhelper.crm.service)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.datastructures import UploadFile

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
from uwhelper.modules import get_module
from uwhelper.repo.cases import ValidationFailure
from uwhelper.web.app import get_db, redirect, render
from uwhelper.web.forms import checkbox, form_dict, read_form, validate

router = APIRouter()
ORG_TYPES = ["borrower_business", "lender", "referral_partner", "brokerage", "law_firm", "title_company",
             "accounting_firm", "insurance_agency", "appraiser", "other"]
CONTACT_TYPES = ["borrower", "guarantor", "referral_partner", "lender_contact", "attorney", "accountant", "other"]


def _clean(data: dict, allowed) -> dict:
    return {k: v for k, v in data.items() if k in allowed and v != ""}


# ------------------------------------------------------------------------------------------ deals
@router.get("/deals")
async def deals_page(request: Request):
    status = request.query_params.get("status") or None
    q = request.query_params.get("q") or None
    with get_db(request).session() as conn:
        deals = crm.list_deals(conn, status=status, q=q, limit=200)
        orgs = crm.list_organizations(conn, org_type="borrower_business", limit=200)
        contacts = crm.list_contacts(conn, limit=200)
        pipeline = crm.pipeline_summary(conn)
    return render(request, "crm/deals.html", deals=deals, orgs=orgs, contacts=contacts, pipeline=pipeline,
                  product_types=PRODUCT_TYPES, status=status, q=q or "", statuses=crm.DEAL_STATUSES)


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
    return render(request, "crm/deal_detail.html", deal=deal, contacts=contacts, orgs=orgs,
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
        elif action == "underwriting-file":
            case_id = crm.start_underwriting_file(conn, deal_id)
            return redirect(f"/cases/{case_id}", "Underwriting file created from the deal; deal documents linked.")
        else:
            raise ValidationFailure("Unknown action.")
    return redirect(f"/deals/{deal_id}", msg)


# ------------------------------------------------------------------------------------------ contacts
@router.get("/contacts")
async def contacts_page(request: Request):
    q = request.query_params.get("q") or None
    with get_db(request).session() as conn:
        contacts = crm.list_contacts(conn, q=q, limit=200)
        orgs = crm.list_organizations(conn, limit=200)
    return render(request, "crm/contacts.html", contacts=contacts, orgs=orgs, contact_types=CONTACT_TYPES, q=q or "")


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
    return render(request, "crm/contact_detail.html", c=c, orgs=orgs, contact_types=CONTACT_TYPES)


@router.post("/contacts/{contact_id}/{action}")
async def contact_action(request: Request, contact_id: str, action: str):
    data = await form_dict(request)
    with get_db(request).session() as conn:
        if action == "edit":
            patch = {k: (v if v != "" else None) for k, v in data.items() if k in ContactPatch.model_fields}
            changes = crm.update_contact(conn, contact_id, validate(ContactPatch, patch))
            msg = f"Saved ({len(changes)} field(s) changed)." if changes else "No changes."
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
    with get_db(request).session() as conn:
        orgs = crm.list_organizations(conn, q=q, org_type=org_type, limit=200)
    return render(request, "crm/organizations.html", orgs=orgs, org_types=ORG_TYPES, q=q or "", org_type=org_type)


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
    return render(request, "crm/organization_detail.html", o=o, org_types=ORG_TYPES)


@router.post("/organizations/{org_id}/edit")
async def org_edit(request: Request, org_id: str):
    data = await form_dict(request)
    patch = {k: (v if v != "" else None) for k, v in data.items() if k in OrganizationPatch.model_fields}
    with get_db(request).session() as conn:
        changes = crm.update_organization(conn, org_id, validate(OrganizationPatch, patch))
    return redirect(f"/organizations/{org_id}", f"Saved ({len(changes)} field(s) changed)." if changes else "No changes.")
