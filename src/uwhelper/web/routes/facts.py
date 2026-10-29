"""Extraction review, fact verification/correction, financial data editor, contradictions, checklist."""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Request

from uwhelper.analysis.resolver import find_contradictions, missing_checklist
from uwhelper.documents.service import list_case_documents
from uwhelper.domain.models import CorrectionIn, FactIn
from uwhelper.modules import get_module
from uwhelper.repo import cases as cases_repo
from uwhelper.repo import facts as repo
from uwhelper.repo.cases import ValidationFailure
from uwhelper.web.app import get_db, redirect, render
from uwhelper.web.forms import checkbox, form_dict, validate
from uwhelper.web.routes.cases import case_context

router = APIRouter()
MODULE = get_module("commercial")


def _label(facts: list[dict]) -> list[dict]:
    catalog = MODULE.field_catalog()
    for f in facts:
        spec = catalog.get(f["field_key"])
        f["field_label"] = spec.label if spec else f["field_key"]
    return facts


def _back(request: Request, case_id: str, default: str) -> str:
    target = request.query_params.get("next") or default
    return target if target.startswith(f"/cases/{case_id}") else default


@router.get("/cases/{case_id}/review")
async def review(request: Request, case_id: str):
    status = request.query_params.get("status", "candidate")
    doc_filter = request.query_params.get("document_id") or None
    statuses = {"candidate": ("candidate",), "verified": ("verified",), "rejected": ("rejected",),
                "superseded": ("superseded",), "all": None}.get(status, ("candidate",))
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        facts = _label(repo.list_facts(conn, case_id, statuses=statuses, document_id=doc_filter))
        docs = list_case_documents(conn, case_id)
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM facts WHERE case_id=? GROUP BY status", (case_id,))}
        conflict_keys = {(c["field_key"], c["period_id"], c["entity_ref"], c["item_label"])
                         for c in find_contradictions(conn, case_id)}
    for f in facts:
        f["in_conflict"] = (f["field_key"], f["period_id"], f["entity_ref"], f["item_label"]) in conflict_keys
    return render(request, "case/review.html", active="review", facts=facts, status=status, docs=docs,
                  doc_filter=doc_filter, counts=counts, **ctx)


@router.post("/cases/{case_id}/facts/{fact_id}/{action}")
async def fact_action(request: Request, case_id: str, fact_id: str, action: str):
    data = checkbox(await form_dict(request), "mark_verified")
    back = _back(request, case_id, f"/cases/{case_id}/review")
    with get_db(request).session() as conn:
        if action == "verify":
            repo.verify_fact(conn, case_id, fact_id, data.get("note") or None)
            msg = "Fact verified."
        elif action == "unverify":
            repo.unverify_fact(conn, case_id, fact_id, data.get("note") or None)
            msg = "Fact returned to unverified."
        elif action == "reject":
            repo.reject_fact(conn, case_id, fact_id, data.get("reason", ""))
            msg = "Fact rejected (kept in history)."
        elif action == "restore":
            repo.restore_fact(conn, case_id, fact_id, data.get("note") or None)
            msg = "Fact restored as an unverified candidate."
        elif action == "correct":
            c = validate(CorrectionIn, {"new_value": data.get("new_value", ""), "reason": data.get("reason", ""),
                                        "mark_verified": data["mark_verified"]})
            repo.correct_fact(conn, case_id, fact_id, c)
            msg = "Correction saved as a new version; the original is kept as superseded."
        else:
            raise ValidationFailure("Unknown action.")
    return redirect(back, msg)


@router.post("/cases/{case_id}/facts/verify-bulk")
async def verify_bulk(request: Request, case_id: str):
    form = await request.form()
    ids = [str(x) for x in form.getlist("fact_ids")]
    if not ids:
        raise ValidationFailure("Select at least one fact.")
    done = 0
    with get_db(request).session() as conn:
        for fid in ids[:200]:
            fact = repo.get_fact(conn, case_id, fid)
            if fact["status"] == "candidate":
                repo.verify_fact(conn, case_id, fid, "Bulk verification after source review")
                done += 1
    return redirect(f"/cases/{case_id}/review", f"{done} fact(s) verified.")


