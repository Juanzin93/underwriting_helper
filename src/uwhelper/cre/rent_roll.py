"""Rent rolls for commercial real estate files.

Flow: import a CSV (preview → draft) → enter the totals printed on the source rent roll → the app reconciles the
unit-level total against them → a named person verifies it. Only a reconciled roll can be verified; verified rolls
are immutable and a newer verified roll supersedes the older one. In verified-only runs only a verified roll is used.

All arithmetic is Decimal. Rents are stored as annual amounts; monthly columns are multiplied by 12 (stated in the
preview). Nothing here calls the language model.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from uwhelper.core.csvutil import parse_date, read_rows
from uwhelper.core.identity import current_actor
from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.domain.money import InvalidAmount, dstr, to_decimal
from uwhelper.repo import audit
from uwhelper.repo.cases import NotFound, ValidationFailure, get_case

TOLERANCE = Decimal("1.00")
MAX_UNITS = 5000
ALIASES = {
    "unit": "unit", "unit_number": "unit", "unit_no": "unit", "suite": "unit", "space": "unit", "apt": "unit",
    "tenant": "tenant", "tenant_name": "tenant", "lessee": "tenant", "name": "tenant",
    "sf": "square_feet", "sqft": "square_feet", "sq_ft": "square_feet", "square_feet": "square_feet", "rsf": "square_feet",
    "nrsf": "square_feet", "area": "square_feet",
    "status": "status", "occupancy": "status", "occupied": "status",
    "monthly_rent": "monthly_rent", "rent": "monthly_rent", "current_rent": "monthly_rent", "contract_rent": "monthly_rent",
    "annual_rent": "annual_rent", "annual_base_rent": "annual_rent", "yearly_rent": "annual_rent",
    "market_rent": "market_monthly_rent", "market_monthly_rent": "market_monthly_rent",
    "market_annual_rent": "market_annual_rent",
    "lease_start": "lease_start", "start": "lease_start", "commencement": "lease_start", "lease_from": "lease_start",
    "lease_end": "lease_end", "end": "lease_end", "expiration": "lease_end", "lease_expiration": "lease_end",
    "expires": "lease_end", "lease_to": "lease_end",
    "month_to_month": "month_to_month", "mtm": "month_to_month",
}
_TRUE = {"1", "y", "yes", "true", "x", "mtm", "month to month", "month-to-month"}
_VACANT = {"vacant", "v", "empty", "available", "no", "0", "n"}


@dataclass
class UnitRow:
    line: int
    unit: str
    tenant: str | None
    square_feet: Decimal | None
    occupied: bool
    annual_rent: Decimal
    market_annual_rent: Decimal | None
    lease_start: date | None
    lease_end: date | None
    month_to_month: bool
    errors: list[str] = field(default_factory=list)


@dataclass
class RentRollPreview:
    columns: list[str]
    ignored_columns: list[str]
    rent_basis: str  # "monthly ×12" | "annual"
    units: list[UnitRow]

    @property
    def valid(self) -> list[UnitRow]:
        return [u for u in self.units if not u.errors]

    @property
    def invalid(self) -> list[UnitRow]:
        return [u for u in self.units if u.errors]

    @property
    def annual_rent(self) -> Decimal:
        return sum((u.annual_rent for u in self.valid if u.occupied), Decimal(0))


def _money(v: str) -> Decimal | None:
    if not v.strip():
        return None
    d = to_decimal(v)
    if d is None or d < 0:
        raise InvalidAmount("must be zero or positive")
    return d


def parse_rent_roll(text: str) -> RentRollPreview:
    header, rows = read_rows(text, MAX_UNITS)
    cols = [ALIASES.get(h, h) for h in header]
    if "unit" not in cols:
        raise ValidationFailure("The rent roll needs a 'unit' (or suite/space) column.")
    if "monthly_rent" not in cols and "annual_rent" not in cols:
        raise ValidationFailure("The rent roll needs a monthly or annual rent column.")
    known = set(ALIASES.values())
    ignored = sorted({h for h, c in zip(header, cols) if c not in known})
    basis = "annual" if "annual_rent" in cols else "monthly ×12"
    units, seen = [], set()
    for line, values in rows:
        d = {c: (values[i] if i < len(values) else "") for i, c in enumerate(cols) if c in known}
        errs: list[str] = []
        unit = d.get("unit", "").strip()[:60]
        if not unit:
            errs.append("unit is blank")
        elif unit.lower() in seen:
            errs.append("unit appears twice")
        seen.add(unit.lower())

        def money(key: str) -> Decimal | None:
            try:
                return _money(d.get(key, ""))
            except InvalidAmount as exc:
                errs.append(f"{key.replace('_', ' ')}: {exc}")
                return None

        def when(key: str) -> date | None:
            try:
                return parse_date(d.get(key, ""))
            except ValueError as exc:
                errs.append(f"{key.replace('_', ' ')}: {exc}")
                return None

        rent = money("annual_rent") if basis == "annual" else money("monthly_rent")
        if rent is not None and basis != "annual":
            rent = rent * 12
        market = money("market_annual_rent")
        if market is None:
            mm = money("market_monthly_rent")
            market = mm * 12 if mm is not None else None
        sqft = money("square_feet")
        status = d.get("status", "").strip().lower()
        tenant = d.get("tenant", "").strip()[:200] or None
        if status:
            occupied = status not in _VACANT
        else:
            occupied = bool(tenant) and bool(rent)
        if tenant and tenant.lower() in ("vacant", "available"):
            occupied, tenant = False, None
        end_raw = d.get("lease_end", "").strip().lower()
        mtm = d.get("month_to_month", "").strip().lower() in _TRUE or end_raw in _TRUE
        start = when("lease_start")
        end = None if end_raw in _TRUE else when("lease_end")
        if occupied and not rent:
            errs.append("occupied unit has no rent")
        if not occupied and rent:
            errs.append("vacant unit shows rent; mark it occupied or clear the rent")
        if start and end and end < start:
            errs.append("lease ends before it starts")
        units.append(UnitRow(line, unit, tenant, sqft, occupied, rent or Decimal(0), market, start, end, mtm, errs))
    if not units:
        raise ValidationFailure("The rent roll has no units.")
    return RentRollPreview([c for c in cols if c in known], ignored, basis, units)


def create_draft(conn: sqlite3.Connection, case_id: str, text: str, *, as_of: date, document_id: str | None = None,
                 source_name: str | None = None, stated_annual_rent: Decimal | None = None,
                 stated_unit_count: int | None = None, skip_invalid: bool = False) -> str:
    get_case(conn, case_id)
    if document_id and not conn.execute("SELECT 1 FROM case_documents WHERE case_id=? AND document_id=?",
                                        (case_id, document_id)).fetchone():
        raise ValidationFailure("The source document is not attached to this file.")
    preview = parse_rent_roll(text)
    if preview.invalid and not skip_invalid:
        raise ValidationFailure(f"{len(preview.invalid)} unit row(s) are invalid. Fix them or choose to skip them.")
    units = preview.valid
    if not units:
        raise ValidationFailure("No valid units to import.")
    rid, now = new_id(), utcnow()
    total = preview.annual_rent
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO rent_rolls(id, case_id, as_of_date, document_id, source_name, stated_annual_rent,
                   stated_unit_count, computed_annual_rent, computed_unit_count, status, created_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,'draft',?,?)""",
            (rid, case_id, as_of.isoformat(), document_id, (source_name or "")[:200] or None, dstr(stated_annual_rent),
             stated_unit_count, dstr(total), len(units), current_actor(), now))
        for u in units:
            conn.execute(
                """INSERT INTO rent_roll_units(rent_roll_id, line_number, unit, tenant, square_feet, occupied, annual_rent,
                       market_annual_rent, lease_start, lease_end, month_to_month) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, u.line, u.unit, u.tenant, dstr(u.square_feet), int(u.occupied), dstr(u.annual_rent),
                 dstr(u.market_annual_rent), u.lease_start.isoformat() if u.lease_start else None,
                 u.lease_end.isoformat() if u.lease_end else None, int(u.month_to_month)))
        _reconcile(conn, rid)
        audit.record(conn, "rent_roll_imported", "rent_roll", rid, case_id,
                     {"as_of": as_of.isoformat(), "units": len(units), "skipped_invalid": len(preview.invalid),
                      "computed_annual_rent": dstr(total), "rent_basis": preview.rent_basis})
    return rid


def _reconcile(conn: sqlite3.Connection, roll_id: str) -> bool:
    r = conn.execute("SELECT * FROM rent_rolls WHERE id=?", (roll_id,)).fetchone()
    ok = r["stated_annual_rent"] is not None and \
        abs(Decimal(r["stated_annual_rent"]) - Decimal(r["computed_annual_rent"])) <= TOLERANCE and \
        (r["stated_unit_count"] is None or r["stated_unit_count"] == r["computed_unit_count"])
    conn.execute("UPDATE rent_rolls SET reconciled=? WHERE id=?", (int(ok), roll_id))
    return ok


def set_stated_totals(conn: sqlite3.Connection, case_id: str, roll_id: str, stated_annual_rent: Decimal,
                      stated_unit_count: int | None) -> bool:
    roll = get_rent_roll(conn, case_id, roll_id)
    if roll["status"] != "draft":
        raise ValidationFailure("Only a draft rent roll can be changed.")
    with Database.transaction(conn):
        conn.execute("UPDATE rent_rolls SET stated_annual_rent=?, stated_unit_count=? WHERE id=?",
                     (dstr(stated_annual_rent), stated_unit_count, roll_id))
        ok = _reconcile(conn, roll_id)
        audit.record(conn, "rent_roll_totals_entered", "rent_roll", roll_id, case_id,
                     {"stated_annual_rent": dstr(stated_annual_rent), "stated_unit_count": stated_unit_count,
                      "reconciled": ok})
    return ok


def verify(conn: sqlite3.Connection, case_id: str, roll_id: str, verified_by: str) -> None:
    roll = get_rent_roll(conn, case_id, roll_id)
    if roll["status"] != "draft":
        raise ValidationFailure(f"This rent roll is already {roll['status']}.")
    if not roll["reconciled"]:
        raise ValidationFailure("Enter the totals printed on the source rent roll first: the unit-level total must "
                                f"match within ${TOLERANCE} (and the unit count, if entered) before it can be verified.")
    if len((verified_by or "").strip()) < 2:
        raise ValidationFailure("Enter the name of the person verifying the rent roll.")
    now = utcnow()
    with Database.transaction(conn):
        prev = conn.execute("SELECT id FROM rent_rolls WHERE case_id=? AND status='verified'", (case_id,)).fetchall()
        for p in prev:
            conn.execute("UPDATE rent_rolls SET status='superseded' WHERE id=?", (p["id"],))
        conn.execute("UPDATE rent_rolls SET status='verified', verified_by=?, verified_at=? WHERE id=?",
                     (verified_by.strip()[:120], now, roll_id))
        audit.record(conn, "rent_roll_verified", "rent_roll", roll_id, case_id,
                     {"verified_by": verified_by.strip()[:120], "superseded": [p["id"] for p in prev]})


def reject(conn: sqlite3.Connection, case_id: str, roll_id: str, reason: str) -> None:
    roll = get_rent_roll(conn, case_id, roll_id)
    if roll["status"] != "draft":
        raise ValidationFailure("Only a draft rent roll can be rejected.")
    if len((reason or "").strip()) < 5:
        raise ValidationFailure("Give a reason for rejecting the rent roll.")
    with Database.transaction(conn):
        conn.execute("UPDATE rent_rolls SET status='rejected', notes=? WHERE id=?", (reason.strip()[:1000], roll_id))
        audit.record(conn, "rent_roll_rejected", "rent_roll", roll_id, case_id, {})


def get_rent_roll(conn: sqlite3.Connection, case_id: str, roll_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT r.*, d.original_filename FROM rent_rolls r LEFT JOIN documents d ON d.id=r.document_id "
                       "WHERE r.id=? AND r.case_id=?", (roll_id, case_id)).fetchone()
    if not row:
        raise NotFound("Rent roll not found")
    out = dict(row)
    out["units"] = [dict(u) for u in conn.execute(
        "SELECT * FROM rent_roll_units WHERE rent_roll_id=? ORDER BY line_number", (roll_id,))]
    return out


def list_rent_rolls(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT r.*, d.original_filename FROM rent_rolls r LEFT JOIN documents d ON d.id=r.document_id "
        "WHERE r.case_id=? ORDER BY r.created_at DESC", (case_id,))]


def roll_for_run(conn: sqlite3.Connection, case_id: str, mode: str) -> dict[str, Any] | None:
    """The rent roll a run uses: the verified one, or (include-unverified runs) the newest draft if newer."""
    statuses = ("verified",) if mode == "verified_only" else ("verified", "draft")
    row = conn.execute(
        f"""SELECT id FROM rent_rolls WHERE case_id=? AND status IN ({','.join('?' * len(statuses))})
            ORDER BY as_of_date DESC, created_at DESC LIMIT 1""", (case_id, *statuses)).fetchone()
    return get_rent_roll(conn, case_id, row["id"]) if row else None


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    import calendar

    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def summarize(roll: dict[str, Any]) -> dict[str, Any]:
    """Deterministic aggregates used as metric inputs (all Decimal; None when not determinable)."""
    as_of = date.fromisoformat(roll["as_of_date"])
    units = roll["units"]
    occ = [u for u in units if u["occupied"]]
    vac = [u for u in units if not u["occupied"]]
    in_place = sum((Decimal(u["annual_rent"]) for u in occ), Decimal(0))
    vac_market = None if any(u["market_annual_rent"] is None for u in vac) else \
        sum((Decimal(u["market_annual_rent"]) for u in vac), Decimal(0))
    h12, h24 = _add_months(as_of, 12), _add_months(as_of, 24)
    exp12 = exp24 = Decimal(0)
    walt_num, walt_rent, unknown_end = Decimal(0), Decimal(0), 0
    for u in occ:
        rent = Decimal(u["annual_rent"])
        end = date.fromisoformat(u["lease_end"]) if u["lease_end"] else None
        if u["month_to_month"] or (end is not None and end <= h12):
            exp12 += rent
        if u["month_to_month"] or (end is not None and end <= h24):
            exp24 += rent
        if u["month_to_month"]:
            walt_rent += rent  # remaining term 0
        elif end is not None:
            remaining_days = max((end - as_of).days, 0)
            walt_num += rent * Decimal(remaining_days) / Decimal(365)
            walt_rent += rent
        else:
            unknown_end += 1
    return {"as_of": roll["as_of_date"], "units_total": Decimal(len(units)), "units_occupied": Decimal(len(occ)),
            "in_place_rent": in_place, "vacant_market_rent": vac_market, "rent_expiring_12m": exp12 if not unknown_end
            else None, "rent_expiring_24m": exp24 if not unknown_end else None,
            "walt_weighted": walt_num if not unknown_end else None, "walt_rent": walt_rent if not unknown_end else None,
            "unknown_lease_ends": unknown_end, "age_days": None}
