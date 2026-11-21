# -*- coding: utf-8 -*-
"""Wave 4A migration checks against the disposable PostgreSQL harness."""

import os
import secrets
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from tests.postgres_harness import resolve_test_database_url

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "scripts" / "migrations"
MIGRATION_001 = MIGRATIONS / "001_submission_attempts.sql"
MIGRATION_002 = MIGRATIONS / "002_outbox_delivery.sql"
APPLY = ROOT / "scripts" / "apply_migration.sh"
LEGACY_SCHEMA = ROOT / "tests" / "fixtures" / "schema_pre_wave3.sql"


def _test_dsn():
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("UPGRADE_TEST_DATABASE_URL missing while UPGRADE_TEST_REQUIRE=1")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL for Wave 4A PostgreSQL coverage")
    return resolve_test_database_url()


def _apply(schema: str, migration: Path = MIGRATION_002, *, expected_success=True):
    env = os.environ.copy()
    env["UPGRADE_TEST_PYTHON"] = sys.executable
    env["UPGRADE_TEST_PGOPTIONS"] = f"-c search_path={schema},public"
    completed = subprocess.run(
        ["bash", str(APPLY), str(migration)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if expected_success:
        assert completed.returncode == 0, completed.stdout + completed.stderr
    else:
        assert completed.returncode != 0, completed.stdout + completed.stderr
    return completed


def _legacy_schema(conn, schema: str):
    conn.execute(f'CREATE SCHEMA "{schema}"')
    conn.execute(f'SET search_path TO "{schema}", public')
    conn.execute(LEGACY_SCHEMA.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO outbox_events (event_type, aggregate_id, payload) "
        "VALUES ('legacy', 'legacy-row', '{}'::jsonb)"
    )


def _drop(conn, schema: str):
    conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.mark.postgres
def test_apply_migration_002_idempotency_checksum_rollback_and_legacy_rows():
    dsn = _test_dsn()
    schema = f"wave4a_{secrets.token_hex(5)}"
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        _legacy_schema(conn, schema)
        before = conn.execute(
            "SELECT id, event_type, flow_target, aggregate_id, payload, status, retry_count, "
            "last_error, locked_at, created_at, processed_at FROM outbox_events ORDER BY id"
        ).fetchall()

        # A valid 001 SHA is insufficient unless 001 was actually applied.
        _apply(schema, expected_success=False)
        assert conn.execute("SELECT to_regclass(%s)", (f'{schema}.schema_migrations',)).fetchone() == (None,)
        assert conn.execute("SELECT id, event_type, flow_target, aggregate_id, payload, status, retry_count, "
                            "last_error, locked_at, created_at, processed_at FROM outbox_events ORDER BY id").fetchall() == before
        assert conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema=%s "
                            "AND table_name='outbox_events' AND column_name='attempts'", (schema,)).fetchone()[0] == 0

        _apply(schema, MIGRATION_001)
        _apply(schema)
        _apply(schema)
        assert conn.execute(f'SELECT version FROM "{schema}".schema_migrations ORDER BY version').fetchall() == [("001",), ("002",)]
        assert conn.execute(
            "SELECT id, event_type, flow_target, aggregate_id, payload, status, retry_count, "
            "last_error, locked_at, created_at, processed_at FROM outbox_events ORDER BY id"
        ).fetchall() == before
        assert conn.execute("SELECT flow_target FROM outbox_events").fetchone() == ("ALL",)
        assert conn.execute("SELECT convalidated FROM pg_constraint WHERE conrelid='outbox_events'::regclass "
                            "AND conname='outbox_events_flow_target_wave4_check'").fetchone() == (True,)

        # An index conflict fails before the ledger insert and rolls every DDL change back.
        _drop(conn, schema)
        _legacy_schema(conn, schema)
        _apply(schema, MIGRATION_001)
        conn.execute(
            "ALTER TABLE outbox_events ADD COLUMN action_key TEXT, "
            "ADD COLUMN aggregate_kind TEXT NOT NULL DEFAULT 'submission'"
        )
        conn.execute(
            "UPDATE outbox_events SET action_key='same' WHERE id IN "
            "(SELECT id FROM outbox_events LIMIT 1)"
        )
        conn.execute(
            "INSERT INTO outbox_events (event_type, flow_target, aggregate_id, payload, action_key) "
            "SELECT event_type, flow_target, aggregate_id, payload, action_key FROM outbox_events LIMIT 1"
        )
        _apply(schema, expected_success=False)
        assert conn.execute(f'SELECT count(*) FROM "{schema}".schema_migrations WHERE version=%s', ("002",)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema=%s "
                            "AND table_name='outbox_events' AND column_name='available_at'", (schema,)).fetchone()[0] == 0

        # The runner computes the migration checksum from the checked-in file; a recorded conflict is rejected.
        _drop(conn, schema)
        _legacy_schema(conn, schema)
        _apply(schema, MIGRATION_001)
        _apply(schema)
        conn.execute(f'UPDATE "{schema}".schema_migrations SET checksum=%s WHERE version=%s', ("0" * 64, "002"))
        _apply(schema, expected_success=False)
    finally:
        _drop(conn, schema)
        conn.close()


@pytest.mark.postgres
def test_fresh_schema_mirrors_wave4a_without_ledger_rows():
    dsn = _test_dsn()
    schema = f"wave4a_fresh_{secrets.token_hex(5)}"
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'SET search_path TO "{schema}", public')
        conn.execute((ROOT / "schema.sql").read_text(encoding="utf-8"))
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema=%s "
                            "AND table_name='outbox_events' AND column_name IN "
                            "('available_at','claimed_by','max_attempts','terminal_reason','lease_until','attempts',"
                            "'action_key','aggregate_kind','result')", (schema,)).fetchone()[0] == 9
        assert conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema=%s "
                            "AND table_name='survey_submissions' AND column_name IN ('work_owner','decision_version')",
                            (schema,)).fetchone()[0] == 2
        conn.execute(
            "INSERT INTO outbox_events(event_type, aggregate_id, payload, flow_target) "
            "VALUES ('legacy', 'all', '{}'::jsonb, 'ALL')"
        )
        conn.execute(
            "INSERT INTO outbox_events(event_type, aggregate_id, payload, flow_target, action_key) "
            "VALUES ('new', 'new', '{}'::jsonb, 'TELEGRAM_NOTIFY', 'notify:new')"
        )
        assert conn.execute("SELECT flow_target FROM outbox_events WHERE action_key='notify:new'").fetchone() == ("TELEGRAM_NOTIFY",)
    finally:
        _drop(conn, schema)
        conn.close()
