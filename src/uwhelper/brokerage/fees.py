"""Fee agreements and the commission ledger.

Agreements per deal:
* ``broker_fee``          — paid to the broker by the borrower (flat, or % of the funded amount)
* ``lender_compensation`` — paid to the broker by the lender (flat, or % of the funded amount)
* ``referral_split``      — owed by the broker to a referral partner (flat, % of the funded amount, or % of the
  broker's gross compensation on the deal)

Agreements are never edited: void one (with a reason) and add a replacement.

Ledger (append-only): ``expected_in`` / ``received`` for money owed to the broker, ``expected_out`` / ``paid_out`` for
money the broker owes. A mistake is fixed with a reversing entry. Expected entries are generated from the
agreements once the deal is funded, using the funded amount from (in order): the linked file's confirmed funding,
the accepted offer, or the requested amount (labelled an estimate). All amounts are Decimal, rounded half-up to
cents per line; a referral split on gross compensation uses the sum of the rounded broker lines.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from uwhelper.core.identity import current_actor
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.domain.money import dstr
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure

CENT = Decimal("0.01")
INBOUND = ("broker_fee", "lender_compensation")
MAX_PERCENT_OF_FUNDED = Decimal("0.25")


def q2(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def _percent(v: object) -> Decimal:
    s = str(v).strip()
    d = Decimal(s.rstrip("%").replace(",", ""))
    return d / 100 if s.endswith("%") or d >= 1 else d


class FeeAgreementIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["broker_fee", "lender_compensation", "referral_split"]
    basis: Literal["flat", "percent_of_funded", "percent_of_broker_gross"]
    value: Decimal
    party_organization_id: str | None = None
    party_contact_id: str | None = None
    description: str | None = Field(default=None, max_length=500)

    @field_validator("party_organization_id", "party_contact_id", "description", mode="before")
    @classmethod
    def _blank(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="before")
    @classmethod
    def _value(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("value") not in (None, ""):
            raw = data["value"]
            if data.get("basis") in ("percent_of_funded", "percent_of_broker_gross"):
                try:
                    data = data | {"value": _percent(raw)}
                except Exception:
                    raise ValueError("value must be a percentage such as 1% or 0.01") from None
            elif isinstance(raw, str):
                data = data | {"value": raw.replace(",", "").replace("$", "").strip()}
        return data

    @model_validator(mode="after")
    def _checks(self) -> FeeAgreementIn:
        if self.value <= 0:
            raise ValueError("value must be greater than zero")
        if self.basis == "percent_of_broker_gross" and self.kind != "referral_split":
            raise ValueError("percent of broker gross applies to referral splits only")
        if self.basis == "percent_of_funded" and self.value > MAX_PERCENT_OF_FUNDED:
            raise ValueError("a percentage of the funded amount above 25% is almost certainly a typo")
        if self.basis == "percent_of_broker_gross" and self.value > 1:
            raise ValueError("a split cannot exceed 100% of the broker's gross")
        if self.kind == "referral_split" and not (self.party_organization_id or self.party_contact_id):
            raise ValueError("a referral split needs the referral partner (organization or contact)")
        return self


class LedgerEntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    entry_type: Literal["received", "paid_out"]
    amount: Decimal = Field(gt=0)
    occurred_on: date
    fee_agreement_id: str | None = None
    counterparty: str | None = Field(default=None, max_length=200)
    reference: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("fee_agreement_id", "counterparty", "reference", "note", mode="before")
    @classmethod
    def _blank(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("amount", mode="before")
    @classmethod
    def _money(cls, v: object) -> object:
        return str(v).replace(",", "").replace("$", "").strip() if isinstance(v, str) else v


# ------------------------------------------------------------------------------------------ agreements
def add_agreement(conn: sqlite3.Connection, deal_id: str, a: FeeAgreementIn) -> str:
    from uwhelper.crm import service as crm

    crm.get_deal(conn, deal_id)
    if a.party_organization_id and not conn.execute("SELECT 1 FROM organizations WHERE id=?",
                                                    (a.party_organization_id,)).fetchone():
        raise ValidationFailure("Organization does not exist.")
    if a.party_contact_id and not conn.execute("SELECT 1 FROM contacts WHERE id=?", (a.party_contact_id,)).fetchone():
        raise ValidationFailure("Contact does not exist.")
    fid = new_id()
    with Database.transaction(conn):
        conn.execute("""INSERT INTO fee_agreements(id, deal_id, kind, basis, value, party_organization_id,
                            party_contact_id, description, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (fid, deal_id, a.kind, a.basis, dstr(a.value), a.party_organization_id, a.party_contact_id,
                      a.description, current_actor(), utcnow()))
        audit.record(conn, "fee_agreement_added", "deal", deal_id, None,
                     {"agreement_id": fid, "kind": a.kind, "basis": a.basis, "value": dstr(a.value)})
    return fid


