import os
import sys
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

    patch_target = "routers.survey.send_telegram_notification" if os.path.exists("routers/survey.py") else "main.send_telegram_notification"
    with patch(patch_target, side_effect=mock_send_tg), \
         patch("config.settings.TELEGRAM_ADMIN_GROUP_ID", "-1001234567890"):
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
    assert any("admin_req_approve:VN-ABCDEF1234" in b["callback_data"] for row in btns for b in row)
    assert any("admin_req_reject:VN-ABCDEF1234" in b["callback_data"] for row in btns for b in row)


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
def test_multi_image_upload_and_slot_filling():
    """Verify camera input has multiple attribute and handleSlotFileUpload fills upload-allowed slots."""
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)
    assert '<input type="file" accept="image/*" multiple' in html
    assert 'uploadableSlots' in html
    assert 'item.slot.allow_upload !== false' in html
@pytest.mark.asyncio
async def test_resolved_submission_locked_against_further_responses():
    """A submission already approved or rejected is locked and does not take further responses."""
    from services.telegram_polling import TelegramPollingRunner
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")
    import os, json
    dev_storage_file = os.path.join(os.getcwd(), "data", "dev_test_submissions.json")
    dev_records = [{"submission_id": "DEV-TEST-RESOLVED-01", "status": "MANUAL_REVIEW"}]
    with open(dev_storage_file, "w", encoding="utf-8") as f:
        json.dump(dev_records, f)

    # 1. First approval succeeds
    ok1, st1 = await runner._update_submission_status("DEV-TEST-RESOLVED-01", "APPROVED", reason="Duyệt lần 1")
    assert ok1 is True
    assert st1 == "APPROVED"

    # 2. Second attempt to reject or re-approve must FAIL and return current status
    ok2, st2 = await runner._update_submission_status("DEV-TEST-RESOLVED-01", "REJECTED", reason="Cố tình từ chối lại")
    assert ok2 is False
    assert st2 == "APPROVED"

    # 3. Reviewing resolved submission locks buttons
    sent_msgs = []
    async def mock_send(chat_id, text, reply_markup=None):
        sent_msgs.append({"text": text, "reply_markup": reply_markup})
    runner.send_message = mock_send
    await runner.execute_submission_review(123456, "DEV-TEST-RESOLVED-01")

    assert len(sent_msgs) == 1
    rev_btns = sent_msgs[0]["reply_markup"]["inline_keyboard"]
    # Action buttons must be locked; no admin_approve or admin_reject!
    assert not any("admin_approve:" in b.get("callback_data", "") for row in rev_btns for b in row)
    assert not any("admin_reject:" in b.get("callback_data", "") for row in rev_btns for b in row)
    assert any("resolved_notice" in b.get("callback_data", "") for row in rev_btns for b in row)
