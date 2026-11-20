# -*- coding: utf-8 -*-
"""Wave 3 migration, idempotency and private-media focused checks."""

import hashlib
import json
import os
import shutil
import secrets
from pathlib import Path

import psycopg
import pytest

from core.submission_attempts import attempt_scope, commit_response, request_fingerprint, reserve
from services import submission_media
from tests.postgres_harness import resolve_test_database_url

MIGRATION = Path(__file__).resolve().parents[1] / "scripts" / "migrations" / "001_submission_attempts.sql"


def _require_pg():
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("UPGRADE_TEST_DATABASE_URL missing while UPGRADE_TEST_REQUIRE=1")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL for Wave 3 PostgreSQL coverage")
    return resolve_test_database_url()


def _migration_sql(checksum: str, fail_before_ledger: bool = False) -> str:
    sql = MIGRATION.read_text(encoding="utf-8").replace(":'migration_sha256'", f"'{checksum}'")
    if fail_before_ledger:
        sql = sql.replace("INSERT INTO schema_migrations", "SELECT 1/0; INSERT INTO schema_migrations", 1)
    return sql


def _apply(conn, checksum: str, fail_before_ledger: bool = False):
    conn.execute(_migration_sql(checksum, fail_before_ledger))
    conn.commit()


@pytest.mark.postgres
def test_migration_is_transactional_restartable_and_checksum_guarded(monkeypatch):
    dsn = _require_pg()
    schema = f"wave3_migration_{secrets.token_hex(4)}"
    checksum = hashlib.sha256(MIGRATION.read_bytes()).hexdigest()
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        conn.execute(f"CREATE SCHEMA {schema}; SET search_path TO {schema}, public;")
        conn.execute("CREATE TABLE survey_submissions (submission_id VARCHAR(64) PRIMARY KEY);")
        _apply(conn, checksum)
        row = conn.execute("SELECT version, checksum FROM schema_migrations").fetchone()
        assert row == ("001", checksum)
        _apply(conn, checksum)  # idempotent rerun
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1

        with pytest.raises(psycopg.errors.RaiseException):
            _apply(conn, "0" * 64)
        conn.rollback()

        with pytest.raises(psycopg.errors.DivisionByZero):
            _apply(conn, checksum, fail_before_ledger=True)
        conn.rollback()
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='survey_submissions' "
            "AND column_name IN ('media_manifest','media_version');",
            (schema,),
        ).fetchone()[0] == 2
    finally:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE;")
        conn.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_attempt_reservation_replay_and_fingerprint_conflict():
    dsn = _require_pg()
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=False)
    try:
        await conn.execute("SELECT 1")
        # Ensure the table exists in this isolated database.
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS submission_idempotency ("
            "scope TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_fingerprint CHAR(64) NOT NULL,"
            "submission_id VARCHAR(64), response_json JSONB, state VARCHAR(16) NOT NULL,"
            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), expires_at TIMESTAMPTZ,"
            "PRIMARY KEY(scope, idempotency_key));"
        )
        await conn.commit()
        scope = f"test:{secrets.token_hex(4)}"
        key = "attempt-key"
        fp1 = request_fingerprint("form", "PROD_MODE", {"a": "1"}, [b"image"])
        fp2 = request_fingerprint("form", "PROD_MODE", {"a": "2"}, [b"image"])
        state, _ = await reserve(conn, scope, key, fp1)
        assert state == "new"
        await commit_response(conn, scope, key, fp1, "SUB-1", {"submission_id": "SUB-1"})
        await conn.commit()
        assert await reserve(conn, scope, key, fp1) == ("replay", {"submission_id": "SUB-1"})
        await conn.rollback()
        assert await reserve(conn, scope, key, fp2) == ("conflict", None)
        await conn.rollback()
    finally:
        await conn.close()


def test_private_media_manifest_and_legacy_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(tmp_path))
    sid = "DEV-PRIVATE-1"
    folder = tmp_path / sid
    folder.mkdir()
    (folder / "slot_0.webp").write_bytes(b"private-bytes")
    legacy = submission_media.manifest_paths(sid, [])
    assert [os.path.basename(p) for p in legacy] == ["slot_0.webp"]
    manifest = submission_media.build_manifest(sid, [{"name": "slot_0.webp", "slot_index": 0}])
    assert manifest[0]["sha256"] == hashlib.sha256(b"private-bytes").hexdigest()
    assert manifest[0]["relative_path"] == "slot_0.webp"
    assert [os.path.basename(p) for p in submission_media.manifest_paths(sid, manifest)] == ["slot_0.webp"]
    with pytest.raises(FileNotFoundError):
        submission_media.safe_media_path(sid, "../outside.webp")
    with pytest.raises(ValueError):
        submission_media.private_submission_dir("../outside")


def test_private_media_only_served_through_authenticated_admin_route(tmp_path, monkeypatch):
    import base64
    from fastapi.testclient import TestClient

    from config import settings
    import main
    from routers import admin_data

    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(tmp_path))
    sid = "LOCKEDPRIVATE1"
    folder = tmp_path / sid
    folder.mkdir()
    (folder / "slot_0.webp").write_bytes(b"private-known-id-bytes")
    monkeypatch.setattr(admin_data, "get_db_pool", lambda: None)
    auth = base64.b64encode(f"{settings.ADMIN_USERNAME}:{settings.ADMIN_PASSWORD}".encode()).decode()
    client = TestClient(main.app)
    headers = {"Authorization": f"Basic {auth}"}
    listing = client.get(f"/api/v1/submissions/{sid}/photos", headers=headers)
    assert listing.status_code == 200
    url = listing.json()["photos"][0]
    served = client.get(url, headers=headers)
    assert served.status_code == 200
    assert served.content == b"private-known-id-bytes"
    assert client.get(url).status_code == 401
    assert client.get(f"/media/{sid}/slot_0.webp").status_code in (401, 404)


FRESH_SCHEMA = Path(__file__).resolve().parents[1] / "schema.sql"


def _schema_conn(schema: str):
    dsn = _require_pg()
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute('CREATE EXTENSION IF NOT EXISTS vector;')
    conn.execute(f"CREATE SCHEMA {schema}; SET search_path TO {schema}, public;")
    return conn


def _drop_schema(conn, schema: str):
    conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE;")
    conn.close()


@pytest.mark.postgres
def test_fresh_install_schema_mirrors_migration_001():
    schema = f"wave3_fresh_{secrets.token_hex(4)}"
    checksum = hashlib.sha256(MIGRATION.read_bytes()).hexdigest()
    conn = _schema_conn(schema)
    try:
        conn.execute(FRESH_SCHEMA.read_text(encoding="utf-8"))
        assert conn.execute(
            "SELECT to_regclass('submission_idempotency'), "
            "to_regclass('schema_migrations');"
        ).fetchone() == ("submission_idempotency", "schema_migrations")
        assert conn.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='survey_submissions' "
            "AND column_name IN ('media_manifest','media_version');",
            (schema,),
        ).fetchone()[0] == 2
        with pytest.raises(psycopg.errors.DivisionByZero):
            _apply(conn, checksum, fail_before_ledger=True)
        conn.rollback()
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 0
        _apply(conn, checksum)
        _apply(conn, checksum)
        assert conn.execute("SELECT version, checksum FROM schema_migrations").fetchone() == ("001", checksum)
    finally:
        _drop_schema(conn, schema)


