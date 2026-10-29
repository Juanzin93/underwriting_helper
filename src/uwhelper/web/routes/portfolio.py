"""Deals portfolio, watchlist, reminders and payoff quotes."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import Response

from uwhelper.portfolio import monitoring as MON
from uwhelper.portfolio import payoff as PO
from uwhelper.portfolio import reports as REP
from uwhelper.repo.cases import ValidationFailure
from uwhelper.web.app import get_db, redirect, render
from uwhelper.web.forms import form_dict

router = APIRouter()


def _date(v: str | None) -> date | None:
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        raise ValidationFailure("Dates must be YYYY-MM-DD.") from None


@router.get("/portfolio/deals")
async def deals_portfolio(request: Request):
    q = request.query_params
    by = q.get("by") if q.get("by") in REP.GROUPS else "lender"
    dt = q.get("deal_type") if q.get("deal_type") in ("small_business", "cre") else None
    dfrom, dto = _date(q.get("from")), _date(q.get("to"))
    with get_db(request).session() as conn:
        rows = REP.funded_deals(conn, date_from=dfrom, date_to=dto, deal_type=dt)
    groups = REP.grouped(rows, by)
    if q.get("format") == "csv":
        name = f"portfolio_by_{by}_{date.today().isoformat()}.csv"
        return Response(REP.to_csv(groups, by), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return render(request, "portfolio_deals.html", rows=rows, groups=groups, totals=REP.totals(rows), by=by,
                  group_names=REP.GROUPS, deal_type=dt or "", date_from=q.get("from") or "", date_to=q.get("to") or "",
                  query=str(request.url.query))


@router.get("/watchlist")
async def watchlist_page(request: Request):
    with get_db(request).session() as conn:
        created = MON.sync_all_reminders(conn)
        items = MON.watchlist(conn)
    return render(request, "watchlist.html", items=items, created=created)


@router.post("/reminders/sync")
async def reminders_sync(request: Request):
    data = await form_dict(request)
    with get_db(request).session() as conn:
        n = MON.sync_all_reminders(conn)
    back = data.get("next") if (data.get("next") or "").startswith("/") and not data.get("next", "").startswith("//") \
        else "/tasks"
    return redirect(back, f"{n} new reminder task(s) created." if n else "Reminders are up to date.")


@router.post("/cases/{case_id}/servicing/payoff/quote")
async def payoff_quote(request: Request, case_id: str):
    from uwhelper.servicing.ledger import account_for_case

    data = await form_dict(request)
    gt = _date(data.get("good_through"))
    if not gt:
        raise ValidationFailure("Enter the good-through date.")
    with get_db(request).session() as conn:
        acct = account_for_case(conn, case_id)
        if not acct:
            raise ValidationFailure("This file has no servicing account.")
        qid = PO.create_quote(conn, acct["id"], gt, data.get("day_count") or "actual/365")
    return redirect(f"/cases/{case_id}/servicing?quote={qid}#payoff", "Payoff quote saved.")