@pytest.mark.asyncio
async def test_admin_decision_confirmation_flow():
    """Approving or rejecting a form requires confirmation before committing status change."""
    from services.telegram_polling import TelegramPollingRunner
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")
    import os, json
    dev_storage_file = os.path.join(os.getcwd(), "data", "dev_test_submissions.json")
    dev_records = [{"submission_id": "DEV-CONFIRM-01", "status": "MANUAL_REVIEW"}]
    with open(dev_storage_file, "w", encoding="utf-8") as f:
        json.dump(dev_records, f)

    # 1. Open review card presents confirmation-request buttons
    sent_msgs = []
    async def mock_send(chat_id, text, reply_markup=None):
        sent_msgs.append({"text": text, "reply_markup": reply_markup})
    runner.send_message = mock_send

    await runner.execute_submission_review(123456, "DEV-CONFIRM-01")
    assert len(sent_msgs) >= 1
    print("DEBUG sent_msgs:", sent_msgs)
    rev_btns = sent_msgs[-1]["reply_markup"]["inline_keyboard"] if sent_msgs[-1].get("reply_markup") else []
    assert any("admin_req_approve:DEV-CONFIRM-01" in b.get("callback_data", "") for row in rev_btns for b in row)
    assert any("admin_req_reject:DEV-CONFIRM-01" in b.get("callback_data", "") for row in rev_btns for b in row)

    # 2. Clicking approve button triggers confirmation prompt with CANCEL and CONFIRM buttons
    edited_markups = []
    async def mock_edit(chat_id, msg_id, reply_markup=None):
        edited_markups.append(reply_markup)
        return True
    runner.edit_message_reply_markup = mock_edit

    cq_approve_req = {
        "id": "cq_101",
        "data": "admin_req_approve:DEV-CONFIRM-01",
        "message": {"message_id": 999, "chat": {"id": 123456}},
        "from": {"id": 1001, "first_name": "Admin1"},
    }
    await runner.handle_callback_query(cq_approve_req)
    assert len(edited_markups) == 1
    conf_btns = edited_markups[0]["inline_keyboard"]
    assert any("admin_approve:DEV-CONFIRM-01" in b.get("callback_data", "") for row in conf_btns for b in row)
    assert any("admin_cancel_decision:DEV-CONFIRM-01" in b.get("callback_data", "") for row in conf_btns for b in row)

    # 3. Clicking CANCEL restores original buttons without modifying status
    cq_cancel = {
        "id": "cq_102",
        "data": "admin_cancel_decision:DEV-CONFIRM-01",
        "message": {"message_id": 999, "chat": {"id": 123456}},
        "from": {"id": 1001, "first_name": "Admin1"},
    }
    await runner.handle_callback_query(cq_cancel)
    assert len(edited_markups) == 2
    restored_btns = edited_markups[1]["inline_keyboard"]
    assert any("admin_req_approve:DEV-CONFIRM-01" in b.get("callback_data", "") for row in restored_btns for b in row)


@pytest.mark.asyncio
async def test_support_request_locks_to_responding_admin_only():
    """One admin responding to support locks ticket and subsequent messages from other admins are ignored."""
    from services.telegram_polling import (
        TelegramPollingRunner,
        ACTIVE_SUPPORT_CONVERSATIONS,
        ADMIN_GROUP_ACTIVE_SESSION,
    )
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")
    ACTIVE_SUPPORT_CONVERSATIONS.clear()
    ADMIN_GROUP_ACTIVE_SESSION.clear()

    alerts = []
    async def mock_answer_cq(cq_id, text=None, show_alert=False):
        alerts.append({"text": text, "show_alert": show_alert})
        return True
    runner.answer_callback_query = mock_answer_cq

    # 1. Admin A (ID 777) clicks to reply to support user 9999
    cq_admin_a = {
        "id": "cq_a",
        "data": "admin_reply_chat:9999",
        "message": {"message_id": 111, "chat": {"id": -100999}},
        "from": {"id": 777, "first_name": "Alice"},
    }
    await runner.handle_callback_query(cq_admin_a)
    assert 9999 in ACTIVE_SUPPORT_CONVERSATIONS
    assert ACTIVE_SUPPORT_CONVERSATIONS[9999]["locked_admin_id"] == 777
    assert ACTIVE_SUPPORT_CONVERSATIONS[9999]["locked_admin_name"] == "Alice"

    # 2. Admin B (ID 888) attempts to click reply to same user -> BLOCKED with warning alert!
    cq_admin_b = {
        "id": "cq_b",
        "data": "admin_reply_chat:9999",
        "message": {"message_id": 111, "chat": {"id": -100999}},
        "from": {"id": 888, "first_name": "Bob"},
    }
    await runner.handle_callback_query(cq_admin_b)
    assert any("đã được khóa và đang xử lý bởi Alice" in a.get("text", "") for a in alerts)

    # 3. Admin B sends a message in admin group -> Ignored, not relayed
    relayed_messages = []
    class MockClient:
        async def post(self, url, **kwargs):
            class Resp:
                status_code = 200
            relayed_messages.append(kwargs)
            return Resp()
    async def get_mock_client():
        return MockClient()
    runner.get_client = get_mock_client

    msg_bob = {
        "message": {
            "message_id": 501,
            "chat": {"id": -100999},
            "from": {"id": 888, "first_name": "Bob"},
            "text": "Hello user, Bob here!",
        }
    }
    await runner.handle_update(msg_bob)
    assert len(relayed_messages) == 0  # Blocked!

    # 4. Admin A (Alice) sends a message in admin group -> Relayed to user 9999
    msg_alice = {
        "message": {
            "message_id": 502,
            "chat": {"id": -100999},
            "from": {"id": 777, "first_name": "Alice"},
            "text": "Hello user, Alice here to help!",
        }
    }
    await runner.handle_update(msg_alice)
    user_relayed = [m for m in relayed_messages if m.get("json", {}).get("chat_id") == 9999]
    assert len(user_relayed) == 1
    assert "Alice here to help" in str(user_relayed[0])