@pytest.mark.postgres
def test_legacy_disposable_schema_accepts_migration_001():
    legacy = (Path(__file__).resolve().parent / "fixtures" / "schema_pre_wave3.sql").read_text(encoding="utf-8")
    schema = f"wave3_legacy_{secrets.token_hex(4)}"
    checksum = hashlib.sha256(MIGRATION.read_bytes()).hexdigest()
    conn = _schema_conn(schema)
    try:
        conn.execute(legacy)
        assert conn.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='survey_submissions' "
            "AND column_name IN ('media_manifest','media_version');",
            (schema,),
        ).fetchone()[0] == 0
        with pytest.raises(psycopg.errors.DivisionByZero):
            _apply(conn, checksum, fail_before_ledger=True)
        conn.rollback()
        assert conn.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='survey_submissions' "
            "AND column_name IN ('media_manifest','media_version');",
            (schema,),
        ).fetchone()[0] == 0
        _apply(conn, checksum)
        assert conn.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='survey_submissions' "
            "AND column_name IN ('media_manifest','media_version');",
            (schema,),
        ).fetchone()[0] == 2
        assert conn.execute("SELECT to_regclass('submission_idempotency');").fetchone()[0] == "submission_idempotency"
        assert conn.execute("SELECT version, checksum FROM schema_migrations").fetchone() == ("001", checksum)
    finally:
        _drop_schema(conn, schema)


def test_manifest_readers_share_current_revision_and_do_not_use_stale_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(tmp_path))
    sid = "VN-MANIFEST-1"
    folder = tmp_path / sid
    folder.mkdir()
    (folder / "slot_0.jpg").write_bytes(b"stale-legacy")
    (folder / "slot_0_v2.webp").write_bytes(b"current-revision")
    manifest = [{
        "slot_index": 0, "relative_path": "slot_0_v2.webp",
        "sha256": hashlib.sha256(b"current-revision").hexdigest(),
    }]
    from services import admin_moderator
    from services.drive_submission_mirror import plan_submission_mirror
    stale_mirror = tmp_path / "Images" / "form" / "submissions" / sid
    stale_mirror.mkdir(parents=True)
    (stale_mirror / "slot_0.webp").write_bytes(b"stale-mirror")
    assert [os.path.basename(p) for p in admin_moderator._collect_submission_photos(sid, manifest)] == ["slot_0_v2.webp"]
    plan = plan_submission_mirror(
        [{"submission_id": sid, "form_id": "form", "status": "APPROVED", "media_manifest": manifest}],
        images_root=str(tmp_path / "Images"),
        photo_store_dir=str(tmp_path),
    )
    assert [image["name"] for image in plan[0]["images"]] == ["slot_0_v2.webp"]
    legacy = plan_submission_mirror(
        [{"submission_id": sid, "form_id": "form", "status": "APPROVED", "media_manifest": []}],
        images_root=str(tmp_path / "empty-images"),
        photo_store_dir=str(tmp_path),
    )
    assert [image["name"] for image in legacy[0]["images"]] == ["slot_0.jpg", "slot_0_v2.webp"]


