import sqlite3

import pytest

from uwhelper.db.connection import Database
from uwhelper.db.migrate import MigrationError, apply_migrations, current_version, split_sql
from uwhelper.domain.models import CorrectionIn, FactIn
from uwhelper.repo import audit, facts
from tests.conftest import make_case


def test_migrations_idempotent_and_versioned(conn):
    assert current_version(conn) >= 1
    assert apply_migrations(conn) == []


def test_modified_migration_detected(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    (d / "0001_a.sql").write_text("CREATE TABLE t (x INTEGER);", encoding="utf-8")
    c = Database(":memory:").connect()
    assert apply_migrations(c, d) == [1]
    (d / "0001_a.sql").write_text("CREATE TABLE t (x INTEGER, y INTEGER);", encoding="utf-8")
    with pytest.raises(MigrationError, match="modified"):
        apply_migrations(c, d)


def test_failed_migration_rolls_back(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    (d / "0001_a.sql").write_text("CREATE TABLE t (x INTEGER);\nINSERT INTO nope VALUES (1);", encoding="utf-8")
    c = Database(":memory:").connect()
    with pytest.raises(MigrationError):
        apply_migrations(c, d)
    assert c.execute("SELECT name FROM sqlite_master WHERE name='t'").fetchone() is None


def test_split_sql_handles_triggers():
    parts = split_sql("CREATE TABLE a(x);\nCREATE TRIGGER t BEFORE UPDATE ON a BEGIN\n SELECT 1;\nEND;\n")
    assert len(parts) == 2 and parts[1].endswith("END;")


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO borrowers(id, case_id, legal_name, created_at, updated_at) VALUES ('b','nope','x','t','t')")


def test_transaction_rollback(conn):
    cid = make_case(conn)
    with pytest.raises(RuntimeError):
        with Database.transaction(conn):
            conn.execute("UPDATE loan_cases SET title='changed' WHERE id=?", (cid,))
            raise RuntimeError("boom")
    assert conn.execute("SELECT title FROM loan_cases WHERE id=?", (cid,)).fetchone()[0] == "Test"


def test_nested_transaction_savepoint(conn):
    cid = make_case(conn)
    with Database.transaction(conn):
        conn.execute("UPDATE loan_cases SET title='outer' WHERE id=?", (cid,))
        with pytest.raises(ValueError):
            with Database.transaction(conn):
                conn.execute("UPDATE loan_cases SET title='inner' WHERE id=?", (cid,))
                raise ValueError
    assert conn.execute("SELECT title FROM loan_cases WHERE id=?", (cid,)).fetchone()[0] == "outer"


def test_facts_are_append_only(conn):
    cid = make_case(conn)
    fid = facts.add_user_fact(conn, cid, FactIn(field_key="revenue", value="100", period_end="2025-12-31", period_months=12))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE facts SET value_numeric='999' WHERE id=?", (fid,))
    conn.execute("UPDATE facts SET status='verified' WHERE id=?", (fid,))  # status changes are allowed


def test_audit_log_append_only(conn):
    cid = make_case(conn)
    events = audit.list_events(conn, cid)
    assert any(e["action"] == "case_created" for e in events)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit_events SET action='tampered' WHERE id=?", (events[0]["id"],))


def test_sql_injection_is_inert(conn):
    from uwhelper.domain.models import BorrowerIn, CaseIn
    from uwhelper.repo import cases

    evil = "x'); DROP TABLE loan_cases; --"
    cid = cases.create_case(conn, CaseIn(title=evil, loan_type="equipment"), BorrowerIn(legal_name=evil))
    assert cases.get_case(conn, cid)["title"] == evil
    assert conn.execute("SELECT COUNT(*) FROM loan_cases").fetchone()[0] == 1
