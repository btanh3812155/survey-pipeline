"""Ingress ownership, commit fences, and periodic Drive scheduling."""

import asyncio
import base64
import hashlib
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import psycopg
import psycopg_pool
import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException
from psycopg.rows import dict_row

from core import durable_work
from core.schema_gate import MIGRATION_001_CHECKSUM
from routers import admin_data, webhooks
from services import google_drive_delivery, outbox_exports, submission_media
from tests.postgres_harness import apply_schema, resolve_test_database_url
from workers import outbox_worker


@pytest.fixture
def database():
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("disposable PostgreSQL is required")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL")
    dsn = resolve_test_database_url()
    migrations = Path(__file__).resolve().parents[1] / "scripts" / "migrations"
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        apply_schema(conn)
        for name in ("001_submission_attempts", "002_outbox_delivery"):
            sql = (migrations / f"{name}.sql").read_bytes()
            conn.execute(
                sql.decode().replace(":'migration_sha256'", f"'{hashlib.sha256(sql).hexdigest()}'")
                .replace(":'migration_001_sha256'", f"'{MIGRATION_001_CHECKSUM}'")
            )
        ids = []
        try:
            yield dsn, conn, ids
        finally:
            conn.execute("DELETE FROM outbox_events WHERE aggregate_id=ANY(%s)", (ids,))
            conn.execute("DELETE FROM system_audit_log WHERE entity_id=ANY(%s)", (ids,))


@pytest.fixture
def lark(monkeypatch, tmp_path):
    key = "lark-ingress-test-key"
    monkeypatch.setattr(webhooks.settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(webhooks.settings, "LARK_ENCRYPT_KEY", key)
    monkeypatch.setattr(webhooks.settings, "DURABLE_LARK_ENABLED", True)
    monkeypatch.setattr(webhooks.settings, "R2_PRIVATE_BUCKET_NAME", "private-evidence")
    monkeypatch.setattr(webhooks.settings, "R2_BUCKET_NAME", "public-surveys")
    monkeypatch.setattr(webhooks, "get_redis_client", lambda: None)
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(tmp_path / "private"))
    monkeypatch.setattr(webhooks, "sanitize_and_normalize_image", lambda *a, **kw: (b"normalized", None))
    monkeypatch.setattr(webhooks, "compute_laplacian_blur_score", lambda *a, **kw: (True, 42.0))
    extract = AsyncMock(return_value={})
    monkeypatch.setattr(webhooks, "vision_client", SimpleNamespace(extract_facts=extract))
    monkeypatch.setattr(webhooks, "evaluate_survey_compliance", lambda f: SimpleNamespace(
        reason_code="APPROVED", missing_fields=[], collaborator_note=None,
    ))
    legacy_telegram, legacy_r2 = AsyncMock(), AsyncMock()
    monkeypatch.setattr(webhooks, "send_telegram_notification", legacy_telegram)
    monkeypatch.setattr(webhooks, "r2_client", SimpleNamespace(upload_image=legacy_r2))
    app = FastAPI()
    app.include_router(webhooks.router)
    transport = httpx.ASGITransport(app=app)
    provider_id = uuid4().hex
    payload = {"schema": "2.0", "header": {"event_id": provider_id}, "event": {
        "submission_id": "VN-EXT-" + provider_id, "collaborator_tg_id": 998877,
        "image_base64": base64.b64encode(b"input evidence").decode(),
    }}
    aggregate_id = "lark_" + hashlib.sha256(provider_id.encode()).hexdigest()[:59]

    def signed(body, nonce, signature=None):
        raw = json.dumps(body).encode()
        ts = str(int(time.time()))
        return {"content": raw, "headers": {
            "X-Lark-Request-Timestamp": ts, "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": signature or hashlib.sha256((ts + nonce + key).encode() + raw).hexdigest(),
        }}

    return SimpleNamespace(transport=transport, payload=payload, aggregate_id=aggregate_id,
                           signed=signed, extract=extract, telegram=legacy_telegram, r2=legacy_r2)


