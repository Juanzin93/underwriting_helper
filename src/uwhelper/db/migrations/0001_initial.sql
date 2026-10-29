-- 0001_initial: core schema for the local underwriting workbench.
-- Monetary and ratio values are stored as TEXT decimal strings so that
-- no binary floating point rounding enters the financial record.

CREATE TABLE loan_cases (
    id                       TEXT PRIMARY KEY,
    case_number              TEXT NOT NULL UNIQUE,
    title                    TEXT NOT NULL,
    module_key               TEXT NOT NULL DEFAULT 'commercial',
    loan_type                TEXT NOT NULL,
    policy_key               TEXT,
    policy_version           TEXT,
    status                   TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','in_review','archived')),
    debt_schedule_confirmed  INTEGER NOT NULL DEFAULT 0 CHECK (debt_schedule_confirmed IN (0,1)),
    base_period_id           TEXT,
    is_synthetic             INTEGER NOT NULL DEFAULT 0 CHECK (is_synthetic IN (0,1)),
    notes                    TEXT,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL
);

CREATE TABLE borrowers (
    id                 TEXT PRIMARY KEY,
    case_id            TEXT NOT NULL UNIQUE REFERENCES loan_cases(id) ON DELETE CASCADE,
    legal_name         TEXT NOT NULL,
    dba_name           TEXT,
    entity_type        TEXT,
    industry           TEXT,
    naics_code         TEXT,
    state_of_formation TEXT,
    year_established   INTEGER,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

-- Owners and guarantors (individuals or entities).
CREATE TABLE parties (
    id              TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    party_type      TEXT NOT NULL DEFAULT 'individual' CHECK (party_type IN ('individual','entity')),
    ownership_pct   TEXT,
    is_guarantor    INTEGER NOT NULL DEFAULT 0 CHECK (is_guarantor IN (0,1)),
    guarantee_type  TEXT NOT NULL DEFAULT 'none' CHECK (guarantee_type IN ('none','unlimited','limited')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX ix_parties_case ON parties(case_id);

-- Proposed loan structure. Every edit inserts a new revision; history is kept.
CREATE TABLE loan_terms (
    id                     TEXT PRIMARY KEY,
    case_id                TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    revision               INTEGER NOT NULL,
    is_current             INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
    loan_purpose           TEXT,
    requested_amount       TEXT,
    term_months            INTEGER,
    amortization_months    INTEGER,
    interest_rate          TEXT,
    rate_type              TEXT NOT NULL DEFAULT 'fixed' CHECK (rate_type IN ('fixed','variable')),
    repayment_type         TEXT NOT NULL DEFAULT 'amortizing' CHECK (repayment_type IN ('amortizing','interest_only')),
    project_total_cost     TEXT,
    requested_closing_date TEXT,
    is_secured             INTEGER NOT NULL DEFAULT 1 CHECK (is_secured IN (0,1)),
    notes                  TEXT,
    created_at             TEXT NOT NULL,
    UNIQUE (case_id, revision)
);
CREATE INDEX ix_loan_terms_case_current ON loan_terms(case_id, is_current);

CREATE TABLE documents (
    id                 TEXT PRIMARY KEY,
    sha256             TEXT NOT NULL UNIQUE,
    original_filename  TEXT NOT NULL,
    stored_name        TEXT NOT NULL,
    file_ext           TEXT NOT NULL,
    size_bytes         INTEGER NOT NULL,
    page_count         INTEGER,
    processing_status  TEXT NOT NULL DEFAULT 'pending'
                       CHECK (processing_status IN ('pending','processed','partial','failed','encrypted','unsupported')),
    processing_error   TEXT,
    ocr_pages          INTEGER NOT NULL DEFAULT 0,
    injection_flags    INTEGER NOT NULL DEFAULT 0,
    imported_at        TEXT NOT NULL,
    processed_at       TEXT
);

CREATE TABLE case_documents (
    case_id      TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    doc_type     TEXT NOT NULL DEFAULT 'other',
    period_label TEXT,
    document_date TEXT,
    notes        TEXT,
    added_at     TEXT NOT NULL,
    PRIMARY KEY (case_id, document_id)
);
CREATE INDEX ix_case_documents_doc ON case_documents(document_id);

CREATE TABLE document_pages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id         TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number         INTEGER NOT NULL CHECK (page_number >= 1),
    text_source         TEXT NOT NULL CHECK (text_source IN ('text_layer','ocr','none')),
    text                TEXT NOT NULL DEFAULT '',
    char_count          INTEGER NOT NULL DEFAULT 0,
    ocr_status          TEXT NOT NULL DEFAULT 'not_needed'
                        CHECK (ocr_status IN ('not_needed','done','unavailable','failed')),
    injection_suspected INTEGER NOT NULL DEFAULT 0,
    injection_patterns  TEXT,
    UNIQUE (document_id, page_number)
);

-- Reporting periods. period_months = 0 means a point-in-time (balance sheet) date.
CREATE TABLE financial_periods (
    id            TEXT PRIMARY KEY,
    case_id       TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    period_end    TEXT NOT NULL,
    period_months INTEGER NOT NULL CHECK (period_months BETWEEN 0 AND 12),
    label         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (case_id, period_end, period_months)
);

CREATE TABLE extraction_runs (
    id               TEXT PRIMARY KEY,
    case_id          TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    document_id      TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    model_name       TEXT NOT NULL,
    model_digest     TEXT,
    prompt_version   TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('running','completed','partial','failed')),
    error_code       TEXT,
    error_message    TEXT,
    chunks_total     INTEGER NOT NULL DEFAULT 0,
    chunks_failed    INTEGER NOT NULL DEFAULT 0,
    facts_created    INTEGER NOT NULL DEFAULT 0,
    started_at       TEXT NOT NULL,
    finished_at      TEXT
);
CREATE INDEX ix_extraction_runs_case ON extraction_runs(case_id);

-- Candidate and confirmed financial facts. Append-only: value columns can
-- never be updated (see trigger). Corrections insert a new row that
-- supersedes the old one. (document_id / extraction_run_id may only change
-- to NULL through ON DELETE SET NULL when a document is purged.)
CREATE TABLE facts (
    id                  TEXT PRIMARY KEY,
    case_id             TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    field_key           TEXT NOT NULL,
    period_id           TEXT REFERENCES financial_periods(id) ON DELETE CASCADE,
    entity_ref          TEXT REFERENCES parties(id) ON DELETE CASCADE,
    item_label          TEXT NOT NULL DEFAULT '',
    value_numeric       TEXT,
    value_text          TEXT,
    unit                TEXT NOT NULL,
    currency            TEXT,
    raw_value_text      TEXT,
    scale               TEXT,
    source_type         TEXT NOT NULL CHECK (source_type IN ('extracted','user_entered','user_corrected')),
    document_id         TEXT REFERENCES documents(id) ON DELETE SET NULL,
    page_number         INTEGER,
    excerpt             TEXT,
    extraction_run_id   TEXT REFERENCES extraction_runs(id) ON DELETE SET NULL,
    model_name          TEXT,
    model_confidence    REAL CHECK (model_confidence IS NULL OR (model_confidence >= 0 AND model_confidence <= 1)),
    evidence_check_json TEXT,
    status              TEXT NOT NULL DEFAULT 'candidate'
                        CHECK (status IN ('candidate','verified','rejected','superseded')),
    supersedes_fact_id  TEXT REFERENCES facts(id) ON DELETE SET NULL,
    created_at          TEXT NOT NULL,
    created_by          TEXT NOT NULL DEFAULT 'local_user'
);
CREATE INDEX ix_facts_lookup ON facts(case_id, field_key, period_id, status);
CREATE INDEX ix_facts_document ON facts(document_id);

CREATE TRIGGER trg_facts_values_immutable
BEFORE UPDATE OF case_id, field_key, period_id, entity_ref, item_label, value_numeric, value_text, unit,
                 currency, raw_value_text, scale, source_type, page_number, excerpt,
                 model_name, model_confidence, created_at
ON facts
BEGIN
    SELECT RAISE(ABORT, 'facts are append-only: insert a correction instead of updating values');
END;

CREATE VIEW verified_facts AS
    SELECT * FROM facts WHERE status = 'verified';

CREATE TABLE fact_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id     TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL CHECK (event_type IN ('created','verified','unverified','rejected','superseded','corrected','restored')),
    from_status TEXT,
    to_status   TEXT,
    note        TEXT,
    actor       TEXT NOT NULL DEFAULT 'local_user',
    occurred_at TEXT NOT NULL
);
CREATE INDEX ix_fact_events_fact ON fact_events(fact_id);

