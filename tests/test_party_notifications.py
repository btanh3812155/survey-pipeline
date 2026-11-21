import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from main import (
    app,
    dispatch_submission_telegram_notifications,
    render_driver_submission_verdict_message,
)

SUBMITTED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_dispatch_notifications_goes_to_collaborator_only(monkeypatch):
    """The Telegram flow is CTV-facing: only the collaborator receives the verdict."""
    monkeypatch.setattr("routers.survey.settings.TELEGRAM_DRIVER_BOT_TOKEN", "123:test-driver")
    mock_send = AsyncMock(return_value=True)

    with patch("main.lookup_telegram_user_by_phone", AsyncMock(return_value=123456789)), \
         patch("main.lookup_telegram_user_for_collaborator", AsyncMock(return_value=987654321)), \
         patch("main.send_telegram_direct_message", mock_send):
        await dispatch_submission_telegram_notifications(
            submission_id="VN-TEST99",
            status_str="REJECTED",
            driver_phone="0988111222",
            driver_name="Nguyễn Văn A",
            collaborator_id="ctv001",
            reason_str="Ảnh mờ không đọc được",
            gross_revenue=750000,
            form_id="TB_GSM_2026_Nov_Test",
        )

        assert mock_send.await_count == 1
        call = mock_send.await_args_list[0]
        assert call.kwargs["chat_id"] == 987654321
        assert "VN-TEST99" in call.kwargs["text"]
        assert "REJECTED" in call.kwargs["text"]
        # rejection carries a return-to-form link with the CTV ref
        assert "/forms/TB_GSM_2026_Nov_Test?ref=CTV001" in call.kwargs["text"]


@pytest.mark.asyncio
async def test_dispatch_notifications_unlinked_collaborator_graceful(monkeypatch):
    """An unlinked collaborator is skipped cleanly; no driver message is sent."""
    monkeypatch.setattr("routers.survey.settings.TELEGRAM_DRIVER_BOT_TOKEN", "123:test-driver")
    mock_send = AsyncMock(return_value=True)
    lookup = AsyncMock(return_value=None)

    with patch("main.lookup_telegram_user_by_phone", AsyncMock(return_value=111222333)), \
         patch("main.lookup_telegram_user_for_collaborator", lookup), \
         patch("main.send_telegram_direct_message", mock_send):
        await dispatch_submission_telegram_notifications(
            submission_id="VN-REJ88",
            status_str="REJECTED",
            driver_phone="0912345678",
            driver_name="Trần Văn B",
            collaborator_id="ctv_unlinked_99",
            reason_str="Ảnh mờ không đọc được",
        )

        lookup.assert_awaited_once_with("ctv_unlinked_99")
        mock_send.assert_not_awaited()


def test_submit_webrtc_schedules_party_notifications(monkeypatch, tmp_path):
    """Submitting a survey schedules dispatch_submission_telegram_notifications in background tasks."""
    monkeypatch.setenv("MOCK_AI", "1")
    from config import settings
    from routers import survey as survey_router

    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-100999")
    monkeypatch.setattr(survey_router, "get_db_pool", lambda: None)
    monkeypatch.setattr(survey_router, "get_redis_client", lambda: None)
    monkeypatch.setattr(survey_router, "SUBMISSION_PHOTOS_DIR", str(tmp_path / "photos"))
    monkeypatch.setattr(survey_router, "append_dev_submission", lambda _record: None)
    client = TestClient(app)

    mock_dispatch = AsyncMock()
    mock_admin_notify = AsyncMock()

    with patch("main.dispatch_submission_telegram_notifications", mock_dispatch), \
         patch("routers.survey.send_telegram_notification", mock_admin_notify), \
         patch("routers.survey.dispatch_action_checklist", AsyncMock()):
        resp = client.post(
            "/api/v1/survey/submit-webrtc",
            data={
                "driver_phone": "0988777888",
                "driver_name": "Tài xế Test",
                "collaborator_id": "ctv_01",
                "gross_revenue": "600000",
                "vehicle_type": "CAR",
                "is_test_mode": "true",
                "bypass_img_check": "true",
            },
        )
        assert resp.status_code == 200
        mock_dispatch.assert_awaited_once()
        mock_admin_notify.assert_awaited_once()
        kwargs = mock_dispatch.await_args.kwargs
        assert kwargs["driver_phone"] == "0988777888"
        assert kwargs["collaborator_id"] == "ctv_01"


