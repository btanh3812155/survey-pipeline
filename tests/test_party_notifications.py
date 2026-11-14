import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from main import (
    app,
    dispatch_submission_telegram_notifications,
    render_driver_submission_verdict_message,
)


@pytest.mark.asyncio
async def test_dispatch_notifications_driver_and_collaborator():
    """Verifies that driver and collaborator receive direct Telegram messages on verdict."""
    mock_send = AsyncMock(return_value=True)

    with patch("main.lookup_telegram_user_by_phone", AsyncMock(return_value=123456789)), \
         patch("main.lookup_telegram_user_for_collaborator", AsyncMock(return_value=987654321)), \
         patch("main.send_telegram_direct_message", mock_send):
        await dispatch_submission_telegram_notifications(
            submission_id="VN-TEST99",
            status_str="APPROVED",
            driver_phone="0988111222",
            driver_name="Nguyễn Văn A",
            collaborator_id="ctv_hanoi_01",
            reason_str="Đạt chuẩn",
            gross_revenue=750000,
        )

        # Must send 2 direct messages: 1 to driver, 1 to collaborator
        assert mock_send.await_count == 2
        calls = mock_send.await_args_list

        # Driver message check
        driver_call = next(c for c in calls if c.kwargs.get("chat_id") == 123456789)
        assert "DUYỆT THÀNH CÔNG" in driver_call.kwargs["text"]
        assert "750,000đ" in driver_call.kwargs["text"]

        # Collaborator message check
        collab_call = next(c for c in calls if c.kwargs.get("chat_id") == 987654321)
        assert "Cập Nhật Khảo Sát #VN-TEST99" in collab_call.kwargs["text"]
        assert "APPROVED" in collab_call.kwargs["text"]


@pytest.mark.asyncio
async def test_dispatch_notifications_unlinked_collaborator_graceful():
    """When collaborator is not yet linked, driver still receives message and no exception is raised."""
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

        # Only 1 direct message to driver; unlinked collaborator is logged cleanly
        assert mock_send.await_count == 1
        driver_call = mock_send.await_args_list[0]
        assert driver_call.kwargs["chat_id"] == 111222333
        assert "CHƯA ĐẠT" in driver_call.kwargs["text"]
        assert driver_call.kwargs["reply_markup"] is not None


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
        # Background task must be executed
        mock_dispatch.assert_awaited_once()
        kwargs = mock_dispatch.await_args.kwargs
        assert kwargs["driver_phone"] == "0988777888"
        assert kwargs["collaborator_id"] == "ctv_01"

@pytest.mark.asyncio
async def test_send_telegram_notification_retries_on_429():
    """Verifies that 429 rate limit is retried instead of dropping the submission notification."""
    from main import send_telegram_notification
    from unittest.mock import MagicMock
    import httpx

    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers = {"Retry-After": "0.01"}

    resp_200 = MagicMock()
    resp_200.status_code = 200

    mock_client = AsyncMock()
    mock_client.is_closed = False
    mock_client.post.side_effect = [resp_429, resp_200]

    with patch("main.http_client", mock_client), \
         patch("main.settings.TELEGRAM_ADMIN_BOT_TOKEN", "real_token_123"):
        success = await send_telegram_notification(chat_id="-100123", text="<b>Thông báo test</b>")
        assert success is True
        assert mock_client.post.await_count == 2


@pytest.mark.asyncio
async def test_send_telegram_notification_fallback_on_400():
    """Verifies that 400 entity parse error strips HTML tags and falls back to plain text."""
    from main import send_telegram_notification
    from unittest.mock import MagicMock

    resp_400 = MagicMock()
    resp_400.status_code = 400
    resp_400.text = "Bad Request: can't parse entities"

    resp_200 = MagicMock()
    resp_200.status_code = 200

    mock_client = AsyncMock()
    mock_client.is_closed = False
    mock_client.post.side_effect = [resp_400, resp_200]

    with patch("main.http_client", mock_client), \
         patch("main.settings.TELEGRAM_ADMIN_BOT_TOKEN", "real_token_123"):
        success = await send_telegram_notification(chat_id="-100123", text="<b>Malformed <tag></b>")
        assert success is True
        assert mock_client.post.await_count == 2
        # Second call should have fallen back without parse_mode and with tags stripped
        fb_call = mock_client.post.await_args_list[1]
        assert "parse_mode" not in fb_call.kwargs["json"]
        assert "<" not in fb_call.kwargs["json"]["text"]


@pytest.mark.asyncio
async def test_admin_decision_notifies_ctv_and_driver():
    """Verifies that admin approve/reject callbacks in TelegramPollingRunner send responses back to CTV and driver."""
    from services.telegram_polling import TelegramPollingRunner

    runner = TelegramPollingRunner(bot_token="mock_admin_token", role="admin")

    mock_dossier = {
        "submission_id": "VN-CTVTEST1",
        "driver_phone": "0988999888",
        "driver_name": "Tài xế CTV",
        "collaborator_id": "ctv001",
        "ctv_code": "CTV001_Dũng_HN",
        "gross_income": 900000,
        "status": "MANUAL_REVIEW",
    }

    mock_dispatch = AsyncMock()
    with patch("services.telegram_polling.query_submission_dossier", AsyncMock(return_value=mock_dossier)), \
         patch.object(runner, "_update_submission_status", AsyncMock(return_value=(True, "APPROVED"))), \
         patch.object(runner, "send_message", AsyncMock(return_value=True)), \
         patch.object(runner, "answer_callback_query", AsyncMock(return_value=True)), \
         patch.object(runner, "edit_message_reply_markup", AsyncMock(return_value=True)), \
         patch("main.dispatch_submission_telegram_notifications", mock_dispatch):

        cq = {
            "id": "cq_123",
            "data": "admin_approve:VN-CTVTEST1",
            "message": {"message_id": 999, "chat": {"id": -100555}},
            "from": {"id": 111, "first_name": "Admin"},
        }
        await runner.handle_callback_query(cq)

        # Must dispatch response back to CTV & driver
        mock_dispatch.assert_awaited_once()
        kwargs = mock_dispatch.await_args.kwargs
        assert kwargs["submission_id"] == "VN-CTVTEST1"
        assert kwargs["status_str"] == "APPROVED"
        assert kwargs["collaborator_id"] in ("ctv001", "CTV001_Dũng_HN")
        assert kwargs["driver_phone"] == "0988999888"