def test_drive_backfill_removes_stale_slot_for_manifest_revision(tmp_path, monkeypatch):
    import io
    from PIL import Image
    from services import google_drive_delivery as delivery

    private_root = tmp_path / "private"
    sub_dir = private_root / "VN-MANIFEST-BACKFILL"
    sub_dir.mkdir(parents=True)
    current = io.BytesIO()
    Image.new("RGB", (8, 8), (12, 34, 56)).save(current, format="WEBP")
    (sub_dir / "slot_0_v2.webp").write_bytes(current.getvalue())
    target = tmp_path / "Images" / "form" / "submissions" / "VN-MANIFEST-BACKFILL"
    target.mkdir(parents=True)
    (target / "slot_0.webp").write_bytes(b"old")
    monkeypatch.setattr(delivery, "LOCAL_IMAGES_DIR", str(tmp_path / "Images"))
    monkeypatch.setattr(delivery, "SUBMISSION_PHOTOS_DIR", str(private_root))
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(private_root))
    manifest = [{"slot_index": 0, "relative_path": "slot_0_v2.webp"}]
    changed = delivery.backfill_mirror_images_from_submission_photos({
        "VN-MANIFEST-BACKFILL": {"form_id": "form", "media_manifest": manifest},
    })
    names = sorted(os.listdir(target))
    assert names == ["slot_0_v2.webp"]
    assert changed == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_replacement_requires_bound_requester_and_commits_manifest_with_approval(tmp_path, monkeypatch):
    from psycopg_pool import AsyncConnectionPool
    from core.phone_links import PHONE_LINK_TABLE_STATEMENTS
    from tests.postgres_harness import apply_schema

    dsn = _require_pg()
    sync = psycopg.connect(dsn, autocommit=False)
    try:
        apply_schema(sync)
        for statement in PHONE_LINK_TABLE_STATEMENTS:
            sync.execute(statement)
        sync.commit()
    finally:
        sync.close()
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(tmp_path))
    sid = f"VN-REPLACE-{secrets.token_hex(4).upper()}"
    phone = "0988123456"
    owner, intruder = 700000001, 700000002
    folder = tmp_path / sid
    folder.mkdir()
    (folder / "slot_0.webp").write_bytes(b"old")
    pool = AsyncConnectionPool(conninfo=dsn, min_size=1, max_size=2, open=False)
    await pool.open()
    await pool.wait()
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO survey_submissions "
                "(submission_id, form_id, driver_phone, status, media_manifest, media_version) "
                "VALUES (%s, 'income_survey', %s, 'REJECTED', %s::jsonb, 1);",
                (sid, phone, json.dumps([{"slot_index": 0, "relative_path": "slot_0.webp"}])),
            )
            await conn.execute(
                "INSERT INTO phone_telegram_links(phone_normalized, telegram_user_id, source) "
                "VALUES (%s, %s, 'verified_contact');",
                (phone, owner),
            )
            await conn.commit()
        with pytest.raises(PermissionError):
            await submission_media.write_replacement(
                pool, sid, 0, b"intruder", requester_telegram_id=intruder, approve_reason="bad"
            )
        assert not (folder / "slot_0_v2.webp").exists()
        with pytest.raises(ValueError):
            await submission_media.write_replacement(
                pool, sid, 99, b"unknown-slot", requester_telegram_id=owner, approve_reason="bad"
            )
        assert not (folder / "slot_99_v2.webp").exists()
        name = await submission_media.write_replacement(
            pool, sid, 0, b"approved-replacement", requester_telegram_id=owner, approve_reason="test"
        )
        assert name == "slot_0_v2.webp"
        assert (folder / name).read_bytes() == b"approved-replacement"
        async with pool.connection() as conn:
            row = await conn.execute(
                "SELECT status, media_version, media_manifest FROM survey_submissions WHERE submission_id=%s;",
                (sid,),
            )
            status, version, manifest = await row.fetchone()
            assert status == "APPROVED" and version == 2
            assert manifest[0]["relative_path"] == "slot_0_v2.webp"
            event = await conn.execute(
                "SELECT event_type, flow_target FROM outbox_events WHERE aggregate_id=%s;", (sid,)
            )
            assert await event.fetchone() == ("survey.validated", "ALL")
            await conn.execute("DELETE FROM outbox_events WHERE aggregate_id=%s;", (sid,))
            await conn.execute("DELETE FROM survey_submissions WHERE submission_id=%s;", (sid,))
            await conn.execute("DELETE FROM phone_telegram_links WHERE phone_normalized=%s;", (phone,))
            await conn.commit()
    finally:
        await pool.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_interruption_recovery_shares_one_attempt_owner_and_retains_media(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from psycopg_pool import AsyncConnectionPool
    from core.submission_attempts import attempt_scope
    from routers.survey import _persist_interrupted_submission, INTERRUPTED_CHECK_REASON
    from tests.postgres_harness import apply_schema

    dsn = _require_pg()
    sync = psycopg.connect(dsn, autocommit=False)
    try:
        apply_schema(sync)
        checksum = hashlib.sha256(MIGRATION.read_bytes()).hexdigest()
        sync.execute(MIGRATION.read_text(encoding="utf-8").replace(":'migration_sha256'", f"'{checksum}'"))
        sync.commit()
    finally:
        sync.close()
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(tmp_path))
    sid = f"VN-RECOVERY-{secrets.token_hex(4).upper()}"
    key = f"RECOVERY-{secrets.token_hex(4)}"
    phone = "0988123456"
    folder = tmp_path / sid
    folder.mkdir()
    (folder / "slot_0.webp").write_bytes(b"preserved-media")
    manifest = [{"slot_index": 0, "relative_path": "slot_0.webp", "sha256": hashlib.sha256(b"preserved-media").hexdigest()}]
    fp = request_fingerprint("income_survey", "PROD_MODE", {"driver_phone": phone}, [b"preserved-media"])
    pending = {
        "submission_id": sid,
        "form_id": "income_survey",
        "driver_phone": phone,
        "driver_name": "Tài xế",
        "ctv_code": None,
        "received_at": None,
        "media_manifest": manifest,
        "db_pool": None,
        "is_test_mode": False,
        "attempt_scope": attempt_scope("income_survey", "PROD_MODE", phone),
        "attempt_key": key,
        "attempt_fingerprint": fp,
        "response_snapshot": {"submission_id": sid, "status": "MANUAL_REVIEW"},
        "final_written": False,
    }
    pool = AsyncConnectionPool(conninfo=dsn, min_size=1, max_size=2, open=False)
    await pool.open()
    await pool.wait()
    pending["db_pool"] = pool
    try:
        request = SimpleNamespace(state=SimpleNamespace(pending_submission=pending))
        await _persist_interrupted_submission(request)
        assert pending["final_written"] is True
        assert (folder / "slot_0.webp").read_bytes() == b"preserved-media"
        async with pool.connection() as conn:
            row = await conn.execute(
                "SELECT status, rejection_reason, media_manifest FROM survey_submissions WHERE submission_id=%s;",
                (sid,),
            )
            status, reason, stored_manifest = await row.fetchone()
            assert status == "MANUAL_REVIEW" and reason == INTERRUPTED_CHECK_REASON
            assert stored_manifest[0]["relative_path"] == "slot_0.webp"
            row = await conn.execute(
                "SELECT state, submission_id, response_json FROM submission_idempotency WHERE idempotency_key=%s;",
                (key,),
            )
            state, stored_id, response = await row.fetchone()
            assert state == "COMMITTED" and stored_id == sid
            assert response["status"] == "MANUAL_REVIEW"
            count = await conn.execute("SELECT count(*) FROM survey_submissions WHERE submission_id=%s;", (sid,))
            assert await count.fetchone() == (1,)
        pending2 = dict(pending, final_written=False, recovery_response=None)
        await _persist_interrupted_submission(SimpleNamespace(state=SimpleNamespace(pending_submission=pending2)))
        assert pending2["recovery_response"]["submission_id"] == sid
        async with pool.connection() as conn:
            count = await conn.execute("SELECT count(*) FROM survey_submissions WHERE submission_id=%s;", (sid,))
            assert await count.fetchone() == (1,)
            await conn.execute("DELETE FROM outbox_events WHERE aggregate_id=%s;", (sid,))
            await conn.execute("DELETE FROM survey_submissions WHERE submission_id=%s;", (sid,))
            await conn.execute("DELETE FROM submission_idempotency WHERE idempotency_key=%s;", (key,))
            await conn.commit()
    finally:
        await pool.close()


def test_attempt_scope_canonicalizes_phone_aliases_and_separates_modes():
    local = attempt_scope("income_survey", "PROD_MODE", "0988123456")
    country = attempt_scope("income_survey", "PROD_MODE", "+84988123456")
    test_mode = attempt_scope("income_survey", "TEST_DEV_MODE", "0988123456")
    assert local == country
    assert local != test_mode
    assert attempt_scope("other_form", "PROD_MODE", "0988123456") != local


