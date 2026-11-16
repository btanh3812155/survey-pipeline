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
        "gsm_duration": "Từ 6 – 12 tháng",
        "gsm_contract_type": "Xe Taxi xanh GSM",
        "daily_trips": "18",
        "monthly_income_range": "12 – 20 triệu VNĐ",
        "gsm_incentive_policy": "Có áp dụng chỉ tiêu",
        "gross_revenue": "1250000",
        "odometer_km": "15200",
        "camera_receipt": [f"mock_img_{i}.jpg" for i in range(7)],
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
        "gsm_duration": "Từ 6 – 12 tháng",
        "gsm_contract_type": "Xe Taxi xanh GSM",
        "daily_trips": "18",
        "monthly_income_range": "12 – 20 triệu VNĐ",
        "gsm_incentive_policy": "Có áp dụng chỉ tiêu",
        "gross_revenue": "1200000",
        "odometer_km": "5000",
        "camera_receipt": [f"mock_img_{i}.jpg" for i in range(7)],
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
        "gsm_duration": "Từ 6 – 12 tháng",
        "gsm_contract_type": "Xe Taxi xanh GSM",
        "daily_trips": "18",
        "monthly_income_range": "12 – 20 triệu VNĐ",
        "gsm_incentive_policy": "Có áp dụng chỉ tiêu",
        "gross_revenue": "300000",
        "odometer_km": "2000",
        "camera_receipt": [f"mock_img_{i}.jpg" for i in range(7)],
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

    # Valid signature + consent, but missing required vehicle_type (Câu 1)
    missing_vehicle_answers = {
        "driver_signature": "data:image/png;base64,mock",
        "consent_choice": "Tôi đồng ý",
    }
    is_valid, err = validate_submission_against_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, missing_vehicle_answers)
    assert is_valid is False
    assert "Hiện tại anh đang chạy dòng xe nào" in err

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

    admin_auth = (settings.ADMIN_USERNAME, settings.ADMIN_PASSWORD)

    # 2. Authenticated request succeeds and sanitizes input with spaces/dashes
    sanitized_resp = client.get("/api/v1/surveys/history?phone=%200988-123-456%20", auth=admin_auth)
    assert sanitized_resp.status_code == 200
    assert sanitized_resp.json()["phone"] == "0988123456"

    # 3. Rejects invalid phone numbers
    bad_resp = client.get("/api/v1/surveys/history?phone=invalid_phone", auth=admin_auth)
    assert bad_resp.status_code == 400

    # 4. Rate-limiting triggers on excessive queries
    rate_limited = False
    for _ in range(35):
        r = client.get("/api/v1/surveys/history?phone=0999888777", auth=admin_auth)
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

def test_multi_image_order_agnostic_matching_success(monkeypatch):
    """Verifies that uploading images in mixed/inverted order matches slots dynamically after classification."""
    monkeypatch.setenv("MOCK_AI", "1")
    import io, random, time
    from PIL import Image, ImageDraw
    client = TestClient(app)

    def make_buf(idx: int):
        im = Image.new("RGB", (600, 800), (255, 255, 255))
        d = ImageDraw.Draw(im)
        rng = random.Random(f"agnostic_{time.time()}_{idx}")
        for _ in range(30):
            x1 = rng.randint(0, 500)
            y1 = rng.randint(0, 700)
            d.rectangle([(x1, y1), (x1 + rng.randint(20, 80), y1 + rng.randint(20, 80))], fill=(rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)))
        b = io.BytesIO()
        im.save(b, format="JPEG")
        b.seek(0)
        return b

    # Swapped order: Slot 0 sent vehicle, Slot 1 sent revenue, Slot 2 sent app_profile!
    files_payload = {
        "survey_photo_0": ("vehicle.jpg", make_buf(0), "image/jpeg"),
        "survey_photo_1": ("revenue_screen.jpg", make_buf(1), "image/jpeg"),
        "survey_photo_2": ("app_profile.jpg", make_buf(2), "image/jpeg"),
        "survey_photo_3": ("slot3.jpg", make_buf(3), "image/jpeg"),
        "survey_photo_4": ("slot4.jpg", make_buf(4), "image/jpeg"),
        "survey_photo_5": ("slot5.jpg", make_buf(5), "image/jpeg"),
        "survey_photo_6": ("slot6.jpg", make_buf(6), "image/jpeg"),
    }
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
            "gsm_duration": "Từ 6 – 12 tháng",
            "gsm_contract_type": "Xe Taxi xanh GSM",
            "daily_trips": "18",
            "monthly_income_range": "12 – 20 triệu VNĐ",
            "gsm_incentive_policy": "Có áp dụng chỉ tiêu",
            "gross_revenue": "650000",
            "odometer_km": "50",
        },
        files=files_payload,
    )
    assert resp.status_code == 200
    res_json = resp.json()
    assert res_json["status"] == "APPROVED"