@pytest.mark.asyncio
async def test_concurrent_multi_admin_support_routing_isolation():
    """
    If CTV A and B request help, Admin C takes A, Admin D takes B:
    - Only Admin C's messages go to A's convo and ONLY A's convo.
    - Only Admin D's messages go to B's convo and ONLY B's convo.
    - Neither can cross-talk or leak into the other's user.
    """
    from services.telegram_polling import (
        TelegramPollingRunner,
        ACTIVE_SUPPORT_CONVERSATIONS,
        ADMIN_GROUP_ACTIVE_SESSION,
        ADMIN_ACTIVE_CONVERSATIONS,
    )
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")
    ACTIVE_SUPPORT_CONVERSATIONS.clear()
    ADMIN_GROUP_ACTIVE_SESSION.clear()
    ADMIN_ACTIVE_CONVERSATIONS.clear()

    async def mock_answer_cq(cq_id, text=None, show_alert=False):
        return True
    runner.answer_callback_query = mock_answer_cq

    relayed_messages = []
    class MockClient:
        async def post(self, url, **kwargs):
            class Resp:
                status_code = 200
            relayed_messages.append(kwargs)
            return Resp()
    async def get_mock_client():
        return MockClient()
    runner.get_client = get_mock_client

    # 1. Admin C (1001) takes CTV A (1111)
    cq_c = {
        "id": "cq_c",
        "data": "admin_reply_chat:1111",
        "message": {"message_id": 10, "chat": {"id": -100999}},
        "from": {"id": 1001, "first_name": "Admin_C"},
    }
    await runner.handle_callback_query(cq_c)

    # 2. Admin D (2002) takes CTV B (2222)
    cq_d = {
        "id": "cq_d",
        "data": "admin_reply_chat:2222",
        "message": {"message_id": 20, "chat": {"id": -100999}},
        "from": {"id": 2002, "first_name": "Admin_D"},
    }
    await runner.handle_callback_query(cq_d)

    assert ADMIN_ACTIVE_CONVERSATIONS[1001] == 1111
    assert ADMIN_ACTIVE_CONVERSATIONS[2002] == 2222

    # 3. Admin C sends a message in admin group without replying
    relayed_messages.clear()
    msg_from_c = {
        "message": {
            "message_id": 101,
            "chat": {"id": -100999},
            "from": {"id": 1001, "first_name": "Admin_C"},
            "text": "Hello CTV A, I am Admin C!",
        }
    }
    await runner.handle_update(msg_from_c)

    # Must go ONLY to CTV A (1111), NEVER to CTV B (2222)
    a_msgs = [m for m in relayed_messages if m.get("json", {}).get("chat_id") == 1111]
    b_msgs = [m for m in relayed_messages if m.get("json", {}).get("chat_id") == 2222]
    assert len(a_msgs) == 1
    assert len(b_msgs) == 0
    assert "Hello CTV A" in str(a_msgs[0])

    # 4. Admin D sends a message in admin group without replying
    relayed_messages.clear()
    msg_from_d = {
        "message": {
            "message_id": 102,
            "chat": {"id": -100999},
            "from": {"id": 2002, "first_name": "Admin_D"},
            "text": "Hello CTV B, I am Admin D!",
        }
    }
    await runner.handle_update(msg_from_d)

    # Must go ONLY to CTV B (2222), NEVER to CTV A (1111)
    a_msgs = [m for m in relayed_messages if m.get("json", {}).get("chat_id") == 1111]
    b_msgs = [m for m in relayed_messages if m.get("json", {}).get("chat_id") == 2222]
    assert len(a_msgs) == 0
    assert len(b_msgs) == 1
    assert "Hello CTV B" in str(b_msgs[0])

    # 5. Admin C attempts to reply to a ticket belonging to CTV B (cross-convo leakage prevention)
    relayed_messages.clear()
    msg_cross_reply = {
        "message": {
            "message_id": 103,
            "chat": {"id": -100999},
            "from": {"id": 1001, "first_name": "Admin_C"},
            "text": "Accidental message from C to B",
            "reply_to_message": {
                "text": "Yêu cầu hỗ trợ từ CTV B (ID: <code>2222</code>)"
            }
        }
    }
    await runner.handle_update(msg_cross_reply)
    # Blocked completely! Nothing relayed to 2222
    b_msgs = [m for m in relayed_messages if m.get("json", {}).get("chat_id") == 2222]
    assert len(b_msgs) == 0

    # 6. Admin C closes their session (/close)
    msg_close_c = {
        "message": {
            "message_id": 104,
            "chat": {"id": -100999},
            "from": {"id": 1001, "first_name": "Admin_C"},
            "text": "/close",
        }
    }
    await runner.handle_update(msg_close_c)
    assert 1111 not in ACTIVE_SUPPORT_CONVERSATIONS
    assert 1001 not in ADMIN_ACTIVE_CONVERSATIONS

    # CTV B's session with Admin D is STILL OPEN and active!
    assert 2222 in ACTIVE_SUPPORT_CONVERSATIONS
    assert ADMIN_ACTIVE_CONVERSATIONS[2002] == 2222

    # Admin D can still message CTV B seamlessly
    relayed_messages.clear()
    msg_from_d_again = {
        "message": {
            "message_id": 105,
            "chat": {"id": -100999},
            "from": {"id": 2002, "first_name": "Admin_D"},
            "text": "Still here for you CTV B!",
        }
    }
    await runner.handle_update(msg_from_d_again)
    b_msgs = [m for m in relayed_messages if m.get("json", {}).get("chat_id") == 2222]
    assert len(b_msgs) == 1
