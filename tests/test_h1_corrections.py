# -*- coding: utf-8 -*-
"""Focused H1 corrections: webhook provenance, original time, DEV form identity, phone history parity."""

import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import core.phone_links as phone_links
import services.telegram_polling as telegram_polling
from config import BUILTIN_TELEGRAM_WEBHOOK_SECRET, settings
from main import app
from routers import survey as survey_router
from routers import webhooks
from services.admin_moderator import _dev_record_dossier
from services.form_engine import FormQuestion, FormSchema, FormSettings, QuestionType
from services.telegram_polling import DriverBotRunner
from tests.postgres_harness import SCHEMA_PATH, resolve_test_database_url


SELF_ID = 9241303232
SELF_PHONE = "0932090967"


def _contact_update(contact_user_id=SELF_ID, phone=SELF_PHONE):
    return {
        "update_id": 1,
        "message": {
            "message_id": 2,
            "date": 0,
            "chat": {"id": SELF_ID, "type": "private"},
            "from": {"id": SELF_ID, "first_name": "Driver", "username": "driver"},
            "contact": {
                "phone_number": phone,
                "first_name": "Driver",
                "user_id": contact_user_id,
            },
        },
    }


def _test_form(form_id="h1_correction_form", source="question"):
    return FormSchema(
        form_id=form_id,
        title="H1 corrections",
        settings=FormSettings(
            is_test_mode=True,
            decree13_required=False,
            ctv_ref_enabled=False,
            tg_response_source=source,
            tg_response_question_id="phone",
        ),
        questions=[FormQuestion(id="phone", title="Phone", type=QuestionType.TEXT, required=True)],
    )


def _webhook_client():
    return TestClient(app)


def test_production_webhook_missing_or_builtin_secret_fails_closed(monkeypatch):
    client = _webhook_client()
    runner = SimpleNamespace(handle_update=AsyncMock())
    monkeypatch.setattr(webhooks, "get_driver_bot_runner", lambda: runner)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")

    for secret in ("", BUILTIN_TELEGRAM_WEBHOOK_SECRET):
        monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", secret)
        resp = client.post(
            "/webhook/telegram/driver",
            json=_contact_update(),
            headers={"X-Telegram-Bot-Api-Secret-Token": secret},
        )
        assert resp.status_code == 503
    runner.handle_update.assert_not_awaited()


def test_production_webhook_requires_matching_header_before_dispatch(monkeypatch):
    client = _webhook_client()
    runner = SimpleNamespace(handle_update=AsyncMock())
    monkeypatch.setattr(webhooks, "get_driver_bot_runner", lambda: runner)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "real-test-webhook-secret")

    assert client.post("/webhook/telegram/driver", json=_contact_update()).status_code == 401
    assert client.post(
        "/webhook/telegram/driver",
        json=_contact_update(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    ).status_code == 401
    assert client.post(
        "/webhook/telegram/driver",
        content=b"not json",
        headers={
            "X-Telegram-Bot-Api-Secret-Token": "wrong",
            "Content-Type": "application/json",
        },
    ).status_code == 401
    runner.handle_update.assert_not_awaited()

    ok = client.post(
        "/webhook/telegram/driver",
        json=_contact_update(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "real-test-webhook-secret"},
    )
    assert ok.status_code == 200
    runner.handle_update.assert_awaited_once()


def test_authenticated_webhook_reaches_self_contact_check_and_rejects_third_party(monkeypatch):
    client = _webhook_client()
    runner = DriverBotRunner(bot_token="mock", db_pool=object())
    monkeypatch.setattr(webhooks, "get_driver_bot_runner", lambda: runner)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "real-test-webhook-secret")
    upsert = AsyncMock(return_value=True)
    send = AsyncMock()
    headers = {"X-Telegram-Bot-Api-Secret-Token": "real-test-webhook-secret"}

    with patch.object(runner, "send_message", send), \
         patch("core.phone_links.upsert_phone_telegram_link", upsert):
        self_resp = client.post("/webhook/telegram/driver", json=_contact_update(), headers=headers)
        third_party = client.post(
            "/webhook/telegram/driver",
            json=_contact_update(contact_user_id=SELF_ID + 1),
            headers=headers,
        )

    assert self_resp.status_code == 200
    assert third_party.status_code == 200
    upsert.assert_awaited_once_with(
        SELF_PHONE, SELF_ID, source="verified_contact", db_pool=runner.db_pool
    )


