"""Workflow, credit memo, structuring, credit decision, conditions, closing and funding pages."""

from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from uwhelper import structuring as S
from uwhelper.analysis.underwriting import list_runs
from uwhelper.llm.client import client_from_settings
from uwhelper.llm.narrative import generate_narrative
from uwhelper.policy.schema import CONDITION_CATEGORIES
from uwhelper.reports import memo as M
from uwhelper.repo.cases import NotFound, ValidationFailure
from uwhelper.web.app import get_db, redirect, render
from uwhelper.web.forms import checkbox, form_dict, validate
from uwhelper.web.routes.cases import case_context
from uwhelper.web.routes.reports import REPORT_CSP
from uwhelper.workflow import closing as CL
from uwhelper.workflow import conditions as C
from uwhelper.workflow import decisions as D
from uwhelper.workflow import lifecycle as L
from uwhelper.workflow.currency import run_status
from uwhelper.workflow.lifecycle import TransitionRefused

router = APIRouter()
TERM_FIELDS = ("principal", "annual_rate", "rate_type", "rate_index_note", "term_months", "amortization_months",
               "interest_only_months", "payment_frequency", "origination_fee", "other_costs", "entered_by", "notes",
               "source_structure_id")


def _terms_from(data: dict, prefix: str = "t_") -> D.TermSetIn:
    return validate(D.TermSetIn, {k: data[prefix + k] for k in TERM_FIELDS if prefix + k in data})


def _refused(request: Request, case_id: str, exc: TransitionRefused, back: str):
    return redirect(back, str(exc), "error")