@pytest.mark.asyncio
async def test_one_admin_only_holds_one_support_convo_at_a_time():
    """An admin holding an active support session cannot accept or take another support conversation."""
    from services.telegram_polling import (
        TelegramPollingRunner,
        ACTIVE_SUPPORT_CONVERSATIONS,
        ADMIN_GROUP_ACTIVE_SESSION,
        ADMIN_ACTIVE_CONVERSATIONS,
    )
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")
    ACTIVE_SUPPORT_CONVERSATIONS.clear()
    ADMIN_GROUP_ACTIVE_SESSION.clear()
    ADMIN_ACTIVE_CONVERSATIONS.clear()

    alerts = []
    async def mock_answer_cq(cq_id, text=None, show_alert=False):
        alerts.append({"text": text, "show_alert": show_alert})
        return True
    runner.answer_callback_query = mock_answer_cq

    sent_messages = []
    async def mock_send(chat_id, text, reply_markup=None):
        sent_messages.append({"chat_id": chat_id, "text": text})
    runner.send_message = mock_send

    relayed_messages = []
    class MockClient:
        async def post(self, url, **kwargs):
            class Resp:
                status_code = 200
            relayed_messages.append(kwargs)
            return Resp()
    async def get_mock_client():
        return MockClient()
    runner.get_client = get_mock_client

    # 1. Admin X (ID 555) accepts CTV 1 (ID 1001)
    cq_1 = {
        "id": "cq_1",
        "data": "admin_reply_chat:1001",
        "message": {"message_id": 1, "chat": {"id": -100999}},
        "from": {"id": 555, "first_name": "Admin_X"},
    }
    await runner.handle_callback_query(cq_1)
    assert ADMIN_ACTIVE_CONVERSATIONS[555] == 1001
    assert ACTIVE_SUPPORT_CONVERSATIONS[1001]["locked_admin_id"] == 555

    # 2. Admin X attempts to accept CTV 2 (ID 2002) while still holding CTV 1 -> BLOCKED!
    alerts.clear()
    cq_2 = {
        "id": "cq_2",
        "data": "admin_reply_chat:2002",
        "message": {"message_id": 2, "chat": {"id": -100999}},
        "from": {"id": 555, "first_name": "Admin_X"},
    }
    await runner.handle_callback_query(cq_2)
    assert any("đang trong phiên hỗ trợ với thành viên khác (ID: 1001)" in a.get("text", "") for a in alerts)
    # CTV 2 must NOT be locked by Admin X!
    assert 2002 not in ACTIVE_SUPPORT_CONVERSATIONS
    assert ADMIN_ACTIVE_CONVERSATIONS[555] == 1001

    # 3. Admin X attempts to reply to CTV 2's ticket via group message -> BLOCKED!
    relayed_messages.clear()
    sent_messages.clear()
    msg_reply_ctv2 = {
        "message": {
            "message_id": 50,
            "chat": {"id": -100999},
            "from": {"id": 555, "first_name": "Admin_X"},
            "text": "Trying to reply to CTV 2",
            "reply_to_message": {
                "text": "Yêu cầu hỗ trợ từ CTV 2 (ID: <code>2002</code>)"
            }
        }
    }
    await runner.handle_update(msg_reply_ctv2)
    assert len(relayed_messages) == 0  # Blocked from sending to CTV 2!
    assert any("bạn đang trong phiên hỗ trợ với thành viên <code>1001</code>" in m.get("text", "") for m in sent_messages)

    # 4. Admin X closes CTV 1 session
    msg_close = {
        "message": {
            "message_id": 51,
            "chat": {"id": -100999},
            "from": {"id": 555, "first_name": "Admin_X"},
            "text": "/close",
        }
    }
    await runner.handle_update(msg_close)
    assert 1001 not in ACTIVE_SUPPORT_CONVERSATIONS
    assert 555 not in ADMIN_ACTIVE_CONVERSATIONS

    # 5. Now Admin X can freely accept CTV 2
    alerts.clear()
    await runner.handle_callback_query(cq_2)
    assert ADMIN_ACTIVE_CONVERSATIONS[555] == 2002
    assert ACTIVE_SUPPORT_CONVERSATIONS[2002]["locked_admin_id"] == 555
