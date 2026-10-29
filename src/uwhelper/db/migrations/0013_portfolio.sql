-- 0013_portfolio (Milestone 7): brokered-loan reminders, rate-reset frequency, payoff quotes.

-- Optional: for variable-rate loans, how often the rate resets (months from funding).
ALTER TABLE deal_closings ADD COLUMN rate_reset_months INTEGER CHECK (rate_reset_months IS NULL OR rate_reset_months BETWEEN 1 AND 120);

-- One reminder task per (deal, kind, event date, lead time) — never created twice.
CREATE TABLE deal_reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id     TEXT NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('maturity','balloon','interest_only_end','rate_reset')),
    event_date  TEXT NOT NULL,
    lead_days   INTEGER NOT NULL,
    task_id     TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (deal_id, kind, event_date, lead_days)
);

-- Payoff quotes for loans serviced in the app: stored with every input so they can be re-checked.
CREATE TABLE payoff_quotes (
    id                  TEXT PRIMARY KEY,
    account_id          TEXT NOT NULL REFERENCES servicing_accounts(id) ON DELETE CASCADE,
    quote_date          TEXT NOT NULL,
    good_through        TEXT NOT NULL,
    day_count           TEXT NOT NULL CHECK (day_count IN ('actual/365','actual/360')),
    principal           TEXT NOT NULL,
    annual_rate         TEXT NOT NULL,
    paid_through        TEXT NOT NULL,
    days                INTEGER NOT NULL,
    per_diem            TEXT NOT NULL,
    accrued_interest    TEXT NOT NULL,
    unpaid_fees         TEXT NOT NULL,
    total               TEXT NOT NULL,
    inputs_json         TEXT NOT NULL,
    inputs_sha256       TEXT NOT NULL,
    created_by          TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX ix_payoff_quotes_account ON payoff_quotes(account_id, created_at);
CREATE TRIGGER trg_payoff_quotes_immutable BEFORE UPDATE ON payoff_quotes
BEGIN
    SELECT RAISE(ABORT, 'payoff quotes are immutable');
END;
