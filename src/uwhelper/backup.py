"""Backup and restore of the local data directory.

A backup is a ZIP containing a consistent SQLite snapshot (online backup API),
all stored documents and reports, and a manifest with SHA-256 checksums.
Backups contain sensitive data: store them on encrypted media.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from uwhelper import __version__
from uwhelper.config import Settings


class BackupError(RuntimeError):
    pass


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def create_backup(settings: Settings, out_path: Path) -> Path:
    if not settings.db_path.is_file():
        raise BackupError("No database found in the data directory.")
    out_path = out_path if out_path.suffix == ".zip" else out_path.with_suffix(".zip")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "uwhelper.db"
        src = sqlite3.connect(settings.db_path)
        dst = sqlite3.connect(snap)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        files: dict[str, Path] = {"uwhelper.db": snap}
        for base, prefix in ((settings.documents_dir, "documents"), (settings.reports_dir, "reports")):
            if base.is_dir():
                for p in sorted(base.rglob("*")):
                    if p.is_file() and ".incoming" not in p.parts:
                        files[f"{prefix}/{p.relative_to(base).as_posix()}"] = p
        manifest = {
            "app": "uwhelper", "app_version": __version__,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": {name: _sha(p) for name, p in files.items()},
        }
        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, p in files.items():
                zf.write(p, name)
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return out_path


def _safe_member(name: str) -> bool:
    p = PurePosixPath(name)
    return not p.is_absolute() and ".." not in p.parts and ":" not in name and "\\" not in name


def restore_backup(settings: Settings, zip_path: Path, *, force: bool = False) -> dict:
    if not zip_path.is_file():
        raise BackupError("Backup file not found.")
    if settings.db_path.exists() and not force:
        raise BackupError("The data directory already contains a database. Use --force to replace it "
                          "(a safety copy of the current database is kept).")
    with zipfile.ZipFile(zip_path) as zf:
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except KeyError as exc:
            raise BackupError("Not a UW Helper backup (manifest missing).") from exc
        if manifest.get("app") != "uwhelper":
            raise BackupError("Not a UW Helper backup.")
        names = set(manifest["files"])
        for name in names:
            if not _safe_member(name) or not (name == "uwhelper.db" or name.startswith(("documents/", "reports/"))):
                raise BackupError(f"Unsafe path in backup: {name!r}")
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            for name in names:
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                if _sha(target) != manifest["files"][name]:
                    raise BackupError(f"Checksum mismatch for {name}; backup is corrupted.")
            conn = sqlite3.connect(staging / "uwhelper.db")
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.close()
            if ok != "ok":
                raise BackupError("Database in backup failed integrity check.")
            settings.ensure_dirs()
            if settings.db_path.exists():
                safety = settings.db_path.with_name(f"uwhelper.pre-restore-{datetime.now():%Y%m%d%H%M%S}.db")
                shutil.copy2(settings.db_path, safety)
                for suffix in ("-wal", "-shm"):
                    Path(str(settings.db_path) + suffix).unlink(missing_ok=True)
            shutil.copy2(staging / "uwhelper.db", settings.db_path)
            for name in names:
                if name.startswith("documents/"):
                    dest = settings.documents_dir / name[len("documents/"):]
                elif name.startswith("reports/"):
                    dest = settings.reports_dir / name[len("reports/"):]
                else:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(staging / name, dest)
    return {"files": len(names), "created_at": manifest.get("created_at")}
