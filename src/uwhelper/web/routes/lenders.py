"""Lender catalog pages and deal matching (thin layer over uwhelper.lenders)."""

from __future__ import annotations

from datetime import date

import yaml
from fastapi import APIRouter, Request
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from uwhelper.core.csvutil import decode
from uwhelper.crm.models import PRODUCT_TYPES
from uwhelper.lenders import importer as imp
from uwhelper.lenders import matching
from uwhelper.lenders import service as svc
from uwhelper.lenders.models import (
    CriteriaVersionIn,
    ProgramIn,
    ProgramPatch,
    known_subjects,
)
from uwhelper.repo.cases import ValidationFailure
from uwhelper.web.app import get_db, redirect, render
from uwhelper.web.forms import form_dict, read_form, validate

router = APIRouter()
ALL_PRODUCTS = sorted({p for ps in PRODUCT_TYPES.values() for p in ps})


def _list(form, key: str) -> list[str]:
    return [str(x) for x in form.getlist(key) if str(x).strip()]


def _states(raw: str) -> list[str]:
    return [s.strip() for s in (raw or "").replace(";", ",").split(",") if s.strip()]


def _program_fields(form) -> dict:
    return {"deal_types": _list(form, "deal_types"), "product_types": _list(form, "product_types"),
            "states": _states(str(form.get("states") or "")),
            "min_amount": str(form.get("min_amount") or "").replace(",", "") or None,
            "max_amount": str(form.get("max_amount") or "").replace(",", "") or None,
            "notes": str(form.get("notes") or "") or None}


@router.get("/lenders")
async def lenders_page(request: Request):
    from uwhelper.crm import service as crm

    show_all = request.query_params.get("inactive") == "1"
    with get_db(request).session() as conn:
        programs = svc.list_programs(conn, include_inactive=show_all)
        orgs = crm.list_organizations(conn, org_type="lender", limit=200)
    max_age = request.app.state.settings.criteria_max_age_days
    for p in programs:
        p["age_days"] = svc.age_days(p["latest_verified_at"]) if p["latest_verified_at"] else None
        p["stale"] = p["age_days"] is not None and p["age_days"] > max_age
    return render(request, "lenders/lenders.html", programs=programs, orgs=orgs, products=ALL_PRODUCTS,
                  max_age=max_age, show_all=show_all)


@router.post("/lenders/programs")
async def program_create(request: Request):
    async with read_form(request) as form:
        fields = _program_fields(form)
        org_id = str(form.get("organization_id") or "")
        name = str(form.get("name") or "")
    with get_db(request).session() as conn:
        pid = svc.create_program(conn, validate(ProgramIn, {"organization_id": org_id, "name": name, **fields}))
    return redirect(f"/lenders/programs/{pid}", "Program created. Add its criteria next.")


@router.get("/lenders/programs/{program_id}")
async def program_page(request: Request, program_id: str):
    with get_db(request).session() as conn:
        p = svc.get_program(conn, program_id)
    max_age = request.app.state.settings.criteria_max_age_days
    current_yaml = yaml.safe_dump(p["current"]["criteria"] if p["current"] else [
        {"id": "DSCR_MIN", "description": "Business DSCR at least 1.25x", "subject": "metric:dscr", "op": ">=",
         "value": "1.25", "required": True}], sort_keys=False, allow_unicode=True)
    subjects = {k: sorted(v) for k, v in known_subjects().items()}
    return render(request, "lenders/program.html", p=p, products=ALL_PRODUCTS, max_age=max_age,
                  age=svc.age_days(p["current"]["verified_at"]) if p["current"] else None, current_yaml=current_yaml,
                  subjects=subjects, today=date.today().isoformat())


@router.post("/lenders/programs/{program_id}/{action}")
async def program_action(request: Request, program_id: str, action: str):
    back = f"/lenders/programs/{program_id}"
    async with read_form(request) as form:
        if action == "edit":
            fields = _program_fields(form) | {"name": str(form.get("name") or "") or None,
                                              "active": form.get("active") == "on"}
            with get_db(request).session() as conn:
                changed = svc.update_program(conn, program_id, validate(ProgramPatch, fields))
            return redirect(back, f"Saved ({len(changed)} field(s) changed)." if changed else "No changes.")
        if action not in ("version", "version-csv"):
            raise ValidationFailure("Unknown action.")
        meta = {k: str(form.get(k) or "") or None for k in ("source", "verified_at", "verified_by", "effective_date",
                                                              "notes")}
        if action == "version":
            try:
                criteria = yaml.safe_load(str(form.get("criteria_yaml") or ""))
            except yaml.YAMLError as exc:
                raise ValidationFailure(f"Criteria are not valid YAML: {str(exc)[:200]}") from None
            if not isinstance(criteria, list):
                raise ValidationFailure("Criteria must be a YAML list (one '- id: …' entry per criterion).")
        else:
            f = form.get("file")
            if not isinstance(f, UploadFile) or not f.filename:
                raise ValidationFailure("Choose a criteria CSV file.")
            parsed, errors = imp.parse_criteria_csv(decode(await f.read(1_000_001)))
            if errors:
                raise ValidationFailure("Criteria CSV has errors: " + " | ".join(errors[:5]))
            criteria = [c.model_dump() for c in parsed]
    try:
        v = CriteriaVersionIn(criteria=criteria, **{k: x for k, x in meta.items() if x is not None})
    except ValidationError as exc:
        raise ValidationFailure("; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                                          for e in exc.errors()[:6])) from None
    with get_db(request).session() as conn:
        res = svc.add_version(conn, program_id, v)
    return redirect(back, f"Criteria version {res['version']} saved." if res["created"]
                    else f"Identical to version {res['version']}; nothing changed.")


@router.get("/lenders/import")
async def import_page(request: Request):
    return render(request, "lenders/import.html", plan=None)


@router.post("/lenders/import/preview")
async def import_preview(request: Request):
    async with read_form(request) as form:
        f = form.get("file")
        if not isinstance(f, UploadFile) or not f.filename:
            raise ValidationFailure("Choose a YAML catalog file.")
        text = decode(await f.read(1_000_001))
    with get_db(request).session() as conn:
        plan = imp.plan_yaml(conn, text)
    return render(request, "lenders/import.html", plan=plan, yaml_text=text)


@router.post("/lenders/import/commit")
async def import_commit(request: Request):
    data = await form_dict(request)
    with get_db(request).session() as conn:
        out = imp.commit_yaml(conn, data.get("yaml_text", ""))
    return redirect("/lenders", f"Imported: {out['lenders_created']} lender(s), {out['programs_created']} new and "
                                f"{out['programs_updated']} updated program(s), {out['versions_added']} criteria version(s).")


@router.post("/deals/{deal_id}/matches/run")
async def deal_match(request: Request, deal_id: str):
    with get_db(request).session() as conn:
        mid = matching.run_matching(conn, deal_id, max_age_days=request.app.state.settings.criteria_max_age_days)
    return redirect(f"/deals/{deal_id}/matches/{mid}", "Matching saved.")


@router.get("/deals/{deal_id}/matches/{match_id}")
async def deal_match_page(request: Request, deal_id: str, match_id: str):
    from uwhelper.crm import service as crm

    with get_db(request).session() as conn:
        deal = crm.get_deal(conn, deal_id)
        m = matching.get_match_run(conn, deal_id, match_id)
    return render(request, "lenders/matches.html", deal=deal, m=m, r=m["results"], fit_text=matching.FIT_TEXT)
