"""Dashboard, case create/edit, terms, parties, debts and collateral."""

from __future__ import annotations

from fastapi import APIRouter, Request

from uwhelper.analysis.underwriting import default_policy_key, latest_run_id
from uwhelper.domain.models import BorrowerIn, CaseIn, CollateralIn, DebtIn, PartyIn, TermsIn, ValuationIn
from uwhelper.modules import get_module
from uwhelper.policy.loader import list_policies
from uwhelper.repo import audit
from uwhelper.repo import cases as repo
from uwhelper.repo.cases import ValidationFailure
from uwhelper.web.app import get_db, redirect, render
from uwhelper.web.forms import checkbox, form_dict, validate

router = APIRouter()
MODULE = get_module("commercial")


def case_context(conn, case_id: str) -> dict:
    from uwhelper.workflow import lifecycle as L

    case = repo.get_case(conn, case_id)
    actions = [a for a in L.available_actions(conn, case_id) if a["forward"] and a["to_stage"] != "DECLINED"]
    return {"case": case, "loan_types": MODULE.loan_types, "doc_types": MODULE.document_types(),
            "latest_run_id": latest_run_id(conn, case_id), "stage_labels": L.STAGE_LABELS,
            "next_actions": actions}


@router.get("/")
async def dashboard(request: Request):
    from uwhelper.workflow import lifecycle as L

    with get_db(request).session() as conn:
        cases = repo.list_cases(conn)
        stages = L.stage_summary(conn)
    return render(request, "dashboard.html", cases=cases, loan_types=MODULE.loan_types, stages=stages,
                  stage_labels=L.STAGE_LABELS)


def _policy_options(conn) -> list[dict]:
    return list_policies(conn)


@router.get("/cases/new")
async def new_case(request: Request):
    with get_db(request).session() as conn:
        policies = _policy_options(conn)
    return render(request, "case_form.html", case=None, loan_types=MODULE.loan_types, policies=policies)


@router.post("/cases")
async def create_case(request: Request):
    data = checkbox(await form_dict(request), "is_secured")
    case_in = validate(CaseIn, {k: data.get(k) for k in ("title", "loan_type", "policy_key", "notes")})
    if not case_in.policy_key:
        case_in.policy_key = default_policy_key(case_in.loan_type)
    borrower = validate(BorrowerIn, {k[2:]: v for k, v in data.items() if k.startswith("b_")})
    terms_data = {k[2:]: v for k, v in data.items() if k.startswith("t_")}
    terms = validate(TermsIn, {**terms_data, "is_secured": data["is_secured"]}) \
        if any(str(v).strip() for v in terms_data.values()) else None
    with get_db(request).session() as conn:
        case_id = repo.create_case(conn, case_in, borrower, terms)
    return redirect(f"/cases/{case_id}", "Loan case created.")


