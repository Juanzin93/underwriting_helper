"""Deal closing, funding confirmation and the gates that decide what blocks a deal's next status.

Gates (checked by ``crm.service.transition_deal`` and the funding confirmation):
* → DUE_DILIGENCE: soft — no accepted offer.
* → CLOSING: soft — required due-diligence-phase or closing-phase conditions not satisfied/waived; required
  due-diligence items not reviewed/waived; required document requests outstanding.
* → FUNDED (only through the funding confirmation): hard — no closing record with a closing date; required
  closing-phase conditions still open. Soft — required due-diligence items outstanding; funded amount or final terms
  that differ from the accepted offer.
Hard blockers cannot be overridden; soft blockers need a documented reason (at least 20 characters), which is stored
with the status history and the audit event.

The funding confirmation is immutable and is the only way a deal becomes FUNDED by hand (a linked underwriting file
that is funded still updates the deal automatically, see ``crm.pipeline``).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from uwhelper.core.identity import current_actor
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.domain.money import dstr
from uwhelper.origination import conditions as C
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure

FUNDING_PHRASE = "I CONFIRM THIS LOAN WAS FUNDED"
MIN_OVERRIDE = 20
GATED = ("DUE_DILIGENCE", "CLOSING", "FUNDED")


@dataclass
class Blocker:
    code: str
    message: str
    hard: bool
    link: str


class ClosingIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    closing_date: date | None = None
    final_amount: Decimal | None = Field(default=None, gt=0)
    final_annual_rate: Decimal | None = None
    final_term_months: int | None = Field(default=None, ge=1, le=600)
    final_amortization_months: int | None = Field(default=None, ge=1, le=600)
    rate_reset_months: int | None = Field(default=None, ge=1, le=120)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("closing_date", "final_amount", "final_term_months", "final_amortization_months",
                     "rate_reset_months", "notes", mode="before")
    @classmethod
    def _blank(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.replace(",", "").replace("$", "").strip()
            return v or None
        return v

    @field_validator("final_annual_rate", mode="before")
    @classmethod
    def _rate(cls, v: object) -> object:
        from uwhelper.domain.models import _rate

        return _rate(v)


class FundingConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    funded_amount: Decimal = Field(gt=0)
    funding_date: date
    confirmed_by: str = Field(min_length=2, max_length=120)
    confirmation_text: str
    override_reason: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("funded_amount", mode="before")
    @classmethod
    def _money(cls, v: object) -> object:
        return str(v).replace(",", "").replace("$", "").strip() if isinstance(v, str) else v

    @field_validator("override_reason", "notes", mode="before")
    @classmethod
    def _blank(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v


def get_closing(conn: sqlite3.Connection, deal_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM deal_closings WHERE deal_id=?", (deal_id,)).fetchone()
    return dict(row) if row else None


def get_funding(conn: sqlite3.Connection, deal_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM deal_fundings WHERE deal_id=?", (deal_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["differences"] = json.loads(d.pop("differences_json"))
    return d


def save_closing(conn: sqlite3.Connection, deal_id: str, c: ClosingIn) -> None:
    deal = conn.execute("SELECT status, loan_case_id FROM deals WHERE id=?", (deal_id,)).fetchone()
    if not deal:
        raise NotFound("Deal not found")
    if deal["status"] not in ("APPROVED", "DUE_DILIGENCE", "CLOSING"):
        raise ValidationFailure("The closing record is kept once the deal is approved (and before funding).")
    if get_funding(conn, deal_id):
        raise ValidationFailure("The closing record is locked after funding.")
    vals = (c.closing_date.isoformat() if c.closing_date else None, dstr(c.final_amount), dstr(c.final_annual_rate),
            c.final_term_months, c.final_amortization_months, c.rate_reset_months, c.notes, current_actor(), utcnow())
    with Database.transaction(conn):
        conn.execute("""INSERT INTO deal_closings(deal_id, closing_date, final_amount, final_annual_rate, final_term_months,
                            final_amortization_months, rate_reset_months, notes, updated_by, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(deal_id) DO UPDATE SET closing_date=excluded.closing_date,
                            final_amount=excluded.final_amount, final_annual_rate=excluded.final_annual_rate,
                            final_term_months=excluded.final_term_months,
                            final_amortization_months=excluded.final_amortization_months,
                            rate_reset_months=excluded.rate_reset_months, notes=excluded.notes,
                            updated_by=excluded.updated_by, updated_at=excluded.updated_at""", (deal_id, *vals))
        audit.record(conn, "deal_closing_saved", "deal", deal_id, deal["loan_case_id"],
                     {"closing_date": vals[0], "final_amount": vals[1], "final_annual_rate": vals[2],
                      "final_term_months": vals[3], "final_amortization_months": vals[4], "rate_reset_months": vals[5]})


def term_differences(offer: dict[str, Any] | None, closing: dict[str, Any] | None,
                     funded_amount: Decimal | None = None) -> list[dict[str, str]]:
    """Differences between the accepted offer and the final terms / funded amount."""
    if not offer:
        return []
    out = []

    def cmp(label: str, offered: Any, final: Any) -> None:
        if final is None or offered is None:
            return
        a, b = Decimal(str(offered)), Decimal(str(final))
        if a != b:
            show = (lambda d: f"{(d * 100).normalize():f}%") if label == "annual rate" else (lambda d: f"{d.normalize():f}")
            out.append({"item": label, "offer": show(a), "final": show(b)})

    if closing:
        cmp("amount", offer["amount"], closing["final_amount"])
        cmp("annual rate", offer["annual_rate"], closing["final_annual_rate"])
        cmp("term (months)", offer["term_months"], closing["final_term_months"])
        cmp("amortization (months)", offer["amortization_months"], closing["final_amortization_months"])
    if funded_amount is not None:
        cmp("funded amount vs offer", offer["amount"], funded_amount)
    return out


def blockers(conn: sqlite3.Connection, deal: dict[str, Any], target: str,
             funded_amount: Decimal | None = None) -> list[Blocker]:
    from uwhelper.brokerage.submissions import accepted_offer
    from uwhelper.crm.requests import summary

    did = deal["id"]
    base = f"/deals/{did}"
    b: list[Blocker] = []
    offer = accepted_offer(conn, did)
    if target == "DUE_DILIGENCE" and not offer:
        b.append(Blocker("accepted_offer", "No accepted lender offer is recorded.", False, f"{base}/offers/compare"))
    if target == "CLOSING":
        n = len(C.open_required_conditions(conn, did, ("due_diligence", "closing")))
        if n:
            b.append(Blocker("conditions", f"{n} required lender condition(s) not satisfied or waived.", False,
                             f"{base}/closing#conditions"))
        dd = [i for i in C.list_dd_items(conn, did) if i["required"] and i["status"] not in C.DD_DONE]
        if dd:
            b.append(Blocker("due_diligence", f"{len(dd)} required due-diligence item(s) not reviewed or waived: "
                             + ", ".join(sorted({i['item_type'].replace('_', ' ') for i in dd})) + ".", False,
                             f"{base}/closing#diligence"))
        req = summary(conn, did)["required_outstanding"]
        if req:
            b.append(Blocker("documents", f"{req} required document request(s) outstanding.", False, f"{base}#documents"))
    if target == "FUNDED":
        closing = get_closing(conn, did)
        if not closing or not closing["closing_date"]:
            b.append(Blocker("closing_record", "Closing date / closing record not entered.", True, f"{base}/closing#record"))
        n = len(C.open_required_conditions(conn, did, ("due_diligence", "closing")))
        if n:
            b.append(Blocker("conditions", f"{n} required condition(s) still open — satisfy or waive them first.", True,
                             f"{base}/closing#conditions"))
        dd = [i for i in C.list_dd_items(conn, did) if i["required"] and i["status"] not in C.DD_DONE]
        if dd:
            b.append(Blocker("due_diligence", f"{len(dd)} required due-diligence item(s) outstanding.", False,
                             f"{base}/closing#diligence"))
        diffs = term_differences(offer, closing, funded_amount)
        if diffs:
            b.append(Blocker("terms_differ", "Final terms differ from the accepted offer: "
                             + "; ".join(f"{d['item']} {d['offer']} → {d['final']}" for d in diffs) + ".", False,
                             f"{base}/closing#record"))
        if not offer:
            b.append(Blocker("accepted_offer", "No accepted lender offer is recorded.", False, f"{base}/offers/compare"))
    return b


def check_gate(conn: sqlite3.Connection, deal: dict[str, Any], target: str, override_reason: str | None,
               funded_amount: Decimal | None = None) -> list[Blocker]:
    """Raise if blocked; return the soft blockers that were overridden (empty if none)."""
    bl = blockers(conn, deal, target, funded_amount)
    hard = [x for x in bl if x.hard]
    if hard:
        raise ValidationFailure("Blocked: " + " ".join(x.message for x in hard))
    if bl and len((override_reason or "").strip()) < MIN_OVERRIDE:
        raise ValidationFailure("Outstanding items: " + " ".join(x.message for x in bl)
                                + f" Resolve them, or give an override reason of at least {MIN_OVERRIDE} characters.")
    return bl


def confirm_funding(conn: sqlite3.Connection, deal_id: str, f: FundingConfirmIn) -> str:
    from uwhelper.brokerage.submissions import accepted_offer
    from uwhelper.crm import pipeline
    from uwhelper.crm import service as crm

    deal = crm.get_deal(conn, deal_id)
    if deal["status"] != "CLOSING":
        raise ValidationFailure(f"Funding can be confirmed only in CLOSING (the deal is {deal['status']}).")
    if f.confirmation_text.strip().upper() != FUNDING_PHRASE:
        raise ValidationFailure(f'Type exactly "{FUNDING_PHRASE}" to confirm.')
    if f.funding_date > date.today():
        raise ValidationFailure("Funding cannot be confirmed for a future date.")
    conflict = pipeline.deal_transition_conflict(conn, deal, "FUNDED")
    if conflict:
        raise ValidationFailure(conflict)
    overridden = check_gate(conn, deal, "FUNDED", f.override_reason, f.funded_amount)
    offer = accepted_offer(conn, deal_id)
    diffs = term_differences(offer, get_closing(conn, deal_id), f.funded_amount)
    fid = new_id()
    reason = f"Funding of {dstr(f.funded_amount)} on {f.funding_date.isoformat()} confirmed by {f.confirmed_by}"
    if overridden:
        reason += f". Override: {f.override_reason.strip()}"  # type: ignore[union-attr]
    with Database.transaction(conn):
        conn.execute("""INSERT INTO deal_fundings(id, deal_id, offer_id, funded_amount, funding_date, confirmed_by,
                            confirmation_text, differences_json, override_reason, notes, created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                     (fid, deal_id, offer["id"] if offer else None, dstr(f.funded_amount), f.funding_date.isoformat(),
                      f.confirmed_by, f.confirmation_text.strip().upper(), json.dumps(diffs),
                      f.override_reason if overridden else None, f.notes, utcnow()))
        audit.record(conn, "deal_funding_confirmed", "deal", deal_id, deal["loan_case_id"],
                     {"funding_id": fid, "funded_amount": dstr(f.funded_amount), "funding_date": f.funding_date.isoformat(),
                      "confirmed_by": f.confirmed_by, "differences": diffs,
                      "overridden": [x.code for x in overridden]})
        crm.write_transition(conn, deal, "FUNDED", reason[:2000], f.confirmed_by)
        from uwhelper.portfolio.monitoring import sync_reminders

        sync_reminders(conn, deal_id)
    return fid


def readiness(conn: sqlite3.Connection, deal_id: str) -> list[dict[str, Any]]:
    """For each forward status the deal can move to: what blocks it (for the deal page)."""
    from uwhelper.crm import service as crm

    deal = crm.get_deal(conn, deal_id)
    out = []
    targets = [t for t in deal["allowed_transitions"] if t in GATED and t != "FUNDED"]
    if deal["status"] == "CLOSING":
        targets.append("FUNDED")
    for t in targets:
        out.append({"to_status": t, "blockers": [asdict(x) for x in blockers(conn, deal, t)],
                    "via": "funding confirmation" if t == "FUNDED" else "status change"})
    return out
