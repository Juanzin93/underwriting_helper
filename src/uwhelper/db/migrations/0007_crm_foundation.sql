-- 0007_crm_foundation: shared entities for loan-officer / brokerage workflows.
-- A deal is the relationship-level opportunity (brokerage pipeline). An underwriting file
-- (loan_cases) can be linked to a deal; existing loan cases are not modified.

CREATE TABLE organizations (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    org_type      TEXT NOT NULL CHECK (org_type IN
                  ('borrower_business','lender','referral_partner','brokerage','law_firm','title_company',
                   'accounting_firm','insurance_agency','appraiser','other')),
    website       TEXT,
    phone         TEXT,
    email         TEXT,
    address_line1 TEXT,
    address_line2 TEXT,
    city          TEXT,
    state         TEXT,
    postal_code   TEXT,
    country       TEXT NOT NULL DEFAULT 'US',
    industry      TEXT,
    notes         TEXT,
    archived      INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0,1)),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_organizations_name_type ON organizations(name COLLATE NOCASE, org_type);
CREATE INDEX ix_organizations_type ON organizations(org_type, archived);

CREATE TABLE contacts (
    id            TEXT PRIMARY KEY,
    first_name    TEXT NOT NULL,
    last_name     TEXT NOT NULL,
    email         TEXT,
    phone         TEXT,
    title         TEXT,
    contact_type  TEXT NOT NULL DEFAULT 'other' CHECK (contact_type IN
                  ('borrower','guarantor','referral_partner','lender_contact','attorney','accountant','other')),
    notes         TEXT,
    archived      INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0,1)),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX ix_contacts_name ON contacts(last_name COLLATE NOCASE, first_name COLLATE NOCASE);
CREATE UNIQUE INDEX ux_contacts_email ON contacts(email COLLATE NOCASE) WHERE email IS NOT NULL;

-- Contact <-> organization affiliations (a person can belong to several organizations).
CREATE TABLE organization_contacts (
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    contact_id      TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    role            TEXT NOT NULL DEFAULT 'member',
    is_primary      INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1)),
    created_at      TEXT NOT NULL,
    PRIMARY KEY (organization_id, contact_id)
);
CREATE INDEX ix_org_contacts_contact ON organization_contacts(contact_id);

CREATE TABLE deals (
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
                               ('INTAKE','UNDERWRITING','STRUCTURING','LENDER_SUBMISSION','APPROVED','DUE_DILIGENCE',
                                'CLOSING','FUNDED','DECLINED','WITHDRAWN','ON_HOLD','CANCELLED')),
    status_changed_at          TEXT NOT NULL,
    hold_return_status         TEXT,
    target_close_date          TEXT,
    notes                      TEXT,
    archived                   INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0,1)),
    created_at                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL
);
CREATE INDEX ix_deals_status ON deals(status, archived);
CREATE INDEX ix_deals_borrower ON deals(borrower_organization_id);

CREATE TABLE deal_status_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id       TEXT NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    from_status   TEXT,
    to_status     TEXT NOT NULL,
    reason        TEXT NOT NULL,
    authorized_by TEXT,
    actor         TEXT NOT NULL DEFAULT 'local_user',
    occurred_at   TEXT NOT NULL
);
CREATE INDEX ix_deal_status_history ON deal_status_history(deal_id, id);

CREATE TRIGGER trg_deal_status_history_no_update BEFORE UPDATE ON deal_status_history
BEGIN
    SELECT RAISE(ABORT, 'deal_status_history is append-only');
END;

CREATE TABLE deal_contacts (
    deal_id     TEXT NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    contact_id  TEXT NOT NULL REFERENCES contacts(id) ON DELETE RESTRICT,
    role        TEXT NOT NULL CHECK (role IN
                ('borrower','guarantor','referral_partner','lender_contact','attorney','accountant','other')),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (deal_id, contact_id, role)
);
CREATE INDEX ix_deal_contacts_contact ON deal_contacts(contact_id);

-- Deal-level documents reuse the hash-named document store (documents table).
CREATE TABLE deal_documents (
    deal_id     TEXT NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    doc_type    TEXT NOT NULL DEFAULT 'other',
    added_at    TEXT NOT NULL,
    PRIMARY KEY (deal_id, document_id)
);
CREATE INDEX ix_deal_documents_doc ON deal_documents(document_id);
