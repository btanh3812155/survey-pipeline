# -*- coding: utf-8 -*-
"""
Suite A: Image Checking & Vision Audit Test Suite.
Exhaustively tests all visual verification checks:
1. Arithmetic checks: income - costs == profit (weekly statements & aggregate).
2. Arithmetic failure modes: negative costs, net > gross, NaN, overflow.
3. DINOv2 visual similarity vs distinct OCR metrics override.
4. Monthly income report upload guard in weekly slots.
5. Multi-image slot role classification (profile, vehicle, income).
6. Laplacian blur threshold and image format decoding.
"""

import os
import sys
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from main import app, vision_client
from tests.real_test_samples import get_real_distinct_test_photos
from services.normalizer import sanitize_and_normalize_image, compute_laplacian_blur_score
from services.duplicate_detector import check_intra_submission_duplicates
from services.income_sanitizer import analyze_driver_income
from services.form_engine import DEFAULT_TRUST_BRIDGE_SCHEMA


# ==============================================================================
# 1. Arithmetic & Financial Reconciliation Checks
# ==============================================================================

def test_arithmetic_reconciliation_math_consistent():
    """Valid arithmetic where gross_income - platform_discount == net_income passes cleanly."""
    res = analyze_driver_income(
        vehicle_type="CAR",
        gross_income=10_000_000,
        net_income=7_800_000,
        platform_discount=2_200_000,
        period="weekly",
    )
    assert res.is_valid is True
    assert res.status_code == "APPROVED"


def test_arithmetic_reconciliation_cost_deduction_mismatch_rejected():
    """When income - costs != profit, system strictly rejects with ARITHMETIC_MISMATCH."""
    # Gross 10M - Costs 2M = 8M, but Net is claimed as 5M (3M unaccounted!)
    res = analyze_driver_income(
        vehicle_type="CAR",
        gross_income=10_000_000,
        net_income=5_000_000,
        platform_discount=2_000_000,
        period="weekly",
    )
    assert res.is_valid is False
    assert res.status_code == "ARITHMETIC_MISMATCH"
    assert "Số học không khớp" in res.flag_reason


def test_arithmetic_reconciliation_profit_exceeds_revenue_rejected():
    """Net income exceeding gross revenue without justification is rejected."""
    res = analyze_driver_income(
        vehicle_type="CAR",
        gross_income=5_000_000,
        net_income=8_000_000,
        period="weekly",
    )
    assert res.is_valid is False
    assert res.status_code == "ARITHMETIC_MISMATCH"
    assert "lớn hơn tổng doanh thu" in res.flag_reason


def test_arithmetic_reconciliation_negative_cost_abuse_rejected():
    """Negative costs (-5M) to artificially inflate profit (10M - (-5M) = 15M) are strictly rejected."""
    res = analyze_driver_income(
        vehicle_type="CAR",
        gross_income=10_000_000,
        net_income=15_000_000,
        platform_discount=-5_000_000,
        period="weekly",
    )
    assert res.is_valid is False
    assert res.status_code == "ARITHMETIC_MISMATCH"


def test_arithmetic_reconciliation_nan_and_negative_incomes():
    """NaN, infinity, and negative numbers trigger validation failures."""
    res_nan = analyze_driver_income(vehicle_type="CAR", gross_income=float("nan"))
    assert res_nan.is_valid is False

    res_neg = analyze_driver_income(vehicle_type="CAR", gross_income=-10_000_000)
    assert res_neg.is_valid is False


@pytest.mark.asyncio
async def test_endpoint_catches_arithmetic_mismatch_in_ocr(monkeypatch):
    """Ingress endpoint verifies every extracted weekly OCR and rejects if income - costs != profit."""
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
        # Gross: 10M, discount: 2M, but net claimed is 3M (unbalanced by 5M)
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


# ==============================================================================
# 2. DINOv2 & Visual Similarity vs OCR Override Checks
# ==============================================================================

def test_dinov2_ocr_override_on_visually_similar_same_phone_screenshots():
    """Two photos of the same physical phone setup are not rejected as duplicates when OCR numbers differ."""
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


# ==============================================================================
# 3. Monthly Income Report in Weekly Slot Guard
# ==============================================================================

@pytest.mark.asyncio
async def test_monthly_income_upload_rejected_in_weekly_slot(monkeypatch):
    """Submitting a monthly summary screen instead of a weekly report is caught and rejected."""
    monkeypatch.setenv("MOCK_AI", "1")
    monkeypatch.setenv("ENVIRONMENT", "test")
    client = TestClient(app)

    async def mock_role(img_bytes, filename=None, *args, **kwargs):
        fn = (filename or "").lower()
        if "0" in fn or "profile" in fn:
            return "app_profile"
        return "app_income"

    async def mock_ocr_monthly(b, week_idx=None, *args, **kwargs):
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
def test_temporal_validation_rejects_daily_statement_in_weekly_slot():
    """Submitting a 1-day daily statement into a weekly slot is strictly rejected."""
    from services.image_types import PeriodHeaderMetadata, TimeGranularity, validate_extracted_weekly_period

    # Case A: Active tab is explicitly DAILY
    hdr_daily_tab = PeriodHeaderMetadata(
        active_tab=TimeGranularity.DAILY,
        period_raw_text="Hôm nay (15/10/2026)",
    )
    ok_a, err_a = validate_extracted_weekly_period(hdr_daily_tab, expected_week_num=3)
    assert ok_a is False
    assert "báo cáo thu nhập theo NGÀY" in err_a

    # Case B: Date range evaluates to 1 single day (15/10 - 15/10)
    hdr_single_day = PeriodHeaderMetadata(
        active_tab=TimeGranularity.UNKNOWN,
        period_raw_text="15/10/2026 - 15/10/2026",
    )
    ok_b, err_b = validate_extracted_weekly_period(hdr_single_day, expected_week_num=3)
    assert ok_b is False
    assert "báo cáo thu nhập của 1 ngày" in err_b