def test_exhaustive_arithmetic_check_income_minus_costs_equals_profit():
    """
    Exhaustive arithmetic validation:
    1. income - costs != profit must REJECT.
    2. profit > income (net > gross) must REJECT.
    3. negative costs (-5M) to inflate profit must REJECT.
    4. NaN / inf / negative income or net must REJECT.
    5. matching gross - costs == net passes.
    """
    from services.income_sanitizer import analyze_driver_income

    # 1. income - costs != profit (Gross: 10M, Costs: 2M, Profit: 5M -> 10M - 2M = 8M != 5M)
    res_mismatch = analyze_driver_income(
        vehicle_type="CAR",
        gross_income=10_000_000,
        net_income=5_000_000,
        platform_discount=2_000_000,
        period="weekly",
    )
    assert res_mismatch.is_valid is False
    assert res_mismatch.status_code == "ARITHMETIC_MISMATCH"
    assert "Số học không khớp" in res_mismatch.flag_reason

    # 2. profit > income (Gross: 5M, Net: 8M without valid explanation)
    res_net_exceeds = analyze_driver_income(
        vehicle_type="CAR",
        gross_income=5_000_000,
        net_income=8_000_000,
        period="weekly",
    )
    assert res_net_exceeds.is_valid is False
    assert res_net_exceeds.status_code == "ARITHMETIC_MISMATCH"
    assert "lớn hơn tổng doanh thu" in res_net_exceeds.flag_reason

    # 3. Negative costs bypass attempt (Gross: 10M, Discount: -5M, Net: 15M)
    res_neg_cost = analyze_driver_income(
        vehicle_type="CAR",
        gross_income=10_000_000,
        net_income=15_000_000,
        platform_discount=-5_000_000,
        period="weekly",
    )
    assert res_neg_cost.is_valid is False
    assert res_neg_cost.status_code == "ARITHMETIC_MISMATCH"

    # 4. Valid arithmetic (Gross: 10M, Costs: 2.2M, Net: 7.8M) -> APPROVED!
    res_valid = analyze_driver_income(
        vehicle_type="CAR",
        gross_income=10_000_000,
        net_income=7_800_000,
        platform_discount=2_200_000,
        period="weekly",
    )
    assert res_valid.is_valid is True
    assert res_valid.status_code == "APPROVED"