@router.get("/cases/{case_id}")
async def case_overview(request: Request, case_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        ctx.update(parties=repo.list_parties(conn, case_id), debts=repo.list_debts(conn, case_id),
                   collateral=repo.list_collateral(conn, case_id), terms_history=repo.terms_history(conn, case_id),
                   events=audit.list_events(conn, case_id, limit=15))
        from uwhelper.crm import pipeline
        from uwhelper.crm import service as crm

        ctx["deal"] = pipeline.deal_for_case(conn, case_id)
        org_id = ctx["case"]["borrower"].get("organization_id")
        ctx["borrower_org"] = crm.get_organization(conn, org_id) if org_id else None
        ctx["all_contacts"] = crm.list_contacts(conn, limit=200)
        ctx["party_contacts"] = {c["id"]: c for c in ctx["all_contacts"]}
        from uwhelper.analysis.resolver import find_contradictions, missing_checklist

        ctx["contradiction_count"] = len(find_contradictions(conn, case_id))
        ctx["missing_count"] = sum(1 for i in missing_checklist(conn, case_id) if i["status"] in ("missing", "unverified"))
        ctx["pending_facts"] = conn.execute("SELECT COUNT(*) AS n FROM facts WHERE case_id=? AND status='candidate'",
                                            (case_id,)).fetchone()["n"]
    return render(request, "case/overview.html", active="overview", **ctx)


@router.get("/cases/{case_id}/edit")
async def edit_case(request: Request, case_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        policies = _policy_options(conn)
    return render(request, "case_form.html", policies=policies, **ctx)


@router.post("/cases/{case_id}/edit")
async def update_case(request: Request, case_id: str):
    data = await form_dict(request)
    case_in = validate(CaseIn, {k: data.get(k) for k in ("title", "loan_type", "policy_key", "policy_version", "notes")})
    borrower = validate(BorrowerIn, {k[2:]: v for k, v in data.items() if k.startswith("b_")})
    with get_db(request).session() as conn:
        repo.update_case(conn, case_id, case_in, borrower)
    return redirect(f"/cases/{case_id}", "Case updated.")


@router.post("/cases/{case_id}/status")
async def case_status(request: Request, case_id: str):
    data = await form_dict(request)
    with get_db(request).session() as conn:
        repo.set_case_status(conn, case_id, data.get("status", ""))
    return redirect(f"/cases/{case_id}", "Status updated.")


@router.post("/cases/{case_id}/terms")
async def save_terms(request: Request, case_id: str):
    data = checkbox(await form_dict(request), "is_secured")
    terms = validate(TermsIn, data)
    with get_db(request).session() as conn:
        repo.get_case(conn, case_id)
        repo.save_terms(conn, case_id, terms)
    return redirect(f"/cases/{case_id}", "Loan terms saved as a new revision (history kept).")


@router.post("/cases/{case_id}/parties")
async def add_party(request: Request, case_id: str):
    data = checkbox(await form_dict(request), "is_guarantor")
    party = validate(PartyIn, data)
    with get_db(request).session() as conn:
        repo.get_case(conn, case_id)
        repo.add_party(conn, case_id, party)
    return redirect(f"/cases/{case_id}", "Owner/guarantor added.")


@router.post("/cases/{case_id}/parties/{party_id}/contact")
async def link_party_contact(request: Request, case_id: str, party_id: str):
    from uwhelper.crm import service as crm

    data = await form_dict(request)
    with get_db(request).session() as conn:
        create = data.get("contact_id") == "__new__"
        crm.link_party_contact(conn, case_id, party_id, None if create else data.get("contact_id") or None, create=create)
    return redirect(f"/cases/{case_id}", "Owner/guarantor linked to a contact.")


@router.post("/cases/{case_id}/parties/{party_id}/delete")
async def delete_party(request: Request, case_id: str, party_id: str):
    with get_db(request).session() as conn:
        repo.delete_party(conn, case_id, party_id)
    return redirect(f"/cases/{case_id}", "Owner/guarantor removed (their personal facts were removed too).")


@router.post("/cases/{case_id}/debts")
async def add_debt(request: Request, case_id: str):
    data = checkbox(await form_dict(request), "to_be_refinanced")
    debt = validate(DebtIn, data)
    with get_db(request).session() as conn:
        repo.get_case(conn, case_id)
        repo.add_debt(conn, case_id, debt)
    return redirect(f"/cases/{case_id}/financials#debts", "Debt added.")


@router.post("/cases/{case_id}/debts/{debt_id}/status")
async def debt_status(request: Request, case_id: str, debt_id: str):
    data = await form_dict(request)
    with get_db(request).session() as conn:
        repo.set_debt_status(conn, case_id, debt_id, data.get("status", ""))
    return redirect(f"/cases/{case_id}/financials#debts", "Debt status updated.")


@router.post("/cases/{case_id}/debts/{debt_id}/delete")
async def delete_debt(request: Request, case_id: str, debt_id: str):
    with get_db(request).session() as conn:
        repo.delete_debt(conn, case_id, debt_id)
    return redirect(f"/cases/{case_id}/financials#debts", "Debt removed (audit log keeps a record).")


@router.post("/cases/{case_id}/debt-schedule")
async def confirm_debts(request: Request, case_id: str):
    data = checkbox(await form_dict(request), "confirmed")
    with get_db(request).session() as conn:
        repo.get_case(conn, case_id)
        repo.set_debt_schedule_confirmed(conn, case_id, data["confirmed"])
    return redirect(f"/cases/{case_id}/financials#debts",
                    "Debt schedule marked complete." if data["confirmed"] else "Debt schedule marked incomplete.")


@router.post("/cases/{case_id}/collateral")
async def add_collateral(request: Request, case_id: str):
    data = await form_dict(request)
    col = validate(CollateralIn, {k: data.get(k) for k in ("description", "collateral_type", "lien_position",
                                                          "prior_lien_balance")})
    val = None
    if (data.get("value") or "").strip():
        val = validate(ValuationIn, {k: data.get(k) for k in ("value", "valuation_date", "valuation_method",
                                                              "verification_status", "document_id", "page_number")})
    with get_db(request).session() as conn:
        repo.get_case(conn, case_id)
        repo.add_collateral(conn, case_id, col, val)
    return redirect(f"/cases/{case_id}/financials#collateral", "Collateral added.")


@router.post("/cases/{case_id}/collateral/{collateral_id}/valuations")
async def add_valuation(request: Request, case_id: str, collateral_id: str):
    val = validate(ValuationIn, await form_dict(request))
    with get_db(request).session() as conn:
        repo.add_valuation(conn, case_id, collateral_id, val)
    return redirect(f"/cases/{case_id}/financials#collateral", "Valuation added (earlier valuations kept).")


@router.post("/cases/{case_id}/valuations/{valuation_id}/status")
async def valuation_status(request: Request, case_id: str, valuation_id: str):
    data = await form_dict(request)
    with get_db(request).session() as conn:
        repo.set_valuation_status(conn, case_id, valuation_id, data.get("status", ""))
    return redirect(f"/cases/{case_id}/financials#collateral", "Valuation status updated.")


@router.post("/cases/{case_id}/collateral/{collateral_id}/delete")
async def delete_collateral(request: Request, case_id: str, collateral_id: str):
    with get_db(request).session() as conn:
        repo.delete_collateral(conn, case_id, collateral_id)
    return redirect(f"/cases/{case_id}/financials#collateral", "Collateral removed.")


@router.get("/cases/{case_id}/delete")
async def delete_confirm(request: Request, case_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
    return render(request, "case/delete.html", active="overview", **ctx)


@router.post("/cases/{case_id}/delete")
async def delete_case(request: Request, case_id: str):
    data = await form_dict(request)
    settings = request.app.state.settings
    with get_db(request).session() as conn:
        case = repo.get_case(conn, case_id)
        if data.get("confirm_case_number", "").strip() != case["case_number"]:
            raise ValidationFailure("Type the exact case number to confirm permanent deletion.")
        result = repo.purge_case(conn, case_id, settings.documents_dir, settings.reports_dir)
    return redirect("/", f"Case {case['case_number']} permanently deleted "
                         f"({result['documents_deleted']} document file(s), {result['reports_deleted']} report(s)).")
