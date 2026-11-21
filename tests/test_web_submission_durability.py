"""Local WebRTC canary: real route, attempts, intents, replay and web appeals."""

from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from config import settings
from core import outbox_delivery
from core.phone_links import PHONE_LINK_TABLE_STATEMENTS
from routers import survey
from services.form_engine import FormQuestion, FormSchema, FormSettings
from tests.test_outbox_delivery import database


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_web_submit_replay_flag_rollback_and_appeals_keep_one_owner(database, monkeypatch, tmp_path):
    dsn, observer, _, _, _, _ = database
    fid = "canary_" + uuid4().hex
    key = "attempt_" + uuid4().hex
    phone = "09" + f"{uuid4().int % 100_000_000:08d}"
    recipient = 90_000_000_000 + uuid4().int % 1_000_000_000
    for statement in PHONE_LINK_TABLE_STATEMENTS:
        observer.execute(statement)
    observer.execute("INSERT INTO phone_telegram_links(phone_normalized,telegram_user_id,source,updated_at) VALUES (%s,%s,'verified_contact',NOW()-interval '1 hour')", (phone, recipient))
    schema = FormSchema(form_id=fid, title="Canary", settings=FormSettings(
        decree13_required=False, tg_response_source="question", tg_response_question_id="contact_phone",
    ), questions=[FormQuestion(id="contact_phone", title="Phone", type="text", required=True)])
    monkeypatch.setitem(survey.FORM_REGISTRY, fid, schema)
    monkeypatch.setattr(survey, "refresh_form_registry_from_disk", lambda: None)
    monkeypatch.setattr(survey, "get_redis_client", lambda: None)
    monkeypatch.setattr(survey, "SUBMISSION_PHOTOS_DIR", str(tmp_path))
    monkeypatch.setattr(survey, "LOCAL_APPEAL_TRACKER", {})
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "DURABLE_WEBRTC_ENABLED", True)
    monkeypatch.setattr(settings, "DURABLE_APPEALS_ENABLED", True)
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", "mock_tg_token")
    monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", "mock_tg_token")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "mock_tg_token")
    monkeypatch.setattr(settings, "LARK_APP_ID", "cli_mock_id")
    monkeypatch.setattr(settings, "R2_PRIVATE_BUCKET_NAME", None)
    monkeypatch.setattr(settings, "GOOGLE_DRIVE_ROOT_FOLDER_ID", None)
    monkeypatch.setattr(settings, "BILLING_PAYOUT_VND", None)
    for field in ("GOOGLE_DRIVE_ACCESS_TOKEN", "GOOGLE_DRIVE_REFRESH_TOKEN", "GOOGLE_DRIVE_CLIENT_ID", "GOOGLE_DRIVE_CLIENT_SECRET"):
        monkeypatch.setattr(settings, field, None)
    legacy = AsyncMock(side_effect=AssertionError("durable record attempted legacy provider dispatch"))
    monkeypatch.setattr(survey, "dispatch_action_checklist", legacy)
    monkeypatch.setattr(survey, "send_telegram_notification", legacy)
    monkeypatch.setattr(survey, "dispatch_submission_telegram_notifications", legacy)
    app = FastAPI()
    app.include_router(survey.router)
    original = {"form_id": fid, "idempotency_key": key, "contact_phone": phone, "driver_phone": "+84" + phone[1:]}
    try:
        async with AsyncConnectionPool(dsn, min_size=1, max_size=3, open=False) as pool:
            monkeypatch.setattr(survey, "get_db_pool", lambda: pool)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/api/v1/survey/submit-webrtc", data=original)
                assert response.status_code == 200, response.text
                first = response.json()
                sid = first["submission_id"]
                assert first["driver_phone"] == phone
                assert first["status"] == "REJECTED"
                monkeypatch.setattr(settings, "DURABLE_WEBRTC_ENABLED", False)
                replay = await client.post("/api/v1/survey/submit-webrtc", data={k: v for k, v in original.items() if k != "driver_phone"})
                assert replay.status_code == 200 and replay.json() == first
                changed = await client.post("/api/v1/survey/submit-webrtc", data={**original, "signature_png": "changed"})
                assert changed.status_code == 409
                actions = observer.execute("SELECT flow_target,payload FROM outbox_events WHERE aggregate_id=%s", (sid,)).fetchall()
                assert len(actions) == 6 and all(row["flow_target"] != "ALL" for row in actions)
                telegram = [row["payload"] for row in actions if row["flow_target"] == "TELEGRAM_NOTIFY"]
                assert {row["message_class"] for row in telegram} == {"ADMIN_REVIEW", "PARTY_VERDICT"}
                party = next(row for row in telegram if row["message_class"] == "PARTY_VERDICT")
                assert party["chat_id"] == recipient and party["recipient_binding"]["phone"] == phone
                assert sum([await outbox_delivery.process(dsn) for _ in range(3)]) == 6
                assert observer.execute("SELECT count(*) AS n FROM outbox_events WHERE aggregate_id=%s AND status='PROCESSED'", (sid,)).fetchone()["n"] == 6
                appeal = await client.post("/api/v1/survey/appeal", json={"submission_id": sid.lower(), "driver_phone": phone})
                assert appeal.status_code == 200 and appeal.json()["status"] == "APPEAL_SUBMITTED"
                cancel = await client.post("/api/v1/survey/appeal/cancel", json={"submission_id": sid, "driver_phone": phone})
                assert cancel.status_code == 200 and cancel.json()["status"] == "CANCELLED"
                repeated = await client.post("/api/v1/survey/appeal/cancel", json={"submission_id": sid, "driver_phone": phone})
                assert repeated.json()["status"] == "NOT_APPEALABLE"
        row = observer.execute("SELECT status,work_owner,decision_version FROM survey_submissions WHERE submission_id=%s", (sid,)).fetchone()
        assert row == {"status": "REJECTED", "work_owner": "outbox_v2", "decision_version": 2}
        assert observer.execute("SELECT count(*) AS n FROM survey_submissions WHERE form_id=%s", (fid,)).fetchone()["n"] == 1
        assert observer.execute("SELECT count(*) AS n FROM submission_idempotency WHERE idempotency_key=%s AND state='COMMITTED'", (key,)).fetchone()["n"] == 1
        assert observer.execute("SELECT count(*) AS n FROM outbox_events WHERE aggregate_id=%s AND flow_target='ALL'", (sid,)).fetchone()["n"] == 0
        legacy.assert_not_awaited()
    finally:
        rows = observer.execute("SELECT submission_id,driver_id FROM survey_submissions WHERE form_id=%s", (fid,)).fetchall()
        ids = [row["submission_id"] for row in rows]
        observer.execute("DELETE FROM outbox_delivery_attempts WHERE event_id IN (SELECT id FROM outbox_events WHERE aggregate_id=ANY(%s))", (ids,))
        observer.execute("DELETE FROM outbox_events WHERE aggregate_id=ANY(%s)", (ids,))
        observer.execute("DELETE FROM system_audit_log WHERE entity_id=ANY(%s)", (ids,))
        observer.execute("DELETE FROM submission_idempotency WHERE idempotency_key=%s", (key,))
        observer.execute("DELETE FROM survey_submissions WHERE form_id=%s", (fid,))
        observer.execute("DELETE FROM driver_identity_vault WHERE driver_id=ANY(%s)", ([row["driver_id"] for row in rows if row["driver_id"]],))
        observer.execute("DELETE FROM phone_telegram_links WHERE phone_normalized=%s AND telegram_user_id=%s", (phone, recipient))
