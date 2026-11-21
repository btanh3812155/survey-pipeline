# -*- coding: utf-8 -*-
"""Wave 3 route-level duplicate/conflict proof against disposable PostgreSQL."""

import asyncio
import hashlib
import io
import os
from pathlib import Path

import httpx
import psycopg
import pytest
from PIL import Image, ImageDraw
from psycopg_pool import AsyncConnectionPool
import pytest_asyncio
from core.embedding_executor import embedding_executor_lifespan
from services import submission_pipeline

from config import settings
from routers import survey as survey_router
from tests.postgres_harness import apply_schema, resolve_test_database_url
from main import app

MIGRATION = Path(__file__).resolve().parents[1] / "scripts" / "migrations" / "001_submission_attempts.sql"


@pytest_asyncio.fixture(autouse=True)
async def _bounded_inference_lifespan():
    async with embedding_executor_lifespan():
        yield


def _rejected_evaluation(monkeypatch):
    async def evaluate(_input, _dependencies, _prepared):
        return submission_pipeline.SubmissionEvaluation(
            normalized_media={}, ai_result={}, status="REJECTED", reason="test evaluation",
            is_approved=False, blur_score=None, weekly_income={},
            income_totals={"total_gross_revenue": 0, "total_net_income": 0, "total_trips": 0},
            target_image=None,
        )
    monkeypatch.setattr(submission_pipeline, "_evaluate", evaluate)


def _require_pg():
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("UPGRADE_TEST_DATABASE_URL missing while UPGRADE_TEST_REQUIRE=1")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL for Wave 3 route coverage")
    return resolve_test_database_url()


def _jpeg(size=(600, 800), color=(255, 255, 255)):
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    for i in range(100, 700, 20):
        draw.line([(50, i), (550, i)], fill=(0, 0, 0), width=4)
    out = io.BytesIO()
    image.save(out, format="JPEG")
    return out.getvalue()


class _FlakyConnection:
    def __init__(self, conn, state):
        self._conn = conn
        self._state = state

    async def commit(self):
        await self._conn.commit()
        if self._state["trip"]:
            self._state["trip"] = False
            raise ConnectionError("simulated commit acknowledgement loss")

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _FlakyConnectionContext:
    def __init__(self, pool, state):
        self._pool = pool
        self._state = state
        self._ctx = None

    async def __aenter__(self):
        self._ctx = self._pool.connection()
        conn = await self._ctx.__aenter__()
        return _FlakyConnection(conn, self._state)

    async def __aexit__(self, *args):
        return await self._ctx.__aexit__(*args)


