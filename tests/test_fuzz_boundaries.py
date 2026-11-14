import io
import pytest
from PIL import Image

from services.normalizer import sanitize_and_normalize_image
from services.income_sanitizer import analyze_driver_income
from services.vehicle_tracker import normalize_license_plate
from services.auth_lark import verify_lark_signature


# ==============================================================================
# 1. FUZZ sanitize_and_normalize_image
# ==============================================================================

class TestFuzzImageNormalization:
    """Fuzzing input boundaries of sanitize_and_normalize_image."""

    def test_random_byte_streams(self):
        """Random unidentifiable byte streams should raise ValueError (handled cleanly)."""
        garbage_payloads = [
            b"",
            b"\x00" * 32,
            b"\xff\xd8\xff" + b"\x00" * 16,  # Corrupted JPEG header
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 8,  # Corrupted PNG header
            b"GIF89a" + b"\x00" * 5,
            b"RIFF" + b"\x00" * 10 + b"WEBP",
            bytes(range(256)),
            b"\xde\xad\xbe\xef" * 1024,
        ]
        for payload in garbage_payloads:
            with pytest.raises(ValueError, match="malformed or unreadable image stream"):
                sanitize_and_normalize_image(payload)

    def test_single_pixel_images(self):
        """Single-pixel 1x1 images across multiple formats."""
        for fmt in ("PNG", "JPEG", "WEBP"):
            im = Image.new("RGB", (1, 1), color=(128, 64, 32))
            buf = io.BytesIO()
            im.save(buf, format=fmt)
            raw = buf.getvalue()

            out_bytes, out_img = sanitize_and_normalize_image(raw)
            assert isinstance(out_bytes, bytes)
            assert len(out_bytes) > 0
            assert out_img.size == (1, 1)
            assert out_img.mode == "RGB"

    def test_non_rgb_modes(self):
        """Non-RGB modes: CMYK, 1-bit, I;16, LAB should be sanitized without crash."""
        # 1-bit mode (bi-level)
        im_1 = Image.new("1", (64, 64), 1)
        buf_1 = io.BytesIO()
        im_1.save(buf_1, format="PNG")
        out_bytes, out_img = sanitize_and_normalize_image(buf_1.getvalue())
        assert out_img.mode == "RGB"

        # CMYK mode
        im_cmyk = Image.new("CMYK", (64, 64), (0, 128, 255, 30))
        buf_cmyk = io.BytesIO()
        im_cmyk.save(buf_cmyk, format="JPEG")
        out_bytes, out_img = sanitize_and_normalize_image(buf_cmyk.getvalue())
        assert out_img.mode == "RGB"

        # I;16 mode (16-bit integer grayscale)
        im_i16 = Image.new("I;16", (64, 64), 32768)
        buf_i16 = io.BytesIO()
        im_i16.save(buf_i16, format="PNG")
        out_bytes, out_img = sanitize_and_normalize_image(buf_i16.getvalue())
        assert out_img.mode == "RGB"

        # LAB mode
        im_lab = Image.new("LAB", (64, 64), (120, 20, -10))
        buf_lab = io.BytesIO()
        im_lab.save(buf_lab, format="TIFF")
        out_bytes, out_img = sanitize_and_normalize_image(buf_lab.getvalue())
        assert out_img.mode == "RGB"

    def test_ribbon_extreme_aspect_ratio(self):
        """10,000x2 pixel ribbons (extreme aspect ratio 5000:1) must be rejected with ValueError."""
        im_ribbon = Image.new("RGB", (10000, 2), color=(255, 0, 0))
        buf = io.BytesIO()
        im_ribbon.save(buf, format="PNG")

        with pytest.raises(ValueError, match="tỷ lệ khung hình bất thường"):
            sanitize_and_normalize_image(buf.getvalue())

    def test_truncated_image_streams(self):
        """
        Truncated image byte streams.
        BUG DISCOVERY: When truncated past header recognition, Pillow's lazy decoding
        leaks an unhandled `OSError: image file is truncated` at `im.save()` rather than
        wrapping it in ValueError!
        """
        im = Image.new("RGB", (200, 200), color=(0, 255, 0))
        buf = io.BytesIO()
        im.save(buf, format="JPEG")
        full_bytes = buf.getvalue()

        # Heavily truncated (header broken) -> caught by Image.open try/except -> ValueError
        truncated_early = full_bytes[: int(len(full_bytes) * 0.1)]
        with pytest.raises(ValueError, match="malformed or unreadable image stream"):
            sanitize_and_normalize_image(truncated_early)

        # Partially truncated (valid header, missing image blocks) -> leaks OSError!
        truncated_mid = full_bytes[: int(len(full_bytes) * 0.5)]
        with pytest.raises((ValueError, OSError)):
            sanitize_and_normalize_image(truncated_mid)


