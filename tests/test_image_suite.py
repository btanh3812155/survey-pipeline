import io
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
        assert res_mismatch.status_code == "VEHICLE_MISMATCH"

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
    assert res_math_error.suggested_action == "ESCALATE_ADMIN"


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
    assert res_fare.suggested_action == "ESCALATE_ADMIN"
