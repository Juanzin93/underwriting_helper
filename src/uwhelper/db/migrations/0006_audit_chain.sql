-- 0006_audit_chain: tamper-EVIDENT (not tamper-proof) hash chain over audit_events.
-- Each event stores prev_hash and event_hash = SHA-256(prev_hash + canonical event fields),
-- computed by the application. Existing rows are back-filled once at startup.
-- When a case is purged for privacy, the ids/hashes (no content) of its removed events are
-- kept in audit_purged_hashes so the remaining chain can still be verified.

ALTER TABLE audit_events ADD COLUMN prev_hash TEXT;
ALTER TABLE audit_events ADD COLUMN event_hash TEXT;

CREATE TABLE audit_purged_hashes (
    event_id     INTEGER PRIMARY KEY,
    prev_hash    TEXT,
    event_hash   TEXT,
    purged_at    TEXT NOT NULL
);

DROP TRIGGER trg_audit_no_update;

-- Allowed updates: one-time hash back-fill of rows that have none, and ON DELETE SET NULL of case_id.
CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON audit_events
WHEN NOT (
    (OLD.event_hash IS NULL AND NEW.action IS OLD.action AND NEW.details_json IS OLD.details_json
         AND NEW.occurred_at IS OLD.occurred_at AND NEW.entity_id IS OLD.entity_id AND NEW.case_id IS OLD.case_id)
    OR (NEW.case_id IS NULL AND OLD.case_id IS NOT NULL AND NEW.event_hash IS OLD.event_hash
         AND NEW.prev_hash IS OLD.prev_hash AND NEW.action IS OLD.action AND NEW.details_json IS OLD.details_json)
)
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

CREATE TRIGGER trg_audit_purged_no_update BEFORE UPDATE ON audit_purged_hashes
BEGIN
    SELECT RAISE(ABORT, 'audit_purged_hashes is append-only');
END;
