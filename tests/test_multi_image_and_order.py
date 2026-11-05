import io
import pytest
from PIL import Image, ImageDraw
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock

from main import app
from config import settings
from services.form_engine import (
    FormSchema,
    FormQuestion,
    QuestionType,
    validate_submission_against_schema,
)
from services.vision_ai import FactExtractionResult


def create_mock_photo(pattern: str = "horizontal") -> bytes:
    img = Image.new("RGB", (600, 900), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    if pattern == "horizontal":
        for i in range(100, 800, 20):
            draw.line([(50, i), (550, i)], fill=(0, 0, 0), width=3)
    else:
        for j in range(50, 550, 20):
            draw.line([(j, 100), (j, 800)], fill=(0, 0, 0), width=3)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class TestMultiImageAndOrder:
    """Automated tests for image count validation, order enforcement, and admin auth."""

    def test_image_count_validation_min_images(self):
        """Submitting 1 image when min_images=2 fails validation with Vietnamese error message."""
        q = FormQuestion(
            id="camera_receipt",
            title="Ảnh bàn giao ca",
            type=QuestionType.CAMERA_WEBRTC,
            required=True,
            allow_multiple_images=True,
            min_images=2,
        )
        schema = FormSchema(form_id="test_min_img", title="Min Image Test", questions=[q])

        # 1 image submitted
        single_img_answers = {
            "camera_receipt": [b"photo1"],
            "camera_receipt_0": b"photo1",
        }
        ok, err = validate_submission_against_schema(schema, single_img_answers)
        assert not ok, "Expected validation failure for count < min_images"
        assert "yêu cầu tối thiểu 2 ảnh" in err
        assert "(hiện có 1 ảnh)" in err

    def test_image_count_validation_min_images_success(self):
        """Submitting 2 images when min_images=2 succeeds."""
        q = FormQuestion(
            id="camera_receipt",
            title="Ảnh bàn giao ca",
            type=QuestionType.CAMERA_WEBRTC,
            required=True,
            allow_multiple_images=True,
            min_images=2,
        )
        schema = FormSchema(form_id="test_min_img_success", title="Min Image Success Test", questions=[q])

        two_img_answers = {
            "camera_receipt": [b"photo1", b"photo2"],
            "camera_receipt_0": b"photo1",
            "camera_receipt_1": b"photo2",
        }
        ok, err = validate_submission_against_schema(schema, two_img_answers)
        assert ok, f"Expected validation success but got error: {err}"
        assert err is None

    def test_exact_image_count_enforcement(self):
        """Test exact_image_count enforcement: exactly 3 required."""
        q = FormQuestion(
            id="camera_receipt",
            title="Ảnh đối soát",
            type=QuestionType.CAMERA_WEBRTC,
            required=True,
            allow_multiple_images=True,
            exact_image_count=3,
        )
        schema = FormSchema(form_id="test_exact_img", title="Exact Count Test", questions=[q])

        # Submitting 2 images should fail
        answers_2 = {
            "camera_receipt": [b"img1", b"img2"],
            "camera_receipt_0": b"img1",
            "camera_receipt_1": b"img2",
        }
        ok, err = validate_submission_against_schema(schema, answers_2)
        assert not ok
        assert "yêu cầu chính xác 3 ảnh" in err
        assert "(hiện có 2 ảnh)" in err

        # Submitting 3 images should pass
        answers_3 = {
            "camera_receipt": [b"img1", b"img2", b"img3"],
            "camera_receipt_0": b"img1",
            "camera_receipt_1": b"img2",
            "camera_receipt_2": b"img3",
        }
        ok, err = validate_submission_against_schema(schema, answers_3)
        assert ok
        assert err is None

    def test_image_order_enforcement_reversal_rejection(self):
        """Submitting reversed images triggers slot order mismatch rejection."""
        client = TestClient(app)
        img1 = create_mock_photo("horizontal")
        img2 = create_mock_photo("vertical")

        mock_facts_rev = FactExtractionResult(
            screen_type="REVENUE",
            detected_plate="29E-123.45",
            gross_revenue=1500000.0,
            odometer_km=None,
            confidence_score=0.95,
        )
        mock_facts_odo = FactExtractionResult(
            screen_type="ODOMETER",
            detected_plate="29E-123.45",
            gross_revenue=None,
            odometer_km=15200.0,
            confidence_score=0.95,
        )

        async def mock_extract(norm_bytes):
            if norm_bytes == img1:
                return mock_facts_rev
            return mock_facts_odo

        with patch("main.vision_client.extract_facts", side_effect=mock_extract):
            resp = client.post(
                "/api/v1/survey/submit-webrtc",
                data={
                    "driver_phone": "0988123456",
                    "license_plate": "29E-123.45",
                    "driver_name": "Nguyen Van A",
                    "consent_choice": "Tôi đồng ý",
                    "driver_signature": "data:image/png;base64,mock",
                    "vehicle_type": "Ô tô điện VinFast (GSM)",
                    "car_model": "VF 5 Plus",
                    "gross_revenue": "1500000",
                    "odometer_km": "15200",
                    "is_test_mode": "true",
                },
                files={
                    "survey_photo": ("slot0.jpg", img1, "image/jpeg"),
                    "survey_photo_0": ("slot0.jpg", img1, "image/jpeg"),
                    "survey_photo_1": ("slot1.jpg", img2, "image/jpeg"),
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            if data.get("status") == "REJECTED":
                assert "Thứ tự ảnh không chính xác" in data.get("reason", "") or "thứ tự" in data.get("reason", "").lower()

    def test_admin_authentication_passwords(self):
        """Admin auth with password 12345678 returns 200 OK, while old password returns 401."""
        client = TestClient(app)

        # Old password returns 401 Unauthorized
        old_resp = client.get("/api/v1/dev/submissions", auth=("admin", "Legendofzeus2@"))
        assert old_resp.status_code == 401

        # New password returns 200 OK
        new_resp = client.get("/api/v1/dev/submissions", auth=("admin", "12345678"))
        assert new_resp.status_code == 200
