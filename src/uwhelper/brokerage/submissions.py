"""Lender submissions and offers (term sheets).

A submission records that the deal was presented to one lender program (outside the app): draft → submitted →
under review → term sheet → accepted, or declined / withdrawn (with a reason). Recording the first submission moves
a deal that is in STRUCTURING to LENDER_SUBMISSION (recorded in the deal history).

Offers are stored exactly as received; their terms never change. They are compared with the deterministic structuring
engine (payment, annual debt service, DSCR at the offered terms, total interest and fees). Accepting an offer needs a
named person and an explicit confirmation; it moves a deal in LENDER_SUBMISSION to APPROVED with that person as the
authorizer. Nothing is sent to a lender.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from uwhelper.core.identity import current_actor
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.domain.models import _rate
from uwhelper.domain.money import dstr
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure

TRANSITIONS = {
    "draft": ["submitted", "withdrawn"],
    "submitted": ["under_review", "term_sheet", "declined", "withdrawn"],
    "under_review": ["term_sheet", "declined", "withdrawn"],
    "term_sheet": ["under_review", "declined", "withdrawn"],
    "accepted": [],
    "declined": ["draft"],
    "withdrawn": ["draft"],
}
NEEDS_REASON = {"declined", "withdrawn"}
LIVE = ("submitted", "under_review", "term_sheet")


class SubmissionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    program_id: str
    package_id: str | None = None
    lender_contact_id: str | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("package_id", "lender_contact_id", "notes", mode="before")
    @classmethod
    def _blank(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v


class SubmissionStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    to_status: str
    reason: str | None = Field(default=None, max_length=1000)
    submitted_on: date | None = None
    submitted_via: Literal["email", "portal", "in_person", "phone", "other"] | None = None

    @field_validator("reason", "submitted_on", "submitted_via", mode="before")
    @classmethod
    def _blank(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v


class OfferIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    received_on: date
    amount: Decimal = Field(gt=0)
    annual_rate: Decimal
    rate_type: Literal["fixed", "variable"] = "fixed"
    index_name: str | None = Field(default=None, max_length=60)
    spread: Decimal | None = None
    term_months: int = Field(ge=1, le=600)
    amortization_months: int = Field(ge=1, le=600)
    interest_only_months: int = Field(default=0, ge=0, le=600)
    payment_frequency: Literal["monthly", "quarterly", "semiannual", "annual"] = "monthly"
    origination_fee: Decimal = Field(default=Decimal(0), ge=0)
    other_fees: Decimal = Field(default=Decimal(0), ge=0)
    prepayment_terms: str | None = Field(default=None, max_length=1000)
    covenants: str | None = Field(default=None, max_length=2000)
    expires_on: date | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("annual_rate", "spread", mode="before")
    @classmethod
    def _r(cls, v: object) -> object:
        return _rate(v)

    @field_validator("index_name", "spread", "prepayment_terms", "covenants", "expires_on", "notes", mode="before")
    @classmethod
    def _blank(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("amount", "origination_fee", "other_fees", mode="before")
    @classmethod
    def _money(cls, v: object) -> object:
        return str(v).replace(",", "").replace("$", "").strip() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _checks(self) -> OfferIn:
        if self.annual_rate is None or self.annual_rate <= 0:
            raise ValueError("annual_rate must be greater than zero")
        if self.interest_only_months >= self.term_months:
            raise ValueError("interest-only period must be shorter than the term")
        if self.expires_on and self.expires_on < self.received_on:
            raise ValueError("an offer cannot expire before it was received")
        return self


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decided_by: str = Field(min_length=2, max_length=120)
    reason: str | None = Field(default=None, max_length=1000)
    confirm: bool = False


# ------------------------------------------------------------------------------------------ submissions
def create_submission(conn: sqlite3.Connection, deal_id: str, s: SubmissionIn) -> str:
    from uwhelper.crm import service as crm
    from uwhelper.lenders import service as lenders

    deal = crm.get_deal(conn, deal_id)
    program = lenders.get_program(conn, s.program_id)
    if s.package_id and not conn.execute("SELECT 1 FROM submission_packages WHERE id=? AND deal_id=? AND program_id=?",
                                         (s.package_id, deal_id, s.program_id)).fetchone():
        raise ValidationFailure("That package was not built for this deal and program.")
    if s.lender_contact_id and not conn.execute("SELECT 1 FROM contacts WHERE id=?", (s.lender_contact_id,)).fetchone():
        raise ValidationFailure("Lender contact does not exist.")
    sid, now, actor = new_id(), utcnow(), current_actor()
    try:
        with Database.transaction(conn):
            conn.execute("""INSERT INTO submissions(id, deal_id, program_id, package_id, lender_contact_id, status,
                                status_changed_at, notes, created_by, created_at, updated_at)
                            VALUES (?,?,?,?,?,'draft',?,?,?,?,?)""",
                         (sid, deal_id, s.program_id, s.package_id, s.lender_contact_id, now, s.notes, actor, now, now))
            conn.execute("INSERT INTO submission_status_history(submission_id, from_status, to_status, reason, actor, "
                         "occurred_at) VALUES (?, NULL, 'draft', 'Submission created', ?, ?)", (sid, actor, now))
            audit.record(conn, "submission_created", "deal", deal_id, deal["loan_case_id"],
                         {"submission_id": sid, "program_id": s.program_id, "lender": program["lender_name"],
                          "package_id": s.package_id})
    except sqlite3.IntegrityError:
        raise ValidationFailure("This deal already has a submission to that program.") from None
    return sid


def get_submission(conn: sqlite3.Connection, deal_id: str, submission_id: str) -> dict[str, Any]:
    row = conn.execute(
        """SELECT s.*, lp.name AS program, o.name AS lender, o.id AS lender_id,
                  c.first_name || ' ' || c.last_name AS contact_name, p.version AS package_version
           FROM submissions s JOIN lender_programs lp ON lp.id=s.program_id JOIN organizations o ON o.id=lp.organization_id
           LEFT JOIN contacts c ON c.id=s.lender_contact_id LEFT JOIN submission_packages p ON p.id=s.package_id
           WHERE s.id=? AND s.deal_id=?""", (submission_id, deal_id)).fetchone()
    if not row:
        raise NotFound("Submission not found")
    d = dict(row)
    d["history"] = [dict(h) for h in conn.execute(
        "SELECT * FROM submission_status_history WHERE submission_id=? ORDER BY id", (submission_id,))]
    d["offers"] = list_offers(conn, deal_id, submission_id=submission_id)
    d["allowed"] = TRANSITIONS[d["status"]]
    return d


def list_submissions(conn: sqlite3.Connection, deal_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        """SELECT s.*, lp.name AS program, o.name AS lender,
                  (SELECT COUNT(*) FROM offers f WHERE f.submission_id=s.id) AS offer_count
           FROM submissions s JOIN lender_programs lp ON lp.id=s.program_id JOIN organizations o ON o.id=lp.organization_id
           WHERE s.deal_id=? ORDER BY s.created_at""", (deal_id,))]


def _set_status(conn: sqlite3.Connection, sub: dict[str, Any], to: str, reason: str | None,
                extra: dict[str, Any] | None = None) -> None:
    now = utcnow()
    cols = {"status": to, "status_changed_at": now, "updated_at": now} | (extra or {})
    conn.execute(f"UPDATE submissions SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", (*cols.values(), sub["id"]))
    conn.execute("INSERT INTO submission_status_history(submission_id, from_status, to_status, reason, actor, occurred_at) "
                 "VALUES (?,?,?,?,?,?)", (sub["id"], sub["status"], to, reason, current_actor(), now))


def transition(conn: sqlite3.Connection, deal_id: str, submission_id: str, t: SubmissionStatusIn) -> dict[str, Any]:
    from uwhelper.crm import service as crm
    from uwhelper.crm.models import DealStatusIn

    sub = get_submission(conn, deal_id, submission_id)
    if t.to_status not in TRANSITIONS[sub["status"]]:
        raise ValidationFailure(f"A submission cannot go from {sub['status']} to {t.to_status}"
                                + (" (accept an offer instead)." if t.to_status == "accepted" else "."))
    if t.to_status in NEEDS_REASON and len((t.reason or "").strip()) < 5:
        raise ValidationFailure("Give a reason (at least 5 characters).")
    extra: dict[str, Any] = {}
    if t.to_status == "submitted":
        if not t.submitted_on or not t.submitted_via:
            raise ValidationFailure("Enter when and how the deal was submitted.")
        if t.submitted_on > date.today():
            raise ValidationFailure("The submission date cannot be in the future.")
        extra = {"submitted_on": t.submitted_on.isoformat(), "submitted_via": t.submitted_via}
    deal_moved = None
    with Database.transaction(conn):
        _set_status(conn, sub, t.to_status, t.reason, extra)
        audit.record(conn, "submission_status_changed", "deal", deal_id, None,
                     {"submission_id": submission_id, "from": sub["status"], "to": t.to_status, "reason": t.reason,
                      **extra})
        if t.to_status == "submitted":
            deal = crm.get_deal(conn, deal_id)
            if deal["status"] == "STRUCTURING":
                deal_moved = crm.transition_deal(conn, deal_id, DealStatusIn(
                    to_status="LENDER_SUBMISSION",
                    reason=f"Submitted to {sub['lender']} ({sub['program']}) on {t.submitted_on.isoformat()}"))
    return {"status": t.to_status, "deal_status": deal_moved}


# ------------------------------------------------------------------------------------------ offers
def add_offer(conn: sqlite3.Connection, deal_id: str, submission_id: str, o: OfferIn) -> str:
    sub = get_submission(conn, deal_id, submission_id)
    if sub["status"] not in LIVE:
        raise ValidationFailure("Record the submission as submitted before adding an offer.")
    if o.received_on > date.today():
        raise ValidationFailure("The offer cannot be received in the future.")
    oid, now = new_id(), utcnow()
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO offers(id, submission_id, deal_id, received_on, amount, annual_rate, rate_type, index_name,
                   spread, term_months, amortization_months, interest_only_months, payment_frequency, origination_fee,
                   other_fees, prepayment_terms, covenants, expires_on, notes, created_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (oid, submission_id, deal_id, o.received_on.isoformat(), dstr(o.amount), dstr(o.annual_rate), o.rate_type,
             o.index_name, dstr(o.spread), o.term_months, o.amortization_months, o.interest_only_months,
             o.payment_frequency, dstr(o.origination_fee), dstr(o.other_fees), o.prepayment_terms, o.covenants,
             o.expires_on.isoformat() if o.expires_on else None, o.notes, current_actor(), now))
        if sub["status"] != "term_sheet":
            _set_status(conn, sub, "term_sheet", "Offer recorded")
        audit.record(conn, "offer_recorded", "deal", deal_id, None,
                     {"offer_id": oid, "submission_id": submission_id, "amount": dstr(o.amount),
                      "annual_rate": dstr(o.annual_rate), "term_months": o.term_months})
    return oid


def list_offers(conn: sqlite3.Connection, deal_id: str, *, submission_id: str | None = None) -> list[dict[str, Any]]:
    sql = """SELECT f.*, lp.name AS program, o.name AS lender FROM offers f JOIN submissions s ON s.id=f.submission_id
             JOIN lender_programs lp ON lp.id=s.program_id JOIN organizations o ON o.id=lp.organization_id
             WHERE f.deal_id=?"""
    params: list[Any] = [deal_id]
    if submission_id:
        sql += " AND f.submission_id=?"
        params.append(submission_id)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY f.received_on, f.created_at", params)]
    today = date.today().isoformat()
    for r in rows:
        r["expired"] = r["status"] == "open" and bool(r["expires_on"]) and r["expires_on"] < today
    return rows


def get_offer(conn: sqlite3.Connection, deal_id: str, offer_id: str) -> dict[str, Any]:
    for o in list_offers(conn, deal_id):
        if o["id"] == offer_id:
            return o
    raise NotFound("Offer not found")


def accepted_offer(conn: sqlite3.Connection, deal_id: str) -> dict[str, Any] | None:
    return next((o for o in list_offers(conn, deal_id) if o["status"] == "accepted"), None)


def accept_offer(conn: sqlite3.Connection, deal_id: str, offer_id: str, d: DecisionIn) -> dict[str, Any]:
    from uwhelper.crm import service as crm
    from uwhelper.crm.models import DealStatusIn

    offer = get_offer(conn, deal_id, offer_id)
    if not d.confirm:
        raise ValidationFailure("Confirm that the borrower accepted this offer (a human decision made outside the app).")
    if offer["status"] != "open":
        raise ValidationFailure(f"This offer is already {offer['status']}.")
    if offer["expired"]:
        raise ValidationFailure("This offer has expired; record a renewed offer from the lender.")
    if accepted_offer(conn, deal_id):
        raise ValidationFailure("Another offer on this deal is already accepted.")
    sub = get_submission(conn, deal_id, offer["submission_id"])
    now = utcnow()
    deal_moved = None
    with Database.transaction(conn):
        conn.execute("UPDATE offers SET status='accepted', decided_by=?, decided_at=?, decision_reason=? WHERE id=?",
                     (d.decided_by, now, d.reason, offer_id))
        _set_status(conn, sub, "accepted", f"Offer accepted by {d.decided_by}")
        audit.record(conn, "offer_accepted", "deal", deal_id, None,
                     {"offer_id": offer_id, "decided_by": d.decided_by, "amount": offer["amount"],
                      "annual_rate": offer["annual_rate"], "lender": offer["lender"]})
        deal = crm.get_deal(conn, deal_id)
        if deal["status"] == "LENDER_SUBMISSION":
            deal_moved = crm.transition_deal(conn, deal_id, DealStatusIn(
                to_status="APPROVED", authorized_by=d.decided_by, confirm=True,
                reason=f"Accepted offer from {offer['lender']}: {offer['amount']} at {offer['annual_rate']} "
                       f"for {offer['term_months']} months"))
    return {"deal_status": deal_moved}


def decline_offer(conn: sqlite3.Connection, deal_id: str, offer_id: str, d: DecisionIn) -> None:
    offer = get_offer(conn, deal_id, offer_id)
    if offer["status"] != "open":
        raise ValidationFailure(f"This offer is already {offer['status']}.")
    if len((d.reason or "").strip()) < 5:
        raise ValidationFailure("Give a reason for declining (at least 5 characters).")
    with Database.transaction(conn):
        conn.execute("UPDATE offers SET status='declined', decided_by=?, decided_at=?, decision_reason=? WHERE id=?",
                     (d.decided_by, utcnow(), d.reason, offer_id))
        audit.record(conn, "offer_declined", "deal", deal_id, None,
                     {"offer_id": offer_id, "decided_by": d.decided_by, "reason": d.reason})


def compare_offers(conn: sqlite3.Connection, deal_id: str) -> dict[str, Any]:
    """Each offer through the structuring engine, with the cash flow of the linked file's latest run."""
    from uwhelper.crm import service as crm
    from uwhelper.metrics.structuring import StructureError, StructureInputs, analyse
    from uwhelper.workflow.currency import latest_base_run

    deal = crm.get_deal(conn, deal_id)
    basis: dict[str, Any] = {"run_number": None, "cash_flow_label": None}
    cfads = existing = collateral = cost = None
    if deal["loan_case_id"]:
        run = latest_base_run(conn, deal["loan_case_id"])
        if run:
            ms = {r["metric_key"]: r for r in conn.execute(
                "SELECT metric_key, value, status FROM run_metrics WHERE run_id=?", (run["id"],))}

            def val(k: str) -> Decimal | None:
                m = ms.get(k)
                return Decimal(m["value"]) if m and m["status"] == "computed" and m["value"] is not None else None

            basis["run_number"] = run["run_number"]
            if deal["deal_type"] == "cre" and val("noi") is not None:
                cfads, existing, basis["cash_flow_label"] = val("noi"), Decimal(0), "NOI"
            else:
                cfads, existing, basis["cash_flow_label"] = val("cfads"), val("existing_debt_service"), "CFADS"
            collateral = val("collateral_value")
            import json as _json

            t = (_json.loads(run["inputs_json"]).get("terms") or {})
            cost = Decimal(t["project_total_cost"]) if t.get("project_total_cost") else None
    rows = []
    for o in list_offers(conn, deal_id):
        si = StructureInputs(principal=Decimal(o["amount"]), annual_rate=Decimal(o["annual_rate"]),
                             term_months=o["term_months"], amortization_months=o["amortization_months"],
                             interest_only_months=o["interest_only_months"], payment_frequency=o["payment_frequency"],
                             origination_fee=Decimal(o["origination_fee"]), other_costs=Decimal(o["other_fees"]),
                             cfads=cfads, existing_debt_service=existing, collateral_value=collateral, project_cost=cost)
        try:
            a = analyse(si)
            a.pop("schedule", None)
            res = {k: a[k] for k in ("level_payment", "interest_only_payment", "year1_debt_service",
                                     "steady_state_annual_debt_service", "total_interest", "total_fees",
                                     "balloon_payment", "dscr_year1", "dscr_amortizing", "ltv", "ltc", "net_proceeds")}
            res["total_cost"] = a["total_interest"] + a["total_fees"]
            rows.append({"offer": o, "analysis": res, "error": None})
        except StructureError as exc:
            rows.append({"offer": o, "analysis": None, "error": str(exc)})
    return {"basis": basis, "rows": rows}
