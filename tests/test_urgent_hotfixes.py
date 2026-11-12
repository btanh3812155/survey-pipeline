import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
from main import app, vision_client
from tests.real_test_samples import get_real_distinct_test_photos
from services.dynamic_form_html import render_dynamic_form_html
from services.form_engine import DEFAULT_TRUST_BRIDGE_SCHEMA, FormSchema, FormQuestion, QuestionType


def test_signature_confirm_button_event_wiring_and_cooldown():
    """Verify signature confirm button has no ontouchend (ghost click prevention) and includes debounce."""
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)
    
    # 1. Ensure ontouchend is stripped to eliminate 300ms mobile ghost-click re-openings
    assert 'ontouchend="confirmModalSig()"' not in html
    assert 'onclick="confirmModalSig(event)"' in html
    
    # 2. Ensure modal close debounce cooldown is active
    assert 'sigModalCooldown' in html
from services.duplicate_detector import check_intra_submission_duplicates


def test_per_slot_ai_detection_prompt_in_schema():
    """Verify every image slot in DEFAULT_TRUST_BRIDGE_SCHEMA has its own dedicated ai_detection_prompt."""
    cam_q = next((q for q in DEFAULT_TRUST_BRIDGE_SCHEMA.questions if q.id == "camera_receipt"), None)
    assert cam_q is not None
    assert cam_q.image_slots is not None
    assert len(cam_q.image_slots) == 7

    for slot in cam_q.image_slots:
        assert "ai_detection_prompt" in slot
        assert len(slot["ai_detection_prompt"]) > 10
        # Weekly slots must specify weekly constraint
        if "income" in slot["role"]:
            assert "TUẦN" in slot["ai_detection_prompt"]


