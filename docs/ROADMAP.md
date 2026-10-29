# Roadmap

Rules for every milestone: local-only by default, forward-only migrations with an upgrade test from the previous
release, temporary DB/files in tests, no network in unit tests, humans make every approval/decline/funding decision,
Python (not the LLM) does all arithmetic.

## Milestone 1 — foundation ✅ (this release)
Settings, SQLite strategy, migration 0007, organizations/contacts/deals, audit, document ingestion service, health
endpoint, JSON error format, `/api/v1`, navigation, lock file, Windows guide. See ARCHITECTURE_ASSESSMENT.md.

## Milestone 2 — shared CRM, borrower, deal, document and task workflows (next)

Ordered by dependency. Each item lands as its own commit with tests.

| # | Item | Depends on | Acceptance criteria |
|---|---|---|---|
| 2.1 | **One pipeline truth.** Deal status owns the brokerage pipeline; the linked loan case keeps the underwriting lifecycle. Add CREDIT_REVIEW and SERVICING to deal statuses (migration recreates the CHECK). Sync rules: case FUNDED ⇒ deal FUNDED (system, with the case's funding confirmation as authorization); deal WITHDRAWN/CANCELLED/DECLINED ⇒ prompt to close the case; no silent two-way sync | M1 | Table of mappings in LIFECYCLE.md; tests for every sync rule; a deal and its case can never show contradictory terminal states |
| 2.2 | **Borrower entity.** Link `borrowers` (per case) to `organizations` (`borrowers.organization_id`), backfill by exact name match into existing orgs, else create; guarantors/owners (`parties`) link to `contacts` | 2.1 | Upgrade test on a schema-7 DB with cases: every case borrower has an org, counts unchanged, no duplicates for same name+type; case pages show the org link |
| 2.3 | **Actor identity.** Record the Windows user (`getpass.getuser()`) as `actor` on audit events and status history alongside typed "authorized by" | M1 | Audit rows show the OS user; tests mock the user; no network calls |
| 2.4 | **Tasks and follow-ups.** `tasks` table (deal/contact/org nullable FKs, title, due date, status, priority, completed_at), "My tasks" dashboard, overdue badges, API endpoints | 2.3 | CRUD + completion audited; overdue query tested across date boundaries; tasks survive deal archive |
| 2.5 | **Document requests per deal.** Needed-items checklist by product type (from YAML, versioned like policies), states requested → received → accepted/rejected, linked to `deal_documents`; carried into the underwriting file | 2.1 | Checklist generated per product; upload marks "received"; acceptance needs a person; carry-over test |
| 2.6 | **Activity timeline and notes.** Append-only `activities` (note, call, email-logged-manually, meeting) per deal/contact/org; timeline merges activities, status history and audit | 2.3 | Notes immutable (corrections append); timeline ordering test; note text never written to the log file |
| 2.7 | **Search, filters, pagination, archive** for deals/contacts/orgs in UI and API; CSV import of contacts/organizations (local file, preview + confirm) | 2.2 | Import is idempotent on email/name; invalid rows reported, none partially imported; 1k-row import < 5 s |
| 2.8 | **Service-layer cleanup** of HTML routes touched above; API tests for every endpoint; offline OpenAPI JSON at `/api/v1/openapi.json` (no CDN UI) | — | Route handlers contain no SQL in touched areas; schema served without external assets |

Exit criteria: all tests pass; upgrade from a schema-7 database with data verified; a broker can take a deal from
intake to lender submission with contacts, tasks, document requests and a linked underwriting file without leaving the app.

## Backlog (planned, not started)

* **M3 — Evidence-backed extraction and underwriting.** Extraction runs pinned to document hashes and model/prompt
  versions; business underwriting (existing) and **CRE** module (rent roll, T-12, NOI, cap rate, debt yield, DSCR at
  stressed rates); versioned rule sets with effective dates; credit memo per deal. *Accept:* every metric traces to
  verified facts with page references; rerunning an old run reproduces it.
* **M4 — Lender and product catalog.** `lenders`, `lender_programs`, `program_criteria` with **versions** and
  `verified_at`/source fields; manual entry and local file import; explainable matching (each criterion PASS/FAIL/
  UNKNOWN with reason); stale-criteria warnings. *Accept:* matches cite the criteria version; stale data flagged.
* **M5 — Structuring and brokerage.** Structures per deal (reuse structuring engine), lender submissions (package
  manifest of exactly which files are shared, recorded before export), offers/term sheets compared side by side,
  referral partners, commission and fee schedules with splits (Decimal). *Accept:* submission records what was shared;
  commission math tested; nothing is sent anywhere automatically.
* **M6 — Credit review to funding.** Map existing decisions/conditions/closing/funding onto deals; lender conditions
  from offers; due-diligence trackers. *Accept:* APPROVED/FUNDED only via human authorization; full audit.
* **M7 — Servicing and portfolio.** Reuse `servicing/`; portfolio by lender/referral source; covenant and
  delinquency monitoring across deals; daily accrual and payoff quotes.
* **M8 — Opt-in integrations and direct lending.** Adapters behind `uwhelper.integrations` (each disabled by default,
  separately enabled, confirmation listing shared data); direct-lending module (own capital, funding sources,
  approval authority matrix). Requires authentication and a licensing review first.

## Status lifecycle (target)

```
INTAKE → UNDERWRITING → CREDIT_REVIEW → STRUCTURING → LENDER_SUBMISSION → APPROVED* → DUE_DILIGENCE → CLOSING → FUNDED* → SERVICING
any open status → ON_HOLD (resume returns to the prior status) | WITHDRAWN | CANCELLED | DECLINED*
* human authorization + confirmation required
```
M1 implements this on deals except CREDIT_REVIEW and SERVICING (added in M2.1).