def test_multi_image_order_enforcement_correct_approved(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    import io
    from PIL import Image, ImageDraw
    client = TestClient(app)

    def make_buf(idx: int):
        import random, time
        im = Image.new("RGB", (600, 800), (255, 255, 255))
        d = ImageDraw.Draw(im)
        rng = random.Random(f"slot_test_{time.time()}_{idx}")
        for _ in range(30):
            x1 = rng.randint(0, 500)
            y1 = rng.randint(0, 700)
            d.rectangle([(x1, y1), (x1 + rng.randint(20, 80), y1 + rng.randint(20, 80))], fill=(rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)))
        b = io.BytesIO()
        im.save(b, format="JPEG")
        b.seek(0)
        return b

    # Correct order: Slot 0 sent app_profile, Slot 1 sent vehicle, Slots 2..6 sent distinct income screens
    files_payload = {
        "survey_photo_0": ("app_profile.jpg", make_buf(0), "image/jpeg"),
        "survey_photo_1": ("vehicle.jpg", make_buf(1), "image/jpeg"),
        "survey_photo_2": ("revenue_screen.jpg", make_buf(2), "image/jpeg"),
        "survey_photo_3": ("slot3.jpg", make_buf(3), "image/jpeg"),
        "survey_photo_4": ("slot4.jpg", make_buf(4), "image/jpeg"),
        "survey_photo_5": ("slot5.jpg", make_buf(5), "image/jpeg"),
        "survey_photo_6": ("slot6.jpg", make_buf(6), "image/jpeg"),
    }
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
            "gsm_duration": "Từ 6 – 12 tháng",
            "gsm_contract_type": "Xe Taxi xanh GSM",
            "daily_trips": "18",
            "monthly_income_range": "12 – 20 triệu VNĐ",
            "gsm_incentive_policy": "Có áp dụng chỉ tiêu",
            "gross_revenue": "650000",
            "odometer_km": "50",
        },
        files=files_payload,
    )
    assert resp.status_code == 200
    res_json = resp.json()
    assert res_json["status"] == "APPROVED"


def test_dynamic_form_qr_code_and_camera_tooltip_and_floating_buttons():
    """Verify QR code share URL generation, camera overlay styling, and floating buttons."""
    client = TestClient(app)
    # 1. Base form without ?ref= returns CTV selector landing page with dropdown using short ctv001 values
    resp = client.get("/forms/income_survey")
    assert resp.status_code == 200
    assert "ctvSelect" in resp.text
    assert 'value="TUAN_LM_HCM_001"' in resp.text
    assert "TUAN_LM_HCM_001" in resp.text
    assert "Mở Biểu Mẫu" in resp.text

    # 2. Form with short ?ref=ctv001 opens individual form with resolved and locked CTV
    ref_resp = client.get("/forms/income_survey?ref=TUAN_LM_HCM_001")
    assert ref_resp.status_code == 200
    html_content = ref_resp.text
    assert "getFormShareUrl" in html_content
    assert 'LOCKED_CTV_CODE = "TUAN_LM_HCM_001"' in html_content
    assert 'ACTIVE_REF_CODE = "TUAN_LM_HCM_001"' in html_content
    # Verify clean text formatting without &amp; double escaping
    assert "Xe tự mua &amp; chạy" not in html_content
    assert "Xe tự mua & chạy" in html_content
    assert "LINK TRUY CẬP BIỂU MẪU:" in html_content
    assert ".camera-overlay-slot-badge" in html_content
    assert "white-space: normal" in html_content
    assert "word-break: break-word" in html_content
    assert "font-size: 0.95rem" in html_content
    assert "width: 170px" in html_content

    # 3. CTV Slug form backwards compatibility
    ctv_resp = client.get("/forms/income-survey-70652f26d42b71")
    assert ctv_resp.status_code == 200
    ctv_html = ctv_resp.text
    assert 'const ACTIVE_CTV_SLUG = "income-survey-70652f26d42b71";' in ctv_html
    assert "TUAN_LM_HCM_001" in ctv_html
    assert "getFormShareUrl" in ctv_html
