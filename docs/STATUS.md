# Milestone status, known limitations, unfinished features

Phase 2 test run: **222 passed, 2 skipped** (`pytest`, Python 3.13 on Linux, Tesseract 5.3.4). The 2 skipped tests are
the opt-in live-Ollama tests (no Ollama/GPU in the build environment). The PowerShell scripts were written for
Windows but have **not been executed** on Windows by the author.

## Phase 1 — underwriting workbench (milestones 1–5) ✅
Foundation, local AI extraction (mock-tested), documents/OCR/evidence, full deterministic analysis, reports and
hardening. See git history and the test files `test_metrics.py`, `test_policy.py`, `test_documents.py`, `test_llm.py`,
`test_underwriting_runs.py`, `test_web.py`, etc.

## Phase 2 — full loan lifecycle

| Step | Status | Verified by |
|---|---|---|
| Inspect project, run baseline tests | ✅ 160 passed before changes | — |
| Repair foundations | ✅ policy registration bypassed the audit chain (fixed); policy resolution now honours effective dates | `test_memo_migrations.py` |
| Lifecycle stages + migrations 0002–0006 | ✅ | `test_lifecycle.py`, migration upgrade test |
| Credit memo generator (HTML/PDF, versions, reproducibility) | ✅ | `test_memo_migrations.py` |
| Loan structuring simulator | ✅ | `test_structuring.py` |
| Due diligence & conditions | ✅ | `test_conditions_closing.py` |
| Human credit decisions, approved/executed terms, closing, funding | ✅ | `test_conditions_closing.py`, `test_lifecycle.py` |
| Servicing: schedules, payments, CSV, delinquency, modifications | ✅ | `test_servicing.py` |
| Covenants, monitoring alerts, portfolio | ✅ | `test_servicing.py` |
| UI integration (all pages) | ✅ | `test_web_lifecycle.py` drives the whole lifecycle through the web forms; screenshots reviewed in a real browser |
| Audit chain, security checks | ✅ | tamper and purge tests; CSRF/Host/Origin tests from phase 1 still pass |
| Documentation | ✅ | README, LIFECYCLE.md, SERVICING.md, ARCHITECTURE.md, SECURITY.md |

## Known limitations
* **Single local user.** "Decided by", "approved by" and "confirmed by" are typed names, not authenticated identities.
  Anyone using your Windows account can act as anyone. This is not a multi-user lending platform.
* **Strict re-underwriting.** Any material change after a decision (including a new collateral valuation received
  during due diligence) makes the run stale and requires a new run and a new human decision before advancing.
* **Servicing math simplifications:** nominal periodic rates (no actual/360 or daily accrual), schedule-based interest
  even after prepayments, no rate floors/caps, no automatic late fees, no escrow, no payoff quotes with per-diem
  interest, no arrears capitalization in modifications, variable rates handled by recording rate-change modifications.
* Payment dates are calendar dates without business-day adjustment.
* Covenant tests use annual verified underwriting runs or manual values; there is no automatic interim-statement
  spreading for quarterly covenants.
* Credit memos list strengths/mitigants only from rule results (≥ 20 % margin); industry and market content appears
  only as labelled analyst commentary. Template changes after an upgrade may change re-rendered HTML (the stored
  context is the reproducibility anchor).
* Closing checklists are illustrative policy content, not legal advice; no document drafting is provided.
* Phase-1 limitations still apply: extraction quality depends on the local model; no built-in encryption at rest;
  one borrower entity per case; contradiction tolerance fixed at $1.00.

## Unfinished / future features
* SBA 7(a)/504 and private/hard-money modules with sourced, versioned program requirements.
* Daily-accrual servicing, payoff statements, escrow, automated late-fee rules, arrears capitalization.
* Borrowing-base monitoring for revolving lines; AR/AP aging analytics.
* Quarterly covenant spreading from interim statements; multi-year trend charts.
* Policy editor UI (policies are YAML files).
* Packaged Windows installer.

## Platform Milestone 1 — foundation ✅

