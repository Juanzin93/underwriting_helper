# Metric definitions

All metrics are computed by `metrics/commercial.py` in `Decimal`. Money is rounded to cents and ratios to
6 dp only for storage/display; policy rules may round (`round_to`, ROUND_HALF_UP) before comparing.

**Basis.** Income-statement items come from the *base period* (default: latest 12-month period with revenue;
selectable). A shorter period is annualized ×12/months and flagged `annualized` (rules then require review).
Balance-sheet items are taken as of the base period end date (otherwise the latest earlier date, flagged
`period_mismatch`). Revenue is never inferred from bank deposits, taxable income is not net income, and NOI
(property) is not EBITDA (business).

**Prerequisites.** If any input is missing, contradictory or (verified-only run) unverified, the metric is
`not_computable`. If the denominator is ≤ 0 it is `not_meaningful`. Zero is never assumed — enter 0 explicitly.

| Metric | Definition |
|---|---|
| Gross profit | Revenue − COGS |
| Operating income | Gross profit − Operating expenses (excl. COGS, interest, income tax) |
| EBITDA | Net income + Interest + Income tax + Depreciation + Amortization |
| Adjusted EBITDA | EBITDA + Σ documented adjustments (unverified excluded in verified-only runs) |
| CFADS | Adjusted EBITDA − Unfinanced capex − Cash taxes paid − Distributions |
| Existing debt service | Σ annualized payments (monthly×12, quarterly×4, …) of debts **not** refinanced; requires schedule confirmed complete |
| Proposed debt service | Amortizing: 12 × P·r/(1−(1+r)^−n), r = rate/12; 0 % rate: P/n; interest-only: P × rate |
| DSCR | CFADS ÷ (existing + proposed debt service) |
| Global cash flow / DS / DSCR | CFADS + Σ guarantor income − Σ living expenses; business DS + Σ personal DS |
| Working capital, current, quick | CA − CL; CA ÷ CL; (cash + AR) ÷ CL |
| Total equity | TA − TL (reported equity only if TA/TL missing) |
| Debt-to-equity | TL ÷ equity (not meaningful if equity ≤ 0) |
| Funded debt ÷ Adjusted EBITDA | (continuing debt balances + requested loan) ÷ Adjusted EBITDA |
| LTV / CLTV | Loan ÷ Σ collateral value; (loan + prior liens) ÷ Σ value |
| LTC | Loan ÷ total project cost |
| Collateral coverage | Σ max(0, value × policy advance rate − prior liens) ÷ loan |
| Break-even revenue | Revenue − (CFADS − DS) ÷ gross-margin ratio (COGS variable, other costs fixed) |
| Revenue cushion | (CFADS − DS) ÷ gross-margin ratio ÷ revenue |
| CRE: EGI, NOI, property DSCR, debt yield | GPR − vacancy + other income; EGI − property opex; NOI ÷ proposed DS; NOI ÷ loan |

**Scenarios/sensitivity**: revenue shock scales revenue and COGS; opex shock scales operating expenses; the
difference flows to net income (taxes unchanged); rate shocks apply to the proposed loan only.

## CRE metrics (Milestone 3, `metrics/cre.py`)

| Metric | Formula |
|---|---|
| Property DSCR at stressed rate | NOI ÷ proposed annual debt service at (rate + policy `cre_stress_rate_bps`) |
| Break-even occupancy | (property opex + proposed annual debt service) ÷ (gross potential rent + other income) |
| Income-approach value | NOI ÷ policy `cre_cap_rate` — a cross-check, not an appraisal |
| LTV on income-approach value | loan ÷ income-approach value |
| Physical occupancy | occupied units ÷ total units (rent roll) |
| Economic occupancy | in-place annual rent ÷ (in-place rent + market rent of vacant units); not calculated if any vacant unit lacks a market rent |
| Rollover 12 / 24 months | annual rent of leases ending within 12 / 24 months of the rent-roll date (month-to-month included) ÷ in-place rent |
| WALT (years) | Σ(rent × remaining days ÷ 365) ÷ Σ rent, occupied units; month-to-month = 0; not calculated if any lease end is unknown |

Rent-roll metrics use the **verified** rent roll in verified-only runs (the newest draft may be used, flagged
unverified, in preview runs). A rent roll older than 180 days at run time is flagged `stale`.
Every value in a run can be traced (Metrics → "Evidence trail") and re-checked (Run → "Traceability check").
