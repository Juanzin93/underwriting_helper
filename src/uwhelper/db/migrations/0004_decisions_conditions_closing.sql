-- 0004: human credit decisions, term sets (approved / executed), conditions, closing and funding.

CREATE TABLE credit_decisions (
    id                     TEXT PRIMARY KEY,
    case_id                TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    run_id                 TEXT NOT NULL REFERENCES underwriting_runs(id) ON DELETE RESTRICT,
    memo_id                TEXT REFERENCES credit_memos(id) ON DELETE SET NULL,
    decision               TEXT NOT NULL CHECK (decision IN ('APPROVE','APPROVE_WITH_CONDITIONS','DECLINE','DEFER')),
    decided_by             TEXT NOT NULL,
    authority_note         TEXT,
    rationale              TEXT NOT NULL,
    conditions_text        TEXT,
    analysis_snapshot_json TEXT NOT NULL DEFAULT '{}',
    decided_at             TEXT NOT NULL
);
CREATE INDEX ix_decisions_case ON credit_decisions(case_id, decided_at);

CREATE TRIGGER trg_decisions_immutable BEFORE UPDATE ON credit_decisions
WHEN NEW.memo_id IS OLD.memo_id
BEGIN
    SELECT RAISE(ABORT, 'credit decisions are immutable: record a new decision');
END;

-- Human-approved and final executed terms (requested terms stay in loan_terms; simulations in loan_structures).
CREATE TABLE term_sets (
    id                    TEXT PRIMARY KEY,
    case_id               TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    term_type             TEXT NOT NULL CHECK (term_type IN ('approved','executed')),
    revision              INTEGER NOT NULL,
    decision_id           TEXT REFERENCES credit_decisions(id) ON DELETE RESTRICT,
    source_structure_id   TEXT REFERENCES loan_structures(id) ON DELETE SET NULL,
    principal             TEXT NOT NULL,
    annual_rate           TEXT NOT NULL,
    rate_type             TEXT NOT NULL CHECK (rate_type IN ('fixed','variable')),
    rate_index_note       TEXT,
    term_months           INTEGER NOT NULL CHECK (term_months > 0),
    amortization_months   INTEGER NOT NULL CHECK (amortization_months > 0),
    interest_only_months  INTEGER NOT NULL DEFAULT 0 CHECK (interest_only_months >= 0),
    payment_frequency     TEXT NOT NULL CHECK (payment_frequency IN ('monthly','quarterly','semiannual','annual')),
    origination_fee       TEXT NOT NULL DEFAULT '0',
    other_costs           TEXT NOT NULL DEFAULT '0',
    entered_by            TEXT NOT NULL,
    notes                 TEXT,
    created_at            TEXT NOT NULL,
    UNIQUE (case_id, term_type, revision)
);

CREATE TRIGGER trg_term_sets_immutable BEFORE UPDATE ON term_sets
WHEN NEW.source_structure_id IS OLD.source_structure_id
BEGIN
    SELECT RAISE(ABORT, 'term sets are immutable: record a new revision');
END;

-- Due-diligence and closing conditions (policy-generated or manual).
CREATE TABLE loan_conditions (
    id                  TEXT PRIMARY KEY,
    case_id             TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    code                TEXT NOT NULL,
    phase               TEXT NOT NULL CHECK (phase IN ('DUE_DILIGENCE','CLOSING')),
    source              TEXT NOT NULL CHECK (source IN ('policy','manual','credit_decision')),
    policy_id           TEXT REFERENCES policies(id) ON DELETE SET NULL,
    description         TEXT NOT NULL,
    category            TEXT NOT NULL,
    required            INTEGER NOT NULL CHECK (required IN (0,1)),
    waivable            INTEGER NOT NULL DEFAULT 1 CHECK (waivable IN (0,1)),
    responsible_party   TEXT NOT NULL DEFAULT 'borrower',
    due_date            TEXT,
    status              TEXT NOT NULL DEFAULT 'NOT_STARTED' CHECK (status IN
                        ('NOT_STARTED','REQUESTED','RECEIVED','UNDER_REVIEW','SATISFIED','WAIVED','REJECTED')),
    document_id         TEXT REFERENCES documents(id) ON DELETE SET NULL,
    evidence_note       TEXT,
    verification_status TEXT NOT NULL DEFAULT 'unverified' CHECK (verification_status IN ('unverified','verified')),
    completed_at        TEXT,
    reviewer_notes      TEXT,
    waiver_reason       TEXT,
    waived_by           TEXT,
    waived_at           TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (case_id, code)
);
CREATE INDEX ix_conditions_case ON loan_conditions(case_id, phase, status);

CREATE TABLE condition_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id  TEXT NOT NULL REFERENCES loan_conditions(id) ON DELETE CASCADE,
    from_status   TEXT,
    to_status     TEXT NOT NULL,
    note          TEXT,
    actor         TEXT NOT NULL DEFAULT 'local_user',
    details_json  TEXT NOT NULL DEFAULT '{}',
    occurred_at   TEXT NOT NULL
);
CREATE INDEX ix_condition_events ON condition_events(condition_id, id);

CREATE TRIGGER trg_condition_events_no_update BEFORE UPDATE ON condition_events
BEGIN
    SELECT RAISE(ABORT, 'condition_events is append-only');
END;

CREATE TABLE closings (
    id                         TEXT PRIMARY KEY,
    case_id                    TEXT NOT NULL UNIQUE REFERENCES loan_cases(id) ON DELETE CASCADE,
    scheduled_closing_date     TEXT,
    disbursement_instructions  TEXT,
    disbursement_status        TEXT NOT NULL DEFAULT 'NOT_STARTED'
                               CHECK (disbursement_status IN ('NOT_STARTED','INSTRUCTIONS_RECORDED','REPORTED_DISBURSED')),
    outstanding_issues         TEXT,
    notes                      TEXT,
    created_at                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL
);

CREATE TABLE fundings (
    id                  TEXT PRIMARY KEY,
    case_id             TEXT NOT NULL UNIQUE REFERENCES loan_cases(id) ON DELETE CASCADE,
    executed_terms_id   TEXT NOT NULL REFERENCES term_sets(id) ON DELETE RESTRICT,
    funded_amount       TEXT NOT NULL,
    funding_date        TEXT NOT NULL,
    first_payment_date  TEXT NOT NULL,
    confirmed_by        TEXT NOT NULL,
    confirmation_text   TEXT NOT NULL,
    notes               TEXT,
    created_at          TEXT NOT NULL
);

CREATE TRIGGER trg_fundings_immutable BEFORE UPDATE ON fundings
BEGIN
    SELECT RAISE(ABORT, 'funding records are immutable');
END;