@pytest.mark.asyncio
async def test_dispatch_uses_resolved_ctv_chat_id_with_mocked_http(monkeypatch):
    """Regression: a bound CTV ref resolves to its Telegram id and is sent there once."""
    from config import settings
    from routers import survey as survey_router

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"ok": True, "result": {"message_id": 4242}}

    class Client:
        def __init__(self):
            self.calls = []

        async def post(self, url, json=None):
            self.calls.append((url, json))
            return Resp()

    client = Client()
    monkeypatch.setattr(survey_router, "get_http_client", lambda: client)
    monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", "123:test-driver", raising=False)
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", "456:test-admin", raising=False)

    with patch("main.lookup_telegram_user_for_collaborator", AsyncMock(return_value=9241303232)), \
         patch("main.send_telegram_direct_message", survey_router.send_telegram_direct_message):
        await dispatch_submission_telegram_notifications(
            submission_id="VN-JERRY01",
            status_str="APPROVED",
            driver_phone="0961281310",
            driver_name="Tài xế Test",
            collaborator_id="TUAN_LM_HCM_001",
            reason_str="Ảnh hợp lệ",
            form_id="TB_GSM_2026_Nov_Test",
        )

    assert len(client.calls) == 1
    url, payload = client.calls[0]
    assert url.endswith("/bot123:test-driver/sendMessage")
    assert payload["chat_id"] == 9241303232
    assert "VN-JERRY01" in payload["text"]


@pytest.mark.asyncio
async def test_dispatch_retries_telegram_429_with_retry_after(monkeypatch):
    """Regression: 429 responses are retried once using Telegram's retry_after."""
    from config import settings
    from routers import survey as survey_router

    class Resp429:
        status_code = 429
        text = "Too Many Requests"

        def json(self):
            return {"ok": False, "parameters": {"retry_after": 1}}

    class Resp200:
        status_code = 200
        text = ""

        def json(self):
            return {"ok": True, "result": {"message_id": 7}}

    class Client:
        def __init__(self):
            self.calls = []

        async def post(self, url, json=None):
            self.calls.append((url, json))
            return Resp429() if len(self.calls) == 1 else Resp200()

    client = Client()
    monkeypatch.setattr(survey_router, "get_http_client", lambda: client)
    monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", "123:test-driver", raising=False)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(survey_router.asyncio, "sleep", no_sleep)
    ok = await survey_router.send_telegram_direct_message("123:test-driver", 9241303232, "hello")
    assert ok is True
    assert len(client.calls) == 2



@pytest.mark.asyncio
async def test_question_source_resolves_phone_mapping_to_chat_id(monkeypatch):
    """Question phone -> verified standalone mapping -> chat, without driver/CTV lookup."""
    from config import settings
    from routers import survey as survey_router

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"ok": True, "result": {"message_id": 9911}}

    class Client:
        def __init__(self):
            self.calls = []

        async def post(self, url, json=None):
            self.calls.append((url, json))
            return Resp()

    client = Client()
    monkeypatch.setattr(survey_router, "get_http_client", lambda: client)
    monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", "123:test-driver", raising=False)
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None, raising=False)
    mapping = AsyncMock(return_value=9241303232)
    phone_lookup = AsyncMock(return_value=111222333)
    collab_lookup = AsyncMock(return_value=987654321)

    with patch("routers.survey.get_telegram_id_for_phone", mapping), \
         patch("main.lookup_telegram_user_by_phone", phone_lookup), \
         patch("main.lookup_telegram_user_for_collaborator", collab_lookup), \
         patch("main.send_telegram_direct_message", survey_router.send_telegram_direct_message):
        await dispatch_submission_telegram_notifications(
            submission_id="DEV-PHONE-MAP",
            status_str="APPROVED",
            driver_phone="0932090967",
            driver_name="[TEST] mapped phone",
            collaborator_id="+84 932 090 967",
            reason_str="Mapped phone path test",
            form_id="TB_GSM_2026_Nov_Test",
            recipient_kind="driver",
            submitted_at=SUBMITTED_AT,
            ctv_ref="TUAN_LM_HCM_001",
        )

    mapping.assert_awaited_once_with("0932090967", submitted_at=SUBMITTED_AT)
    phone_lookup.assert_not_awaited()
    collab_lookup.assert_not_awaited()
    assert len(client.calls) == 1
    _, payload = client.calls[0]
    assert payload["chat_id"] == 9241303232
    assert "DEV-PHONE-MAP" in payload["text"]


