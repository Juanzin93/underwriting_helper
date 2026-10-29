"""Commercial business lending module (first fully implemented module)."""

from __future__ import annotations

from dataclasses import dataclass, field

from uwhelper.modules.base import FieldSpec, LendingModule, LoanTypeSpec, MetricSpec, RequiredItem

F = FieldSpec
_FIELDS: list[FieldSpec] = [
    # --- Identity / request (static) ---
    F("borrower_legal_name", "Borrower legal name", "identity", "text", "static", "Legal name of the borrowing entity as shown on the document."),
    F("requested_loan_amount", "Requested loan amount (as stated in a document)", "request", "USD", "static",
      "Loan amount stated in a loan request or term sheet. Informational: the authoritative amount is the proposed loan terms.",
      informational=True),
    # --- Income statement (duration) ---
    F("revenue", "Revenue / net sales", "income_statement", "USD", "duration", "Total net revenue or net sales for the period. NOT bank deposits, NOT gross receipts before returns."),
    F("cost_of_goods_sold", "Cost of goods sold", "income_statement", "USD", "duration", "Cost of goods sold / cost of sales."),
    F("gross_profit", "Gross profit (as reported)", "income_statement", "USD", "duration", "Gross profit as printed. Informational; metrics recompute revenue − COGS.", informational=True),
    F("operating_expenses", "Total operating expenses", "income_statement", "USD", "duration", "Total operating (SG&A) expenses excluding COGS, interest and income taxes."),
    F("officer_compensation", "Officer / owner compensation", "income_statement", "USD", "duration", "Compensation of officers/owners.", informational=True),
    F("rent_expense", "Rent expense", "income_statement", "USD", "duration", "Rent / lease expense.", informational=True),
    F("interest_expense", "Interest expense", "income_statement", "USD", "duration", "Interest expense for the period."),
    F("depreciation", "Depreciation", "income_statement", "USD", "duration", "Depreciation expense."),
    F("amortization", "Amortization", "income_statement", "USD", "duration", "Amortization expense (enter 0 explicitly if none)."),
    F("income_tax_expense", "Income tax expense", "income_statement", "USD", "duration", "Income tax expense / provision (0 for pass-through entities if none)."),
    F("net_income", "Net income", "income_statement", "USD", "duration", "Net income (loss) after taxes. Do not confuse with taxable income."),
    F("ebitda_reported", "EBITDA (as reported)", "income_statement", "USD", "duration", "EBITDA as printed by the borrower. Informational; never used directly.", informational=True),
    F("ebitda_adjustment", "EBITDA adjustment (add-back)", "adjustments", "USD", "duration",
      "One documented add-back (+) or deduction (−); describe it in the item label (e.g. 'one-time legal settlement').",
      multi_item=True),
    F("unfinanced_capex", "Unfinanced capital expenditures", "cash_flow", "USD", "duration", "Capital expenditures paid from cash flow (not financed). Enter 0 if none."),
    F("cash_taxes_paid", "Cash taxes paid", "cash_flow", "USD", "duration", "Income taxes actually paid in cash. Enter 0 if none."),
    F("distributions", "Owner distributions / dividends", "cash_flow", "USD", "duration", "Distributions or dividends paid to owners. Enter 0 if none."),
    F("bank_deposits_total", "Total bank deposits", "bank", "USD", "duration", "Sum of deposits on bank statements. Informational only — deposits are not revenue.", informational=True),
    F("projected_revenue", "Projected revenue", "projection", "USD", "duration", "Borrower projection. Informational — never used in base-case metrics.", informational=True),
    # --- Balance sheet (instant) ---
    F("cash", "Cash and equivalents", "balance_sheet", "USD", "instant", "Cash and cash equivalents at the balance sheet date."),
    F("accounts_receivable", "Accounts receivable", "balance_sheet", "USD", "instant", "Trade accounts receivable (net)."),
    F("inventory", "Inventory", "balance_sheet", "USD", "instant", "Inventory."),
    F("current_assets", "Total current assets", "balance_sheet", "USD", "instant", "Total current assets."),
    F("total_assets", "Total assets", "balance_sheet", "USD", "instant", "Total assets."),
    F("accounts_payable", "Accounts payable", "balance_sheet", "USD", "instant", "Trade accounts payable."),
    F("current_liabilities", "Total current liabilities", "balance_sheet", "USD", "instant", "Total current liabilities."),
    F("total_liabilities", "Total liabilities", "balance_sheet", "USD", "instant", "Total liabilities."),
    F("total_equity", "Total equity (as reported)", "balance_sheet", "USD", "instant", "Total equity / net worth as printed."),
    F("ar_over_90_days", "Receivables > 90 days", "aging", "USD", "instant", "Accounts receivable aged over 90 days.", informational=True),
    F("ap_over_90_days", "Payables > 90 days", "aging", "USD", "instant", "Accounts payable aged over 90 days.", informational=True),
    # --- Commercial real estate (duration) ---
    F("gross_potential_rent", "Gross potential rent", "property", "USD", "duration", "Annual rent at full occupancy."),
    F("vacancy_credit_loss", "Vacancy & credit loss", "property", "USD", "duration", "Vacancy and collection loss (positive amount)."),
    F("other_property_income", "Other property income", "property", "USD", "duration", "Other property income (parking, fees). Enter 0 if none."),
    F("property_operating_expenses", "Property operating expenses", "property", "USD", "duration", "Property operating expenses excluding debt service, depreciation and capex."),
    F("net_operating_income_reported", "NOI (as reported)", "property", "USD", "duration", "Net operating income as printed."),
    # --- Guarantor personal (duration, party-scoped) ---
    F("guarantor_personal_income", "Guarantor personal income (excl. this business)", "guarantor", "USD", "duration",
      "Annual personal cash income NOT paid by the borrowing business (avoid double counting).", party_scoped=True),
    F("guarantor_living_expenses", "Guarantor living expenses", "guarantor", "USD", "duration", "Annual personal living expenses.", party_scoped=True),
    F("guarantor_personal_debt_service", "Guarantor personal debt service", "guarantor", "USD", "duration",
      "Annual personal debt payments (mortgage, auto, cards, etc.).", party_scoped=True),
]

