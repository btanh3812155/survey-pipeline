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
        assert "Chào mừng Bác tài" in text
        assert "keyboard" in markup
        assert any(btn.get("request_contact") for row in markup["keyboard"] for btn in row)


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
        assert "Ảnh chụp vẫn còn bị mờ" in text
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
