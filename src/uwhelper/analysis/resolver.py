"""Turn stored facts into calculation inputs, without guessing.

Rules
-----
* Facts are grouped by (field, period, party, item label).
* Different active values in one group = contradiction -> input state
  ``conflict``; no value is chosen automatically (not even the "better" one).
* ``verified_only`` mode: unverified candidates are excluded (state
  ``missing`` with a note). ``include_unverified`` mode: they are used but
  flagged ``unverified``, so no rule can PASS on them.
* Period handling: duration facts come from the base period (annualized
  ×12/months only if the base period is shorter than 12 months, flagged);
  instant (balance sheet) facts come from the base period end date.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from uwhelper.domain.money import to_decimal
from uwhelper.metrics.finance import LoanMathError, annualize_payment
from uwhelper.metrics.types import (
    AnalysisInputs,
    CollateralInput,
    DebtInput,
    GuarantorInput,
    InputValue,
    LoanTermsInput,
    RentRollInput,
)
from uwhelper.modules import get_module
from uwhelper.repo import cases as cases_repo
from uwhelper.repo import facts as facts_repo

CONFLICT_ABS_TOLERANCE = Decimal("1.00")  # values within $1 are treated as equal (rounding)


@dataclass
class FactGroup:
    field_key: str
    period_id: str | None
    period_label: str | None
    entity_ref: str | None
    item_label: str
    facts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def verified(self) -> list[dict[str, Any]]:
        return [f for f in self.facts if f["status"] == "verified"]

    def distinct_values(self) -> list[Decimal | str]:
        vals: list[Decimal | str] = []
        for f in self.facts:
            v = _fact_value(f)
            if v is None:
                continue
            if isinstance(v, Decimal):
                if not any(isinstance(x, Decimal) and abs(x - v) <= CONFLICT_ABS_TOLERANCE for x in vals):
                    vals.append(v)
            elif v.strip().lower() not in [str(x).strip().lower() for x in vals]:
                vals.append(v)
        return vals

    @property
    def is_contradiction(self) -> bool:
        return len(self.distinct_values()) > 1


def _fact_value(f: dict[str, Any]) -> Decimal | str | None:
    if f.get("value_numeric") is not None:
        return to_decimal(f["value_numeric"])
    return f.get("value_text")


def _source(f: dict[str, Any]) -> dict[str, Any]:
    return {"fact_id": f["id"], "document_id": f.get("document_id"), "filename": f.get("original_filename"),
            "page_number": f.get("page_number"), "source_type": f.get("source_type"), "status": f.get("status")}


def group_facts(facts: list[dict[str, Any]]) -> dict[tuple, FactGroup]:
    groups: dict[tuple, FactGroup] = {}
    for f in facts:
        key = (f["field_key"], f["period_id"], f["entity_ref"], f["item_label"] or "")
        g = groups.get(key)
        if g is None:
            g = groups[key] = FactGroup(f["field_key"], f["period_id"], f.get("period_label"), f["entity_ref"],
                                        f["item_label"] or "")
        g.facts.append(f)
    return groups


def find_contradictions(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    facts = facts_repo.list_facts(conn, case_id, statuses=facts_repo.ACTIVE_STATUSES)
    catalog = get_module("commercial").field_catalog()
    out = []
    for g in group_facts(facts).values():
        if g.is_contradiction:
            spec = catalog.get(g.field_key)
            out.append({
                "field_key": g.field_key,
                "field_label": spec.label if spec else g.field_key,
                "period_id": g.period_id,
                "period_label": g.period_label,
                "entity_ref": g.entity_ref,
                "party_name": g.facts[0].get("party_name"),
                "item_label": g.item_label,
                "facts": g.facts,
                "values": [str(v) for v in g.distinct_values()],
            })
    return out


def _resolve_group(g: FactGroup | None, name: str, label: str, mode: str) -> InputValue:
    if g is None or not g.facts:
        return InputValue.missing(name, label)
    sources = [_source(f) for f in g.facts]
    fact_ids = [f["id"] for f in g.facts]
    if g.is_contradiction:
        return InputValue(name, label, None, "conflict", False, {"conflict"}, fact_ids, sources,
                          "Sources disagree: " + " vs ".join(str(v) for v in g.distinct_values()))
    value = next((_fact_value(f) for f in g.facts if _fact_value(f) is not None), None)
    if not isinstance(value, Decimal):
        return InputValue.missing(name, label, "No numeric value.")
    flags: set[str] = set()
    if any(f["source_type"] in ("user_entered", "user_corrected") for f in g.facts):
        flags.add("user_entered")
    if any(f["source_type"] == "extracted" for f in g.facts):
        flags.add("extracted")
    if g.verified:
        return InputValue(name, label, value, "ok", True, flags, [f["id"] for f in g.verified],
                          [_source(f) for f in g.verified])
    if mode == "verified_only":
        return InputValue(name, label, None, "missing", False, flags | {"unverified_excluded"}, fact_ids, sources,
                          "Only unverified candidate value(s) exist; verify to use.")
    return InputValue(name, label, value, "ok", False, flags | {"unverified"}, fact_ids, sources,
                      "Unverified value used (preview run).")


def choose_base_period(conn: sqlite3.Connection, case: dict[str, Any], facts: list[dict[str, Any]]) -> dict | None:
    periods = facts_repo.list_periods(conn, case["id"])
    if case.get("base_period_id"):
        for p in periods:
            if p["id"] == case["base_period_id"]:
                return p
    with_revenue = {f["period_id"] for f in facts if f["field_key"] in ("revenue", "gross_potential_rent")}
    full_years = [p for p in periods if p["period_months"] == 12 and p["id"] in with_revenue]
    if full_years:
        return max(full_years, key=lambda p: p["period_end"])
    return None


def _scale(iv: InputValue, factor: Decimal) -> InputValue:
    if iv.ok and factor != 1:
        iv.value = iv.value * factor  # type: ignore[operator]
        iv.flags.add("annualized")
        iv.note = (iv.note + " " if iv.note else "") + f"Annualized ×{factor:.4f}."
    return iv


def build_inputs(conn: sqlite3.Connection, case_id: str, mode: str = "verified_only",
                 as_of: date | None = None) -> tuple[AnalysisInputs, list[dict[str, Any]]]:
    """Return (inputs, data_quality_findings)."""
    case = cases_repo.get_case(conn, case_id)
    module = get_module(case["module_key"])
    catalog = module.field_catalog()
    findings: list[dict[str, Any]] = []
    facts = facts_repo.list_facts(conn, case_id, statuses=facts_repo.ACTIVE_STATUSES)
    groups = group_facts(facts)
    base = choose_base_period(conn, case, facts)
    inputs = AnalysisInputs(loan_type=case["loan_type"])

    t = case.get("terms") or {}
    inputs.terms = LoanTermsInput(
        requested_amount=to_decimal(t.get("requested_amount")),
        interest_rate=to_decimal(t.get("interest_rate")),
        term_months=t.get("term_months"),
        amortization_months=t.get("amortization_months"),
        repayment_type=t.get("repayment_type") or "amortizing",
        rate_type=t.get("rate_type") or "fixed",
        project_total_cost=to_decimal(t.get("project_total_cost")),
        is_secured=bool(t.get("is_secured", 1)),
        loan_purpose=t.get("loan_purpose"),
    )

    factor = Decimal(1)
    instant_period_id = None
    if base is not None:
        inputs.period_label, inputs.period_end, inputs.period_months = base["label"], base["period_end"], base["period_months"]
        if base["period_months"] not in (0, 12):
            factor = Decimal(12) / Decimal(base["period_months"])
            findings.append({"finding_type": "assumption", "severity": "medium", "field_key": None,
                             "period_label": base["label"],
                             "message": f"Base period is {base['period_months']} months; income-statement values are "
                                        f"annualized ×12/{base['period_months']}. Seasonality is ignored."})
        row = conn.execute("SELECT * FROM financial_periods WHERE case_id=? AND period_end=? AND period_months=0",
                           (case_id, base["period_end"])).fetchone()
        if row:
            instant_period_id, inputs.balance_sheet_label = row["id"], row["label"]
        else:
            row = conn.execute(
                "SELECT * FROM financial_periods WHERE case_id=? AND period_months=0 AND period_end<=? "
                "ORDER BY period_end DESC LIMIT 1", (case_id, base["period_end"])).fetchone()
            if row:
                instant_period_id, inputs.balance_sheet_label = row["id"], row["label"]
                findings.append({"finding_type": "data_quality", "severity": "medium", "field_key": None,
                                 "period_label": row["label"],
                                 "message": f"No balance sheet dated {base['period_end']}; using {row['label']} "
                                            "(period mismatch flagged)."})
    else:
        findings.append({"finding_type": "missing", "severity": "high", "field_key": None, "period_label": None,
                         "message": "No 12-month base period with revenue was found or selected. "
                                    "Income-statement metrics cannot be calculated."})

    for key, spec in catalog.items():
        if spec.party_scoped or spec.multi_item or spec.unit != "USD" or spec.informational:
            continue
        if spec.timing == "duration":
            pid = base["id"] if base else None
            iv = _resolve_group(groups.get((key, pid, None, "")) if pid else None, key, spec.label, mode)
            inputs.values[key] = _scale(iv, factor)
        elif spec.timing == "instant":
            iv = _resolve_group(groups.get((key, instant_period_id, None, "")) if instant_period_id else None,
                                key, spec.label, mode)
            if iv.ok and inputs.balance_sheet_label and base and not inputs.balance_sheet_label.endswith(base["period_end"]):
                iv.flags.add("period_mismatch")
            inputs.values[key] = iv
        elif spec.timing == "static":
            # Only the authoritative structured data drives metrics; static facts are informational.
            continue
        else:  # pragma: no cover
            continue

    # Reported NOI is a fallback input for CRE.
    if base:
        inputs.values["net_operating_income_reported"] = _scale(
            _resolve_group(groups.get(("net_operating_income_reported", base["id"], None, "")),
                           "net_operating_income_reported", "NOI (as reported)", mode), factor)

    # EBITDA adjustments (multi-item).
    if base:
        for (fk, pid, ent, item), g in groups.items():
            if fk != "ebitda_adjustment" or pid != base["id"]:
                continue
            iv = _resolve_group(g, f"ebitda_adjustment:{item}", f"Adjustment: {item}", mode)
            if iv.state == "missing" and "unverified_excluded" in iv.flags:
                inputs.excluded_adjustments.append(iv)
                findings.append({"finding_type": "unverified", "severity": "medium", "field_key": "ebitda_adjustment",
                                 "period_label": base["label"],
                                 "message": f"Unverified EBITDA adjustment '{item}' excluded from verified-only run."})
            else:
                inputs.adjustments.append(_scale(iv, factor))

    # Existing debts.
    inputs.debt_schedule_confirmed = bool(case["debt_schedule_confirmed"])
    for d in cases_repo.list_debts(conn, case_id):
        if d["verification_status"] == "rejected":
            continue
        verified = d["verification_status"] == "verified"
        src = [{"fact_id": None, "document_id": d["document_id"], "filename": None, "page_number": d["page_number"],
                "source_type": "debt_schedule", "status": d["verification_status"]}]

        def _debt_iv(name: str, label: str, value: Decimal | None) -> InputValue:
            if value is None:
                return InputValue.missing(name, label, "Not entered on the debt schedule.")
            if not verified and mode == "verified_only" and not d["to_be_refinanced"]:
                return InputValue(name, label, None, "missing", False, {"unverified_excluded"}, [], src,
                                  "Debt not verified; verify it to use.")
            flags = set() if verified else {"unverified"}
            return InputValue(name, label, value, "ok", verified, flags, [], src)

        pay = to_decimal(d["payment_amount"])
        annual = None
        if pay is not None:
            try:
                annual = annualize_payment(pay, d["payment_frequency"])
            except LoanMathError:
                annual = None
        inputs.debts.append(DebtInput(
            id=d["id"], creditor=d["creditor"],
            balance=_debt_iv(f"debt_balance:{d['id']}", f"{d['creditor']} balance", to_decimal(d["current_balance"])),
            annual_payment=_debt_iv(f"debt_payment:{d['id']}", f"{d['creditor']} annual payments "
                                    f"({d['payment_frequency']} × {annualize_factor(d['payment_frequency'])})", annual),
            to_be_refinanced=bool(d["to_be_refinanced"]),
            interest_rate=to_decimal(d["interest_rate"]), maturity_date=d["maturity_date"],
        ))

    # Collateral.
    today = as_of or date.today()
    for c in cases_repo.list_collateral(conn, case_id):
        v = c["current_valuation"]
        if v is None:
            iv = InputValue.missing(f"collateral:{c['id']}", c["description"], "No valuation recorded.")
        else:
            verified = v["verification_status"] == "verified"
            src = [{"fact_id": None, "document_id": v["document_id"], "filename": None, "page_number": v["page_number"],
                    "source_type": f"valuation:{v['valuation_method']}", "status": v["verification_status"]}]
            flags: set[str] = set()
            if v["valuation_method"] in ("user_estimate", "book_value"):
                flags.add("estimate")
            if not verified and mode == "verified_only":
                iv = InputValue(f"collateral:{c['id']}", c["description"], None, "missing", False,
                                flags | {"unverified_excluded"}, [], src, "Valuation not verified; verify it to use.")
            else:
                if not verified:
                    flags.add("unverified")
                iv = InputValue(f"collateral:{c['id']}", f"{c['description']} value", to_decimal(v["value"]), "ok",
                                verified, flags, [], src, f"{v['valuation_method']} dated {v['valuation_date'] or 'unknown'}")
            if v["valuation_date"]:
                try:
                    age = (today - date.fromisoformat(v["valuation_date"])).days
                    if age > 365:
                        findings.append({"finding_type": "stale", "severity": "medium", "field_key": "collateral",
                                         "period_label": v["valuation_date"],
                                         "message": f"Valuation of '{c['description']}' is {age} days old."})
                except ValueError:
                    pass
        inputs.collateral.append(CollateralInput(
            id=c["id"], description=c["description"], collateral_type=c["collateral_type"], value=iv,
            valuation_date=v["valuation_date"] if v else None, valuation_method=v["valuation_method"] if v else None,
            prior_lien_balance=to_decimal(c["prior_lien_balance"]) or Decimal(0), lien_position=c["lien_position"],
        ))

    # Guarantors (personal cash flow from the base period).
    for p in cases_repo.list_parties(conn, case_id):
        if not p["is_guarantor"]:
            continue
        pid = base["id"] if base else None

        def _g(key: str) -> InputValue:
            spec = catalog[key]
            g = groups.get((key, pid, p["id"], "")) if pid else None
            return _scale(_resolve_group(g, f"{key}:{p['id']}", f"{p['name']}: {spec.label}", mode), factor)

        inputs.guarantors.append(GuarantorInput(p["id"], p["name"], _g("guarantor_personal_income"),
                                                _g("guarantor_living_expenses"), _g("guarantor_personal_debt_service")))
    if case["loan_type"] == "commercial_real_estate":
        inputs.rent_roll = _rent_roll_input(conn, case_id, mode, as_of or date.today(), findings)
    return inputs, findings


RENT_ROLL_STALE_DAYS = 180
RENT_ROLL_LABELS = {
    "units_total": "Rent roll: total units", "units_occupied": "Rent roll: occupied units",
    "in_place_rent": "Rent roll: in-place annual rent", "vacant_market_rent": "Rent roll: market rent of vacant units",
    "rent_expiring_12m": "Rent roll: annual rent expiring within 12 months",
    "rent_expiring_24m": "Rent roll: annual rent expiring within 24 months",
    "walt_weighted": "Rent roll: Σ rent × remaining years", "walt_rent": "Rent roll: rent with known lease end",
}


def _rent_roll_input(conn: sqlite3.Connection, case_id: str, mode: str, as_of: date,
                     findings: list[dict[str, Any]]) -> RentRollInput | None:
    from uwhelper.cre.rent_roll import roll_for_run, summarize

    roll = roll_for_run(conn, case_id, mode)
    if roll is None:
        return None
    summ = summarize(roll)
    age = (as_of - date.fromisoformat(roll["as_of_date"])).days
    flags = {"stale"} if age > RENT_ROLL_STALE_DAYS else set()
    if flags:
        findings.append({"finding_type": "stale", "severity": "medium", "field_key": "rent_roll",
                         "period_label": roll["as_of_date"],
                         "message": f"The rent roll is {age} days old (as of {roll['as_of_date']})."})
    if summ["unknown_lease_ends"]:
        findings.append({"finding_type": "data_quality", "severity": "low", "field_key": "rent_roll",
                         "period_label": roll["as_of_date"],
                         "message": f"{summ['unknown_lease_ends']} occupied unit(s) have no lease end date; rollover and "
                                    "WALT are not calculated."})
    src = [{"fact_id": None, "document_id": roll["document_id"], "filename": roll.get("original_filename"),
            "page_number": None, "source_type": "rent_roll", "status": roll["status"], "rent_roll_id": roll["id"]}]
    values = {}
    for key, label in RENT_ROLL_LABELS.items():
        val = summ[key]
        if val is None:
            values[key] = InputValue.missing(key, label, "Not determinable from the rent roll (missing market rents or "
                                                         "lease end dates).")
        else:
            values[key] = InputValue(name=key, label=label, value=val, state="ok",
                                     verified=roll["status"] == "verified", flags=set(flags), sources=list(src),
                                     note=f"Rent roll as of {roll['as_of_date']} ({roll['status']}).")
    return RentRollInput(roll["id"], roll["as_of_date"], roll["status"], values)


def annualize_factor(freq: str) -> int:
    return {"monthly": 12, "quarterly": 4, "semiannual": 2, "annual": 1}.get(freq, 0)


def missing_checklist(conn: sqlite3.Connection, case_id: str, mode: str = "verified_only") -> list[dict[str, Any]]:
    """Live checklist of required information for the case's loan type."""
    case = cases_repo.get_case(conn, case_id)
    module = get_module(case["module_key"])
    inputs, _ = build_inputs(conn, case_id, mode)
    catalog = module.field_catalog()
    terms = case.get("terms") or {}
    items = []
    for req in module.required_items(case["loan_type"]):
        status, details = "complete", []
        if req.kind == "base_period":
            if not inputs.period_label:
                status, details = "missing", ["No 12-month period with revenue. Enter facts for a fiscal year or select a base period."]
            else:
                details = [f"Using {inputs.period_label}."]
        elif req.kind == "terms":
            missing = []
            for f in req.fields:
                if f == "amortization_months" and terms.get("repayment_type") == "interest_only":
                    continue
                if not terms.get(f):
                    missing.append(f.replace("_", " "))
            if missing:
                status, details = "missing", [f"Not entered: {', '.join(missing)}."]
        elif req.kind == "fact":
            for f in req.fields:
                iv = inputs.values.get(f)
                label = catalog[f].label if f in catalog else f
                if iv is None or iv.state == "missing":
                    note = " (unverified candidate exists — verify it)" if iv and "unverified_excluded" in iv.flags else ""
                    details.append(f"{label}: missing{note}")
                elif iv.state == "conflict":
                    details.append(f"{label}: CONTRADICTORY sources")
                elif not iv.verified:
                    details.append(f"{label}: unverified")
            if details:
                status = "missing" if any("missing" in d or "CONTRADICTORY" in d for d in details) else "unverified"
        elif req.kind == "debt_schedule":
            if not inputs.debt_schedule_confirmed:
                status, details = "missing", ["Confirm the existing debt schedule is complete (even if there is no debt)."]
            else:
                pending = [d.creditor for d in inputs.debts if not d.to_be_refinanced and not d.annual_payment.ok]
                if pending:
                    status, details = "unverified", [f"Unverified or incomplete debts: {', '.join(pending)}."]
        elif req.kind == "collateral":
            if not inputs.terms.is_secured:
                status, details = "not_applicable", ["Loan is unsecured."]
            elif not inputs.collateral:
                status, details = "missing", ["No collateral recorded."]
            else:
                bad = [c.description for c in inputs.collateral if not c.value.ok or not c.valuation_date]
                if bad:
                    status, details = "missing", [f"Valuation missing, undated or unverified: {', '.join(bad)}."]
        elif req.kind == "guarantor":
            if not inputs.guarantors:
                status, details = "not_applicable", ["No guarantors recorded."]
            else:
                for g in inputs.guarantors:
                    for iv in (g.personal_income, g.living_expenses, g.personal_debt_service):
                        if not iv.ok:
                            details.append(f"{iv.label}: {'CONTRADICTORY' if iv.state == 'conflict' else 'missing'}")
                if details:
                    status = "missing"
        items.append({"key": req.key, "label": req.label, "why": req.why, "status": status, "details": details})
    return items
