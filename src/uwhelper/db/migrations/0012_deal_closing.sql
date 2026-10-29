-- 0012_deal_closing (Milestone 6): lender conditions, due diligence, closing and funding on deals.

CREATE TABLE deal_conditions (
    id              TEXT PRIMARY KEY,
    deal_id         TEXT NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    offer_id        TEXT REFERENCES offers(id) ON DELETE SET NULL,
    source          TEXT NOT NULL DEFAULT 'lender' CHECK (source IN ('lender','broker','other')),
    description     TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'other',
    phase           TEXT NOT NULL DEFAULT 'closing' CHECK (phase IN ('due_diligence','closing','post_closing')),
    required        INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0,1)),
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','received','satisfied','waived')),
    responsible     TEXT,
    due_on          TEXT,
    document_id     TEXT REFERENCES documents(id) ON DELETE SET NULL,
    resolved_by     TEXT,
    resolved_at     TEXT,
    resolution_note TEXT,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX ix_deal_conditions_deal ON deal_conditions(deal_id, status);

CREATE TABLE deal_condition_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id  TEXT NOT NULL REFERENCES deal_conditions(id) ON DELETE CASCADE,
    from_status   TEXT,
    to_status     TEXT NOT NULL,
    note          TEXT,
    actor         TEXT NOT NULL,
    occurred_at   TEXT NOT NULL
);
CREATE TRIGGER trg_deal_condition_events_no_update BEFORE UPDATE ON deal_condition_events
BEGIN
    SELECT RAISE(ABORT, 'condition history is append-only');
END;

CREATE TABLE dd_items (
    id                      TEXT PRIMARY KEY,
    deal_id                 TEXT NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    item_type               TEXT NOT NULL CHECK (item_type IN ('appraisal','environmental','title','insurance','survey',
                                                               'ucc_lien_search','background_check','flood','zoning',
                                                               'other')),
    description             TEXT,
    vendor_organization_id  TEXT REFERENCES organizations(id) ON DELETE SET NULL,
    vendor_name             TEXT,
    status                  TEXT NOT NULL DEFAULT 'not_ordered'
                            CHECK (status IN ('not_ordered','ordered','received','reviewed','waived')),
    required                INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0,1)),
    ordered_on              TEXT,
    due_on                  TEXT,
    received_on             TEXT,
    document_id             TEXT REFERENCES documents(id) ON DELETE SET NULL,
    reviewed_by             TEXT,
    note                    TEXT,
    created_by              TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);
CREATE INDEX ix_dd_items_deal ON dd_items(deal_id, status);

CREATE TABLE dd_item_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dd_item_id   TEXT NOT NULL REFERENCES dd_items(id) ON DELETE CASCADE,
    from_status  TEXT,
    to_status    TEXT NOT NULL,
    note         TEXT,
    actor        TEXT NOT NULL,
    occurred_at  TEXT NOT NULL
);
CREATE TRIGGER trg_dd_item_events_no_update BEFORE UPDATE ON dd_item_events
BEGIN
    SELECT RAISE(ABORT, 'due-diligence history is append-only');
END;

-- Closing record (editable until the deal is funded) and the funding confirmation (immutable).
CREATE TABLE deal_closings (
    deal_id                    TEXT PRIMARY KEY REFERENCES deals(id) ON DELETE CASCADE,
    closing_date               TEXT,
    final_amount               TEXT,
    final_annual_rate          TEXT,
    final_term_months          INTEGER,
    final_amortization_months  INTEGER,
    notes                      TEXT,
    updated_by                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL
);
CREATE TRIGGER trg_deal_closings_locked BEFORE UPDATE ON deal_closings
WHEN EXISTS (SELECT 1 FROM deal_fundings f WHERE f.deal_id = OLD.deal_id)
BEGIN
    SELECT RAISE(ABORT, 'the closing record is locked after funding');
END;

CREATE TABLE deal_fundings (
    id                 TEXT PRIMARY KEY,
    deal_id            TEXT NOT NULL UNIQUE REFERENCES deals(id) ON DELETE CASCADE,
    offer_id           TEXT REFERENCES offers(id) ON DELETE SET NULL,
    funded_amount      TEXT NOT NULL CHECK (CAST(funded_amount AS REAL) > 0),
    funding_date       TEXT NOT NULL,
    confirmed_by       TEXT NOT NULL,
    confirmation_text  TEXT NOT NULL,
    differences_json   TEXT NOT NULL DEFAULT '[]',
    override_reason    TEXT,
    notes              TEXT,
    created_at         TEXT NOT NULL
);
CREATE TRIGGER trg_deal_fundings_immutable BEFORE UPDATE ON deal_fundings
WHEN NOT (NEW.offer_id IS NULL AND OLD.offer_id IS NOT NULL AND NEW.funded_amount IS OLD.funded_amount)
BEGIN
    SELECT RAISE(ABORT, 'funding confirmations are immutable');
END;