def void_agreement(conn: sqlite3.Connection, deal_id: str, agreement_id: str, reason: str) -> None:
    row = conn.execute("SELECT * FROM fee_agreements WHERE id=? AND deal_id=?", (agreement_id, deal_id)).fetchone()
    if not row:
        raise NotFound("Fee agreement not found")
    if row["voided"]:
        raise ValidationFailure("Already voided.")
    if len((reason or "").strip()) < 5:
        raise ValidationFailure("Give a reason for voiding (at least 5 characters).")
    if conn.execute("SELECT 1 FROM commission_entries WHERE fee_agreement_id=? AND id NOT IN "
                    "(SELECT reverses_entry_id FROM commission_entries WHERE reverses_entry_id IS NOT NULL) "
                    "AND reverses_entry_id IS NULL", (agreement_id,)).fetchone():
        raise ValidationFailure("Ledger entries use this agreement; reverse them first.")
    with Database.transaction(conn):
        conn.execute("UPDATE fee_agreements SET voided=1, void_reason=? WHERE id=?", (reason.strip(), agreement_id))
        audit.record(conn, "fee_agreement_voided", "deal", deal_id, None, {"agreement_id": agreement_id,
                                                                           "reason": reason.strip()})


def list_agreements(conn: sqlite3.Connection, deal_id: str, include_voided: bool = True) -> list[dict[str, Any]]:
    rows = [dict(r) for r in conn.execute(
        """SELECT f.*, o.name AS party_org, c.first_name || ' ' || c.last_name AS party_contact FROM fee_agreements f
           LEFT JOIN organizations o ON o.id=f.party_organization_id LEFT JOIN contacts c ON c.id=f.party_contact_id
           WHERE f.deal_id=? ORDER BY f.voided, f.created_at""", (deal_id,))]
    return rows if include_voided else [r for r in rows if not r["voided"]]


# ------------------------------------------------------------------------------------------ calculation
def funded_amount(conn: sqlite3.Connection, deal_id: str) -> tuple[Decimal | None, str]:
    from uwhelper.brokerage.submissions import accepted_offer

    deal = conn.execute("SELECT loan_case_id, requested_amount FROM deals WHERE id=?", (deal_id,)).fetchone()
    if not deal:
        raise NotFound("Deal not found")
    if deal["loan_case_id"]:
        f = conn.execute("SELECT funded_amount FROM fundings WHERE case_id=?", (deal["loan_case_id"],)).fetchone()
        if f:
            return Decimal(f["funded_amount"]), "confirmed funding on the underwriting file"
    offer = accepted_offer(conn, deal_id)
    if offer:
        return Decimal(offer["amount"]), f"accepted offer from {offer['lender']}"
    if deal["requested_amount"]:
        return Decimal(deal["requested_amount"]), "requested amount (ESTIMATE — no funding or accepted offer recorded)"
    return None, "no amount available"


