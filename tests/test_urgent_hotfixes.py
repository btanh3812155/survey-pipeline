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
    assert 'document.getElementById(\'paperModal\')' in html


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
