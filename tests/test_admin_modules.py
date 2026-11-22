"""Database-backed module registry and safe logic execution."""
import hashlib
import json
import os
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool

from core import admin_modules
from services import image_objects
from tests.postgres_harness import SCHEMA_PATH, resolve_test_database_url

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


@pytest.fixture
def database():
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("disposable PostgreSQL is required")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL")
    dsn = resolve_test_database_url()
    name = "modules_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            yield make_conninfo(dsn, options=f"-c search_path={name}")
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest_asyncio.fixture
async def store(database, monkeypatch):
    schema = SCHEMA_PATH.read_text()
    module_block = schema.split("CREATE TABLE IF NOT EXISTS admin_module_registry", 1)[1].split("-- Migration 007", 1)[0]
    audit = schema.split("CREATE TABLE IF NOT EXISTS system_audit_log", 1)[1].split("CREATE INDEX", 1)[0]
    with psycopg.connect(database, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS admin_module_registry" + module_block)
        conn.execute("CREATE TABLE IF NOT EXISTS system_audit_log" + audit)
    old_objects = image_objects._DATABASE_OBJECTS
    try:
        async with AsyncConnectionPool(database, open=False, min_size=1, max_size=4) as pool:
            assert await admin_modules.ensure_admin_module_registry(pool)
            monkeypatch.setattr("core.state.get_db_pool", lambda: pool)
            yield pool, conn
    finally:
        image_objects._DATABASE_OBJECTS = old_objects
        image_objects._CACHE["signature"] = None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_registry_seeds_examples_and_runs_only_allowlisted_logic(store):
    pool, _conn = store
    modules = {row["module_id"]: row for row in await admin_modules.list_modules(pool)}
    assert {"gsm-profile-screen", "get-percentage", "weekly_income"} <= set(modules)
    assert modules["get-percentage"]["module_type"] == "logic"
    assert "function" not in modules["get-percentage"]["definition"]
    result = await admin_modules.execute_logic_module(pool, "get-percentage", {"target": 7, "total": 10})
    assert result == {"percentage": 70.0}
    assert await admin_modules.execute_logic_module(pool, "get-percentage", {"target": 5, "total": 0}) == {"percentage": 0.0}
    with pytest.raises(admin_modules.ModuleDefinitionError):
        await admin_modules.execute_logic_module(pool, "get-percentage", {"target": "7", "total": 10})
    with pytest.raises(admin_modules.ModuleDefinitionError):
        await admin_modules.save_module(pool, "unsafe", "logic", "Unsafe", "1", {
            "name": "unsafe", "version": "1", "description": "",
            "implementation": "os.system", "inputs": [],
        }, True, "tester")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_gsm_profile_prompt_fields_and_disable(store):
    pool, _conn = store
    obj = image_objects.get_object("gsm-profile-screen")
    assert obj.extractor == "generic"
    assert obj.enabled is True
    fields = {field.key: field for field in obj.expected_fields}
    assert fields["membership"].type == "string"
    assert fields["achievements"].required and fields["history"].required
    assert obj.customizable[0].default == "2026.10"
    assert obj.customizable[1].default == "gsm"
    prompt = image_objects.build_detection_prompt(obj)
    assert "Return only one valid JSON object" in prompt
    assert "membership, achievements, history, name, version, app_name" in prompt
    assert await admin_modules.set_module_enabled(pool, "gsm-profile-screen", False, "tester")
    with pytest.raises(image_objects.ImageObjectDefinitionError):
        image_objects.resolve_slot_object({"object_type": "gsm-profile-screen"})
    assert image_objects.load_registry()["objects"]["gsm-profile-screen"].enabled is False


@pytest.mark.postgres
def test_migration_008_applies_once_from_007(database):
    migrations = ROOT / "scripts" / "migrations"
    previous = hashlib.sha256((migrations / "007_admin_account_bin.sql").read_bytes()).hexdigest()
    path = migrations / "008_admin_module_registry.sql"
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    migration = path.read_text().replace(":'migration_007_sha256'", f"'{previous}'").replace(":'migration_sha256'", f"'{checksum}'")
    schema = "module_migration_" + uuid4().hex
    with psycopg.connect(database, autocommit=True) as root:
        root.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            dsn = make_conninfo(database, options=f"-c search_path={schema}")
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, checksum TEXT NOT NULL)")
                conn.execute("INSERT INTO schema_migrations VALUES ('007',%s)", (previous,))
                conn.execute(migration)
                assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [("007",), ("008",)]
                assert conn.execute("SELECT module_id FROM admin_module_registry ORDER BY module_id").fetchall() == [
                    ("get-percentage",), ("gsm-profile-screen",),
                ]
                conn.execute(migration)
                assert conn.execute("SELECT count(*) FROM admin_module_registry").fetchone() == (2,)
        finally:
            root.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
