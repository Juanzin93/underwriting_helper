-- uwh:foreign_keys=off
-- 0008_crm_workflows (Milestone 2): one pipeline truth, borrower entities, tasks, document requests, activities.
-- Runs with foreign-key enforcement off so the deals table can be rebuilt without firing ON DELETE CASCADE on
-- deal_status_history / deal_contacts / deal_documents; the runner verifies PRAGMA foreign_key_check before commit.

-- 1. Deals gain CREDIT_REVIEW and SERVICING (rebuild; SQLite cannot alter a CHECK constraint).
CREATE TABLE deals_new (
    id                         TEXT PRIMARY KEY,
    deal_number                TEXT NOT NULL UNIQUE,
    name                       TEXT NOT NULL,
    deal_type                  TEXT NOT NULL CHECK (deal_type IN ('small_business','cre')),
    product_type               TEXT NOT NULL,
    loan_purpose               TEXT,
    requested_amount           TEXT CHECK (requested_amount IS NULL OR CAST(requested_amount AS REAL) > 0),
    currency                   TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    collateral_summary         TEXT,
    borrower_organization_id   TEXT REFERENCES organizations(id) ON DELETE RESTRICT,
    primary_contact_id         TEXT REFERENCES contacts(id) ON DELETE RESTRICT,
    referral_source_contact_id TEXT REFERENCES contacts(id) ON DELETE RESTRICT,
    loan_case_id               TEXT UNIQUE REFERENCES loan_cases(id) ON DELETE SET NULL,
    status                     TEXT NOT NULL DEFAULT 'INTAKE' CHECK (status IN
                               ('INTAKE','UNDERWRITING','CREDIT_REVIEW','STRUCTURING','LENDER_SUBMISSION','APPROVED',
                                'DUE_DILIGENCE','CLOSING','FUNDED','SERVICING','DECLINED','WITHDRAWN','ON_HOLD',
                                'CANCELLED')),
    status_changed_at          TEXT NOT NULL,
    hold_return_status         TEXT,
    target_close_date          TEXT,
    notes                      TEXT,
    archived                   INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0,1)),
    created_at                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL
);
INSERT INTO deals_new (id, deal_number, name, deal_type, product_type, loan_purpose, requested_amount, currency,
                       collateral_summary, borrower_organization_id, primary_contact_id, referral_source_contact_id,
                       loan_case_id, status, status_changed_at, hold_return_status, target_close_date, notes, archived,
                       created_at, updated_at)
SELECT id, deal_number, name, deal_type, product_type, loan_purpose, requested_amount, currency,
       collateral_summary, borrower_organization_id, primary_contact_id, referral_source_contact_id,
       loan_case_id, status, status_changed_at, hold_return_status, target_close_date, notes, archived,
       created_at, updated_at
FROM deals;
DROP TABLE deals;
ALTER TABLE deals_new RENAME TO deals;
CREATE INDEX ix_deals_status ON deals(status, archived);
CREATE INDEX ix_deals_borrower ON deals(borrower_organization_id);

-- 2. Underwriting-file borrowers link to borrower organizations; owners/guarantors can link to contacts.
ALTER TABLE borrowers ADD COLUMN organization_id TEXT REFERENCES organizations(id) ON DELETE SET NULL;
ALTER TABLE parties ADD COLUMN contact_id TEXT REFERENCES contacts(id) ON DELETE SET NULL;
CREATE INDEX ix_borrowers_org ON borrowers(organization_id);
CREATE INDEX ix_parties_contact ON parties(contact_id);

-- Backfill: one borrower organization per distinct legal name (case-insensitive, trimmed); reuse existing ones.
INSERT INTO organizations (id, name, org_type, industry, country, notes, archived, created_at, updated_at)
SELECT lower(hex(randomblob(16))), MIN(trim(b.legal_name)), 'borrower_business', MAX(b.industry), 'US',
       'Created from existing underwriting files by migration 0008.', 0, MIN(b.created_at), MIN(b.created_at)
