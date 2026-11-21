import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from config import settings
from main import app
from services.telegram_polling import (
    TelegramPollingRunner,
    DriverBotRunner,
    ACTIVE_SUPPORT_CONVERSATIONS,
    ADMIN_ACTIVE_CONVERSATIONS,
    ADMIN_GROUP_ACTIVE_SESSION,
)


@pytest.fixture(autouse=True)
def clean_support_sessions(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", "123:test-admin")
    monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", "456:test-driver")
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-5445563473")
    ADMIN_ACTIVE_CONVERSATIONS.clear()
    ACTIVE_SUPPORT_CONVERSATIONS.clear()
    ADMIN_GROUP_ACTIVE_SESSION.clear()
    yield
    ACTIVE_SUPPORT_CONVERSATIONS.clear()
    ADMIN_GROUP_ACTIVE_SESSION.clear()
    ADMIN_ACTIVE_CONVERSATIONS.clear()


@pytest.mark.asyncio
async def test_admin_help_request_creates_reply_button():
    """User help request generates admin card with reply button in admin group."""
    driver_bot = DriverBotRunner(bot_token="mock_driver_token")
    mock_send = AsyncMock(return_value=True)
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=AsyncMock(status_code=200))

    with patch.object(driver_bot, "send_message", mock_send), \
         patch.object(driver_bot, "get_client", AsyncMock(return_value=mock_client)), \
         patch.object(driver_bot, "get_driver_phone", AsyncMock(return_value="0988111222")):
        await driver_bot.handle_admin_help_request(
            chat_id=12345,
            user_id=12345,
            message_text="Cần hỗ trợ về tiền thưởng phiếu VN-100",
            from_user={"id": 12345, "first_name": "Nguyễn", "last_name": "A", "username": "nguyena"},
        )

        mock_client.post.assert_awaited_once()
        admin_call_json = mock_client.post.await_args[1]["json"]
        assert "YÊU CẦU TRỢ GIÚP TỪ THÀNH VIÊN" in admin_call_json["text"]
        buttons = admin_call_json.get("reply_markup", {}).get("inline_keyboard", [])
        assert any(btn.get("callback_data") == "admin_reply_chat:12345" for row in buttons for btn in row)


@pytest.mark.asyncio
async def test_two_way_relay_lifecycle():
    """Tests opening chat, admin relaying to user, user relaying to admin, and closing conversation."""
    admin_bot = TelegramPollingRunner(bot_token="mock_admin_token", role="admin")
    driver_bot = DriverBotRunner(bot_token="mock_driver_token")

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=AsyncMock(status_code=200))

    with patch.object(admin_bot, "get_client", AsyncMock(return_value=mock_client)), \
         patch.object(admin_bot, "send_message", AsyncMock(return_value=True)), \
         patch.object(driver_bot, "get_client", AsyncMock(return_value=mock_client)), \
         patch.object(driver_bot, "send_message", AsyncMock(return_value=True)):

        # 1. Admin clicks "admin_reply_chat:777888" in admin group (-5445563473)
        await admin_bot.handle_callback_query({
            "id": "cq_1",
            "data": "admin_reply_chat:777888",
            "message": {"chat": {"id": -5445563473, "type": "group"}},
            "from": {"id": 111, "first_name": "Admin Boss"},
        })

        assert 777888 in ACTIVE_SUPPORT_CONVERSATIONS
        assert ADMIN_GROUP_ACTIVE_SESSION.get("-5445563473") == 777888

        # 2. Admin sends a message in admin group -> relays to user 777888 DM
        mock_client.post.reset_mock()
        await admin_bot.handle_update({
            "message": {
                "chat": {"id": -5445563473, "type": "group"},
                "from": {"id": 111, "first_name": "Admin Boss"},
                "text": "Chào bạn, mình đã kiểm tra và chuyển khoản tiền thưởng cho bạn rồi nhé!",
            }
        })
        mock_client.post.assert_awaited_once()
        user_relay_payload = mock_client.post.await_args[1]["json"]
        assert user_relay_payload["chat_id"] == 777888
        assert "Phản hồi từ Ban Quản Trị" in user_relay_payload["text"]
        assert "chuyển khoản tiền thưởng" in user_relay_payload["text"]

        # 3. User sends a message in DM -> relays to admin group (-5445563473)
        mock_client.post.reset_mock()
        await driver_bot.handle_update({
            "message": {
                "chat": {"id": 777888, "type": "private"},
                "from": {"id": 777888, "first_name": "Tài xế Vui Vẻ"},
                "text": "Mình đã nhận được tiền rồi, cảm ơn admin nhiều!",
            }
        })
        mock_client.post.assert_awaited_once()
        admin_relay_payload = mock_client.post.await_args[1]["json"]
        assert admin_relay_payload["chat_id"] == -5445563473
        assert "Tài xế Vui Vẻ" in admin_relay_payload["text"]