@pytest.mark.asyncio
async def test_lark_auth_challenge_and_unavailable_db_never_use_legacy(lark, monkeypatch):
    monkeypatch.setattr(webhooks, "get_db_pool", lambda: None)
    async with httpx.AsyncClient(transport=lark.transport, base_url="http://test") as client:
        url = "/webhook/lark-survey"
        challenge = await client.post(url, json={"type": "url_verification", "challenge": "hello"})
        assert challenge.json() == {"challenge": "hello"}
        assert (await client.post(url, **lark.signed(lark.payload, "auth", "wrong"))).status_code == 401
        no_identity = {"event": {**lark.payload["event"], "event_id": "not-provider-metadata"}}
        assert (await client.post(url, **lark.signed(no_identity, "missing"))).status_code == 400
        assert (await client.post(url, **lark.signed(lark.payload, "unavailable"))).status_code == 503
    lark.extract.assert_not_awaited()
    lark.telegram.assert_not_awaited()
    lark.r2.assert_not_awaited()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_lark_intents_commit_once_and_survive_replay_and_cutover(database, lark, monkeypatch):
    dsn, observer, ids = database
    ids.append(lark.aggregate_id)
    async with psycopg_pool.AsyncConnectionPool(dsn, min_size=1, max_size=3, open=False) as pool:
        monkeypatch.setattr(webhooks, "get_db_pool", lambda: pool)
        async with httpx.AsyncClient(transport=lark.transport, base_url="http://test") as client:
            url = "/webhook/lark-survey"
            first, racing = await asyncio.gather(*(
                client.post(url, **lark.signed(lark.payload, nonce)) for nonce in ("first", "racing")
            ))
            expected = {"status": "processed", "submission_id": lark.payload["event"]["submission_id"],
                        "verdict": "APPROVED"}
            assert first.status_code == racing.status_code == 200
            assert first.json() == racing.json() == expected
            rows = observer.execute("SELECT * FROM outbox_events WHERE aggregate_id=%s", (lark.aggregate_id,)).fetchall()
            assert len(rows) == 2 and {row["aggregate_kind"] for row in rows} == {"lark_event"}
            by_target = {row["flow_target"]: row for row in rows}
            verdict = by_target["TELEGRAM_NOTIFY"]["payload"]
            assert verdict["chat_id"] == 998877 and verdict["message_class"] == "PARTY_VERDICT"
            original_nonce = next(n for n in ("first", "racing") if
                                  hashlib.sha256(n.encode()).hexdigest() == verdict["nonce_hash"])
            assert (await client.post(url, **lark.signed(lark.payload, original_nonce))).status_code == 409
            different = {**lark.payload, "event": {**lark.payload["event"], "collaborator_tg_id": 123}}
            evaluated = lark.extract.await_count
            assert (await client.post(url, **lark.signed(different, "conflict"))).status_code == 409
            monkeypatch.setattr(webhooks.settings, "DURABLE_LARK_ENABLED", False)
            assert (await client.post(url, **lark.signed(lark.payload, "cutover"))).json() == expected
            assert lark.extract.await_count == evaluated
        assert not observer.execute("SELECT 1 FROM survey_submissions WHERE submission_id=ANY(%s)",
                                    ([lark.aggregate_id, expected["submission_id"]],)).fetchone()
    # The worker can resolve and hash the snapshotted evidence without an ingress pool or survey row.
    r2_event = by_target["R2_UPLOAD"]
    assert r2_event["payload"]["destination"] == {"bucket": "private-evidence", "private": True}
    assert r2_event["payload"]["media_manifest"] == verdict["media_manifest"]
    sid, media = outbox_exports._media(r2_event, None)
    assert sid == lark.aggregate_id and media[0]["content"] == b"normalized"
    lark.telegram.assert_not_awaited()
    lark.r2.assert_not_awaited()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_lark_partial_enqueue_rolls_back_and_same_nonce_can_retry(database, lark, monkeypatch):
    dsn, observer, ids = database
    ids.append(lark.aggregate_id)
    real_enqueue = durable_work.enqueue_action

    async def fail_r2(conn, event_id, target, *args, **kwargs):
        if target == "R2_UPLOAD":
            raise psycopg.OperationalError("database interrupted")
        return await real_enqueue(conn, event_id, target, *args, **kwargs)

    async with psycopg_pool.AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False) as pool:
        monkeypatch.setattr(webhooks, "get_db_pool", lambda: pool)
        monkeypatch.setattr(webhooks, "enqueue_action", fail_r2)
        async with httpx.AsyncClient(transport=lark.transport, base_url="http://test") as client:
            args = lark.signed(lark.payload, "retry")
            url = "/webhook/lark-survey"
            assert (await client.post(url, **args)).status_code == 503
            assert not observer.execute("SELECT 1 FROM outbox_events WHERE aggregate_id=%s",
                                        (lark.aggregate_id,)).fetchone()
            monkeypatch.setattr(webhooks, "enqueue_action", real_enqueue)
            assert (await client.post(url, **args)).status_code == 200
    lark.telegram.assert_not_awaited()
    lark.r2.assert_not_awaited()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("sharp, unreadable, reason, extension", [
    (False, False, "BLURRY_IMAGE", ".webp"),
    (False, True, "UNREADABLE_IMAGE", ".bin"),
])
async def test_lark_rejected_image_retains_private_evidence_without_r2(
    database, lark, monkeypatch, sharp, unreadable, reason, extension,
):
    dsn, observer, ids = database
    ids.append(lark.aggregate_id)
    monkeypatch.setattr(webhooks, "compute_laplacian_blur_score", lambda *a, **kw: (sharp, 1.0))
    if unreadable:
        def unsupported(*a, **kw):
            raise ValueError("unsupported image")

        monkeypatch.setattr(webhooks, "sanitize_and_normalize_image", unsupported)
    async with psycopg_pool.AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False) as pool:
        monkeypatch.setattr(webhooks, "get_db_pool", lambda: pool)
        async with httpx.AsyncClient(transport=lark.transport, base_url="http://test") as client:
            result = await client.post("/webhook/lark-survey", **lark.signed(lark.payload, "reject"))
    assert result.status_code == 200
    assert result.json() == {"status": "rejected", "reason": reason, **({} if unreadable else {"variance": 1.0})}
    (verdict,) = observer.execute("SELECT * FROM outbox_events WHERE aggregate_id=%s",
                                 (lark.aggregate_id,)).fetchall()
    assert verdict["flow_target"] == "TELEGRAM_NOTIFY"
    [path] = submission_media.manifest_paths(lark.aggregate_id, verdict["payload"]["media_manifest"])
    assert path.endswith(extension)
    assert Path(path).read_bytes() == (b"input evidence" if unreadable else b"normalized")
    lark.extract.assert_not_awaited()
    lark.telegram.assert_not_awaited()
    lark.r2.assert_not_awaited()