@pytest.mark.asyncio
async def test_question_source_without_mapping_skips_send(monkeypatch, caplog):
    """An unmapped phone logs clearly and does not fall back to driver/CTV rows."""
    import logging
    from config import settings
    from routers import survey as survey_router

    class Client:
        def __init__(self):
            self.calls = []

        async def post(self, url, json=None):
            self.calls.append((url, json))
            class Resp:
                status_code = 200
                text = ""
            return Resp()

    client = Client()
    monkeypatch.setattr(survey_router, "get_http_client", lambda: client)
    monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", "123:test-driver", raising=False)
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None, raising=False)
    mapping = AsyncMock(return_value=None)
    phone_lookup = AsyncMock(return_value=111222333)
    collab_lookup = AsyncMock(return_value=987654321)

    with caplog.at_level(logging.WARNING, logger="routers.survey"):
        with patch("routers.survey.get_telegram_id_for_phone", mapping), \
             patch("main.lookup_telegram_user_by_phone", phone_lookup), \
             patch("main.lookup_telegram_user_for_collaborator", collab_lookup), \
             patch("main.send_telegram_direct_message", survey_router.send_telegram_direct_message):
            await dispatch_submission_telegram_notifications(
                submission_id="DEV-UNMAPPED-PHONE",
                status_str="REJECTED",
                driver_phone="0932090967",
                collaborator_id="0932090967",
                reason_str="Unmapped phone",
                form_id="TB_GSM_2026_Nov_Test",
                recipient_kind="driver",
                submitted_at=SUBMITTED_AT,
            )

    mapping.assert_awaited_once_with("0932090967", submitted_at=SUBMITTED_AT)
    phone_lookup.assert_not_awaited()
    collab_lookup.assert_not_awaited()
    assert client.calls == []
    assert "no verified phone_telegram_links mapping" in caplog.text
    assert "0932090967" not in caplog.text


@pytest.mark.asyncio
async def test_question_source_rejects_raw_chat_id_and_username(monkeypatch):
    """Question-source phone delivery never accepts a direct recipient bypass."""
    from config import settings
    from routers import survey as survey_router

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"ok": True, "result": {"message_id": 9912}}

    class Client:
        def __init__(self):
            self.calls = []

        async def post(self, url, json=None):
            self.calls.append((url, json))
            return Resp()

    client = Client()
    monkeypatch.setattr(survey_router, "get_http_client", lambda: client)
    monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", "123:test-driver", raising=False)
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None, raising=False)
    mapping = AsyncMock(return_value=999999999)

    with patch("routers.survey.get_telegram_id_for_phone", mapping), \
         patch("main.send_telegram_direct_message", survey_router.send_telegram_direct_message):
        for value in ("9241303232", "@JerryTest"):
            await dispatch_submission_telegram_notifications(
                submission_id=f"DEV-DIRECT-{value[-4:]}",
                status_str="APPROVED",
                collaborator_id=value,
                reason_str="Direct chat id",
                form_id="TB_GSM_2026_Nov_Test",
                recipient_kind="driver",
                submitted_at=SUBMITTED_AT,
            )

    mapping.assert_not_awaited()
    assert client.calls == []


@pytest.mark.asyncio
async def test_question_source_non_phone_answer_skips_send(monkeypatch, caplog):
    """A non-phone, non-chat answer is not sent anywhere."""
    import logging
    from config import settings
    from routers import survey as survey_router

    class Client:
        def __init__(self):
            self.calls = []

        async def post(self, url, json=None):
            self.calls.append((url, json))
            class Resp:
                status_code = 200
                text = ""
            return Resp()

    client = Client()
    monkeypatch.setattr(survey_router, "get_http_client", lambda: client)
    monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", "123:test-driver", raising=False)
    mapping = AsyncMock(return_value=None)

    with caplog.at_level(logging.WARNING, logger="routers.survey"):
        with patch("routers.survey.get_telegram_id_for_phone", mapping), \
             patch("main.send_telegram_direct_message", survey_router.send_telegram_direct_message):
            await dispatch_submission_telegram_notifications(
                submission_id="DEV-GARBAGE",
                status_str="REJECTED",
                collaborator_id="not-a-phone",
                form_id="TB_GSM_2026_Nov_Test",
                recipient_kind="driver",
                submitted_at=SUBMITTED_AT,
            )

    mapping.assert_not_awaited()
    assert client.calls == []
    assert "invalid phone answer" in caplog.text


