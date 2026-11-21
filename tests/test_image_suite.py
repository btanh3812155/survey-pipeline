import io
import json
import pytest
import numpy as np
from PIL import Image, ImageDraw

from services.normalizer import (
    sanitize_and_normalize_image,
    compute_laplacian_blur_score,
    detect_image_manipulation,
)
from services.online_and_ai_detector import (
    OnlineStockAndAIDetector,
    compute_dhash_int,
    detect_ai_generated_fft_anomalies,
)
from services.vehicle_tracker import (
    VehiclePersistenceTracker,
    normalize_license_plate,
)
from services.income_sanitizer import (
    analyze_driver_income,
    INCOME_BENCHMARKS,
)
from tests.test_architect_fixes import make_test_image, submit_camera, weekly_ocr


def test_license_plate_normalization():
    assert normalize_license_plate("29E-123.45") == "29E12345"
    assert normalize_license_plate("51h - 999.88 ") == "51H99988"
    assert normalize_license_plate("29-AA 012.34") == "29AA01234"

def test_vehicle_persistence_and_mismatch():
    import asyncio
    tracker = VehiclePersistenceTracker(db_pool=None)

    async def _test():
        # 1. first registration
        res1 = await tracker.verify_and_persist_vehicle(
            driver_phone="0988123456",
            detected_plate="29E-123.45",
            detected_vehicle_type="CAR",
        )
        assert res1.is_match is True

        # 2. mismatch detection (submitting a different/fake plate)
        res_mismatch = await tracker.verify_and_persist_vehicle(
            driver_phone="0988123456",
            detected_plate="FAKE9999",
            detected_vehicle_type="CAR",
        )
        assert res_mismatch.is_match is False
        assert res_mismatch.status_code == "VEHICLE_UNREGISTERED"

    asyncio.run(_test())
def test_online_stock_image_detection():
    detector = OnlineStockAndAIDetector()

    # create a synthetic stock marketing vehicle image
    stock_img = Image.new("RGB", (500, 500), (0, 150, 200))
    draw = ImageDraw.Draw(stock_img)
    draw.rectangle([100, 100, 400, 300], fill=(255, 255, 255))
    draw.text((150, 200), "VINFAST VF8 STOCK PHOTO", fill=(0, 0, 0))

    # register into stock database
    detector.register_stock_image(stock_img)

    # test query with exact or slightly cropped stock photo
    test_query = stock_img.resize((480, 480))
    res = detector.evaluate_image(test_query)
    assert res.is_flagged is True
    assert res.status_code == "STOCK_IMAGE_DETECTED"

    # test query with an unrelated image (natural outdoor camera shot)
    natural_img = Image.new("RGB", (500, 500), (50, 50, 50))
    draw_nat = ImageDraw.Draw(natural_img)
    for i in range(0, 500, 20):
        draw_nat.line([(0, i), (500, 500 - i)], fill=(200, 100, 0))
    res_clean = detector.evaluate_image(natural_img)
    assert res_clean.is_flagged is False


def test_ai_generated_image_detection():
    detector = OnlineStockAndAIDetector()
    test_img = Image.new("RGB", (512, 512), (100, 100, 100))

    # test when visual AI flags synthetic diffusion artifacts
    res_ai = detector.evaluate_image(
        test_img,
        ai_detected_flag=True,
        ai_evidence_note="Mangled VinFast logo and unnatural glossy reflection",
    )
    assert res_ai.is_flagged is True
    assert res_ai.status_code == "AI_GENERATED_DETECTED"
    assert "Mangled VinFast logo" in res_ai.flag_reason


def test_income_sanitizer_bike_driver_outlandish_10k_usd():
    # Bike driver reporting $10,000 USD (250,000,000 VND) -> MUST AUTO REJECT!
    res_outlandish = analyze_driver_income(
        vehicle_type="BIKE",
        gross_income=250_000_000,
        period="monthly"
    )
    assert res_outlandish.is_valid is False
    assert res_outlandish.status_code == "OUTLANDISH_INCOME"
    assert res_outlandish.suggested_action == "AUTO_REJECT"


def test_income_sanitizer_bike_driver_realistic():
    # Bike driver reporting 12,000,000 VND across 350 rides (~34k/ride) -> APPROVED!
    res_realistic = analyze_driver_income(
        vehicle_type="BIKE",
        gross_income=12_000_000,
        trips_count=350,
        platform_discount=2_400_000,
        net_income=9_600_000,
        period="monthly"
    )
    assert res_realistic.is_valid is True
    assert res_realistic.status_code == "APPROVED"
    assert res_realistic.suggested_action == "APPROVE"


