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

## Milestone 3 — evidence-backed extraction and business/CRE underwriting ✅

| # | Delivered | Verified by (`tests/unit/test_m3_evidence_cre.py` unless noted) |
|---|---|---|
| 3.1 | Extraction runs record model + digest, Ollama version, prompt fingerprint (SHA-256 of prompt version, system prompt and output schema), document SHA-256, pages sent and generation options; shown on the Documents page and in evidence trails, with "document changed since" / "prompt changed since" checks | `test_extraction_run_records_full_provenance_*`, `test_prompt_fingerprint_*` |
| 3.2 | Evidence trail per metric (inputs classified as fact / record / assumption / derived; facts with page, excerpt, extraction provenance and review history) and a traceability check per run | `test_every_metric_of_sample_run_is_traced`, `test_untraced_input_is_reported` |
| 3.3 | CRE: rent roll storage, stressed property DSCR, break-even occupancy, income-approach value and LTV on it, physical and economic occupancy, 12/24-month rollover, WALT; illustrative policy `demo_cre` v1.2.0 (effective 2026-11-01) with cap rate and stress parameters and four new rules | `test_cre_metrics_hand_worked` (every value checked against hand-worked Decimal arithmetic), `test_verified_only_ignores_draft_roll_*`, `test_non_cre_runs_unchanged_*` |
| 3.4 | Rent-roll CSV import (preview → draft → reconcile to printed totals within $1 → named verification; verified rolls immutable, newer ones supersede) and T-12 import (keyword mapping the user can change, section detection, monthly columns summed, reconciliation of EGI and expenses; totals saved as verified facts only when reconciled and confirmed by a named person; lines kept immutably) | `test_rent_roll_*`, `test_t12_*` |
| 3.5 | Runs store the rule-evaluation context; any run can be re-checked from its stored inputs and exact policy version (metrics always, rules for runs saved since M3); policy version diff page | `test_saved_run_reproduces_exactly`, `test_reproduction_detects_*`, `test_policy_versions_*`; 5/5 runs of a Milestone 2 database reproduced after upgrade |
| 3.6 | Versioned, immutable credit memo per deal (deal, borrower, people, checklist with version, linked run with policy version and input hash, exceptions, decision); verify re-renders byte-for-byte; HTML/PDF | `test_deal_memo_*` |

Design decision: CRE is **not** a separately registered lending module. The single `commercial` module is wired
through extraction, the resolver, policies and cases; splitting it would touch all of them for no user benefit. CRE
has its own package (`uwhelper/cre/`: rent rolls, T-12) and calculations (`metrics/cre.py`) and stays a loan type of
the commercial module, sharing fields and extraction.

## Milestone 4 — lender and product catalog, explainable matching (next)

| # | Item | Depends on | Acceptance criteria |
|---|---|---|---|
| 4.1 | **Lenders and programs.** `lender_programs` (lender organization, product types, deal types, states, min/max amount, notes), `program_criteria_versions` (immutable criteria sets with `source`, `verified_at`, `verified_by`, effective date) | M2 orgs | A new criteria version never alters an older one; editing creates a version; audit on each |
| 4.2 | **Criteria language.** Reuse the policy condition subjects (`metric:`, `terms:`, `case:`, `deal:`) with operators; validation against the metric catalog | 4.1, M3 | Unknown metric or field refused with a clear message; YAML/CSV import with preview |
| 4.3 | **Explainable matching.** For a deal's latest current run: per program, each criterion PASS / FAIL / UNKNOWN with observed value and reason; ranking only by count of PASS/UNKNOWN, never a hidden score | 4.2 | Every match row cites the criteria version and run number; UNKNOWN never treated as PASS |
| 4.4 | **Freshness.** Criteria older than a configurable age (default 180 days) are flagged on match results and the lender page | 4.1 | Stale flag tested across the boundary date |
| 4.5 | **Lender CRM view.** Lender organization page shows programs, contacts, past submissions (placeholder until M5) | 4.1 | Page renders with and without programs |

## Backlog (planned, not started)

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
