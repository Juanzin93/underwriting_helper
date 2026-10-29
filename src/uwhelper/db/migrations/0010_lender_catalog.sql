-- 0010_lender_catalog (Milestone 4): lender programs, versioned criteria, saved match runs.

-- A lender program belongs to a lender organization. Basic eligibility (deal types, products, states, amount range)
-- is part of the program; detailed credit criteria live in immutable, versioned criteria sets.
CREATE TABLE lender_programs (
    id                  TEXT PRIMARY KEY,
    organization_id     TEXT NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    name                TEXT NOT NULL,
    deal_types_json     TEXT NOT NULL DEFAULT '[]',
    product_types_json  TEXT NOT NULL DEFAULT '[]',
    states_json         TEXT NOT NULL DEFAULT '[]',
    min_amount          TEXT,
    max_amount          TEXT,
    active              INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    notes               TEXT,
    created_by          TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_lender_programs_name ON lender_programs(organization_id, name COLLATE NOCASE);

-- Criteria versions are immutable: a change creates a new version. verified_at is the date the criteria were last
-- confirmed with the lender (drives freshness warnings); source says where they came from.
CREATE TABLE program_criteria_versions (
    id               TEXT PRIMARY KEY,
    program_id       TEXT NOT NULL REFERENCES lender_programs(id) ON DELETE RESTRICT,
    version          INTEGER NOT NULL,
    criteria_json    TEXT NOT NULL,
    criteria_sha256  TEXT NOT NULL,
    source           TEXT NOT NULL,
    verified_at      TEXT NOT NULL,
    verified_by      TEXT NOT NULL,
    effective_date   TEXT NOT NULL,
    notes            TEXT,
    created_by       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (program_id, version)
);

CREATE TRIGGER trg_criteria_versions_no_update BEFORE UPDATE ON program_criteria_versions
BEGIN
    SELECT RAISE(ABORT, 'criteria versions are immutable; create a new version');
END;

CREATE TRIGGER trg_criteria_versions_no_delete BEFORE DELETE ON program_criteria_versions
BEGIN
    SELECT RAISE(ABORT, 'criteria versions are kept for history');
END;

-- A saved, immutable matching result for a deal (cites the run and every criteria version used).
CREATE TABLE match_runs (
    id                  TEXT PRIMARY KEY,
    deal_id             TEXT NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    case_id             TEXT REFERENCES loan_cases(id) ON DELETE SET NULL,
    run_id              TEXT REFERENCES underwriting_runs(id) ON DELETE SET NULL,
    run_number          INTEGER,
    run_current         INTEGER CHECK (run_current IN (0,1)),
    max_age_days        INTEGER NOT NULL,
    results_json        TEXT NOT NULL,
    results_sha256      TEXT NOT NULL,
    created_by          TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX ix_match_runs_deal ON match_runs(deal_id, created_at);

CREATE TRIGGER trg_match_runs_immutable BEFORE UPDATE ON match_runs
WHEN NOT ((NEW.case_id IS NULL OR NEW.run_id IS NULL) AND NEW.results_json IS OLD.results_json)
BEGIN
    SELECT RAISE(ABORT, 'match results are immutable');
END;
