"""Servicing account, payments, covenants, monitoring and portfolio pages."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Request
from starlette.datastructures import UploadFile

from uwhelper.analysis.underwriting import list_runs
from uwhelper.repo.audit import verify_chain
from uwhelper.repo.cases import ValidationFailure
from uwhelper.servicing import ledger as LG
from uwhelper.servicing import monitoring as MO
from uwhelper.web.app import get_db, redirect, render
from uwhelper.web.forms import checkbox, form_dict, validate
from uwhelper.web.routes.cases import case_context
from uwhelper.workflow.currency import latest_base_run

router = APIRouter()


def _acct(conn, case_id: str) -> dict:
    a = LG.account_for_case(conn, case_id)
    if not a:
        raise ValidationFailure("No servicing account yet. Confirm funding, then open the servicing account.")
    return a


@router.get("/cases/{case_id}/servicing")
async def servicing_page(request: Request, case_id: str):
    as_of_raw = request.query_params.get("as_of")
    as_of = date.fromisoformat(as_of_raw) if as_of_raw else date.today()
    with get_db(request).session() as conn:
        ctx = case_context(conn, case_id)
        acct = LG.account_for_case(conn, case_id)
        ctx["account"] = acct
        if acct:
            ctx.update(state=LG.account_state(conn, acct["id"], as_of), payments=LG.payments(conn, acct["id"]),
                       schedules=LG.schedules(conn, acct["id"]), modifications=LG.modifications(conn, acct["id"]),
                       covenants=MO.covenant_status(conn, acct["id"], as_of), items=LG.monitoring_items(conn, acct["id"]),
                       alerts=MO.alerts(conn, acct["id"], as_of), events=MO.events(conn, acct["id"]),
                       fees=[dict(r) for r in conn.execute("SELECT * FROM account_fees WHERE account_id=? ORDER BY fee_date",
                                                           (acct["id"],))],
                       runs=[r for r in list_runs(conn, case_id) if r["mode"] == "verified_only" and not r["scenario_name"]],
                       covenant_metrics=MO.COVENANT_METRICS, as_of=as_of.isoformat())
            run = latest_base_run(conn, case_id)
            cf = conn.execute("SELECT value FROM run_metrics WHERE run_id=? AND metric_key='cfads' AND status='computed'",
                              (run["id"],)).fetchone() if run else None
            ctx["latest_cfads"] = cf["value"] if cf else None
            ctx["what_if"] = None
            if request.query_params.get("wi"):
                q = request.query_params
                ctx["what_if"] = LG.what_if(conn, acct["id"], rate=LG.parse_optional_amount(q.get("wi_rate")) / 100
                                            if q.get("wi_rate") else None,
                                            extra_payment=LG.parse_optional_amount(q.get("wi_extra")) or Decimal(0),
                                            cfads=LG.parse_optional_amount(q.get("wi_cfads")),
                                            existing_other_ds=LG.parse_optional_amount(q.get("wi_other_ds")) or Decimal(0))
        else:
            from uwhelper.workflow.closing import get_funding

            ctx["funding"] = get_funding(conn, case_id)
    return render(request, "case/servicing.html", active="servicing", **ctx)


@router.post("/cases/{case_id}/servicing/open")
async def open_account(request: Request, case_id: str):
    data = checkbox(await form_dict(request), "add_default_monitoring")
    a = validate(LG.OpenAccountIn, data)
    with get_db(request).session() as conn:
        LG.open_account(conn, case_id, a)
    return redirect(f"/cases/{case_id}/servicing", "Servicing account opened; payment schedule generated.")


@router.post("/cases/{case_id}/servicing/{action}")
async def servicing_action(request: Request, case_id: str, action: str):
    if action == "import":
        form = await request.form()
        f = form.get("file")
        if not isinstance(f, UploadFile):
            raise ValidationFailure("Choose a CSV file.")
        data_bytes = await f.read(2_000_001)
        if len(data_bytes) > 2_000_000:
            raise ValidationFailure("CSV larger than 2 MB.")
        with get_db(request).session() as conn:
            r = LG.import_payments_csv(conn, _acct(conn, case_id)["id"], data_bytes)
        return redirect(f"/cases/{case_id}/servicing", f"Imported {r['imported']} payment(s); "
                        f"{r['duplicates']} duplicate(s) skipped (batch {r['batch']}).")
    data = await form_dict(request)
    if action == "covenant-active":
        checkbox(data, "active")
    with get_db(request).session() as conn:
        aid = _acct(conn, case_id)["id"]
        if action == "payment":
            LG.record_payment(conn, aid, validate(LG.PaymentIn, data))
            msg = "Payment recorded (as reported by you; no bank connection)."
        elif action == "reverse":
            LG.reverse_payment(conn, aid, data.get("payment_id", ""), data.get("reason", ""))
            msg = "Payment reversed (original kept, reversal entry added)."
        elif action == "fee":
            LG.assess_fee(conn, aid, validate(LG.FeeIn, data))
            msg = "Fee recorded."
        elif action == "modify":
            LG.modify(conn, aid, validate(LG.ModificationIn, data))
            msg = "Modification recorded; a new schedule version is now current (history kept)."
        elif action == "covenant":
            MO.add_covenant(conn, aid, validate(MO.CovenantIn, data))
            msg = "Covenant added."
        elif action == "covenant-test":
            pe = date.fromisoformat(data.get("period_end", ""))
            if data.get("run_id"):
                MO.test_from_run(conn, aid, data["covenant_id"], pe, data["run_id"])
            else:
                MO.test_manual(conn, aid, data["covenant_id"], pe, data.get("value"), data.get("evidence_note", ""))
            msg = "Covenant test recorded."
        elif action == "covenant-active":
            MO.set_covenant_active(conn, aid, data.get("covenant_id", ""), data["active"], data.get("reason", ""))
            msg = "Covenant status changed."
        elif action == "item":
            LG.add_monitoring_item(conn, aid, validate(LG.MonitoringItemIn, data))
            msg = "Monitoring item added."
        elif action == "item-done":
            LG.complete_monitoring_item(conn, aid, data.get("item_id", ""), data.get("note", ""))
            msg = "Item completed; the next occurrence was scheduled if it recurs."
        elif action == "refresh":
            n = MO.refresh_events(conn, aid)
            msg = f"{n} new monitoring event(s) recorded."
        elif action == "ack":
            MO.acknowledge_event(conn, aid, int(data.get("event_id", "0")), data.get("note", ""))
            msg = "Event acknowledged."
        elif action == "paid-off":
            LG.mark_paid_off(conn, aid, data.get("confirmed_by", ""), data.get("note", ""))
            msg = "Loan marked paid off."
        else:
            raise ValidationFailure("Unknown action.")
    return redirect(f"/cases/{case_id}/servicing", msg)


@router.get("/portfolio")
async def portfolio(request: Request):
    with get_db(request).session() as conn:
        p = MO.portfolio(conn)
    return render(request, "portfolio.html", p=p)


@router.get("/audit")
async def audit_all(request: Request):
    from uwhelper.repo.audit import list_events

    with get_db(request).session() as conn:
        events = list_events(conn, None, 500)
        chain = verify_chain(conn)
    return render(request, "audit_all.html", events=events, chain=chain)
