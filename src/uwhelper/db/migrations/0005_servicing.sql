-- 0005_servicing: servicing accounts, versioned payment schedules, actual payments, fees,
-- covenants and tests, monitoring items/events, modifications.

CREATE TABLE servicing_accounts (
    id                     TEXT PRIMARY KEY,
    case_id                TEXT NOT NULL UNIQUE REFERENCES loan_cases(id) ON DELETE CASCADE,
    funding_id             TEXT NOT NULL UNIQUE REFERENCES fundings(id) ON DELETE RESTRICT,
    loan_number            TEXT NOT NULL UNIQUE,
    original_principal     TEXT NOT NULL,     -- executed (contract) principal
    funded_principal       TEXT NOT NULL,     -- actual amount funded
    grace_days             INTEGER NOT NULL DEFAULT 10 CHECK (grace_days >= 0),
    status                 TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','PAID_OFF')),
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE TABLE payment_schedules (
    id                    TEXT PRIMARY KEY,
    account_id            TEXT NOT NULL REFERENCES servicing_accounts(id) ON DELETE CASCADE,
    version               INTEGER NOT NULL,
    is_current            INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
    reason                TEXT NOT NULL,
    effective_date        TEXT NOT NULL,
    principal             TEXT NOT NULL,
    annual_rate           TEXT NOT NULL,
    rate_type             TEXT NOT NULL,
    payment_frequency     TEXT NOT NULL,
    term_months           INTEGER NOT NULL,
    amortization_months   INTEGER NOT NULL,
    interest_only_months  INTEGER NOT NULL DEFAULT 0,
    first_payment_date    TEXT NOT NULL,
    maturity_date         TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    UNIQUE (account_id, version)
);

CREATE TABLE schedule_installments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id   TEXT NOT NULL REFERENCES payment_schedules(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    due_date      TEXT NOT NULL,
    payment       TEXT NOT NULL,
    interest      TEXT NOT NULL,
    principal     TEXT NOT NULL,
    balance_after TEXT NOT NULL,
    is_balloon    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (schedule_id, seq)
);

CREATE TRIGGER trg_installments_immutable BEFORE UPDATE ON schedule_installments
BEGIN
    SELECT RAISE(ABORT, 'schedule installments are immutable: create a new schedule version');
END;

-- Actual money received, as reported by the user. Immutable; corrections are reversal entries.
CREATE TABLE payments (
    id               TEXT PRIMARY KEY,
    account_id       TEXT NOT NULL REFERENCES servicing_accounts(id) ON DELETE CASCADE,
    received_date    TEXT NOT NULL,
    amount           TEXT NOT NULL,
    entry_type       TEXT NOT NULL DEFAULT 'PAYMENT' CHECK (entry_type IN ('PAYMENT','REVERSAL')),
    reverses_id      TEXT REFERENCES payments(id) ON DELETE RESTRICT,
    method           TEXT,
    reference        TEXT,
    source           TEXT NOT NULL CHECK (source IN ('manual','csv')),
    import_batch     TEXT,
    note             TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX ix_payments_account ON payments(account_id, received_date);
CREATE UNIQUE INDEX ux_payments_reversal ON payments(reverses_id) WHERE reverses_id IS NOT NULL;

CREATE TRIGGER trg_payments_immutable BEFORE UPDATE ON payments
BEGIN
    SELECT RAISE(ABORT, 'payments are immutable: record a reversal instead');
END;

CREATE TABLE account_fees (
    id          TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL REFERENCES servicing_accounts(id) ON DELETE CASCADE,
    fee_date    TEXT NOT NULL,
    fee_type    TEXT NOT NULL,
    amount      TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE covenants (
    id               TEXT PRIMARY KEY,
    account_id       TEXT NOT NULL REFERENCES servicing_accounts(id) ON DELETE CASCADE,
    code             TEXT NOT NULL,
    description      TEXT NOT NULL,
    metric_key       TEXT NOT NULL,
    operator         TEXT NOT NULL CHECK (operator IN ('>=','>','<=','<')),
    threshold        TEXT NOT NULL,
    frequency        TEXT NOT NULL CHECK (frequency IN ('quarterly','semiannual','annual')),
    first_test_date  TEXT NOT NULL,
    reporting_days   INTEGER NOT NULL DEFAULT 120 CHECK (reporting_days >= 0),
    active           INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at       TEXT NOT NULL,
    UNIQUE (account_id, code)
);

CREATE TABLE covenant_tests (
    id              TEXT PRIMARY KEY,
    covenant_id     TEXT NOT NULL REFERENCES covenants(id) ON DELETE CASCADE,
    test_period_end TEXT NOT NULL,
    observed_value  TEXT,
    source          TEXT NOT NULL CHECK (source IN ('underwriting_run','manual')),
    run_id          TEXT REFERENCES underwriting_runs(id) ON DELETE SET NULL,
    evidence_note   TEXT,
    result          TEXT NOT NULL CHECK (result IN ('PASS','FAIL','UNKNOWN')),
    explanation     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX ix_covenant_tests ON covenant_tests(covenant_id, test_period_end);

CREATE TRIGGER trg_covenant_tests_immutable BEFORE UPDATE ON covenant_tests
WHEN NEW.run_id IS OLD.run_id
BEGIN
    SELECT RAISE(ABORT, 'covenant tests are immutable: record a new test');
END;

CREATE TABLE monitoring_items (
    id                TEXT PRIMARY KEY,
    account_id        TEXT NOT NULL REFERENCES servicing_accounts(id) ON DELETE CASCADE,
    item_type         TEXT NOT NULL CHECK (item_type IN
                      ('financial_statements','insurance_renewal','collateral_revaluation','tax_returns','other')),
    description       TEXT NOT NULL,
    due_date          TEXT NOT NULL,
    frequency_months  INTEGER CHECK (frequency_months IS NULL OR frequency_months > 0),
    status            TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','DONE')),
    completed_at      TEXT,
    completion_note   TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX ix_monitoring_items ON monitoring_items(account_id, status, due_date);

CREATE TABLE monitoring_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       TEXT NOT NULL REFERENCES servicing_accounts(id) ON DELETE CASCADE,
    event_key        TEXT NOT NULL,
    rule             TEXT NOT NULL,
    severity         TEXT NOT NULL,
    message          TEXT NOT NULL,
    first_seen_at    TEXT NOT NULL,
    acknowledged_at  TEXT,
    acknowledged_note TEXT,
    UNIQUE (account_id, event_key)
);

CREATE TABLE loan_modifications (
    id                 TEXT PRIMARY KEY,
    account_id         TEXT NOT NULL REFERENCES servicing_accounts(id) ON DELETE CASCADE,
    modification_type  TEXT NOT NULL CHECK (modification_type IN
                       ('rate_change','term_extension','payment_deferral','re_amortization','restructure')),
    effective_date     TEXT NOT NULL,
    reason             TEXT NOT NULL,
    approved_by        TEXT NOT NULL,
    before_json        TEXT NOT NULL,
    after_json         TEXT NOT NULL,
    old_schedule_id    TEXT NOT NULL REFERENCES payment_schedules(id) ON DELETE RESTRICT,
    new_schedule_id    TEXT NOT NULL REFERENCES payment_schedules(id) ON DELETE RESTRICT,
    created_at         TEXT NOT NULL
);

CREATE TRIGGER trg_modifications_immutable BEFORE UPDATE ON loan_modifications
BEGIN
    SELECT RAISE(ABORT, 'loan modifications are immutable');
END;
