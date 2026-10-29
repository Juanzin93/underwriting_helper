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

## Milestone 5 — structuring and brokerage ✅

| # | Delivered | Verified by (`tests/unit/test_m5_brokerage.py`) |
|---|---|---|
| 5.1 | Submission package manifests (each file with SHA-256, memo version, figures from the run with its input hash, matching result and criteria version, disclosure text) stored immutably before export; ZIP export only after a named confirmation; every file re-hashed at export (changed file → refused); deterministic archives; nothing sent anywhere | `test_package_manifest_*`, `test_package_export_*` |
| 5.2 | Submissions per lender program with history (draft → submitted → under review → term sheet → accepted / declined / withdrawn, reasons required); first submission moves a deal in STRUCTURING to LENDER_SUBMISSION (recorded) | `test_submission_lifecycle_and_deal_sync`, `test_submission_does_not_skip_deal_stages` |
| 5.3 | Offers stored exactly as received (terms immutable); side-by-side comparison through the structuring engine (payment, year-1 debt service, DSCR, balloon, interest, fees, LTV/LTC); accepting needs a named person + confirmation and moves the deal to APPROVED; expired offers cannot be accepted | `test_offers_compare_with_structuring_engine`, `test_accepting_an_offer_*` |
| 5.4 | Fee agreements (broker fee, lender compensation, referral split; flat / % of funded / % of broker gross), never edited (void + replace); configurable disclosure (`UWH_BROKER_DISCLOSURE`); funded amount from confirmed funding → accepted offer → requested (labelled estimate) | `test_fee_agreement_validation`, `test_fee_math_to_the_cent_*` |
| 5.5 | Append-only commission ledger (expected in/out, received, paid out, reversals), receivable/payable per deal, aging buckets, all-deals Commissions page | `test_ledger_*`, `test_recalculate_after_reversing_expected` |

## Milestone 6 — lender approval to funding on deals (next)

| # | Item | Depends on | Acceptance criteria |
|---|---|---|---|
| 6.1 | **Lender conditions from the accepted offer.** Record the lender's commitment conditions on the deal (reuse the condition engine: requested → received → satisfied / waived by a named person), linked to deal documents | M5 | Conditions immutable history; waivers need a reason and a name; deal cannot move to CLOSING with required open conditions (override with documented reason) |
| 6.2 | **Due-diligence tracker on deals.** Third-party items (appraisal, environmental, title, insurance) with vendor, ordered/received dates and status; reuse tasks for follow-ups | 6.1, M2 tasks | Overdue items surface on the Tasks page; every status change audited |
| 6.3 | **Closing and funding on deals.** Closing date, final terms (from the accepted offer, differences flagged), typed funding confirmation (who, when, amount); deal → FUNDED only through this confirmation; links to the underwriting file's own funding when present | 6.1 | FUNDED requires the confirmation record; funded amount differences from the offer are flagged; commission expectations use the confirmed amount |
| 6.4 | **Deal-level checklist view.** One page showing what blocks the next deal status (documents, conditions, due diligence, tasks) with links | 6.1–6.3 | Each blocker links to where it is resolved; hard vs soft blockers distinguished as in the file lifecycle |

## Backlog (planned, not started)

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