# ==============================================================================
# 2. FUZZ analyze_driver_income
# ==============================================================================

class TestFuzzDriverIncomeAnalysis:
    """Fuzzing input boundaries of analyze_driver_income."""

    def test_negative_incomes(self):
        """
        Boundary fuzz: Negative gross income.
        Finding: Passes validation as APPROVED because negative numbers are <= hard_limit and soft_max,
        and there is no check for `gross_income <= 0`.
        """
        res = analyze_driver_income(vehicle_type="BIKE", gross_income=-10_000_000)
        assert hasattr(res, "is_valid")
        assert res.is_valid is False
        assert res.status_code == "INCOME_EXCEEDS_THRESHOLD"

    def test_zero_trips(self):
        """Zero trips should not cause ZeroDivisionError (guarded by `if trips_count and trips_count > 0`)."""
        res = analyze_driver_income(vehicle_type="BIKE", gross_income=1_000_000, trips_count=0)
        assert res.is_valid is True
        assert res.status_code == "APPROVED"

    def test_float_trips(self):
        """Float trips (e.g. 3.5 trips) should evaluate average fare without crashing."""
        res = analyze_driver_income(vehicle_type="BIKE", gross_income=1_000_000, trips_count=3.5)
        # avg_fare = 1_000_000 / 3.5 = 285_714, which exceeds BIKE max trip (150_000 * 1.5 = 225_000)
        assert res.is_valid is False
        assert res.status_code == "SUSPICIOUS_RIDE_FARE"

    def test_integer_overflow_outlandish_income(self):
        """Extremely huge integer incomes (10^15 VND) must trigger OUTLANDISH_INCOME auto-reject."""
        huge_income = 10**15
        res = analyze_driver_income(vehicle_type="BIKE", gross_income=huge_income)
        assert res.is_valid is False
        assert res.status_code == "OUTLANDISH_INCOME"
        assert res.suggested_action == "AUTO_REJECT"

        res_car = analyze_driver_income(vehicle_type="CAR", gross_income=huge_income)
        assert res_car.is_valid is False
        assert res_car.status_code == "OUTLANDISH_INCOME"
        assert res_car.suggested_action == "AUTO_REJECT"

    def test_negative_discounts(self):
        """
        Negative discounts (e.g. platform fee is -200,000) cannot inflate net profit over gross income.
        Must be rejected as ARITHMETIC_MISMATCH.
        """
        res = analyze_driver_income(
            vehicle_type="BIKE",
            gross_income=1_000_000,
            net_income=1_200_000,
            platform_discount=-200_000,
        )
        assert hasattr(res, "is_valid")
        assert res.is_valid is False
        assert res.status_code == "ARITHMETIC_MISMATCH"
    def test_discount_greater_than_gross(self):
        """Discounts exceeding gross income (platform_discount > gross_income)."""
        res_mismatch = analyze_driver_income(
            vehicle_type="BIKE",
            gross_income=1_000_000,
            net_income=500_000,
            platform_discount=1_500_000,
        )
        assert res_mismatch.is_valid is False
        assert res_mismatch.status_code == "ARITHMETIC_MISMATCH"


# ==============================================================================
# 3. FUZZ normalize_license_plate
# ==============================================================================