CREATE TRIGGER trg_fact_events_no_update BEFORE UPDATE ON fact_events
BEGIN
    SELECT RAISE(ABORT, 'fact_events is append-only');
END;

CREATE TABLE user_corrections (
    id                TEXT PRIMARY KEY,
    case_id           TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    original_fact_id  TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    corrected_fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    previous_value    TEXT,
    new_value         TEXT,
    reason            TEXT NOT NULL,
    actor             TEXT NOT NULL DEFAULT 'local_user',
    corrected_at      TEXT NOT NULL
);

CREATE TABLE existing_debts (
    id                   TEXT PRIMARY KEY,
    case_id              TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    creditor             TEXT NOT NULL,
    description          TEXT,
    debt_type            TEXT NOT NULL DEFAULT 'term_loan',
    original_amount      TEXT,
    current_balance      TEXT,
    interest_rate        TEXT,
    payment_amount       TEXT,
    payment_frequency    TEXT NOT NULL DEFAULT 'monthly' CHECK (payment_frequency IN ('monthly','quarterly','semiannual','annual')),
    maturity_date        TEXT,
    to_be_refinanced     INTEGER NOT NULL DEFAULT 0 CHECK (to_be_refinanced IN (0,1)),
    document_id          TEXT REFERENCES documents(id) ON DELETE SET NULL,
    page_number          INTEGER,
    verification_status  TEXT NOT NULL DEFAULT 'unverified' CHECK (verification_status IN ('unverified','verified','rejected')),
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX ix_existing_debts_case ON existing_debts(case_id);

CREATE TABLE collateral (
    id                 TEXT PRIMARY KEY,
    case_id            TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    description        TEXT NOT NULL,
    collateral_type    TEXT NOT NULL CHECK (collateral_type IN
                       ('real_estate','equipment','vehicles','accounts_receivable','inventory','cash_securities','business_assets','other')),
    lien_position      INTEGER NOT NULL DEFAULT 1 CHECK (lien_position >= 1),
    prior_lien_balance TEXT NOT NULL DEFAULT '0',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX ix_collateral_case ON collateral(case_id);

CREATE TABLE collateral_valuations (
    id                  TEXT PRIMARY KEY,
    collateral_id       TEXT NOT NULL REFERENCES collateral(id) ON DELETE CASCADE,
    value               TEXT NOT NULL,
    valuation_date      TEXT,
    valuation_method    TEXT NOT NULL DEFAULT 'user_estimate' CHECK (valuation_method IN
                        ('appraisal','broker_opinion','invoice','book_value','tax_assessment','user_estimate','other')),
    document_id         TEXT REFERENCES documents(id) ON DELETE SET NULL,
    page_number         INTEGER,
    verification_status TEXT NOT NULL DEFAULT 'unverified' CHECK (verification_status IN ('unverified','verified','rejected')),
    created_at          TEXT NOT NULL
);
CREATE INDEX ix_valuations_collateral ON collateral_valuations(collateral_id);

CREATE TABLE policies (
    id                 TEXT PRIMARY KEY,
    policy_key         TEXT NOT NULL,
    version            TEXT NOT NULL,
    name               TEXT NOT NULL,
    module_key         TEXT NOT NULL,
    loan_types_json    TEXT NOT NULL,
    effective_date     TEXT NOT NULL,
    illustrative       INTEGER NOT NULL CHECK (illustrative IN (0,1)),
    source_description TEXT NOT NULL,
    source_file        TEXT,
    content_sha256     TEXT NOT NULL,
    content_json       TEXT NOT NULL,
    loaded_at          TEXT NOT NULL,
    UNIQUE (policy_key, version)
);

CREATE TRIGGER trg_policies_immutable BEFORE UPDATE OF content_sha256, content_json, version, policy_key ON policies
BEGIN
    SELECT RAISE(ABORT, 'policy versions are immutable: publish a new version');
END;

CREATE TABLE scenarios (
    id               TEXT PRIMARY KEY,
    case_id          TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    description      TEXT,
    adjustments_json TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX ix_scenarios_case ON scenarios(case_id);

CREATE TABLE underwriting_runs (
    id              TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    run_number      INTEGER NOT NULL,
    policy_id       TEXT REFERENCES policies(id) ON DELETE RESTRICT,
    scenario_id     TEXT REFERENCES scenarios(id) ON DELETE SET NULL,
    scenario_name   TEXT,
    mode            TEXT NOT NULL CHECK (mode IN ('verified_only','include_unverified')),
    base_period_label TEXT,
    engine_version  TEXT NOT NULL,
    inputs_json     TEXT NOT NULL,
    inputs_sha256   TEXT NOT NULL,
    overall_status  TEXT NOT NULL,
    summary_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (case_id, run_number)
);
CREATE INDEX ix_runs_case ON underwriting_runs(case_id);

CREATE TRIGGER trg_runs_immutable BEFORE UPDATE ON underwriting_runs
WHEN NEW.scenario_id IS OLD.scenario_id
BEGIN
    SELECT RAISE(ABORT, 'underwriting runs are immutable');
END;

CREATE TABLE run_metrics (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL REFERENCES underwriting_runs(id) ON DELETE CASCADE,
    metric_key     TEXT NOT NULL,
    label          TEXT NOT NULL,
    value          TEXT,
    unit           TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('computed','not_computable','not_meaningful','not_applicable')),
    formula        TEXT NOT NULL,
    inputs_json    TEXT NOT NULL,
    explanation    TEXT NOT NULL,
    flags_json     TEXT NOT NULL DEFAULT '[]',
    UNIQUE (run_id, metric_key)
);

CREATE TABLE rule_evaluations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL REFERENCES underwriting_runs(id) ON DELETE CASCADE,
    rule_id           TEXT NOT NULL,
    rule_version      INTEGER NOT NULL,
    description       TEXT NOT NULL,
    severity          TEXT NOT NULL,
    result            TEXT NOT NULL CHECK (result IN ('PASS','FAIL','UNKNOWN','NOT_APPLICABLE','REVIEW_REQUIRED')),
    indicative_result TEXT,
    subject           TEXT,
    observed_value    TEXT,
    operator          TEXT,
    threshold         TEXT,
    explanation       TEXT NOT NULL,
    requires_review   INTEGER NOT NULL CHECK (requires_review IN (0,1)),
    policy_source     TEXT
);
CREATE INDEX ix_rule_evals_run ON rule_evaluations(run_id);

CREATE TABLE evidence_refs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             TEXT NOT NULL REFERENCES underwriting_runs(id) ON DELETE CASCADE,
    metric_key         TEXT,
    rule_evaluation_id INTEGER REFERENCES rule_evaluations(id) ON DELETE CASCADE,
    fact_id            TEXT REFERENCES facts(id) ON DELETE SET NULL,
    document_id        TEXT REFERENCES documents(id) ON DELETE SET NULL,
    page_number        INTEGER,
    description        TEXT NOT NULL
);
CREATE INDEX ix_evidence_run ON evidence_refs(run_id);

