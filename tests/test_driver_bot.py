import io
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from PIL import Image

from services.telegram_polling import DriverBotRunner, TelegramPollingRunner


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
        assert "Chào mừng bạn" in text
        assert "keyboard" in markup
        assert any(btn.get("request_contact") for row in markup["keyboard"] for btn in row)
        assert any("Hỗ Trợ" in btn.get("text", "") for row in markup["keyboard"] for btn in row)


@pytest.mark.asyncio
async def test_driver_bot_start_deep_link_fix(driver_runner):
    """Driver /start fix_VN-1234_slot_odometer arms pending fix session."""
    mock_send = AsyncMock()
    with patch.object(driver_runner, "send_message", mock_send):
        await driver_runner.handle_driver_command(
            chat_id=222,
            user_id=222,
            text="/start fix_VN-9999_slot_odometer",
            from_user={"first_name": "Tài xế B"},
        )
        mock_send.assert_awaited_once()
        text = mock_send.await_args[0][1]
        assert "Bổ Sung Ảnh Cho Phiếu #VN-9999" in text
        assert 222 in driver_runner.pending_fixes
        assert driver_runner.pending_fixes[222]["sub_id"] == "VN-9999"
        assert driver_runner.pending_fixes[222]["slot"] == "slot_odometer"


@pytest.mark.asyncio
async def test_driver_bot_contact_sharing(driver_runner):
    """Sharing contact card binds phone number and welcomes driver."""
    mock_send = AsyncMock()
    with patch.object(driver_runner, "send_message", mock_send), \
         patch.object(driver_runner, "bind_driver_phone", AsyncMock()):
        await driver_runner.handle_contact_sharing(
            chat_id=333,
            user_id=333,
            contact={"phone_number": "0988123456", "first_name": "Nguyễn", "last_name": "Văn A"},
            from_user={"username": "driver_a"},
        )
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
    """Driver appeals are rate limited to max 3 per 8 hours."""
    mock_send = AsyncMock()
    with patch.object(driver_runner, "send_message", mock_send), \
         patch.object(driver_runner, "_update_submission_status", AsyncMock()), \
         patch.object(driver_runner, "get_driver_phone", AsyncMock(return_value="0988111222")):
        # Appeals 1, 2, 3 should succeed
        for i in range(3):
            await driver_runner.handle_driver_appeal(chat_id=555, user_id=555, sub_id=f"VN-APP{i}")
            assert "đã được gửi thành công" in mock_send.await_args[0][1]

        # Appeal 4 must be rate-limited
        mock_send.reset_mock()
        await driver_runner.handle_driver_appeal(chat_id=555, user_id=555, sub_id="VN-APP4")
        mock_send.assert_awaited_once()
        text = mock_send.await_args[0][1]
        assert "tối đa 3 lần phúc khảo" in text


@pytest.mark.asyncio
async def test_driver_bot_privacy_scrub_prompt(driver_runner):
    """Driver /privacy prompt displays Decree 13 notice and confirmation buttons."""
    mock_send = AsyncMock()
    with patch.object(driver_runner, "send_message", mock_send):
        await driver_runner.handle_decree13_scrub(chat_id=666, user_id=666)
        mock_send.assert_awaited_once()
        text = mock_send.await_args[0][1]
        markup = mock_send.await_args[1].get("reply_markup", {})
        assert "Nghị định 13/2023/NĐ-CP" in text
        assert "inline_keyboard" in markup
        assert any(btn.get("callback_data") == "driver_confirm_scrub" for row in markup["inline_keyboard"] for btn in row)

@pytest.mark.asyncio
async def test_driver_bot_forms_clean_no_promo_bullets(driver_runner):
    """Forms command provides clean links without promotional feature advertising."""
    mock_send = AsyncMock()
    with patch.object(driver_runner, "send_message", mock_send):
        await driver_runner.handle_driver_command(
            chat_id=777, user_id=777, text="/forms", from_user={"first_name": "CTV Test"}
        )
        mock_send.assert_awaited_once()
        text = mock_send.await_args[0][1]
        assert "Biểu Mẫu Khảo Sát Đang Mở" in text
        assert "https://survey.trustbridgevn.com/forms/" in text
        # Must NOT contain advertising marketing bullets
        assert "chống rung" not in text
        assert "tính cước bằng AI" not in text


@pytest.mark.asyncio
async def test_driver_bot_support_request_direct(driver_runner):
    """Direct /support message forwards to admin group and confirms to user."""
    mock_send = AsyncMock()
    mock_client = AsyncMock()
    mock_client.post = AsyncMock()

    with patch.object(driver_runner, "send_message", mock_send), \
         patch.object(driver_runner, "get_client", AsyncMock(return_value=mock_client)), \
         patch.object(driver_runner, "get_driver_phone", AsyncMock(return_value="0988555666")):
        await driver_runner.handle_driver_command(
            chat_id=888,
            user_id=888,
            text="/support Tôi cần hỗ trợ kiểm tra tiền thưởng phiếu VN-999",
            from_user={"first_name": "Lê", "last_name": "Văn C", "username": "levanc"},
        )
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
async def test_driver_bot_support_interactive_flow(driver_runner):
    """Tapping support button arms state, next message is forwarded to admin."""
    mock_send = AsyncMock()
    mock_client = AsyncMock()
    mock_client.post = AsyncMock()

    with patch.object(driver_runner, "send_message", mock_send), \
         patch.object(driver_runner, "get_client", AsyncMock(return_value=mock_client)):
        # Step 1: User taps "🆘 Yêu Cầu Hỗ Trợ"
        await driver_runner.handle_update({
            "message": {
                "chat": {"id": 999},
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
                "chat": {"id": 999},
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