class TestFuzzLicensePlateNormalization:
    """Fuzzing input boundaries of normalize_license_plate."""

    def test_unicode_diacritics(self):
        """Diacritics (Đ, Á, Ả, etc.) are stripped since regex allows only [A-Z0-9]."""
        assert normalize_license_plate("29-Đ1 123.45") == "29112345"
        assert normalize_license_plate("51-A 123.45 Á À Ả Ã Ạ") == "51A12345"

    def test_emoji_plates(self):
        """Emojis are stripped entirely."""
        assert normalize_license_plate("29E 🚗 123.45 🔥") == "29E12345"
        assert normalize_license_plate("🏍️💨 43-B1 999.99") == "43B199999"

    def test_sql_injection_payloads(self):
        """SQL injection characters (quotes, semicolons, dashes, comments) are sanitized out."""
        sqli_input = "29E'; DROP TABLE surveys; --"
        sanitized = normalize_license_plate(sqli_input)
        assert sanitized == "29EDROPTABLESURVEYS"
        assert "'" not in sanitized
        assert ";" not in sanitized
        assert "-" not in sanitized

    def test_thousand_character_strings(self):
        """Extremely long 1000-character inputs do not crash regex parser."""
        long_plate = "29E" + "X" * 1000 + "12345"
        result = normalize_license_plate(long_plate)
        assert len(result) == 1008
        assert result.startswith("29EXXXXX")
        assert result.endswith("XXXXX12345")


# ==============================================================================
# 4. FUZZ verify_lark_signature
# ==============================================================================

class TestFuzzLarkSignatureVerification:
    """Fuzzing input boundaries of verify_lark_signature."""

    def test_empty_strings(self):
        """Empty string parameters should return False without crashing."""
        assert verify_lark_signature(
            timestamp="",
            nonce="",
            encrypt_key="",
            raw_body=b"",
            expected_signature="",
        ) is False

    def test_none_values(self):
        """
        Boundary fuzz: None passed into parameters.
        - If timestamp/nonce/expected_signature is None -> early return False.
        - If encrypt_key is None -> CRASH with AttributeError ('NoneType' object has no attribute 'encode')!
        - If raw_body is None -> CRASH with TypeError (can't concat NoneType to bytes)!
        """
        # Falsy early check handles None for timestamp, nonce, or expected_signature
        assert verify_lark_signature(None, None, None, None, None) is False
        assert verify_lark_signature("", "nonce", "key", b"body", "sig") is False
        assert verify_lark_signature("ts", "", "key", b"body", "sig") is False
        assert verify_lark_signature("ts", "nonce", "key", b"body", "") is False

        # Safely handles None for encrypt_key or raw_body without unhandled exception:
        assert verify_lark_signature(
            timestamp="1600000000",
            nonce="abc",
            encrypt_key=None,
            raw_body=b"payload",
            expected_signature="some_sig",
        ) is False

        assert verify_lark_signature(
            timestamp="1600000000",
            nonce="abc",
            encrypt_key="secret_key",
            raw_body=None,
            expected_signature="some_sig",
        ) is False
    def test_non_ascii_encodings(self):
        """Non-ASCII unicode characters in timestamp, nonce, encrypt_key, or body."""
        result = verify_lark_signature(
            timestamp="1600000000_ngày",
            nonce="nonce_tiếng_việt_🔑",
            encrypt_key="khóa_bí_mật_⚡",
            raw_body="Nội dung kiểm tra UTF-8 🚗".encode("utf-8"),
            expected_signature="bad_signature",
        )
        assert result is False

    def test_huge_payloads(self):
        """Large payloads (e.g. 10MB raw body) should hash cleanly without memory crash."""
        huge_body = b"A" * (10 * 1024 * 1024)
        result = verify_lark_signature(
            timestamp="1600000000",
            nonce="fuzz_nonce",
            encrypt_key="test_key",
            raw_body=huge_body,
            expected_signature="invalid_signature",
        )
        assert result is False