@pytest.mark.asyncio
async def test_admin_swipe_reply_relays_to_user_dm():
    """Admin swiping and replying directly to ticket card relays to user DM automatically."""
    admin_bot = TelegramPollingRunner(bot_token="mock_admin_token", role="admin")
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=AsyncMock(status_code=200))

    with patch.object(admin_bot, "get_client", AsyncMock(return_value=mock_client)), \
         patch.object(admin_bot, "send_message", AsyncMock(return_value=True)):
        # Admin replies to ticket message in admin group
        await admin_bot.handle_update({
            "message": {
                "chat": {"id": -5445563473, "type": "group"},
                "from": {"id": 111, "first_name": "Supervisor Lan"},
                "text": "Em gửi lại số căn cước công dân để bên mình kiểm tra nhé!",
                "reply_to_message": {
                    "text": "🆘 YÊU CẦU TRỢ GIÚP TỪ THÀNH VIÊN!\n• Người gửi: Lê Văn C\n• Chat ID: <code>999000111</code>\n• Nội dung: Em bị lỗi nộp",
                }
            }
        })

        # Must relay directly to 999000111
        mock_client.post.assert_awaited_once()
        payload = mock_client.post.await_args[1]["json"]
        assert payload["chat_id"] == 999000111
        assert "Supervisor Lan" in payload["text"]
        assert "căn cước công dân" in payload["text"]

        # Must activate session
        assert 999000111 in ACTIVE_SUPPORT_CONVERSATIONS
        assert ADMIN_GROUP_ACTIVE_SESSION.get("-5445563473") == 999000111

        # 4. Admin types /close -> closes conversation
        await admin_bot.handle_update({
            "message": {
                "chat": {"id": -5445563473, "type": "group"},
                "from": {"id": 111, "first_name": "Supervisor Lan"},
                "text": "/close",
            }
        })
        assert 999000111 not in ACTIVE_SUPPORT_CONVERSATIONS
        assert "-5445563473" not in ADMIN_GROUP_ACTIVE_SESSION

def test_random_subdomain_routing_to_form(monkeypatch):
    """A random-suffix host serves the matching form without redirecting."""
    from routers import system
    from services.form_engine import FormSchema

    monkeypatch.setattr(system, "refresh_form_registry_from_disk", lambda: None)
    monkeypatch.setitem(system.FORM_REGISTRY, "income_survey", FormSchema(
        form_id="income_survey", title="Subdomain routing fixture", questions=[],
    ))
    resp = TestClient(app).get(
        "/", headers={"Host": "income-survey-dfjhgkshgksd.survey.trustbridgevn.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "Subdomain routing fixture" in resp.text
    assert "/api/v1/survey/submit-webrtc" in resp.text


def test_caddy_on_demand_check_endpoint():
    """Tests that /api/v1/caddy-check approves our system domains."""
    client = TestClient(app)
    r1 = client.get("/api/v1/caddy-check?domain=income-survey-x92m.146.190.201.32.sslip.io")
    assert r1.status_code == 200

    r2 = client.get("/api/v1/caddy-check?domain=income-survey-abc.survey.trustbridgevn.com")
    assert r2.status_code == 200

    r3 = client.get("/api/v1/caddy-check?domain=malicious-domain.com")
    assert r3.status_code == 403