@pytest.mark.asyncio
async def test_dev_writer_persists_form_id_and_dossier_keeps_it(monkeypatch, tmp_path):
    form = _test_form()
    monkeypatch.setitem(survey_router.FORM_REGISTRY, form.form_id, form)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", "mock_tg_token")
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", "mock_tg_token")
    monkeypatch.setattr(survey_router, "get_db_pool", lambda: None)
    monkeypatch.setattr(survey_router, "get_redis_client", lambda: None)
    monkeypatch.setattr(survey_router, "SUBMISSION_PHOTOS_DIR", str(tmp_path / "photos"))
    captured = {}
    monkeypatch.setattr(
        survey_router,
        "append_dev_submission",
        lambda record: captured.update(record),
    )
    response = TestClient(app).post(
        "/api/v1/survey/submit-webrtc",
        data={
            "form_id": form.form_id,
            "phone": SELF_PHONE,
            "driver_phone": SELF_PHONE,
            "is_test_mode": "true",
        },
    )
    assert response.status_code == 200, response.text
    assert captured["form_id"] == form.form_id
    assert captured["submitted_at"]
    submitted_at = datetime.fromisoformat(captured["submitted_at"])
    assert submitted_at.utcoffset() is not None

    dossier = _dev_record_dossier(captured["submission_id"], captured)
    assert dossier["form_id"] == form.form_id
    assert dossier["submitted_at"] == captured["submitted_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["question", "none", "ctv"])
async def test_dev_writer_record_through_dossier_and_moderation_policies(monkeypatch, tmp_path, source):
    form = _test_form(form_id=f"h1_dev_mod_{source}", source=source)
    monkeypatch.setitem(survey_router.FORM_REGISTRY, form.form_id, form)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", "mock_tg_token")
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", "mock_tg_token")
    monkeypatch.setattr(survey_router, "get_db_pool", lambda: None)
    monkeypatch.setattr(survey_router, "get_redis_client", lambda: None)
    monkeypatch.setattr(survey_router, "SUBMISSION_PHOTOS_DIR", str(tmp_path / ("photos_" + source)))
    captured = {}
    monkeypatch.setattr(survey_router, "append_dev_submission", lambda record: captured.update(record))
    data = {
        "form_id": form.form_id,
        "phone": SELF_PHONE,
        "driver_phone": SELF_PHONE,
        "is_test_mode": "true",
    }
    if source == "ctv":
        data["ctv_code"] = "TUAN_LM_HCM_001"
    response = TestClient(app).post("/api/v1/survey/submit-webrtc", data=data)
    assert response.status_code == 200, response.text
    assert captured["form_id"] == form.form_id

    dossier = _dev_record_dossier(captured["submission_id"], captured)
    assert dossier["form_id"] == form.form_id
    runner = DriverBotRunner(bot_token="mock", db_pool=None)
    dispatch = AsyncMock()
    with patch.object(telegram_polling, "query_submission_dossier", AsyncMock(return_value=dossier)), \
         patch("routers.survey.dispatch_submission_telegram_notifications", dispatch), \
         patch.object(runner, "send_message", AsyncMock()):
        await runner._notify_parties_of_decision(captured["submission_id"], "REJECTED", "retry")

    if source == "none":
        dispatch.assert_not_awaited()
        return
    dispatch.assert_awaited_once()
    kwargs = dispatch.await_args.kwargs
    assert kwargs["form_id"] == form.form_id
    if source == "question":
        assert kwargs["recipient_kind"] == "driver"
        assert kwargs["collaborator_id"] == SELF_PHONE
    else:
        assert kwargs["recipient_kind"] == "ctv"
        assert kwargs["collaborator_id"] == "TUAN_LM_HCM_001"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_original_time_and_phone_forms_control_history(monkeypatch):
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("UPGRADE_TEST_DATABASE_URL missing while UPGRADE_TEST_REQUIRE=1")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL for real PostgreSQL H1 coverage")

    from psycopg_pool import AsyncConnectionPool

    from tests.postgres_harness import apply_schema
    dsn = resolve_test_database_url()
    pool = AsyncConnectionPool(conninfo=dsn, min_size=1, max_size=2, open=False)
    old_id = f"TEST-H1-OLD-{secrets.token_hex(4)}"
    new_id = f"TEST-H1-NEW-{secrets.token_hex(4)}"
    other_id = f"TEST-H1-OTHER-{secrets.token_hex(4)}"
    equiv_ids = [f"TEST-H1-EQUIV-{n}-{secrets.token_hex(2)}" for n in range(3)]
    user_id = secrets.randbelow(2**50) + 1
    try:
        await pool.open()
        await pool.wait()
        async with pool.connection() as conn:
            await conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
            await conn.commit()
        assert await phone_links.ensure_phone_link_schema(pool)
        def routing(target):
            return json.dumps({"_delivery_policy": {"source": "question", "question_id": "notify"}, "notify": target})
        before = datetime.now(timezone.utc) - timedelta(minutes=5)
        after = datetime.now(timezone.utc) + timedelta(minutes=5)
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO survey_submissions (submission_id, driver_phone, status, created_at, form_data) "
                "VALUES (%s, %s, 'REJECTED', %s, %s::jsonb);",
                (old_id, "+84 932 090 967", before, routing(SELF_PHONE)),
            )
            await conn.execute(
                "INSERT INTO survey_submissions (submission_id, driver_phone, status, created_at, form_data) "
                "VALUES (%s, %s, 'REJECTED', %s, %s::jsonb);",
                (new_id, "0900000000", after, routing([SELF_PHONE, "0900000000"])),
            )
            for equiv_id, raw_phone in zip(equiv_ids, ("84932090967", "0084932090967", "932090967")):
                await conn.execute(
                    "INSERT INTO survey_submissions (submission_id, driver_phone, status, created_at, form_data) "
                    "VALUES (%s, %s, 'REJECTED', %s, %s::jsonb);",
                    (equiv_id, raw_phone, after, routing(raw_phone)),
                )
            await conn.execute(
                "INSERT INTO survey_submissions (submission_id, driver_phone, status, created_at, form_data) "
                "VALUES (%s, %s, 'REJECTED', %s, '{}'::jsonb);",
                (other_id, SELF_PHONE, after),
            )
            await conn.commit()
        assert await phone_links.upsert_phone_telegram_link(SELF_PHONE, user_id, source="verified_contact", db_pool=pool)
        assert await phone_links.get_telegram_id_for_phone(SELF_PHONE, db_pool=pool, submitted_at=before) is None
        assert await phone_links.get_telegram_id_for_phone(SELF_PHONE, db_pool=pool, submitted_at=after) == user_id

        runner = DriverBotRunner(bot_token="mock", db_pool=pool)
        send = AsyncMock()
        with patch.object(runner, "send_message", send):
            await runner.handle_driver_surveys_lookup(user_id, user_id)
        text = send.await_args.args[1]
        assert old_id not in text
        assert new_id in text
        for equiv_id in equiv_ids:
            assert equiv_id in text
        assert other_id not in text
        buttons = send.await_args.kwargs["reply_markup"]["inline_keyboard"]
        assert {button["callback_data"] for row in buttons for button in row} == {
            f"driver_fix:{sid}:flat_0" for sid in equiv_ids
        }
        denied = AsyncMock()
        with patch.object(runner, "answer_callback_query", denied):
            await runner.handle_update({"callback_query": {
                "id": "forged-repair", "data": f"driver_fix:{new_id}:flat_0", "from": {"id": user_id},
                "message": {"chat": {"id": user_id, "type": "private"}, "message_id": 1},
            }})
        assert user_id not in runner.pending_fixes
        assert denied.await_args.kwargs["show_alert"] is True

        # Later moderation delivery uses the original submission time, not the
        # time of the moderation decision.
        import main as main_module
        from routers import survey as survey_runtime
        sender = AsyncMock(return_value=True)
        monkeypatch.setattr(phone_links, "get_db_pool", lambda: pool)
        monkeypatch.setattr(survey_runtime.settings, "TELEGRAM_DRIVER_BOT_TOKEN", "test-driver-token")
        monkeypatch.setattr(survey_runtime.settings, "TELEGRAM_ADMIN_BOT_TOKEN", None)
        monkeypatch.setattr(survey_runtime, "send_telegram_direct_message", sender)
        monkeypatch.setattr(main_module, "send_telegram_direct_message", sender)
        await survey_runtime.dispatch_submission_telegram_notifications(
            submission_id=old_id,
            status_str="APPROVED",
            collaborator_id=SELF_PHONE,
            recipient_kind="driver",
            submitted_at=before,
        )
        sender.assert_not_awaited()
        await survey_runtime.dispatch_submission_telegram_notifications(
            submission_id=new_id,
            status_str="APPROVED",
            collaborator_id=SELF_PHONE,
            recipient_kind="driver",
            submitted_at=after,
        )
        sender.assert_awaited_once()
    finally:
        try:
            async with pool.connection() as conn:
                await conn.execute(
                    "DELETE FROM survey_submissions WHERE submission_id = ANY(%s);",
                    ([old_id, new_id, other_id, *equiv_ids],),
                )
                await conn.execute("DELETE FROM phone_telegram_links WHERE telegram_user_id = %s;", (user_id,))
                await conn.commit()
        finally:
            await pool.close()