def test_unmapped_question_phone_still_notifies_admin(monkeypatch, tmp_path):
    """A missing verified party link never suppresses the independent admin card."""
    from config import settings
    from routers import survey as survey_router
    from services.form_engine import FormQuestion, FormSchema, FormSettings, QuestionType

    schema = FormSchema(
        form_id="h1_admin_independent",
        title="H1 delivery boundary",
        settings=FormSettings(
            is_test_mode=True, decree13_required=False, ctv_ref_enabled=False,
            tg_response_source="question", tg_response_question_id="driver_phone",
        ),
        questions=[FormQuestion(id="driver_phone", title="Phone", type=QuestionType.TEXT, required=True)],
    )
    monkeypatch.setitem(survey_router.FORM_REGISTRY, schema.form_id, schema)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", "123:boundary-test")
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-100999")
    monkeypatch.setattr(survey_router, "refresh_form_registry_from_disk", lambda: None)
    monkeypatch.setattr(survey_router, "get_db_pool", lambda: None)
    monkeypatch.setattr(survey_router, "get_redis_client", lambda: None)
    monkeypatch.setattr(survey_router, "SUBMISSION_PHOTOS_DIR", str(tmp_path / "photos"))
    monkeypatch.setattr(survey_router, "append_dev_submission", lambda _record: None)
    admin = AsyncMock()
    direct = AsyncMock(return_value=True)
    mapping = AsyncMock(return_value=None)
    driver_lookup = AsyncMock(side_effect=AssertionError("driver records must not authorize phone delivery"))
    ctv_lookup = AsyncMock(side_effect=AssertionError("CTV records must not authorize phone delivery"))
    with patch.object(survey_router, "send_telegram_notification", admin), \
         patch.object(survey_router, "dispatch_action_checklist", AsyncMock()), \
         patch.object(survey_router, "get_telegram_id_for_phone", mapping), \
         patch("main.send_telegram_direct_message", direct), \
         patch("main.lookup_telegram_user_by_phone", driver_lookup), \
         patch("main.lookup_telegram_user_for_collaborator", ctv_lookup):
        response = TestClient(app).post(
            "/api/v1/survey/submit-webrtc",
            data={
                "form_id": schema.form_id, "driver_phone": "0932090967",
                "is_test_mode": "true", "bypass_img_check": "true",
                "submitted_at": "2099-01-01T00:00:00+00:00",
            },
        )
    assert response.status_code == 200, response.text
    admin.assert_awaited_once()
    assert admin.await_args.args[0] == "-100999"
    mapping.assert_awaited_once()
    assert mapping.await_args.args == ("0932090967",)
    original_time = mapping.await_args.kwargs["submitted_at"]
    assert isinstance(original_time, datetime) and original_time.utcoffset() is not None
    assert original_time != datetime(2099, 1, 1, tzinfo=timezone.utc)
    direct.assert_not_awaited()
    driver_lookup.assert_not_awaited()
    ctv_lookup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("submitted_at", [None, "unknown", "2026-01-01T00:00:00"])
async def test_question_verdict_requires_original_aware_submission_time(monkeypatch, submitted_at):
    from config import settings
    from routers import survey

    monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", "123:offline-time-check")
    mapping = AsyncMock(return_value=333)
    sender = AsyncMock(return_value=True)
    with patch.object(survey, "get_telegram_id_for_phone", mapping), \
         patch("main.send_telegram_direct_message", sender):
        await survey.dispatch_submission_telegram_notifications(
            submission_id="DEV-TIME-UNKNOWN", status_str="APPROVED",
            collaborator_id="0932090967", recipient_kind="driver", submitted_at=submitted_at,
        )
    mapping.assert_not_awaited()
    sender.assert_not_awaited()