class _FlakyPool:
    def __init__(self, pool):
        self._pool = pool
        self.state = {"trip": True}

    def connection(self):
        return _FlakyConnectionContext(self._pool, self.state)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_route_replays_after_commit_ack_loss_on_fresh_connection(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    _rejected_evaluation(monkeypatch)
    dsn = _require_pg()
    checksum = hashlib.sha256(MIGRATION.read_bytes()).hexdigest()
    sync = psycopg.connect(dsn, autocommit=False)
    try:
        apply_schema(sync)
        sync.execute(MIGRATION.read_text(encoding="utf-8").replace(":'migration_sha256'", f"'{checksum}'"))
        sync.commit()
    finally:
        sync.close()
    pool = AsyncConnectionPool(conninfo=dsn, min_size=1, max_size=2, open=False)
    await pool.open()
    await pool.wait()
    flaky = _FlakyPool(pool)
    monkeypatch.setattr(survey_router, "get_db_pool", lambda: flaky)
    monkeypatch.setattr(survey_router, "get_redis_client", lambda: None)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    key = f"ROUTE-ACK-{os.urandom(4).hex()}"
    data = {
        "driver_phone": "0988123456", "license_plate": "29E-123.45",
        "driver_name": "Tài xế Test Mode", "consent_choice": "Tôi đồng ý",
        "signature_png": "data:image/png;base64,mock", "vehicle_type": "Ô tô điện VinFast (GSM)",
        "car_model": "VF 5 Plus", "gross_revenue": "650000", "odometer_km": "50",
        "form_id": "income_survey", "idempotency_key": key, "is_test_mode": "false",
    }
    sid = None
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/survey/submit-webrtc", data=data,
                files={"survey_photo": ("test.jpg", _jpeg(), "image/jpeg")},
            )
            assert response.status_code == 200, response.text
            sid = response.json()["submission_id"]
            retry = await client.post(
                "/api/v1/survey/submit-webrtc", data=data,
                files={"survey_photo": ("test.jpg", _jpeg(), "image/jpeg")},
            )
            assert retry.status_code == 200
            assert retry.json()["submission_id"] == sid
        async with pool.connection() as conn:
            row = await conn.execute(
                "SELECT count(*) FROM survey_submissions WHERE submission_id=%s;", (sid,)
            )
            assert await row.fetchone() == (1,)
            await conn.execute("DELETE FROM outbox_events WHERE aggregate_id=%s;", (sid,))
            await conn.execute("DELETE FROM survey_submissions WHERE submission_id=%s;", (sid,))
            await conn.execute("DELETE FROM submission_idempotency WHERE idempotency_key=%s;", (key,))
            await conn.commit()
    finally:
        await pool.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_no_key_requests_remain_compatible_without_cross_request_dedupe(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    _rejected_evaluation(monkeypatch)
    dsn = _require_pg()
    checksum = hashlib.sha256(MIGRATION.read_bytes()).hexdigest()
    sync = psycopg.connect(dsn, autocommit=False)
    try:
        apply_schema(sync)
        sync.execute(MIGRATION.read_text(encoding="utf-8").replace(":'migration_sha256'", f"'{checksum}'"))
        sync.commit()
    finally:
        sync.close()
    pool = AsyncConnectionPool(conninfo=dsn, min_size=1, max_size=2, open=False)
    await pool.open()
    await pool.wait()
    monkeypatch.setattr(survey_router, "get_db_pool", lambda: pool)
    monkeypatch.setattr(survey_router, "get_redis_client", lambda: None)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    data = {
        "driver_phone": "0988123456", "license_plate": "29E-123.45",
        "driver_name": "Tài xế Test Mode", "consent_choice": "Tôi đồng ý",
        "signature_png": "data:image/png;base64,mock", "vehicle_type": "Ô tô điện VinFast (GSM)",
        "car_model": "VF 5 Plus", "gross_revenue": "650000", "odometer_km": "50",
        "form_id": "income_survey", "is_test_mode": "false",
    }
    ids = []
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for _ in range(2):
                response = await client.post(
                    "/api/v1/survey/submit-webrtc", data=data,
                    files={"survey_photo": ("test.jpg", _jpeg(), "image/jpeg")},
                )
                assert response.status_code == 200, response.text
                ids.append(response.json()["submission_id"])
        assert len(set(ids)) == 2
        async with pool.connection() as conn:
            row = await conn.execute(
                "SELECT count(*) FROM survey_submissions WHERE submission_id = ANY(%s);", (ids,)
            )
            assert await row.fetchone() == (2,)
            await conn.execute("DELETE FROM outbox_events WHERE aggregate_id = ANY(%s);", (ids,))
            await conn.execute("DELETE FROM survey_submissions WHERE submission_id = ANY(%s);", (ids,))
            await conn.commit()
    finally:
        await pool.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrent_same_key_requests_produce_one_committed_submission(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    _rejected_evaluation(monkeypatch)
    dsn = _require_pg()
    checksum = hashlib.sha256(MIGRATION.read_bytes()).hexdigest()
    sync = psycopg.connect(dsn, autocommit=False)
    try:
        apply_schema(sync)
        sync.execute(MIGRATION.read_text(encoding="utf-8").replace(":'migration_sha256'", f"'{checksum}'"))
        sync.commit()
    finally:
        sync.close()
    pool = AsyncConnectionPool(conninfo=dsn, min_size=1, max_size=4, open=False)
    await pool.open()
    await pool.wait()
    monkeypatch.setattr(survey_router, "get_db_pool", lambda: pool)
    monkeypatch.setattr(survey_router, "get_redis_client", lambda: None)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    key = f"ROUTE-CONCURRENT-{os.urandom(4).hex()}"
    data = {
        "driver_phone": "0988123456", "license_plate": "29E-123.45",
        "driver_name": "Tài xế Test Mode", "consent_choice": "Tôi đồng ý",
        "signature_png": "data:image/png;base64,mock", "vehicle_type": "Ô tô điện VinFast (GSM)",
        "car_model": "VF 5 Plus", "gross_revenue": "650000", "odometer_km": "50",
        "form_id": "income_survey", "idempotency_key": key, "is_test_mode": "false",
    }
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first, second = await asyncio.gather(
                client.post("/api/v1/survey/submit-webrtc", data=data,
                            files={"survey_photo": ("test.jpg", _jpeg(), "image/jpeg")}),
                client.post("/api/v1/survey/submit-webrtc", data=data,
                            files={"survey_photo": ("test.jpg", _jpeg(), "image/jpeg")}),
            )
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["submission_id"] == second.json()["submission_id"]
        async with pool.connection() as conn:
            row = await conn.execute(
                "SELECT count(*), min(state) FROM submission_idempotency WHERE idempotency_key=%s;",
                (key,),
            )
            assert await row.fetchone() == (1, "COMMITTED")
            row = await conn.execute(
                "SELECT count(*) FROM survey_submissions WHERE submission_id=%s;",
                (first.json()["submission_id"],),
            )
            assert await row.fetchone() == (1,)
            await conn.execute("DELETE FROM outbox_events WHERE aggregate_id=%s;", (first.json()["submission_id"],))
            await conn.execute("DELETE FROM survey_submissions WHERE submission_id=%s;", (first.json()["submission_id"],))
            await conn.execute("DELETE FROM submission_idempotency WHERE idempotency_key=%s;", (key,))
            await conn.commit()
    finally:
        await pool.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_route_replays_same_payload_and_rejects_changed_payload(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    _rejected_evaluation(monkeypatch)
    dsn = _require_pg()
    checksum = hashlib.sha256(MIGRATION.read_bytes()).hexdigest()
    sync = psycopg.connect(dsn, autocommit=False)
    try:
        apply_schema(sync)
        sql = MIGRATION.read_text(encoding="utf-8").replace(":'migration_sha256'", f"'{checksum}'")
        sync.execute(sql)
        sync.commit()
    finally:
        sync.close()

    pool = AsyncConnectionPool(conninfo=dsn, min_size=1, max_size=2, open=False)
    await pool.open()
    await pool.wait()
    monkeypatch.setattr(survey_router, "get_db_pool", lambda: pool)
    monkeypatch.setattr(survey_router, "get_redis_client", lambda: None)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    phone = "0988123456"
    key = f"ROUTE-IDEMP-{os.urandom(4).hex()}"
    image_bytes = _jpeg()
    base = {
        "driver_phone": phone,
        "license_plate": "29E-123.45",
        "driver_name": "Tài xế Test Mode",
        "consent_choice": "Tôi đồng ý",
        "signature_png": "data:image/png;base64,mock",
        "vehicle_type": "Ô tô điện VinFast (GSM)",
        "car_model": "VF 5 Plus",
        "gross_revenue": "650000",
        "odometer_km": "50",
        "form_id": "income_survey",
        "idempotency_key": key,
        "is_test_mode": "false",
    }
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post(
                "/api/v1/survey/submit-webrtc", data=base,
                files={"survey_photo": ("test.jpg", image_bytes, "image/jpeg")},
            )
            assert first.status_code == 200, first.text
            first_payload = first.json()
            second = await client.post(
                "/api/v1/survey/submit-webrtc", data=base,
                files={"survey_photo": ("test.jpg", image_bytes, "image/jpeg")},
            )
            assert second.status_code == 200, second.text
            assert second.json() == first_payload
            changed = dict(base, gross_revenue="650001")
            third = await client.post(
                "/api/v1/survey/submit-webrtc", data=changed,
                files={"survey_photo": ("test.jpg", image_bytes, "image/jpeg")},
            )
            assert third.status_code == 409, third.text
            alias_retry = {k: v for k, v in base.items() if k != "signature_png"}
            alias_retry["driver_signature"] = "data:image/png;base64,mock"
            alias_response = await client.post(
                "/api/v1/survey/submit-webrtc", data=alias_retry,
                files={"survey_photo": ("test.jpg", image_bytes, "image/jpeg")},
            )
            assert alias_response.status_code == 200, alias_response.text
            changed_sig = dict(base, driver_signature="data:image/png;base64,changed")
            assert (await client.post(
                "/api/v1/survey/submit-webrtc", data=changed_sig,
                files={"survey_photo": ("test.jpg", image_bytes, "image/jpeg")},
            )).status_code == 409
            changed_gps = dict(base, gps_latitude="10.1", gps_longitude="106.2")
            assert (await client.post(
                "/api/v1/survey/submit-webrtc", data=changed_gps,
                files={"survey_photo": ("test.jpg", image_bytes, "image/jpeg")},
            )).status_code == 409
            changed_bytes = _jpeg(size=(601, 801), color=(10, 20, 30))
            assert (await client.post(
                "/api/v1/survey/submit-webrtc", data=base,
                files={"survey_photo": ("test.jpg", changed_bytes, "image/jpeg")},
            )).status_code == 409
        async with pool.connection() as conn:
            row = await conn.execute(
                "SELECT state, submission_id FROM submission_idempotency WHERE idempotency_key=%s;",
                (key,),
            )
            assert await row.fetchone() == ("COMMITTED", first_payload["submission_id"])
            await conn.execute("DELETE FROM survey_submissions WHERE submission_id=%s;", (first_payload["submission_id"],))
            await conn.execute("DELETE FROM submission_idempotency WHERE idempotency_key=%s;", (key,))
            await conn.commit()

        # A server-side statement error on a non-required extended column must
        # roll back to the savepoint, then commit the minimal row plus ALL event.
        savepoint_key = f"ROUTE-SAVEPOINT-{os.urandom(4).hex()}"
        savepoint_base = dict(base, idempotency_key=savepoint_key)
        async with pool.connection() as conn:
            await conn.execute("ALTER TABLE survey_submissions RENAME COLUMN action_status TO action_status_legacy;")
            await conn.commit()
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                saved = await client.post(
                    "/api/v1/survey/submit-webrtc", data=savepoint_base,
                    files={"survey_photo": ("test.jpg", image_bytes, "image/jpeg")},
                )
                assert saved.status_code == 200, saved.text
            async with pool.connection() as conn:
                row = await conn.execute(
                    "SELECT s.submission_id, s.status, i.state, "
                    "EXISTS(SELECT 1 FROM outbox_events o WHERE o.aggregate_id=s.submission_id AND o.flow_target='ALL') "
                    "FROM survey_submissions s JOIN submission_idempotency i "
                    "ON i.submission_id=s.submission_id WHERE i.idempotency_key=%s;",
                    (savepoint_key,),
                )
                assert await row.fetchone() == (saved.json()["submission_id"], saved.json()["status"], "COMMITTED", True)
                await conn.execute("DELETE FROM outbox_events WHERE aggregate_id=%s;", (saved.json()["submission_id"],))
                await conn.execute("DELETE FROM survey_submissions WHERE submission_id=%s;", (saved.json()["submission_id"],))
                await conn.execute("DELETE FROM submission_idempotency WHERE idempotency_key=%s;", (savepoint_key,))
                await conn.commit()
        finally:
            async with pool.connection() as conn:
                await conn.execute("ALTER TABLE survey_submissions RENAME COLUMN action_status_legacy TO action_status;")
                await conn.commit()
    finally:
        await pool.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_production_writer_rejects_absent_or_mismatched_ledger_before_media(monkeypatch, tmp_path):
    dsn = _require_pg()
    sync = psycopg.connect(dsn, autocommit=False)
    try:
        apply_schema(sync)
        from core.phone_links import PHONE_LINK_TABLE_STATEMENTS
        for statement in PHONE_LINK_TABLE_STATEMENTS:
            sync.execute(statement)
        sync.execute("DELETE FROM schema_migrations WHERE version='001';")
        sync.commit()
    finally:
        sync.close()
    pool = AsyncConnectionPool(conninfo=dsn, min_size=1, max_size=2, open=False)
    await pool.open()
    await pool.wait()
    monkeypatch.setattr(survey_router, "get_db_pool", lambda: pool)
    monkeypatch.setattr(survey_router, "get_redis_client", lambda: None)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(survey_router, "SUBMISSION_PHOTOS_DIR", str(tmp_path / "photos"))
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            absent = await client.post(
                "/api/v1/survey/submit-webrtc",
                data={"driver_phone": "0988123456", "form_id": "income_survey", "idempotency_key": "gate-absent"},
            )
            assert absent.status_code == 503, absent.text
            assert not (tmp_path / "photos").exists()
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO schema_migrations(version, checksum) VALUES ('001', %s) "
                    "ON CONFLICT (version) DO UPDATE SET checksum=EXCLUDED.checksum;",
                    ("0" * 64,),
                )
                await conn.commit()
            mismatched = await client.post(
                "/api/v1/survey/submit-webrtc",
                data={"driver_phone": "0988123456", "form_id": "income_survey", "idempotency_key": "gate-mismatch"},
            )
            assert mismatched.status_code == 503, mismatched.text
            assert not (tmp_path / "photos").exists()
    finally:
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM schema_migrations WHERE version='001';")
            await conn.commit()
        await pool.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_actual_route_interruption_uses_first_snapshot_identity_then_replays(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_AI", "1")
    monkeypatch.setattr(
        submission_pipeline, "_evaluate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("evaluation interrupted")),
    )
    dsn = _require_pg()
    checksum = hashlib.sha256(MIGRATION.read_bytes()).hexdigest()
    sync = psycopg.connect(dsn, autocommit=False)
    try:
        apply_schema(sync)
        sync.execute(MIGRATION.read_text(encoding="utf-8").replace(":'migration_sha256'", f"'{checksum}'"))
        sync.commit()
    finally:
        sync.close()
    pool = AsyncConnectionPool(conninfo=dsn, min_size=1, max_size=2, open=False)
    await pool.open()
    await pool.wait()
    monkeypatch.setattr(survey_router, "get_db_pool", lambda: pool)
    monkeypatch.setattr(survey_router, "get_redis_client", lambda: None)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(survey_router, "SUBMISSION_PHOTOS_DIR", str(tmp_path / "photos"))
    monkeypatch.setattr(
        submission_pipeline, "_evaluate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("evaluation interrupted")),
    )
    key = f"ROUTE-INTERRUPT-{os.urandom(4).hex()}"
    image_bytes = _jpeg()
    data = {
        "driver_phone": "0988123456", "license_plate": "29E-123.45",
        "driver_name": "Tài xế Test Mode", "consent_choice": "Tôi đồng ý",
        "signature_png": "data:image/png;base64,mock", "vehicle_type": "Ô tô điện VinFast (GSM)",
        "car_model": "VF 5 Plus", "gross_revenue": "650000", "odometer_km": "50",
        "form_id": "income_survey", "idempotency_key": key, "is_test_mode": "false",
    }
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            recovered = await client.post(
                "/api/v1/survey/submit-webrtc", data=data,
                files={"survey_photo": ("test.jpg", image_bytes, "image/jpeg")},
            )
            assert recovered.status_code == 200, recovered.text
            assert recovered.json()["status"] == "MANUAL_REVIEW"
            assert recovered.json()["mode"] == "PROD_MODE"
            assert recovered.json()["driver_phone"] == "0988123456"
            assert {"driver_tag", "detected_plate", "gross_revenue", "net_income", "trip_count", "ocr_data"} <= set(recovered.json())
            sid = recovered.json()["submission_id"]
            retry = await client.post(
                "/api/v1/survey/submit-webrtc", data=data,
                files={"survey_photo": ("test.jpg", image_bytes, "image/jpeg")},
            )
            assert retry.status_code == 200 and retry.json() == recovered.json()
        async with pool.connection() as conn:
            row = await conn.execute("SELECT count(*) FROM survey_submissions WHERE submission_id=%s;", (sid,))
            assert await row.fetchone() == (1,)
            row = await conn.execute(
                "SELECT state, submission_id FROM submission_idempotency WHERE idempotency_key=%s;", (key,)
            )
            assert await row.fetchone() == ("COMMITTED", sid)
            row = await conn.execute(
                "SELECT count(*) FROM outbox_events WHERE aggregate_id=%s AND flow_target='ALL';", (sid,)
            )
            assert await row.fetchone() == (1,)
            assert (tmp_path / "photos" / sid).is_dir()
            await conn.execute("DELETE FROM outbox_events WHERE aggregate_id=%s;", (sid,))
            await conn.execute("DELETE FROM survey_submissions WHERE submission_id=%s;", (sid,))
            await conn.execute("DELETE FROM submission_idempotency WHERE idempotency_key=%s;", (key,))
            await conn.commit()
    finally:
        await pool.close()