def test_income_sanitizer_car_driver_outlandish():
    # Car driver reporting 150,000,000 VND (> 100M ceiling) -> AUTO REJECT
    res_car_fake = analyze_driver_income(
        vehicle_type="CAR",
        gross_income=150_000_000,
        period="monthly"
    )
    assert res_car_fake.is_valid is False
    assert res_car_fake.status_code == "OUTLANDISH_INCOME"


def test_income_sanitizer_arithmetic_mismatch():
    # Gross 20M - Discount 4M != Net 10M (6M missing!) -> ESCALATE TO ADMIN
    res_math_error = analyze_driver_income(
        vehicle_type="CAR",
        gross_income=20_000_000,
        platform_discount=4_000_000,
        net_income=10_000_000,  # Expected 16M
        period="monthly"
    )
    assert res_math_error.is_valid is False
    assert res_math_error.status_code == "ARITHMETIC_MISMATCH"
    assert res_math_error.suggested_action == "REJECT"


def test_income_sanitizer_suspicious_ride_fare():
    # Bike driver: 1,000,000 VND across only 2 rides = 500,000 VND/ride!
    # A single bike ride cannot be 500k VND! -> ESCALATE TO ADMIN
    res_fare = analyze_driver_income(
        vehicle_type="BIKE",
        gross_income=1_000_000,
        trips_count=2,
        period="daily"
    )
    assert res_fare.is_valid is False
    assert res_fare.status_code == "SUSPICIOUS_RIDE_FARE"
    assert res_fare.suggested_action == "REJECT"

def test_dual_edge_scaling_and_ribbon_protection():
    import pytest

    # 1. Standard 4:3 phone capture (4000 x 3000)
    # Clamped by short edge (720): 3000 -> 720, 4000 -> 960 (Aspect ratio 4:3 preserved!)
    img_4_3 = Image.new("RGB", (4000, 3000), (200, 200, 200))
    buf = io.BytesIO()
    img_4_3.save(buf, format="JPEG")
    _, norm_4_3 = sanitize_and_normalize_image(buf.getvalue(), max_short_edge=720, max_long_edge=2160)
    assert norm_4_3.size == (960, 720)

    # 2. Modern tall 20:9 smartphone screen (1080 x 2400)
    # Clamped by short edge (720): 1080 -> 720, 2400 -> 1600 (Aspect ratio 20:9 preserved!)
    img_tall = Image.new("RGB", (1080, 2400), (200, 200, 200))
    buf = io.BytesIO()
    img_tall.save(buf, format="JPEG")
    _, norm_tall = sanitize_and_normalize_image(buf.getvalue(), max_short_edge=720, max_long_edge=2160)
    assert norm_tall.size == (720, 1600)

    # 3. Continuous long receipt (1000 x 4000, aspect ratio 4:1)
    # Clamped by long edge (2160): 4000 -> 2160, 1000 -> 540 (Aspect ratio 4:1 preserved!)
    img_long = Image.new("RGB", (1000, 4000), (200, 200, 200))
    buf = io.BytesIO()
    img_long.save(buf, format="JPEG")
    _, norm_long = sanitize_and_normalize_image(buf.getvalue(), max_short_edge=720, max_long_edge=2160)
    assert norm_long.size == (540, 2160)

    # 4. Absurd ribbon image (50 x 1000, aspect ratio 20:1 > max 6.0) -> Rejects safely!
    img_ribbon = Image.new("RGB", (50, 1000), (200, 200, 200))
    buf = io.BytesIO()
    img_ribbon.save(buf, format="JPEG")
    with pytest.raises(ValueError) as exc:
        sanitize_and_normalize_image(buf.getvalue(), max_aspect_ratio=6.0)
    assert "tỷ lệ khung hình bất thường" in str(exc.value)

def test_mask_aware_quality_checks_on_transparent_images():
    from services.normalizer import detect_optical_underexposure, detect_specular_glare_obliteration

    # Create an RGBA image where 60% of the image is transparent, and 40% is a pitch-black dark subject (15/255)
    dark_rgba = Image.new("RGBA", (500, 500), (0, 0, 0, 0)) # transparent border
    draw = ImageDraw.Draw(dark_rgba)
    draw.rectangle([150, 150, 350, 350], fill=(15, 15, 15, 255)) # pitch black subject

    buf = io.BytesIO()
    dark_rgba.save(buf, format="PNG")
    _, norm_img = sanitize_and_normalize_image(buf.getvalue())

    # 1. Mask-aware underexposure: Correctly catches dark subject despite the newly inserted white canvas!
    is_dark, dark_reason = detect_optical_underexposure(norm_img)
    assert is_dark is True
    assert "quá tối" in dark_reason

    # 2. Mask-aware glare: Does NOT trigger false specular glare despite 60% white canvas!
    is_glared, _ = detect_specular_glare_obliteration(norm_img)
    assert is_glared is False

