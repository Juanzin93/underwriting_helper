# Architecture assessment (Milestone 1 of the lending platform)

Date: 2026-10-29. Baseline before Milestone 1: 222 passed, 2 skipped (live Ollama tests).

## 1. What exists

A working **modular monolith** (FastAPI + Jinja2 + SQLite, Python 3.11–3.13) with a complete single-loan workflow:
documents/OCR → local-LLM candidate facts → human verification → deterministic metrics → versioned YAML policies →
immutable underwriting runs → credit memo → human decision → conditions → closing → human-confirmed funding →
servicing. 54 tables, 7 forward-only checksum-verified migrations, hash-chained audit log, CSRF/Host/Origin
protection, loopback-only binding and Ollama access.

Strengths worth keeping: Decimal-only money math with explicit formulas; append-only facts/runs/decisions enforced by
triggers; the LLM never authors a number that is used without human verification; strong local-only guards.

## 2. Problems found in the inspection (and what Milestone 1 did)

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | No shared organization/contact/deal entities — everything hangs off one `loan_cases` row with an embedded borrower; no way to run a brokerage pipeline across many deals | High (blocks the business model) | **Added** `organizations`, `contacts`, `deals` + links (migration 0007) |
| 2 | Settings scattered: no way to put the DB or documents on another (encrypted) volume; no `.env`; integrations had no switch | Medium | **Fixed** (`UWH_DB_FILE`, `UWH_DOCUMENTS_PATH`, `.env`, integration flags, validation) |
| 3 | No lock file — fresh installs could resolve to untested versions (PyMuPDF 2.x adds onnxruntime/numpy/networkx) | Medium | **Fixed** `requirements.lock`, `pymupdf<2`, setup uses the lock + `pip check` |
| 4 | FastAPI ≥ 0.16x ships native OpenTelemetry hooks (`opentelemetry-api` is a dependency) | Medium (privacy) | **Disabled** (`telemetry` off when supported, `OTEL_SDK_DISABLED=true`, auto-configure env var removed); no exporter installed; test asserts it |
| 5 | Errors were HTML-only; no request IDs; validation errors leaked raw input in details | Medium | **Fixed** JSON error envelope for API paths with `X-Request-ID`; input values stripped |
| 6 | No health endpoint; diagnostics required the CLI | Medium | **Added** `/health` (200 ok/degraded, 503 when DB down; Ollama optional and cached) |
| 7 | SQLite busy timeout hard-coded; WAL never checkpointed on shutdown | Low | **Fixed** configurable timeout, `shutdown()` checkpoint + optimize in lifespan |
| 8 | Upload handlers leaked spooled temp files (form never closed) | Low | **Fixed** `read_form()` context manager |
| 9 | Two tests leaked SQLite connections | Low | **Fixed**; suite passes with `-W error::ResourceWarning` |
| 10 | Tests could reach the network by accident | Medium | **Fixed** autouse guard blocks non-loopback connects and DNS |
| 11 | Data dir inside `web/static` would serve documents | Low (misconfig) | **Refused** at settings validation; test confirms `/static/../uwhelper.db` → 404 |

## 3. Risks that remain

| Risk | Impact | Mitigation / plan |
|---|---|---|
| **No authentication; "authorized by" is a typed name** | Fine for one person; not acceptable once staff share an install | M2 records the Windows account (or `UWH_ACTOR_NAME`) as actor — attribution, not authentication. Local accounts are required before any multi-user use |
| **PyMuPDF is AGPL-3.0** | Using it privately is fine; distributing the app to clients/partners as part of a brokerage business triggers AGPL obligations | Decide before distribution: keep source available, buy an Artifex licence, or swap to pypdfium2 (Apache/BSD) behind `documents/pdf.py` |
| Two pipelines: `deals.status` (brokerage) and `loan_cases.stage` (underwriting lifecycle) | Users could see contradictory states | **Resolved in M2**: recorded sync rules and guards (docs/LIFECYCLE.md) |
| Borrower duplicated in `borrowers` (per case) and `organizations` | Data drift | **Resolved in M2**: every file borrower links to an organization (backfilled) |
| No encryption at rest | Disk theft exposes borrower data | BitLocker/VeraCrypt (docs/SECURITY.md); `UWH_DB_FILE` now allows an encrypted volume |
| SQLite single writer | Fine for 1–3 local users; not for a team server | Keep; revisit only with real multi-user demand |
| Some HTML routes still run SQL directly | Harder to test/reuse | Move into services as each area is touched (no big-bang refactor) |
| `opentelemetry-api` installed transitively | No exporter, disabled; only an API package | Re-check on every FastAPI upgrade (lock file makes upgrades deliberate) |
| Ollama listens unauthenticated on 127.0.0.1:11434 | Other local processes can use it | Outside app control; documented |
| Windows scripts and live-Ollama tests not run by the author | Possible script errors on real Windows | Run `scripts\setup.ps1` and `run_tests.ps1 -Ollama` once on the target PC |

