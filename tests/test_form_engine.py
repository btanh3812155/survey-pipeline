import pytest
from fastapi.testclient import TestClient
from config import settings
from main import app
from services.form_engine import (
    FormSchema,
    FormQuestion,
    QuestionType,
    ValidationRule,
    ConditionalRule,
    DEFAULT_TRUST_BRIDGE_SCHEMA,
    validate_submission_against_schema,
)
def test_default_schema_valid_submission():
    valid_answers = {
        "driver_signature": "data:image/png;base64,mock",
        "consent_choice": "Tôi đồng ý",
        "driver_phone": "0988123456",
        "driver_name": "Nguyễn Văn An",
        "license_plate": "29E-123.45",
        "vehicle_type": "Ô tô điện VinFast (GSM)",
        "car_model": "VF 5 Plus",
        "gross_revenue": "1250000",
        "odometer_km": "15200",
        "camera_receipt": ["mock_odometer.jpg", "mock_revenue.jpg"],
    }
    is_valid, err = validate_submission_against_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, valid_answers)
    assert is_valid is True
    assert err is None

def test_regex_constraint_invalid_phone():
    invalid_answers = {
        "driver_signature": "data:image/png;base64,mock",
        "consent_choice": "Tôi đồng ý",
        "driver_phone": "12345", # Fails ^0[0-9]{9,10}$
        "driver_name": "Nguyễn Văn An",
        "license_plate": "29E-123.45",
        "vehicle_type": "Ô tô điện VinFast (GSM)",
        "car_model": "VF 5 Plus",
        "gross_revenue": "1250000",
        "odometer_km": "15200",
        "camera_receipt": "mock_blob",
    }
    is_valid, err = validate_submission_against_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, invalid_answers)
    assert is_valid is False
    assert "Số điện thoại" in err

def test_conditional_question_branching():
    answers_car = {
        "driver_signature": "data:image/png;base64,mock",
        "consent_choice": "Tôi đồng ý",
        "driver_phone": "0988123456",
        "driver_name": "Trần Văn Bình",
        "license_plate": "29E-123.45",
        "vehicle_type": "Ô tô điện VinFast (GSM)",
        "car_model": "VF 8",
        "gross_revenue": "1200000",
        "odometer_km": "5000",
        "camera_receipt": ["mock_odometer.jpg", "mock_revenue.jpg"],
    }
    is_valid, err = validate_submission_against_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, answers_car)
    assert is_valid is True
    assert err is None

    answers_bike = {
        "driver_signature": "data:image/png;base64,mock",
        "consent_choice": "Tôi đồng ý",
        "driver_phone": "0988123456",
        "driver_name": "Trần Văn Bình",
        "license_plate": "29E-123.45",
        "vehicle_type": "Xe máy điện (GSM Bike)",
        "gross_revenue": "300000",
        "odometer_km": "2000",
        "camera_receipt": ["mock_odometer.jpg", "mock_revenue.jpg"],
    }
    is_valid, err = validate_submission_against_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, answers_bike)
    assert is_valid is True
    assert err is None
def test_conditional_no_consent_skips_subsequent_questions():
    # When user chooses "Tôi không đồng ý", questions 3-9 are skipped
    no_consent_answers = {
        "driver_signature": "data:image/png;base64,mock",
        "consent_choice": "Tôi không đồng ý"
    }
    is_valid, err = validate_submission_against_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, no_consent_answers)
    assert is_valid is False
    assert "Nghị định 13" in err

def test_conditional_requires_both_signature_and_consent():
    # Missing signature with consent: questions 3-10 are hidden/skipped, but driver_signature (Q1) is required
    no_signature_answers = {
        "consent_choice": "Tôi đồng ý",
        "driver_phone": "0988123456",
    }
    is_valid, err = validate_submission_against_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, no_signature_answers)
    assert is_valid is False
    assert "Chữ ký xác nhận" in err

    # Missing signature with consent and no Q3-Q10: fails on driver_signature
    consent_only_answers = {
        "consent_choice": "Tôi đồng ý",
    }
    is_valid, err = validate_submission_against_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, consent_only_answers)
    assert is_valid is False
    assert "Chữ ký xác nhận" in err

    # Valid signature + consent, but missing required Q3 (driver_phone)
    missing_q3_answers = {
        "driver_signature": "data:image/png;base64,mock",
        "consent_choice": "Tôi đồng ý",
    }
    is_valid, err = validate_submission_against_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, missing_q3_answers)
    assert is_valid is False
    assert "Số điện thoại tài xế" in err

