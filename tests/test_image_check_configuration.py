import io
import pytest
import numpy as np
from PIL import Image
from fastapi.testclient import TestClient

from main import app, FORM_REGISTRY
from services.form_engine import (
    FormSchema,
    FormSettings,
    FormQuestion,
    QuestionType,
    ImageCheckConfig,
    get_effective_image_checks,
)

client = TestClient(app)

ADMIN_AUTH = ("admin", "Legendofzeus2@")


def make_blurry_image_bytes() -> bytes:
    """Generates an intentionally blurry image with 0 Laplacian variance."""
    blurry_arr = np.ones((300, 300), dtype=np.uint8) * 128
    blurry_pil = Image.fromarray(blurry_arr)
    buf = io.BytesIO()
    blurry_pil.save(buf, format="JPEG")
    return buf.getvalue()


def make_sharp_image_bytes() -> bytes:
    """Generates a high-contrast sharp image with strong edges."""
    img = Image.new("RGB", (600, 600), color=(255, 255, 255))
    for x in range(0, 600, 20):
        for y in range(600):
            if (x // 20) % 2 == 0:
                img.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_default_image_check_config_instantiation():
    """Test default ImageCheckConfig model instantiation and field defaults."""
    cfg = ImageCheckConfig()
    assert cfg.enable_blur_check is True
    assert cfg.blur_threshold is None
    assert cfg.enable_ai_vision is True
    assert cfg.enable_vehicle_match is True
    assert cfg.enable_anti_fraud is True
    assert cfg.enable_ai_generated_check is True
    assert cfg.enable_screen_photo_check is True

    custom_cfg = ImageCheckConfig(
        enable_blur_check=False,
        blur_threshold=65.5,
        enable_ai_vision=False,
        enable_vehicle_match=False,
        enable_anti_fraud=False,
        enable_ai_generated_check=False,
        enable_screen_photo_check=False,
    )
    assert custom_cfg.enable_blur_check is False
    assert custom_cfg.blur_threshold == 65.5
    assert custom_cfg.enable_ai_vision is False


def test_get_effective_image_checks_global_defaults():
    """Test get_effective_image_checks returns global defaults when question has None."""
    # When schema is None
    assert get_effective_image_checks(None) == ImageCheckConfig()

    schema = FormSchema(
        form_id="test_global_defaults",
        title="Test Global Defaults",
        settings=FormSettings(
            image_checks=ImageCheckConfig(
                enable_blur_check=False,
                blur_threshold=55.0,
                enable_ai_vision=True,
            )
        ),
        questions=[
            FormQuestion(
                id="q_camera_1",
                title="Chụp ảnh giấy tờ",
                type=QuestionType.CAMERA_WEBRTC,
                image_checks=None,
            )
        ],
    )

    # Question has None -> returns global settings
    effective = get_effective_image_checks(schema, "q_camera_1")
    assert effective.enable_blur_check is False
    assert effective.blur_threshold == 55.0
    assert effective.enable_ai_vision is True

    # Unknown question id -> returns global settings
    effective_unknown = get_effective_image_checks(schema, "nonexistent_id")
    assert effective_unknown.enable_blur_check is False
    assert effective_unknown.blur_threshold == 55.0


def test_get_effective_image_checks_question_override():
    """Test get_effective_image_checks returning question-level override when specified."""
    schema = FormSchema(
        form_id="test_question_override",
        title="Test Question Override",
        settings=FormSettings(
            image_checks=ImageCheckConfig(
                enable_blur_check=True,
                blur_threshold=40.0,
                enable_ai_vision=True,
            )
        ),
        questions=[
            FormQuestion(
                id="q_camera_custom",
                title="Chụp ảnh giấy tờ ngoại lệ",
                type=QuestionType.CAMERA_WEBRTC,
                image_checks=ImageCheckConfig(
                    enable_blur_check=False,
                    enable_ai_vision=False,
                ),
            )
        ],
    )

    effective = get_effective_image_checks(schema, "q_camera_custom")
    assert effective.enable_blur_check is False
    assert effective.enable_ai_vision is False
    assert effective.blur_threshold == 40.0  # Inherited from base global setting


def test_submit_webrtc_with_enable_blur_check_false():
    """
    Test /api/v1/survey/submit-webrtc with enable_blur_check=False:
    submit a severely blurry image and assert it does NOT reject with blurry error.
    """
    form_id = "test_form_blur_disabled"
    schema = FormSchema(
        form_id=form_id,
        title="Test Form Blur Disabled",
        settings=FormSettings(
            is_test_mode=True,
            image_checks=ImageCheckConfig(
                enable_blur_check=False,
                enable_ai_vision=False,  # Skip AI call as well to isolate blur testing
            ),
        ),
        questions=[
            FormQuestion(
                id="camera_receipt",
                title="Chụp ảnh biên lai",
                type=QuestionType.CAMERA_WEBRTC,
            )
        ],
    )
    FORM_REGISTRY[form_id] = schema

    blurry_bytes = make_blurry_image_bytes()
    resp = client.post(
        "/api/v1/survey/submit-webrtc",
        data={
            "form_id": form_id,
            "question_id": "camera_receipt",
            "driver_phone": "0988111222",
            "is_test_mode": "true",
            "bypass_img_check": "false",
        },
        files={"survey_photo": ("blurry.jpg", io.BytesIO(blurry_bytes), "image/jpeg")},
    )

    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["status"] == "APPROVED"
    assert "mờ hoặc rung tay" not in res_data.get("reason", "")


def test_submit_webrtc_with_enable_ai_vision_false():
    """
    Test /api/v1/survey/submit-webrtc with enable_ai_vision=False:
    assert it completes immediately with status APPROVED and skips AI call.
    """
    form_id = "test_form_ai_disabled"
    schema = FormSchema(
        form_id=form_id,
        title="Test Form AI Disabled",
        settings=FormSettings(
            is_test_mode=True,
            image_checks=ImageCheckConfig(
                enable_blur_check=True,
                enable_ai_vision=False,
            ),
        ),
        questions=[
            FormQuestion(
                id="camera_receipt",
                title="Chụp ảnh biên lai",
                type=QuestionType.CAMERA_WEBRTC,
            )
        ],
    )
    FORM_REGISTRY[form_id] = schema

    # Clear dev submissions before test
    client.delete("/api/v1/dev/submissions", auth=ADMIN_AUTH)

    sharp_bytes = make_sharp_image_bytes()
    resp = client.post(
        "/api/v1/survey/submit-webrtc",
        data={
            "form_id": form_id,
            "question_id": "camera_receipt",
            "driver_phone": "0988333444",
            "is_test_mode": "true",
            "bypass_img_check": "false",
        },
        files={"survey_photo": ("sharp.jpg", io.BytesIO(sharp_bytes), "image/jpeg")},
    )

    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["status"] == "APPROVED"
    assert "bỏ qua kiểm tra ai" in res_data.get("reason", "").lower()

    # Verify dev submission record shows ai_result was skipped
    dev_resp = client.get("/api/v1/dev/submissions", auth=ADMIN_AUTH)
    assert dev_resp.status_code == 200
    subs = dev_resp.json().get("submissions", [])
    matching = [s for s in subs if s.get("submission_id") == res_data["submission_id"]]
    assert len(matching) == 1
    dev_rec = matching[0]
    assert dev_rec["ai_result"].get("skipped") is True
    assert "Bỏ qua kiểm tra AI" in dev_rec["ai_result"].get("reason", "")


def test_schema_upload_with_custom_global_and_question_image_checks():
    """Test schema upload via /api/v1/forms/upload-schema with custom global and question-level image_checks."""
    custom_schema_payload = {
        "form_id": "test_custom_checks_upload",
        "title": "Biểu Mẫu Tùy Chỉnh Kiểm Tra Ảnh",
        "description": "Upload schema test with customized image_checks",
        "settings": {
            "response_limit": 100,
            "access_control": "public",
            "is_test_mode": True,
            "image_checks": {
                "enable_blur_check": False,
                "blur_threshold": 30.0,
                "enable_ai_vision": False,
                "enable_vehicle_match": False,
                "enable_anti_fraud": False,
                "enable_ai_generated_check": False,
                "enable_screen_photo_check": False,
            },
        },
        "questions": [
            {
                "id": "q_webrtc_1",
                "title": "Ảnh hiện trường",
                "type": "camera_webrtc",
                "required": True,
                "image_checks": {
                    "enable_blur_check": True,
                    "blur_threshold": 50.0,
                    "enable_ai_vision": True,
                },
            }
        ],
    }

    resp = client.post(
        "/api/v1/forms/upload-schema",
        json=custom_schema_payload,
        auth=ADMIN_AUTH,
    )
    assert resp.status_code == 200
    res = resp.json()
    assert res["status"] == "success"
    assert res["form_id"] == "test_custom_checks_upload"

    # Verify registered schema in FORM_REGISTRY
    saved_schema = FORM_REGISTRY.get("test_custom_checks_upload")
    assert saved_schema is not None
    assert saved_schema.settings.image_checks.enable_blur_check is False
    assert saved_schema.settings.image_checks.blur_threshold == 30.0
    assert saved_schema.questions[0].image_checks is not None
    assert saved_schema.questions[0].image_checks.enable_blur_check is True
    assert saved_schema.questions[0].image_checks.blur_threshold == 50.0