@pytest.mark.asyncio
async def test_monthly_income_upload_rejected_in_weekly_slot(monkeypatch):
    """Submitting a monthly income statement (e.g. Tháng 1) into weekly slots is rejected by backend guard."""
    monkeypatch.setenv("MOCK_AI", "1")
    monkeypatch.setenv("ENVIRONMENT", "test")
    client = TestClient(app)

    async def mock_role(img_bytes, filename=None, *args, **kwargs):
        fn = (filename or "").lower()
        if "0" in fn or "profile" in fn:
            return "app_profile"
        return "app_income"

    async def mock_ocr_monthly(b, week_idx=None, *args, **kwargs):
        # Driver mistakenly uploads a monthly summary screen
        return {
            "gross_revenue": 18000000,
            "net_income": 14000000,
            "total_trips": 120,
            "period": "Tháng 10/2026",
            "period_type": "MONTHLY",
            "is_weekly_report": False,
            "confidence_score": 0.99,
            "is_legible": True,
            "is_authentic_gsm_ui": True,
        }

    real_photos = get_real_distinct_test_photos(7)
    with patch.object(vision_client, "classify_image_role", side_effect=mock_role), \
         patch.object(vision_client, "ocr_driver_app_income", side_effect=mock_ocr_monthly):
        resp = client.post(
            "/api/v1/survey/submit-webrtc",
            data={
                "driver_phone": "0912345678",
                "driver_name": "Tài xế Nộp Nhầm Tháng",
                "consent_choice": "Tôi đồng ý",
                "signature_png": "data:image/png;base64,mock",
                "vehicle_type": "Ô tô",
                "car_model": "VF 5 Plus",
                "gsm_duration": "Từ 6 – 12 tháng",
                "gsm_contract_type": "Xe Taxi xanh GSM",
                "daily_trips": "18",
                "monthly_income_range": "12 – 20 triệu VNĐ",
                "gsm_incentive_policy": "Có áp dụng chỉ tiêu",
                "is_test_mode": "true",
                "bypass_img_check": "false",
            },
            files={
                f"survey_photo_{i}": (real_photos[i][0], real_photos[i][1], "image/jpeg")
                for i in range(min(7, len(real_photos)))
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "REJECTED"
    assert "theo THÁNG" in data.get("reason") and "theo TUẦN" in data.get("reason")


@pytest.mark.asyncio
async def test_live_submissions_routed_to_manual_review_and_tg(monkeypatch):
    """Live submissions (is_test_mode=False) route to MANUAL_REVIEW for human Telegram vetting."""
    monkeypatch.setenv("MOCK_AI", "1")
    monkeypatch.setenv("ENVIRONMENT", "test")
    client = TestClient(app)

    async def mock_role(img_bytes, filename=None, *args, **kwargs):
        fn = (filename or "").lower()
        if "0" in fn or "profile" in fn:
            return "app_profile"
        return "app_income"

    async def mock_ocr_valid_week(b, week_idx=None, *args, **kwargs):
        w = week_idx or 1
        return {
            "gross_revenue": 2500000 * w,
            "net_income": 2000000 * w,
            "total_trips": 25,
            "period": f"Tuần {w}",
            "period_type": "WEEKLY",
            "is_weekly_report": True,
            "confidence_score": 0.99,
            "is_legible": True,
            "is_authentic_gsm_ui": True,
        }

    real_photos = get_real_distinct_test_photos(7)
    with patch.object(vision_client, "classify_image_role", side_effect=mock_role), \
         patch.object(vision_client, "ocr_driver_app_income", side_effect=mock_ocr_valid_week):
        resp = client.post(
            "/api/v1/survey/submit-webrtc",
            data={
                "driver_phone": "0988666777",
                "driver_name": "Tài xế Live Vetting",
                "consent_choice": "Tôi đồng ý",
                "signature_png": "data:image/png;base64,mock",
                "vehicle_type": "Ô tô",
                "car_model": "VF 5 Plus",
                "gsm_duration": "Từ 6 – 12 tháng",
                "gsm_contract_type": "Xe Taxi xanh GSM",
                "daily_trips": "18",
                "monthly_income_range": "12 – 20 triệu VNĐ",
                "gsm_incentive_policy": "Có áp dụng chỉ tiêu",
                "is_test_mode": "false",  # LIVE PRODUCTION SUBMISSION
                "bypass_img_check": "false",
            },
            files={
                f"survey_photo_{i}": (real_photos[i][0], real_photos[i][1], "image/jpeg")
                for i in range(min(7, len(real_photos)))
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "MANUAL_REVIEW", f"FAILED REASON: {data.get('reason')}"
    assert "Telegram" in data.get("reason")


def test_ocr_override_on_visually_similar_same_phone_screenshots():
    """Two photos of the same physical cracked phone screen are not rejected as duplicates when OCR numbers differ."""
    with open("tests/fixtures/user_phone_test/slot2_week1.png", "rb") as f:
        b1 = f.read()
    with open("tests/fixtures/user_phone_test/slot3_week2.png", "rb") as f:
        b2 = f.read()

    weekly_incomes = {
        "week_1": {"gross_revenue": 2828510, "net_income": 2321732, "total_trips": 65, "period": "07 - 13"},
        "week_2": {"gross_revenue": 3860932, "net_income": 3169166, "total_trips": 80, "period": "14 - 20"},
    }

    raw_bytes = [b"dummy0", b"dummy1", b1, b2, b"dummy4", b"dummy5", b"dummy6"]
    target_q = next(q for q in DEFAULT_TRUST_BRIDGE_SCHEMA.questions if q.id == "camera_receipt")

    is_dup, reason = check_intra_submission_duplicates(raw_bytes, target_q, weekly_incomes)
    assert is_dup is False, f"Expected duplicate check to pass via OCR override, but got: {reason}"
@pytest.mark.asyncio
async def test_telegram_admin_notification_no_license_plate_and_has_review_button(monkeypatch):
    """Admin TG message has NO license plate and includes a button to bring up the review form."""
    monkeypatch.setenv("MOCK_AI", "1")
    monkeypatch.setenv("ENVIRONMENT", "test")
    client = TestClient(app)

    dispatched_messages = []
    async def mock_send_tg(chat_id, text, reply_markup=None):
        dispatched_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})

    with patch("main.send_telegram_notification", side_effect=mock_send_tg), \
         patch("main.settings.TELEGRAM_ADMIN_GROUP_ID", "-1001234567890"):
        real_photos = get_real_distinct_test_photos(7)
        resp = client.post(
            "/api/v1/survey/submit-webrtc",
            data={
                "driver_phone": "0988999888",
                "license_plate": "29E-999.88",
                "driver_name": "Tài xế Bí Mật",
                "consent_choice": "Tôi đồng ý",
                "signature_png": "data:image/png;base64,mock",
                "vehicle_type": "Ô tô",
                "car_model": "VF 5 Plus",
                "gsm_duration": "Từ 6 – 12 tháng",
                "gsm_contract_type": "Xe Taxi xanh GSM",
                "daily_trips": "18",
                "monthly_income_range": "12 – 20 triệu VNĐ",
                "gsm_incentive_policy": "Có áp dụng chỉ tiêu",
                "is_test_mode": "false",
                "bypass_img_check": "false",
            },
            files={
                f"survey_photo_{i}": (real_photos[i][0], real_photos[i][1], "image/jpeg")
                for i in range(min(7, len(real_photos)))
            },
        )
        assert resp.status_code == 200

    admin_msg = next((m for m in dispatched_messages if str(m["chat_id"]) == "-1001234567890"), None)
    assert admin_msg is not None
    # 1. MUST NOT send license plate
    assert "Biển số xe" not in admin_msg["text"]
    assert "29E-999.88" not in admin_msg["text"]
    # 2. MUST have button to bring up review form
    assert admin_msg["reply_markup"] is not None
    buttons = admin_msg["reply_markup"]["inline_keyboard"]
    assert any("admin_review:" in b["callback_data"] for row in buttons for b in row)


@pytest.mark.asyncio
async def test_telegram_reply_with_check_runs_command_for_replied_submission():
    """Replying to a form notification with /check or /audit without sub_id extracts ID and runs."""
    from services.telegram_polling import TelegramPollingRunner
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")

    sent_messages = []
    async def mock_send(chat_id, text, reply_markup=None):
        sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
    runner.send_message = mock_send

    # 1. User replies to notification with /check
    replied_notification = {
        "text": "⏳ PHIẾU KHẢO SÁT CHỜ DUYỆT THỦ CÔNG #VN-ABCDEF1234\n• Tài xế: Nguyễn Văn A (0988111222)\n• Doanh thu: 15,000,000 VNĐ"
    }
    await runner.handle_command(
        chat_id=123456,
        text="/check",
        reply_to_message=replied_notification,
    )

    assert len(sent_messages) == 1
    resp_msg = sent_messages[0]
    assert "VN-ABCDEF1234" in resp_msg["text"]
    # The brought up form MUST have buttons to approve or reject
    assert resp_msg["reply_markup"] is not None
    btns = resp_msg["reply_markup"]["inline_keyboard"]
    assert any("admin_approve:VN-ABCDEF1234" in b["callback_data"] for row in btns for b in row)
    assert any("admin_reject:VN-ABCDEF1234" in b["callback_data"] for row in btns for b in row)


@pytest.mark.asyncio
async def test_telegram_menu_check_alone_prompts_with_fill_button():
    """Tapping /check alone without replying prompts with switch_inline_query_current_chat button to fill in ID."""
    from services.telegram_polling import TelegramPollingRunner
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")

    sent_messages = []
    async def mock_send(chat_id, text, reply_markup=None):
        sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
    runner.send_message = mock_send

    await runner.handle_command(chat_id=123456, text="/check", reply_to_message=None)
    assert len(sent_messages) == 1
    msg = sent_messages[0]
    assert "Vui lòng cung cấp mã phiếu" in msg["text"]
    assert msg["reply_markup"] is not None
    btns = msg["reply_markup"]["inline_keyboard"]
    # Must have switch_inline_query_current_chat to prefill /check in input box
    assert any(b.get("switch_inline_query_current_chat") == "/check " for row in btns for b in row)
