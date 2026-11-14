# -*- coding: utf-8 -*-
"""
Suite D: User-Facing Behavior & Simulation Test Suite.
Verifies all human-facing interactions, UI behaviors, and multi-party routing by simulation:
1. Form & modal buttons: signature confirmation with cooldown, reset prompts, CTV selection activation.
2. Admin decision buttons & two-step confirmation flows (Duyệt/Từ chối).
3. Admin routing simulation: 1-to-1 conversation bindings between multiple admins and CTVs.
4. 1-Admin-1-Convo Cap: strict restriction preventing ticket hoarding.
5. User-facing information display: driver name suppression when absent/placeholder, license plate stripping.
"""

import os
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from main import app
from tests.real_test_samples import get_real_distinct_test_photos
from services.dynamic_form_html import render_dynamic_form_html
from services.form_engine import DEFAULT_TRUST_BRIDGE_SCHEMA
from services.admin_moderator import render_dossier_message
from services.telegram_polling import (
    TelegramPollingRunner,
    ACTIVE_SUPPORT_CONVERSATIONS,
    ADMIN_GROUP_ACTIVE_SESSION,
    ADMIN_ACTIVE_CONVERSATIONS,
)


# ==============================================================================
# 1. User-Facing Buttons & Modals (Client UI)
# ==============================================================================

def test_signature_modal_button_event_wiring_and_cooldown():
    """
    Touchscreen Ghost-Click Prevention:
    On touchscreens, mobile browsers synthesize a fake mouse click ~300ms after touchend.
    Having both ontouchend and onclick caused double-tap ghost clicks that closed and instantly
    reopened modals. We strip ontouchend and enforce a 400ms cooldown (sigModalCooldown).
    """
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)
    # 1. Ensure ontouchend is stripped
    assert 'ontouchend="confirmModalSig()"' not in html
    assert 'onclick="confirmModalSig(event)"' in html
    # 2. Cooldown timer exists in script
    assert "sigModalCooldown" in html


# ==============================================================================
# 2. Admin Decision Buttons & Two-Step Confirmation
# ==============================================================================

@pytest.mark.asyncio
async def test_admin_decision_buttons_require_confirmation_before_committing():
    """Admin tapping Duyệt/Từ chối triggers confirmation prompt before modifying submission state."""
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")

    import os, json
    dev_storage_file = os.path.join(os.getcwd(), "data", "dev_test_submissions.json")
    os.makedirs(os.path.dirname(dev_storage_file), exist_ok=True)
    dev_records = [{"submission_id": "SIM-TEST-01", "status": "MANUAL_REVIEW"}]
    with open(dev_storage_file, "w", encoding="utf-8") as f:
        json.dump(dev_records, f)

    sent_msgs = []
    async def mock_send(chat_id, text, reply_markup=None):
        sent_msgs.append({"text": text, "reply_markup": reply_markup})
    runner.send_message = mock_send

    edited_markups = []
    async def mock_edit(chat_id, msg_id, reply_markup=None):
        edited_markups.append(reply_markup)
        return True
    runner.edit_message_reply_markup = mock_edit

    async def mock_answer_cq(cq_id, text=None, show_alert=False):
        return True
    runner.answer_callback_query = mock_answer_cq

    # 1. Open review card presents request-confirmation buttons
    await runner.execute_submission_review(123456, "SIM-TEST-01")
    assert len(sent_msgs) >= 1
    rev_btns = sent_msgs[-1]["reply_markup"]["inline_keyboard"]
    assert any("admin_req_approve:SIM-TEST-01" in b.get("callback_data", "") for row in rev_btns for b in row)

    # 2. Clicking Duyệt prompts confirmation dialog with CONFIRM and CANCEL
    cq_req = {
        "id": "cq_1",
        "data": "admin_req_approve:SIM-TEST-01",
        "message": {"message_id": 100, "chat": {"id": 123456}},
        "from": {"id": 888, "first_name": "Admin1"},
    }
    await runner.handle_callback_query(cq_req)
    assert len(edited_markups) == 1
    conf_btns = edited_markups[0]["inline_keyboard"]
    assert any("admin_approve:SIM-TEST-01" in b.get("callback_data", "") for row in conf_btns for b in row)
    assert any("admin_cancel_decision:SIM-TEST-01" in b.get("callback_data", "") for row in conf_btns for b in row)

    # 3. Confirming commits status change and permanently locks card
    cq_confirm = {
        "id": "cq_2",
        "data": "admin_approve:SIM-TEST-01",
        "message": {"message_id": 100, "chat": {"id": 123456}},
        "from": {"id": 888, "first_name": "Admin1"},
    }
    await runner.handle_callback_query(cq_confirm)
    assert len(edited_markups) == 2
    final_btns = edited_markups[1]["inline_keyboard"]
    assert any("resolved_notice" in b.get("callback_data", "") for row in final_btns for b in row)