def _drive_app(monkeypatch):
    monkeypatch.setattr(admin_data.settings, "DURABLE_DRIVE_ENABLED", True)
    monkeypatch.setattr(admin_data, "has_google_credentials", lambda: True)
    legacy = AsyncMock()
    monkeypatch.setattr(admin_data, "sync_all_approved_to_drive", legacy)
    # Preserve the route's existing main-module compatibility seam.
    import sys
    if "main" in sys.modules:
        monkeypatch.setattr(sys.modules["main"], "has_google_credentials", lambda: True)
        monkeypatch.setattr(sys.modules["main"], "sync_all_approved_to_drive", legacy)
    app = FastAPI()
    app.include_router(admin_data.router)
    app.dependency_overrides[admin_data.verify_admin_auth] = lambda: "test-admin"
    return app, legacy


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_manual_drive_job_visible_before_queued(database, monkeypatch):
    dsn, observer, ids = database
    app, legacy = _drive_app(monkeypatch)
    async with psycopg_pool.AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False) as pool:
        monkeypatch.setattr(admin_data, "get_db_pool", lambda: pool)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            result = await client.post("/api/v1/admin/sync-drive")
        assert result.status_code == 200 and result.json()["status"] == "QUEUED"
        job_id = result.json()["job_id"]
        ids.append(job_id)
        row = observer.execute("SELECT * FROM outbox_events WHERE aggregate_id=%s", (job_id,)).fetchone()
        assert (row["aggregate_kind"], row["flow_target"], row["event_type"]) == (
            "drive_sync", "GDRIVE_EXPORT", "drive.full_sync",
        )
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_drive_commit_failure_is_503_not_background(monkeypatch):
    _, legacy = _drive_app(monkeypatch)
    conn = SimpleNamespace(commit=AsyncMock(side_effect=psycopg.OperationalError("commit failed")))

    @asynccontextmanager
    async def connection():
        yield conn

    monkeypatch.setattr(admin_data, "get_db_pool", lambda: SimpleNamespace(connection=connection))
    enqueue = AsyncMock(return_value="uncommitted-job")
    monkeypatch.setattr(durable_work, "queue_drive_sync", enqueue)
    background = BackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        await admin_data.trigger_manual_drive_sync(background, "test-admin")
    assert exc.value.status_code == 503 and not background.tasks
    enqueue.assert_awaited_once_with(conn, actor="test-admin")
    conn.commit.assert_awaited_once()
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_periodic_drive_uses_global_lock_and_one_key_per_period(monkeypatch):
    monkeypatch.setattr(admin_data.settings, "DURABLE_DRIVE_ENABLED", True)
    monkeypatch.setattr(google_drive_delivery, "has_google_credentials", lambda: True)
    legacy = AsyncMock()
    monkeypatch.setattr(google_drive_delivery, "sync_all_approved_to_drive", legacy)
    queue = AsyncMock()
    monkeypatch.setattr(durable_work, "queue_drive_sync", queue)
    periods = iter([(False, 10), (True, 10), (True, 10), (True, 11)])

    async def execute(sql, args):
        assert "pg_try_advisory_xact_lock" in sql and "clock_timestamp()" in sql
        assert args == (google_drive_delivery.DRIVE_SYNC_LOCK_KEY, 15.0)
        return SimpleNamespace(fetchone=AsyncMock(return_value=next(periods)))

    conn = SimpleNamespace(execute=execute, commit=AsyncMock())

    @asynccontextmanager
    async def connection():
        yield conn

    pool = SimpleNamespace(connection=connection, open=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", lambda *a, **kw: pool)
    monkeypatch.setattr(outbox_worker, "asyncio", SimpleNamespace(
        sleep=AsyncMock(side_effect=[None, None, None, asyncio.CancelledError()]),
        CancelledError=asyncio.CancelledError,
    ))
    with pytest.raises(asyncio.CancelledError):
        await outbox_worker.run_drive_sync_daemon("unused", interval_sec=15.0)
    keys = [call.kwargs["action_key"] for call in queue.await_args_list]
    assert keys[0] == keys[1] and keys[1] != keys[2] and len(keys) == 3
    assert conn.commit.await_count == 3
    pool.close.assert_awaited_once()
    legacy.assert_not_awaited()