@router.get("/cases/{case_id}/facts/{fact_id}/history")
async def fact_history(request: Request, case_id: str, fact_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        hist = repo.fact_history(conn, case_id, fact_id)
    _label(hist["versions"])
    return render(request, "case/fact_history.html", active="review", history=hist, **ctx)


@router.get("/cases/{case_id}/financials")
async def financials(request: Request, case_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        periods = repo.list_periods(conn, case_id)
        facts = _label(repo.list_facts(conn, case_id, statuses=("candidate", "verified")))
        docs = list_case_documents(conn, case_id)
        parties = cases_repo.list_parties(conn, case_id)
        debts = cases_repo.list_debts(conn, case_id)
        collateral = cases_repo.list_collateral(conn, case_id)
        conflict_keys = {(c["field_key"], c["period_id"], c["entity_ref"], c["item_label"])
                         for c in find_contradictions(conn, case_id)}
    catalog = MODULE.field_catalog()
    grid: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    adjustments, guarantor_facts, static_facts = [], [], []
    for f in facts:
        spec = catalog.get(f["field_key"])
        f["in_conflict"] = (f["field_key"], f["period_id"], f["entity_ref"], f["item_label"]) in conflict_keys
        if spec is None:
            continue
        if spec.multi_item:
            adjustments.append(f)
        elif spec.party_scoped:
            guarantor_facts.append(f)
        elif spec.timing == "static":
            static_facts.append(f)
        else:
            grid[f["field_key"]][f["period_id"]].append(f)
    categories: dict[str, list] = defaultdict(list)
    for key, spec in catalog.items():
        if not spec.multi_item and not spec.party_scoped and spec.timing != "static":
            categories[spec.category].append(spec)
    return render(request, "case/financials.html", active="financials", periods=periods, grid=grid,
                  categories=categories, catalog=catalog, adjustments=adjustments, guarantor_facts=guarantor_facts,
                  static_facts=static_facts, docs=docs, parties=parties, debts=debts, collateral=collateral, **ctx)


@router.post("/cases/{case_id}/facts")
async def add_fact(request: Request, case_id: str):
    data = checkbox(await form_dict(request), "mark_verified")
    f = validate(FactIn, data)
    with get_db(request).session() as conn:
        cases_repo.get_case(conn, case_id)
        repo.add_user_fact(conn, case_id, f)
    return redirect(_back(request, case_id, f"/cases/{case_id}/financials"),
                    "Value saved as USER-ENTERED" + (" and VERIFIED." if f.mark_verified else " (unverified)."))


@router.post("/cases/{case_id}/base-period")
async def base_period(request: Request, case_id: str):
    data = await form_dict(request)
    with get_db(request).session() as conn:
        cases_repo.set_base_period(conn, case_id, data.get("period_id") or None)
    return redirect(f"/cases/{case_id}/financials", "Base period updated.")


@router.get("/cases/{case_id}/contradictions")
async def contradictions(request: Request, case_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        items = find_contradictions(conn, case_id)
        resolved = [dict(r) for r in conn.execute(
            "SELECT * FROM audit_events WHERE case_id=? AND action='contradiction_resolved' ORDER BY id DESC LIMIT 50",
            (case_id,))]
    import json

    for r in resolved:
        r["details"] = json.loads(r["details_json"])
    return render(request, "case/contradictions.html", active="contradictions", items=items, resolved=resolved, **ctx)


@router.post("/cases/{case_id}/contradictions/resolve")
async def resolve(request: Request, case_id: str):
    form = await request.form()
    keep = str(form.get("keep_fact_id") or "")
    others = [str(x) for x in form.getlist("group_fact_ids") if str(x) != keep]
    reason = str(form.get("reason") or "")
    if not keep:
        raise ValidationFailure("Choose which value is supported by the evidence.")
    with get_db(request).session() as conn:
        repo.resolve_contradiction(conn, case_id, keep, others, reason)
    return redirect(f"/cases/{case_id}/contradictions", "Contradiction resolved with a documented reason.")


@router.get("/cases/{case_id}/missing")
async def missing(request: Request, case_id: str):
    mode = request.query_params.get("mode", "verified_only")
    if mode not in ("verified_only", "include_unverified"):
        mode = "verified_only"
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        items = missing_checklist(conn, case_id, mode)
    return render(request, "case/missing.html", active="missing", items=items, mode=mode, **ctx)
