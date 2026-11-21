import io
import json
from datetime import datetime, timezone
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from PIL import Image

from config import settings
from services.telegram_polling import DriverBotRunner


@pytest.fixture
def driver_runner():
    return DriverBotRunner(bot_token="mock_driver_token")


@pytest.mark.asyncio
async def test_driver_bot_start_standard(driver_runner):
    """Driver /start shows welcome message and persistent keyboard."""
    mock_send = AsyncMock()
    with patch.object(driver_runner, "send_message", mock_send):
        await driver_runner.handle_driver_command(
            chat_id=111, user_id=111, text="/start", from_user={"first_name": "Tài xế A"}
        )
        mock_send.assert_awaited_once()
        text = mock_send.await_args[0][1]
        markup = mock_send.await_args[1].get("reply_markup", {})
        assert "Kênh Hỗ Trợ Khảo Sát" in text
        assert "keyboard" in markup
        assert any(btn.get("request_contact") for row in markup["keyboard"] for btn in row)
        assert any("Hỗ Trợ" in btn.get("text", "") for row in markup["keyboard"] for btn in row)


@pytest.mark.asyncio
async def test_driver_bot_start_deep_link_fix(driver_runner):
    """Driver /start fix_VN-1234_slot_odometer arms pending fix session."""
    mock_send = AsyncMock()
    status = AsyncMock(return_value="REJECTED")
    with patch.object(driver_runner, "send_message", mock_send), \
         patch.object(driver_runner, "_get_submission_status", status):
        await driver_runner.handle_update({"message": {
            "chat": {"id": 222, "type": "private"},
            "from": {"id": 222, "first_name": "Tài xế B"},
            "text": "/start fix_VN-9999_slot_odometer",
        }})
        mock_send.assert_awaited_once()
        status.assert_awaited_once_with("VN-9999", requester_telegram_id=222)
        assert 222 in driver_runner.pending_fixes
        assert driver_runner.pending_fixes[222]["sub_id"] == "VN-9999"
        assert driver_runner.pending_fixes[222]["slot"] == "slot_odometer"


@pytest.mark.asyncio
async def test_driver_bot_contact_sharing(driver_runner):
    """A matching self-contact stores only the independent verified binding."""
    mock_send = AsyncMock()
    store = AsyncMock(return_value=True)
    with patch.object(driver_runner, "send_message", mock_send), \
         patch("core.phone_links.upsert_phone_telegram_link", store):
        await driver_runner.handle_contact_sharing(
            chat_id=333,
            user_id=333,
            contact={"user_id": 333, "phone_number": "0988123456", "first_name": "Nguyễn", "last_name": "Văn A"},
            from_user={"id": 333, "username": "driver_a"},
        )
        store.assert_awaited_once_with("0988123456", 333, source="verified_contact", db_pool=None)
        mock_send.assert_awaited_once()
        text = mock_send.await_args[0][1]
        assert "Xác thực số điện thoại thành công" in text
        assert "098****456" in text