@pytest.mark.asyncio
async def test_dev_history_normalizes_equivalent_phone_forms(monkeypatch, tmp_path):
    phone = "0932090967"
    verified_since = datetime.now(timezone.utc) - timedelta(minutes=10)
    records = [
        {"submission_id": "DEV-NORM-OLD", "driver_phone": phone, "status": "APPROVED", "submitted_at": (verified_since - timedelta(minutes=1)).isoformat()},
        {"submission_id": "DEV-NORM-OTHER", "driver_phone": "0900000000", "status": "APPROVED", "submitted_at": datetime.now(timezone.utc).isoformat()},
        {"submission_id": "DEV-NORM-1", "driver_phone": "+84 932 090 967", "status": "APPROVED", "submitted_at": datetime.now(timezone.utc).isoformat()},
        {"submission_id": "DEV-NORM-2", "driver_phone": "84932090967", "status": "APPROVED", "submitted_at": datetime.now(timezone.utc).isoformat()},
        {"submission_id": "DEV-NORM-3", "driver_phone": "0084932090967", "status": "APPROVED", "submitted_at": datetime.now(timezone.utc).isoformat()},
        {"submission_id": "DEV-NORM-4", "driver_phone": "932090967", "status": "APPROVED", "submitted_at": datetime.now(timezone.utc).isoformat()},
        {"submission_id": "DEV-NORM-5", "driver_phone": "0932090967", "status": "APPROVED", "submitted_at": datetime.now(timezone.utc).isoformat()},
    ]
    for row in records:
        row["form_data"] = {"_delivery_policy": {"source": "question", "question_id": "notify"}, "notify": row.pop("driver_phone")}
    path = tmp_path / "dev_submissions.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    monkeypatch.setattr(telegram_polling, "DEV_STORAGE_FILE", str(path))
    monkeypatch.setattr(
        phone_links,
        "get_verified_phone_binding_for_telegram_user",
        AsyncMock(return_value=(phone, verified_since)),
    )
    runner = DriverBotRunner(bot_token="mock", db_pool=None)
    send = AsyncMock()
    with patch.object(runner, "send_message", send):
        await runner.handle_driver_surveys_lookup(777, 777)
    text = send.await_args.args[1]
    for expected in ("DEV-NORM-1", "DEV-NORM-2", "DEV-NORM-3", "DEV-NORM-4", "DEV-NORM-5"):
        assert expected in text
    assert "DEV-NORM-OLD" not in text
    assert "DEV-NORM-OTHER" not in text


