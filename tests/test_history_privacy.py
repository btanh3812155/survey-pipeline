import json

import pytest
from fastapi.testclient import TestClient

from config import settings
from core.cache import BoundedTTLCache
from main import app
from routers import survey as survey_mod


ADMIN_USER = "history-test-admin"
ADMIN_PASSWORD = "history-test-password"
PHONE = "0999888777"
OTHER_PHONE = "0900000000"


class _FakeRedis:
    def __init__(self):
        self.counts = {}

    async def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, _key, _seconds):
        return True


class _FakePool:
    def __init__(self, rows_by_phone):
        self.rows_by_phone = rows_by_phone
        self.phone = None

    def connection(self):
        return self

    def cursor(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _sql, params):
        self.phone = params[0]

    async def fetchall(self):
        return self.rows_by_phone.get(self.phone, [])


@pytest.fixture

def history_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", ADMIN_PASSWORD)

    history_file = tmp_path / "dev_history.json"
    history_file.write_text(
        json.dumps([{
            "driver_phone": PHONE,
            "submission_id": "DEV-HISTORY-1",
            "status": "REJECTED",
            "reason": "Review required",
            "submitted_at": "2026-01-02T03:04:05+00:00",
        }]),
        encoding="utf-8",
    )
    monkeypatch.setattr(survey_mod, "DEV_STORAGE_FILE", str(history_file))

    pool = _FakePool({PHONE: [("DB-HISTORY-1", "APPROVED", None, None)]})
    redis = _FakeRedis()
    limiter = BoundedTTLCache(max_size=8, ttl_seconds=60)
    monkeypatch.setattr(survey_mod, "get_db_pool", lambda: pool)
    monkeypatch.setattr(survey_mod, "get_redis_client", lambda: redis)
    monkeypatch.setattr(survey_mod, "HISTORY_RATE_LIMITER", limiter)

    return TestClient(app), pool, redis, limiter


def test_history_rejects_anonymous_and_wrong_auth_before_processing(history_env, monkeypatch):
    client, _pool, _redis, limiter = history_env
    side_effects = {"storage": 0, "pool": 0, "redis": 0}
    verified_phone_mapping = {PHONE: 9241303232}

    def fail_if_called(name):
        def fail(*_args, **_kwargs):
            side_effects[name] += 1
            raise AssertionError(f"{name} accessed before authentication")
        return fail

    monkeypatch.setattr(survey_mod, "get_db_pool", fail_if_called("pool"))
    monkeypatch.setattr(survey_mod, "get_redis_client", fail_if_called("redis"))
    monkeypatch.setattr(
        survey_mod,
        "os",
        type("FakeOS", (), {"path": type("FakePath", (), {"exists": fail_if_called("storage")})})(),
    )

    anonymous = client.get(f"/api/v1/surveys/history?phone={PHONE}")
    invalid_phone = client.get("/api/v1/surveys/history?phone=not-a-phone")
    wrong_auth = client.get(
        f"/api/v1/surveys/history?phone={PHONE}", auth=("not-admin", "wrong-password")
    )

    assert verified_phone_mapping[PHONE] == 9241303232
    assert anonymous.status_code == 401
    assert invalid_phone.status_code == 401
    assert wrong_auth.status_code == 401
    assert side_effects == {"storage": 0, "pool": 0, "redis": 0}
    assert len(limiter) == 0


def test_admin_history_is_not_limited_to_a_linked_phone(history_env, monkeypatch):
    client, pool, redis, _limiter = history_env
    verified_phone_mapping = {PHONE: 9241303232}
    auth = (ADMIN_USER, ADMIN_PASSWORD)

    response = client.get(
        f"/api/v1/surveys/history?phone=%200999-888-777%20", auth=auth
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["phone"] == PHONE
    assert payload["count"] == 2
    assert {row["mode"] for row in payload["attempts"]} == {"DEV_TEST", "PRODUCTION"}
    assert {row["submission_id"] for row in payload["attempts"]} == {
        "DEV-HISTORY-1", "DB-HISTORY-1"
    }
    assert pool.phone == PHONE

    other_phone_response = client.get(
        f"/api/v1/surveys/history?phone={OTHER_PHONE}", auth=auth
    )
    assert other_phone_response.status_code == 200
    assert other_phone_response.json()["phone"] == OTHER_PHONE
    assert verified_phone_mapping.get(OTHER_PHONE) is None
    assert len(redis.counts) == 1
    assert PHONE not in next(iter(redis.counts))
    assert OTHER_PHONE not in next(iter(redis.counts))


def test_missing_redis_does_not_grant_public_history_access(history_env, monkeypatch):
    client, _pool, _redis, limiter = history_env
    monkeypatch.setattr(survey_mod, "get_redis_client", lambda: None)

    anonymous = client.get(f"/api/v1/surveys/history?phone={PHONE}")
    assert anonymous.status_code == 401
    assert len(limiter) == 0

    authenticated = client.get(
        f"/api/v1/surveys/history?phone={PHONE}", auth=(ADMIN_USER, ADMIN_PASSWORD)
    )
    assert authenticated.status_code == 200
    assert len(limiter) == 1


def test_submission_id_status_remains_public_and_pii_free(history_env, monkeypatch, tmp_path):
    client, _pool, _redis, _limiter = history_env
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(survey_mod, "STATUS_RATE_LIMITER", BoundedTTLCache(max_size=8, ttl_seconds=60))
    data = tmp_path / "data"
    data.mkdir()
    (data / "dev_submissions.json").write_text(json.dumps([{
        "submission_id": "DEV-H1-STATUS",
        "status": "APPROVED",
        "reason": "Approved",
        "driver_phone": PHONE,
        "driver_name": "Private name",
        "submitted_at": "2026-01-02T03:04:05+00:00",
    }]), encoding="utf-8")
    response = client.get("/api/v1/survey/status/DEV-H1-STATUS")
    assert response.status_code == 200
    assert response.json() == {
        "submission_id": "DEV-H1-STATUS", "status": "APPROVED", "reason": "Approved",
        "updated_at": "2026-01-02T03:04:05+00:00", "is_test_mode": True,
    }
    assert PHONE not in response.text
    assert "Private name" not in response.text
    assert client.get("/api/v1/survey/status/DEV-H1-UNKNOWN").status_code == 404
