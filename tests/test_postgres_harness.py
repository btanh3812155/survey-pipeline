# -*- coding: utf-8 -*-
"""Wave 0: prove the disposable PostgreSQL/pgvector harness is real and isolated."""

import os

import psycopg
import pytest

from tests.postgres_harness import (
    apply_schema,
    connect,
    database_name,
    has_pgvector,
    reset_harness_table,
    resolve_test_database_url,
)

pytestmark = pytest.mark.postgres


def _require_test_target() -> str:
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("UPGRADE_TEST_DATABASE_URL missing while UPGRADE_TEST_REQUIRE=1")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL to run real PostgreSQL harness tests")
    return resolve_test_database_url()


@pytest.fixture(scope="module")
def dsn() -> str:
    return _require_test_target()


@pytest.fixture(scope="module")
def schema_conn(dsn: str):
    conn = connect(dsn)
    try:
        apply_schema(conn)
        yield conn
    finally:
        conn.close()


def test_target_is_disposable_and_has_pgvector(schema_conn):
    assert database_name(schema_conn).endswith("_test")
    assert has_pgvector(schema_conn), "pgvector extension is required"
    version = schema_conn.execute("SHOW server_version").fetchone()[0]
    assert version


def test_schema_applies_and_creates_core_tables(schema_conn):
    rows = schema_conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    tables = {row[0] for row in rows}
    assert {"survey_submissions", "authorized_users", "outbox_events"} <= tables


def test_savepoint_and_transaction_abort_isolation(schema_conn):
    schema_conn.execute("DROP TABLE IF EXISTS wave0_tx")
    schema_conn.execute("CREATE TEMP TABLE wave0_tx (id INT PRIMARY KEY)")
    schema_conn.commit()
    try:
        with schema_conn.transaction():
            schema_conn.execute("INSERT INTO wave0_tx VALUES (1)")
            schema_conn.execute("SAVEPOINT wave0_sp")
            schema_conn.execute("INSERT INTO wave0_tx VALUES (2)")
            schema_conn.execute("ROLLBACK TO SAVEPOINT wave0_sp")
            assert schema_conn.execute("SELECT count(*) FROM wave0_tx").fetchone()[0] == 1
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass
    assert schema_conn.execute("SELECT count(*) FROM wave0_tx").fetchone()[0] == 0


def test_independent_connections_enforce_unique_key(dsn: str):
    conn1 = connect(dsn)
    conn2 = connect(dsn)
    try:
        reset_harness_table(conn1)
        conn1.execute("INSERT INTO wave0_harness_rows (id) VALUES ('same-key')")
        conn1.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn2.execute("INSERT INTO wave0_harness_rows (id) VALUES ('same-key')")
            conn2.commit()
        conn2.rollback()
        assert conn2.execute("SELECT count(*) FROM wave0_harness_rows").fetchone()[0] == 1
    finally:
        conn2.close()
        conn1.close()


def test_production_looking_targets_are_rejected(monkeypatch):
    cases = [
        "postgresql://postgres:x@146.190.201.32:5432/production",
        "postgresql://postgres:x@localhost:5432/surveydb",
        "postgresql://postgres:x@localhost:5432/survey_missing_suffix",
        "postgresql://postgres:x@db.example.com/survey_test",
    ]
    for value in cases:
        monkeypatch.setenv("UPGRADE_TEST_DATABASE_URL", value)
        with pytest.raises(RuntimeError):
            resolve_test_database_url()

def test_effective_libpq_overrides_are_rejected():
    cases = [
        "postgresql://localhost/survey_pipeline_test?host=example.invalid&dbname=other_database",
        "postgresql://localhost/survey_pipeline_test?hostaddr=146.190.201.32",
        "postgresql://localhost/survey_pipeline_test?service=production",
        "postgresql://localhost/survey_pipeline_test?passfile=/tmp/prod.pgpass",
    ]
    for value in cases:
        with pytest.raises(RuntimeError):
            resolve_test_database_url(value)


def test_explicit_connect_also_validates_effective_target():
    bad = "postgresql://localhost/survey_pipeline_test?host=example.invalid"
    with pytest.raises(RuntimeError):
        connect(bad)



def test_missing_explicit_host_is_rejected():
    with pytest.raises(RuntimeError, match="explicit host"):
        resolve_test_database_url("dbname=survey_pipeline_test")


@pytest.mark.parametrize("env_key", ["PGHOST", "PGHOSTADDR", "PGSERVICE", "PGDATABASE", "PGPASSWORD"])
def test_inherited_libpq_environment_is_rejected_before_connect(monkeypatch, env_key):
    monkeypatch.setenv(env_key, "192.0.2.1")
    called = False

    def fake_connect(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("psycopg.connect must not be reached")

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    with pytest.raises(RuntimeError, match="inherited libpq environment"):
        connect("postgresql://127.0.0.1/survey_pipeline_test")
    assert called is False


def test_missing_host_inherited_from_environment_is_rejected(monkeypatch):
    monkeypatch.setenv("PGHOST", "example.invalid")
    called = False

    def fake_connect(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("psycopg.connect must not be reached")

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    with pytest.raises(RuntimeError, match="inherited libpq environment"):
        connect("dbname=survey_pipeline_test")
    assert called is False