@pytest.mark.asyncio
async def test_support_ticket_markup_has_no_dead_close_button():
    """Support ticket button card contains ONLY active locked status; no dead close button."""
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
        "id": "cq_c",
        "data": "admin_reply_chat:1001",
        "message": {"message_id": 999, "chat": {"id": -100999}},
        "from": {"id": 888, "first_name": "Alex"},
    }
    await runner.handle_callback_query(cq_claim)

    assert len(edited_markups) == 1
    buttons = edited_markups[0]["inline_keyboard"]
    all_callbacks = [b.get("callback_data", "") for row in buttons for b in row]
    assert any("admin_locked_info:1001" in cb for cb in all_callbacks)
    assert not any("admin_close_ticket" in cb for cb in all_callbacks)


# ==============================================================================
# 3. Admin Routing & 1-to-1 Multi-Party Simulation
# ==============================================================================

@pytest.mark.asyncio
async def test_simulated_multi_admin_routing_isolation():
    """
    Simulation:
    CTV A and CTV B request support simultaneously.
    Admin C accepts A; Admin D accepts B.
    - Admin C messages route exclusively to A.
    - Admin D messages route exclusively to B.
    - Accidental cross-replies are blocked.
    """
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
    await runner.handle_callback_query({
        "id": "c1",
        "data": "admin_reply_chat:1111",
        "message": {"message_id": 10, "chat": {"id": -100999}},
        "from": {"id": 1001, "first_name": "Admin_C"},
    })

    # 2. Admin D (2002) takes CTV B (2222)
    await runner.handle_callback_query({
        "id": "d1",
        "data": "admin_reply_chat:2222",
        "message": {"message_id": 20, "chat": {"id": -100999}},
        "from": {"id": 2002, "first_name": "Admin_D"},
    })

    assert ADMIN_ACTIVE_CONVERSATIONS[1001] == 1111
    assert ADMIN_ACTIVE_CONVERSATIONS[2002] == 2222

    # 3. Admin C sends message -> goes ONLY to CTV A
    relayed_messages.clear()
    await runner.handle_update({
        "message": {
            "message_id": 101,
            "chat": {"id": -100999},
            "from": {"id": 1001, "first_name": "Admin_C"},
            "text": "Message to CTV A",
        }
    })
    a_msgs = [m for m in relayed_messages if m.get("json", {}).get("chat_id") == 1111]
    b_msgs = [m for m in relayed_messages if m.get("json", {}).get("chat_id") == 2222]
    assert len(a_msgs) == 1
    assert len(b_msgs) == 0

    # 4. Admin D sends message -> goes ONLY to CTV B
    relayed_messages.clear()
    await runner.handle_update({
        "message": {
            "message_id": 102,
            "chat": {"id": -100999},
            "from": {"id": 2002, "first_name": "Admin_D"},
            "text": "Message to CTV B",
        }
    })
    a_msgs = [m for m in relayed_messages if m.get("json", {}).get("chat_id") == 1111]
    b_msgs = [m for m in relayed_messages if m.get("json", {}).get("chat_id") == 2222]
    assert len(a_msgs) == 0
    assert len(b_msgs) == 1


