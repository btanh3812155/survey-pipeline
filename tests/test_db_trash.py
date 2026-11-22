"""Database-side trash retention, transaction audit, and Drive name/ID ledger."""
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from core.trash import add_trash_item, compact_trash, list_trash, record_drive_ledger
from tests.postgres_harness import SCHEMA_PATH, apply_schema, resolve_test_database_url

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database():
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("disposable PostgreSQL is required")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL")
    dsn = resolve_test_database_url()
    name = "trash_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
        apply_schema(conn)
        try:
            yield make_conninfo(dsn, options=f"-c search_path={name},public")
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest_asyncio.fixture
async def pool(database):
    async with AsyncConnectionPool(database, open=False, min_size=1, max_size=3) as connection_pool:
        yield connection_pool


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_trash_snapshot_ledger_and_retention_compaction(pool):
    now = datetime.now(timezone.utc)
    hourly = now - timedelta(minutes=30)
    four_hour = (now - timedelta(days=2)).replace(hour=1, minute=0, second=0, microsecond=0)
    semiannual = (now - timedelta(days=400)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly = now - timedelta(days=100)
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor() as cur:
            for index in range(12):
                await add_trash_item(
                    cur, "form", "FORM_HOUR", "Hour", {"form_id": "FORM_HOUR"}, "tester",
                    deleted_at=hourly.replace(second=index),
                )
                await add_trash_item(
                    cur, "form", "FORM_FOUR", "Four", {"form_id": "FORM_FOUR"}, "tester",
                    deleted_at=four_hour.replace(second=index),
                )
                await add_trash_item(
                    cur, "form", "FORM_YEAR", "Year", {"form_id": "FORM_YEAR"}, "tester",
                    deleted_at=semiannual.replace(second=index),
                )
            await add_trash_item(
                cur, "form", "FORM_MONTH", "Month", {"form_id": "FORM_MONTH"}, "tester",
                deleted_at=monthly,
            )
    await record_drive_ledger(pool, "form", "FORM_FOUR", [{
        "id": "drive-folder-1", "name": "FORM_FOUR",
        "parent_id": "root", "parent_name": "root",
        "mime_type": "application/vnd.google-apps.folder",
    }], "tester")
    rows = await list_trash(pool)
    assert len(rows) == 37
    compacted = await compact_trash(pool, now=now)
    assert compacted["removed"] == 3
    assert compacted["protected_recent"] == 31
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT retention_tier,count(*) AS n FROM trash_items GROUP BY retention_tier")
            tiers = {row["retention_tier"]: row["n"] for row in await cur.fetchall()}
            await cur.execute("SELECT state,object_name FROM drive_name_ledger WHERE drive_id='drive-folder-1'")
            ledger = await cur.fetchone()
            await cur.execute("SELECT action,count(*) AS n FROM trash_transactions GROUP BY action")
            actions = {row["action"]: row["n"] for row in await cur.fetchall()}
            await cur.execute("SELECT count(*) FROM trash_items WHERE purged_at IS NOT NULL")
            expired = (await cur.fetchone())["count"]
    assert tiers == {"hourly": 11, "four_hourly": 11, "monthly": 1, "semiannual": 11}
    assert ledger["state"] == "TRASHED" and ledger["object_name"] == "FORM_FOUR"
    assert actions["TRASHED"] == 37
    assert actions["DRIVE_TRASHED"] == 1
    assert actions["TRASH_COMPACTED"] == 1
    assert expired == 0


@pytest.mark.postgres
def test_migration_013_retunes_retention(database):
    migrations = ROOT / "scripts" / "migrations"
    previous = hashlib.sha256((migrations / "012_db_trash.sql").read_bytes()).hexdigest()
    path = migrations / "013_trash_retention_policy.sql"
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    migration = path.read_text().replace(":'migration_012_sha256'", f"'{previous}'").replace(":'migration_sha256'", f"'{checksum}'")
    schema = "trash_retention_migration_" + uuid4().hex
    with psycopg.connect(database, autocommit=True) as root:
        root.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            dsn = make_conninfo(database, options=f"-c search_path={schema}")
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, checksum TEXT NOT NULL)")
                conn.execute("INSERT INTO schema_migrations VALUES ('012',%s)", (previous,))
                conn.execute("""CREATE TABLE trash_items (
                    id UUID PRIMARY KEY,
                    expires_at TIMESTAMPTZ NOT NULL,
                    retention_tier VARCHAR(16) NOT NULL CHECK (retention_tier IN ('all','hourly','daily','weekly','monthly','expired'))
                )""")
                conn.execute("INSERT INTO trash_items VALUES ('00000000-0000-0000-0000-000000000001', now(), 'expired')")
                conn.execute(migration)
                assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [("012",), ("013",)]
                assert conn.execute("SELECT expires_at,retention_tier FROM trash_items").fetchone() == (None, "semiannual")
                conn.execute("INSERT INTO trash_items VALUES ('00000000-0000-0000-0000-000000000002', NULL, 'four_hourly')")
        finally:
            root.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.postgres
def test_migration_012_creates_trash_tables(database):
    migrations = ROOT / "scripts" / "migrations"
    previous = hashlib.sha256((migrations / "011_engine_modules.sql").read_bytes()).hexdigest()
    path = migrations / "012_db_trash.sql"
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    migration = path.read_text().replace(":'migration_011_sha256'", f"'{previous}'").replace(":'migration_sha256'", f"'{checksum}'")
    schema = "trash_migration_" + uuid4().hex
    with psycopg.connect(database, autocommit=True) as root:
        root.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            dsn = make_conninfo(database, options=f"-c search_path={schema}")
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, checksum TEXT NOT NULL)")
                conn.execute("INSERT INTO schema_migrations VALUES ('011',%s)", (previous,))
                conn.execute(migration)
                assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [("011",), ("012",)]
                for table in ("trash_items", "trash_transactions", "drive_name_ledger"):
                    assert conn.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone() == (True,)
        finally:
            root.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
