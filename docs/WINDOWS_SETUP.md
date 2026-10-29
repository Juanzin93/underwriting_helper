# Windows setup, configuration, run, backup and restore

Everything runs on your PC. Internet is needed only to download the installers, Python packages and the model.
After that the app works offline.

## 1. Prerequisites (one time)

```powershell
winget install --id Python.Python.3.12          # 3.11–3.13 supported; tick "Add to PATH" with the python.org installer
winget install --id Ollama.Ollama               # local LLM runtime (optional: the app works without it)
winget install --id UB-Mannheim.TesseractOCR    # OCR for scanned pages (optional)
winget install --id Git.Git
```

Open a **new** PowerShell window so PATH changes apply.

## 2. Install

```powershell
cd C:\path\to\underwriting_helper
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

`setup.ps1` creates `.venv`, installs the **exact tested versions from `requirements.lock`**, runs `pip check`,
installs the git hook that blocks committing documents, creates/upgrades the database, runs the tests and prints
`uwhelper doctor`. Options: `-Model qwen3:4b -PullModel` (choose and download a model), `-SkipTests`,
`-Latest` (resolve newest versions allowed by `requirements-dev.txt` instead of the lock).

Manual equivalent:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.lock
.\.venv\Scripts\python -m pip install -e . --no-deps
.\.venv\Scripts\python -m pip check
.\.venv\Scripts\python -m uwhelper init
```

Pull a local model (optional; only extraction and narrative drafting need it):

```powershell
ollama pull qwen3:8b      # ~5 GB; use qwen3:4b with < 8 GB VRAM. Models ending in ":cloud" are refused.
```

## 3. Configure

Defaults work without any configuration. To change something, copy the example and edit it:

```powershell
Copy-Item .env.example .env
notepad .env
```

Precedence (lowest → highest): built-in defaults → `%LOCALAPPDATA%\UWHelper\uwhelper.toml` → `.env` in the current
folder (or the file named by `UWH_ENV_FILE`) → `UWH_*` environment variables → command-line flags.
Only `UWH_*` keys are read from `.env`; `.env` is ignored by git.

| Setting | Default | Notes |
|---|---|---|
| `UWH_DATA_DIR` | `%LOCALAPPDATA%\UWHelper` | Database, documents, reports, backups, log |
| `UWH_DB_FILE` | `<data dir>\uwhelper.db` | Optional explicit database file |
| `UWH_DOCUMENTS_PATH` | `<data dir>\documents` | Optional explicit document store |
| `UWH_HOST` / `UWH_PORT` | `127.0.0.1` / `8765` | Non-loopback hosts refused |
| `UWH_OLLAMA_URL` / `UWH_OLLAMA_MODEL` | `http://127.0.0.1:11434` / `qwen3:8b` | Loopback URL only |
| `UWH_MAX_UPLOAD_MB` / `UWH_MAX_PAGES` | `50` / `300` | Upload limits |
| `UWH_DB_BUSY_TIMEOUT_MS` | `30000` | SQLite lock wait |
| `UWH_LOG_LEVEL` | `INFO` | `DEBUG` … `CRITICAL` |
| `UWH_ACTOR_NAME` | Windows account name | "Recorded by" on audit events, tasks, notes (attribution, not a login) |
| `UWH_EXTERNAL_INTEGRATIONS_ALLOWED` / `UWH_ENABLED_INTEGRATIONS` | `false` / empty | Both needed to enable a named integration; none exist yet |

Keep the data folder off OneDrive/Dropbox and outside the repository. The app refuses to start if the data,
database or document folder is inside the web `static` folder.

## 4. Run

```powershell
scripts\start.ps1                                  # opens http://127.0.0.1:8765
# or
.\.venv\Scripts\python -m uwhelper serve
```

Check health (works without Ollama):

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

`status` is `ok` when everything is available, `degraded` when only optional parts (Ollama, OCR) are missing — deals,
contacts, documents and underwriting math still work — and HTTP 503 `unavailable` when the database cannot be used.

Confirm the server listens only on loopback:

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen | Select-Object LocalAddress   # expect 127.0.0.1
```

Stop with **Ctrl+C**; the app checkpoints the SQLite write-ahead log on shutdown.

## 5. Backup

```powershell
.\.venv\Scripts\python -m uwhelper backup --out E:\Encrypted\uwhelper-2026-10-29.zip
# or: scripts\backup.ps1 -Out E:\Encrypted\uwhelper-2026-10-29.zip
```

Safe while the app runs (SQLite online backup). The ZIP holds the database, documents, reports and a SHA-256 manifest.
It contains borrower data — store it on encrypted media (e.g. BitLocker To Go).

## 6. Restore

Stop the app first, then:

```powershell
.\.venv\Scripts\python -m uwhelper restore E:\Encrypted\uwhelper-2026-10-29.zip           # into an empty data folder
.\.venv\Scripts\python -m uwhelper restore E:\Encrypted\uwhelper-2026-10-29.zip --force   # replace; keeps uwhelper.pre-restore-*.db
.\.venv\Scripts\python -m uwhelper verify-audit
```

Restore checks every checksum, rejects unsafe paths, runs `PRAGMA integrity_check`, and writes into the configured
database and document locations (including `UWH_DB_FILE` / `UWH_DOCUMENTS_PATH`). An older backup is upgraded by the
migrations the next time the app starts.

## 7. Upgrade

```powershell
.\.venv\Scripts\python -m uwhelper backup
git pull
.\.venv\Scripts\python -m pip install -r requirements.lock
.\.venv\Scripts\python -m pip install -e . --no-deps
.\.venv\Scripts\python -m uwhelper init        # applies new migrations (0007 CRM, 0008 workflows); existing data kept
```

Migrations are forward-only and checksum-verified; the app refuses to start if an applied migration file was edited.

## 8. Tests and offline check

```powershell
.\.venv\Scripts\python -m pytest -q            # no internet or Ollama needed; tests block non-loopback network
scripts\run_tests.ps1 -Ollama                  # also runs the live local-model tests
```

To confirm offline operation yourself: disconnect the network (or disable the adapter), start the app, open
`/health` and create a deal. Everything except model-based extraction keeps working.
