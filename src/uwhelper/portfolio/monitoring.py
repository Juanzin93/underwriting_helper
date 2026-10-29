"""Monitoring funded deals: key dates, reminder tasks and one watchlist across all loans.

Brokered loans (funded deals without an in-app servicing account) get key dates from the funding confirmation and the
final terms (closing record, else the accepted offer):
* interest-only end = funding date + interest-only months
* rate resets every N months (variable rate, if the closing record says how often) until maturity
* maturity = funding date + term months; a balloon falls due at maturity when amortization is longer than the term
  (its amount comes from the structuring engine)
Reminder tasks are created ahead of each date (``LEADS``) exactly once per (deal, kind, date, lead time).

Loans serviced in the app keep their own alerts (``servicing.monitoring.alerts``); the watchlist shows those once,
mapped to their deal, and never repeats them with brokered-loan dates for the same deal.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from uwhelper.metrics.structuring import add_months
from uwhelper.repo.cases import NotFound

LEADS = {"maturity": (180, 60), "balloon": (365, 180, 90), "interest_only_end": (60,), "rate_reset": (30,)}
WINDOWS = {"maturity": 90, "balloon": 180, "interest_only_end": 60, "rate_reset": 30}
LABELS = {"maturity": "Loan matures", "balloon": "Balloon payment due", "interest_only_end": "Interest-only period ends",
          "rate_reset": "Interest rate resets"}


def _serviced_case(conn: sqlite3.Connection, deal: sqlite3.Row) -> str | None:
    if not deal["loan_case_id"]:
        return None
    row = conn.execute("SELECT id FROM servicing_accounts WHERE case_id=?", (deal["loan_case_id"],)).fetchone()
    return row["id"] if row else None


def loan_terms(conn: sqlite3.Connection, deal_id: str) -> dict[str, Any] | None:
    """Final terms of a funded deal (None when the deal has no funding confirmation)."""
    f = conn.execute("SELECT * FROM deal_fundings WHERE deal_id=?", (deal_id,)).fetchone()
    if not f:
        return None
    offer = conn.execute("SELECT * FROM offers WHERE id=?", (f["offer_id"],)).fetchone() if f["offer_id"] else None
    cl = conn.execute("SELECT * FROM deal_closings WHERE deal_id=?", (deal_id,)).fetchone()

    def pick(col_cl: str, col_offer: str) -> Any:
        if cl and cl[col_cl] is not None:
            return cl[col_cl]
        return offer[col_offer] if offer else None

    return {"funded_amount": Decimal(f["funded_amount"]), "funding_date": date.fromisoformat(f["funding_date"]),
            "annual_rate": pick("final_annual_rate", "annual_rate"), "term_months": pick("final_term_months", "term_months"),
            "amortization_months": pick("final_amortization_months", "amortization_months"),
            "interest_only_months": offer["interest_only_months"] if offer else 0,
            "payment_frequency": offer["payment_frequency"] if offer else "monthly",
            "rate_type": offer["rate_type"] if offer else "fixed",
            "rate_reset_months": cl["rate_reset_months"] if cl else None,
            "lender": conn.execute("""SELECT o.name FROM offers f JOIN submissions s ON s.id=f.submission_id
                                      JOIN lender_programs p ON p.id=s.program_id JOIN organizations o
                                      ON o.id=p.organization_id WHERE f.id=?""", (f["offer_id"],)).fetchone()[0]
            if f["offer_id"] else None}


def key_dates(conn: sqlite3.Connection, deal_id: str) -> list[dict[str, Any]]:
    from uwhelper.metrics.structuring import StructureError, StructureInputs, analyse

    t = loan_terms(conn, deal_id)
    if not t or not t["term_months"]:
        return []
    fd, term = t["funding_date"], int(t["term_months"])
    maturity = add_months(fd, term)
    out: list[dict[str, Any]] = []
    io = int(t["interest_only_months"] or 0)
    if io:
        out.append({"kind": "interest_only_end", "date": add_months(fd, io), "detail": f"after {io} months"})
    if t["rate_type"] == "variable" and t["rate_reset_months"]:
        step = int(t["rate_reset_months"])
        m = step
        while m < term:
            out.append({"kind": "rate_reset", "date": add_months(fd, m), "detail": f"every {step} months"})
            m += step
    out.append({"kind": "maturity", "date": maturity, "detail": f"{term}-month term"})
    amort = t["amortization_months"]
    if amort and int(amort) > term and t["annual_rate"] is not None:
        try:
            a = analyse(StructureInputs(principal=t["funded_amount"], annual_rate=Decimal(str(t["annual_rate"])),
                                        term_months=term, amortization_months=int(amort), interest_only_months=io,
                                        payment_frequency=t["payment_frequency"]))
            amount = a["balloon_payment"]
        except StructureError:
            amount = None
        out.append({"kind": "balloon", "date": maturity, "amount": amount,
                    "detail": f"{amort}-month amortization over a {term}-month term"})
    out.sort(key=lambda x: (x["date"], x["kind"]))
    return out


def sync_reminders(conn: sqlite3.Connection, deal_id: str, today: date | None = None) -> int:
    """Create missing reminder tasks for a brokered loan's upcoming dates. Returns how many were created."""
    from uwhelper.crm import tasks as task_svc
    from uwhelper.crm.models import TaskIn
    from uwhelper.db.connection import Database, utcnow

    today = today or date.today()
    deal = conn.execute("SELECT id, deal_number, loan_case_id, status FROM deals WHERE id=?", (deal_id,)).fetchone()
    if not deal:
        raise NotFound("Deal not found")
    if deal["status"] not in ("FUNDED", "SERVICING") or _serviced_case(conn, deal):
        return 0
    created = 0
    for kd in key_dates(conn, deal_id):
        if kd["date"] < today:
            continue
        for lead in LEADS[kd["kind"]]:
            remind_on = date.fromordinal(kd["date"].toordinal() - lead)
            exists = conn.execute("SELECT 1 FROM deal_reminders WHERE deal_id=? AND kind=? AND event_date=? AND lead_days=?",
                                  (deal_id, kd["kind"], kd["date"].isoformat(), lead)).fetchone()
            if exists:
                continue
            title = f"{LABELS[kd['kind']]} on {kd['date'].isoformat()} ({deal['deal_number']}, {lead} days' notice)"
            if kd.get("amount"):
                title += f" — ${kd['amount']:,.2f}"
            with Database.transaction(conn):
                tid = task_svc.create_task(conn, TaskIn(title=title[:200], deal_id=deal_id,
                                                        due_date=max(remind_on, today), priority="high"
                                                        if kd["kind"] in ("balloon", "maturity") else "normal",
                                                        description=f"Reminder {lead} days ahead ({kd['detail']})."))
                conn.execute("INSERT INTO deal_reminders(deal_id, kind, event_date, lead_days, task_id, created_at) "
                             "VALUES (?,?,?,?,?,?)", (deal_id, kd["kind"], kd["date"].isoformat(), lead, tid, utcnow()))
            created += 1
    return created


