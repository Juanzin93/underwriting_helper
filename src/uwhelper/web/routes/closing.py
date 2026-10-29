"""Deal conditions, due diligence, closing record and funding confirmation pages."""

from __future__ import annotations

from fastapi import APIRouter, Request

from uwhelper.origination import closing as CL
from uwhelper.origination import conditions as C
from uwhelper.repo.cases import ValidationFailure
from uwhelper.web.app import get_db, redirect, render
from uwhelper.web.forms import checkbox, form_dict, validate

router = APIRouter()


def _clean(data: dict, allowed) -> dict:
    return {k: v for k, v in data.items() if k in allowed and v != ""}


def closing_context(conn, deal_id: str) -> dict:
    from uwhelper.brokerage.submissions import accepted_offer
    from uwhelper.crm import service as crm

    deal = crm.get_deal(conn, deal_id)
    offer = accepted_offer(conn, deal_id)
    closing = CL.get_closing(conn, deal_id)
    return {"deal": deal, "offer": offer, "closing": closing, "funding": CL.get_funding(conn, deal_id),
            "conditions": C.list_conditions(conn, deal_id), "dd_items": C.list_dd_items(conn, deal_id),
            "readiness": CL.readiness(conn, deal_id), "differences": CL.term_differences(offer, closing),
            "vendors": crm.list_organizations(conn, limit=200), "phrase": CL.FUNDING_PHRASE, "dd_types": C.DD_TYPES}


@router.get("/deals/{deal_id}/closing")
async def closing_page(request: Request, deal_id: str):
    from datetime import date

    with get_db(request).session() as conn:
        ctx = closing_context(conn, deal_id)
    return render(request, "origination/closing.html", today=date.today().isoformat(), **ctx)


@router.post("/deals/{deal_id}/closing/{action}")
async def closing_action(request: Request, deal_id: str, action: str):
    data = checkbox(await form_dict(request), "required")
    base = f"/deals/{deal_id}/closing"
    with get_db(request).session() as conn:
        if action == "condition":
            C.add_condition(conn, deal_id, validate(C.ConditionIn, _clean(data, C.ConditionIn.model_fields)
                                                    | {"required": data["required"]}))
            return redirect(f"{base}#conditions", "Condition recorded.")
        if action == "conditions-paste":
            n = C.add_conditions_from_text(conn, deal_id, data.get("text", ""), offer_id=data.get("offer_id") or None,
                                           phase=data.get("phase") or "closing")
            return redirect(f"{base}#conditions", f"{n} condition(s) recorded.")
        if action == "dd":
            C.add_dd_item(conn, deal_id, validate(C.DDItemIn, _clean(data, C.DDItemIn.model_fields)
                                                  | {"required": data["required"]}))
            return redirect(f"{base}#diligence", "Due-diligence item added.")
        if action == "record":
            CL.save_closing(conn, deal_id, validate(CL.ClosingIn, {k: data.get(k) for k in CL.ClosingIn.model_fields}))
            return redirect(f"{base}#record", "Closing record saved.")
        if action == "fund":
            CL.confirm_funding(conn, deal_id, validate(CL.FundingConfirmIn,
                                                       _clean(data, CL.FundingConfirmIn.model_fields)))
            return redirect(f"/deals/{deal_id}", "Funding confirmed; the deal is FUNDED.")
    raise ValidationFailure("Unknown action.")


@router.post("/deals/{deal_id}/conditions/{condition_id}/update")
async def condition_update(request: Request, deal_id: str, condition_id: str):
    data = await form_dict(request)
    with get_db(request).session() as conn:
        C.update_condition(conn, deal_id, condition_id,
                           validate(C.ConditionUpdateIn, _clean(data, C.ConditionUpdateIn.model_fields)))
    return redirect(f"/deals/{deal_id}/closing#conditions", "Condition updated.")


@router.post("/deals/{deal_id}/diligence/{item_id}/update")
async def dd_update(request: Request, deal_id: str, item_id: str):
    data = await form_dict(request)
    with get_db(request).session() as conn:
        C.update_dd_item(conn, deal_id, item_id, validate(C.DDUpdateIn, _clean(data, C.DDUpdateIn.model_fields)))
    return redirect(f"/deals/{deal_id}/closing#diligence", "Due-diligence item updated.")
