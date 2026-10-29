# Servicing, payments, covenants and monitoring

## Money and rounding policy
* Amounts are `Decimal` and stored as text; no binary floating point is used for money.
* Payment schedules (structuring and servicing share one engine, `metrics/structuring.py`):
  periodic rate = annual nominal rate ÷ payments per year; interest = round-half-up(balance × rate, cents);
  level payment = round-half-up(annuity payment, cents), recomputed after an interest-only period or a rate step;
  the last amortizing payment clears the balance exactly; an unamortized balance at the term end is a balloon.
* Day counts (actual/360, actual/365), rate floors/caps and business-day adjustments are **not** modelled.

## Scheduled vs actual
`payment_schedules` / `schedule_installments` are what is **due**; `payments` are what you **report** as received
(manually or by CSV). Nothing is assumed paid because a date passed. Payments are immutable; a mistake is corrected
by a reversal entry (reason required), and both remain visible.

## Allocation (deterministic)
1. Reversed payments and their reversals are excluded.
2. Payments, in date order, pay installments in due-date order: interest, then scheduled principal
   (money beyond what is due pays the next installments — "paid ahead").
3. After all installments: assessed fees, then unscheduled principal (prepayment).
4. Outstanding principal = funded principal − scheduled principal paid − unscheduled principal paid.
Interest follows the schedule (no daily accrual); a prepayment does not re-amortize the schedule unless you record a
modification.

## Delinquency
An installment is past due when its due date has passed and it is not fully paid. Days past due count from the oldest
unpaid due date. Buckets: CURRENT, WITHIN_GRACE (≤ grace days), DPD_1_29, DPD_30_59, DPD_60_89, DPD_90_PLUS.

## Modifications
Rate change, term extension, payment deferral, re-amortization or restructure create a **new schedule version**
effective on a date; the prior version still governs installments due before that date. The account must be current
and must not have payments applied ahead past the effective date (arrears capitalization is not implemented).
Before/after terms, approver and reason are stored in `loan_modifications`.

## CSV import
Header `date,amount` plus optional `reference,method,note`; UTF-8; ≤ 5,000 rows. All rows are validated first —
if any is invalid nothing is imported. Rows identical to an existing payment (date, amount, reference) are skipped.

## Covenants and monitoring
Covenants reference a metric (DSCR, global DSCR, current/quick ratio, debt-to-equity, funded debt/EBITDA, working
capital, equity, LTV) or a manual measurement, with operator, threshold, frequency and reporting days. Tests come from a
**verified-only base underwriting run** (deterministic metric value) or a manual value with an evidence note. A test
with no measurable value is UNKNOWN, never PASS. Monitoring items (annual statements, tax returns, insurance renewal,
collateral revaluation) recur automatically when completed.

Alerts are explicit rules: PAYMENT_PAST_DUE, MATURITY_WITHIN_90_DAYS, BALLOON_WITHIN_180_DAYS, COVENANT_BREACH,
COVENANT_UNKNOWN, COVENANT_TEST_MISSING, METRIC_DETERIORATION (≥ 15 % adverse change between the last two tests),
MONITORING_ITEM_OVERDUE. "Record current alerts" stores new ones as monitoring events (de-duplicated) that can be
acknowledged with a note. No model predictions are used.

## What-if
Re-amortizes the current outstanding balance over the remaining payments under a different rate or after an extra
payment, with DSCR from a CFADS you supply (defaults to the latest verified run). Planning only; nothing is saved.
