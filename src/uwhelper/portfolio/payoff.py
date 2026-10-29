"""Payoff quotes for loans serviced in the app (simple daily interest; documented convention).

    paid-through date = due date of the last installment whose scheduled interest is fully paid, in order
                        (the funding date if none)
    principal         = outstanding principal from the payment ledger on the quote date
    per diem          = principal × annual rate ÷ day basis          (365 for actual/365, 360 for actual/360)
    accrued interest  = principal × annual rate × days ÷ day basis, days = good-through date − paid-through date
    payoff            = principal + accrued interest + unpaid fees

Amounts are Decimal and rounded half-up to cents once, at the end of each line (the accrual uses the unrounded per
diem). The rate is the current schedule's rate; for variable-rate loans confirm it matches the lender's current rate.
A quote is a planning figure, not a lender payoff letter. Every quote is stored with all its inputs and can be
re-checked: recomputing from the stored inputs must give the same figures, and the app also reports whether the
ledger has changed since (e.g. a back-dated payment).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from uwhelper.core.identity import current_actor
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.domain.money import dstr
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure

BASIS = {"actual/365": Decimal(365), "actual/360": Decimal(360)}
CENT = Decimal("0.01")
MAX_AHEAD_DAYS = 60


def q2(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def compute(inputs: dict[str, Any]) -> dict[str, Any]:
    """Pure calculation from stored inputs (strings/ISO dates)."""
    principal, rate = Decimal(inputs["principal"]), Decimal(inputs["annual_rate"])
    basis = BASIS[inputs["day_count"]]
    days = (date.fromisoformat(inputs["good_through"]) - date.fromisoformat(inputs["paid_through"])).days
    if days < 0:
        raise ValidationFailure("The good-through date is before the interest paid-through date.")
    per_diem_exact = principal * rate / basis
    accrued = q2(per_diem_exact * days)
    fees = Decimal(inputs["unpaid_fees"])
    return {"days": days, "per_diem": q2(per_diem_exact), "accrued_interest": accrued,
            "total": q2(principal + accrued + fees)}


def ledger_inputs(conn: sqlite3.Connection, account_id: str, quote_date: date) -> dict[str, Any]:
    from uwhelper.servicing.ledger import account_state, get_account

    acct = get_account(conn, account_id)
    if acct["status"] != "ACTIVE":
        raise ValidationFailure("This loan is paid off.")
    st = account_state(conn, account_id, quote_date)
    funding = conn.execute("SELECT funding_date FROM fundings WHERE id=?", (acct["funding_id"],)).fetchone()
    paid_through = funding["funding_date"]
    for i in st.installments:
        if Decimal(i["paid_interest"]) >= Decimal(i["interest"]):
            paid_through = i["due_date"]
        else:
            break
    sched = conn.execute("SELECT annual_rate FROM payment_schedules WHERE account_id=? AND is_current=1",
                         (account_id,)).fetchone()
    return {"principal": dstr(st.outstanding_principal), "annual_rate": sched["annual_rate"],
            "paid_through": paid_through, "unpaid_fees": dstr(st.fees_assessed - st.fees_paid),
            "loan_number": acct["loan_number"]}


def create_quote(conn: sqlite3.Connection, account_id: str, good_through: date, day_count: str = "actual/365",
                 quote_date: date | None = None) -> str:
    quote_date = quote_date or date.today()
    if day_count not in BASIS:
        raise ValidationFailure("Day count must be actual/365 or actual/360.")
    if quote_date > date.today():
        raise ValidationFailure("The quote date cannot be in the future.")
    if good_through < quote_date or good_through > quote_date + timedelta(days=MAX_AHEAD_DAYS):
        raise ValidationFailure(f"The good-through date must be within {MAX_AHEAD_DAYS} days after the quote date.")
    li = ledger_inputs(conn, account_id, quote_date)
    inputs = {"principal": li["principal"], "annual_rate": li["annual_rate"], "paid_through": li["paid_through"],
              "unpaid_fees": li["unpaid_fees"], "good_through": good_through.isoformat(), "day_count": day_count,
              "quote_date": quote_date.isoformat()}
    res = compute(inputs)
    body = json.dumps(inputs, sort_keys=True)
    qid = new_id()
    case_id = conn.execute("SELECT case_id FROM servicing_accounts WHERE id=?", (account_id,)).fetchone()[0]
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO payoff_quotes(id, account_id, quote_date, good_through, day_count, principal, annual_rate,
                   paid_through, days, per_diem, accrued_interest, unpaid_fees, total, inputs_json, inputs_sha256,
                   created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (qid, account_id, inputs["quote_date"], inputs["good_through"], day_count, inputs["principal"],
             inputs["annual_rate"], inputs["paid_through"], res["days"], dstr(res["per_diem"]),
             dstr(res["accrued_interest"]), inputs["unpaid_fees"], dstr(res["total"]), body,
             hashlib.sha256(body.encode()).hexdigest(), current_actor(), utcnow()))
        audit.record(conn, "payoff_quote_created", "servicing_account", account_id, case_id,
                     {"quote_id": qid, "good_through": inputs["good_through"], "day_count": day_count,
                      "total": dstr(res["total"])})
    return qid


def get_quote(conn: sqlite3.Connection, account_id: str, quote_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM payoff_quotes WHERE id=? AND account_id=?", (quote_id, account_id)).fetchone()
    if not row:
        raise NotFound("Payoff quote not found")
    return dict(row)


def list_quotes(conn: sqlite3.Connection, account_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM payoff_quotes WHERE account_id=? ORDER BY created_at DESC, rowid DESC", (account_id,))]


def verify_quote(conn: sqlite3.Connection, account_id: str, quote_id: str) -> dict[str, Any]:
    q = get_quote(conn, account_id, quote_id)
    inputs = json.loads(q["inputs_json"])
    intact = hashlib.sha256(q["inputs_json"].encode()).hexdigest() == q["inputs_sha256"]
    res = compute(inputs)
    reproduced = (dstr(res["per_diem"]), dstr(res["accrued_interest"]), dstr(res["total"]), res["days"]) == \
        (q["per_diem"], q["accrued_interest"], q["total"], q["days"])
    try:
        now = ledger_inputs(conn, account_id, date.fromisoformat(inputs["quote_date"]))
        changed = [k for k in ("principal", "annual_rate", "paid_through", "unpaid_fees") if now[k] != inputs[k]]
    except ValidationFailure:
        changed = ["loan paid off since"]
    return {"inputs_intact": intact, "reproduced": reproduced, "ledger_changed": changed}