@pytest.mark.postgres
def test_actual_checked_in_migration_command_first_apply_rerun_reject_and_rollback():
    import subprocess

    dsn = _require_pg()
    repo = Path(__file__).resolve().parents[1]
    if not os.environ.get("UPGRADE_TEST_POSTGRES_CONTAINER") and not shutil.which("psql"):
        pytest.skip("psql or disposable container is required for the actual command test")
    command = ["bash", str(repo / "scripts" / "apply_migration.sh"), str(MIGRATION)]
    base_env = os.environ.copy()
    base_env["UPGRADE_TEST_DATABASE_URL"] = dsn

    def run_for(schema: str):
        env = dict(base_env)
        env["UPGRADE_TEST_PGOPTIONS"] = f"-c search_path={schema},public"
        return subprocess.run(command, cwd=repo, env=env, text=True, capture_output=True, timeout=90)

    def inspect(schema: str, query: str, params=None):
        conn = psycopg.connect(dsn, autocommit=True)
        try:
            conn.execute(f"SET search_path TO {schema}, public;")
            return conn.execute(query, params).fetchone()
        finally:
            conn.close()

    good = f"wave3_cli_{secrets.token_hex(4)}"
    rollback = f"wave3_cli_fail_{secrets.token_hex(4)}"
    incompatible = f"wave3_cli_bad_{secrets.token_hex(4)}"
    bad_constraint = f"wave3_cli_bad_constraint_{secrets.token_hex(4)}"
    bad_index = f"wave3_cli_bad_index_{secrets.token_hex(4)}"
    bad_default = f"wave3_cli_bad_default_{secrets.token_hex(4)}"
    bad_weak_constraint = f"wave3_cli_bad_weak_{secrets.token_hex(4)}"
    bad_submission_null = f"wave3_cli_bad_submission_{secrets.token_hex(4)}"
    bad_extra_state = f"wave3_cli_bad_extra_{secrets.token_hex(4)}"
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        conn.execute(f"CREATE SCHEMA {good};")
        conn.execute(f"SET search_path TO {good}, public;")
        conn.execute("CREATE TABLE survey_submissions (submission_id VARCHAR(64) PRIMARY KEY);")
        conn.execute(f"CREATE SCHEMA {rollback};")
        conn.execute(f"SET search_path TO {rollback}, public;")
        conn.execute("CREATE TABLE survey_submissions (submission_id VARCHAR(64) PRIMARY KEY);")
        conn.execute(
            "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, checksum TEXT NOT NULL, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW());"
        )
        conn.execute(
            "CREATE FUNCTION reject_ledger() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'injected ledger failure'; END $$;"
        )
        conn.execute(
            "CREATE TRIGGER reject_ledger_before BEFORE INSERT ON schema_migrations "
            "FOR EACH ROW EXECUTE FUNCTION reject_ledger();"
        )
        conn.execute(f"CREATE SCHEMA {incompatible};")
        conn.execute(f"SET search_path TO {incompatible}, public;")
        conn.execute("CREATE TABLE survey_submissions (submission_id VARCHAR(64) PRIMARY KEY);")
        conn.execute(
            "CREATE TABLE submission_idempotency ("
            "scope INTEGER NOT NULL, idempotency_key TEXT NOT NULL, "
            "request_fingerprint CHAR(64) NOT NULL, submission_id VARCHAR(64), "
            "response_json JSONB, state VARCHAR(16) NOT NULL, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), expires_at TIMESTAMPTZ, "
            "PRIMARY KEY(scope, idempotency_key));"
        )
        for bad_schema, state_check, created_definition, submission_definition in (
            (bad_constraint, "CHECK (state IN ('RESERVED'))", "TIMESTAMPTZ NOT NULL DEFAULT NOW()", "submission_id VARCHAR(64)"),
            (bad_index, "CHECK (state IN ('RESERVED','COMMITTED'))", "TIMESTAMPTZ NOT NULL DEFAULT NOW()", "submission_id VARCHAR(64)"),
            (bad_default, "CHECK (state IN ('RESERVED','COMMITTED'))", "TIMESTAMPTZ NOT NULL", "submission_id VARCHAR(64)"),
            (bad_weak_constraint, "CHECK (state IN ('RESERVED','COMMITTED') AND state <> 'COMMITTED')", "TIMESTAMPTZ NOT NULL DEFAULT NOW()", "submission_id VARCHAR(64)"),
            (bad_submission_null, "CHECK (state IN ('RESERVED','COMMITTED'))", "TIMESTAMPTZ NOT NULL DEFAULT NOW()", "submission_id VARCHAR(64) NOT NULL"),
            (bad_extra_state, "CHECK (state IN ('RESERVED','COMMITTED','EXTRA'))", "TIMESTAMPTZ NOT NULL DEFAULT NOW()", "submission_id VARCHAR(64)"),
        ):
            conn.execute(f"CREATE SCHEMA {bad_schema};")
            conn.execute(f"SET search_path TO {bad_schema}, public;")
            conn.execute("CREATE TABLE survey_submissions (submission_id VARCHAR(64) PRIMARY KEY);")
            conn.execute(
                "CREATE TABLE submission_idempotency ("
                "scope TEXT NOT NULL, idempotency_key TEXT NOT NULL, "
                "request_fingerprint CHAR(64) NOT NULL, " + submission_definition + ", "
                "response_json JSONB, state VARCHAR(16) NOT NULL " + state_check + ", "
                "created_at " + created_definition + ", expires_at TIMESTAMPTZ, "
                "PRIMARY KEY(scope, idempotency_key));"
            )
            if bad_schema == bad_index:
                conn.execute(
                    "CREATE INDEX idx_submission_idempotency_expiry "
                    "ON submission_idempotency (idempotency_key);"
                )
            else:
                conn.execute(
                    "CREATE INDEX idx_submission_idempotency_expiry "
                    "ON submission_idempotency (expires_at) WHERE expires_at IS NOT NULL;"
                )
    finally:
        conn.close()

    try:
        first = run_for(good)
        assert first.returncode == 0, first.stderr
        assert inspect(good, "SELECT version, checksum FROM schema_migrations;") == (
            "001", hashlib.sha256(MIGRATION.read_bytes()).hexdigest()
        )
        assert inspect(good, "SELECT count(*) FROM submission_idempotency;") == (0,)
        second = run_for(good)
        assert second.returncode == 0, second.stderr
        assert inspect(good, "SELECT count(*) FROM schema_migrations;") == (1,)

        conn = psycopg.connect(dsn, autocommit=True)
        try:
            conn.execute(f"SET search_path TO {good}, public;")
            conn.execute("UPDATE schema_migrations SET checksum=%s WHERE version='001';", ("0" * 64,))
        finally:
            conn.close()
        rejected = run_for(good)
        assert rejected.returncode != 0
        assert "checksum mismatch" in (rejected.stderr + rejected.stdout).lower()

        failed = run_for(rollback)
        assert failed.returncode != 0
        assert "injected ledger failure" in (failed.stderr + failed.stdout).lower()
        assert inspect(rollback, f"SELECT to_regclass('{rollback}.submission_idempotency');") == (None,)
        assert inspect(rollback, "SELECT count(*) FROM schema_migrations;") == (0,)
        assert inspect(
            rollback,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='survey_submissions' "
            "AND column_name IN ('media_manifest','media_version');",
            (rollback,),
        ) == (0,)
        incompatible_result = run_for(incompatible)
        assert incompatible_result.returncode != 0
        assert "incompatible submission_idempotency" in (incompatible_result.stderr + incompatible_result.stdout).lower()
        assert inspect(incompatible, f"SELECT to_regclass('{incompatible}.schema_migrations');") == (None,)
        for bad_schema in (bad_constraint, bad_index, bad_default, bad_weak_constraint, bad_submission_null, bad_extra_state):
            bad_result = run_for(bad_schema)
            assert bad_result.returncode != 0
            assert "submission_idempotency" in (bad_result.stderr + bad_result.stdout).lower()
            if bad_schema == bad_extra_state:
                assert "state constraint mismatch" in (bad_result.stderr + bad_result.stdout).lower()
            assert inspect(bad_schema, f"SELECT to_regclass('{bad_schema}.schema_migrations');") == (None,)
    finally:
        conn = psycopg.connect(dsn, autocommit=True)
        try:
            conn.execute(f"DROP SCHEMA IF EXISTS {good} CASCADE;")
            conn.execute(f"DROP SCHEMA IF EXISTS {rollback} CASCADE;")
            conn.execute(f"DROP SCHEMA IF EXISTS {incompatible} CASCADE;")
            conn.execute(f"DROP SCHEMA IF EXISTS {bad_constraint} CASCADE;")
            conn.execute(f"DROP SCHEMA IF EXISTS {bad_index} CASCADE;")
            conn.execute(f"DROP SCHEMA IF EXISTS {bad_default} CASCADE;")
            conn.execute(f"DROP SCHEMA IF EXISTS {bad_weak_constraint} CASCADE;")
            conn.execute(f"DROP SCHEMA IF EXISTS {bad_submission_null} CASCADE;")
            conn.execute(f"DROP SCHEMA IF EXISTS {bad_extra_state} CASCADE;")
        finally:
            conn.close()


