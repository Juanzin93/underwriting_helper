-- 0002_lifecycle: persistent loan workflow stage and an append-only transition history.
-- Existing cases start in INTAKE; one history row records that assignment.

ALTER TABLE loan_cases ADD COLUMN stage TEXT NOT NULL DEFAULT 'INTAKE'
    CHECK (stage IN ('INTAKE','UNDERWRITING','CREDIT_REVIEW','STRUCTURING','DUE_DILIGENCE','CLOSING','FUNDED',
                     'SERVICING','PAID_OFF','WITHDRAWN','DECLINED','ON_HOLD','CANCELLED'));
ALTER TABLE loan_cases ADD COLUMN stage_entered_at TEXT;
ALTER TABLE loan_cases ADD COLUMN hold_return_stage TEXT;

CREATE TABLE workflow_transitions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id          TEXT NOT NULL REFERENCES loan_cases(id) ON DELETE CASCADE,
    from_stage       TEXT,
    to_stage         TEXT NOT NULL,
    reason           TEXT NOT NULL,
    is_override      INTEGER NOT NULL DEFAULT 0 CHECK (is_override IN (0,1)),
    blockers_json    TEXT NOT NULL DEFAULT '[]',
    actor            TEXT NOT NULL DEFAULT 'local_user',
    occurred_at      TEXT NOT NULL
);
CREATE INDEX ix_transitions_case ON workflow_transitions(case_id, id);

CREATE TRIGGER trg_transitions_no_update BEFORE UPDATE ON workflow_transitions
BEGIN
    SELECT RAISE(ABORT, 'workflow_transitions is append-only');
END;

UPDATE loan_cases SET stage_entered_at = COALESCE(stage_entered_at, created_at);

INSERT INTO workflow_transitions(case_id, from_stage, to_stage, reason, occurred_at)
    SELECT id, NULL, 'INTAKE', 'Initial stage assigned by migration 0002 (case existed before lifecycle tracking)',
           strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
    FROM loan_cases;