@pytest.mark.asyncio
async def test_simulated_one_admin_one_convo_cap():
    """An admin already holding an active support session cannot claim another ticket."""
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")
    ACTIVE_SUPPORT_CONVERSATIONS.clear()
    ADMIN_GROUP_ACTIVE_SESSION.clear()
    ADMIN_ACTIVE_CONVERSATIONS.clear()

    alerts = []
    async def mock_answer_cq(cq_id, text=None, show_alert=False):
        alerts.append({"text": text, "show_alert": show_alert})
        return True
    runner.answer_callback_query = mock_answer_cq

    # 1. Admin accepts Ticket 1
    await runner.handle_callback_query({
        "id": "c1",
        "data": "admin_reply_chat:1001",
        "message": {"message_id": 1, "chat": {"id": -100999}},
        "from": {"id": 555, "first_name": "Admin_X"},
    })
    assert ADMIN_ACTIVE_CONVERSATIONS[555] == 1001

    # 2. Admin attempts to claim Ticket 2 -> BLOCKED
    alerts.clear()
    await runner.handle_callback_query({
        "id": "c2",
        "data": "admin_reply_chat:2002",
        "message": {"message_id": 2, "chat": {"id": -100999}},
        "from": {"id": 555, "first_name": "Admin_X"},
    })
    assert any("đang trong phiên hỗ trợ với thành viên khác" in a.get("text", "") for a in alerts)
    assert 2002 not in ACTIVE_SUPPORT_CONVERSATIONS


# ==============================================================================
# 4. User-Facing Information Display Checks
# ==============================================================================

def test_user_facing_driver_name_suppression_when_placeholder():
    """When driver name is missing or placeholder ('Tài xế'), dossier displays 'Số điện thoại:' directly."""
    # Placeholder
    dossier_anon = {
        "submission_id": "VN-01",
        "status": "APPROVED",
        "driver_name": "Tài xế",
        "driver_phone": "0912345678",
    }
    out_anon = render_dossier_message(dossier_anon)
    assert "• <b>Số điện thoại:</b> <code>0912345678</code>" in out_anon
    assert "Tài xế: Tài xế" not in out_anon

    # Verified
    dossier_named = {
        "submission_id": "VN-02",
        "status": "APPROVED",
        "driver_name": "Trần Văn C",
        "driver_phone": "0987654321",
    }
    out_named = render_dossier_message(dossier_named)
    assert "• <b>Tài xế:</b> <b>Trần Văn C</b> (<code>0987654321</code>)" in out_named


@pytest.mark.asyncio
async def test_telegram_admin_notification_license_plate_stripped(monkeypatch):
    """Admin group alert notification strips license plate to prevent privacy leakage."""
    monkeypatch.setenv("MOCK_AI", "1")
    monkeypatch.setenv("ENVIRONMENT", "test")
    client = TestClient(app)

    dispatched = []
    async def mock_send_tg(chat_id, text, reply_markup=None):
        dispatched.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})

    patch_target = "routers.survey.send_telegram_notification" if os.path.exists("routers/survey.py") else "main.send_telegram_notification"
    with patch(patch_target, side_effect=mock_send_tg), \
         patch("config.settings.TELEGRAM_ADMIN_GROUP_ID", "-1001234567890"):
        real_photos = get_real_distinct_test_photos(7)
        resp = client.post(
            "/api/v1/survey/submit-webrtc",
            data={
                "driver_phone": "0988999888",
                "license_plate": "29E-999.88",
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
                f"survey_photo_{i}": (f"slot_{i}.jpg", real_photos[i][1], "image/jpeg")
                for i in range(min(7, len(real_photos)))
            },
        )
        assert resp.status_code == 200

    admin_msg = next((m for m in dispatched if str(m["chat_id"]) == "-1001234567890"), None)
    assert admin_msg is not None
    assert "29E-999.88" not in admin_msg["text"]
    assert "Biển số xe" not in admin_msg["text"]
    assert admin_msg["reply_markup"] is not None
# ==============================================================================
# 5. Simulated User Actions & Edge-Case Input Checks
# ==============================================================================