def test_media_store_preserves_ingestion_identity_order(tmp_path):
    from routers.survey import _store_submission_photos
    items = [
        {"question_id": "q_second", "slot_index": 4, "flat_index": 7},
        {"question_id": "q_first", "slot_index": 1, "flat_index": 2},
    ]
    manifest = _store_submission_photos(str(tmp_path), [b"second", b"first"], items)
    assert [entry["relative_path"] for entry in manifest] == ["slot_7.bin", "slot_2.bin"]
    assert [(entry["question_id"], entry["slot_index"], entry["flat_index"]) for entry in manifest] == [
        ("q_second", 4, 7), ("q_first", 1, 2),
    ]
    assert [entry["sha256"] for entry in manifest] == [
        hashlib.sha256(b"second").hexdigest(), hashlib.sha256(b"first").hexdigest(),
    ]


def test_media_store_required_write_failure_aborts(tmp_path, monkeypatch):
    import builtins
    from routers.survey import _store_submission_photos
    original_open = builtins.open
    def fail_target(path, mode="r", *args, **kwargs):
        if str(path).endswith(".bin"):
            raise OSError("disk full")
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", fail_target)
    with pytest.raises(OSError):
        _store_submission_photos(str(tmp_path), [b"must-fail"], [{"flat_index": 0}])


def test_manifest_hash_mismatch_and_legacy_symlink_escape_are_denied(tmp_path, monkeypatch):
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(tmp_path))
    sid = "VN-HASH-BOUNDARY"
    folder = tmp_path / sid
    folder.mkdir()
    (folder / "slot_0.webp").write_bytes(b"actual")
    manifest = [{"relative_path": "slot_0.webp", "sha256": hashlib.sha256(b"other").hexdigest()}]
    assert submission_media.manifest_paths(sid, manifest) == []
    outside = tmp_path / "outside.webp"
    outside.write_bytes(b"outside")
    (folder / "slot_1.webp").symlink_to(outside)
    assert [os.path.basename(path) for path in submission_media.manifest_paths(sid, [])] == ["slot_0.webp"]
    with pytest.raises(FileNotFoundError):
        submission_media.safe_media_path(sid, "slot_1.webp")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_telegram_button_upload_readback_preserves_untouched_slots(tmp_path, monkeypatch):
    import io
    from unittest.mock import AsyncMock
    from PIL import Image, ImageDraw
    from psycopg_pool import AsyncConnectionPool
    from core.phone_links import PHONE_LINK_TABLE_STATEMENTS
    from services.telegram_polling import DriverBotRunner
    from tests.postgres_harness import apply_schema

    dsn = _require_pg()
    sync = psycopg.connect(dsn, autocommit=False)
    try:
        apply_schema(sync)
        for statement in PHONE_LINK_TABLE_STATEMENTS:
            sync.execute(statement)
        sync.commit()
    finally:
        sync.close()
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(tmp_path))
    sid = f"VN-TG-REPAIR-{secrets.token_hex(4).upper()}"
    phone, owner = "0988123456", 700000011
    folder = tmp_path / sid
    folder.mkdir()
    (folder / "slot_0.webp").write_bytes(b"old-zero")
    (folder / "slot_1.webp").write_bytes(b"untouched-one")
    pool = AsyncConnectionPool(conninfo=dsn, min_size=1, max_size=2, open=False)
    await pool.open()
    await pool.wait()
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO survey_submissions "
                "(submission_id, form_id, driver_phone, status, media_manifest, media_version, created_at) "
                "VALUES (%s, 'income_survey', %s, 'REJECTED', '[]'::jsonb, 1, NOW() - INTERVAL '1 hour');",
                (sid, phone),
            )
            await conn.execute(
                "INSERT INTO phone_telegram_links(phone_normalized, telegram_user_id, source, updated_at) "
                "VALUES (%s, %s, 'verified_contact', NOW() - INTERVAL '2 hours');",
                (phone, owner),
            )
            await conn.commit()
        image = Image.new("RGB", (600, 800), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        for y in range(100, 700, 20):
            draw.line([(50, y), (550, y)], fill=(0, 0, 0), width=4)
        buf = io.BytesIO()
        image.save(buf, format="JPEG")
        runner = DriverBotRunner(bot_token="mock_driver", db_pool=pool)
        runner.send_message = AsyncMock()
        runner.answer_callback_query = AsyncMock()
        runner.download_telegram_photo = AsyncMock(return_value=buf.getvalue())
        await runner.handle_update({
            "callback_query": {
                "id": "cq-repair", "data": f"driver_fix:{sid}:slot_0",
                "from": {"id": owner}, "message": {"chat": {"id": owner}},
            }
        })
        assert runner.pending_fixes[owner]["slot"] == "slot_0"
        await runner.handle_photo_upload(owner, owner, [{"file_id": "repair-photo"}])
        async with pool.connection() as conn:
            row = await conn.execute(
                "SELECT status, media_version, media_manifest FROM survey_submissions WHERE submission_id=%s;",
                (sid,),
            )
            status, version, manifest = await row.fetchone()
            assert status == "APPROVED" and version == 2
            entries = {int(item["slot_index"]): item for item in manifest}
            assert entries[1]["relative_path"] == "slot_1.webp"
            assert entries[0]["relative_path"] == "slot_0_v2.webp"
            event = await conn.execute(
                "SELECT count(*) FROM outbox_events WHERE aggregate_id=%s AND flow_target='ALL';", (sid,)
            )
            assert await event.fetchone() == (1,)
            await conn.execute("DELETE FROM outbox_events WHERE aggregate_id=%s;", (sid,))
            await conn.execute("DELETE FROM survey_submissions WHERE submission_id=%s;", (sid,))
            await conn.execute("DELETE FROM phone_telegram_links WHERE phone_normalized=%s;", (phone,))
            await conn.commit()
        assert [os.path.basename(p) for p in submission_media.manifest_paths(sid, manifest)] == [
            "slot_0_v2.webp", "slot_1.webp",
        ]
    finally:
        await pool.close()


def test_actual_migration_command_rejects_production_looking_target():
    import subprocess
    repo = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["UPGRADE_TEST_DATABASE_URL"] = "postgresql://postgres:test@127.0.0.1:55432/surveydb_production_test"
    completed = subprocess.run(
        ["bash", str(repo / "scripts" / "apply_migration.sh"), str(MIGRATION)],
        cwd=repo, env=env, text=True, capture_output=True, timeout=30,
    )
    assert completed.returncode != 0
    assert "production-looking" in (completed.stderr + completed.stdout).lower()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_replacement_denied_when_binding_postdates_submission(tmp_path, monkeypatch):
    from psycopg_pool import AsyncConnectionPool
    from core.phone_links import PHONE_LINK_TABLE_STATEMENTS
    from tests.postgres_harness import apply_schema

    dsn = _require_pg()
    sync = psycopg.connect(dsn, autocommit=False)
    try:
        apply_schema(sync)
        for statement in PHONE_LINK_TABLE_STATEMENTS:
            sync.execute(statement)
        sync.commit()
    finally:
        sync.close()
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(tmp_path))
    sid = f"VN-REASSIGN-{secrets.token_hex(4).upper()}"
    phone, owner = "0988123456", 700000021
    folder = tmp_path / sid
    folder.mkdir()
    (folder / "slot_0.webp").write_bytes(b"old")
    pool = AsyncConnectionPool(conninfo=dsn, min_size=1, max_size=2, open=False)
    await pool.open()
    await pool.wait()
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO survey_submissions "
                "(submission_id, form_id, driver_phone, status, media_manifest, media_version, created_at) "
                "VALUES (%s, 'income_survey', %s, 'REJECTED', %s::jsonb, 1, NOW() - INTERVAL '3 days');",
                (sid, phone, json.dumps([{"slot_index": 0, "relative_path": "slot_0.webp"}])),
            )
            await conn.execute(
                "INSERT INTO phone_telegram_links(phone_normalized, telegram_user_id, source, updated_at) "
                "VALUES (%s, %s, 'verified_contact', NOW() - INTERVAL '2 days');",
                (phone, owner),
            )
            await conn.commit()
        with pytest.raises(PermissionError):
            await submission_media.write_replacement(
                pool, sid, 0, b"new", requester_telegram_id=owner, approve_reason="should not apply"
            )
        assert not (folder / "slot_0_v2.webp").exists()
        async with pool.connection() as conn:
            row = await conn.execute("SELECT status FROM survey_submissions WHERE submission_id=%s;", (sid,))
            assert await row.fetchone() == ("REJECTED",)
            await conn.execute("DELETE FROM survey_submissions WHERE submission_id=%s;", (sid,))
            await conn.execute("DELETE FROM phone_telegram_links WHERE phone_normalized=%s;", (phone,))
            await conn.commit()
    finally:
        await pool.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_replacement_uses_flat_identity_with_duplicate_local_slots(tmp_path, monkeypatch):
    from psycopg_pool import AsyncConnectionPool
    from core.phone_links import PHONE_LINK_TABLE_STATEMENTS
    from tests.postgres_harness import apply_schema

    dsn = _require_pg()
    sync = psycopg.connect(dsn, autocommit=False)
    try:
        apply_schema(sync)
        for statement in PHONE_LINK_TABLE_STATEMENTS:
            sync.execute(statement)
        sync.commit()
    finally:
        sync.close()
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(tmp_path))
    sid = f"VN-DUPE-SLOT-{secrets.token_hex(4).upper()}"
    phone, owner = "0988123456", 700000031
    folder = tmp_path / sid
    folder.mkdir()
    (folder / "slot_0.webp").write_bytes(b"q1")
    (folder / "slot_1.webp").write_bytes(b"q2")
    manifest = [
        {"question_id": "q1", "slot_index": 0, "flat_index": 0, "relative_path": "slot_0.webp",
         "sha256": hashlib.sha256(b"q1").hexdigest(), "mime": "image/webp", "media_version": 1},
        {"question_id": "q2", "slot_index": 0, "flat_index": 1, "relative_path": "slot_1.webp",
         "sha256": hashlib.sha256(b"q2").hexdigest(), "mime": "image/webp", "media_version": 1},
    ]
    pool = AsyncConnectionPool(conninfo=dsn, min_size=1, max_size=2, open=False)
    await pool.open()
    await pool.wait()
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO survey_submissions "
                "(submission_id, form_id, driver_phone, status, media_manifest, media_version, created_at) "
                "VALUES (%s, 'income_survey', %s, 'REJECTED', %s::jsonb, 1, NOW() - INTERVAL '1 hour');",
                (sid, phone, json.dumps(manifest)),
            )
            await conn.execute(
                "INSERT INTO phone_telegram_links(phone_normalized, telegram_user_id, source, updated_at) "
                "VALUES (%s, %s, 'verified_contact', NOW() - INTERVAL '2 hours');",
                (phone, owner),
            )
            await conn.commit()
        import io
        from unittest.mock import AsyncMock
        from PIL import Image, ImageDraw
        from services.telegram_polling import DriverBotRunner
        image = Image.new("RGB", (600, 800), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        for y in range(100, 700, 20):
            draw.line([(50, y), (550, y)], fill=(0, 0, 0), width=4)
        buf = io.BytesIO()
        image.save(buf, format="JPEG")
        runner = DriverBotRunner(bot_token="mock_driver", db_pool=pool)
        runner.send_message = AsyncMock()
        runner.answer_callback_query = AsyncMock()
        runner.download_telegram_photo = AsyncMock(return_value=buf.getvalue())
        await runner.handle_update({
            "callback_query": {
                "id": "cq-dupe-slot", "data": f"driver_fix:{sid}:q2__slot_0",
                "from": {"id": owner}, "message": {"chat": {"id": owner}},
            }
        })
        await runner.handle_photo_upload(owner, owner, [{"file_id": "repair-photo"}])
        async with pool.connection() as conn:
            row = await conn.execute(
                "SELECT status, media_manifest FROM survey_submissions WHERE submission_id=%s;", (sid,)
            )
            status, stored = await row.fetchone()
            assert status == "APPROVED"
            assert [(e["question_id"], e["slot_index"], e["flat_index"], e["relative_path"]) for e in stored] == [
                ("q1", 0, 0, "slot_0.webp"),
                ("q2", 0, 1, "slot_1_v2.webp"),
            ]
            assert (folder / "slot_0.webp").read_bytes() == b"q1"
            assert (folder / "slot_1_v2.webp").read_bytes()[:4] == b"RIFF"
            await conn.execute("DELETE FROM outbox_events WHERE aggregate_id=%s;", (sid,))
            await conn.execute("DELETE FROM survey_submissions WHERE submission_id=%s;", (sid,))
            await conn.execute("DELETE FROM phone_telegram_links WHERE phone_normalized=%s;", (phone,))
            await conn.commit()
    finally:
        await pool.close()


def _manifest_webp(path: Path, color=(10, 20, 30)):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="WEBP")
    payload = buf.getvalue()
    path.write_bytes(payload)
    return payload