def sync_all_reminders(conn: sqlite3.Connection, today: date | None = None) -> int:
    return sum(sync_reminders(conn, r["id"], today) for r in conn.execute(
        "SELECT d.id FROM deals d JOIN deal_fundings f ON f.deal_id=d.id WHERE d.status IN ('FUNDED','SERVICING')"))


def watchlist(conn: sqlite3.Connection, today: date | None = None) -> list[dict[str, Any]]:
    """In-app servicing alerts (mapped to deals) + brokered-loan dates inside their warning windows."""
    from uwhelper.servicing.monitoring import alerts

    today = today or date.today()
    items: list[dict[str, Any]] = []
    seen_cases: set[str] = set()
    for acct in conn.execute("""SELECT a.id, a.case_id, a.loan_number, d.id AS deal_id, d.deal_number
                                FROM servicing_accounts a LEFT JOIN deals d ON d.loan_case_id=a.case_id
                                WHERE a.status='ACTIVE' ORDER BY a.loan_number"""):
        seen_cases.add(acct["case_id"])
        for al in alerts(conn, acct["id"], today):
            items.append({"source": "servicing", "severity": al["severity"], "rule": al["rule"], "message": al["message"],
                          "key": f"acct:{acct['id']}:{al['key']}", "deal_id": acct["deal_id"],
                          "deal_number": acct["deal_number"], "case_id": acct["case_id"],
                          "loan_number": acct["loan_number"]})
    for d in conn.execute("""SELECT d.id, d.deal_number, d.name, d.loan_case_id FROM deals d
                             JOIN deal_fundings f ON f.deal_id=d.id WHERE d.status IN ('FUNDED','SERVICING')"""):
        if d["loan_case_id"] in seen_cases:
            continue  # serviced in the app: its own alerts already cover these dates
        for kd in key_dates(conn, d["id"]):
            days = (kd["date"] - today).days
            if 0 <= days <= WINDOWS[kd["kind"]]:
                sev = "high" if kd["kind"] == "balloon" or days <= 30 else "medium"
                msg = f"{LABELS[kd['kind']]} on {kd['date'].isoformat()} ({days} days)"
                if kd.get("amount"):
                    msg += f" — ${kd['amount']:,.2f}"
                items.append({"source": "brokered", "severity": sev, "rule": kd["kind"].upper(), "message": msg,
                              "key": f"deal:{d['id']}:{kd['kind']}:{kd['date'].isoformat()}", "deal_id": d["id"],
                              "deal_number": d["deal_number"], "case_id": None, "loan_number": None})
    order = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda x: (order.get(x["severity"], 3), x["deal_number"] or "", x["rule"]))
    return items