def test_simulated_user_floating_reset_button_clearing():
    """Simulates user filling form and clicking floating reset: all state, inputs, and drafts are purged."""
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)
    # 1. Reset button exists as floating overlay with prompt handler
    assert "btn-floating-reset" in html
    assert "promptResetForm()" in html
    # 2. Reset execution purges localStorage draft and deep-resets camera slots
    assert "executeResetForm(" in html
    assert "localStorage.removeItem(storageKey)" in html
    assert "delete answers[k]" in html
    assert "selectCameraSlot(q.id, 0)" in html


@pytest.mark.asyncio
async def test_simulated_user_bypass_checkbox_toggle_not_sticky(monkeypatch):
    """
    Simulates user toggling 'Bỏ qua kiểm tra ảnh':
    1. Submitting with bypass_img_check=true approves test mode.
    2. Submitting with bypass_img_check=false immediately enforces real checks (not sticky!).
    """
    monkeypatch.setenv("MOCK_AI", "1")
    monkeypatch.setenv("ENVIRONMENT", "test")
    client = TestClient(app)

    # Submission 1: Checked bypass -> passes
    resp_bypassed = client.post(
        "/api/v1/survey/submit-webrtc",
        data={
            "driver_phone": "0911222333",
            "is_test_mode": "true",
            "bypass_img_check": "true",
            "consent_choice": "Tôi đồng ý",
            "signature_png": "data:image/png;base64,mock",
            "vehicle_type": "Ô tô",
            "car_model": "VF 5 Plus",
            "gsm_duration": "Từ 6 – 12 tháng",
            "gsm_contract_type": "Xe Taxi xanh GSM",
            "daily_trips": "18",
            "monthly_income_range": "12 – 20 triệu VNĐ",
            "gsm_incentive_policy": "Có áp dụng chỉ tiêu",
        },
    )
    assert resp_bypassed.status_code == 200
    assert resp_bypassed.json().get("status") == "APPROVED"

    # Submission 2: User explicitly unchecks bypass -> must NOT remain bypassed!
    resp_unbypassed = client.post(
        "/api/v1/survey/submit-webrtc",
        data={
            "driver_phone": "0911222333",
            "is_test_mode": "true",
            "bypass_img_check": "false",
            "consent_choice": "Tôi đồng ý",
            "signature_png": "data:image/png;base64,mock",
            "vehicle_type": "Ô tô",
            "car_model": "VF 5 Plus",
            "gsm_duration": "Từ 6 – 12 tháng",
            "gsm_contract_type": "Xe Taxi xanh GSM",
            "daily_trips": "18",
            "monthly_income_range": "12 – 20 triệu VNĐ",
            "gsm_incentive_policy": "Có áp dụng chỉ tiêu",
        },
    )
    assert resp_unbypassed.status_code == 200
    # Must reject because photos were not attached!
    assert resp_unbypassed.json().get("status") == "REJECTED"


def test_simulated_user_multi_image_upload_stays_on_slot_without_camera():
    """
    Simulates user choosing file from gallery:
    Verifies that slot selection keeps camera off and stays on slot (no auto-jump, no camera turn-on).
    """
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)
    assert "selectCameraSlot(qid, targetIdx, false)" in html
    assert "shouldActivateCamera = false" in html


def test_simulated_user_ctv_draft_isolation_client_only():
    """Simulates draft persistence: verifies draft endpoint is client_only to prevent server-side phone leaks."""
    client = TestClient(app)
    resp = client.get("/api/v1/forms/draft/ref-ctv004")
    assert resp.status_code == 200
    assert resp.json().get("status") == "client_only"

    # Saving drafts also returns client_only
    resp_post = client.post(
        "/api/v1/forms/draft",
        json={"ctv_slug": "ref-ctv004", "draft_data": {"driver_phone": "0580800056"}},
    )
    assert resp_post.status_code == 200
    assert resp_post.json().get("status") == "client_only"


