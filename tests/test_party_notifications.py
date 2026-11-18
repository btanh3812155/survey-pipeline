import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from main import (
    app,
    dispatch_submission_telegram_notifications,
    render_driver_submission_verdict_message,
)


@pytest.mark.asyncio
async def test_dispatch_notifications_goes_to_collaborator_only():
    """The Telegram flow is CTV-facing: only the collaborator receives the verdict."""
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
        assert "Cập Nhật Khảo Sát #VN-TEST99" in call.kwargs["text"]
        assert "REJECTED" in call.kwargs["text"]
        # rejection carries a return-to-form link with the CTV ref
        assert "/forms/TB_GSM_2026_Nov_Test?ref=CTV001" in call.kwargs["text"]


@pytest.mark.asyncio
async def test_dispatch_notifications_unlinked_collaborator_graceful():
    """An unlinked collaborator is skipped cleanly; no driver message is sent."""
    mock_send = AsyncMock(return_value=True)

    with patch("main.lookup_telegram_user_by_phone", AsyncMock(return_value=111222333)), \
         patch("main.lookup_telegram_user_for_collaborator", AsyncMock(return_value=None)), \
         patch("main.send_telegram_direct_message", mock_send):
        await dispatch_submission_telegram_notifications(
            submission_id="VN-REJ88",
            status_str="REJECTED",
            driver_phone="0912345678",
            driver_name="Trần Văn B",
            collaborator_id="ctv_unlinked_99",
            reason_str="Ảnh mờ không đọc được",
        )

        assert mock_send.await_count == 0


def test_submit_webrtc_schedules_party_notifications(monkeypatch):
    """Submitting a survey schedules dispatch_submission_telegram_notifications in background tasks."""
    monkeypatch.setenv("MOCK_AI", "1")
    client = TestClient(app)

    mock_dispatch = AsyncMock()
    mock_admin_notify = AsyncMock()

    with patch("main.dispatch_submission_telegram_notifications", mock_dispatch), \
         patch("main.send_telegram_notification", mock_admin_notify):
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
async def test_phone_recipient_is_resolved_by_phone_not_as_chat_id(monkeypatch):
    """Regression: a driver phone answer must not become chat_id=961281318."""
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

    phone_lookup = AsyncMock(return_value=None)
    collab_lookup = AsyncMock(return_value=None)
    with patch("main.lookup_telegram_user_by_phone", phone_lookup), \
         patch("main.lookup_telegram_user_for_collaborator", collab_lookup), \
         patch("main.send_telegram_direct_message", survey_router.send_telegram_direct_message):
        await dispatch_submission_telegram_notifications(
            submission_id="VN-PHONE01",
            status_str="REJECTED",
            driver_phone="0961281318",
            collaborator_id="0961281318",
            reason_str="Ảnh mờ",
            form_id="TB_GSM_2026_Nov_Test",
            recipient_kind="driver",
        )

    phone_lookup.assert_awaited_once_with("0961281318")
    collab_lookup.assert_not_awaited()
    assert client.calls == []


@pytest.mark.asyncio
async def test_question_source_sends_to_phone_without_any_ctv_directory(monkeypatch):
    """Question source resolves the phone directly; no CTV row/code is required."""
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
    phone_lookup = AsyncMock(return_value=9241303232)
    collab_lookup = AsyncMock(return_value=None)

    with patch("main.lookup_telegram_user_by_phone", phone_lookup), \
         patch("main.lookup_telegram_user_for_collaborator", collab_lookup), \
         patch("main.send_telegram_direct_message", survey_router.send_telegram_direct_message):
        await dispatch_submission_telegram_notifications(
            submission_id="DEV-PHONE-PATH",
            status_str="APPROVED",
            driver_phone="0932090967",
            driver_name="[TEST] Jerry phone path",
            collaborator_id="0932090967",
            reason_str="Phone path test",
            form_id="TB_GSM_2026_Nov_Test",
            recipient_kind="driver",
            ctv_ref="TUAN_LM_HCM_001",
        )

    phone_lookup.assert_awaited_once_with("0932090967")
    collab_lookup.assert_not_awaited()
    assert len(client.calls) == 1
    _, payload = client.calls[0]
    assert payload["chat_id"] == 9241303232
    assert "DEV-PHONE-PATH" in payload["text"]