## 4. Module map: current code → target boundaries

Target boundaries are **packages inside one process**, not services.

| Target | Current location | Notes |
|---|---|---|
| core | `core/` (errors, request IDs), `config.py`, `bootstrap.py` | New in M1 |
| db | `db/` | Connection, migrations |
| crm | `crm/` | M1: organizations, contacts, deals. M2: `pipeline` (sync), `tasks`, `requests` (checklists), `activities`, `importer` |
| borrowers | `repo/cases.py` (borrowers/parties) linked to `crm` organizations/contacts | Linked in M2 |
| documents | `documents/` | `ingest_file` now shared by cases and deals |
| underwriting/common | `analysis/`, `metrics/finance.py`, `policy/` | |
| underwriting/business | `modules/commercial.py`, `metrics/commercial.py` | |
| underwriting/cre | `cre/` (rent rolls, T-12) and `metrics/cre.py`; EGI/NOI/property DSCR/debt yield in `metrics/commercial.py` | M3. Kept as a loan type of the commercial module (see ROADMAP design decision) |
| lenders, matching | `lenders/` (service, models, matching, importer) | M4 |
| structuring | `structuring.py`, `metrics/structuring.py` | |
| origination | `workflow/` (file lifecycle, decisions, conditions, closing); `origination/` (deal conditions, due diligence, closing, funding, gates) | M6 for deals |
| brokerage | `brokerage/` (packages, submissions & offers, fees & commission ledger); deal pipeline in `crm/` | M5 |
| servicing | `servicing/` | |
| direct_lending | — | M8 |
| reporting | `reports/`; `portfolio/` (monitoring, portfolio reports, payoff quotes) | M7 |
| integrations | `integrations/` | Registry + disabled-by-default gate; no providers |
| ai | `llm/` | Local Ollama only |
| web | `web/` (HTML routes + `/api/v1`) | |

## 5. Design decisions in Milestone 1

* **Deals sit above loan cases.** A deal is the brokerage/CRM record; "Start underwriting file" creates and links a
  loan case (`deals.loan_case_id`, unique) and copies the deal's documents. Existing cases are untouched and still work
  without a deal.
* **Deal statuses:** INTAKE, UNDERWRITING, STRUCTURING, LENDER_SUBMISSION, APPROVED, DUE_DILIGENCE, CLOSING, FUNDED,
  DECLINED, WITHDRAWN, ON_HOLD, CANCELLED. CREDIT_REVIEW and SERVICING already exist in the loan-case lifecycle,
  where they belong (internal credit review; post-funding servicing). M2.1 decides whether the deal mirrors them.
* APPROVED, FUNDED and DECLINED need `authorized_by` + explicit confirmation; resuming from ON_HOLD back to the same
  status does not re-ask. FUNDED deals are locked. Every transition is in an append-only history table and the audit log.
* Money stored as TEXT decimals (consistent with the existing schema); `CHECK` constraints on enums and amounts;
  foreign keys `RESTRICT` where deleting would orphan history.
* Audit details record changed field names and old/new values for short fields; free-text fields log only "(changed)".
* Integrations: a registry with two required switches; any future provider must call `require_share_confirmation`
  listing exactly what will be sent. Nothing is registered.
* No breaking changes: all existing URLs, forms and CLI commands behave the same. The navigation gained
  Deals / Contacts / Organizations; "Cases" is now labelled "Underwriting files" at the same URL (`/`).