@pytest.mark.asyncio
async def test_simulated_admin_swipe_reply_check_extracts_sub_id():
    """Simulates admin swiping right on a Telegram notification to reply /check: extracts sub_id seamlessly."""
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")

    import os, json
    dev_storage_file = os.path.join(os.getcwd(), "data", "dev_test_submissions.json")
    os.makedirs(os.path.dirname(dev_storage_file), exist_ok=True)
    with open(dev_storage_file, "w", encoding="utf-8") as f:
        json.dump([{"submission_id": "VN-SWIPE-99", "status": "MANUAL_REVIEW"}], f)

    sent_msgs = []
    async def mock_send(chat_id, text, reply_markup=None):
        sent_msgs.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
    runner.send_message = mock_send

    # Admin sends /check replying to a message containing the submission ID
    msg_reply = {
        "text": "/check",
        "reply_to_message": {
            "text": "⏳ PHIẾU KHẢO SÁT CHỜ DUYỆT THỦ CÔNG #VN-SWIPE-99\n• Số điện thoại: 0988111222\n• Doanh thu: 15,000,000 VNĐ"
        }
    }
    await runner.handle_command(123456, "/check", reply_to_message=msg_reply["reply_to_message"])

    assert len(sent_msgs) >= 1
    resp = sent_msgs[-1]
    assert "VN-SWIPE-99" in resp["text"]
    assert resp["reply_markup"] is not None


@pytest.mark.asyncio
async def test_simulated_admin_menu_check_alone_prefills_input_box():
    """Simulates admin clicking /check alone from bot menu: pre-fills /check with switch_inline_query."""
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")

    sent_msgs = []
    async def mock_send(chat_id, text, reply_markup=None):
        sent_msgs.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
    runner.send_message = mock_send

    # Bare /check without replied message
    await runner.handle_command(123456, "/check", reply_to_message=None)

    assert len(sent_msgs) >= 1
    resp = sent_msgs[-1]
    assert resp["reply_markup"] is not None
    btns = resp["reply_markup"]["inline_keyboard"]
    assert any(b.get("switch_inline_query_current_chat") == "/check " for row in btns for b in row)


@pytest.mark.asyncio
async def test_simulated_admin_decision_cancel_flow():
    """Simulates admin clicking Duyệt then clicking HỦY BỎ: restores original buttons without touching status."""
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")

    import os, json
    dev_storage_file = os.path.join(os.getcwd(), "data", "dev_test_submissions.json")
    os.makedirs(os.path.dirname(dev_storage_file), exist_ok=True)
    with open(dev_storage_file, "w", encoding="utf-8") as f:
        json.dump([{"submission_id": "VN-CANCEL-01", "status": "MANUAL_REVIEW"}], f)

    edited_markups = []
    async def mock_edit(chat_id, msg_id, reply_markup=None):
        edited_markups.append(reply_markup)
        return True
    runner.edit_message_reply_markup = mock_edit

    async def mock_answer_cq(cq_id, text=None, show_alert=False):
        return True
    runner.answer_callback_query = mock_answer_cq

    # 1. Click Duyệt -> prompts confirmation
    await runner.handle_callback_query({
        "id": "cq_1",
        "data": "admin_req_approve:VN-CANCEL-01",
        "message": {"message_id": 200, "chat": {"id": 123456}},
        "from": {"id": 777, "first_name": "Admin1"},
    })
    assert len(edited_markups) == 1

    # 2. Click HỦY BỎ -> restores original decision buttons
    await runner.handle_callback_query({
        "id": "cq_2",
        "data": "admin_cancel_decision:VN-CANCEL-01",
        "message": {"message_id": 200, "chat": {"id": 123456}},
        "from": {"id": 777, "first_name": "Admin1"},
    })
    assert len(edited_markups) == 2
    restored_btns = edited_markups[1]["inline_keyboard"]
    assert any("admin_req_approve:VN-CANCEL-01" in b.get("callback_data", "") for row in restored_btns for b in row)

    # 3. Status in dev storage is still MANUAL_REVIEW!
    with open(dev_storage_file, "r", encoding="utf-8") as f:
        recs = json.load(f)
    assert recs[0]["status"] == "MANUAL_REVIEW"
