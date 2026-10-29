"""Submission packages, submissions/offers, fees and commissions (thin layer over uwhelper.brokerage)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from uwhelper.brokerage import fees as F
from uwhelper.brokerage import packages as P
from uwhelper.brokerage import submissions as S
from uwhelper.repo.cases import ValidationFailure
from uwhelper.web.app import get_db, redirect, render
from uwhelper.web.forms import checkbox, form_dict, read_form, validate

router = APIRouter()


def _clean(data: dict, allowed) -> dict:
    return {k: v for k, v in data.items() if k in allowed and v != ""}


@router.post("/deals/{deal_id}/packages/build")
async def package_build(request: Request, deal_id: str):
    async with read_form(request) as form:
        program_id = str(form.get("program_id") or "")
        docs = [str(x) for x in form.getlist("document_ids")]
        memo_id = str(form.get("memo_id") or "") or None
        match_id = str(form.get("match_run_id") or "") or None
        figures = form.get("include_figures") == "on"
    with get_db(request).session() as conn:
        pid = P.build_package(conn, request.app.state.settings, deal_id, program_id, docs, memo_id=memo_id,
                              match_run_id=match_id, include_figures=figures)
    return redirect(f"/deals/{deal_id}/packages/{pid}", "Package manifest saved. Review it before exporting.")


@router.get("/deals/{deal_id}/packages/{package_id}")
async def package_page(request: Request, deal_id: str, package_id: str):
    from uwhelper.crm import service as crm

    with get_db(request).session() as conn:
        deal = crm.get_deal(conn, deal_id)
        pkg = P.get_package(conn, deal_id, package_id)
    return render(request, "brokerage/package.html", deal=deal, pkg=pkg, m=pkg["manifest"])


@router.post("/deals/{deal_id}/packages/{package_id}/export")
async def package_export(request: Request, deal_id: str, package_id: str):
    data = checkbox(await form_dict(request), "confirm")
    with get_db(request).session() as conn:
        res = P.export_package(conn, request.app.state.settings, deal_id, package_id,
                               confirmed_by=data.get("confirmed_by", ""), confirm=data["confirm"])
    return redirect(f"/deals/{deal_id}/packages/{package_id}", f"Exported {res['file_name']} to the local reports folder.")


@router.get("/deals/{deal_id}/packages/{package_id}/exports/{export_id}")
async def package_download(request: Request, deal_id: str, package_id: str, export_id: str):
    with get_db(request).session() as conn:
        path = P.export_path(conn, request.app.state.settings, deal_id, package_id, export_id)
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.post("/deals/{deal_id}/submissions/new")
async def submission_create(request: Request, deal_id: str):
    data = await form_dict(request)
    with get_db(request).session() as conn:
        sid = S.create_submission(conn, deal_id, validate(S.SubmissionIn, _clean(data, S.SubmissionIn.model_fields)))
    return redirect(f"/deals/{deal_id}/submissions/{sid}", "Submission created as a draft.")


@router.get("/deals/{deal_id}/submissions/{submission_id}")
async def submission_page(request: Request, deal_id: str, submission_id: str):
    from uwhelper.crm import service as crm

    with get_db(request).session() as conn:
        deal = crm.get_deal(conn, deal_id)
        sub = S.get_submission(conn, deal_id, submission_id)
        contacts = crm.get_organization(conn, sub["lender_id"])["contacts"]
    return render(request, "brokerage/submission.html", deal=deal, sub=sub, contacts=contacts,
                  today=date.today().isoformat())


@router.post("/deals/{deal_id}/submissions/{submission_id}/{action}")
async def submission_action(request: Request, deal_id: str, submission_id: str, action: str):
    data = await form_dict(request)
    back = f"/deals/{deal_id}/submissions/{submission_id}"
    with get_db(request).session() as conn:
        if action == "status":
            res = S.transition(conn, deal_id, submission_id,
                               validate(S.SubmissionStatusIn, _clean(data, S.SubmissionStatusIn.model_fields)))
            msg = f"Submission is now {res['status'].replace('_', ' ')}." + (
                f" Deal moved to {res['deal_status'].replace('_', ' ')}." if res["deal_status"] else "")
            return redirect(back, msg)
        if action == "offer":
            S.add_offer(conn, deal_id, submission_id, validate(S.OfferIn, _clean(data, S.OfferIn.model_fields)))
            return redirect(back, "Offer recorded exactly as entered.")
    raise ValidationFailure("Unknown action.")


@router.post("/deals/{deal_id}/offers/{offer_id}/{action}")
async def offer_action(request: Request, deal_id: str, offer_id: str, action: str):
    data = checkbox(await form_dict(request), "confirm")
    d = validate(S.DecisionIn, _clean(data, S.DecisionIn.model_fields) | {"confirm": data["confirm"]})
    with get_db(request).session() as conn:
        if action == "accept":
            res = S.accept_offer(conn, deal_id, offer_id, d)
            msg = "Offer accepted." + (f" Deal moved to {res['deal_status']}." if res["deal_status"] else "")
        elif action == "decline":
            S.decline_offer(conn, deal_id, offer_id, d)
            msg = "Offer declined."
        else:
            raise ValidationFailure("Unknown action.")
    return redirect(f"/deals/{deal_id}/offers/compare", msg)


@router.get("/deals/{deal_id}/offers/compare")
async def offers_page(request: Request, deal_id: str):
    from uwhelper.crm import service as crm

    with get_db(request).session() as conn:
        deal = crm.get_deal(conn, deal_id)
        cmp = S.compare_offers(conn, deal_id)
    return render(request, "brokerage/offers.html", deal=deal, cmp=cmp)


@router.post("/deals/{deal_id}/fees/{action}")
async def fees_action(request: Request, deal_id: str, action: str):
    data = await form_dict(request)
    back = f"/deals/{deal_id}#fees"
    with get_db(request).session() as conn:
        if action == "agreement":
            F.add_agreement(conn, deal_id, validate(F.FeeAgreementIn, _clean(data, F.FeeAgreementIn.model_fields)))
            msg = "Fee agreement added."
        elif action == "void":
            F.void_agreement(conn, deal_id, data.get("agreement_id", ""), data.get("reason", ""))
            msg = "Fee agreement voided."
        elif action == "expected":
            try:
                on = date.fromisoformat(data.get("occurred_on") or date.today().isoformat())
            except ValueError:
                raise ValidationFailure("Enter a valid date.") from None
            res = F.record_expected(conn, deal_id, on)
            msg = f"Expected commissions recorded (gross {res['gross']}, payouts {res['payouts']})."
        elif action == "entry":
            F.record_entry(conn, deal_id, validate(F.LedgerEntryIn, _clean(data, F.LedgerEntryIn.model_fields)))
            msg = "Ledger entry recorded."
        elif action == "reverse":
            F.reverse_entry(conn, deal_id, data.get("entry_id", ""), data.get("reason", ""))
            msg = "Entry reversed (both entries are kept)."
        else:
            raise ValidationFailure("Unknown action.")
    return redirect(back, msg)


@router.get("/commissions")
async def commissions_page(request: Request):
    with get_db(request).session() as conn:
        rep = F.commissions_report(conn)
    return render(request, "brokerage/commissions.html", rep=rep)
