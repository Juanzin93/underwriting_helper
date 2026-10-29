"""Initialise data directory, database schema and policy registry."""

from __future__ import annotations

import logging

from uwhelper.config import Settings
from uwhelper.db.connection import Database
from uwhelper.db.migrate import apply_migrations
from uwhelper.policy.loader import LoadReport, sync_policies_dir

log = logging.getLogger("uwhelper")


def initialise(settings: Settings) -> tuple[Database, LoadReport]:
    settings.ensure_dirs()
    db = Database(settings.db_path)
    with db.session() as conn:
        applied = apply_migrations(conn)
        if applied:
            log.info("Applied migrations: %s", applied)
        report = sync_policies_dir(conn, settings.policies_dir)
    for err in report.errors:
        log.error("Policy load error: %s", err)
    return db, report