@pytest.mark.asyncio
async def test_driver_bot_photo_upload_blurry(driver_runner):
    """A blurry uploaded photo (< 35.0 blur score) is rejected with warning."""
    # Create an artificial flat/blurry 100x100 gray image
    img = Image.new("RGB", (100, 100), color=(128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    photo_bytes = buf.getvalue()

    driver_runner.pending_fixes[444] = {"sub_id": "VN-BLUR1", "slot": "slot_odometer"}
    mock_send = AsyncMock()

    with patch.object(driver_runner, "send_message", mock_send), \
         patch.object(driver_runner, "download_telegram_photo", AsyncMock(return_value=photo_bytes)):
        await driver_runner.handle_photo_upload(
            chat_id=444, user_id=444, photos=[{"file_id": "test_blurry_id"}]
        )
        mock_send.assert_awaited_once()
        text = mock_send.await_args[0][1]
        assert "Ảnh gửi chưa đạt chuẩn" in text
        # Pending fix must remain armed
        assert 444 in driver_runner.pending_fixes


@pytest.mark.asyncio
async def test_driver_bot_appeal_rate_limit(driver_runner):
    """Driver appeals are rate limited to max 5 per 2 hours."""
    mock_send = AsyncMock()
    with patch.object(driver_runner, "send_message", mock_send), \
         patch.object(driver_runner, "_get_submission_status", AsyncMock(return_value="REJECTED")), \
         patch.object(driver_runner, "_update_submission_status", AsyncMock(return_value=(True, "APPEAL_PENDING"))), \
         patch.object(driver_runner, "get_driver_phone", AsyncMock(return_value="0988111222")):
        # Appeals 1 to 5 should succeed (clearing active appeal between calls to test rate limit)
        for i in range(5):
            driver_runner.active_appeals.clear()
            await driver_runner.handle_driver_appeal(chat_id=555, user_id=555, sub_id=f"VN-APP{i}")
            assert "đã được gửi thành công" in mock_send.await_args[0][1]

        # Appeal 6 must be rate-limited
        mock_send.reset_mock()
        driver_runner.active_appeals.clear()
        await driver_runner.handle_driver_appeal(chat_id=555, user_id=555, sub_id="VN-APP6")
        mock_send.assert_awaited_once()
        text = mock_send.await_args[0][1]
        assert "tối đa 5" in text


@pytest.mark.asyncio
async def test_driver_bot_prevent_duplicate_active_and_cancel(driver_runner):
    """Prevents duplicate active requests and allows cancelling."""
    mock_send = AsyncMock()
    status_state = {"value": "REJECTED"}

    async def fake_status(_sub_id, *, requester_telegram_id=None):
        assert requester_telegram_id in (None, 601)
        return status_state["value"]

    async def fake_update(_sub_id, new_status, *, reason, actor, requester_telegram_id, expected_statuses=("REJECTED",)):
        assert requester_telegram_id == 601 and actor == "driver:601"
        if status_state["value"] not in expected_statuses:
            return False, status_state["value"]
        status_state["value"] = new_status
        if new_status != "APPEAL_PENDING":
            driver_runner._clear_active_appeal(_sub_id)
        return True, new_status

    with patch.object(driver_runner, "send_message", mock_send), \
         patch.object(driver_runner, "_get_submission_status", side_effect=fake_status), \
         patch.object(driver_runner, "_update_submission_status", side_effect=fake_update):
        # Step 1: Submit first appeal
        await driver_runner.handle_driver_appeal(chat_id=601, user_id=601, sub_id="VN-ACTIVE1")
        assert "đã được gửi thành công" in mock_send.await_args[0][1]

        # Step 2: Try to appeal again while active -> blocked
        mock_send.reset_mock()
        await driver_runner.handle_driver_appeal(chat_id=601, user_id=601, sub_id="VN-ACTIVE1")
        assert "đã có một yêu cầu kiểm tra/phúc khảo đang hoạt động" in mock_send.await_args[0][1]

        # Step 3: Cancel active appeal via /cancel
        mock_send.reset_mock()
        await driver_runner.handle_cancel_request(chat_id=601, user_id=601, target_arg="VN-ACTIVE1")
        assert "Đã hủy thành công" in mock_send.await_args[0][1]
        assert 601 not in driver_runner.active_appeals

@pytest.mark.asyncio
async def test_driver_bot_removed_privacy_command_falls_back(driver_runner):
    """The /privacy command was removed from the CTV bot."""
    mock_send = AsyncMock()
    with patch.object(driver_runner, "send_message", mock_send):
        await driver_runner.handle_driver_command(
            chat_id=666, user_id=666, text="/privacy", from_user={"first_name": "CTV"}
        )
        mock_send.assert_awaited_once()
        assert "Nghị định 13" not in mock_send.await_args[0][1]
        assert "Bạn cần hỗ trợ gì?" in mock_send.await_args[0][1]


@pytest.mark.asyncio
async def test_partner_history_stays_in_verified_private_chat(driver_runner):
    driver_runner.handle_driver_surveys_lookup = AsyncMock()
    for chat in ({"id": 777, "type": "supergroup"}, {"id": 777, "type": "private"}):
        await driver_runner.handle_update({"message": {"chat": chat, "text": "/surveys", "from": {"id": 778}}})
    driver_runner.handle_driver_surveys_lookup.assert_not_awaited()
    await driver_runner.handle_update({"message": {"chat": {"id": 777, "type": "private"}, "from": {"id": 777}, "text": "/surveys"}})
    driver_runner.handle_driver_surveys_lookup.assert_awaited_once_with(777, 777)


@pytest.mark.asyncio
async def test_driver_bot_support_request_direct(driver_runner, monkeypatch):
    """Direct /support message forwards to admin group and confirms to user."""
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", "123:test-admin")
    mock_send = AsyncMock()
    mock_client = AsyncMock()
    mock_client.post = AsyncMock()

    with patch.object(driver_runner, "send_message", mock_send), \
         patch.object(driver_runner, "get_client", AsyncMock(return_value=mock_client)), \
         patch.object(driver_runner, "get_driver_phone", AsyncMock(return_value="0988555666")):
        await driver_runner.handle_update({"message": {
            "chat": {"id": 888, "type": "private"},
            "from": {"id": 888, "first_name": "Lê", "last_name": "Văn C", "username": "levanc"},
            "text": "/support Tôi cần hỗ trợ kiểm tra tiền thưởng phiếu VN-999",
        }})
        mock_send.assert_awaited_once()
        user_confirm = mock_send.await_args[0][1]
        assert "Đã gửi yêu cầu hỗ trợ tới Ban Quản Trị" in user_confirm
        assert "VN-999" in user_confirm

        # Admin group alert must be posted
        mock_client.post.assert_awaited_once()
        admin_payload = mock_client.post.await_args[1]["json"]
        assert "YÊU CẦU TRỢ GIÚP TỪ THÀNH VIÊN" in admin_payload["text"]
        assert "0988555666" in admin_payload["text"]
        assert "VN-999" in admin_payload["text"]


@pytest.mark.asyncio
async def test_driver_bot_support_interactive_flow(driver_runner, monkeypatch):
    """Tapping support button arms state, next message is forwarded to admin."""
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", "123:test-admin")
    mock_send = AsyncMock()
    mock_client = AsyncMock()
    mock_client.post = AsyncMock()

    with patch.object(driver_runner, "send_message", mock_send), \
         patch.object(driver_runner, "get_client", AsyncMock(return_value=mock_client)):
        # Step 1: User taps "🆘 Yêu Cầu Hỗ Trợ"
        await driver_runner.handle_update({
            "message": {
                "chat": {"id": 999, "type": "private"},
                "from": {"id": 999, "first_name": "Hoàng"},
                "text": "🆘 Yêu Cầu Hỗ Trợ",
            }
        })
        assert driver_runner.pending_support.get(999) is True
        prompt_text = mock_send.await_args[0][1]
        assert "Bạn cần Ban Quản Trị hỗ trợ vấn đề gì" in prompt_text

        # Step 2: User types their problem
        mock_send.reset_mock()
        await driver_runner.handle_update({
            "message": {
                "chat": {"id": 999, "type": "private"},
                "from": {"id": 999, "first_name": "Hoàng"},
                "text": "Tôi không chụp được ảnh đồng hồ xe máy",
            }
        })
        assert 999 not in driver_runner.pending_support
        confirm_text = mock_send.await_args[0][1]
        assert "Đã gửi yêu cầu hỗ trợ" in confirm_text
        assert "Tôi không chụp được ảnh đồng hồ xe máy" in confirm_text

        # Admin alert posted
        mock_client.post.assert_awaited_once()
        admin_payload = mock_client.post.await_args[1]["json"]
        assert "Tôi không chụp được ảnh đồng hồ xe máy" in admin_payload["text"]


@pytest.mark.asyncio
async def test_unverified_bot_history_never_reads_storage(driver_runner):
    """An unverified sender must not fall back to everyone else's DEV rows."""
    lookup = AsyncMock(return_value=None)
    send = AsyncMock()
    pool = MagicMock()
    driver_runner.db_pool = pool
    with patch("core.phone_links.get_verified_phone_binding_for_telegram_user", lookup), \
         patch.object(driver_runner, "send_message", send), \
         patch("builtins.open", side_effect=AssertionError("unverified history must not read files")):
        await driver_runner.handle_driver_surveys_lookup(chat_id=333, user_id=333)
    lookup.assert_awaited_once_with(333, db_pool=pool, sources=("verified_contact",))
    pool.connection.assert_not_called()
    assert "chưa xác thực" in send.await_args.args[1]


@pytest.mark.asyncio
async def test_verified_bot_history_uses_only_bound_phone(driver_runner, tmp_path, monkeypatch):
    """A verified phone never grants pre-verification or undated DEV history."""
    storage = tmp_path / "dev_submissions.json"
    records = [
        {"submission_id": "DEV-OWN-H1", "driver_phone": "0988123456", "status": "APPROVED", "submitted_at": "2026-01-02T00:00:00+00:00"},
        {"submission_id": "DEV-OTHER-H1", "driver_phone": "0911111111", "status": "REJECTED", "submitted_at": "2026-01-02T00:00:00+00:00"},
        {"submission_id": "DEV-OLD-OWNER", "driver_phone": "0988123456", "status": "APPROVED", "submitted_at": "2020-01-01T00:00:00+00:00"},
        {"submission_id": "DEV-UNKNOWN-DATE", "driver_phone": "0988123456", "status": "APPROVED"},
        {"submission_id": "DEV-NAIVE-DATE", "driver_phone": "0988123456", "status": "APPROVED", "submitted_at": "2026-01-02T00:00:00"},
    ]
    for row in records:
        row["form_data"] = {"_delivery_policy": {"source": "question", "question_id": "target"},
                            "target": row.pop("driver_phone")}
    storage.write_text(json.dumps(records), encoding="utf-8")
    monkeypatch.setattr("services.telegram_polling.DEV_STORAGE_FILE", str(storage))
    lookup = AsyncMock(return_value=("0988123456", datetime(2026, 1, 1, tzinfo=timezone.utc)))
    send = AsyncMock()
    with patch("core.phone_links.get_verified_phone_binding_for_telegram_user", lookup), \
         patch.object(driver_runner, "send_tracked", send):
        await driver_runner.handle_driver_surveys_lookup(chat_id=333, user_id=333)
    lookup.assert_awaited_once_with(333, db_pool=None, sources=("verified_contact",))
    body = send.await_args.args[2]
    assert "DEV-OWN-H1" in body
    assert "DEV-OTHER-H1" not in body
    assert "DEV-OLD-OWNER" not in body
    assert "DEV-UNKNOWN-DATE" not in body
    assert "DEV-NAIVE-DATE" not in body


@pytest.mark.asyncio
async def test_bot_history_is_not_disclosed_to_group_chat(driver_runner):
    lookup = AsyncMock(side_effect=AssertionError("group request must not read identity"))
    send = AsyncMock()
    with patch("core.phone_links.get_verified_phone_binding_for_telegram_user", lookup), \
         patch.object(driver_runner, "send_message", send):
        await driver_runner.handle_driver_surveys_lookup(chat_id=-100, user_id=333)
    lookup.assert_not_awaited()
    assert "trò chuyện riêng" in send.await_args.args[1]
