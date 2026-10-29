-- 0011_brokerage (Milestone 5): submission packages, submissions, offers, fee agreements, commission ledger.

-- A package manifest lists exactly what would be shared with one lender program. It is stored (immutable)
-- before anything is exported; exports are recorded separately after the user confirms the contents.
CREATE TABLE submission_packages (
    id                TEXT PRIMARY KEY,
    deal_id           TEXT NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    program_id        TEXT NOT NULL REFERENCES lender_programs(id) ON DELETE RESTRICT,
    match_run_id      TEXT REFERENCES match_runs(id) ON DELETE SET NULL,
    version           INTEGER NOT NULL,
    manifest_json     TEXT NOT NULL,
    manifest_sha256   TEXT NOT NULL,
    created_by        TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    UNIQUE (deal_id, program_id, version)
);
CREATE TRIGGER trg_packages_immutable BEFORE UPDATE ON submission_packages
WHEN NOT (NEW.match_run_id IS NULL AND OLD.match_run_id IS NOT NULL AND NEW.manifest_json IS OLD.manifest_json)
BEGIN
    SELECT RAISE(ABORT, 'submission packages are immutable; build a new one');
END;

CREATE TABLE package_exports (
    id            TEXT PRIMARY KEY,
    package_id    TEXT NOT NULL REFERENCES submission_packages(id) ON DELETE CASCADE,
    confirmed_by  TEXT NOT NULL,
    file_name     TEXT NOT NULL,
    zip_sha256    TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    exported_at   TEXT NOT NULL
);
CREATE TRIGGER trg_package_exports_no_update BEFORE UPDATE ON package_exports
BEGIN
    SELECT RAISE(ABORT, 'package exports are append-only');
END;

-- A submission is the broker's record of presenting a deal to one lender program (made outside the app).
CREATE TABLE submissions (
    id                 TEXT PRIMARY KEY,
    deal_id            TEXT NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    program_id         TEXT NOT NULL REFERENCES lender_programs(id) ON DELETE RESTRICT,
    package_id         TEXT REFERENCES submission_packages(id) ON DELETE SET NULL,
    lender_contact_id  TEXT REFERENCES contacts(id) ON DELETE SET NULL,
    status             TEXT NOT NULL DEFAULT 'draft' CHECK (status IN
                       ('draft','submitted','under_review','term_sheet','accepted','declined','withdrawn')),
    submitted_on       TEXT,
    submitted_via      TEXT CHECK (submitted_via IS NULL OR submitted_via IN ('email','portal','in_person','phone','other')),
    status_changed_at  TEXT NOT NULL,
    notes              TEXT,
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE (deal_id, program_id)
);
CREATE INDEX ix_submissions_deal ON submissions(deal_id);

CREATE TABLE submission_status_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id  TEXT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    from_status    TEXT,
    to_status      TEXT NOT NULL,
    reason         TEXT,
    actor          TEXT NOT NULL,
    occurred_at    TEXT NOT NULL
);
CREATE TRIGGER trg_submission_history_no_update BEFORE UPDATE ON submission_status_history
BEGIN
    SELECT RAISE(ABORT, 'submission history is append-only');
END;

