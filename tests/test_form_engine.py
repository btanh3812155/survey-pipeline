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
        "driver_signature": "data:image/png;base64,mock",
        "consent_choice": "Tôi đồng ý",
        "driver_phone": "0988123456",
        "driver_name": "Nguyễn Văn An",
        "platform_app": "Grab",
        "vehicle_category": "Xe máy 2 bánh",
        "working_hours_day": "8 - 12 tiếng (Toàn thời gian)",
        "daily_net_income": "650000",
        "monthly_gross_revenue": "18500000",
        "camera_receipt": "mock_blob"
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
        "platform_app": "Grab",
        "vehicle_category": "Xe máy 2 bánh",
        "working_hours_day": "8 - 12 tiếng (Toàn thời gian)",
        "daily_net_income": "650000",
        "camera_receipt": "mock_blob"
    }
    is_valid, err = validate_submission_against_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, invalid_answers)
    assert is_valid is False
    assert "Số điện thoại" in err

def test_conditional_question_branching():
    answers_with_consent = {
        "driver_signature": "data:image/png;base64,mock",
        "consent_choice": "Tôi đồng ý",
        "driver_phone": "0988123456",
        "driver_name": "Trần Văn Bình",
        "platform_app": "Be",
        "vehicle_category": "Ô tô 4 - 7 chỗ",
        "working_hours_day": "8 - 12 tiếng (Toàn thời gian)",
        "daily_net_income": "1200000",
        "camera_receipt": "mock_blob"
    }
    is_valid, err = validate_submission_against_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, answers_with_consent)
    assert is_valid is True
    assert err is None

def test_conditional_no_consent_skips_subsequent_questions():
    # When user chooses "Tôi không đồng ý", questions 3-9 are skipped
    no_consent_answers = {
        "driver_signature": "data:image/png;base64,mock",
        "consent_choice": "Tôi không đồng ý"
    }
    is_valid, err = validate_submission_against_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, no_consent_answers)
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
    # Admin endpoint requires Basic Auth
    unauth_resp = client.post("/api/v1/forms/upload-schema", json=arbitrary_schema)
    assert unauth_resp.status_code == 401

    resp = client.post("/api/v1/forms/upload-schema", json=arbitrary_schema, auth=("admin", "Legendofzeus2@"))
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
            "idempotency_key": idemp_key
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
            "idempotency_key": idemp_key
        },
        files={"survey_photo": ("test2.jpg", buf2, "image/jpeg")}
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    sub_id_2 = data2["submission_id"]

    # Must return identical submission_id
    assert sub_id_1 == sub_id_2
    assert data1["status"] == data2["status"]

def test_survey_history_auth():
    client = TestClient(app)
    # 1. Unauthenticated request must return 401
    unauth_resp = client.get("/api/v1/surveys/history?phone=0988123456")
    assert unauth_resp.status_code == 401

    # 2. Authenticated request returns 200
    auth_resp = client.get("/api/v1/surveys/history?phone=0988123456", auth=("admin", "Legendofzeus2@"))
    assert auth_resp.status_code == 200
    assert auth_resp.json()["phone"] == "0988123456"