Latest test run: **255 passed, 2 skipped** (`python -m pytest -q`, Python 3.13, Linux; also with
`-W error::ResourceWarning`, and in a fresh venv installed from `requirements.lock`).

| Item | Verified by |
|---|---|
| Dependency cleanup + lock | fresh-venv install from the lock, `pip check`, full suite |
| Central settings (.env, DB/doc paths, integration flags) | `test_m1_foundation.py` settings tests |
| SQLite strategy (FKs, busy timeout, transactions, shutdown checkpoint) | DB + lifespan tests; real server log |
| Migration 0007 + upgrade | test upgrade from schema 6 with data; real phase-2 demo DB upgraded with all counts preserved and audit chain intact |
| Organizations, contacts, deals, links | relationship, API CRUD, workflow tests |
| Audit events | create/update/transition audit tests; chain verification |
| Document storage | dedupe, size/type limits, deal attachment tests |
| Health endpoint | ok/degraded/503 tests; offline server run (degraded, DB ok) |
| Error format + validation | API error tests (404/422/403, request ID, no input echo) |
| Navigation | CRM pages render; existing page tests unchanged |
| Offline operation | network-blocking test guard; server run in a network namespace with no external route |

Not verified here: the PowerShell scripts on real Windows, and the 2 live Ollama tests. See
[ARCHITECTURE_ASSESSMENT.md](ARCHITECTURE_ASSESSMENT.md) for risks and [ROADMAP.md](ROADMAP.md) for Milestone 2.

## Platform Milestone 2 — CRM, borrower, deal, document and task workflows ✅

Latest test run: **285 passed, 2 skipped** (`python -m pytest -q`; also with `-W error::ResourceWarning`). New tests:
`tests/unit/test_m2_crm_workflows.py` (30). Details per item: [ROADMAP.md](ROADMAP.md#milestone-2--shared-crm-borrower-deal-document-and-task-workflows-).

Also verified by hand:
* A database created by the Milestone 1 release (lifecycle demo + a deal with history) upgraded to schema 8: all row
  counts preserved, every borrower linked to an organization, integrity ok, no foreign-key violations, audit chain intact.
* The new pages (deal checklist/tasks/notes/timeline, tasks, file page links, import) in Chromium at desktop and phone
  width (no horizontal scrolling at 390 px).

Known limitations added in M2:
* Sync between a deal and its file covers funding, servicing, decline, withdrawal and cancellation only; other stages
  are independent by design (the deal page shows notices when they diverge).
* The actor is the Windows account name (or `UWH_ACTOR_NAME`): attribution, not authentication.
* The document checklist is an illustrative YAML file; edit it to match your practice.
* CSV import is limited to 1 MB / 5,000 rows per file and never updates existing records.

Still not verified here: the PowerShell scripts on real Windows and the 2 live Ollama tests.

## Platform Milestone 3 — evidence-backed extraction and business/CRE underwriting ✅

Latest test run: **306 passed, 2 skipped** (`python -m pytest -q`; also with `-W error::ResourceWarning`). New tests:
`tests/unit/test_m3_evidence_cre.py` (21). Details per item: [ROADMAP.md](ROADMAP.md).

Also verified by hand:
* A database created by the Milestone 2 release (lifecycle demo) upgraded to schema 9: counts preserved, integrity ok,
  no foreign-key violations, audit chain intact, existing policy versions unchanged, and **all 5 old runs reproduce**.
  (This check found and fixed a bug: declaring the new CRE policy parameters as schema fields changed the content
  hash of existing policy versions; they are now validated without altering older policies.)
* Property, rent-roll, evidence, traceability and deal-memo pages in Chromium at desktop and 390 px width.

Known limitations added in M3:
* CRE stays a loan type of the commercial module (not a separate module); rent roll and T-12 come from CSV only
  (export from Excel/PDF tools first). Lease-level details beyond rent, dates, area and market rent are not modelled.
* Rule results are re-checkable only for runs saved since M3; older runs are re-checked on metrics only.
* The income-approach value uses a single policy cap rate; it is a cross-check, not an appraisal.
* Deal memos are HTML/PDF built from stored records; there is no narrative text and no model involvement.
