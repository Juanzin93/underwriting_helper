"""Underwriting runs, metrics, findings, scenarios and run history."""

from __future__ import annotations

from fastapi import APIRouter, Request

from uwhelper.analysis.underwriting import (
    RunError,
    create_scenario,
    get_run,
    latest_run_id,
    list_runs,
    list_scenarios,
    run_underwriting,
)
from uwhelper.domain.enums import OVERALL_STATUS_TEXT
from uwhelper.domain.models import ScenarioIn
from uwhelper.metrics.finance import LoanMathError, amortization_schedule
from uwhelper.domain.money import to_decimal
from uwhelper.repo.cases import ValidationFailure
from uwhelper.web.app import get_db, redirect, render
from uwhelper.web.forms import form_dict, validate
from uwhelper.web.routes.cases import case_context

router = APIRouter()

METRIC_GROUPS = [
    ("Income statement", ["revenue", "gross_profit", "gross_margin", "operating_income", "operating_margin",
                          "net_margin", "ebitda", "ebitda_adjustments", "adjusted_ebitda", "ebitda_margin"]),
    ("Cash flow and debt service", ["cfads", "existing_debt_service", "proposed_debt_service", "total_debt_service",
                                    "dscr", "global_cash_flow", "global_debt_service", "global_dscr"]),
    ("Liquidity and leverage", ["working_capital", "current_ratio", "quick_ratio", "total_equity", "debt_to_equity",
                                "funded_debt_pro_forma", "debt_to_ebitda"]),
    ("Collateral", ["collateral_value", "ltv", "cltv", "ltc", "discounted_collateral_value", "collateral_coverage"]),
    ("Break-even", ["breakeven_revenue", "revenue_cushion"]),
    ("Property (CRE)", ["effective_gross_income", "noi", "dscr_property", "debt_yield"]),
]


def _run_or_latest(conn, case_id: str, run_id: str | None):
    rid = run_id or latest_run_id(conn, case_id)
    return get_run(conn, rid, case_id) if rid else None


@router.post("/cases/{case_id}/run")
async def run(request: Request, case_id: str):
    data = await form_dict(request)
    mode = data.get("mode", "verified_only")
    scenario_id = data.get("scenario_id") or None
    with get_db(request).session() as conn:
        try:
            res = run_underwriting(conn, case_id, mode=mode, scenario_id=scenario_id)
        except RunError as exc:
            raise ValidationFailure(str(exc)) from exc
    target = data.get("next") or "findings"
    if target not in ("findings", "metrics", "scenarios"):
        target = "findings"
    url = f"/cases/{case_id}/{target}" + (f"?run_id={res.run_id}" if target != "scenarios" else "")
    return redirect(url, f"Run #{res.run_number} saved: {res.overall_status}. This is a screening, not a decision.")


@router.get("/cases/{case_id}/metrics")
async def metrics(request: Request, case_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        run = _run_or_latest(conn, case_id, request.query_params.get("run_id"))
    groups = []
    if run:
        groups = [(title, [run["metrics_by_key"][k] for k in keys if k in run["metrics_by_key"]])
                  for title, keys in METRIC_GROUPS]
    schedule = []
    t = ctx["case"]["terms"]
    if t and t.get("requested_amount") and t.get("interest_rate") and (t.get("amortization_months")
                                                                       or t.get("repayment_type") == "interest_only"):
        try:
            schedule = amortization_schedule(to_decimal(t["requested_amount"]), to_decimal(t["interest_rate"]),
                                             t.get("amortization_months") or t.get("term_months") or 12,
                                             t.get("term_months"), t.get("repayment_type") or "amortizing")
        except (LoanMathError, TypeError):
            schedule = []
    yearly = []
    for row in schedule:
        y = (row.month - 1) // 12
        if len(yearly) <= y:
            yearly.append({"year": y + 1, "payment": 0, "interest": 0, "principal": 0, "balance": row.balance})
        yearly[y]["payment"] += row.payment
        yearly[y]["interest"] += row.interest
        yearly[y]["principal"] += row.principal
        yearly[y]["balance"] = row.balance
    return render(request, "case/metrics.html", active="metrics", run=run, groups=groups, yearly=yearly, **ctx)


@router.get("/cases/{case_id}/findings")
async def findings(request: Request, case_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        run = _run_or_latest(conn, case_id, request.query_params.get("run_id"))
    return render(request, "case/findings.html", active="findings", run=run, status_text=OVERALL_STATUS_TEXT, **ctx)


@router.get("/cases/{case_id}/runs")
async def runs(request: Request, case_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        items = list_runs(conn, case_id)
    return render(request, "case/runs.html", active="runs", runs=items, **ctx)


@router.get("/cases/{case_id}/runs/compare")
async def compare(request: Request, case_id: str):
    a, b = request.query_params.get("a"), request.query_params.get("b")
    if not a or not b:
        raise ValidationFailure("Select two runs to compare.")
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        ra, rb = get_run(conn, a, case_id), get_run(conn, b, case_id)
    metric_rows = []
    for m in ra["metrics"]:
        other = rb["metrics_by_key"].get(m["metric_key"])
        if m["status"] == "not_applicable" and (not other or other["status"] == "not_applicable"):
            continue
        metric_rows.append({"label": m["label"], "unit": m["unit"], "a": m, "b": other,
                            "changed": (m["value"], m["status"]) != ((other or {}).get("value"), (other or {}).get("status"))})
    rules_b = {e["rule_id"]: e for e in rb["evaluations"]}
    rule_rows = [{"rule_id": e["rule_id"], "description": e["description"], "a": e["result"],
                  "b": rules_b.get(e["rule_id"], {}).get("result"),
                  "changed": e["result"] != rules_b.get(e["rule_id"], {}).get("result")} for e in ra["evaluations"]]
    return render(request, "case/compare.html", active="runs", ra=ra, rb=rb, metric_rows=metric_rows,
                  rule_rows=rule_rows, **ctx)


@router.get("/cases/{case_id}/scenarios")
async def scenarios(request: Request, case_id: str):
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        items = list_scenarios(conn, case_id)
        base = _run_or_latest(conn, case_id, None)
        compared = []
        for s in items:
            if s["last_run"]:
                compared.append({"scenario": s, "run": get_run(conn, s["last_run"]["id"], case_id)})
    keys = ["cfads", "total_debt_service", "dscr", "global_dscr", "dscr_property", "ltv", "debt_to_ebitda",
            "collateral_coverage"]
    return render(request, "case/scenarios.html", active="scenarios", scenarios=items, base=base, compared=compared,
                  compare_keys=keys, **ctx)


@router.post("/cases/{case_id}/scenarios")
async def add_scenario(request: Request, case_id: str):
    data = await form_dict(request)
    mode = data.pop("mode", "verified_only")
    s = validate(ScenarioIn, data)
    with get_db(request).session() as conn:
        case_context(conn, case_id)
        sid = create_scenario(conn, case_id, s.name, s.description, s.adjustments())
        try:
            run_underwriting(conn, case_id, mode=mode, scenario_id=sid)
        except RunError as exc:
            raise ValidationFailure(str(exc)) from exc
    return redirect(f"/cases/{case_id}/scenarios", "Scenario saved and evaluated. The base case was not changed.")