def test_admin_dev_history_normalizes_legacy_phone_forms(monkeypatch, tmp_path):
    from core.cache import BoundedTTLCache
    from routers import survey as survey_runtime

    path = tmp_path / "dev_submissions.json"
    path.write_text(json.dumps([
        {"submission_id": "DEV-LEGACY-NONCANON", "driver_phone": "+84 932 090 967", "status": "APPROVED", "submitted_at": datetime.now(timezone.utc).isoformat()},
        {"submission_id": "DEV-LEGACY-OTHER", "driver_phone": "0900000000", "status": "APPROVED", "submitted_at": datetime.now(timezone.utc).isoformat()},
    ]), encoding="utf-8")
    monkeypatch.setattr(survey_runtime, "DEV_STORAGE_FILE", str(path))
    monkeypatch.setattr(survey_runtime, "get_db_pool", lambda: None)
    monkeypatch.setattr(survey_runtime, "get_redis_client", lambda: None)
    monkeypatch.setattr(survey_runtime, "HISTORY_RATE_LIMITER", BoundedTTLCache(max_size=8, ttl_seconds=60))
    monkeypatch.setattr(settings, "ADMIN_USERNAME", "h1-admin")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "h1-password")
    response = TestClient(app).get(
        "/api/v1/surveys/history?phone=0932090967", auth=("h1-admin", "h1-password")
    )
    assert response.status_code == 200
    ids = {row["submission_id"] for row in response.json()["attempts"]}
    assert ids == {"DEV-LEGACY-NONCANON"}


