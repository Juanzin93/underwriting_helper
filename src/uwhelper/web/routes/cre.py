"""Property data for CRE underwriting files: rent rolls and operating statements (T-12)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from starlette.datastructures import UploadFile

from uwhelper.core.csvutil import decode
from uwhelper.cre import rent_roll as RR
from uwhelper.cre import t12 as T12
from uwhelper.documents.service import list_case_documents
from uwhelper.domain.money import InvalidAmount, to_decimal
from uwhelper.repo.cases import ValidationFailure
from uwhelper.web.app import get_db, redirect, render
from uwhelper.web.forms import checkbox, form_dict, read_form
from uwhelper.web.routes.cases import case_context

router = APIRouter()


def _date(v: object, what: str) -> date:
    try:
        return date.fromisoformat(str(v or "").strip())
    except ValueError:
        raise ValidationFailure(f"Enter the {what} as a date.") from None


def _amount(v: object, what: str):
    s = str(v or "").strip()
    if not s:
        return None
    try:
        d = to_decimal(s)
    except InvalidAmount as exc:
        raise ValidationFailure(f"{what}: {exc}") from None
    if d is None or d < 0:
        raise ValidationFailure(f"{what} must be zero or positive.")
    return d


def _count(v: object):
    s = str(v or "").strip()
    if not s:
        return None
    if not s.isdigit():
        raise ValidationFailure("Unit count must be a whole number.")
    return int(s)


def _ctx(conn, case_id: str) -> dict:
    ctx = case_context(conn, case_id)
    if ctx["case"]["loan_type"] != "commercial_real_estate":
        raise ValidationFailure("Rent rolls and operating statements apply to commercial real estate files.")
    ctx["docs"] = list_case_documents(conn, case_id)
    return ctx


@router.get("/cases/{case_id}/property")
async def property_page(request: Request, case_id: str):
    with get_db(request).session() as conn:
        ctx = _ctx(conn, case_id)
        rolls = RR.list_rent_rolls(conn, case_id)
        imports = T12.list_imports(conn, case_id)
    return render(request, "case/property.html", active="property", rolls=rolls, imports=imports,
                  target_labels=T12.TARGET_LABELS, **ctx)


@router.post("/cases/{case_id}/property/rent-roll/preview")
async def rent_roll_preview(request: Request, case_id: str):
    async with read_form(request) as form:
        f = form.get("file")
        if not isinstance(f, UploadFile) or not f.filename:
            raise ValidationFailure("Choose a rent roll CSV file.")
        text = decode(await f.read(1_000_001))
        fields = {k: str(form.get(k) or "") for k in ("as_of", "document_id", "stated_annual_rent", "stated_unit_count")}
        filename = f.filename
    _date(fields["as_of"], "rent roll date")
    with get_db(request).session() as conn:
        ctx = _ctx(conn, case_id)
    preview = RR.parse_rent_roll(text)
    stated = _amount(fields["stated_annual_rent"], "Stated annual rent")
    return render(request, "case/rent_roll.html", active="property", preview=preview, roll=None, csv_text=text,
                  filename=filename, fields=fields, stated=stated, tolerance=RR.TOLERANCE, **ctx)


@router.post("/cases/{case_id}/property/rent-roll")
async def rent_roll_create(request: Request, case_id: str):
    data = checkbox(await form_dict(request), "skip_invalid")
    with get_db(request).session() as conn:
        rid = RR.create_draft(conn, case_id, data.get("csv_text", ""), as_of=_date(data.get("as_of"), "rent roll date"),
                              document_id=data.get("document_id") or None, source_name=data.get("filename"),
                              stated_annual_rent=_amount(data.get("stated_annual_rent"), "Stated annual rent"),
                              stated_unit_count=_count(data.get("stated_unit_count")),
                              skip_invalid=data["skip_invalid"])
    return redirect(f"/cases/{case_id}/property/rent-roll/{rid}", "Rent roll saved as a draft.")


@router.get("/cases/{case_id}/property/rent-roll/{roll_id}")
async def rent_roll_detail(request: Request, case_id: str, roll_id: str):
    with get_db(request).session() as conn:
        ctx = _ctx(conn, case_id)
        roll = RR.get_rent_roll(conn, case_id, roll_id)
    return render(request, "case/rent_roll.html", active="property", preview=None, roll=roll,
                  summary=RR.summarize(roll), tolerance=RR.TOLERANCE, **ctx)


@router.post("/cases/{case_id}/property/rent-roll/{roll_id}/{action}")
async def rent_roll_action(request: Request, case_id: str, roll_id: str, action: str):
    data = await form_dict(request)
    back = f"/cases/{case_id}/property/rent-roll/{roll_id}"
    with get_db(request).session() as conn:
        if action == "totals":
            ok = RR.set_stated_totals(conn, case_id, roll_id, _amount(data.get("stated_annual_rent"), "Stated annual rent")
                                      or 0, _count(data.get("stated_unit_count")))
            return redirect(back, "Totals reconcile; the rent roll can be verified." if ok
                            else "Totals do not reconcile with the units.", "info" if ok else "error")
        if action == "verify":
            RR.verify(conn, case_id, roll_id, data.get("verified_by", ""))
            return redirect(back, "Rent roll verified. Re-run the screening to use it.")
        if action == "reject":
            RR.reject(conn, case_id, roll_id, data.get("reason", ""))
            return redirect(f"/cases/{case_id}/property", "Draft rent roll rejected.")
    raise ValidationFailure("Unknown action.")


@router.post("/cases/{case_id}/property/t12/preview")
async def t12_preview(request: Request, case_id: str):
    async with read_form(request) as form:
        f = form.get("file")
        if not isinstance(f, UploadFile) or not f.filename:
            raise ValidationFailure("Choose an operating statement CSV file.")
        text = decode(await f.read(1_000_001))
        fields = {k: str(form.get(k) or "") for k in ("period_end", "period_months", "document_id",
                                                       "stated_income", "stated_expenses")}
        filename = f.filename
    _date(fields["period_end"], "statement period end")
    with get_db(request).session() as conn:
        ctx = _ctx(conn, case_id)
    st = T12.parse_statement(text)
    t = T12.totals(st.lines, {})
    rec = T12.reconcile(t, _amount(fields["stated_income"], "Stated total income"),
                        _amount(fields["stated_expenses"], "Stated total expenses"))
    return render(request, "case/t12.html", active="property", statement=st, totals=t, rec=rec, csv_text=text,
                  filename=filename, fields=fields, targets=T12.TARGETS, target_labels=T12.TARGET_LABELS, **ctx)


@router.post("/cases/{case_id}/property/t12")
async def t12_commit(request: Request, case_id: str):
    data = checkbox(await form_dict(request), "confirm_verified")
    mapping = {}
    for k, v in data.items():
        if k.startswith("map_") and k[4:].isdigit():
            mapping[int(k[4:])] = v
    try:
        months = int(data.get("period_months") or 12)
    except ValueError:
        raise ValidationFailure("Months must be a whole number.") from None
    with get_db(request).session() as conn:
        res = T12.commit_statement(
            conn, case_id, data.get("csv_text", ""), period_end=_date(data.get("period_end"), "statement period end"),
            period_months=months, mapping=mapping,
            stated_income=_amount(data.get("stated_income"), "Stated total income"),
            stated_expenses=_amount(data.get("stated_expenses"), "Stated total expenses"),
            document_id=data.get("document_id") or None, source_name=data.get("filename"),
            confirm_verified=data["confirm_verified"], confirmed_by=data.get("confirmed_by"))
    msg = (f"{len(res['fact_ids'])} property total(s) saved as "
           + ("VERIFIED (statement reconciled)." if res["verified"] else "UNVERIFIED — review them on Financial data."))
    return redirect(f"/cases/{case_id}/property", msg)
