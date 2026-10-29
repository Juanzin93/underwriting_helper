-- 0003_memos_structures: credit memo versions and loan-structure scenarios.

CREATE TABLE credit_memos (
    id                 TEXT PRIMARY KEY,
    case_id            TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    run_id             TEXT NOT NULL REFERENCES underwriting_runs(id) ON DELETE RESTRICT,
    version            INTEGER NOT NULL,
    generator_version  TEXT NOT NULL,
    context_json       TEXT NOT NULL,          -- complete render input; re-rendering reproduces the memo
    context_sha256     TEXT NOT NULL,
    html_file          TEXT NOT NULL,
    html_sha256        TEXT NOT NULL,
    pdf_file           TEXT,
    pdf_sha256         TEXT,
    opinion_code       TEXT NOT NULL,
    includes_narrative INTEGER NOT NULL DEFAULT 0 CHECK (includes_narrative IN (0,1)),
    created_at         TEXT NOT NULL,
    UNIQUE (case_id, version)
);
CREATE INDEX ix_memos_case ON credit_memos(case_id);

CREATE TRIGGER trg_memos_immutable BEFORE UPDATE ON credit_memos
BEGIN
    SELECT RAISE(ABORT, 'credit memo versions are immutable');
END;

-- Simulated loan structures. Inputs and the calculation snapshot are immutable;
-- only the label status (draft/proposed/archived) may change.
CREATE TABLE loan_structures (
    id             TEXT PRIMARY KEY,
    case_id        TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    description    TEXT,
    is_baseline    INTEGER NOT NULL DEFAULT 0 CHECK (is_baseline IN (0,1)),
    status         TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','proposed','archived')),
    inputs_json    TEXT NOT NULL,
    inputs_sha256  TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX ix_structures_case ON loan_structures(case_id);

CREATE TRIGGER trg_structures_inputs_immutable BEFORE UPDATE OF inputs_json, inputs_sha256, case_id, is_baseline, created_at
ON loan_structures
BEGIN
    SELECT RAISE(ABORT, 'structure inputs are immutable: create a new structure');
END;

CREATE TABLE structure_calculations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    structure_id    TEXT NOT NULL REFERENCES loan_structures(id) ON DELETE CASCADE,
    run_id          TEXT REFERENCES underwriting_runs(id) ON DELETE SET NULL,
    policy_id       TEXT REFERENCES policies(id) ON DELETE SET NULL,
    engine_version  TEXT NOT NULL,
    results_json    TEXT NOT NULL,
    computed_at     TEXT NOT NULL
);
CREATE INDEX ix_structure_calcs ON structure_calculations(structure_id, id);

CREATE TRIGGER trg_structure_calcs_no_update BEFORE UPDATE ON structure_calculations
WHEN NEW.run_id IS OLD.run_id AND NEW.policy_id IS OLD.policy_id
BEGIN
    SELECT RAISE(ABORT, 'structure calculations are immutable');
END;