-- Offers / term sheets as received. Terms are immutable; only the decision fields change.
CREATE TABLE offers (
    id                  TEXT PRIMARY KEY,
    submission_id       TEXT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    deal_id             TEXT NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    received_on         TEXT NOT NULL,
    amount              TEXT NOT NULL CHECK (CAST(amount AS REAL) > 0),
    annual_rate         TEXT NOT NULL,
    rate_type           TEXT NOT NULL DEFAULT 'fixed' CHECK (rate_type IN ('fixed','variable')),
    index_name          TEXT,
    spread              TEXT,
    term_months         INTEGER NOT NULL CHECK (term_months > 0),
    amortization_months INTEGER NOT NULL CHECK (amortization_months > 0),
    interest_only_months INTEGER NOT NULL DEFAULT 0,
    payment_frequency   TEXT NOT NULL DEFAULT 'monthly',
    origination_fee     TEXT NOT NULL DEFAULT '0',
    other_fees          TEXT NOT NULL DEFAULT '0',
    prepayment_terms    TEXT,
    covenants           TEXT,
    expires_on          TEXT,
    status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','accepted','declined','expired')),
    decided_by          TEXT,
    decided_at          TEXT,
    decision_reason     TEXT,
    notes               TEXT,
    created_by          TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX ix_offers_deal ON offers(deal_id);
CREATE TRIGGER trg_offers_terms_immutable BEFORE UPDATE ON offers
WHEN NEW.amount IS NOT OLD.amount OR NEW.annual_rate IS NOT OLD.annual_rate OR NEW.term_months IS NOT OLD.term_months
  OR NEW.amortization_months IS NOT OLD.amortization_months OR NEW.interest_only_months IS NOT OLD.interest_only_months
  OR NEW.origination_fee IS NOT OLD.origination_fee OR NEW.other_fees IS NOT OLD.other_fees
  OR NEW.received_on IS NOT OLD.received_on OR NEW.submission_id IS NOT OLD.submission_id
  OR (OLD.status <> 'open' AND NEW.status IS NOT OLD.status)
BEGIN
    SELECT RAISE(ABORT, 'offer terms are immutable and a decided offer cannot change; record a new offer');
END;

-- Fee agreements: what the broker earns and what it owes referral partners. Never edited; voided and replaced.
CREATE TABLE fee_agreements (
    id                 TEXT PRIMARY KEY,
    deal_id            TEXT NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    kind               TEXT NOT NULL CHECK (kind IN ('broker_fee','lender_compensation','referral_split')),
    basis              TEXT NOT NULL CHECK (basis IN ('flat','percent_of_funded','percent_of_broker_gross')),
    value              TEXT NOT NULL,
    party_organization_id TEXT REFERENCES organizations(id) ON DELETE RESTRICT,
    party_contact_id   TEXT REFERENCES contacts(id) ON DELETE RESTRICT,
    description        TEXT,
    voided             INTEGER NOT NULL DEFAULT 0 CHECK (voided IN (0,1)),
    void_reason        TEXT,
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    CHECK (kind <> 'referral_split' OR party_organization_id IS NOT NULL OR party_contact_id IS NOT NULL),
    CHECK (basis <> 'percent_of_broker_gross' OR kind = 'referral_split')
);
CREATE INDEX ix_fee_agreements_deal ON fee_agreements(deal_id);
CREATE TRIGGER trg_fee_agreements_void_only BEFORE UPDATE ON fee_agreements
WHEN NEW.kind IS NOT OLD.kind OR NEW.basis IS NOT OLD.basis OR NEW.value IS NOT OLD.value
  OR NEW.deal_id IS NOT OLD.deal_id OR OLD.voided = 1
BEGIN
    SELECT RAISE(ABORT, 'fee agreements cannot be edited; void and add a new one');
END;

-- Commission ledger (append-only). Amounts are positive; the entry type gives the direction:
-- expected_in (broker is owed), received (broker was paid), expected_out (broker owes a partner), paid_out.
-- A correction is a new entry that reverses the one it corrects.
CREATE TABLE commission_entries (
    id                 TEXT PRIMARY KEY,
    deal_id            TEXT NOT NULL REFERENCES deals(id) ON DELETE RESTRICT,
    fee_agreement_id   TEXT REFERENCES fee_agreements(id) ON DELETE RESTRICT,
    entry_type         TEXT NOT NULL CHECK (entry_type IN ('expected_in','received','expected_out','paid_out')),
    amount             TEXT NOT NULL CHECK (CAST(amount AS REAL) > 0),
    occurred_on        TEXT NOT NULL,
    counterparty       TEXT,
    reference          TEXT,
    basis_json         TEXT,
    reverses_entry_id  TEXT REFERENCES commission_entries(id) ON DELETE RESTRICT,
    note               TEXT,
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL
);
CREATE INDEX ix_commission_entries_deal ON commission_entries(deal_id);
CREATE TRIGGER trg_commission_no_update BEFORE UPDATE ON commission_entries
BEGIN
    SELECT RAISE(ABORT, 'commission entries are append-only; add a reversing entry');
END;
CREATE TRIGGER trg_commission_no_delete BEFORE DELETE ON commission_entries
BEGIN
    SELECT RAISE(ABORT, 'commission entries are append-only');
END;
