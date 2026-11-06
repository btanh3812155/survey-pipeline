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