def compute(agreements: list[dict[str, Any]], funded: Decimal | None) -> dict[str, Any]:
    lines_in, lines_out, problems = [], [], []
    for a in agreements:
        if a["voided"] or a["kind"] not in INBOUND:
            continue
        v = Decimal(a["value"])
        if a["basis"] == "flat":
            amt = q2(v)
        elif funded is None:
            problems.append(f"{a['kind'].replace('_', ' ')}: needs a funded amount")
            continue
        else:
            amt = q2(funded * v)
        lines_in.append({"agreement_id": a["id"], "kind": a["kind"], "basis": a["basis"], "value": a["value"],
                         "amount": amt})
    gross = sum((x["amount"] for x in lines_in), Decimal(0))
    for a in agreements:
        if a["voided"] or a["kind"] != "referral_split":
            continue
        v = Decimal(a["value"])
        if a["basis"] == "flat":
            amt = q2(v)
        elif a["basis"] == "percent_of_broker_gross":
            amt = q2(gross * v)
        elif funded is None:
            problems.append("referral split: needs a funded amount")
            continue
        else:
            amt = q2(funded * v)
        lines_out.append({"agreement_id": a["id"], "kind": a["kind"], "basis": a["basis"], "value": a["value"],
                          "amount": amt, "party": a.get("party_org") or a.get("party_contact")})
    paid_out = sum((x["amount"] for x in lines_out), Decimal(0))
    return {"funded": funded, "lines_in": lines_in, "lines_out": lines_out, "gross": gross, "payouts": paid_out,
            "net": gross - paid_out, "problems": problems}


# ------------------------------------------------------------------------------------------ ledger
def _entries(conn: sqlite3.Connection, deal_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM commission_entries WHERE deal_id=? ORDER BY occurred_on, created_at", (deal_id,))]


def effective_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Entries that are neither reversals nor reversed."""
    reversed_ids = {e["reverses_entry_id"] for e in entries if e["reverses_entry_id"]}
    return [e for e in entries if not e["reverses_entry_id"] and e["id"] not in reversed_ids]


def record_expected(conn: sqlite3.Connection, deal_id: str, occurred_on: date) -> dict[str, Any]:
    deal = conn.execute("SELECT status, loan_case_id FROM deals WHERE id=?", (deal_id,)).fetchone()
    if not deal:
        raise NotFound("Deal not found")
    if deal["status"] not in ("FUNDED", "SERVICING"):
        raise ValidationFailure("Expected commissions are recorded once the deal is funded.")
    if any(e["entry_type"] in ("expected_in", "expected_out") for e in effective_entries(_entries(conn, deal_id))):
        raise ValidationFailure("Expected commissions are already recorded; reverse them to recalculate.")
    funded, basis = funded_amount(conn, deal_id)
    calc = compute(list_agreements(conn, deal_id, include_voided=False), funded)
    if calc["problems"]:
        raise ValidationFailure("; ".join(calc["problems"]))
    if not calc["lines_in"] and not calc["lines_out"]:
        raise ValidationFailure("No active fee agreements on this deal.")
    basis_json = json.dumps({"funded_amount": dstr(funded), "funded_basis": basis, "gross": dstr(calc["gross"])})
    now, actor = utcnow(), current_actor()
    with Database.transaction(conn):
        for kind, lines in (("expected_in", calc["lines_in"]), ("expected_out", calc["lines_out"])):
            for ln in lines:
                if ln["amount"] <= 0:
                    continue
                conn.execute("""INSERT INTO commission_entries(id, deal_id, fee_agreement_id, entry_type, amount,
                                    occurred_on, counterparty, basis_json, created_by, created_at)
                                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                             (new_id(), deal_id, ln["agreement_id"], kind, dstr(ln["amount"]), occurred_on.isoformat(),
                              ln.get("party"), basis_json, actor, now))
        audit.record(conn, "commissions_expected_recorded", "deal", deal_id, deal["loan_case_id"],
                     {"funded_amount": dstr(funded), "funded_basis": basis, "gross": dstr(calc["gross"]),
                      "payouts": dstr(calc["payouts"])})
    return calc | {"funded_basis": basis}