def test_api_upload_and_render_arbitrary_form():
    client = TestClient(app)

    arbitrary_schema = {
        "form_id": "customer_feedback_v1",
        "title": "Khảo Sát Ý Kiến Khách Hàng",
        "description": "Đánh giá chất lượng dịch vụ xe điện",
        "settings": {
            "response_limit": 500,
            "access_control": "public",
            "allow_anonymous": True,
            "decree13_required": True
        },
        "questions": [
            {
                "id": "rating",
                "title": "Đánh giá độ hài lòng (1-5 sao)",
                "type": "number",
                "required": True,
                "validation": {"min_value": 1, "max_value": 5}
            },
            {
                "id": "comment",
                "title": "Ý kiến đóng góp",
                "type": "text",
                "required": False
            }
        ]
    }

    # 1. Upload schema via API
    # Admin endpoint requires Basic Auth
    unauth_resp = client.post("/api/v1/forms/upload-schema", json=arbitrary_schema)
    assert unauth_resp.status_code == 401

    resp = client.post("/api/v1/forms/upload-schema", json=arbitrary_schema, auth=("admin", settings.ADMIN_PASSWORD))
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert resp.json()["form_id"] == "customer_feedback_v1"

    # 2. Render form HTML dynamically
    html_resp = client.get("/forms/customer_feedback_v1")
    assert html_resp.status_code == 200
    assert "Khảo Sát Ý Kiến Khách Hàng" in html_resp.text
    assert "Đánh giá độ hài lòng" in html_resp.text