def test_manifest_mirror_refresh_preserves_existing_on_missing_hash_or_write_failure(tmp_path, monkeypatch):
    from services import google_drive_delivery as delivery

    private_root = tmp_path / "private"
    source_dir = private_root / "VN-MIRROR-FAIL"
    source_dir.mkdir(parents=True)
    target = tmp_path / "Images" / "form" / "submissions" / "VN-MIRROR-FAIL"
    target.mkdir(parents=True)
    (target / "slot_0.webp").write_bytes(b"old-evidence")
    monkeypatch.setattr(delivery, "LOCAL_IMAGES_DIR", str(tmp_path / "Images"))
    monkeypatch.setattr(delivery, "SUBMISSION_PHOTOS_DIR", str(private_root))
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(private_root))
    record = {"form_id": "form", "media_manifest": [{"slot_index": 0, "flat_index": 0, "relative_path": "missing.webp"}]}
    report = {}
    delivery.backfill_mirror_images_from_submission_photos({"VN-MIRROR-FAIL": record}, report=report)
    assert "VN-MIRROR-FAIL" in report["failed"]
    assert (target / "slot_0.webp").read_bytes() == b"old-evidence"

    source = source_dir / "slot_0_v2.webp"
    payload = _manifest_webp(source)
    record["media_manifest"] = [{
        "slot_index": 0, "flat_index": 0, "relative_path": "slot_0_v2.webp",
        "sha256": hashlib.sha256(b"wrong").hexdigest(),
    }]
    report = {}
    delivery.backfill_mirror_images_from_submission_photos({"VN-MIRROR-FAIL": record}, report=report)
    assert "VN-MIRROR-FAIL" in report["failed"]
    assert (target / "slot_0.webp").read_bytes() == b"old-evidence"

    record["media_manifest"][0]["sha256"] = hashlib.sha256(payload).hexdigest()
    original_replace = delivery.os.replace
    failed_once = {"value": False}
    def fail_publish(src, dst):
        if str(dst) == str(target) and not failed_once["value"]:
            failed_once["value"] = True
            raise OSError("publish failed")
        return original_replace(src, dst)
    monkeypatch.setattr(delivery.os, "replace", fail_publish)
    report = {}
    delivery.backfill_mirror_images_from_submission_photos({"VN-MIRROR-FAIL": record}, report=report)
    assert "VN-MIRROR-FAIL" in report["failed"]
    assert (target / "slot_0.webp").read_bytes() == b"old-evidence"


