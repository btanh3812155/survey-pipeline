import os
import io
import pytest
from fastapi.testclient import TestClient

from main import app, FORM_REGISTRY
from services.form_engine import DEFAULT_TRUST_BRIDGE_SCHEMA, TEST_TRUST_BRIDGE_SCHEMA
from services.dynamic_form_html import render_dynamic_form_html
from services.google_drive_delivery import (
    upload_to_google_drive,
    LOCAL_IMAGES_DIR,
    MASTER_CSV_FILE,
)


def test_location_question_removed_and_phone_optional():
    """Verifies location question is removed, phone is optional, all others mandatory."""
    q_ids = [q.id for q in DEFAULT_TRUST_BRIDGE_SCHEMA.questions]
    assert "survey_location" not in q_ids, "survey_location question should be removed"

    phone_q = next(q for q in DEFAULT_TRUST_BRIDGE_SCHEMA.questions if q.id == "driver_phone")
    assert phone_q.required is False, "driver_phone should not be mandatory"

    # All other questions must be mandatory
    for q in DEFAULT_TRUST_BRIDGE_SCHEMA.questions:
        if q.id != "driver_phone":
            assert q.required is True, f"Question '{q.id}' must be mandatory"


def test_form_test_mode_split():
    """Current form has no test banner; test copy has test banner."""
    assert DEFAULT_TRUST_BRIDGE_SCHEMA.settings.is_test_mode is False
    assert TEST_TRUST_BRIDGE_SCHEMA.settings.is_test_mode is True

    # Render HTML for current form -> must NOT contain testModeBanner
    html_current = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)
    assert 'id="testModeBanner"' not in html_current

    # Render HTML for test clone -> must contain testModeBanner
    html_test = render_dynamic_form_html(TEST_TRUST_BRIDGE_SCHEMA)
    assert 'id="testModeBanner"' in html_test


def test_header_qr_and_lang_button_alignment():
    """QR button is placed on the same row as language toggle, aligned to the right."""
    html_content = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)
    assert 'class="top-nav-bar"' in html_content
    assert 'btnOpenQrModal' in html_content
    assert 'btn-lang-toggle' in html_content

    # In HTML order, QR button appears right before btn-lang-toggle inside top-nav-bar
    nav_idx = html_content.index('class="top-nav-bar"')
    qr_idx = html_content.index('id="btnOpenQrModal"', nav_idx)
    lang_idx = html_content.index('class="btn-lang-toggle"', nav_idx)
    assert qr_idx < lang_idx, "QR button must be on the left of language button"


@pytest.mark.asyncio
async def test_google_drive_delivery_structure():
    """Tests saving images to subfolder per submission and appending row to master CSV."""
    sub_id = "DEV-DRIVE-TEST-01"
    sample_record = {
        "submission_id": sub_id,
        "status": "APPROVED",
        "driver_phone": "0988555666",
        "driver_name": "Phan Văn C",
        "collaborator_id": "CTV001_Dũng_HN",
        "license_plate": "29A-999.88",
        "gross_revenue": 1500000,
        "trip_count": 22,
        "odometer_km": 45000,
        "gps_latitude": 21.0285,
        "gps_longitude": 105.8542,
        "submitted_at": "2026-11-07T09:30:00Z",
        "reason": "Hợp lệ",
    }
    sample_images = [
        {"filename": "slot_0_app_profile.webp", "bytes": b"fake_webp_bytes_0"},
        {"filename": "slot_1_vehicle.webp", "bytes": b"fake_webp_bytes_1"},
    ]

    ok = await upload_to_google_drive(
        submission_id=sub_id,
        record=sample_record,
        images=sample_images,
    )
    assert ok is True

    # 1. Check subfolder in Images
    sub_folder = os.path.join(LOCAL_IMAGES_DIR, sub_id)
    assert os.path.exists(sub_folder)
    assert os.path.exists(os.path.join(sub_folder, "slot_0_app_profile.webp"))
    assert os.path.exists(os.path.join(sub_folder, "slot_1_vehicle.webp"))

    # 2. Check row in Master CSV
    assert os.path.exists(MASTER_CSV_FILE)
    with open(MASTER_CSV_FILE, "r", encoding="utf-8-sig") as f:
        content = f.read()
    assert sub_id in content
    assert "0988555666" in content
    assert "Phan Văn C" in content


def test_submit_form_optional_phone(monkeypatch):
    """Submitting survey without phone number succeeds gracefully without crashing."""
    monkeypatch.setenv("MOCK_AI", "1")
    client = TestClient(app)
    resp = client.post(
        "/api/v1/survey/submit-webrtc",
        data={
            "driver_name": "Tài xế Không Để SĐT",
            "license_plate": "30E-123.45",
            "gross_revenue": "800000",
            "is_test_mode": "true",
            "bypass_img_check": "true",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "submission_id" in data
    assert data["driver_phone"] is None
