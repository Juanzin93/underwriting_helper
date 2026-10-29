"""SQLite access with foreign keys, WAL and explicit transactions.

All SQL in the application uses ``?`` parameters; no SQL is ever built from
user input by string formatting.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


class Database:
    """Connection factory. One short-lived connection per request/job (``session()``); never shared across threads.

    Every connection: foreign keys ON, WAL journal, busy timeout, autocommit mode with explicit
    ``transaction()`` blocks (BEGIN IMMEDIATE ... COMMIT / ROLLBACK).
    """

    def __init__(self, path: Path | str, busy_timeout_ms: int = 30000):
        self.path = str(path)
        self.busy_timeout_ms = int(busy_timeout_ms)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms:d}")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def check(self) -> dict[str, Any]:
        """Lightweight availability check used by the health endpoint."""
        with self.session() as conn:
            conn.execute("SELECT 1").fetchone()
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            ver = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        return {"foreign_keys": bool(fk), "schema_version": ver}

    def shutdown(self) -> None:
        """Clean shutdown: fold the WAL into the main file and refresh query-planner statistics."""
        if self.path == ":memory:":
            return
        with self.session() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA optimize")

    @staticmethod
    @contextmanager
    def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT``; rolls back on any exception."""
        if conn.in_transaction:
            # Nested use: a SAVEPOINT keeps the inner block atomic.
            name = f"sp_{uuid.uuid4().hex[:8]}"
            conn.execute(f"SAVEPOINT {name}")
            try:
                yield conn
            except BaseException:
                conn.execute(f"ROLLBACK TO {name}")
                conn.execute(f"RELEASE {name}")
                raise
            conn.execute(f"RELEASE {name}")
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