def test_manifest_mirror_refresh_publishes_all_current_slots_and_removes_stale(tmp_path, monkeypatch):
    from services import google_drive_delivery as delivery

    private_root = tmp_path / "private"
    source_dir = private_root / "VN-MIRROR-OK"
    source_dir.mkdir(parents=True)
    target = tmp_path / "Images" / "form" / "submissions" / "VN-MIRROR-OK"
    target.mkdir(parents=True)
    (target / "slot_0.webp").write_bytes(b"stale")
    payload0 = _manifest_webp(source_dir / "slot_0_v2.webp", (1, 2, 3))
    payload1 = _manifest_webp(source_dir / "slot_1_v2.webp", (4, 5, 6))
    manifest = [
        {"slot_index": 0, "flat_index": 0, "relative_path": "slot_0_v2.webp", "sha256": hashlib.sha256(payload0).hexdigest()},
        {"slot_index": 1, "flat_index": 1, "relative_path": "slot_1_v2.webp", "sha256": hashlib.sha256(payload1).hexdigest()},
    ]
    monkeypatch.setattr(delivery, "LOCAL_IMAGES_DIR", str(tmp_path / "Images"))
    monkeypatch.setattr(delivery, "SUBMISSION_PHOTOS_DIR", str(private_root))
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(private_root))
    changed = delivery.backfill_mirror_images_from_submission_photos({
        "VN-MIRROR-OK": {"form_id": "form", "media_manifest": manifest},
    })
    assert changed == 2
    assert sorted(os.listdir(target)) == ["slot_0_v2.webp", "slot_1_v2.webp"]
    assert (target / "slot_0_v2.webp").read_bytes() == payload0
    assert (target / "slot_1_v2.webp").read_bytes() == payload1


def test_actual_migration_rejections_invoke_no_psql_or_docker_exec(tmp_path):
    import subprocess
    repo = Path(__file__).resolve().parents[1]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "commands.log"
    for name in ("psql", "docker"):
        script = bindir / name
        if name == "docker":
            body = (
                "if [ \"$1\" = inspect ]; then\n"
                "  case \"$*\" in\n"
                "    *\".Id\"*) printf 'fake-container-id\\n' ;;\n"
                "    *Labels*) printf '%s\\n' \"${FAKE_DOCKER_LABEL:-true}\" ;;\n"
                "    *) printf '' ;;\n"
                "  esac\n"
                "  exit 0\n"
                "fi\n"
                "exit 0\n"
            )
        else:
            body = "exit 0\n"
        script.write_text(
            "#!/bin/sh\n"
            "printf '%s %s\\n' \"$0\" \"$*\" >> \"$MIGRATION_COMMAND_LOG\"\n"
            + body
        )
        script.chmod(0o755)
    command = ["bash", str(repo / "scripts" / "apply_migration.sh"), str(MIGRATION)]
    base_env = os.environ.copy()
    base_env.update({
        "PATH": f"{bindir}:{base_env.get('PATH', '')}",
        "MIGRATION_COMMAND_LOG": str(log),
        "UPGRADE_TEST_PYTHON": str(repo / ".venv" / "bin" / "python"),
        "UPGRADE_TEST_DATABASE_URL": "postgresql://postgres:test@127.0.0.1:55432/survey_pipeline_test",
    })
    cases = [
        {"UPGRADE_TEST_DATABASE_URL": "postgresql://postgres:test@127.0.0.1:55432/survey_pipeline_test?hostaddr=8.8.8.8"},
        {"UPGRADE_TEST_DATABASE_URL": "postgresql://postgres:test@127.0.0.1:55432/survey_pipeline_test?dbname=production_test"},
        {"UPGRADE_TEST_DATABASE_URL": "host=127.0.0.1 hostaddr=8.8.8.8 dbname=survey_pipeline_test"},
        {"PGHOST": "8.8.8.8"},
        {
            "UPGRADE_TEST_POSTGRES_CONTAINER": "unowned-container",
            "UPGRADE_TEST_DOCKER_ENDPOINT": "unix:///tmp/fake.sock",
            "UPGRADE_TEST_POSTGRES_DB": "survey_pipeline_test",
            "FAKE_DOCKER_LABEL": "false",
        },
        {
            "UPGRADE_TEST_POSTGRES_CONTAINER": "owned-container",
            "UPGRADE_TEST_DOCKER_ENDPOINT": "unix:///tmp/fake.sock",
            "UPGRADE_TEST_POSTGRES_DB": "host=192.0.2.10 dbname=upgrade_test",
            "FAKE_DOCKER_LABEL": "true",
        },
        {
            "UPGRADE_TEST_POSTGRES_CONTAINER": "owned-container",
            "UPGRADE_TEST_DOCKER_ENDPOINT": "tcp://192.0.2.10:2375",
            "UPGRADE_TEST_POSTGRES_DB": "survey_pipeline_test",
            "FAKE_DOCKER_LABEL": "true",
        },
    ]
    for overrides in cases:
        if log.exists():
            log.unlink()
        env = dict(base_env)
        env.update(overrides)
        completed = subprocess.run(command, cwd=repo, env=env, text=True, capture_output=True, timeout=30)
        assert completed.returncode != 0
        commands = log.read_text().splitlines() if log.exists() else []
        assert not any("psql" in line for line in commands), commands
        assert not any("docker exec" in line for line in commands), commands


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_legacy_retry_ignores_uncommitted_revision_artifact(tmp_path, monkeypatch):
    from psycopg_pool import AsyncConnectionPool
    from core.phone_links import PHONE_LINK_TABLE_STATEMENTS
    from tests.postgres_harness import apply_schema

    dsn = _require_pg()
    sync = psycopg.connect(dsn, autocommit=False)
    try:
        apply_schema(sync)
        for statement in PHONE_LINK_TABLE_STATEMENTS:
            sync.execute(statement)
        sync.commit()
    finally:
        sync.close()
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(tmp_path))
    sid = f"VN-RETRY-ARTIFACT-{secrets.token_hex(4).upper()}"
    phone, owner = "0988123456", 700000041
    folder = tmp_path / sid
    folder.mkdir()
    (folder / "slot_0.jpg").write_bytes(b"committed-base")
    (folder / "slot_0_v2.webp").write_bytes(b"uncommitted-artifact")
    (folder / "slot_1.webp").write_bytes(b"untouched")
    pool = AsyncConnectionPool(conninfo=dsn, min_size=1, max_size=2, open=False)
    await pool.open()
    await pool.wait()
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO survey_submissions "
                "(submission_id, form_id, driver_phone, status, media_manifest, media_version, created_at) "
                "VALUES (%s, 'income_survey', %s, 'REJECTED', '[]'::jsonb, 1, NOW() - INTERVAL '1 hour');",
                (sid, phone),
            )
            await conn.execute(
                "INSERT INTO phone_telegram_links(phone_normalized, telegram_user_id, source, updated_at) "
                "VALUES (%s, %s, 'verified_contact', NOW() - INTERVAL '2 hours');",
                (phone, owner),
            )
            await conn.commit()
        name = await submission_media.write_replacement(
            pool, sid, 0, b"retry-replacement", requester_telegram_id=owner, approve_reason="retry"
        )
        assert name == "slot_0_v2.webp"
        assert (folder / "slot_0.jpg").read_bytes() == b"committed-base"
        assert (folder / "slot_1.webp").read_bytes() == b"untouched"
        async with pool.connection() as conn:
            row = await conn.execute(
                "SELECT status, media_version, media_manifest FROM survey_submissions WHERE submission_id=%s;",
                (sid,),
            )
            status, version, manifest = await row.fetchone()
            assert status == "APPROVED" and version == 2
            assert [(e["slot_index"], e["relative_path"]) for e in manifest] == [
                (0, "slot_0_v2.webp"), (1, "slot_1.webp"),
            ]
            await conn.execute("DELETE FROM outbox_events WHERE aggregate_id=%s;", (sid,))
            await conn.execute("DELETE FROM survey_submissions WHERE submission_id=%s;", (sid,))
            await conn.execute("DELETE FROM phone_telegram_links WHERE phone_normalized=%s;", (phone,))
            await conn.commit()
    finally:
        await pool.close()


