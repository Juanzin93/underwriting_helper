# Operations: backup, restore, upgrade, logs

## Data directory
`%LOCALAPPDATA%\UWHelper` (override `UWH_DATA_DIR`): `uwhelper.db`, `documents\`, `reports\`, `backups\`, `uwhelper.log`.
`UWH_DB_FILE` and `UWH_DOCUMENTS_PATH` move the database or documents elsewhere (e.g. an encrypted volume); backup and
restore follow them. Full Windows walk-through: [WINDOWS_SETUP.md](WINDOWS_SETUP.md).

## Health
`GET http://127.0.0.1:8765/health` (`?checks=basic` skips the Ollama probe). 200 `ok`/`degraded`, 503 if the database
is unavailable. Responses include component status only — no paths to documents or data values.

## Backup
```powershell
scripts\backup.ps1 -Out E:\Encrypted\uwhelper-2026-10-29.zip
```
Uses SQLite's online backup API (safe while the app runs) and writes a ZIP with documents, reports and a
SHA-256 manifest.

## Restore
```powershell
.\.venv\Scripts\python -m uwhelper restore E:\Encrypted\uwhelper-2026-10-29.zip            # empty data dir
.\.venv\Scripts\python -m uwhelper restore E:\Encrypted\uwhelper-2026-10-29.zip --force    # replace; keeps uwhelper.pre-restore-*.db
```
Restore verifies every checksum, rejects unsafe paths and runs `PRAGMA integrity_check`. Stop the app first.

## Upgrades / migrations
Back up, pull new code, then `pip install -r requirements.lock`, `pip install -e . --no-deps` and `python -m uwhelper init`.
Migrations apply automatically; take a backup first.

## Policies
Add or bump YAML files in `policies\`, then *Policies → Reload policy files* (or restart).

## Logs
`uwhelper.log` (rotating, local; level `UWH_LOG_LEVEL`). HTTP access logs are disabled. Errors log the exception
type and request ID only; quote the `request_id` from an error response to find the matching log line.

## Shutdown
Ctrl+C. The app checkpoints the SQLite WAL (`wal_checkpoint(TRUNCATE)`) and runs `PRAGMA optimize` on shutdown.
