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

## Milestone 4 — lender and product catalog, explainable matching ✅

| # | Delivered | Verified by (`tests/unit/test_m4_lenders.py`) |
|---|---|---|
| 4.1 | Lender programs (lender organization, deal types, products, states, amount range, active flag) and immutable, versioned criteria sets with source, confirmed-on date, confirmed-by and effective date; a version is added only when something changed | `test_program_rules`, `test_versions_are_immutable_*` |
| 4.2 | Criteria language (`metric:`, `input:`, `terms:`, `case:`, `deal:` subjects; >=, >, <=, <, ==, !=, in, not_in, is_known, is_unknown; optional "preferred" criteria) validated against the metric and field catalogs; YAML catalog import (preview, all-or-nothing, idempotent), criteria CSV upload, `uwhelper import-lenders` CLI; fictional sample catalog | `test_criterion_validation`, `test_yaml_import_*`, `test_criteria_csv` |
| 4.3 | Explainable matching per deal: every check PASS / FAIL / UNKNOWN with observed value, requirement and reason; grouped meets-all / possible / not-a-fit and ordered only by visible counts; UNKNOWN never counted as PASS; saved immutably citing the run number and each criteria version | `test_matching_explains_every_check`, `test_unknown_is_never_a_pass`, `test_inactive_programs_left_out_*` |
| 4.4 | Freshness: criteria confirmed more than `UWH_CRITERIA_MAX_AGE_DAYS` (default 180) ago are flagged on matches, the lender list and program pages | `test_freshness_boundary` (180 vs 181 days) |
| 4.5 | Lender organization page lists programs (submissions placeholder for M5); Lenders menu; API endpoints; offline OpenAPI | `test_lender_pages_and_forms`, `test_lender_api` |

## Milestone 5 — structuring and brokerage (next)

| # | Item | Depends on | Acceptance criteria |
|---|---|---|---|
| 5.1 | **Submission packages.** From a saved matching, choose a program; build a package manifest listing exactly which deal documents (by SHA-256), memo version and figures would be shared; the manifest is stored before any export | M4 | Manifest immutable; export writes a local ZIP only after the user confirms the listed contents; nothing is sent anywhere by the app |
| 5.2 | **Submission tracking.** Per program: draft → submitted (date, contact, how) → under review → term sheet / declined / withdrawn; deal status moves to LENDER_SUBMISSION when the first submission is recorded | 5.1, M2.1 | Status history per submission; deal sync recorded; reasons required for declines/withdrawals |
| 5.3 | **Offers / term sheets.** Enter lender terms (amount, rate, index/spread, amortization, term, fees, prepayment, covenants); side-by-side comparison using the structuring engine (payment, DSCR at offered terms, total cost) | 5.2, structuring | All math Decimal and reproducible; an accepted offer becomes the deal's approved terms only with a named person's confirmation |
| 5.4 | **Referral partners and fees.** Referral source on deals (exists), fee agreements (flat / % of funded amount / split), broker fee and lender-paid compensation schedules | M2 | Fee math tested to the cent, including splits; disclosures text configurable |
| 5.5 | **Commission ledger.** Expected vs received commissions per funded deal; aging of unpaid commissions | 5.4, funding | Ledger entries immutable (corrections append); totals reconcile in tests |

## Backlog (planned, not started)

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
