"""Forward-only, checksum-verified SQL migrations.

Strategy
--------
* Migrations live in ``db/migrations/NNNN_description.sql`` and are applied
  in numeric order, each inside its own transaction.
* ``schema_migrations`` records version, name, SHA-256 checksum and time.
* An already-applied migration whose file content changed is an error: never
  edit an applied migration, add a new one instead.
* There are no down-migrations; restore from a backup to go back
  (see docs/OPERATIONS.md).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .connection import utcnow

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        m = _NAME_RE.match(path.name)
        if not m:
            raise MigrationError(f"Badly named migration file: {path.name}")
        found.append(Migration(int(m.group(1)), m.group(2), path.read_text(encoding="utf-8")))
    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise MigrationError("Duplicate migration version numbers")
    return found


def apply_migrations(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list[int]:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               name TEXT NOT NULL,
               checksum TEXT NOT NULL,
               applied_at TEXT NOT NULL)"""
    )
    applied = {
        row["version"]: row["checksum"]
        for row in conn.execute("SELECT version, checksum FROM schema_migrations")
    }
    newly: list[int] = []
    for mig in discover(directory):
        if mig.version in applied:
            if applied[mig.version] != mig.checksum:
                raise MigrationError(
                    f"Migration {mig.version:04d}_{mig.name} was modified after being applied. "
                    "Create a new migration instead of editing an applied one."
                )
            continue
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in split_sql(mig.sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?,?,?,?)",
                (mig.version, mig.name, mig.checksum, utcnow()),
            )
            conn.execute("COMMIT")
        except Exception as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"Migration {mig.version:04d}_{mig.name} failed: {exc}") from exc
        newly.append(mig.version)
    return newly


def split_sql(sql: str) -> list[str]:
    """Split a migration script into statements (handles trigger bodies)."""
    statements: list[str] = []
    buf: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") and not buf:
            continue
        buf.append(line)
        candidate = "\n".join(buf).strip()
        if candidate.endswith(";") and sqlite3.complete_statement(candidate):
            statements.append(candidate)
            buf = []
    tail = "\n".join(buf).strip()
    if tail and not all(l.strip().startswith("--") or not l.strip() for l in buf):
        statements.append(tail)
    return statements


def current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["v"] or 0)
