# -*- coding: utf-8 -*-
"""Wave 1 runtime dependency, readiness and test-authority guardrails.

All providers are fakes; the disposable PostgreSQL harness covers real schema
behavior separately in test_postgres_harness.py.
"""

from contextlib import asynccontextmanager

import asyncio
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import main
from config import (
    BUILTIN_ADMIN_PASSWORD,
    BUILTIN_APP_SECRET,
    BUILTIN_DATABASE_URL,
    Settings,
    settings,
)
from routers import system as system_router
from routers import survey as survey_router
from services import submission_pipeline
from services.form_engine import FormQuestion, FormSchema, FormSettings, QuestionType


def _settings(**overrides):
    values = {
        "ENVIRONMENT": "test",
        "ADMIN_PASSWORD": "secure-admin-password",
        "APP_SECRET": "x" * 32,
        "DATABASE_URL": "postgresql://postgres:test@db:5432/testdb",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_environment_is_required_and_recognized(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    with pytest.raises(ValidationError):
        _settings(ENVIRONMENT="staging")


def test_production_rejects_builtin_credentials_secret_and_database():
    with pytest.raises(ValidationError):
        _settings(ENVIRONMENT="production", ADMIN_PASSWORD=BUILTIN_ADMIN_PASSWORD)
    with pytest.raises(ValidationError):
        _settings(ENVIRONMENT="production", APP_SECRET=BUILTIN_APP_SECRET)
    with pytest.raises(ValidationError):
        _settings(ENVIRONMENT="production", APP_SECRET="short")
    with pytest.raises(ValidationError):
        _settings(ENVIRONMENT="production", DATABASE_URL="sqlite:///test.db")
    with pytest.raises(ValidationError):
        _settings(ENVIRONMENT="production", DATABASE_URL=BUILTIN_DATABASE_URL)


def test_production_validation_does_not_echo_secret_values():
    valid = _settings(ENVIRONMENT="production")
    assert valid.ENVIRONMENT == "production"
    with pytest.raises(ValidationError) as exc:
        _settings(ENVIRONMENT="production", ADMIN_PASSWORD=BUILTIN_ADMIN_PASSWORD)
    assert BUILTIN_ADMIN_PASSWORD not in str(exc.value)
    assert "secure-admin-password" not in str(exc.value)

    with pytest.raises(ValidationError) as secret_exc:
        _settings(
            ENVIRONMENT="production",
            APP_SECRET="short",
            DATABASE_URL="postgresql://dummy:unrelated-db-password@db:5432/testdb",
        )
    assert "unrelated-db-password" not in str(secret_exc.value)


class _FakeCursor:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows

    async def fetchone(self):
        return self.row

    async def fetchall(self):
        return self.rows or []


class _FakeConn:
    def __init__(self, row=None, fail=False, checksum=None):
        self.row = row
        self.fail = fail
        self.checksum = checksum

    async def execute(self, sql, *_args, **_kwargs):
        if self.fail:
            raise RuntimeError("database lost")
        if "to_regclass" in str(sql):
            return _FakeCursor(self.row)
        if "information_schema.columns" in str(sql):
            return _FakeCursor(rows=[("media_manifest",), ("media_version",)])
        if "schema_migrations" in str(sql):
            return _FakeCursor(row=(self.checksum if self.checksum is not None else system_router.MIGRATION_001_CHECKSUM,))
        return _FakeCursor(self.row)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakePool:
    def __init__(self, row=None, fail=False, checksum=None):
        self.row = row
        self.fail = fail
        self.checksum = checksum

    def connection(self):
        return _FakeConn(self.row, self.fail, self.checksum)


def test_health_is_liveness_only_and_ready_requires_db_schema(monkeypatch):
    client = TestClient(main.app)
    assert client.get("/health").status_code == 200

    monkeypatch.setattr(system_router, "get_db_pool", lambda: None)
    assert client.get("/ready").status_code == 503

    tables = tuple(f"table_{i}" for i in range(8))
    monkeypatch.setattr(system_router, "get_db_pool", lambda: _FakePool(row=tables))
    assert client.get("/ready").status_code == 200

    monkeypatch.setattr(system_router, "get_db_pool", lambda: _FakePool(row=(None,) + tables[1:]))
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["reason"] == "schema_incomplete"
    assert "survey_submissions" in response.json()["missing"]

    for index, table in ((4, "driver_registered_vehicles"), (5, "phone_telegram_links"),
                         (6, "submission_idempotency"), (7, "schema_migrations")):
        missing = tables[:index] + (None,) + tables[index + 1:]
        monkeypatch.setattr(system_router, "get_db_pool", lambda row=missing: _FakePool(row=row))
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["reason"] == "schema_incomplete"
        assert table in response.json()["missing"]


def test_ready_fails_closed_on_migration_checksum_mismatch(monkeypatch):
    client = TestClient(main.app)
    tables = tuple(f"table_{i}" for i in range(8))
    monkeypatch.setattr(system_router, "get_db_pool", lambda: _FakePool(row=tables, checksum="0" * 64))
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["reason"] == "schema_incomplete"
    assert response.json()["missing_migration"] is None


def test_ready_returns_503_on_database_connection_loss(monkeypatch):
    client = TestClient(main.app)
    monkeypatch.setattr(system_router, "get_db_pool", lambda: _FakePool(fail=True))
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["reason"] == "postgresql_unavailable"


def test_production_submission_fails_closed_without_database(monkeypatch):
    client = TestClient(main.app)
    monkeypatch.setattr(survey_router.settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(survey_router, "get_db_pool", lambda: None)
    response = client.post(
        "/api/v1/survey/submit-webrtc",
        data={"form_id": "TB_GSM_2026_Nov_Test"},
    )
    assert response.status_code == 503


def test_test_and_bypass_flags_require_server_side_authority(monkeypatch):
    normal_schema = FormSchema(
        form_id="normal",
        title="Normal",
        settings=FormSettings(is_test_mode=False),
        questions=[FormQuestion(id="q", title="Q", type=QuestionType.TEXT)],
    )
    test_schema = FormSchema(
        form_id="test",
        title="Test",
        settings=FormSettings(is_test_mode=True),
        questions=[FormQuestion(id="q", title="Q", type=QuestionType.TEXT)],
    )
    monkeypatch.setattr(survey_router.settings, "ENVIRONMENT", "production")

    with pytest.raises(Exception) as normal:
        survey_router._authorize_test_flags(normal_schema, True, False)
    assert getattr(normal.value, "status_code", None) == 403
    with pytest.raises(Exception) as bypass:
        survey_router._authorize_test_flags(normal_schema, False, True)
    assert getattr(bypass.value, "status_code", None) == 400

    survey_router._authorize_test_flags(test_schema, True, True)
    monkeypatch.setattr(survey_router.settings, "ENVIRONMENT", "development")
    survey_router._authorize_test_flags(normal_schema, True, False)


@pytest.mark.asyncio
async def test_production_startup_failure_is_fatal_and_closes_partial_resources(monkeypatch):
    class FakeHttp:
        closed = False

        async def aclose(self):
            self.closed = True

    class FakeRedis:
        closed = False

        async def ping(self):
            raise RuntimeError("redis unavailable")

        async def aclose(self):
            self.closed = True

    class FailingPool:
        async def open(self):
            raise RuntimeError("postgres unavailable")

        async def close(self):
            pass

    http_client = FakeHttp()
    redis_client = FakeRedis()
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **_kwargs: http_client)
    monkeypatch.setattr(main.aioredis, "from_url", lambda *_args, **_kwargs: redis_client)
    monkeypatch.setattr(main, "AsyncConnectionPool", lambda **_kwargs: FailingPool())
    monkeypatch.setattr(main.settings, "ENVIRONMENT", "production")

    with pytest.raises(RuntimeError, match="production PostgreSQL startup failed"):
        async with main.lifespan(main.app):
            pass
    assert http_client.closed is True
    assert redis_client.closed is True


def test_production_request_flags_are_rejected_by_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(survey_router, "refresh_form_registry_from_disk", lambda: None)
    monkeypatch.setitem(survey_router.FORM_REGISTRY, "runtime-normal", FormSchema(
        form_id="runtime-normal", title="Normal", settings=FormSettings(is_test_mode=False), questions=[]))
    client = TestClient(main.app)
    monkeypatch.setattr(survey_router, "get_db_pool", lambda: _FakePool(row=("ok",)))

    test_resp = client.post(
        "/api/v1/survey/submit-webrtc",
        data={"form_id": "runtime-normal", "is_test_mode": "true"},
    )
    assert test_resp.status_code == 403
    assert "not authorized" in test_resp.text.lower() or "không được phép" in test_resp.text.lower()

    bypass_resp = client.post(
        "/api/v1/survey/submit-webrtc",
        data={"form_id": "runtime-normal", "bypass_img_check": "true"},
    )
    assert bypass_resp.status_code == 400




@pytest.mark.asyncio
async def test_local_idempotency_cache_is_scoped_by_form_mode_and_phone():
    from services.submission_pipeline import SubmissionDependencies, SubmissionInput, _cached_response, _prepare

    cache = {}
    dependencies = SubmissionDependencies(
        vision=None, media_root="", dev_storage_file="", append_dev_record=lambda _: None,
        image_max_dimension=600, blur_threshold=40, legacy_cache=cache,
    )
    original = SubmissionInput(form_id="form-a", driver_phone="0988123456", idempotency_key="same")
    prepared = _prepare(original)
    cache_key = f"idemp:{prepared.attempt.scope}:{prepared.attempt.key}"
    cache[cache_key] = json.dumps({
        "fingerprint": prepared.attempt.fingerprint,
        "response": {"status": "APPROVED", "submission_id": "CACHED"},
    })

    replay = await _cached_response(original, dependencies, prepared)
    assert replay is not None and replay.replayed and replay.submission_id == "CACHED"
    for other in (
        replace(original, form_id="form-b"),
        replace(original, is_test_mode=True),
        replace(original, driver_phone="0912345678"),
    ):
        assert await _cached_response(other, dependencies, _prepare(other)) is None
def test_cached_response_does_not_bypass_test_flag_authorization(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(survey_router, "refresh_form_registry_from_disk", lambda: None)
    monkeypatch.setitem(survey_router.FORM_REGISTRY, "runtime-normal", FormSchema(
        form_id="runtime-normal", title="Normal", settings=FormSettings(is_test_mode=False), questions=[]))
    from unittest.mock import AsyncMock

    client = TestClient(main.app)
    monkeypatch.setattr(survey_router, "get_db_pool", lambda: _FakePool(row=("ok",)))
    monkeypatch.setattr(survey_router, "get_redis_client", lambda: None)
    use_case = AsyncMock()
    monkeypatch.setattr(survey_router, "process_submission", use_case)
    response = client.post(
        "/api/v1/survey/submit-webrtc",
        data={"form_id": "runtime-normal", "is_test_mode": "true", "idempotency_key": "cache-auth-bypass"},
    )
    assert response.status_code == 403
    use_case.assert_not_awaited()




class _CloseTrackingPool:
    closed = False

    async def open(self):
        raise asyncio.CancelledError()

    async def close(self):
        self.closed = True


class _CancelOnConnectionPool(_CloseTrackingPool):
    async def open(self):
        return None

    def connection(self):
        class Conn:
            async def __aenter__(self):
                raise asyncio.CancelledError()

            async def __aexit__(self, *_exc):
                return False

        return Conn()


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_cls", [_CloseTrackingPool, _CancelOnConnectionPool])
async def test_cancelled_startup_closes_unpublished_pool(monkeypatch, pool_cls):
    class FakeHttp:
        closed = False

        async def aclose(self):
            self.closed = True

    class FakeRedis:
        closed = False

        async def ping(self):
            return None

        async def aclose(self):
            self.closed = True

    http_client = FakeHttp()
    redis_client = FakeRedis()
    pool = pool_cls()
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **_kwargs: http_client)
    monkeypatch.setattr(main.aioredis, "from_url", lambda *_args, **_kwargs: redis_client)
    monkeypatch.setattr(main, "AsyncConnectionPool", lambda **_kwargs: pool)
    monkeypatch.setattr(main.settings, "ENVIRONMENT", "production")
    main.state.db_pool = None
    main.db_pool = None

    with pytest.raises(asyncio.CancelledError):
        async with main.lifespan(main.app):
            pass
    assert pool.closed is True
    assert http_client.closed is True
    assert redis_client.closed is True


