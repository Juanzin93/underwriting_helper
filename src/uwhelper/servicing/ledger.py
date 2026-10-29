"""Servicing accounts, payment schedules, actual payments, allocation, delinquency and modifications.

Scheduled vs actual: installments are what is DUE; payments are what the user REPORTS was received.
Nothing is assumed paid because a date passed.

Allocation policy (deterministic, documented):
1. Reversed payments are excluded together with their reversal entries.
2. Remaining payments are applied in received-date order to installments in due-date order:
   interest first, then scheduled principal (a payment may cover future installments — "paid ahead").
3. After all installments are covered, money goes to assessed fees, then to unscheduled principal.
Interest follows the schedule (no daily accrual); a prepayment reduces outstanding principal but does not
re-amortize the schedule unless a modification is recorded.

Delinquency: an installment is past due once its due date has passed and it is not fully paid.
Days past due count from the oldest unpaid due date; buckets: CURRENT, WITHIN_GRACE, 1-29, 30-59, 60-89, 90+.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.domain.models import _dec, _rate
from uwhelper.domain.money import InvalidAmount, dstr, to_decimal
from uwhelper.metrics.structuring import (
    PERIODS,
    StructureError,
    StructureInputs,
    add_months,
    build_schedule,
    due_dates,
)
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure, get_case
from uwhelper.workflow import lifecycle

ZERO = Decimal(0)
MAX_CSV_ROWS = 5000


# ------------------------------------------------------------------------------------------ schedules
def _insert_schedule(conn, account_id: str, *, version: int, reason: str, effective: date, principal: Decimal,
                     rate: Decimal, rate_type: str, frequency: str, term_months: int, amort_months: int,
                     io_months: int, first_payment: date) -> str:
    si = StructureInputs(principal=principal, annual_rate=rate, term_months=term_months,
                         amortization_months=amort_months, interest_only_months=io_months,
                         payment_frequency=frequency, rate_type=rate_type)
    try:
        rows = build_schedule(si)
    except StructureError as exc:
        raise ValidationFailure(str(exc)) from exc
    dates = due_dates(first_payment, len(rows), frequency)
    sid = new_id()
    conn.execute("UPDATE payment_schedules SET is_current=0 WHERE account_id=?", (account_id,))
    conn.execute(
        """INSERT INTO payment_schedules(id, account_id, version, is_current, reason, effective_date, principal,
               annual_rate, rate_type, payment_frequency, term_months, amortization_months, interest_only_months,
               first_payment_date, maturity_date, created_at) VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sid, account_id, version, reason, effective.isoformat(), dstr(principal), dstr(rate), rate_type, frequency,
         term_months, amort_months, io_months, first_payment.isoformat(), dates[-1].isoformat(), utcnow()))
    conn.executemany(
        """INSERT INTO schedule_installments(schedule_id, seq, due_date, payment, interest, principal, balance_after,
               is_balloon) VALUES (?,?,?,?,?,?,?,?)""",
        [(sid, r.seq, d.isoformat(), dstr(r.payment), dstr(r.interest), dstr(r.principal), dstr(r.balance),
          int(r.kind == "balloon")) for r, d in zip(rows, dates)])
    return sid


class OpenAccountIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    loan_number: str | None = Field(default=None, max_length=40)
    grace_days: int = Field(default=10, ge=0, le=60)
    add_default_monitoring: bool = True

    @field_validator("loan_number", mode="before")
    @classmethod
    def _b(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v


def open_account(conn: sqlite3.Connection, case_id: str, a: OpenAccountIn) -> str:
    case = get_case(conn, case_id)
    lifecycle.require_stage(case, ("FUNDED",), "Opening a servicing account")
    funding = conn.execute("SELECT * FROM fundings WHERE case_id=?", (case_id,)).fetchone()
    if not funding:
        raise ValidationFailure("No funding record.")
    ex = conn.execute("SELECT * FROM term_sets WHERE id=?", (funding["executed_terms_id"],)).fetchone()
    acct_id, now = new_id(), utcnow()
    loan_number = a.loan_number or case["case_number"].replace("UW-", "LN-")
    funded = Decimal(funding["funded_amount"])
    first = date.fromisoformat(funding["first_payment_date"])
    with Database.transaction(conn):
        if conn.execute("SELECT 1 FROM servicing_accounts WHERE loan_number=?", (loan_number,)).fetchone():
            raise ValidationFailure("That loan number is already in use.")
        conn.execute(
            """INSERT INTO servicing_accounts(id, case_id, funding_id, loan_number, original_principal, funded_principal,
                   grace_days, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (acct_id, case_id, funding["id"], loan_number, ex["principal"], dstr(funded), a.grace_days, now, now))
        _insert_schedule(conn, acct_id, version=1, reason="Original schedule from executed terms and funded amount",
                         effective=date.fromisoformat(funding["funding_date"]), principal=funded,
                         rate=Decimal(ex["annual_rate"]), rate_type=ex["rate_type"], frequency=ex["payment_frequency"],
                         term_months=ex["term_months"], amort_months=ex["amortization_months"],
                         io_months=ex["interest_only_months"], first_payment=first)
        if a.add_default_monitoring:
            fd = date.fromisoformat(funding["funding_date"])
            for typ, desc, months, freq in (
                    ("financial_statements", "Annual business financial statements", 12, 12),
                    ("tax_returns", "Annual business tax return", 12, 12),
                    ("insurance_renewal", "Insurance renewal evidence (lender as loss payee)", 12, 12)):
                _add_item(conn, acct_id, typ, desc, add_months(fd, months), freq)
            if case["terms"] and case["terms"].get("is_secured"):
                _add_item(conn, acct_id, "collateral_revaluation", "Collateral revaluation / inspection",
                          add_months(fd, 24), 24)
        audit.record(conn, "servicing_account_opened", "servicing_account", acct_id, case_id,
                     {"loan_number": loan_number, "funded_principal": dstr(funded), "grace_days": a.grace_days})
        lifecycle._write(conn, get_case(conn, case_id), "SERVICING", "Servicing account opened", False, [], None)
    return acct_id


def get_account(conn: sqlite3.Connection, account_id: str) -> dict[str, Any]:
    row = conn.execute("""SELECT a.*, c.case_number, c.title, c.loan_type, b.legal_name FROM servicing_accounts a
                          JOIN loan_cases c ON c.id=a.case_id LEFT JOIN borrowers b ON b.case_id=c.id
                          WHERE a.id=?""", (account_id,)).fetchone()
    if not row:
        raise NotFound("Servicing account not found")
    return dict(row)


def account_for_case(conn: sqlite3.Connection, case_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT id FROM servicing_accounts WHERE case_id=?", (case_id,)).fetchone()
    return get_account(conn, row["id"]) if row else None


def schedules(conn: sqlite3.Connection, account_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM payment_schedules WHERE account_id=? ORDER BY version",
                                          (account_id,))]


def effective_installments(conn: sqlite3.Connection, account_id: str) -> list[dict[str, Any]]:
    """Obligations across schedule versions: each version governs dues before the next version's effective date."""
    vers = schedules(conn, account_id)
    out: list[dict[str, Any]] = []
    for i, s in enumerate(vers):
        cutoff = vers[i + 1]["effective_date"] if i + 1 < len(vers) else None
        for r in conn.execute("SELECT * FROM schedule_installments WHERE schedule_id=? ORDER BY seq", (s["id"],)):
            if cutoff is None or r["due_date"] < cutoff:
                d = dict(r)
                d["schedule_version"] = s["version"]
                out.append(d)
    out.sort(key=lambda x: (x["due_date"], x["schedule_version"], x["seq"]))
    return out


# ------------------------------------------------------------------------------------------ payments
class PaymentIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    received_date: date
    amount: Decimal
    method: str | None = Field(default=None, max_length=40)
    reference: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("amount", mode="before")
    @classmethod
    def _a(cls, v: object) -> Decimal:
        d = _dec(v, allow_negative=False, required=True)
        if d is None or d <= 0:
            raise ValueError("amount must be greater than zero")
        if d != d.quantize(Decimal("0.01")):
            raise ValueError("amount must be in whole cents")
        return d

    @field_validator("method", "reference", "note", mode="before")
    @classmethod
    def _b(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v


def _check_active(acct: dict[str, Any]) -> None:
    if acct["status"] != "ACTIVE":
        raise ValidationFailure("The account is not active.")


def record_payment(conn: sqlite3.Connection, account_id: str, p: PaymentIn, *, source: str = "manual",
                   batch: str | None = None) -> str:
    acct = get_account(conn, account_id)
    _check_active(acct)
    funding_date = conn.execute("SELECT funding_date FROM fundings WHERE id=?", (acct["funding_id"],)).fetchone()[0]
    if p.received_date.isoformat() < funding_date:
        raise ValidationFailure("Payment date is before the funding date.")
    if p.received_date > date.today():
        raise ValidationFailure("Payments cannot be recorded with a future date (record them when received).")
    pid = new_id()
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO payments(id, account_id, received_date, amount, entry_type, method, reference, source,
                   import_batch, note, created_at) VALUES (?,?,?,?, 'PAYMENT', ?,?,?,?,?,?)""",
            (pid, account_id, p.received_date.isoformat(), dstr(p.amount), p.method, p.reference, source, batch,
             p.note, utcnow()))
        audit.record(conn, "payment_recorded", "payment", pid, acct["case_id"],
                     {"amount": dstr(p.amount), "received_date": p.received_date.isoformat(), "source": source,
                      "reference": p.reference, "batch": batch})
    return pid


def reverse_payment(conn: sqlite3.Connection, account_id: str, payment_id: str, reason: str) -> str:
    acct = get_account(conn, account_id)
    row = conn.execute("SELECT * FROM payments WHERE id=? AND account_id=? AND entry_type='PAYMENT'",
                       (payment_id, account_id)).fetchone()
    if not row:
        raise NotFound("Payment not found")
    if conn.execute("SELECT 1 FROM payments WHERE reverses_id=?", (payment_id,)).fetchone():
        raise ValidationFailure("Payment was already reversed.")
    if len((reason or "").strip()) < 10:
        raise ValidationFailure("Give a reason for the reversal (at least 10 characters).")
    rid = new_id()
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO payments(id, account_id, received_date, amount, entry_type, reverses_id, source, note, created_at)
               VALUES (?,?,?,?, 'REVERSAL', ?, 'manual', ?, ?)""",
            (rid, account_id, date.today().isoformat(), dstr(-Decimal(row["amount"])), payment_id, reason.strip(), utcnow()))
        audit.record(conn, "payment_reversed", "payment", payment_id, acct["case_id"],
                     {"amount": row["amount"], "received_date": row["received_date"], "reason": reason.strip()})
    return rid


def import_payments_csv(conn: sqlite3.Connection, account_id: str, data: bytes) -> dict[str, Any]:
    """Columns: date (YYYY-MM-DD), amount, and optional reference, method, note. All-or-nothing on errors."""
    acct = get_account(conn, account_id)
    _check_active(acct)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationFailure("CSV must be UTF-8 text.") from exc
    reader = csv.DictReader(io.StringIO(text))
    cols = {c.strip().lower() for c in (reader.fieldnames or [])}
    if not {"date", "amount"} <= cols:
        raise ValidationFailure("CSV needs a header row with at least 'date' and 'amount' columns.")
    rows, errors = [], []
    for i, raw in enumerate(reader, start=2):
        if i > MAX_CSV_ROWS + 1:
            raise ValidationFailure(f"CSV has more than {MAX_CSV_ROWS} rows.")
        r = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        if not any(r.values()):
            continue
        try:
            rows.append(PaymentIn(received_date=r.get("date"), amount=r.get("amount"), method=r.get("method") or None,
                                  reference=r.get("reference") or None, note=r.get("note") or None))
        except Exception as exc:  # pydantic ValidationError -> concise message
            errors.append(f"row {i}: {str(exc).splitlines()[1] if len(str(exc).splitlines()) > 1 else exc}")
    if errors:
        raise ValidationFailure("CSV not imported (no rows saved). " + "; ".join(errors[:10]))
    existing = {(r["received_date"], r["amount"], r["reference"] or "") for r in conn.execute(
        "SELECT received_date, amount, reference FROM payments WHERE account_id=? AND entry_type='PAYMENT'", (account_id,))}
    batch = new_id()[:12]
    imported, duplicates = 0, 0
    with Database.transaction(conn):
        for p in rows:
            key = (p.received_date.isoformat(), dstr(p.amount), p.reference or "")
            if key in existing:
                duplicates += 1
                continue
            record_payment(conn, account_id, p, source="csv", batch=batch)
            existing.add(key)
            imported += 1
        audit.record(conn, "payments_imported", "servicing_account", account_id, acct["case_id"],
                     {"batch": batch, "imported": imported, "duplicates_skipped": duplicates})
    return {"batch": batch, "imported": imported, "duplicates": duplicates}


class FeeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    fee_date: date
    fee_type: Literal["late_fee", "modification_fee", "inspection_fee", "legal_fee", "other"]
    amount: Decimal
    note: str | None = Field(default=None, max_length=500)

    @field_validator("amount", mode="before")
    @classmethod
    def _a(cls, v: object) -> Decimal:
        d = _dec(v, allow_negative=False, required=True)
        if d is None or d <= 0:
            raise ValueError("amount must be greater than zero")
        return d


def assess_fee(conn: sqlite3.Connection, account_id: str, f: FeeIn) -> str:
    acct = get_account(conn, account_id)
    _check_active(acct)
    fid = new_id()
    with Database.transaction(conn):
        conn.execute("INSERT INTO account_fees(id, account_id, fee_date, fee_type, amount, note, created_at) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (fid, account_id, f.fee_date.isoformat(), f.fee_type, dstr(f.amount), f.note, utcnow()))
        audit.record(conn, "fee_assessed", "account_fee", fid, acct["case_id"],
                     {"type": f.fee_type, "amount": dstr(f.amount), "date": f.fee_date.isoformat()})
    return fid


def payments(conn: sqlite3.Connection, account_id: str) -> list[dict[str, Any]]:
    rows = [dict(r) for r in conn.execute("SELECT * FROM payments WHERE account_id=? ORDER BY received_date, created_at",
                                          (account_id,))]
    reversed_ids = {r["reverses_id"] for r in rows if r["entry_type"] == "REVERSAL"}
    for r in rows:
        r["reversed"] = r["id"] in reversed_ids
    return rows


# ------------------------------------------------------------------------------------------ state
@dataclass
class AccountState:
    as_of: date
    installments: list[dict[str, Any]] = field(default_factory=list)
    funded_principal: Decimal = ZERO
    outstanding_principal: Decimal = ZERO
    scheduled_principal_paid: Decimal = ZERO
    unscheduled_principal_paid: Decimal = ZERO
    interest_paid: Decimal = ZERO
    fees_assessed: Decimal = ZERO
    fees_paid: Decimal = ZERO
    total_received: Decimal = ZERO
    past_due_amount: Decimal = ZERO
    past_due_count: int = 0
    days_past_due: int = 0
    delinquency: str = "CURRENT"
    next_due: dict[str, Any] | None = None
    maturity_date: str | None = None
    balloon: dict[str, Any] | None = None
    paid_off: bool = False

    def to_dict(self) -> dict[str, Any]:
        out = {}
        for k, v in self.__dict__.items():
            out[k] = dstr(v) if isinstance(v, Decimal) else (v.isoformat() if isinstance(v, date) else v)
        return out


def bucket(days: int, grace: int) -> str:
    if days <= 0:
        return "CURRENT"
    if days <= grace:
        return "WITHIN_GRACE"
    if days < 30:
        return "DPD_1_29"
    if days < 60:
        return "DPD_30_59"
    if days < 90:
        return "DPD_60_89"
    return "DPD_90_PLUS"


def account_state(conn: sqlite3.Connection, account_id: str, as_of: date | None = None) -> AccountState:
    acct = get_account(conn, account_id)
    as_of = as_of or date.today()
    inst = effective_installments(conn, account_id)
    st = AccountState(as_of=as_of, funded_principal=Decimal(acct["funded_principal"]))
    pays = [p for p in payments(conn, account_id)
            if p["entry_type"] == "PAYMENT" and not p["reversed"] and p["received_date"] <= as_of.isoformat()]
    fees = [Decimal(f["amount"]) for f in conn.execute(
        "SELECT amount FROM account_fees WHERE account_id=? AND fee_date<=?", (account_id, as_of.isoformat()))]
    for i in inst:
        i.update(paid_interest=ZERO, paid_principal=ZERO, paid_dates=[])
    fee_due = sum(fees, ZERO)
    st.fees_assessed = fee_due
    extra_principal = ZERO
    idx = 0
    for p in pays:
        money = Decimal(p["amount"])
        st.total_received += money
        while money > 0 and idx < len(inst):
            i = inst[idx]
            need_i = Decimal(i["interest"]) - i["paid_interest"]
            take = min(money, need_i)
            i["paid_interest"] += take
            money -= take
            need_p = Decimal(i["principal"]) - i["paid_principal"]
            take = min(money, need_p)
            i["paid_principal"] += take
            money -= take
            if take > 0 or need_i > 0:
                i["paid_dates"].append(p["received_date"])
            if i["paid_interest"] + i["paid_principal"] >= Decimal(i["payment"]):
                idx += 1
        take = min(money, fee_due - st.fees_paid)
        st.fees_paid += take
        money -= take
        extra_principal += money  # unscheduled principal (prepayment)
    oldest_unpaid: str | None = None
    for i in inst:
        paid = i["paid_interest"] + i["paid_principal"]
        due = Decimal(i["payment"])
        i["paid_total"] = paid
        i["remaining"] = due - paid
        i["status"] = "PAID" if paid >= due else ("PARTIAL" if paid > 0 else "UNPAID")
        i["past_due"] = i["due_date"] < as_of.isoformat() and paid < due
        if i["past_due"]:
            st.past_due_amount += due - paid
            st.past_due_count += 1
            oldest_unpaid = oldest_unpaid or i["due_date"]
        st.interest_paid += i["paid_interest"]
        st.scheduled_principal_paid += i["paid_principal"]
    st.unscheduled_principal_paid = extra_principal
    st.outstanding_principal = max(ZERO, st.funded_principal - st.scheduled_principal_paid - extra_principal)
    if oldest_unpaid:
        st.days_past_due = (as_of - date.fromisoformat(oldest_unpaid)).days
    st.delinquency = bucket(st.days_past_due, int(acct["grace_days"]))
    st.next_due = next(({"due_date": i["due_date"], "remaining": dstr(i["remaining"]), "payment": i["payment"]}
                        for i in inst if i["status"] != "PAID" and not i["past_due"]), None)
    st.maturity_date = inst[-1]["due_date"] if inst else None
    b = next((i for i in inst if i["is_balloon"]), None)
    st.balloon = {"due_date": b["due_date"], "payment": b["payment"]} if b else None
    st.paid_off = st.outstanding_principal == 0 and st.past_due_amount == 0
    for i in inst:
        for k in ("paid_interest", "paid_principal", "paid_total", "remaining"):
            i[k] = dstr(i[k])
    st.installments = inst
    return st


def mark_paid_off(conn: sqlite3.Connection, account_id: str, confirmed_by: str, note: str) -> None:
    acct = get_account(conn, account_id)
    st = account_state(conn, account_id)
    if not st.paid_off:
        raise ValidationFailure("Outstanding principal or past-due amounts remain according to recorded payments.")
    if len((confirmed_by or "").strip()) < 2:
        raise ValidationFailure("Enter who confirmed the payoff.")
    with Database.transaction(conn):
        conn.execute("UPDATE servicing_accounts SET status='PAID_OFF', updated_at=? WHERE id=?", (utcnow(), account_id))
        audit.record(conn, "loan_paid_off", "servicing_account", account_id, acct["case_id"],
                     {"confirmed_by": confirmed_by, "note": note})
        lifecycle._write(conn, get_case(conn, acct["case_id"]), "PAID_OFF",
                         f"Paid off; confirmed by {confirmed_by}. {note}".strip(), False, [], confirmed_by)


# ------------------------------------------------------------------------------------------ modifications
class ModificationIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    modification_type: Literal["rate_change", "term_extension", "payment_deferral", "re_amortization", "restructure"]
    effective_date: date
    new_annual_rate: Decimal | None = None
    new_first_payment_date: date
    remaining_term_months: int = Field(ge=1, le=600)
    remaining_amortization_months: int = Field(ge=1, le=600)
    interest_only_months: int = Field(default=0, ge=0, le=600)
    payment_frequency: Literal["monthly", "quarterly", "semiannual", "annual"] | None = None
    reason: str = Field(min_length=10, max_length=2000)
    approved_by: str = Field(min_length=2, max_length=120)

    @field_validator("new_annual_rate", mode="before")
    @classmethod
    def _r(cls, v: object) -> Decimal | None:
        return _rate(v)


def modify(conn: sqlite3.Connection, account_id: str, m: ModificationIn) -> str:
    acct = get_account(conn, account_id)
    _check_active(acct)
    current = next(s for s in schedules(conn, account_id) if s["is_current"])
    if m.effective_date.isoformat() <= current["effective_date"]:
        raise ValidationFailure("Effective date must be after the current schedule's effective date.")
    if m.new_first_payment_date <= m.effective_date:
        raise ValidationFailure("The first payment under the new terms must fall after the effective date.")
    st = account_state(conn, account_id, m.effective_date)
    unpaid_before = [i for i in st.installments if i["due_date"] < m.effective_date.isoformat() and i["status"] != "PAID"]
    if unpaid_before:
        raise ValidationFailure(f"{len(unpaid_before)} installment(s) due before the effective date are unpaid. "
                                "This version does not capitalize arrears: bring the account current first "
                                "(or record the arrears resolution as payments).")
    paid_ahead = [i for i in st.installments if i["due_date"] >= m.effective_date.isoformat()
                  and Decimal(i["paid_total"]) > 0]
    if paid_ahead:
        raise ValidationFailure("Payments were applied ahead to installments due on/after the effective date; "
                                "choose an effective date after those installments.")
    later_payments = conn.execute("SELECT COUNT(*) FROM payments WHERE account_id=? AND received_date>=?",
                                  (account_id, m.effective_date.isoformat())).fetchone()[0]
    if later_payments:
        raise ValidationFailure("Payments already recorded on/after the effective date; choose a later effective date.")
    principal = st.outstanding_principal
    if principal <= 0:
        raise ValidationFailure("No outstanding principal to modify.")
    rate = m.new_annual_rate if m.new_annual_rate is not None else Decimal(current["annual_rate"])
    freq = m.payment_frequency or current["payment_frequency"]
    before = {k: current[k] for k in ("version", "principal", "annual_rate", "payment_frequency", "term_months",
                                      "amortization_months", "interest_only_months", "first_payment_date",
                                      "maturity_date")}
    before["outstanding_principal_at_effective_date"] = dstr(principal)
    mid = new_id()
    with Database.transaction(conn):
        sid = _insert_schedule(conn, account_id, version=current["version"] + 1,
                               reason=f"Modification ({m.modification_type}): {m.reason}", effective=m.effective_date,
                               principal=principal, rate=rate, rate_type=current["rate_type"], frequency=freq,
                               term_months=m.remaining_term_months, amort_months=m.remaining_amortization_months,
                               io_months=m.interest_only_months, first_payment=m.new_first_payment_date)
        new = dict(conn.execute("SELECT * FROM payment_schedules WHERE id=?", (sid,)).fetchone())
        after = {k: new[k] for k in before if k in new}
        conn.execute(
            """INSERT INTO loan_modifications(id, account_id, modification_type, effective_date, reason, approved_by,
                   before_json, after_json, old_schedule_id, new_schedule_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, account_id, m.modification_type, m.effective_date.isoformat(), m.reason, m.approved_by,
             json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True), current["id"], sid, utcnow()))
        audit.record(conn, "loan_modified", "loan_modification", mid, acct["case_id"],
                     {"type": m.modification_type, "approved_by": m.approved_by, "before": before, "after": after})
    return mid


def modifications(conn: sqlite3.Connection, account_id: str) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute("SELECT * FROM loan_modifications WHERE account_id=? ORDER BY created_at", (account_id,)):
        d = dict(r)
        d["before"], d["after"] = json.loads(r["before_json"]), json.loads(r["after_json"])
        out.append(d)
    return out


# ------------------------------------------------------------------------------------------ monitoring items
def _add_item(conn, account_id: str, item_type: str, description: str, due: date, freq: int | None) -> str:
    iid = new_id()
    conn.execute("INSERT INTO monitoring_items(id, account_id, item_type, description, due_date, frequency_months, "
                 "created_at) VALUES (?,?,?,?,?,?,?)", (iid, account_id, item_type, description, due.isoformat(), freq,
                                                        utcnow()))
    return iid


class MonitoringItemIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    item_type: Literal["financial_statements", "insurance_renewal", "collateral_revaluation", "tax_returns", "other"]
    description: str = Field(min_length=3, max_length=300)
    due_date: date
    frequency_months: int | None = Field(default=None, ge=1, le=120)

    @field_validator("frequency_months", mode="before")
    @classmethod
    def _b(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v


def add_monitoring_item(conn: sqlite3.Connection, account_id: str, m: MonitoringItemIn) -> str:
    acct = get_account(conn, account_id)
    with Database.transaction(conn):
        iid = _add_item(conn, account_id, m.item_type, m.description, m.due_date, m.frequency_months)
        audit.record(conn, "monitoring_item_added", "monitoring_item", iid, acct["case_id"],
                     {"type": m.item_type, "due": m.due_date.isoformat()})
    return iid


def complete_monitoring_item(conn: sqlite3.Connection, account_id: str, item_id: str, note: str) -> str | None:
    acct = get_account(conn, account_id)
    row = conn.execute("SELECT * FROM monitoring_items WHERE id=? AND account_id=?", (item_id, account_id)).fetchone()
    if not row:
        raise NotFound("Item not found")
    if row["status"] == "DONE":
        raise ValidationFailure("Already completed.")
    if len((note or "").strip()) < 5:
        raise ValidationFailure("Describe what was received or reviewed.")
    nxt = None
    with Database.transaction(conn):
        conn.execute("UPDATE monitoring_items SET status='DONE', completed_at=?, completion_note=? WHERE id=?",
                     (utcnow(), note.strip(), item_id))
        if row["frequency_months"]:
            nxt = _add_item(conn, account_id, row["item_type"], row["description"],
                            add_months(date.fromisoformat(row["due_date"]), row["frequency_months"]),
                            row["frequency_months"])
        audit.record(conn, "monitoring_item_completed", "monitoring_item", item_id, acct["case_id"],
                     {"type": row["item_type"], "due": row["due_date"], "note": note.strip(), "next_item": nxt})
    return nxt


def monitoring_items(conn: sqlite3.Connection, account_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM monitoring_items WHERE account_id=? ORDER BY status DESC, "
                                          "due_date", (account_id,))]


def what_if(conn: sqlite3.Connection, account_id: str, *, rate: Decimal | None, extra_payment: Decimal,
            cfads: Decimal | None, existing_other_ds: Decimal = ZERO) -> dict[str, Any]:
    """Planning-only: re-amortize the outstanding balance over the remaining schedule under new assumptions."""
    from uwhelper.metrics.structuring import analyse

    st = account_state(conn, account_id)
    current = next(s for s in schedules(conn, account_id) if s["is_current"])
    remaining = [i for i in st.installments if i["status"] != "PAID"]
    if not remaining or st.outstanding_principal <= 0:
        raise ValidationFailure("Nothing outstanding to model.")
    step = 12 // PERIODS[current["payment_frequency"]]
    n = len(remaining)
    months = n * step
    principal = max(Decimal("0.01"), st.outstanding_principal - extra_payment)
    r = rate if rate is not None else Decimal(current["annual_rate"])
    remaining_amort = max(months, current["amortization_months"] - (len(st.installments) - n) * step)
    a = analyse(StructureInputs(principal=principal, annual_rate=r, term_months=months,
                                amortization_months=remaining_amort, payment_frequency=current["payment_frequency"],
                                cfads=cfads, existing_debt_service=existing_other_ds if cfads is not None else None))
    return {"principal": dstr(principal), "rate": dstr(r), "remaining_payments": n,
            "level_payment": dstr(a["level_payment"]), "max_annual_debt_service": dstr(a["max_annual_debt_service"]),
            "dscr_minimum": dstr(a["dscr_minimum"]), "balloon_payment": dstr(a["balloon_payment"]),
            "note": "Planning estimate only; not a modification. CFADS is from the latest verified underwriting run."}


def parse_optional_amount(v: str | None) -> Decimal | None:
    try:
        return to_decimal(v)
    except InvalidAmount as exc:
        raise ValidationFailure(str(exc)) from exc
