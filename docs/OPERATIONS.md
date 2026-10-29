# Operations: backup, restore, upgrade, logs

## Data directory
`%LOCALAPPDATA%\UWHelper` (override `UWH_DATA_DIR`): `uwhelper.db`, `documents\`, `reports\`, `backups\`, `uwhelper.log`.

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
Pull new code, then `.\.venv\Scripts\python -m pip install -e . --no-deps` and `python -m uwhelper init`.
Migrations apply automatically; take a backup first.

## Policies
Add or bump YAML files in `policies\`, then *Policies → Reload policy files* (or restart).

## Logs
`uwhelper.log` (rotating, local). HTTP access logs are disabled.