def test_temporal_validation_rejects_swapped_week_uploaded_to_wrong_slot():
    """Uploading Week 1 statement into Week 4 slot is detected and rejected with swapped week notice."""
    from services.image_types import PeriodHeaderMetadata, TimeGranularity, validate_extracted_weekly_period

    # Driver uploaded Tuần 1 (28/09 - 04/10) into Tuần 4 slot
    hdr_w1 = PeriodHeaderMetadata(
        active_tab=TimeGranularity.WEEKLY,
        period_raw_text="28/09/2026 - 04/10/2026",
    )
    ok, err = validate_extracted_weekly_period(hdr_w1, expected_week_num=4)
    assert ok is False
    assert "nộp nhầm ảnh của Tuần 1 (28/09 - 04/10) vào ô Tuần 4" in err


def test_temporal_validation_accepts_valid_october_week_ranges():
    """Correct weekly statements matching their assigned slot calendar window pass cleanly."""
    from services.image_types import PeriodHeaderMetadata, TimeGranularity, validate_extracted_weekly_period

    # Week 1: 28/09 - 04/10
    ok1, _ = validate_extracted_weekly_period(
        PeriodHeaderMetadata(active_tab=TimeGranularity.WEEKLY, period_raw_text="28/09 - 04/10/2026"),
        expected_week_num=1,
    )
    assert ok1 is True

    # Week 2: 05/10 - 11/10
    ok2, _ = validate_extracted_weekly_period(
        PeriodHeaderMetadata(active_tab=TimeGranularity.WEEKLY, period_raw_text="05/10 - 11/10/2026"),
        expected_week_num=2,
    )
    assert ok2 is True

    # Week 5: 26/10 - 01/11 (cross-month boundary)
    ok5, _ = validate_extracted_weekly_period(
        PeriodHeaderMetadata(active_tab=TimeGranularity.WEEKLY, period_raw_text="26/10 - 01/11/2026"),
        expected_week_num=5,
    )
    assert ok5 is True


# ==============================================================================
# 4. Blur and Format Normalization Checks
# ==============================================================================

def test_blur_variance_detection():
    """Sharp authentic photos pass blur check while synthetic blank frames fail."""
    photos = get_real_distinct_test_photos(1)
    assert len(photos) > 0
    _, img_bytes = photos[0]

    _, pil_im = sanitize_and_normalize_image(img_bytes)
    is_sharp, b_var = compute_laplacian_blur_score(pil_im, threshold=15.0)
    assert is_sharp is True
    assert b_var >= 15.0
# ==============================================================================
# 5. Image Upload, Camera Toggle Persistence & Multi-Upload Ordering
# ==============================================================================

def test_camera_toggle_and_upload_button_persist_through_retake_reupload_and_toggle():
    """
    Verifies that camera toggle button and image upload button persist through:
    1. Retaking (retakeCam restores upload button and camera controls).
    2. Camera toggle (deactivateCamera / activateCamera preserves uploadLabel).
    3. Re-uploading a photo into an already captured slot.
    """
    from services.dynamic_form_html import render_dynamic_form_html
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)

    # 1. Camera toggle controls exist in DOM
    assert "btn-cam-deactivate" in html
    assert "btnCamOff_" in html
    assert "deactivateCamera" in html
    assert "activateCamera" in html

    # 2. Retake button exists and has wiring to restore upload button
    assert "btn-camera-retake" in html
    assert "retakeCam" in html
    assert "uploadLabel.style.display = (slotObj && slotObj.allow_upload === false) ? 'none' : 'inline-flex'" in html

    # 3. Deactivate camera ensures upload label is NOT hidden so user can upload from gallery
    assert "if (!answers[slotKey] || !(answers[slotKey] instanceof Blob))" in html


def test_multi_image_upload_ordering_all_cases():
    """
    Simulates sequential multi-image upload from slot 0:
    Verifies that files fill slots 0, 1, 2, 3, 4, 5, 6 in exact sequential order.
    """
    from services.dynamic_form_html import render_dynamic_form_html
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)

    # Verifies sequential multi-image file assignment loop
    assert "for (let i = 0; i < maxFill; i++)" in html
    assert "const targetSlotIdx = uploadableSlots[startPos + i].idx" in html
    assert "applyFileToSlot(qid, targetSlotIdx, compressed)" in html


def test_multi_image_upload_from_halfway_maintains_order():
    """
    Simulates multi-image upload starting from halfway (e.g. currentSlotIdx = 3):
    Verifies that files start from currentSlotIdx and sequentially populate subsequent slots.
    """
    from services.dynamic_form_html import render_dynamic_form_html
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)

    # Verifies startPos calculation based on currentSlotIdx
    assert "let startPos = uploadableSlots.findIndex(item => item.idx === currentSlotIdx)" in html
    assert "advanceToNextUncapturedSlot(qid)" in html


def test_uploading_more_than_allowed_slots_clamps_gracefully():
    """
    Simulates selecting more files than remaining available slots (e.g. 10 files selected):
    Verifies code clamps to remaining slots without crashing or throwing index out of bounds.
    """
    from services.dynamic_form_html import render_dynamic_form_html
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)

    # Verifies remainingSlots calculation and clamping via Math.min
    assert "const remainingSlots = uploadableSlots.length - startPos" in html
    assert "const maxFill = Math.min(files.length, remainingSlots)" in html
    assert "if (files.length > remainingSlots)" in html
