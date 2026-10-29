# Milestone status, known limitations, unfinished features

Latest test run: **222 passed, 2 skipped** (`pytest`, Python 3.13 on Linux, Tesseract 5.3.4). The 2 skipped tests are
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