def test_mirror_refresh_isolates_binary_evidence_and_continues_valid_records(tmp_path, monkeypatch):
    from services import google_drive_delivery as delivery
    from services.drive_submission_mirror import plan_submission_mirror

    private_root = tmp_path / "private"
    binary_dir = private_root / "VN-BINARY-EVIDENCE"
    image_dir = private_root / "VN-VALID-IMAGE"
    binary_dir.mkdir(parents=True)
    image_dir.mkdir(parents=True)
    (binary_dir / "slot_0.bin").write_bytes(b"undecodable-evidence")
    image_payload = _manifest_webp(image_dir / "slot_0.webp", (9, 8, 7))
    target_root = tmp_path / "Images"
    binary_target = target_root / "form" / "submissions" / "VN-BINARY-EVIDENCE"
    image_target = target_root / "form" / "submissions" / "VN-VALID-IMAGE"
    binary_target.mkdir(parents=True)
    image_target.mkdir(parents=True)
    (binary_target / "slot_0.webp").write_bytes(b"stale-binary")
    (image_target / "slot_0.webp").write_bytes(b"stale-image")
    monkeypatch.setattr(delivery, "LOCAL_IMAGES_DIR", str(target_root))
    monkeypatch.setattr(delivery, "SUBMISSION_PHOTOS_DIR", str(private_root))
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(private_root))
    records = {
        "VN-BINARY-EVIDENCE": {
            "form_id": "form", "status": "APPROVED",
            "media_manifest": [{
                "slot_index": 0, "flat_index": 0, "relative_path": "slot_0.bin",
                "sha256": hashlib.sha256(b"undecodable-evidence").hexdigest(),
                "mime": "application/octet-stream",
            }],
        },
        "VN-VALID-IMAGE": {
            "form_id": "form", "status": "APPROVED",
            "media_manifest": [{
                "slot_index": 0, "flat_index": 0, "relative_path": "slot_0.webp",
                "sha256": hashlib.sha256(image_payload).hexdigest(), "mime": "image/webp",
            }],
        },
    }
    report = {}
    changed = delivery.backfill_mirror_images_from_submission_photos(records, report=report)
    assert changed == 1
    assert report["evidence_only"] == ["VN-BINARY-EVIDENCE"]
    assert report["failed"] == {}
    assert (binary_target / "slot_0.webp").read_bytes() == b"stale-binary"
    assert (image_target / "slot_0.webp").read_bytes()[:4] == b"RIFF"
    binary_plan = plan_submission_mirror(
        [{"submission_id": "VN-BINARY-EVIDENCE", "form_id": "form", "status": "APPROVED",
          "media_manifest": records["VN-BINARY-EVIDENCE"]["media_manifest"]}],
        images_root=str(target_root), photo_store_dir=str(private_root),
    )
    assert binary_plan[0]["images"] == []
    assert (binary_dir / "slot_0.bin").read_bytes() == b"undecodable-evidence"


def test_actual_migration_all_docker_operations_pin_host_and_clear_context(tmp_path):
    import subprocess
    repo = Path(__file__).resolve().parents[1]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "docker.log"
    docker = bindir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "printf '%s|%s|%s\\n' \"${DOCKER_HOST:-}\" \"${DOCKER_CONTEXT:-}\" \"$*\" >> \"$MIGRATION_COMMAND_LOG\"\n"
        "if [ \"$1\" = inspect ]; then\n"
        "  case \"$*\" in *\".Id\"*) printf 'fake-container-id\\n' ;; *Labels*) printf 'true\\n' ;; esac\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$1\" = exec ]; then\n"
        "  case \"$*\" in *current_database*) printf 'survey_pipeline_test\\n' ;; esac\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    docker.chmod(0o755)
    psql = bindir / "psql"
    psql.write_text("#!/bin/sh\nexit 0\n")
    psql.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "PATH": f"{bindir}:{env.get('PATH', '')}",
        "MIGRATION_COMMAND_LOG": str(log),
        "UPGRADE_TEST_PYTHON": str(repo / ".venv" / "bin" / "python"),
        "UPGRADE_TEST_DATABASE_URL": "postgresql://postgres:test@127.0.0.1:55432/survey_pipeline_test",
        "UPGRADE_TEST_POSTGRES_CONTAINER": "owned-container",
        "UPGRADE_TEST_POSTGRES_DB": "survey_pipeline_test",
        "UPGRADE_TEST_DOCKER_ENDPOINT": "unix:///tmp/fake.sock",
        "DOCKER_CONTEXT": "hostile-context",
    })
    completed = subprocess.run(
        ["bash", str(repo / "scripts" / "apply_migration.sh"), str(MIGRATION)],
        cwd=repo, env=env, text=True, capture_output=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    lines = [line for line in log.read_text().splitlines() if line]
    assert any("|inspect " in line for line in lines), lines
    assert any("|exec " in line for line in lines), lines
    for line in lines:
        host, context, command = line.split("|", 2)
        assert host == "unix:///tmp/fake.sock", line
        assert context == "", line
