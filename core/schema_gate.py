# -*- coding: utf-8 -*-
"""Shared PostgreSQL writer-schema gate.

The writer and readiness endpoint must agree on the same additive migration
contract; a reachable database with the wrong ledger must not accept writes.
"""

import hashlib
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_001_PATH = ROOT / "scripts" / "migrations" / "001_submission_attempts.sql"
try:
    MIGRATION_001_CHECKSUM = hashlib.sha256(MIGRATION_001_PATH.read_bytes()).hexdigest()
except OSError:
    MIGRATION_001_CHECKSUM = ""

REQUIRED_WRITER_TABLES = (
    "survey_submissions",
    "outbox_events",
    "authorized_users",
    "driver_identity_vault",
    "driver_registered_vehicles",
    "phone_telegram_links",
    "submission_idempotency",
    "schema_migrations",
)
REQUIRED_WRITER_COLUMNS = ("media_manifest", "media_version")


async def writer_schema_state(conn) -> Dict[str, Any]:
    """Inspect the current connection; never mutate schema."""
    await conn.execute("SELECT 1")
    row = await conn.execute(
        "SELECT to_regclass('public.survey_submissions'), "
        "to_regclass('public.outbox_events'), "
        "to_regclass('public.authorized_users'), "
        "to_regclass('public.driver_identity_vault'), "
        "to_regclass('public.driver_registered_vehicles'), "
        "to_regclass('public.phone_telegram_links'), "
        "to_regclass('public.submission_idempotency'), "
        "to_regclass('public.schema_migrations')"
    )
    table_row = await row.fetchone()
    missing = [
        table for index, table in enumerate(REQUIRED_WRITER_TABLES)
        if not table_row or index >= len(table_row) or table_row[index] is None
    ]
    columns = await conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='survey_submissions' "
        "AND column_name IN ('media_manifest','media_version')"
    )
    column_rows = await columns.fetchall()
    migration = await conn.execute(
        "SELECT checksum FROM schema_migrations WHERE version='001'"
    )
    migration_row = await migration.fetchone()
    missing_columns = set(REQUIRED_WRITER_COLUMNS) - {
        row[0] for row in (column_rows or [])
    }
    migration_checksum = migration_row[0] if migration_row else None
    return {
        "missing": missing,
        "missing_columns": missing_columns,
        "migration_checksum": migration_checksum,
    }


async def writer_schema_ready(conn) -> bool:
    state = await writer_schema_state(conn)
    return (
        not state["missing"]
        and not state["missing_columns"]
        and bool(MIGRATION_001_CHECKSUM)
        and state["migration_checksum"] == MIGRATION_001_CHECKSUM
    )