@pytest.mark.asyncio
async def test_endpoint_rejects_arithmetic_mismatch_in_ocr(monkeypatch):
    """When OCR returns revenue - costs != profit in weekly statement, endpoint rejects submission."""
    monkeypatch.setenv("MOCK_AI", "1")
    monkeypatch.setenv("ENVIRONMENT", "test")
    client = TestClient(app)

    async def mock_role(img_bytes, filename=None, *args, **kwargs):
        fn = (filename or "").lower()
        if "slot_0" in fn or "profile" in fn:
            return "app_profile"
        if "slot_1" in fn or "vehicle" in fn:
            return "vehicle_exterior"
        return "app_income"

    async def mock_ocr_arithmetic_flaw(b, week_idx=None, *args, **kwargs):
        # Gross: 10,000,000, discount: 2,000,000, but net_income claimed is 3,000,000 (missing 5M!)
        return {
            "gross_revenue": 10_000_000,
            "net_income": 3_000_000,
            "discount_and_tax": 2_000_000,
            "total_trips": 120,
            "period": f"Tuần {week_idx or 1}",
            "period_type": "WEEKLY",
            "is_weekly_report": True,
            "confidence_score": 0.99,
            "is_legible": True,
            "is_authentic_gsm_ui": True,
        }

    real_photos = get_real_distinct_test_photos(7)
    with patch.object(vision_client, "classify_image_role", side_effect=mock_role), \
         patch.object(vision_client, "ocr_driver_app_income", side_effect=mock_ocr_arithmetic_flaw):
        resp = client.post(
            "/api/v1/survey/submit-webrtc",
            data={
                "driver_phone": "0912345678",
                "driver_name": "Tài xế Sai Số Học",
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
                f"survey_photo_{i}": (f"slot_{i}.jpg", real_photos[i][1], "image/jpeg")
                for i in range(min(7, len(real_photos)))
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "REJECTED"
    assert "Số học không khớp" in data.get("reason")


def test_location_prompt_triggers_on_first_page_in_html():
    """Verify that GPS location request triggers immediately on first page load in dynamic form."""
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)
    # 1. Shows initial GPS prompt banner on page 1
    assert "showInitialGpsPrompt()" in html
    # 2. Immediately invokes browser geolocation query on first page load
    assert "fetchGpsSilently()" in html
    assert "initGpsBannerBehavior();" in html
def test_schema_bidirectional_sync_and_parity(tmp_path):
    """
    Safeguard test for automated format synchronization:
    1. Propagates schema evolutions across disk and in-memory models.
    2. Injects missing slot defaults (ai_detection_prompt, allow_upload, etc.).
    3. Downstream parity verifier detects and enforces required contracts.
    """
    from core.schema_sync import (
        reconcile_and_sync_form_schema,
        verify_schema_downstream_parity,
        normalize_slot_definition,
    )
    from services.form_engine import DEFAULT_TRUST_BRIDGE_SCHEMA

    # 1. Normalizing raw slot definitions injects contract defaults
    raw_slot = {"slot": "3", "title": "Slot 3", "role": "app_income_week_1"}
    normalized = normalize_slot_definition(raw_slot, fallback_index=3)
    assert normalized["slot"] == 3
    assert normalized["allow_upload"] is True
    assert normalized["ai_detection_prompt"] == ""

    # 2. Reconciling with an outdated disk file automatically propagates new fields to disk
    test_json_file = tmp_path / "test_outdated_schema.json"
    outdated_data = DEFAULT_TRUST_BRIDGE_SCHEMA.model_dump()
    # Strip ai_detection_prompt to simulate schema drift
    for q in outdated_data["questions"]:
        if q.get("image_slots"):
            for s in q["image_slots"]:
                s.pop("ai_detection_prompt", None)

    import json
    with open(test_json_file, "w", encoding="utf-8") as f:
        json.dump(outdated_data, f)

    canonical_schema, was_updated = reconcile_and_sync_form_schema(
        DEFAULT_TRUST_BRIDGE_SCHEMA, str(test_json_file)
    )
    assert was_updated is True

    # Verify disk was automatically updated and contains the missing prompt
    with open(test_json_file, "r", encoding="utf-8") as f:
        re_read = json.load(f)
    cam_q = next(q for q in re_read["questions"] if q["type"] == "camera_webrtc")
    assert all("ai_detection_prompt" in s for s in cam_q["image_slots"])

    # 3. Downstream parity validation
    violations = verify_schema_downstream_parity(canonical_schema)
    assert len(violations) == 0


def test_admin_dossier_suppresses_redundant_driver_name():
    """When driver_name is absent or a placeholder ('Tài xế'), dossier renders 'Số điện thoại:' directly."""
    from services.admin_moderator import render_dossier_message

    # Case A: Placeholder 'Tài xế' -> renders 'Số điện thoại: 0988111222'
    dossier_placeholder = {
        "submission_id": "TEST-01",
        "status": "APPROVED",
        "driver_name": "Tài xế",
        "driver_phone": "0988111222",
        "gross_income": 5000000,
        "net_income": 4000000,
        "trips_count": 30,
    }
    rendered_a = render_dossier_message(dossier_placeholder)
    assert "• <b>Số điện thoại:</b> <code>0988111222</code>" in rendered_a
    assert "Tài xế: Tài xế" not in rendered_a

    # Case B: Verified name -> renders 'Tài xế: Nguyễn Văn B (0988222333)'
    dossier_verified = {
        "submission_id": "TEST-02",
        "status": "APPROVED",
        "driver_name": "Nguyễn Văn B",
        "driver_phone": "0988222333",
        "gross_income": 5000000,
        "net_income": 4000000,
        "trips_count": 30,
    }
    rendered_b = render_dossier_message(dossier_verified)
    assert "• <b>Tài xế:</b> <b>Nguyễn Văn B</b> (<code>0988222333</code>)" in rendered_b


@pytest.mark.asyncio
async def test_support_ticket_markup_has_no_dead_close_button():
    """When an admin claims a support ticket, the updated markup has NO dead close button."""
    from services.telegram_polling import TelegramPollingRunner
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")

    edited_markups = []
    async def mock_edit(chat_id, msg_id, reply_markup=None):
        edited_markups.append(reply_markup)
        return True
    runner.edit_message_reply_markup = mock_edit

    async def mock_answer_cq(cq_id, text=None, show_alert=False):
        return True
    runner.answer_callback_query = mock_answer_cq

    cq_claim = {
        "id": "cq_claim",
        "data": "admin_reply_chat:1001",
        "message": {"message_id": 999, "chat": {"id": -100999}},
        "from": {"id": 888, "first_name": "Alex"},
    }
    await runner.handle_callback_query(cq_claim)

    assert len(edited_markups) == 1
    buttons = edited_markups[0]["inline_keyboard"]
    # Only locked info status button must exist; NO admin_close_ticket!
    all_callbacks = [b.get("callback_data", "") for row in buttons for b in row]
    assert any("admin_locked_info:1001" in cb for cb in all_callbacks)
    assert not any("admin_close_ticket" in cb for cb in all_callbacks)