@pytest.mark.asyncio
async def test_admin_query_submission_dossier(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from unittest.mock import AsyncMock

    from config import settings
    from main import app
    from routers import webhooks
    from services.admin_moderator import query_submission_dossier, render_dossier_message
    from services.telegram_polling import TelegramPollingRunner

    record = {
        "submission_id": "VN-9921", "status": "REJECTED", "reason": "Ảnh thu nhập bị trùng lặp",
        "driver_phone": "0988123456", "license_plate": "29E-123.45",
        "vehicle_type": "CAR", "car_model": "VF 5 Plus", "form_id": "income_survey",
        "submitted_at": "2026-11-20T08:00:00+00:00",
    }
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "dev_test_submissions.json").write_text(json.dumps([record]), encoding="utf-8")

    dossier = await query_submission_dossier("VN-9921", db_pool=None)
    assert dossier["submission_id"] == record["submission_id"]
    assert dossier["status"] == "REJECTED"
    assert dossier["driver_phone"] == record["driver_phone"]
    assert dossier["raw_data"]["license_plate"] == record["license_plate"]
    assert dossier["form_id"] == record["form_id"]
    assert dossier["submitted_at"] == record["submitted_at"]

    report_text = render_dossier_message(dossier)
    for detail in ("VN-9921", "REJECTED", record["driver_phone"], record["car_model"], record["reason"]):
        assert detail in report_text

    admin_gid = -1001234567890
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "dossier-test-secret")
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", str(admin_gid))
    runner = TelegramPollingRunner(bot_token="mock_tg_token", db_pool=None, role="admin")
    deliver = AsyncMock()
    monkeypatch.setattr(runner, "send_dossier_bundle", deliver)
    monkeypatch.setattr(webhooks, "admin_bot_runner", runner)
    response = TestClient(app).post(
        "/webhook/telegram",
        json={"message": {"chat": {"id": admin_gid, "type": "supergroup"},
                          "from": {"id": 111222}, "text": "/query VN-9921"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "dossier-test-secret"},
    )
    assert response.status_code == 200, response.text
    deliver.assert_awaited_once()
    chat_id, _key, delivered_dossier, _caption, attachment = deliver.await_args.args
    assert chat_id == admin_gid
    assert delivered_dossier == dossier
    assert json.loads(attachment)["status"] == "REJECTED"


@pytest.mark.asyncio
async def test_reject_unauthorized_app_spoofing(submit_camera):
    """Competitor app screenshots are rejected even when all numbers reconcile."""
    async def extract(_image_bytes, *, week_idx, prompt):
        return weekly_ocr(
            week_idx, detected_app="GRAB", is_authentic_gsm_ui=False,
        ) if week_idx == 1 else weekly_ocr(week_idx)

    response = await submit_camera(extract)
    assert response["status"] == "REJECTED", response
    assert "không phải ứng dụng Tài xế Xanh SM chính thức" in response["reason"]
    assert response["ocr_data"]["detected_app"] == "GRAB"
    assert response["ocr_data"]["is_authentic_gsm_ui"] is False


@pytest.mark.asyncio
async def test_reject_intra_submission_duplicates_same_screen(submit_camera):
    """Identical photos and financials are duplicates even if OCR assigns two different periods."""
    photos = [make_test_image(f"duplicate-slot-{i}") for i in range(7)]
    photos[3] = photos[2]

    async def extract(_image_bytes, *, week_idx, prompt):
        if week_idx == 2:
            return weekly_ocr(week_idx, gross_revenue=3_000_000, net_income=2_500_000,
                              discount_and_tax=500_000, total_trips=20)
        return weekly_ocr(week_idx)

    response = await submit_camera(extract, photos=photos)
    assert response["status"] == "REJECTED", response
    assert "trùng lặp" in response["reason"]
    assert response["duplicate_detected"] is True
    assert response["duplicate_type"] == "INTRA_SUBMISSION"


@pytest.mark.asyncio
async def test_censored_profile_slot_allowed_when_require_unique_false(submit_camera):
    """Different drivers may submit the same profile photo with distinct weekly statements."""
    profile = make_test_image("shared-censored-profile")

    async def extract_first(_image_bytes, *, week_idx, prompt):
        return weekly_ocr(week_idx)

    async def extract_second(_image_bytes, *, week_idx, prompt):
        return weekly_ocr(week_idx, gross_revenue=4_000_000 * week_idx,
                          net_income=3_500_000 * week_idx, total_trips=25 * week_idx)

    for phone, extract in [("0988654321", extract_first), ("0988654322", extract_second)]:
        photos = [profile, *(make_test_image(f"{phone}-{i}") for i in range(1, 7))]
        response = await submit_camera(extract, photos=photos, phone=phone)
        assert response["status"] == "APPROVED", response
        assert response.get("duplicate_detected") is not True