_DOC_TYPES = {
    "business_tax_return": "Business tax return",
    "income_statement": "Income statement / P&L",
    "balance_sheet": "Balance sheet",
    "cash_flow_statement": "Cash flow statement",
    "interim_financials": "Interim / YTD financial statements",
    "bank_statement": "Bank statement",
    "debt_schedule": "Business debt schedule",
    "loan_agreement": "Existing loan agreement / note",
    "ar_aging": "Accounts receivable aging",
    "ap_aging": "Accounts payable aging",
    "projections": "Financial projections",
    "appraisal": "Appraisal / collateral valuation",
    "equipment_quote": "Equipment invoice / quote",
    "purchase_agreement": "Purchase / acquisition agreement",
    "rent_roll": "Rent roll",
    "operating_statement": "Property operating statement",
    "personal_financial_statement": "Personal financial statement",
    "personal_tax_return": "Personal tax return",
    "loan_request": "Loan request / term sheet",
    "other": "Other",
}

_LOAN_TYPES = [
    LoanTypeSpec("general_commercial", "General commercial business loan"),
    LoanTypeSpec("working_capital", "Working capital loan / line"),
    LoanTypeSpec("equipment", "Equipment financing"),
    LoanTypeSpec("business_acquisition", "Business acquisition loan"),
    LoanTypeSpec("commercial_real_estate", "Commercial real estate loan"),
    LoanTypeSpec("sba_7a", "SBA 7(a) (later extension)", implemented=False,
                 note="Requires properly sourced, versioned SBA SOP requirements. Not implemented."),
    LoanTypeSpec("sba_504", "SBA 504 (later extension)", implemented=False, note="Not implemented."),
    LoanTypeSpec("private_hard_money", "Private / hard-money (later extension)", implemented=False, note="Not implemented."),
]

R = RequiredItem
_CORE_IS = ("revenue", "cost_of_goods_sold", "operating_expenses", "net_income", "interest_expense",
            "depreciation", "amortization", "income_tax_expense")
_CASH_DEDUCTIONS = ("unfinanced_capex", "cash_taxes_paid", "distributions")
_BALANCE = ("cash", "accounts_receivable", "current_assets", "current_liabilities", "total_assets", "total_liabilities")
_BUSINESS_TYPES = ("general_commercial", "working_capital", "equipment", "business_acquisition", "commercial_real_estate")

_REQUIRED: list[RequiredItem] = [
    R("base_period", "A 12-month base financial period selected or detectable", "base_period",
      why="Annual metrics need a defined reporting period."),
    R("terms", "Proposed loan terms (amount, rate, amortization or interest-only)", "terms",
      fields=("requested_amount", "interest_rate", "amortization_months"), why="Needed for proposed debt service."),
    R("income_statement", "Income statement line items for the base period", "fact", fields=_CORE_IS,
      why="Needed for EBITDA, margins and break-even."),
    R("cash_deductions", "Cash-flow deductions (capex, cash taxes, distributions; 0 if none)", "fact",
      fields=_CASH_DEDUCTIONS, why="Needed for CFADS and DSCR."),
    R("balance_sheet", "Balance sheet as of base period end", "fact", fields=_BALANCE,
      why="Needed for liquidity and leverage ratios."),
    R("debt_schedule", "Existing debt schedule confirmed complete", "debt_schedule",
      why="Existing debt service and pro forma leverage."),
    R("collateral", "Collateral with a dated valuation (secured loans)", "collateral", why="LTV and collateral coverage."),
    R("guarantor", "Guarantor personal cash flow (if guarantors exist)", "guarantor", why="Global cash flow."),
    R("project_cost", "Total project cost", "terms", fields=("project_total_cost",),
      loan_types=("equipment", "business_acquisition", "commercial_real_estate"), why="Loan-to-cost."),
    R("property", "Property operating data (rent, vacancy, other income, operating expenses)", "fact",
      fields=("gross_potential_rent", "vacancy_credit_loss", "other_property_income", "property_operating_expenses"),
      loan_types=("commercial_real_estate",), why="NOI, property DSCR, debt yield."),
]


@dataclass
class CommercialModule(LendingModule):
    key: str = "commercial"
    name: str = "Commercial business lending"
    loan_types: dict[str, LoanTypeSpec] = field(default_factory=lambda: {lt.key: lt for lt in _LOAN_TYPES})

    def field_catalog(self) -> dict[str, FieldSpec]:
        return {f.key: f for f in _FIELDS}

    def metric_catalog(self) -> dict[str, MetricSpec]:
        from uwhelper.metrics.commercial import METRIC_SPECS

        return dict(METRIC_SPECS)

    def compute_metrics(self, inputs, params):  # type: ignore[override]
        from uwhelper.metrics.commercial import compute_commercial_metrics

        return compute_commercial_metrics(inputs, params)

    def required_items(self, loan_type: str) -> list[RequiredItem]:
        return [r for r in _REQUIRED if r.loan_types is None or loan_type in r.loan_types]

    def document_types(self) -> dict[str, str]:
        return dict(_DOC_TYPES)
