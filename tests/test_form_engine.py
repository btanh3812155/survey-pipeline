import pytest
from fastapi.testclient import TestClient
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
        "driver_phone": "0988123456",
        "driver_name": "Nguyễn Văn An",
        "vehicle_type": "Ô tô điện VinFast (GSM)",
        "car_model": "VF 5 Plus",
        "license_plate": "29E-123.45",
        "gross_revenue": "1500000",
        "camera_receipt": "mock_blob",
        "driver_signature": "data:image/png;base64,mock"
    }
    is_valid, err = validate_submission_against_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, valid_answers)
    assert is_valid is True
    assert err is None

def test_regex_constraint_invalid_phone():
    invalid_answers = {
        "driver_phone": "12345", # Fails ^0[0-9]{9,10}$
        "driver_name": "Nguyễn Văn An",
        "vehicle_type": "Xe máy điện (GSM Bike)",
        "license_plate": "29E-123.45",
        "camera_receipt": "mock_blob",
        "driver_signature": "data:image/png;base64,mock"
    }
    is_valid, err = validate_submission_against_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, invalid_answers)
    assert is_valid is False
    assert "Số điện thoại" in err

def test_conditional_question_branching():
    # When vehicle_type is "Xe máy điện", car_model is conditional and should be skipped
    bike_answers = {
        "driver_phone": "0988123456",
        "driver_name": "Trần Văn Bình",
        "vehicle_type": "Xe máy điện (GSM Bike)",
        "license_plate": "59A-999.88",
        "camera_receipt": "mock_blob",
        "driver_signature": "data:image/png;base64,mock"
    }
    is_valid, err = validate_submission_against_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, bike_answers)
    assert is_valid is True
    assert err is None

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
    resp = client.post("/api/v1/forms/upload-schema", json=arbitrary_schema)
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
    client.delete("/api/v1/dev/submissions")

    # 2. Submit with is_test_mode = True
    resp = client.post(
        "/api/v1/survey/submit-webrtc",
        data={
            "driver_phone": "0988123456",
            "license_plate": "29E-123.45",
            "is_test_mode": "true",
            "driver_name": "Tài xế Test Mode"
        },
        files={"survey_photo": ("test.jpg", buf, "image/jpeg")}
    )
    assert resp.status_code == 200
    res_json = resp.json()
    assert res_json["mode"] == "TEST_DEV_MODE"
    assert res_json["submission_id"].startswith("DEV-")

    # 3. Inspect temporary dev storage endpoint
    get_resp = client.get("/api/v1/dev/submissions")
    assert get_resp.status_code == 200
    dev_data = get_resp.json()
    assert dev_data["count"] >= 1
    assert any(s["submission_id"] == res_json["submission_id"] for s in dev_data["submissions"])

    # 4. Clean up dev storage
    del_resp = client.delete("/api/v1/dev/submissions")
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "cleared"