def test_multiple_choice_option_groups_and_mutual_exclusivity():
    """Verify multiple choice question option groups, mutual exclusivity, and compatibility."""
    # 1. Test using DEFAULT_TRUST_BRIDGE_SCHEMA for gsm_incentive_policy
    base_answers = {
        "driver_signature": "data:image/png;base64,mocksignature",
        "consent_choice": "Tôi đồng ý",
        "driver_phone": "0987654321",
        "driver_name": "Nguyễn Văn An",
        "license_plate": "29E-123.45",
        "vehicle_type": "Ô tô điện VinFast (GSM)",
        "car_model": "VF 5 Plus",
        "gsm_duration": "Từ 6 – 12 tháng",
        "gsm_contract_type": "Xe Taxi xanh GSM",
        "daily_trips": "18",
        "monthly_income_range": "12 – 20 triệu VNĐ",
        "gross_revenue": "1250000",
        "odometer_km": "15200",
        "camera_receipt": [f"mock_img_{i}.jpg" for i in range(7)],
    }

    # Case 1: Selecting multiple options in the same group ("applicable") succeeds
    valid_same_group_answers = dict(base_answers)
    valid_same_group_answers["gsm_incentive_policy"] = [
        "Có áp dụng chỉ tiêu",
        "Có khung giờ vàng",
        "Có chương trình thưởng",
    ]
    is_valid, err = validate_submission_against_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, valid_same_group_answers)
    assert is_valid is True
    assert err is None

    # Case 2: Selecting options from mutually exclusive groups ("applicable" vs "none_exclusive") fails
    invalid_exclusive_answers = dict(base_answers)
    invalid_exclusive_answers["gsm_incentive_policy"] = [
        "Có áp dụng chỉ tiêu",
        "Không có",
    ]
    is_valid, err = validate_submission_against_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, invalid_exclusive_answers)
    assert is_valid is False
    assert "Các lựa chọn thuộc nhóm đối lập không thể cùng được chọn!" in err

    # Case 3: Declaring groups compatible allows them to coexist
    custom_schema = FormSchema(
        form_id="test_mc_groups",
        title="Test Multi Choice Groups",
        questions=[
            FormQuestion(
                id="mc_q1",
                title="Câu hỏi khảo sát nhóm",
                type=QuestionType.MULTIPLE_CHOICE,
                required=True,
                options=[
                    {"label": "Opt A1", "group": "group_a"},
                    {"label": "Opt A2", "group": "group_a"},
                    {"label": "Opt B1", "group": "group_b"},
                    {"label": "Opt C1", "group": "group_c"},
                ],
                group_compatibility={
                    "group_a": ["group_b"],  # group_a and group_b are declared compatible
                },
            )
        ],
    )

    # Sub-case 3a: Compatible groups group_a and group_b coexist -> valid
    compat_answers = {"mc_q1": ["Opt A1", "Opt B1"]}
    is_valid, err = validate_submission_against_schema(custom_schema, compat_answers)
    assert is_valid is True
    assert err is None

    # Sub-case 3b: Incompatible groups group_b and group_c coexist -> invalid
    incompat_answers = {"mc_q1": ["Opt B1", "Opt C1"]}
    is_valid, err = validate_submission_against_schema(custom_schema, incompat_answers)
    assert is_valid is False
    assert "Các lựa chọn thuộc nhóm đối lập không thể cùng được chọn!" in err

    # Sub-case 3c: group_a and group_c are incompatible -> invalid
    incompat_answers_2 = {"mc_q1": ["Opt A1", "Opt C1"]}
    is_valid, err = validate_submission_against_schema(custom_schema, incompat_answers_2)
    assert is_valid is False
    assert "Các lựa chọn thuộc nhóm đối lập không thể cùng được chọn!" in err


def test_submit_validates_against_submitted_form_only(monkeypatch):
    """A submission must never be validated against the default form's questions."""
    monkeypatch.setenv("MOCK_AI", "1")
    from main import app, FORM_REGISTRY
    from services.form_engine import FormSchema as _FS, FormQuestion as _FQ, FormSettings as _FSet, QuestionType as _QT

    form_id = "unit_required_form"
    FORM_REGISTRY[form_id] = _FS(
        form_id=form_id,
        title="Unit Required Form",
        settings=_FSet(decree13_required=False, is_test_mode=True),
        questions=[_FQ(id="q_req", title="Bắt buộc thử", type=_QT.TEXT, required=True)],
    )
    client = TestClient(app)
    common = {"is_test_mode": "true", "bypass_img_check": "true"}

    resp = client.post(
        "/api/v1/survey/submit-webrtc",
        data={"form_id": form_id, **common},
    )
    body = resp.json()
    assert "Bắt buộc thử" in (body.get("reason") or ""), body
    assert "Cam kết thông tin" not in (body.get("reason") or "")

    legacy = client.post("/api/v1/survey/submit-webrtc", data=common)
    legacy_reason = legacy.json().get("reason") or ""
    assert "Cam kết thông tin" not in legacy_reason