def record_entry(conn: sqlite3.Connection, deal_id: str, e: LedgerEntryIn) -> str:
    if not conn.execute("SELECT 1 FROM deals WHERE id=?", (deal_id,)).fetchone():
        raise NotFound("Deal not found")
    if e.fee_agreement_id:
        a = conn.execute("SELECT kind FROM fee_agreements WHERE id=? AND deal_id=?", (e.fee_agreement_id, deal_id)).fetchone()
        if not a:
            raise ValidationFailure("Fee agreement not found on this deal.")
        if (e.entry_type == "received") != (a["kind"] in INBOUND):
            raise ValidationFailure("Received payments go against broker fees / lender compensation; payouts against "
                                    "referral splits.")
    if e.occurred_on > date.today():
        raise ValidationFailure("The payment date cannot be in the future.")
    eid = new_id()
    with Database.transaction(conn):
        conn.execute("""INSERT INTO commission_entries(id, deal_id, fee_agreement_id, entry_type, amount, occurred_on,
                            counterparty, reference, note, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                     (eid, deal_id, e.fee_agreement_id, e.entry_type, dstr(e.amount), e.occurred_on.isoformat(),
                      e.counterparty, e.reference, e.note, current_actor(), utcnow()))
        audit.record(conn, f"commission_{e.entry_type}", "deal", deal_id, None,
                     {"entry_id": eid, "amount": dstr(e.amount), "agreement_id": e.fee_agreement_id,
                      "reference": e.reference})
    return eid


def reverse_entry(conn: sqlite3.Connection, deal_id: str, entry_id: str, reason: str) -> str:
    entries = _entries(conn, deal_id)
    orig = next((e for e in entries if e["id"] == entry_id), None)
    if not orig:
        raise NotFound("Ledger entry not found")
    if orig["reverses_entry_id"]:
        raise ValidationFailure("A reversing entry cannot itself be reversed; add a new entry instead.")
    if any(e["reverses_entry_id"] == entry_id for e in entries):
        raise ValidationFailure("This entry is already reversed.")
    if len((reason or "").strip()) < 5:
        raise ValidationFailure("Give a reason for the reversal (at least 5 characters).")
    rid = new_id()
    with Database.transaction(conn):
        conn.execute("""INSERT INTO commission_entries(id, deal_id, fee_agreement_id, entry_type, amount, occurred_on,
                            counterparty, reference, reverses_entry_id, note, created_by, created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (rid, deal_id, orig["fee_agreement_id"], orig["entry_type"], orig["amount"],
                      date.today().isoformat(), orig["counterparty"], orig["reference"], entry_id, reason.strip(),
                      current_actor(), utcnow()))
        audit.record(conn, "commission_entry_reversed", "deal", deal_id, None,
                     {"entry_id": entry_id, "reversal_id": rid, "reason": reason.strip()})
    return rid


def _bucket(days: int) -> str:
    return "0-30" if days <= 30 else "31-60" if days <= 60 else "61-90" if days <= 90 else "90+"


def ledger(conn: sqlite3.Connection, deal_id: str, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    entries = _entries(conn, deal_id)
    eff = effective_entries(entries)

    def total(t: str) -> Decimal:
        return sum((Decimal(e["amount"]) for e in eff if e["entry_type"] == t), Decimal(0))

    exp_in, rec, exp_out, paid = total("expected_in"), total("received"), total("expected_out"), total("paid_out")
    receivable = exp_in - rec
    oldest = min((e["occurred_on"] for e in eff if e["entry_type"] == "expected_in"), default=None)
    age = (today - date.fromisoformat(oldest)).days if oldest and receivable > 0 else None
    return {"entries": entries, "reversed_ids": {e["reverses_entry_id"] for e in entries if e["reverses_entry_id"]},
            "expected_in": exp_in, "received": rec, "receivable": receivable, "expected_out": exp_out,
            "paid_out": paid, "payable": exp_out - paid, "net_received": rec - paid,
            "receivable_age_days": age, "bucket": _bucket(age) if age is not None else None}


def commissions_report(conn: sqlite3.Connection, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    rows = []
    for d in conn.execute("""SELECT DISTINCT d.id, d.deal_number, d.name, d.status FROM deals d
                             JOIN commission_entries c ON c.deal_id=d.id ORDER BY d.deal_number"""):
        lg = ledger(conn, d["id"], today)
        rows.append({"deal_id": d["id"], "deal_number": d["deal_number"], "name": d["name"], "status": d["status"],
                     **{k: lg[k] for k in ("expected_in", "received", "receivable", "expected_out", "paid_out",
                                           "payable", "receivable_age_days", "bucket")}})
    buckets = {b: Decimal(0) for b in ("0-30", "31-60", "61-90", "90+")}
    for r in rows:
        if r["receivable"] > 0 and r["bucket"]:
            buckets[r["bucket"]] += r["receivable"]
    totals = {k: sum((r[k] for r in rows), Decimal(0)) for k in ("expected_in", "received", "receivable",
                                                                  "expected_out", "paid_out", "payable")}
    return {"rows": rows, "buckets": buckets, "totals": totals, "as_of": today.isoformat()}
