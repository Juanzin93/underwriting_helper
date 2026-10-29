"""Deterministic commercial lending metrics.

Every metric:
* has an explicit formula string and the exact inputs used (with provenance);
* is NOT calculated when a prerequisite is missing, contradictory or not
  meaningful (no zero-filling, no guessing);
* carries quality flags (unverified, annualized, stale, ...) from its inputs.

Definitions are documented in docs/METRICS.md and mirrored in
``METRIC_SPECS`` below.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Callable, Iterable

from uwhelper.domain.money import q_money, q_ratio
from uwhelper.metrics.finance import LoanMathError, annual_debt_service
from uwhelper.metrics.types import AnalysisInputs, InputValue, MetricResult
from uwhelper.modules.base import MetricSpec

ZERO = Decimal(0)
ONE = Decimal(1)


class NotMeaningful(Exception):
    """Inputs exist but the ratio has no meaningful interpretation."""


M = MetricSpec
METRIC_SPECS: dict[str, MetricSpec] = {m.key: m for m in [
    M("revenue", "Revenue (annual basis)", "USD", "Net revenue / net sales for the base period, annualized ×12/months if the period is shorter than 12 months. Never bank deposits."),
    M("gross_profit", "Gross profit", "USD", "Revenue − Cost of goods sold."),
    M("gross_margin", "Gross margin", "percent", "Gross profit ÷ Revenue. Not meaningful if revenue ≤ 0."),
    M("operating_income", "Operating income (EBIT, operating)", "USD", "Gross profit − Operating expenses (operating expenses exclude COGS, interest and income taxes)."),
    M("operating_margin", "Operating margin", "percent", "Operating income ÷ Revenue."),
    M("net_margin", "Net margin", "percent", "Net income ÷ Revenue."),
    M("ebitda", "EBITDA (unadjusted)", "USD", "Net income + Interest expense + Income tax expense + Depreciation + Amortization. Every component must be present (enter 0 explicitly when none)."),
    M("ebitda_adjustments", "EBITDA adjustments (add-backs)", "USD", "Sum of individually documented add-backs/deductions. Unverified adjustments are excluded in verified-only runs."),
    M("adjusted_ebitda", "Adjusted EBITDA", "USD", "EBITDA + EBITDA adjustments."),
    M("ebitda_margin", "Adjusted EBITDA margin", "percent", "Adjusted EBITDA ÷ Revenue."),
    M("cfads", "Cash flow available for debt service", "USD", "Adjusted EBITDA − Unfinanced capital expenditures − Cash taxes paid − Owner distributions/dividends."),
    M("existing_debt_service", "Existing annual debt service (continuing)", "USD", "Σ annualized scheduled payments of existing debts that will NOT be refinanced by this loan. Requires the debt schedule to be confirmed complete."),
    M("proposed_debt_service", "Proposed annual debt service", "USD", "Amortizing: 12 × level monthly payment P·r/(1−(1+r)^−n), r = rate/12, n = amortization months. Interest-only: P × annual rate."),
    M("total_debt_service", "Total annual debt service (pro forma)", "USD", "Existing continuing debt service + Proposed debt service."),
    M("dscr", "Business DSCR (pro forma)", "ratio", "CFADS ÷ Total annual debt service. Not meaningful if debt service ≤ 0."),
    M("global_cash_flow", "Global cash flow available", "USD", "CFADS + Σ guarantor personal income − Σ guarantor living expenses. Guarantor income must exclude amounts paid by this business (no double counting)."),
    M("global_debt_service", "Global debt service", "USD", "Total business debt service + Σ guarantor personal debt service."),
    M("global_dscr", "Global DSCR", "ratio", "Global cash flow ÷ Global debt service."),
    M("working_capital", "Working capital", "USD", "Current assets − Current liabilities (balance sheet as of base period end)."),
    M("current_ratio", "Current ratio", "ratio", "Current assets ÷ Current liabilities. Not meaningful if current liabilities ≤ 0."),
    M("quick_ratio", "Quick ratio", "ratio", "(Cash + Accounts receivable) ÷ Current liabilities."),
    M("total_equity", "Total equity (net worth)", "USD", "Total assets − Total liabilities (falls back to reported total equity only when assets/liabilities are unavailable)."),
    M("debt_to_equity", "Debt-to-equity (total liabilities ÷ equity)", "ratio", "Total liabilities ÷ Total equity. Not meaningful if equity ≤ 0 (negative net worth is reported separately)."),
    M("funded_debt_pro_forma", "Pro forma funded debt", "USD", "Σ balances of existing debts not being refinanced + Requested loan amount."),
    M("debt_to_ebitda", "Funded debt ÷ Adjusted EBITDA (leverage)", "ratio", "Pro forma funded debt ÷ Adjusted EBITDA. Not meaningful if Adjusted EBITDA ≤ 0."),
    M("collateral_value", "Total collateral value (gross)", "USD", "Σ latest non-rejected valuation of each collateral item."),
    M("ltv", "Loan-to-value (LTV)", "percent", "Requested loan amount ÷ Total collateral value."),
    M("cltv", "Combined loan-to-value (CLTV)", "percent", "(Requested loan amount + Σ prior lien balances) ÷ Total collateral value."),
    M("ltc", "Loan-to-cost (LTC)", "percent", "Requested loan amount ÷ Total project cost (acquisition/construction/equipment cost)."),
    M("discounted_collateral_value", "Discounted (lendable) collateral value", "USD", "Σ max(0, value × policy advance rate for the collateral type − prior liens)."),
    M("collateral_coverage", "Collateral coverage", "ratio", "Discounted collateral value ÷ Requested loan amount."),
    M("breakeven_revenue", "Debt-service break-even revenue", "USD", "Revenue − (CFADS − Total debt service) ÷ Contribution margin ratio. Assumes COGS is fully variable and all other costs are fixed."),
    M("revenue_cushion", "Revenue cushion before DSCR < 1.00x", "percent", "(CFADS − Total debt service) ÷ Contribution margin ratio ÷ Revenue."),
    M("effective_gross_income", "Effective gross income (property)", "USD", "Gross potential rent − Vacancy & credit loss + Other property income."),
    M("noi", "Net operating income (property)", "USD", "Effective gross income − Property operating expenses (falls back to reported NOI, flagged, when components are missing)."),
    M("dscr_property", "Property DSCR", "ratio", "NOI ÷ Proposed annual debt service."),
    M("debt_yield", "Debt yield", "percent", "NOI ÷ Requested loan amount."),
    # --- CRE extensions (metrics/cre.py) ---
    M("dscr_property_stressed", "Property DSCR at stressed rate", "ratio", "NOI ÷ Proposed annual debt service recalculated at (interest rate + policy stress, in basis points)."),
    M("breakeven_occupancy", "Break-even occupancy", "percent", "(Property operating expenses + Proposed annual debt service) ÷ (Gross potential rent + Other property income)."),
    M("income_approach_value", "Income-approach value (policy cap rate)", "USD", "NOI ÷ policy capitalization rate. An illustrative cross-check, not an appraisal."),
    M("ltv_income_value", "LTV on income-approach value", "percent", "Requested loan amount ÷ Income-approach value."),
    M("occupancy_physical", "Physical occupancy (rent roll)", "percent", "Occupied units ÷ Total units on the rent roll."),
    M("occupancy_economic", "Economic occupancy (rent roll)", "percent", "In-place annual rent ÷ (In-place annual rent + Market rent of vacant units)."),
    M("rollover_12m", "Lease rollover within 12 months", "percent", "Annual rent of leases ending within 12 months of the rent-roll date (month-to-month included) ÷ In-place annual rent."),
    M("rollover_24m", "Lease rollover within 24 months", "percent", "Annual rent of leases ending within 24 months of the rent-roll date (month-to-month included) ÷ In-place annual rent."),
    M("walt_years", "Weighted average lease term remaining", "years", "Σ(annual rent × remaining years) ÷ Σ annual rent, occupied units with a lease end (month-to-month = 0 years)."),
]}

_PROPAGATE_EXCLUDE = {"derived", "proposed_terms", "user_entered", "extracted"}


def _ratio(n: Decimal, d: Decimal, what: str) -> Decimal:
    if d <= 0:
        raise NotMeaningful(f"{what} is zero or negative ({d}); the ratio is not meaningful.")
    return n / d


class _Builder:
    def __init__(self) -> None:
        self.results: dict[str, MetricResult] = {}

    def na(self, key: str, reason: str) -> MetricResult:
        spec = METRIC_SPECS[key]
        res = MetricResult(key, spec.label, spec.unit, "not_applicable", None, spec.definition, [], reason)
        self.results[key] = res
        return res

    def add(
        self,
        key: str,
        inputs: list[InputValue],
        compute: Callable[..., Decimal],
        *,
        formula: str | None = None,
        note: str = "",
        extra_flags: Iterable[str] = (),
    ) -> MetricResult:
        spec = METRIC_SPECS[key]
        formula = formula or spec.definition
        flags: set[str] = set(extra_flags)
        for i in inputs:
            flags |= (i.flags - _PROPAGATE_EXCLUDE)
            if i.state == "ok" and not i.verified:
                flags.add("unverified")
        blocking = [i for i in inputs if not i.ok]
        if blocking:
            reasons = []
            for i in blocking:
                if i.state == "conflict":
                    reasons.append(f"{i.label}: sources disagree (unresolved contradiction)")
                elif i.state == "not_meaningful":
                    reasons.append(f"{i.label}: not meaningful")
                elif "derived" in i.flags:
                    reasons.append(f"{i.label}: not calculated")
                else:
                    reasons.append(f"{i.label}: missing" + (f" ({i.note})" if i.note else ""))
            res = MetricResult(key, spec.label, spec.unit, "not_computable", None, formula, inputs,
                               "Not calculated — prerequisites unavailable: " + "; ".join(reasons), flags)
        else:
            try:
                raw = compute(*[i.value for i in inputs])
                value = q_money(raw) if spec.unit == "USD" else q_ratio(raw)
                expl = note or "Calculated from the inputs listed."
                if "unverified" in flags:
                    expl += " WARNING: uses unverified values."
                res = MetricResult(key, spec.label, spec.unit, "computed", value, formula, inputs, expl, flags)
            except NotMeaningful as nm:
                res = MetricResult(key, spec.label, spec.unit, "not_meaningful", None, formula, inputs, str(nm), flags)
            except (LoanMathError, ArithmeticError) as exc:
                res = MetricResult(key, spec.label, spec.unit, "not_computable", None, formula, inputs,
                                   f"Not calculated — invalid inputs: {exc}", flags)
        self.results[key] = res
        return res


def _terms_input(name: str, label: str, value: Decimal | int | None, note: str = "Proposed loan terms (user-entered structure).") -> InputValue:
    if value is None:
        return InputValue.missing(name, label, "Not entered in the proposed loan terms.")
    return InputValue.assumed(name, label, Decimal(value), note)


def compute_commercial_metrics(inputs: AnalysisInputs, params: dict | None = None) -> dict[str, MetricResult]:
    params = params or {}
    b = _Builder()
    v = inputs.get
    lt = inputs.loan_type
    t = inputs.terms

    # ---------------- Income statement ----------------
    revenue = b.add("revenue", [v("revenue", "Revenue")], lambda r: r)
    rev_in = revenue.as_input("Revenue")
    gp = b.add("gross_profit", [rev_in, v("cost_of_goods_sold", "Cost of goods sold")], lambda r, c: r - c)
    gp_in = gp.as_input("Gross profit")
    b.add("gross_margin", [gp_in, rev_in], lambda g, r: _ratio(g, r, "Revenue"))
    oi = b.add("operating_income", [gp_in, v("operating_expenses", "Operating expenses")], lambda g, o: g - o)
    b.add("operating_margin", [oi.as_input("Operating income"), rev_in], lambda o, r: _ratio(o, r, "Revenue"))
    b.add("net_margin", [v("net_income", "Net income"), rev_in], lambda n, r: _ratio(n, r, "Revenue"))

    ebitda = b.add(
        "ebitda",
        [v("net_income", "Net income"), v("interest_expense", "Interest expense"),
         v("income_tax_expense", "Income tax expense"), v("depreciation", "Depreciation"),
         v("amortization", "Amortization")],
        lambda n, i, tx, d, a: n + i + tx + d + a,
    )
    adj_inputs = list(inputs.adjustments)
    adj_note = f"{len(adj_inputs)} adjustment(s) included."
    if inputs.excluded_adjustments:
        adj_note += f" {len(inputs.excluded_adjustments)} unverified adjustment(s) excluded."
    adjustments = b.add("ebitda_adjustments", adj_inputs, lambda *xs: sum(xs, ZERO),
                        note=adj_note if adj_inputs or inputs.excluded_adjustments else "No EBITDA adjustments recorded (0).")
    adj_ebitda = b.add("adjusted_ebitda", [ebitda.as_input("EBITDA"), adjustments.as_input("EBITDA adjustments")],
                       lambda e, a: e + a)
    adj_in = adj_ebitda.as_input("Adjusted EBITDA")
    b.add("ebitda_margin", [adj_in, rev_in], lambda e, r: _ratio(e, r, "Revenue"))

    cfads = b.add(
        "cfads",
        [adj_in, v("unfinanced_capex", "Unfinanced capital expenditures"),
         v("cash_taxes_paid", "Cash taxes paid"), v("distributions", "Owner distributions / dividends")],
        lambda e, cx, tx, dist: e - cx - tx - dist,
    )
    cfads_in = cfads.as_input("CFADS")

    # ---------------- Debt service ----------------
    if not inputs.debt_schedule_confirmed:
        b.add("existing_debt_service",
              [InputValue.missing("debt_schedule", "Existing debt schedule",
                                  "Debt schedule not confirmed complete by the user.")],
              lambda x: x)
    else:
        continuing = [d for d in inputs.debts if not d.to_be_refinanced]
        pays = [d.annual_payment for d in continuing]
        note = (f"{len(continuing)} continuing debt(s); {len(inputs.debts) - len(continuing)} to be refinanced (excluded)."
                if inputs.debts else "Debt schedule confirmed: no existing debt.")
        b.add("existing_debt_service", pays, lambda *xs: sum(xs, ZERO), note=note)
    existing_in = b.results["existing_debt_service"].as_input("Existing debt service")

    amt_in = _terms_input("requested_amount", "Requested loan amount", t.requested_amount)
    rate_in = _terms_input("interest_rate", "Interest rate (annual)", t.interest_rate)
    if t.repayment_type == "interest_only":
        proposed = b.add("proposed_debt_service", [amt_in, rate_in],
                         lambda p, r: annual_debt_service(p, r, None, "interest_only"),
                         formula="Interest-only: Requested amount × annual interest rate.")
    else:
        amort_in = _terms_input("amortization_months", "Amortization (months)", t.amortization_months)
        proposed = b.add("proposed_debt_service", [amt_in, rate_in, amort_in],
                         lambda p, r, n: annual_debt_service(p, r, int(n), "amortizing"))
    if t.rate_type == "variable" and proposed.computed:
        proposed.flags.add("variable_rate")
        proposed.explanation += " Variable rate: payment shown at the current rate; see sensitivity analysis."
    proposed_in = proposed.as_input("Proposed debt service")
    tds = b.add("total_debt_service", [existing_in, proposed_in], lambda e, p: e + p)
    tds_in = tds.as_input("Total debt service")
    b.add("dscr", [cfads_in, tds_in], lambda c, d: _ratio(c, d, "Total debt service"))

    # ---------------- Global cash flow ----------------
    if not inputs.guarantors:
        for k in ("global_cash_flow", "global_debt_service", "global_dscr"):
            b.na(k, "No guarantors with personal financial information on this case.")
    else:
        incomes = [g.personal_income for g in inputs.guarantors]
        living = [g.living_expenses for g in inputs.guarantors]
        pds = [g.personal_debt_service for g in inputs.guarantors]
        n = len(inputs.guarantors)
        gcf = b.add("global_cash_flow", [cfads_in, *incomes, *living],
                    lambda c, *xs: c + sum(xs[:n], ZERO) - sum(xs[n:], ZERO))
        gds = b.add("global_debt_service", [tds_in, *pds], lambda d, *xs: d + sum(xs, ZERO))
        b.add("global_dscr", [gcf.as_input("Global cash flow"), gds.as_input("Global debt service")],
              lambda c, d: _ratio(c, d, "Global debt service"))

    # ---------------- Balance sheet ----------------
    ca, cl = v("current_assets", "Current assets"), v("current_liabilities", "Current liabilities")
    b.add("working_capital", [ca, cl], lambda a, l: a - l)
    b.add("current_ratio", [ca, cl], lambda a, l: _ratio(a, l, "Current liabilities"))
    b.add("quick_ratio", [v("cash", "Cash"), v("accounts_receivable", "Accounts receivable"), cl],
          lambda c, ar, l: _ratio(c + ar, l, "Current liabilities"))
    ta, tl = v("total_assets", "Total assets"), v("total_liabilities", "Total liabilities")
    if ta.ok and tl.ok:
        eq = b.add("total_equity", [ta, tl], lambda a, l: a - l)
    else:
        eq = b.add("total_equity", [v("total_equity", "Total equity (reported)")], lambda e: e,
                   formula="Reported total equity (total assets/liabilities unavailable).")
    eq_in = eq.as_input("Total equity")
    b.add("debt_to_equity", [tl, eq_in], lambda l, e: _ratio(l, e, "Total equity"))

    # ---------------- Leverage ----------------
    if not inputs.debt_schedule_confirmed:
        b.add("funded_debt_pro_forma",
              [InputValue.missing("debt_schedule", "Existing debt schedule", "Debt schedule not confirmed complete."), amt_in],
              lambda x, a: x + a)
    else:
        bals = [d.balance for d in inputs.debts if not d.to_be_refinanced]
        b.add("funded_debt_pro_forma", [*bals, amt_in], lambda *xs: sum(xs, ZERO))
    b.add("debt_to_ebitda", [b.results["funded_debt_pro_forma"].as_input("Pro forma funded debt"), adj_in],
          lambda d, e: _ratio(d, e, "Adjusted EBITDA"))

    # ---------------- Collateral ----------------
    if not t.is_secured:
        for k in ("collateral_value", "ltv", "cltv", "discounted_collateral_value", "collateral_coverage"):
            b.na(k, "Proposed loan is unsecured.")
    elif not inputs.collateral:
        for k in ("collateral_value", "ltv", "cltv", "discounted_collateral_value", "collateral_coverage"):
            b.add(k, [InputValue.missing("collateral", "Collateral", "No collateral recorded for a secured loan.")], lambda x: x)
    else:
        vals = [c.value for c in inputs.collateral]
        cv = b.add("collateral_value", vals, lambda *xs: sum(xs, ZERO))
        cv_in = cv.as_input("Total collateral value")
        b.add("ltv", [amt_in, cv_in], lambda a, c: _ratio(a, c, "Collateral value"))
        prior = sum((c.prior_lien_balance for c in inputs.collateral), ZERO)
        prior_in = InputValue.assumed("prior_liens", "Σ prior lien balances", prior, "Entered on collateral records.",
                                      {"user_entered"})
        b.add("cltv", [amt_in, prior_in, cv_in], lambda a, p, c: _ratio(a + p, c, "Collateral value"))
        rates = params.get("collateral_advance_rates", {}) or {}
        missing_rates = sorted({c.collateral_type for c in inputs.collateral if c.collateral_type not in rates})
        rate_ins = []
        for c in inputs.collateral:
            ar = rates.get(c.collateral_type)
            if ar is None:
                rate_ins.append(InputValue.missing(f"advance_rate:{c.collateral_type}",
                                                   f"Advance rate for {c.collateral_type}", "Not defined in policy."))
            else:
                rate_ins.append(InputValue.assumed(f"advance_rate:{c.collateral_type}",
                                                   f"Policy advance rate ({c.collateral_type})", Decimal(str(ar)),
                                                   "From policy parameters.", {"policy_parameter"}))
        n = len(inputs.collateral)
        liens = [c.prior_lien_balance for c in inputs.collateral]

        def _dcv(*xs: Decimal) -> Decimal:
            values, ars = xs[:n], xs[n:]
            return sum((max(ZERO, val * ar - lien) for val, ar, lien in zip(values, ars, liens)), ZERO)

        dcv = b.add("discounted_collateral_value", [*vals, *rate_ins], _dcv,
                    note="Advance rates are policy parameters (illustrative unless your policy says otherwise)."
                    + (f" Missing advance rates: {', '.join(missing_rates)}." if missing_rates else ""))
        b.add("collateral_coverage", [dcv.as_input("Discounted collateral value"), amt_in],
              lambda d, a: _ratio(d, a, "Requested loan amount"))

    # ---------------- Loan-to-cost ----------------
    if t.project_total_cost is None and lt in ("general_commercial", "working_capital"):
        b.na("ltc", "No project cost for this loan type.")
    else:
        b.add("ltc", [amt_in, _terms_input("project_total_cost", "Total project cost", t.project_total_cost)],
              lambda a, c: _ratio(a, c, "Total project cost"))

    # ---------------- Break-even ----------------
    gm = b.results["gross_margin"].as_input("Contribution margin ratio (gross margin)")

    def _breakeven(r: Decimal, c: Decimal, d: Decimal, cmr: Decimal) -> Decimal:
        if cmr <= 0:
            raise NotMeaningful("Contribution margin ratio ≤ 0; break-even revenue is not meaningful.")
        return r - (c - d) / cmr

    def _cushion(r: Decimal, c: Decimal, d: Decimal, cmr: Decimal) -> Decimal:
        if cmr <= 0 or r <= 0:
            raise NotMeaningful("Revenue or contribution margin ≤ 0; cushion is not meaningful.")
        return (c - d) / cmr / r

    be_inputs = [rev_in, cfads_in, tds_in, gm]
    b.add("breakeven_revenue", be_inputs, _breakeven, extra_flags={"assumption_cogs_variable"},
          note="Assumes COGS varies with revenue and all other costs are fixed.")
    b.add("revenue_cushion", be_inputs, _cushion, extra_flags={"assumption_cogs_variable"},
          note="Negative means revenue already falls short of covering debt service.")

    # ---------------- CRE / property ----------------
    if lt != "commercial_real_estate":
        from uwhelper.metrics.cre import CRE_METRICS

        for k in ("effective_gross_income", "noi", "dscr_property", "debt_yield", *CRE_METRICS):
            b.na(k, "Property-level metrics apply to commercial real estate loans only.")
    else:
        egi = b.add("effective_gross_income",
                    [v("gross_potential_rent", "Gross potential rent"), v("vacancy_credit_loss", "Vacancy & credit loss"),
                     v("other_property_income", "Other property income")],
                    lambda g, vac, o: g - abs(vac) + o)
        if egi.computed or not v("net_operating_income_reported").ok:
            noi = b.add("noi", [egi.as_input("Effective gross income"),
                                v("property_operating_expenses", "Property operating expenses")],
                        lambda e, o: e - o)
        else:
            noi = b.add("noi", [v("net_operating_income_reported", "NOI as reported")], lambda x: x,
                        formula="NOI as reported in the source (components unavailable; not recomputed).",
                        extra_flags={"reported_not_recomputed"})
        noi_in = noi.as_input("NOI")
        b.add("dscr_property", [noi_in, proposed_in], lambda n, d: _ratio(n, d, "Proposed debt service"))
        b.add("debt_yield", [noi_in, amt_in], lambda n, a: _ratio(n, a, "Requested loan amount"))
        from uwhelper.metrics.cre import add_cre_metrics

        add_cre_metrics(b, inputs, params, noi_in=noi_in, proposed_in=proposed_in, amt_in=amt_in, rate_in=rate_in)

    # Every catalog metric must be present exactly once.
    for key in METRIC_SPECS:
        if key not in b.results:  # pragma: no cover - defensive
            b.na(key, "Not evaluated.")
    return b.results


def days_between(start_iso: str | None, end: date) -> int | None:
    if not start_iso:
        return None
    try:
        return (end - date.fromisoformat(start_iso[:10])).days
    except ValueError:
        return None
