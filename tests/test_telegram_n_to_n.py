"""N-to-N support relay: two admins each hold one user without cross-leakage."""
from unittest.mock import AsyncMock, patch

import pytest

from config import settings
from services.telegram_polling import (
    ACTIVE_SUPPORT_CONVERSATIONS,
    ADMIN_ACTIVE_CONVERSATIONS,
    ADMIN_GROUP_ACTIVE_SESSION,
    DriverBotRunner,
    TelegramPollingRunner,
)

GROUP = -5445563473
ADMIN_A, ADMIN_B = 101, 202
USER_A, USER_B = 777001, 777002


@pytest.fixture(autouse=True)
def clean_sessions(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", "123:test-admin")
    monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", "456:test-driver")
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", str(GROUP))
    ACTIVE_SUPPORT_CONVERSATIONS.clear()
    ADMIN_ACTIVE_CONVERSATIONS.clear()
    ADMIN_GROUP_ACTIVE_SESSION.clear()
    yield
    ACTIVE_SUPPORT_CONVERSATIONS.clear()
    ADMIN_ACTIVE_CONVERSATIONS.clear()
    ADMIN_GROUP_ACTIVE_SESSION.clear()


class _Client:
    def __init__(self):
        self.posts = []

    async def post(self, url, json=None, **kwargs):
        self.posts.append((url, json))
        class _Resp:
            status_code = 200
            text = ""
        return _Resp()


def _admin_message(admin_id, text):
    return {"message": {
        "chat": {"id": GROUP, "type": "group"},
        "from": {"id": admin_id, "first_name": f"Admin {admin_id}"},
        "text": text,
    }}


def _driver_message(user_id, name, text):
    return {"message": {
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "first_name": name},
        "text": text,
    }}


@pytest.mark.asyncio
async def test_two_admins_and_two_users_relay_only_to_their_own_session():
    client = _Client()
    admin_bot = TelegramPollingRunner(bot_token="mock_admin_token", role="admin")
    driver_bot = DriverBotRunner(bot_token="mock_driver_token")
    admin_send = AsyncMock(return_value=True)
    driver_send = AsyncMock(return_value=True)
    with patch.object(admin_bot, "get_client", AsyncMock(return_value=client)), \
         patch.object(admin_bot, "send_message", admin_send), \
         patch.object(driver_bot, "get_client", AsyncMock(return_value=client)), \
         patch.object(driver_bot, "send_message", driver_send):
        for index, (admin_id, user_id) in enumerate(((ADMIN_A, USER_A), (ADMIN_B, USER_B))):
            await admin_bot.handle_callback_query({
                "id": f"cq-{index}",
                "data": f"admin_reply_chat:{user_id}",
                "message": {"message_id": 900 + index, "chat": {"id": GROUP, "type": "group"}},
                "from": {"id": admin_id, "first_name": f"Admin {admin_id}"},
            })
        assert ADMIN_ACTIVE_CONVERSATIONS == {ADMIN_A: USER_A, ADMIN_B: USER_B}
        assert set(ACTIVE_SUPPORT_CONVERSATIONS) == {USER_A, USER_B}

        client.posts.clear()
        await admin_bot.handle_update(_admin_message(ADMIN_A, "Reply only to A"))
        assert [(url.split("/bot", 1)[-1], payload["chat_id"]) for url, payload in client.posts] == [
            ("456:test-driver/sendMessage", USER_A)
        ]
        assert "Reply only to A" in client.posts[0][1]["text"]

        client.posts.clear()
        await admin_bot.handle_update(_admin_message(ADMIN_B, "Reply only to B"))
        assert [(url.split("/bot", 1)[-1], payload["chat_id"]) for url, payload in client.posts] == [
            ("456:test-driver/sendMessage", USER_B)
        ]
        assert "Reply only to B" in client.posts[0][1]["text"]

        client.posts.clear()
        await driver_bot.handle_update(_driver_message(USER_A, "User A", "From A"))
        assert len(client.posts) == 1
        url, payload = client.posts[0]
        assert url.endswith("/bot123:test-admin/sendMessage")
        assert payload["chat_id"] == GROUP
        assert "User A" in payload["text"] and str(USER_A) in payload["text"]

        client.posts.clear()
        await driver_bot.handle_update(_driver_message(USER_B, "User B", "From B"))
        assert len(client.posts) == 1
        url, payload = client.posts[0]
        assert url.endswith("/bot123:test-admin/sendMessage")
        assert payload["chat_id"] == GROUP
        assert "User B" in payload["text"] and str(USER_B) in payload["text"]

        # Closing one admin's conversation must not remove the other one.
        await admin_bot.handle_update(_admin_message(ADMIN_A, "/close"))
        assert USER_A not in ACTIVE_SUPPORT_CONVERSATIONS
        assert USER_B in ACTIVE_SUPPORT_CONVERSATIONS
        assert ADMIN_ACTIVE_CONVERSATIONS.get(ADMIN_B) == USER_B