CREATE TABLE run_findings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL REFERENCES underwriting_runs(id) ON DELETE CASCADE,
    finding_type  TEXT NOT NULL CHECK (finding_type IN ('missing','contradiction','unverified','stale','data_quality','assumption')),
    severity      TEXT NOT NULL,
    field_key     TEXT,
    period_label  TEXT,
    message       TEXT NOT NULL,
    details_json  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX ix_findings_run ON run_findings(run_id);

CREATE TABLE reports (
    id                 TEXT PRIMARY KEY,
    case_id            TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    run_id             TEXT NOT NULL REFERENCES underwriting_runs(id) ON DELETE CASCADE,
    version            INTEGER NOT NULL,
    format             TEXT NOT NULL CHECK (format IN ('html','pdf')),
    file_name          TEXT NOT NULL,
    content_sha256     TEXT NOT NULL,
    includes_narrative INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    UNIQUE (case_id, version, format)
);

CREATE TABLE audit_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at  TEXT NOT NULL,
    case_id      TEXT REFERENCES loan_cases(id) ON DELETE SET NULL,
    entity_type  TEXT NOT NULL,
    entity_id    TEXT,
    action       TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    actor        TEXT NOT NULL DEFAULT 'local_user'
);
CREATE INDEX ix_audit_case ON audit_events(case_id, occurred_at);

CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON audit_events
WHEN NEW.case_id IS OLD.case_id
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;
