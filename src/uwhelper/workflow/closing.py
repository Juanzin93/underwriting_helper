"""Closing workspace and funding confirmation.

The application tracks closing prerequisites; it does not prepare legally sufficient documents,
execute agreements, move money or release funds. "Funded" is recorded only after a human
explicitly confirms that funding actually happened outside this application.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.domain.models import _dec
from uwhelper.domain.money import dstr
from uwhelper.repo import audit
from uwhelper.repo.cases import ValidationFailure, get_case
from uwhelper.workflow import lifecycle
from uwhelper.workflow.lifecycle import TransitionRefused, funding_blockers, require_stage

FUNDING_PHRASE = "I CONFIRM THIS LOAN WAS FUNDED"


class ClosingIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    scheduled_closing_date: str | None = None
    disbursement_instructions: str | None = Field(default=None, max_length=4000)
    disbursement_status: Literal["NOT_STARTED", "INSTRUCTIONS_RECORDED", "REPORTED_DISBURSED"] = "NOT_STARTED"
    outstanding_issues: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("scheduled_closing_date", mode="before")
    @classmethod
    def _d(cls, v: object) -> str | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        try:
            return date.fromisoformat(str(v)[:10]).isoformat()
        except ValueError as exc:
            raise ValueError("date must be YYYY-MM-DD") from exc


class FundingIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    funded_amount: Decimal
    funding_date: date
    first_payment_date: date
    confirmed_by: str = Field(min_length=2, max_length=120)
    confirmation_text: str
    override_reason: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("funded_amount", mode="before")
    @classmethod
    def _a(cls, v: object) -> Decimal:
        d = _dec(v, allow_negative=False, required=True)
        if d is None or d <= 0:
            raise ValueError("funded amount must be greater than zero")
        return d


def get_closing(conn: sqlite3.Connection, case_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM closings WHERE case_id=?", (case_id,)).fetchone()
    return dict(row) if row else None


def save_closing(conn: sqlite3.Connection, case_id: str, c: ClosingIn) -> None:
    case = get_case(conn, case_id)
    require_stage(case, ("DUE_DILIGENCE", "CLOSING"), "Editing the closing workspace")
    before = get_closing(conn, case_id)
    now = utcnow()
    data = c.model_dump()
    with Database.transaction(conn):
        if before is None:
            conn.execute(
                """INSERT INTO closings(id, case_id, scheduled_closing_date, disbursement_instructions, disbursement_status,
                       outstanding_issues, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (new_id(), case_id, c.scheduled_closing_date, c.disbursement_instructions, c.disbursement_status,
                 c.outstanding_issues, c.notes, now, now))
            changed = {k: [None, v] for k, v in data.items() if v}
        else:
            conn.execute(
                """UPDATE closings SET scheduled_closing_date=?, disbursement_instructions=?, disbursement_status=?,
                       outstanding_issues=?, notes=?, updated_at=? WHERE case_id=?""",
                (c.scheduled_closing_date, c.disbursement_instructions, c.disbursement_status, c.outstanding_issues,
                 c.notes, now, case_id))
            changed = {k: [before[k], v] for k, v in data.items() if before.get(k) != v}
        # Disbursement instructions may contain account details: the audit keeps only that they changed.
        if "disbursement_instructions" in changed:
            changed["disbursement_instructions"] = ["(changed)", "(changed)"]
        audit.record(conn, "closing_updated", "closing", case_id, case_id, {"changed": changed})


def confirm_funding(conn: sqlite3.Connection, case_id: str, f: FundingIn) -> str:
    case = get_case(conn, case_id)
    require_stage(case, ("CLOSING",), "Funding confirmation")
    if f.confirmation_text.strip().upper() != FUNDING_PHRASE:
        raise ValidationFailure(f'Type exactly "{FUNDING_PHRASE}" to confirm.')
    if f.first_payment_date <= f.funding_date:
        raise ValidationFailure("The first payment date must be after the funding date.")
    if f.funding_date > date.today():
        raise ValidationFailure("Funding cannot be confirmed for a future date. Record it on or after the funding day.")
    blockers = funding_blockers(conn, case_id)
    hard = [b for b in blockers if b.hard]
    if hard:
        raise TransitionRefused("Funding blocked: " + " ".join(b.message for b in hard), blockers)
    if blockers and len((f.override_reason or "").strip()) < lifecycle.MIN_OVERRIDE_REASON:
        raise TransitionRefused("Outstanding items need a documented override reason (at least "
                                f"{lifecycle.MIN_OVERRIDE_REASON} characters): "
                                + " ".join(b.message for b in blockers), blockers)
    executed = conn.execute("SELECT * FROM term_sets WHERE case_id=? AND term_type='executed' ORDER BY revision DESC "
                            "LIMIT 1", (case_id,)).fetchone()
    if f.funded_amount > Decimal(executed["principal"]):
        raise ValidationFailure("Funded amount exceeds the executed principal.")
    fid = new_id()
    reason = f"Funding confirmed by {f.confirmed_by} on {f.funding_date.isoformat()}"
    if blockers:
        reason += f". Override: {f.override_reason.strip()}"  # type: ignore[union-attr]
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO fundings(id, case_id, executed_terms_id, funded_amount, funding_date, first_payment_date,
                   confirmed_by, confirmation_text, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (fid, case_id, executed["id"], dstr(f.funded_amount), f.funding_date.isoformat(),
             f.first_payment_date.isoformat(), f.confirmed_by, f.confirmation_text.strip().upper(), f.notes, utcnow()))
        audit.record(conn, "funding_confirmed", "funding", fid, case_id,
                     {"funded_amount": dstr(f.funded_amount), "executed_principal": executed["principal"],
                      "funding_date": f.funding_date.isoformat(), "confirmed_by": f.confirmed_by,
                      "overridden": [asdict(b)["code"] for b in blockers]})
        case_now = get_case(conn, case_id)
        lifecycle._write(conn, case_now, "FUNDED", reason, bool(blockers), blockers, f.confirmed_by)
    return fid


def get_funding(conn: sqlite3.Connection, case_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM fundings WHERE case_id=?", (case_id,)).fetchone()
    return dict(row) if row else None
