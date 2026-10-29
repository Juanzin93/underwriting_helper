# UW Helper — local-first commercial loan underwriting workbench

A personal research tool for **preliminary** commercial credit analysis that runs entirely on your Windows PC:

* imports PDFs/images, reads text with PyMuPDF and local **Tesseract** OCR, tracks every page and SHA-256;
* extracts **candidate** facts with a **local Ollama model** (default `qwen3:8b`) — never a cloud API;
* lets you compare each value with its source page, **verify / correct / reject** it (full history kept);
* detects **contradictions** between sources and never picks a value for you;
* calculates DSCR, global DSCR, LTV/CLTV/LTC, leverage, liquidity, margins, EBITDA/CFADS, collateral coverage,
  break-even and sensitivity **deterministically in Python** with explicit formulas and inputs;
* applies **versioned YAML policies** (PASS / FAIL / UNKNOWN / NOT_APPLICABLE / REVIEW_REQUIRED);
* saves immutable underwriting runs, scenarios and an audit log in SQLite;
* exports an evidence-backed **HTML/PDF preliminary credit analysis**.

> **It is not a credit decision-maker.** The model never approves or denies. A clean screen is shown as
> "no policy exceptions identified — NOT a credit approval". A FAIL means "needs human review", not "denied".
> The bundled policy thresholds are **illustrative demo values**, not lending standards.

---

## 1. Install (Windows 10/11, PowerShell)

Prerequisites (one-time, internet required):

```powershell
winget install --id Python.Python.3.12          # or python.org installer; tick "Add to PATH"
winget install --id Ollama.Ollama               # local LLM runtime
winget install --id UB-Mannheim.TesseractOCR    # OCR for scanned pages
winget install --id Git.Git                     # optional
```

Open a **new** PowerShell window, then:

```powershell
cd C:\path\to\underwriting_helper
ollama pull qwen3:8b                 # ~5 GB download; use qwen3:4b on GPUs with < 8 GB VRAM
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

`setup.ps1` creates `.venv`, installs dependencies, installs a git hook that blocks committing documents,
creates the database, runs the test suite and prints a local-only health check (`uwhelper doctor`).
Use `scripts\setup.ps1 -Model qwen3:4b -PullModel` to configure and download the smaller model.

Manual equivalent:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pip install -e . --no-deps
.\.venv\Scripts\python -m uwhelper init
```

## 2. Start

```powershell
scripts\start.ps1                    # opens http://127.0.0.1:8765
scripts\start.ps1 -Model qwen3:4b    # switch model for this session
# or: .\.venv\Scripts\python -m uwhelper serve
```

Load the fictional sample loan (useful first step):

```powershell
.\.venv\Scripts\python -m uwhelper load-sample
```

The sample ("Riverbend Precision Machining LLC (FICTIONAL)", $480k CNC equipment loan) contains a text P&L,
a balance sheet, a tax return whose revenue **contradicts** the P&L, a **scanned** equipment quote that needs OCR,
and a notes page with a **prompt-injection attempt**. Suggested walk-through:
Overview → Documents (see OCR + injection warning) → Run screening (INCOMPLETE: revenue contradiction, unverified debt)
→ Contradictions (resolve with a reason) → Financial data (verify the line of credit) → Run again → Metrics →
Findings → Scenarios (e.g. revenue −15 %, +200 bps) → Reports.

## 3. Tests

```powershell
scripts\run_tests.ps1                # 160 tests; no Ollama or internet needed
scripts\run_tests.ps1 -Ollama        # also runs live tests against your local model
# or: .\.venv\Scripts\python -m pytest -q
```

## 4. Commands

| Command | Purpose |
|---|---|
| `python -m uwhelper serve [--port N] [--model M]` | Web app on 127.0.0.1 |
| `python -m uwhelper init` | Create/upgrade DB, register policy files |
| `python -m uwhelper doctor` | Local-only, Ollama, model and Tesseract checks |
| `python -m uwhelper load-sample` | Load the synthetic sample case |
| `python -m uwhelper sample-pdfs --out samples\generated` | Write the synthetic PDFs to disk |
| `python -m uwhelper backup [--out FILE]` / `restore FILE [--force]` | Backup / restore |
| `python -m uwhelper extract CASE_ID DOCUMENT_ID` | Run extraction from the CLI |
| `--data-dir DIR` (any command) | Use another data directory |

## 5. Configuration

Data lives **outside the repository** in `%LOCALAPPDATA%\UWHelper` (database, documents, reports, log).
Override with `UWH_DATA_DIR` or `--data-dir`. Settings come from `%LOCALAPPDATA%\UWHelper\uwhelper.toml`
and `UWH_*` environment variables, e.g.:

```toml
ollama_model = "qwen3:4b"        # any locally pulled model; "...cloud" models are refused
ollama_timeout_seconds = 300
ollama_num_ctx = 8192
tesseract_cmd = "C:\\Program Files\\Tesseract-OCR\\tesseract.exe"
max_upload_mb = 50
port = 8765
```

The Ollama URL must be loopback (`http://127.0.0.1:11434`); the app refuses anything else, and refuses to
bind to a non-loopback address unless `UWH_ALLOW_NON_LOOPBACK_BIND=1` (not recommended).

## 6. Architecture

```
src/uwhelper/
  config.py            settings + loopback/cloud-model validation
  db/                  SQLite connection, transactions, checksum-verified migrations (migrations/*.sql)
  domain/              Decimal money parsing/rounding, enums, Pydantic input models
  modules/             lending-module interface + commercial module (fields, loan types, checklist)
  metrics/             loan math (finance.py) and deterministic commercial metrics (commercial.py)
  policy/              policy schema (Pydantic), loader/versioning, rule engine
  analysis/            fact resolution & contradictions, scenarios/sensitivity, run orchestration
  documents/           safe upload storage, PyMuPDF/OCR extraction, injection heuristics
  llm/                 local Ollama client, structured extraction, optional narrative
  repo/                data access for cases, facts (append-only), audit log
  reports/             report context, HTML template, PDF rendering (PyMuPDF Story)
  web/                 FastAPI app, security middleware, routes, Jinja2 templates, local CSS/JS
  backup.py cli.py diagnostics.py sample_data.py
policies/              versioned YAML policies (illustrative demo)
scripts/               Windows setup/start/test/backup scripts, git hook
tests/                 unit tests (no Ollama) + opt-in live Ollama tests
docs/                  architecture/ERD, metrics, policies, security, operations, troubleshooting, checklist
```

Flow: **document → pages (text/OCR) → LLM candidates (untrusted) → human verification → resolver (contradictions,
periods, verified-only) → metrics (Python) → policy engine → immutable run → report.**

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (with ER diagram), [docs/METRICS.md](docs/METRICS.md),
[docs/POLICIES.md](docs/POLICIES.md), [docs/SECURITY.md](docs/SECURITY.md), [docs/OPERATIONS.md](docs/OPERATIONS.md),
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md), [docs/LOCAL_ONLY_CHECKLIST.md](docs/LOCAL_ONLY_CHECKLIST.md),
[docs/LICENSING.md](docs/LICENSING.md), [docs/STATUS.md](docs/STATUS.md) (milestones, limitations, unfinished features).
