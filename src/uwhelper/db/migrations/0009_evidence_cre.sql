-- 0009_evidence_cre (Milestone 3): extraction provenance, rent rolls, property statement (T-12) imports, deal memos.

-- 1. Extraction runs record exactly what was sent to the local model.
-- prompt_sha256: prompt version + system prompt + JSON schema (allowed fields); document_sha256: document hash at
-- run time; pages_json: page numbers sent; options_json: temperature, seed policy, context size, chunking.
ALTER TABLE extraction_runs ADD COLUMN prompt_sha256 TEXT;
ALTER TABLE extraction_runs ADD COLUMN document_sha256 TEXT;
ALTER TABLE extraction_runs ADD COLUMN pages_json TEXT;
ALTER TABLE extraction_runs ADD COLUMN options_json TEXT;
ALTER TABLE extraction_runs ADD COLUMN ollama_version TEXT;

-- 2. Rent rolls. A rent roll is a dated set of units; it becomes usable as VERIFIED only after its totals reconcile
--    to the totals stated on the source document and a named person confirms it. Verified rolls are immutable;
--    a newer verified roll supersedes the previous one.
CREATE TABLE rent_rolls (
    id                    TEXT PRIMARY KEY,
    case_id               TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    as_of_date            TEXT NOT NULL,
    document_id           TEXT REFERENCES documents(id) ON DELETE SET NULL,
    source_name           TEXT,
    stated_annual_rent    TEXT,
    stated_unit_count     INTEGER,
    computed_annual_rent  TEXT NOT NULL,
    computed_unit_count   INTEGER NOT NULL,
    reconciled            INTEGER NOT NULL DEFAULT 0 CHECK (reconciled IN (0,1)),
    status                TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','verified','superseded','rejected')),
    verified_by           TEXT,
    verified_at           TEXT,
    notes                 TEXT,
    created_by            TEXT NOT NULL,
    created_at            TEXT NOT NULL
);
CREATE INDEX ix_rent_rolls_case ON rent_rolls(case_id, status, as_of_date);

CREATE TABLE rent_roll_units (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    rent_roll_id        TEXT NOT NULL REFERENCES rent_rolls(id) ON DELETE CASCADE,
    line_number         INTEGER NOT NULL,
    unit                TEXT NOT NULL,
    tenant              TEXT,
    square_feet         TEXT,
    occupied            INTEGER NOT NULL CHECK (occupied IN (0,1)),
    annual_rent         TEXT NOT NULL DEFAULT '0',
    market_annual_rent  TEXT,
    lease_start         TEXT,
    lease_end           TEXT,
    month_to_month      INTEGER NOT NULL DEFAULT 0 CHECK (month_to_month IN (0,1))
);
CREATE INDEX ix_rent_roll_units_roll ON rent_roll_units(rent_roll_id);

CREATE TRIGGER trg_rent_roll_units_locked_insert BEFORE INSERT ON rent_roll_units
WHEN (SELECT status FROM rent_rolls WHERE id = NEW.rent_roll_id) <> 'draft'
BEGIN
    SELECT RAISE(ABORT, 'rent roll is no longer a draft');
END;

CREATE TRIGGER trg_rent_roll_units_no_update BEFORE UPDATE ON rent_roll_units
BEGIN
    SELECT RAISE(ABORT, 'rent roll units are immutable; import a new rent roll');
END;

CREATE TRIGGER trg_rent_rolls_verified_immutable BEFORE UPDATE ON rent_rolls
WHEN OLD.status IN ('verified','superseded','rejected')
     AND NOT (OLD.status = 'verified' AND NEW.status = 'superseded'
              AND NEW.as_of_date IS OLD.as_of_date AND NEW.computed_annual_rent IS OLD.computed_annual_rent
              AND NEW.verified_by IS OLD.verified_by AND NEW.document_id IS OLD.document_id)
     AND NOT (NEW.document_id IS NULL AND OLD.document_id IS NOT NULL AND NEW.status IS OLD.status
              AND NEW.computed_annual_rent IS OLD.computed_annual_rent AND NEW.verified_by IS OLD.verified_by)
BEGIN
    SELECT RAISE(ABORT, 'a verified rent roll is immutable (it can only be superseded)');
END;

-- 3. Property operating statement (T-12) imports: the mapped lines are kept for traceability; the totals become
--    user-entered facts (verified only when the stated totals reconcile and a named person confirms).
CREATE TABLE property_statement_imports (
    id                     TEXT PRIMARY KEY,
    case_id                TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    period_end             TEXT NOT NULL,
    period_months          INTEGER NOT NULL CHECK (period_months BETWEEN 1 AND 12),
    document_id            TEXT REFERENCES documents(id) ON DELETE SET NULL,
    source_name            TEXT,
    lines_json             TEXT NOT NULL,
    totals_json            TEXT NOT NULL,
    stated_total_income    TEXT,
    stated_total_expenses  TEXT,
    reconciled             INTEGER NOT NULL CHECK (reconciled IN (0,1)),
    verified               INTEGER NOT NULL CHECK (verified IN (0,1)),
    confirmed_by           TEXT,
    fact_ids_json          TEXT NOT NULL DEFAULT '[]',
    created_by             TEXT NOT NULL,
    created_at             TEXT NOT NULL
);
CREATE INDEX ix_property_statement_imports_case ON property_statement_imports(case_id);

CREATE TRIGGER trg_property_statement_imports_immutable BEFORE UPDATE ON property_statement_imports
WHEN NOT (NEW.document_id IS NULL AND OLD.document_id IS NOT NULL AND NEW.lines_json IS OLD.lines_json
          AND NEW.totals_json IS OLD.totals_json AND NEW.verified IS OLD.verified)
BEGIN
    SELECT RAISE(ABORT, 'property statement imports are immutable');
END;

-- 4. Credit memos per deal (versioned, reproducible from the stored context).
CREATE TABLE deal_memos (
    id              TEXT PRIMARY KEY,
    deal_id         TEXT NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    case_id         TEXT REFERENCES loan_cases(id) ON DELETE SET NULL,
    run_id          TEXT REFERENCES underwriting_runs(id) ON DELETE SET NULL,
    context_json    TEXT NOT NULL,
    context_sha256  TEXT NOT NULL,
    html            TEXT NOT NULL,
    html_sha256     TEXT NOT NULL,
    template_version TEXT NOT NULL,
    generated_by    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (deal_id, version)
);

CREATE TRIGGER trg_deal_memos_immutable BEFORE UPDATE ON deal_memos
WHEN NOT (NEW.case_id IS NULL AND OLD.case_id IS NOT NULL AND NEW.context_json IS OLD.context_json)
     AND NOT (NEW.run_id IS NULL AND OLD.run_id IS NOT NULL AND NEW.context_json IS OLD.context_json)
BEGIN
    SELECT RAISE(ABORT, 'deal memos are immutable; generate a new version');
END;
