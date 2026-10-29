# Roadmap

Rules for every milestone: local-only by default, forward-only migrations with an upgrade test from the previous
release, temporary DB/files in tests, no network in unit tests, humans make every approval/decline/funding decision,
Python (not the LLM) does all arithmetic.

## Milestone 1 — foundation ✅
Settings, SQLite strategy, migration 0007, organizations/contacts/deals, audit, document ingestion service, health
endpoint, JSON error format, `/api/v1`, navigation, lock file, Windows guide. See ARCHITECTURE_ASSESSMENT.md.

## Milestone 2 — shared CRM, borrower, deal, document and task workflows ✅

| # | Delivered | Verified by (`tests/unit/test_m2_crm_workflows.py` unless noted) |
|---|---|---|
| 2.1 | Deal statuses CREDIT_REVIEW and SERVICING; recorded case↔deal sync; guards against contradictory terminal states; link an existing file to a deal (docs/LIFECYCLE.md) | `test_case_funding_syncs_*`, `test_case_decline_*`, `test_deal_withdrawal_*`, `test_guards_*`, `test_link_existing_*` |
| 2.2 | `borrowers.organization_id` (backfilled by migration 0008, case-insensitive, no duplicates), new/renamed borrowers linked automatically, guarantors copied from deals with contact links, owner → contact link on the file page; purging a file removes an organization auto-created for it if nothing else uses it | `test_upgrade_from_schema7_*`, `test_case_borrower_*`, `test_purge_removes_auto_created_org_*`, real M1 database upgraded |
| 2.3 | Actor = Windows account name (or `UWH_ACTOR_NAME`) on audit events, status history, tasks and notes | `test_actor_*` |
| 2.4 | Tasks (deal/contact/organization), My tasks page with overdue/today/week views, API | `test_tasks_*` |
| 2.5 | Document checklist per deal from versioned `crm/checklists/deal_documents.yaml`; requested → received → accepted/rejected, waivers with reasons, named reviewer; files flow into the linked underwriting file | `test_document_request_*`, `test_checklist_*` |
| 2.6 | Append-only notes/calls/meetings/emails with corrections; deal timeline (activities, status, events, tasks); note text never in the audit log or log file | `test_activities_*`, `test_deal_timeline_*` |
| 2.7 | Search, type filters, archived toggle, paging, archive/restore for deals, contacts, organizations; CSV import with preview, idempotent, all-or-nothing (1,000 rows well under 5 s) | `test_csv_import_*`, `test_search_*` |
| 2.8 | API for all of the above; offline OpenAPI schema at `/api/v1/openapi.json` (no CDN UI); HTML routes delegate to services | `test_api_endpoints_end_to_end`, `test_offline_openapi_schema`, `test_web_pages_and_forms` |

Also fixed: CRM templates were missing from package data (non-editable installs); flash messages were lost on
redirects with a `#fragment`; wide tables caused horizontal scrolling on phones.

## Milestone 3 — evidence-backed extraction and business/CRE underwriting (next)

| # | Item | Depends on | Acceptance criteria |
|---|---|---|---|
| 3.1 | **Pinned extraction runs.** Record model name + digest (from local Ollama `/api/show`), prompt template hash, document SHA-256 and page range on every extraction run; show them on facts | M2 | Re-running extraction on the same inputs shows identical provenance; facts link to run + page; no network beyond loopback |
| 3.2 | **Evidence view per metric.** From any metric, list the verified facts, their pages and the formula (existing data, new page) | 3.1 | Every computed metric in a run links to ≥ 1 verified fact or is marked user-entered |
| 3.3 | **CRE module.** Today NOI, property DSCR and debt yield are computed from property totals inside the commercial module. Split out a CRE module with unit/lease-level rent roll, T-12 lines, taxes, insurance, reserves, and add cap-rate value, DSCR at a stressed rate, breakeven occupancy and lease-rollover exposure; illustrative CRE policy v2 | — | Metrics computed in Decimal with tests against hand-worked examples; missing inputs → UNKNOWN, never zero |
| 3.4 | **Rent-roll and T-12 import** from CSV/XLSX-exported CSV with column mapping preview (same pattern as the CRM import) | 3.3 | Totals reconcile to the source; mismatches block "verified" |
| 3.5 | **Versioned rule sets with effective dates** across both modules; run records rule-set version; diff view between versions | 3.3 | Old runs reproduce exactly with their original rule versions |
| 3.6 | **Credit memo per deal** combining the file's run, deal summary, sponsors/guarantors (contacts) and document checklist status | 2.x, 3.2 | Memo cites run number and checklist version; regenerating an old memo gives the same text |

Exit criteria: a CRE deal can go from rent roll + T-12 to a reproducible run and memo with every number traced to a page or
a named person's entry.

## Backlog (planned, not started)

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
Implemented on deals since M2 (CREDIT_REVIEW optional for brokered deals).