# ------------------------------------------------------------------------------------------ workflow
@router.get("/cases/{case_id}/workflow")
async def workflow(request: Request, case_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        ctx.update(actions=L.available_actions(conn, case_id), history=L.history(conn, case_id),
                   run_state=run_status(conn, case_id), pipeline=L.PIPELINE, labels=L.STAGE_LABELS,
                   exception_stages=L.EXCEPTION_STAGES)
    return render(request, "case/workflow.html", active="workflow", **ctx)


@router.post("/cases/{case_id}/workflow/transition")
async def do_transition(request: Request, case_id: str):
    data = checkbox(await form_dict(request), "override")
    with get_db(request).session() as conn:
        try:
            new = L.transition(conn, case_id, data.get("to_stage", ""), data.get("reason", ""), override=data["override"])
        except TransitionRefused as exc:
            return _refused(request, case_id, exc, f"/cases/{case_id}/workflow")
    return redirect(f"/cases/{case_id}/workflow", f"Moved to {L.STAGE_LABELS.get(new, new)}.")


# ------------------------------------------------------------------------------------------ credit memo
@router.get("/cases/{case_id}/memo")
async def memo_page(request: Request, case_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        ctx.update(memos=M.list_memos(conn, case_id), run_state=run_status(conn, case_id),
                   runs=[r for r in list_runs(conn, case_id) if r["mode"] == "verified_only" and not r["scenario_name"]])
    return render(request, "case/memo.html", active="memo", opinions=M.OPINIONS, **ctx)


@router.post("/cases/{case_id}/memo")
async def memo_generate(request: Request, case_id: str):
    data = checkbox(await form_dict(request), "include_narrative", "include_pdf")
    settings = request.app.state.settings
    analyst = {k: data.get(f"analyst_{k}", "") for k in ("summary", "business_profile", "industry_risks", "mitigants")}
    with get_db(request).session() as conn:
        run_id = data.get("run_id")
        if not run_id:
            raise ValidationFailure("Choose the underwriting run the memo is based on.")
        narrative = None
        if data["include_narrative"]:
            from uwhelper.analysis.underwriting import get_run

            with client_from_settings(settings) as client:
                narrative = generate_narrative(client, get_run(conn, run_id, case_id))
        out = M.generate_memo(conn, settings, case_id, run_id, analyst=analyst, narrative=narrative,
                              pdf=data["include_pdf"])
    return redirect(f"/cases/{case_id}/memo", f"Credit memo version {out['version']} generated ({out['opinion_code']}).")


@router.get("/cases/{case_id}/memo/{memo_id}/{fmt}")
async def memo_file(request: Request, case_id: str, memo_id: str, fmt: str):
    settings = request.app.state.settings
    with get_db(request).session() as conn:
        memo = M.get_memo(conn, case_id, memo_id)
        if fmt == "verify":
            result = M.verify_reproduction(conn, case_id, memo_id)
            ok = result["context_intact"] and result["html_reproduced"]
            return redirect(f"/cases/{case_id}/memo", f"Memo v{memo['version']}: stored inputs "
                            f"{'intact' if result['context_intact'] else 'MODIFIED'}; re-rendered HTML "
                            f"{'matches' if result['html_reproduced'] else 'differs'}.", "info" if ok else "warn")
    name = memo["html_file"] if fmt == "html" else memo["pdf_file"] if fmt == "pdf" else None
    if not name:
        raise NotFound("File not available")
    path = (settings.reports_dir / case_id / name).resolve()
    if settings.reports_dir.resolve() not in path.parents or not path.is_file():
        raise NotFound("Memo file is missing from the data directory")
    if fmt == "html":
        return HTMLResponse(path.read_text(encoding="utf-8"), headers={"Content-Security-Policy": REPORT_CSP})
    return FileResponse(path, media_type="application/pdf", filename=name,
                        content_disposition_type="inline" if request.query_params.get("inline") else "attachment")


# ------------------------------------------------------------------------------------------ structuring
@router.get("/cases/{case_id}/structuring")
async def structuring_page(request: Request, case_id: str):
    show_archived = request.query_params.get("archived") == "1"
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        structures = S.list_structures(conn, case_id, include_archived=show_archived)
        ctx.update(structures=structures, comparison=S.compare(structures) if structures else [],
                   run_state=run_status(conn, case_id), show_archived=show_archived,
                   latest_decision=D.latest_decision(conn, case_id))
    return render(request, "case/structuring.html", active="structuring", **ctx)


@router.post("/cases/{case_id}/structuring")
async def structuring_create(request: Request, case_id: str):
    data = checkbox(await form_dict(request), "fees_financed")
    fields = set(S.StructureIn.model_fields)
    s = validate(S.StructureIn, {k: v for k, v in data.items() if k in fields and v != ""})
    with get_db(request).session() as conn:
        S.create_structure(conn, case_id, s)
    return redirect(f"/cases/{case_id}/structuring", "Structure saved and calculated. Requested terms were not changed.")


@router.post("/cases/{case_id}/structuring/baseline")
async def structuring_baseline(request: Request, case_id: str):
    with get_db(request).session() as conn:
        S.baseline_from_requested(conn, case_id)
    return redirect(f"/cases/{case_id}/structuring", "Baseline created from the requested terms.")


@router.post("/cases/{case_id}/structuring/{structure_id}/{action}")
async def structuring_action(request: Request, case_id: str, structure_id: str, action: str):
    with get_db(request).session() as conn:
        if action == "recalculate":
            S.recalculate(conn, case_id, structure_id)
            msg = "Recalculated with the latest verified run (earlier calculation kept)."
        elif action in ("proposed", "draft", "archived"):
            S.set_status(conn, case_id, structure_id, action)
            msg = f"Structure marked {action}."
        else:
            raise ValidationFailure("Unknown action.")
    return redirect(f"/cases/{case_id}/structuring", msg)


@router.get("/cases/{case_id}/structuring/{structure_id}")
async def structuring_detail(request: Request, case_id: str, structure_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        s = S.get_structure(conn, case_id, structure_id)
    return render(request, "case/structure_detail.html", active="structuring", s=s, **ctx)


# ------------------------------------------------------------------------------------------ decision
@router.get("/cases/{case_id}/decision")
async def decision_page(request: Request, case_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        rs = run_status(conn, case_id)
        ctx.update(run_state=rs, decisions=D.list_decisions(conn, case_id), term_sets=D.term_sets(conn, case_id),
                   memos=M.list_memos(conn, case_id), structures=S.list_structures(conn, case_id),
                   latest_decision=D.latest_decision(conn, case_id))
        prefill = request.query_params.get("from_structure")
        ctx["prefill"] = S.get_structure(conn, case_id, prefill)["inputs"] if prefill else None
        ctx["prefill_id"] = prefill
    return render(request, "case/decision.html", active="decision", **ctx)


@router.post("/cases/{case_id}/decision")
async def decision_record(request: Request, case_id: str):
    data = checkbox(await form_dict(request), "confirm_human")
    d = validate(D.DecisionIn, {k: data.get(k) for k in ("decision", "decided_by", "authority_note", "rationale",
                                                         "conditions_text", "confirm_human")})
    approved = _terms_from({**data, "t_entered_by": data.get("decided_by", "")}) if d.decision in D.APPROVALS else None
    with get_db(request).session() as conn:
        D.record_decision(conn, case_id, d, approved)
    return redirect(f"/cases/{case_id}/decision", f"Human credit decision recorded: {d.decision}.")


@router.post("/cases/{case_id}/decision/approved-terms")
async def approved_terms(request: Request, case_id: str):
    data = await form_dict(request)
    with get_db(request).session() as conn:
        D.record_approved_terms(conn, case_id, _terms_from(data))
    return redirect(f"/cases/{case_id}/decision", "Approved terms revised (previous revision kept).")


# ------------------------------------------------------------------------------------------ conditions
@router.get("/cases/{case_id}/conditions")
async def conditions_page(request: Request, case_id: str):
    phase = request.query_params.get("phase") or None
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        from uwhelper.documents.service import list_case_documents

        ctx.update(conditions=C.list_conditions(conn, case_id, phase), summary=C.summary(conn, case_id),
                   docs=list_case_documents(conn, case_id), categories=CONDITION_CATEGORIES, phase=phase)
        cid = request.query_params.get("history")
        ctx["history"] = C.condition_history(conn, case_id, cid) if cid else None
        ctx["history_id"] = cid
    return render(request, "case/conditions.html", active="conditions", **ctx)


@router.post("/cases/{case_id}/conditions/generate")
async def conditions_generate(request: Request, case_id: str):
    with get_db(request).session() as conn:
        r = C.generate_from_policy(conn, case_id)
    return redirect(f"/cases/{case_id}/conditions", f"{r['created']} condition(s) created from policy; "
                    f"{r['existing']} already existed; {r['not_applicable']} not applicable to this loan.")


@router.post("/cases/{case_id}/conditions")
async def conditions_add(request: Request, case_id: str):
    data = checkbox(await form_dict(request), "required", "waivable")
    c = validate(C.ConditionIn, data)
    with get_db(request).session() as conn:
        C.add_manual(conn, case_id, c)
    return redirect(f"/cases/{case_id}/conditions", f"Condition {c.code} added.")


@router.post("/cases/{case_id}/conditions/{condition_id}/{action}")
async def conditions_action(request: Request, case_id: str, condition_id: str, action: str):
    data = checkbox(await form_dict(request), "verified", "confirm")
    back = data.get("next") if (data.get("next") or "").startswith(f"/cases/{case_id}") else f"/cases/{case_id}/conditions"
    with get_db(request).session() as conn:
        if action == "status":
            C.set_status(conn, case_id, condition_id, data.get("to_status", ""), note=data.get("note"),
                         document_id=data.get("document_id") or None, evidence_note=data.get("evidence_note"),
                         verified=data["verified"])
            msg = f"Status changed to {data.get('to_status')}."
        elif action == "waive":
            C.waive(conn, case_id, condition_id, waived_by=data.get("waived_by", ""), reason=data.get("reason", ""),
                    confirm=data["confirm"])
            msg = "Waiver recorded with the approver and explanation."
        elif action == "details":
            C.update_details(conn, case_id, condition_id, due_date=data.get("due_date"),
                             responsible_party=data.get("responsible_party"), reviewer_notes=data.get("reviewer_notes"))
            msg = "Condition details updated."
        else:
            raise ValidationFailure("Unknown action.")
    return redirect(back, msg)


# ------------------------------------------------------------------------------------------ closing & funding
@router.get("/cases/{case_id}/closing")
async def closing_page(request: Request, case_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        from uwhelper.documents.service import list_case_documents

        ctx.update(closing=CL.get_closing(conn, case_id), items=C.list_conditions(conn, case_id, "CLOSING"),
                   approved=D.latest_terms(conn, case_id, "approved"), executed=D.term_sets(conn, case_id, "executed"),
                   blockers=L.funding_blockers(conn, case_id) if ctx["case"]["stage"] == "CLOSING" else [],
                   funding=CL.get_funding(conn, case_id), docs=list_case_documents(conn, case_id),
                   dd_summary=C.summary(conn, case_id))
    return render(request, "case/closing.html", active="closing", **ctx)


@router.post("/cases/{case_id}/closing")
async def closing_save(request: Request, case_id: str):
    data = await form_dict(request)
    c = validate(CL.ClosingIn, {k: data.get(k) for k in CL.ClosingIn.model_fields if k in data})
    with get_db(request).session() as conn:
        CL.save_closing(conn, case_id, c)
    return redirect(f"/cases/{case_id}/closing", "Closing workspace saved.")


@router.post("/cases/{case_id}/closing/executed-terms")
async def executed_terms(request: Request, case_id: str):
    data = await form_dict(request)
    with get_db(request).session() as conn:
        D.record_executed_terms(conn, case_id, _terms_from(data))
    return redirect(f"/cases/{case_id}/closing", "Executed terms recorded (approved terms unchanged).")


@router.get("/cases/{case_id}/funding")
async def funding_page(request: Request, case_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        ctx.update(blockers=L.funding_blockers(conn, case_id) if ctx["case"]["stage"] == "CLOSING" else [],
                   executed=D.latest_terms(conn, case_id, "executed"), approved=D.latest_terms(conn, case_id, "approved"),
                   funding=CL.get_funding(conn, case_id), phrase=CL.FUNDING_PHRASE, today=date.today().isoformat())
    return render(request, "case/funding.html", active="closing", **ctx)


@router.post("/cases/{case_id}/funding")
async def funding_confirm(request: Request, case_id: str):
    data = await form_dict(request)
    f = validate(CL.FundingIn, {k: data.get(k) for k in CL.FundingIn.model_fields if data.get(k) not in (None, "")})
    with get_db(request).session() as conn:
        try:
            CL.confirm_funding(conn, case_id, f)
        except TransitionRefused as exc:
            return _refused(request, case_id, exc, f"/cases/{case_id}/funding")
    return redirect(f"/cases/{case_id}/servicing", "Funding recorded as confirmed by a human. Open the servicing account next.")


def stage_context_json(actions) -> str:  # pragma: no cover - debugging helper
    return json.dumps(actions, default=str)