def test_dev_test_mode_submission_routing(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    import io
    from PIL import Image, ImageDraw
    client = TestClient(app)

    # Create mock sharp photo with lines
    img = Image.new("RGB", (600, 800), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for i in range(100, 700, 20):
        draw.line([(50, i), (550, i)], fill=(0, 0, 0), width=4)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)

    # 1. Clear any previous dev submissions
    client.delete("/api/v1/dev/submissions", auth=("admin", settings.ADMIN_PASSWORD))

    # 2. Submit with is_test_mode = True
    resp = client.post(
        "/api/v1/survey/submit-webrtc",
        data={
            "driver_phone": "0988123456",
            "license_plate": "29E-123.45",
            "is_test_mode": "true",
            "driver_name": "Tài xế Test Mode",
            "consent_choice": "Tôi đồng ý",
            "signature_png": "data:image/png;base64,mock",
            "vehicle_type": "Ô tô điện VinFast (GSM)",
            "car_model": "VF 5 Plus",
            "gross_revenue": "650000",
            "odometer_km": "50",
        },
        files={"survey_photo": ("test.jpg", buf, "image/jpeg")}
    )
    assert resp.status_code == 200
    res_json = resp.json()
    assert res_json["mode"] == "TEST_DEV_MODE"
    assert res_json["submission_id"].startswith("DEV-")

    # 3. Inspect temporary dev storage endpoint
    get_resp = client.get("/api/v1/dev/submissions", auth=("admin", settings.ADMIN_PASSWORD))
    assert get_resp.status_code == 200
    dev_data = get_resp.json()
    assert dev_data["count"] >= 1
    assert any(s["submission_id"] == res_json["submission_id"] for s in dev_data["submissions"])

    # 4. Clean up dev storage
    del_resp = client.delete("/api/v1/dev/submissions", auth=("admin", settings.ADMIN_PASSWORD))
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "cleared"

@pytest.mark.asyncio
async def test_idempotency_duplicate_submission(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    import io
    from PIL import Image, ImageDraw
    client = TestClient(app)

    img = Image.new("RGB", (600, 800), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for i in range(100, 700, 20):
        draw.line([(50, i), (550, i)], fill=(0, 0, 0), width=4)
    buf1 = io.BytesIO()
    img.save(buf1, format="JPEG")
    buf1.seek(0)

    idemp_key = "IDEMP-TEST-KEY-999"

    # 1. First submission
    resp1 = client.post(
        "/api/v1/survey/submit-webrtc",
        data={
            "driver_phone": "0988123456",
            "license_plate": "29E-123.45",
            "is_test_mode": "true",
            "idempotency_key": idemp_key,
            "consent_choice": "Tôi đồng ý",
            "signature_png": "data:image/png;base64,mock",
            "driver_name": "Tài xế Test Mode",
            "vehicle_type": "Ô tô điện VinFast (GSM)",
            "car_model": "VF 5 Plus",
            "gross_revenue": "650000",
            "odometer_km": "50",
        },
        files={"survey_photo": ("test1.jpg", buf1, "image/jpeg")}
    )
    assert resp1.status_code == 200
    data1 = resp1.json()
    sub_id_1 = data1["submission_id"]

    # 2. Duplicate submission with exact same idempotency_key
    buf2 = io.BytesIO()
    img.save(buf2, format="JPEG")
    buf2.seek(0)

    resp2 = client.post(
        "/api/v1/survey/submit-webrtc",
        data={
            "driver_phone": "0988123456",
            "license_plate": "29E-123.45",
            "is_test_mode": "true",
            "idempotency_key": idemp_key,
            "consent_choice": "Tôi đồng ý",
            "signature_png": "data:image/png;base64,mock",
            "driver_name": "Tài xế Test Mode",
            "vehicle_type": "Ô tô điện VinFast (GSM)",
            "car_model": "VF 5 Plus",
            "gross_revenue": "650000",
            "odometer_km": "50",
        },
        files={"survey_photo": ("test2.jpg", buf2, "image/jpeg")}
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    sub_id_2 = data2["submission_id"]

    # Must return identical submission_id
    assert sub_id_1 == sub_id_2
    assert data1["status"] == data2["status"]

def test_survey_history_public_access():
    client = TestClient(app)
    # 1. Unauthenticated public request succeeds without Basic Auth
    unauth_resp = client.get("/api/v1/surveys/history?phone=0988123456")
    assert unauth_resp.status_code == 200
    assert unauth_resp.json()["phone"] == "0988123456"

    # 2. Sanitizes input with spaces/dashes
    sanitized_resp = client.get("/api/v1/surveys/history?phone=%200988-123-456%20")
    assert sanitized_resp.status_code == 200
    assert sanitized_resp.json()["phone"] == "0988123456"

    # 3. Rejects invalid phone numbers
    bad_resp = client.get("/api/v1/surveys/history?phone=invalid_phone")
    assert bad_resp.status_code == 400

    # 4. Rate-limiting triggers on excessive queries
    rate_limited = False
    for _ in range(35):
        r = client.get("/api/v1/surveys/history?phone=0999888777")
        if r.status_code == 429:
            rate_limited = True
            break
    assert rate_limited is True

def test_multi_image_validation_count_enforcement():
    # Test schema with exact_image_count, min_images, max_images
    schema = FormSchema(
        form_id="test_multi_image_schema",
        title="Kiểm tra đa ảnh",
        questions=[
            FormQuestion(
                id="exact_cam",
                title="Ảnh bắt buộc đúng 2 tấm",
                type=QuestionType.CAMERA_WEBRTC,
                exact_image_count=2,
                allow_multiple_images=True,
            ),
            FormQuestion(
                id="min_max_cam",
                title="Ảnh từ 2 đến 3 tấm",
                type=QuestionType.CAMERA_WEBRTC,
                min_images=2,
                max_images=3,
                allow_multiple_images=True,
            ),
        ],
    )

    # 1. Exact count failure
    ok, err = validate_submission_against_schema(schema, {"exact_cam": ["img1"], "min_max_cam": ["a", "b"]})
    assert ok is False
    assert "yêu cầu chính xác 2 ảnh (hiện có 1 ảnh)!" in err

    # 2. Min images failure (list)
    ok, err = validate_submission_against_schema(schema, {"exact_cam": ["img1", "img2"], "min_max_cam": ["a"]})
    assert ok is False
    assert "yêu cầu tối thiểu 2 ảnh (hiện có 1 ảnh)!" in err

    # 3. Max images failure (list)
    ok, err = validate_submission_against_schema(schema, {"exact_cam": ["img1", "img2"], "min_max_cam": ["a", "b", "c", "d"]})
    assert ok is False
    assert "không được vượt quá 3 ảnh!" in err

    # 4. Matching keys check f"{q.id}_"
    ok, err = validate_submission_against_schema(schema, {
        "exact_cam_0": "img1",
        "exact_cam_1": "img2",
        "min_max_cam_0": "a",
    })
    assert ok is False
    assert "yêu cầu tối thiểu 2 ảnh (hiện có 1 ảnh)!" in err

    # 5. Dict answers check
    ok, err = validate_submission_against_schema(schema, {
        "exact_cam": {"0": "img1", "1": "img2"},
        "min_max_cam": {"slot0": "a", "slot1": "b"},
    })
    assert ok is True
    assert err is None

def test_multi_image_order_enforcement_inverted_rejected(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    import io
    from PIL import Image, ImageDraw
    client = TestClient(app)

    img = Image.new("RGB", (600, 800), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for i in range(100, 700, 20):
        draw.line([(50, i), (550, i)], fill=(0, 0, 0), width=4)
    buf1 = io.BytesIO()
    img.save(buf1, format="JPEG")
    buf1.seek(0)
    buf2 = io.BytesIO()
    img.save(buf2, format="JPEG")
    buf2.seek(0)

    # Inverted order: Slot 0 sent revenue_screen.jpg, Slot 1 sent odometer.jpg
    resp = client.post(
        "/api/v1/survey/submit-webrtc",
        data={
            "driver_phone": "0988123456",
            "license_plate": "29E-123.45",
            "is_test_mode": "true",
            "bypass_img_check": "false",
            "driver_name": "Tài xế Test Order",
            "consent_choice": "Tôi đồng ý",
            "signature_png": "data:image/png;base64,mock",
            "vehicle_type": "Ô tô điện VinFast (GSM)",
            "car_model": "VF 5 Plus",
            "gross_revenue": "650000",
            "odometer_km": "50",
        },
        files={
            "survey_photo_0": ("revenue_screen.jpg", buf1, "image/jpeg"),
            "survey_photo_1": ("odometer.jpg", buf2, "image/jpeg"),
        }
    )
    assert resp.status_code == 200
    res_json = resp.json()
    assert res_json["status"] == "REJECTED"
    assert "Thứ tự ảnh không chính xác: Ảnh 1 phải là đồng hồ công-tơ-mét, Ảnh 2 phải là màn hình doanh thu!" in res_json["reason"]

def test_multi_image_order_enforcement_correct_approved(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    import io
    from PIL import Image, ImageDraw
    client = TestClient(app)

    img = Image.new("RGB", (600, 800), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for i in range(100, 700, 20):
        draw.line([(50, i), (550, i)], fill=(0, 0, 0), width=4)
    buf1 = io.BytesIO()
    img.save(buf1, format="JPEG")
    buf1.seek(0)
    buf2 = io.BytesIO()
    img.save(buf2, format="JPEG")
    buf2.seek(0)

    # Correct order: Slot 0 sent odometer.jpg, Slot 1 sent revenue_screen.jpg
    resp = client.post(
        "/api/v1/survey/submit-webrtc",
        data={
            "driver_phone": "0988123456",
            "license_plate": "29E-123.45",
            "is_test_mode": "true",
            "bypass_img_check": "false",
            "driver_name": "Tài xế Test Order",
            "consent_choice": "Tôi đồng ý",
            "signature_png": "data:image/png;base64,mock",
            "vehicle_type": "Ô tô điện VinFast (GSM)",
            "car_model": "VF 5 Plus",
            "gross_revenue": "650000",
            "odometer_km": "50",
        },
        files={
            "survey_photo_0": ("odometer.jpg", buf1, "image/jpeg"),
            "survey_photo_1": ("revenue_screen.jpg", buf2, "image/jpeg"),
        }
    )
    assert resp.status_code == 200
    res_json = resp.json()
    assert res_json["status"] == "APPROVED"