def test_actual_dev_writer_uses_server_timestamp_and_form_id(monkeypatch, tmp_path):
    from services.form_engine import ImageCheckConfig

    form_id = "h1_writer_timestamp"
    form = FormSchema(
        form_id=form_id,
        title="H1 writer",
        settings=FormSettings(
            is_test_mode=False,
            decree13_required=False,
            ctv_ref_enabled=False,
            tg_response_source="none",
            image_checks=ImageCheckConfig(
                enable_blur_check=False, enable_ai_vision=False, enable_vehicle_match=False,
                enable_anti_fraud=False, enable_ai_generated_check=False, enable_screen_photo_check=False,
            ),
        ),
        questions=[FormQuestion(id="phone", title="Phone", type=QuestionType.TEXT, required=False)],
    )
    monkeypatch.setitem(survey_router.FORM_REGISTRY, form_id, form)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", "mock_tg_token")
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", "mock_tg_token")
    monkeypatch.setattr(survey_router, "SUBMISSION_PHOTOS_DIR", str(tmp_path / "photos"))

    committed = {}

    class Cursor:
        def __init__(self, conn):
            self.conn = conn
            self.rowcount = -1
            self.row = None

        async def execute(self, sql, params=None):
            self.row = None
            self.rowcount = -1
            if sql.startswith("INSERT INTO driver_identity_vault"):
                self.row = (params[0],)
                self.rowcount = 1
            elif sql == "SAVEPOINT survey_extended_insert":
                assert not self.conn.insert_savepoint
                self.conn.insert_savepoint = True
            elif sql.startswith("INSERT INTO survey_submissions"):
                assert self.conn.insert_savepoint
                column_text = sql.split("(", 1)[1].split(")", 1)[0]
                columns = [item.strip() for item in column_text.split(",")]
                row = dict(zip(columns, params, strict=True))
                sub_id = row["submission_id"]
                self.rowcount = int(sub_id not in self.conn.pending and sub_id not in committed)
                if self.rowcount:
                    self.conn.pending[sub_id] = row
            elif sql == "RELEASE SAVEPOINT survey_extended_insert":
                assert self.conn.insert_savepoint
                self.conn.insert_savepoint = False
            elif sql.startswith("INSERT INTO outbox_events"):
                _, sub_id, payload = params
                assert json.loads(payload)["submission_id"] == sub_id
                assert sub_id in self.conn.pending
                self.row = (1,)
                self.rowcount = 1
            else:
                pytest.fail(f"Unexpected writer SQL: {sql}")
            return self

        async def fetchone(self):
            row, self.row = self.row, None
            return row

    class Conn:
        def __init__(self):
            self.pending = {}
            self.insert_savepoint = False

        @asynccontextmanager
        async def cursor(self):
            yield Cursor(self)

        async def commit(self):
            assert not self.insert_savepoint
            committed.update(self.pending)
            self.pending.clear()

        async def rollback(self):
            self.pending.clear()
            self.insert_savepoint = False

    class Pool:
        @asynccontextmanager
        async def connection(self):
            conn = Conn()
            try:
                yield conn
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()

    pool = Pool()
    monkeypatch.setattr(survey_router, "get_db_pool", lambda: pool)
    monkeypatch.setattr(settings, "DURABLE_WEBRTC_ENABLED", False)
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "")
    monkeypatch.setattr(survey_router, "get_redis_client", lambda: None)
    monkeypatch.setattr(survey_router, "dispatch_action_checklist", AsyncMock())

    ingress_time = datetime(2026, 11, 20, 8, 0, 0, tzinfo=timezone.utc)
    persistence_time = ingress_time + timedelta(hours=6)

    class ControlledClock(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            return ingress_time if cls.calls == 1 else persistence_time

    monkeypatch.setattr(survey_router, "datetime", ControlledClock)
    response = TestClient(app).post(
        "/api/v1/survey/submit-webrtc",
        data={"form_id": form_id, "phone": SELF_PHONE, "driver_phone": SELF_PHONE},
    )
    assert response.status_code == 200, response.text
    row = committed[response.json()["submission_id"]]
    created = row["created_at"]
    assert isinstance(created, datetime)
    assert created.utcoffset() is not None
    assert created == ingress_time
    assert created != persistence_time
    assert row["form_id"] == form_id