FROM borrowers b
WHERE trim(b.legal_name) <> ''
  AND NOT EXISTS (SELECT 1 FROM organizations o
                  WHERE o.org_type = 'borrower_business' AND o.name = trim(b.legal_name) COLLATE NOCASE)
GROUP BY trim(b.legal_name) COLLATE NOCASE;

UPDATE borrowers SET organization_id = (
    SELECT o.id FROM organizations o
    WHERE o.org_type = 'borrower_business' AND o.name = trim(borrowers.legal_name) COLLATE NOCASE)
WHERE organization_id IS NULL;

-- 3. Tasks and follow-ups.
CREATE TABLE tasks (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT,
    deal_id         TEXT REFERENCES deals(id) ON DELETE CASCADE,
    contact_id      TEXT REFERENCES contacts(id) ON DELETE CASCADE,
    organization_id TEXT REFERENCES organizations(id) ON DELETE CASCADE,
    due_date        TEXT,
    priority        TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low','normal','high')),
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','cancelled')),
    assignee        TEXT,
    created_by      TEXT NOT NULL,
    completed_at    TEXT,
    completed_by    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX ix_tasks_status_due ON tasks(status, due_date);
CREATE INDEX ix_tasks_deal ON tasks(deal_id);
CREATE INDEX ix_tasks_contact ON tasks(contact_id);
CREATE INDEX ix_tasks_org ON tasks(organization_id);

-- 4. Document requests (needed-items checklist) per deal.
CREATE TABLE document_requests (
    id                TEXT PRIMARY KEY,
    deal_id           TEXT NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    item_key          TEXT NOT NULL,
    label             TEXT NOT NULL,
    doc_type          TEXT NOT NULL DEFAULT 'other',
    required          INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0,1)),
    checklist_key     TEXT,
    checklist_version TEXT,
    status            TEXT NOT NULL DEFAULT 'requested'
                      CHECK (status IN ('requested','received','accepted','rejected','waived')),
    document_id       TEXT REFERENCES documents(id) ON DELETE RESTRICT,
    note              TEXT,
    reviewed_by       TEXT,
    reviewed_at       TEXT,
    requested_at      TEXT NOT NULL,
    received_at       TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (deal_id, item_key)
);
CREATE INDEX ix_document_requests_deal ON document_requests(deal_id, status);
CREATE INDEX ix_document_requests_doc ON document_requests(document_id);

-- 5. Activities (notes, calls, meetings, manually logged emails). Append-only: corrections are new rows.
CREATE TABLE activities (
    id                   TEXT PRIMARY KEY,
    deal_id              TEXT REFERENCES deals(id),
    contact_id           TEXT REFERENCES contacts(id),
    organization_id      TEXT REFERENCES organizations(id),
    kind                 TEXT NOT NULL CHECK (kind IN ('note','call','email','meeting','other')),
    subject              TEXT NOT NULL,
    body                 TEXT,
    occurred_at          TEXT NOT NULL,
    actor                TEXT NOT NULL,
    corrects_activity_id TEXT REFERENCES activities(id),
    created_at           TEXT NOT NULL,
    CHECK (deal_id IS NOT NULL OR contact_id IS NOT NULL OR organization_id IS NOT NULL)
);
CREATE INDEX ix_activities_deal ON activities(deal_id, occurred_at);
CREATE INDEX ix_activities_contact ON activities(contact_id, occurred_at);
CREATE INDEX ix_activities_org ON activities(organization_id, occurred_at);

CREATE TRIGGER trg_activities_no_update BEFORE UPDATE ON activities
BEGIN
    SELECT RAISE(ABORT, 'activities are append-only; add a correction instead');
END;

CREATE TRIGGER trg_activities_no_delete BEFORE DELETE ON activities
BEGIN
    SELECT RAISE(ABORT, 'activities are append-only');
END;
