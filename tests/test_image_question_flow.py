"""End-to-end image-question flow: submit, persist private media, serve to admin."""
import hashlib
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from main import FORM_REGISTRY
from routers import admin_data, survey as survey_router
from services import submission_media
from services.form_engine import (
    FormQuestion,
    FormSchema,
    FormSettings,
    ImageCheckConfig,
    QuestionType,
)


@pytest.fixture
def isolated_image_app(monkeypatch, tmp_path):
    media_root = tmp_path / "submission_photos"
    media_root.mkdir()
    monkeypatch.setenv("MOCK_AI", "1")
    monkeypatch.setattr(survey_router.settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(survey_router, "SUBMISSION_PHOTOS_DIR", str(media_root))
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(media_root))
    monkeypatch.setattr(survey_router, "get_db_pool", lambda: None)
    monkeypatch.setattr(survey_router, "get_redis_client", lambda: None)
    monkeypatch.setattr(survey_router, "append_dev_submission", lambda _record: None)
    monkeypatch.setattr(survey_router, "refresh_form_registry_from_disk", lambda: None)
    monkeypatch.setattr(survey_router, "send_telegram_notification", AsyncMock())
    monkeypatch.setattr(survey_router, "dispatch_submission_telegram_notifications", AsyncMock())
    monkeypatch.setattr(survey_router, "dispatch_action_checklist", AsyncMock())
    form_id = "IMAGE_FLOW_TEST"
    schema = FormSchema(
        form_id=form_id,
        title="Image flow",
        settings=FormSettings(
            decree13_required=False,
            is_test_mode=True,
            image_checks=ImageCheckConfig(
                enable_blur_check=False,
                enable_ai_vision=False,
                enable_anti_fraud=False,
                enable_vehicle_match=False,
                enable_ai_generated_check=False,
                require_unique=False,
            ),
        ),
        questions=[FormQuestion(
            id="evidence", title="Evidence", type=QuestionType.CAMERA_WEBRTC,
            required=True, image_slots=[{"slot": 0, "title": "Evidence", "role": "app_profile", "required": True}],
            min_images=1, max_images=1, exact_image_count=1,
        )],
    )
    FORM_REGISTRY[form_id] = schema
    app = FastAPI()
    app.include_router(survey_router.router)
    try:
        yield TestClient(app), form_id, media_root
    finally:
        FORM_REGISTRY.pop(form_id, None)


def _jpeg() -> bytes:
    out = BytesIO()
    Image.new("RGB", (160, 200), (40, 120, 200)).save(out, format="JPEG")
    return out.getvalue()


def test_image_question_submit_persists_and_serves_private_media(isolated_image_app, monkeypatch):
    client, form_id, media_root = isolated_image_app
    original = _jpeg()
    response = client.post(
        "/api/v1/survey/submit-webrtc",
        data={
            "form_id": form_id,
            "driver_phone": "0988123456",
            "driver_name": "Image Flow",
            "vehicle_type": "CAR",
            "is_test_mode": "true",
            "bypass_img_check": "true",
            "signature_png": "data:image/png;base64,mock",
        },
        files={"evidence__file_0": ("evidence.jpg", original, "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    submission_id = body["submission_id"]
    assert submission_id

    folder = media_root / submission_id
    stored = sorted(path for path in folder.iterdir() if path.is_file())
    assert stored, "image question did not write private media"
    first = stored[0]
    manifest = submission_media.build_manifest(
        submission_id,
        [{"name": first.name, "question_id": "evidence", "slot_index": 0, "flat_index": 0,
          "mime": "image/webp" if first.suffix == ".webp" else "image/jpeg"}],
        base_dir=str(folder),
    )
    assert manifest[0]["sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()

    async def fake_manifest(_pool, _sid):
        return manifest

    monkeypatch.setattr(admin_data, "load_manifest", fake_manifest)
    admin_app = FastAPI()
    admin_app.include_router(admin_data.router)
    admin_app.dependency_overrides[admin_data.verify_admin_auth] = lambda: "image-admin"
    admin_client = TestClient(admin_app)
    unauth_app = FastAPI()
    unauth_app.include_router(admin_data.router)
    assert TestClient(unauth_app).get(
        f"/api/v1/submissions/{submission_id}/photos"
    ).status_code == 401
    listing = admin_client.get(f"/api/v1/submissions/{submission_id}/photos")
    assert listing.status_code == 200
    urls = listing.json()["photos"]
    assert len(urls) == 1 and urls[0].endswith(first.name)
    image = admin_client.get(urls[0])
    assert image.status_code == 200
    assert image.content == first.read_bytes()
