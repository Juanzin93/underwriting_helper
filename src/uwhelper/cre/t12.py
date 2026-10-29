"""Property operating statement (T-12) import.

A CSV with a line description and either one amount column (total / amount / t12 / annual) or monthly columns
(which are added up). Each line is mapped to a property field by keyword (shown in the preview; the user can change
every mapping):

* gross_potential_rent, vacancy_credit_loss (stored as a positive amount), other_property_income
* property_operating_expenses (all operating expense lines are added up)
* net_operating_income_reported (an NOI line, informational)
* ignore — subtotals, debt service, depreciation, amortization, capital items and reserves (not operating expenses)

Reconciliation: effective gross income (rent − vacancy + other income) must equal the "total income" printed on the
statement, and operating expenses must equal its "total expenses", each within $1.00. The totals become user-entered
facts for the statement period; they are VERIFIED only when both totals reconcile and a named person confirms.
Otherwise they are saved as unverified candidates. Existing facts are never overwritten — differing values appear
as contradictions to resolve. The mapped lines are kept (immutable) so every total can be traced back to its lines.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from uwhelper.core.csvutil import read_rows
from uwhelper.core.identity import current_actor
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.domain.money import InvalidAmount, dstr, to_decimal
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure, get_case

TOLERANCE = Decimal("1.00")
TARGETS = ("gross_potential_rent", "vacancy_credit_loss", "other_property_income", "property_operating_expenses",
           "net_operating_income_reported", "ignore")
TARGET_LABELS = {
    "gross_potential_rent": "Gross potential / rental income", "vacancy_credit_loss": "Vacancy, concessions & credit loss",
    "other_property_income": "Other property income", "property_operating_expenses": "Operating expense",
    "net_operating_income_reported": "NOI (as printed)", "ignore": "Ignore (subtotal / not operating)",
}
LABEL_COLS = ("line", "description", "account", "category", "item", "name", "line_item", "gl_account")
TOTAL_COLS = ("total", "amount", "t12", "t_12", "annual", "trailing_12", "ttm", "ytd_total", "12_month_total")
_EXCLUDE = re.compile(r"debt service|mortgage|principal|interest expense|loan payment|depreciation|amortization|"
                      r"capital|capex|reserve|improvement|leasing commission|tenant improvement|income tax")
_SUBTOTAL = re.compile(r"^(total|net|gross)\b|\btotal\b|subtotal|effective gross")
_NOI = re.compile(r"net operating income|\bnoi\b")
_VACANCY = re.compile(r"vacancy|concession|bad debt|credit loss|loss to lease|collection loss|write.?off")
_GPR = re.compile(r"gross potential|potential rent|scheduled rent|base rent|rental income|^rent|rent income|"
                  r"apartment rent|office rent|retail rent|lease income")
_OTHER = re.compile(r"parking|laundry|late fee|fees|reimburs|\bcam\b|utility income|other income|storage|pet|"
                    r"application|vending|miscellaneous income|signage|antenna")


@dataclass
class Line:
    line: int
    label: str
    amount: Decimal | None
    section: str  # income | expense | unknown
    suggested: str
    errors: list[str] = field(default_factory=list)


@dataclass
class Statement:
    amount_basis: str
    lines: list[Line]


def suggest(label: str, section: str) -> str:
    low = label.lower()
    if _NOI.search(low):
        return "net_operating_income_reported"
    if _SUBTOTAL.search(low) or _EXCLUDE.search(low):
        return "ignore"
    if section == "expense":
        return "property_operating_expenses"
    if _VACANCY.search(low):
        return "vacancy_credit_loss"
    if _GPR.search(low):
        return "gross_potential_rent"
    if _OTHER.search(low):
        return "other_property_income"
    if section == "income":
        return "other_property_income"
    if section == "expense":
        return "property_operating_expenses"
    return "property_operating_expenses" if "expense" in low or "fee" not in low else "other_property_income"


def parse_statement(text: str) -> Statement:
    header, rows = read_rows(text, 2000)
    label_idx = next((i for i, h in enumerate(header) if h in LABEL_COLS), 0)
    total_idx = next((i for i, h in enumerate(header) if h in TOTAL_COLS), None)
    amount_idx = [total_idx] if total_idx is not None else [i for i in range(len(header)) if i != label_idx]
    basis = f"column '{header[total_idx]}'" if total_idx is not None else f"sum of {len(amount_idx)} column(s)"
    lines, section = [], "unknown"
    for n, values in rows:
        label = values[label_idx].strip()[:200] if label_idx < len(values) else ""
        raw = [values[i] for i in amount_idx if i < len(values) and values[i].strip()]
        errs: list[str] = []
        amount: Decimal | None = None
        if raw:
            try:
                amount = sum((to_decimal(v) or Decimal(0) for v in raw), Decimal(0))
            except InvalidAmount as exc:
                errs.append(f"amount: {exc}")
        else:  # a heading row switches the section
            low = label.lower()
            if "expense" in low:
                section = "expense"
            elif "income" in low or "revenue" in low:
                section = "income"
            continue
        if not label:
            errs.append("line has no description")
        lines.append(Line(n, label, amount, section, suggest(label, section) if not errs else "ignore", errs))
    if not lines:
        raise ValidationFailure("No amounts found in the statement.")
    return Statement(basis, lines)


def totals(lines: list[Line], mapping: dict[int, str]) -> dict[str, Decimal]:
    out = {t: Decimal(0) for t in TARGETS if t != "ignore"}
    seen = {t: False for t in out}
    for ln in lines:
        target = mapping.get(ln.line, ln.suggested)
        if target == "ignore" or ln.errors or ln.amount is None:
            continue
        amt = abs(ln.amount) if target in ("vacancy_credit_loss", "property_operating_expenses") else ln.amount
        out[target] += amt
        seen[target] = True
    out["effective_gross_income"] = out["gross_potential_rent"] - out["vacancy_credit_loss"] + out["other_property_income"]
    out["_seen"] = seen  # type: ignore[assignment]
    return out


def reconcile(t: dict[str, Any], stated_income: Decimal | None, stated_expenses: Decimal | None) -> dict[str, Any]:
    inc_ok = stated_income is not None and abs(t["effective_gross_income"] - stated_income) <= TOLERANCE
    exp_ok = stated_expenses is not None and abs(t["property_operating_expenses"] - stated_expenses) <= TOLERANCE
    return {"income_ok": inc_ok, "expenses_ok": exp_ok, "reconciled": inc_ok and exp_ok,
            "income_difference": None if stated_income is None else t["effective_gross_income"] - stated_income,
            "expense_difference": None if stated_expenses is None else t["property_operating_expenses"] - stated_expenses}


def commit_statement(conn: sqlite3.Connection, case_id: str, text: str, *, period_end: date, period_months: int = 12,
                     mapping: dict[int, str] | None = None, stated_income: Decimal | None = None,
                     stated_expenses: Decimal | None = None, document_id: str | None = None,
                     source_name: str | None = None, confirm_verified: bool = False,
                     confirmed_by: str | None = None) -> dict[str, Any]:
    from uwhelper.domain.models import FactIn
    from uwhelper.repo.facts import add_user_fact

    get_case(conn, case_id)
    if not 1 <= period_months <= 12:
        raise ValidationFailure("Period months must be 1–12.")
    if document_id and not conn.execute("SELECT 1 FROM case_documents WHERE case_id=? AND document_id=?",
                                        (case_id, document_id)).fetchone():
        raise ValidationFailure("The source document is not attached to this file.")
    st = parse_statement(text)
    mapping = {k: v for k, v in (mapping or {}).items() if v in TARGETS}
    bad = [ln for ln in st.lines if ln.errors and mapping.get(ln.line, "ignore") != "ignore"]
    if bad:
        raise ValidationFailure(f"Line {bad[0].line}: {'; '.join(bad[0].errors)}")
    t = totals(st.lines, mapping)
    rec = reconcile(t, stated_income, stated_expenses)
    verified = bool(confirm_verified and rec["reconciled"])
    if confirm_verified and not rec["reconciled"]:
        raise ValidationFailure("The totals do not reconcile to the statement, so they cannot be saved as verified. "
                                "Fix the mapping or save them as unverified.")
    if verified and len((confirmed_by or "").strip()) < 2:
        raise ValidationFailure("Enter the name of the person confirming the statement.")
    seen = t.pop("_seen")
    imp_id = new_id()
    note = f"From property statement import {imp_id[:8]} ({period_months} months to {period_end.isoformat()})"
    fact_ids = []
    with Database.transaction(conn):
        for key in ("gross_potential_rent", "vacancy_credit_loss", "other_property_income",
                    "property_operating_expenses", "net_operating_income_reported"):
            if not seen[key]:
                continue
            fact_ids.append(add_user_fact(conn, case_id, FactIn(
                field_key=key, value=dstr(t[key]), period_end=period_end.isoformat(), period_months=period_months,
                document_id=document_id, note=note, mark_verified=verified and key != "net_operating_income_reported")))
        lines_json = [{"line": ln.line, "label": ln.label, "amount": dstr(ln.amount), "section": ln.section,
                       "target": mapping.get(ln.line, ln.suggested), "suggested": ln.suggested, "errors": ln.errors}
                      for ln in st.lines]
        conn.execute(
            """INSERT INTO property_statement_imports(id, case_id, period_end, period_months, document_id, source_name,
                   lines_json, totals_json, stated_total_income, stated_total_expenses, reconciled, verified,
                   confirmed_by, fact_ids_json, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (imp_id, case_id, period_end.isoformat(), period_months, document_id, (source_name or "")[:200] or None,
             json.dumps(lines_json), json.dumps({k: dstr(v) for k, v in t.items()}, sort_keys=True),
             dstr(stated_income), dstr(stated_expenses), int(rec["reconciled"]), int(verified),
             (confirmed_by or "").strip()[:120] or None, json.dumps(fact_ids), current_actor(), utcnow()))
        audit.record(conn, "property_statement_imported", "property_statement", imp_id, case_id,
                     {"period_end": period_end.isoformat(), "months": period_months, "reconciled": rec["reconciled"],
                      "verified": verified, "confirmed_by": confirmed_by, "facts": len(fact_ids),
                      "lines": len(st.lines)})
    return {"import_id": imp_id, "fact_ids": fact_ids, "verified": verified, **rec,
            "totals": {k: v for k, v in t.items()}}


def list_imports(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute("SELECT * FROM property_statement_imports WHERE case_id=? ORDER BY created_at DESC", (case_id,)):
        d = dict(r)
        d["lines"] = json.loads(d.pop("lines_json"))
        d["totals"] = json.loads(d.pop("totals_json"))
        out.append(d)
    return out


def get_import(conn: sqlite3.Connection, case_id: str, import_id: str) -> dict[str, Any]:
    for d in list_imports(conn, case_id):
        if d["id"] == import_id:
            return d
    raise NotFound("Statement import not found")
