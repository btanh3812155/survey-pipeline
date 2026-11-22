import os
import io
import asyncio
import pytest
import shutil
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
    """Tests saving images to subfolder per submission and appending row with pure Vietnamese headers to master CSV."""
    sub_id = "VN-REAL-8F2A1C90"
    sample_record = {
        "submission_id": sub_id,
        "form_id": "income_survey",
        "status": "APPROVED",
        "driver_phone": "0988555666",
        "driver_name": "Phan Văn C",
        "collaborator_id": "CTV001_Dũng_HN",
        "license_plate": "29A-999.88",
        "form_data": {
            "vehicle_type": "Ô tô",
            "car_model": "VF e34",
            "gsm_duration": "Từ 6 tháng đến 1 năm",
            "gsm_contract_type": "Thuê xe tự doanh",
            "daily_trips": "25",
            "monthly_income_range": "15 - 20 triệu",
            "gsm_incentive_policy": ["Có áp dụng chỉ tiêu", "Có thưởng tuần"],
        },
        "ocr_data": {
            "gross_revenue": 1500000,
            "net_income": 1200000,
            "discount_and_tax": -300000,
            "total_trips": 22,
            "online_hours": "10h 30m",
            "period": "Tuần 42",
            "confidence_score": 0.98,
            "is_legible": True,
            "detected_app": "XANH_SM",
        },
        "gps_latitude": 21.0285,
        "gps_longitude": 105.8542,
        "submitted_at": "2026-11-07T09:30:00Z",
    }
    sample_images = [
        {"filename": "slot_0_app_profile.webp", "bytes": b"fake_webp_bytes_0"},
        {"filename": "slot_1_vehicle.webp", "bytes": b"fake_webp_bytes_1"},
    ]

    # Test non-approved rejected form is excluded
    rejected_record = dict(sample_record)
    rejected_record["status"] = "REJECTED"
    rejected_record["submission_id"] = "DEV-REJECTED-99"
    ok_rejected = await upload_to_google_drive(
        submission_id="DEV-REJECTED-99",
        record=rejected_record,
        images=sample_images,
    )
    assert ok_rejected is False

    # Test dev submission images and VN-DRIVE-TEST-01 are skipped from local deliverables mirror
    dev_sub_id = "DEV-DRIVE-TEST-01"
    dev_record = dict(sample_record)
    dev_record["submission_id"] = dev_sub_id
    ok_dev = await upload_to_google_drive(
        submission_id=dev_sub_id,
        record=dev_record,
        images=sample_images,
    )
    assert ok_dev is False
    assert not os.path.exists(os.path.join(LOCAL_IMAGES_DIR, dev_sub_id))

    vn_test_id = "VN-DRIVE-TEST-01"
    vn_test_rec = dict(sample_record)
    vn_test_rec["submission_id"] = vn_test_id
    ok_vn_test = await upload_to_google_drive(
        submission_id=vn_test_id,
        record=vn_test_rec,
        images=sample_images,
    )
    assert ok_vn_test is False
    assert not os.path.exists(os.path.join(LOCAL_IMAGES_DIR, vn_test_id))
    try:
        ok = await upload_to_google_drive(
            submission_id=sub_id,
            record=sample_record,
            images=sample_images,
        )
        assert ok is True

        # 1. Check subfolder in Images/<form_id>/submissions for production ID
        sub_folder = os.path.join(LOCAL_IMAGES_DIR, "income_survey", "submissions", sub_id)
        assert os.path.exists(sub_folder)
        assert os.path.exists(os.path.join(sub_folder, "slot_0_app_profile.webp"))
        assert os.path.exists(os.path.join(sub_folder, "slot_1_vehicle.webp"))

        # 2. Check row in Master CSV
        assert os.path.exists(MASTER_CSV_FILE)
        with open(MASTER_CSV_FILE, "r", encoding="utf-8-sig") as f:
            content = f.read()
        assert "VF e34" in content
        assert "1500000" in content
        assert "1200000" in content
        assert "21.0285" in content
        # Ensure excluded PII fields are NOT in CSV row; submission id is now an explicit column
        assert "0988555666" not in content
        assert "Phan Văn C" not in content
        assert "CTV001_Dũng_HN" not in content
        assert "VN-REAL-8F2A1C90" in content
    finally:
        sub_folder = os.path.join(LOCAL_IMAGES_DIR, "income_survey", "submissions", sub_id)
        if os.path.exists(sub_folder):
            import shutil
            shutil.rmtree(sub_folder, ignore_errors=True)

@pytest.mark.asyncio
async def test_google_drive_delivery_5_weeks_and_membership():
    """Tests export of 5-week income statistics and profile membership type while excluding PII."""
    sub_id = "VN-5WEEK-OCT2026"
    record = {
        "submission_id": sub_id,
        "form_id": "income_survey",
        "status": "APPROVED",
        "driver_phone": "0912345678",
        "driver_name": "Nguyễn Văn Test",
        "collaborator_id": "CTV002_SG",
        "membership_type": "Bạch Kim",
        "form_data": {
            "vehicle_type": "Ô tô",
            "car_model": "VF 8",
            "gsm_duration": "Từ 1 – 2 năm",
            "gsm_contract_type": "Xe Taxi xanh GSM",
            "daily_trips": "28",
            "monthly_income_range": "Trên 20 triệu VNĐ",
            "gsm_incentive_policy": ["Có áp dụng chỉ tiêu", "Có chương trình thưởng"],
        },
        "weekly_incomes": {
            "week_1": {"gross_revenue": 3100000, "net_income": 2232000, "total_trips": 24},
            "week_2": {"gross_revenue": 3250000, "net_income": 2340000, "total_trips": 26},
            "week_3": {"gross_revenue": 2980000, "net_income": 2145600, "total_trips": 22},
            "week_4": {"gross_revenue": 3400000, "net_income": 2448000, "total_trips": 27},
            "week_5": {"gross_revenue": 3600000, "net_income": 2592000, "total_trips": 30},
        },
        "total_gross_revenue": 16330000,
        "total_net_income": 11757600,
        "total_trips": 129,
        "ocr_data": {
            "confidence_score": 0.99,
            "is_legible": True,
            "detected_app": "XANH_SM",
        },
        "gps_latitude": 10.7769,
        "gps_longitude": 106.7009,
        "submitted_at": "2026-11-08T10:00:00Z",
    }
    sample_imgs = [{"filename": "slot_0.webp", "bytes": b"img0"}]
    try:
        ok = await upload_to_google_drive(submission_id=sub_id, record=record, images=sample_imgs)
        assert ok is True
        assert os.path.exists(MASTER_CSV_FILE)
        with open(MASTER_CSV_FILE, "r", encoding="utf-8-sig") as f:
            csv_text = f.read()

        # Verify membership type and 5-week figures
        assert "Hạng Hội Viên" in csv_text
        assert "Bạch Kim" in csv_text
        assert "Doanh Thu Tuần 1 (28/09 - 04/10)" in csv_text
        assert "Doanh Thu Tuần 5 (26/10 - 01/11)" in csv_text
        assert "Tổng Doanh Thu 5 Tuần" in csv_text
        assert "3100000" in csv_text
        assert "3600000" in csv_text
        assert "16330000" in csv_text
        assert "11757600" in csv_text
        assert "129" in csv_text
        assert "VF 8" in csv_text

        # Verify excluded fields are absent
        assert "0912345678" not in csv_text
        assert "Nguyễn Văn Test" not in csv_text
        assert "CTV002_SG" not in csv_text
        assert "VN-5WEEK-OCT2026" in csv_text
        # Verify status and consent are omitted
        assert "Trạng Thái" not in csv_text
        assert "APPROVED" not in csv_text
        assert "Nghị Định 13" not in csv_text
        assert "Tôi đồng ý" not in csv_text
    finally:
        shutil.rmtree(os.path.join(LOCAL_IMAGES_DIR, "income_survey", "submissions", sub_id), ignore_errors=True)

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

def test_camera_badge_no_can_chinh_prefix_and_empty_tooltips():
    """Ensures 'Căn chỉnh: ' prefix is removed and empty tooltips are not rendered."""
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)
    assert "Căn chỉnh: " not in html

    # Check image slots in DEFAULT_TRUST_BRIDGE_SCHEMA
    q = next(item for item in DEFAULT_TRUST_BRIDGE_SCHEMA.questions if item.id == "camera_receipt")
    assert q.image_slots is not None
    for slot in q.image_slots:
        assert slot.get("tooltip", "") == "", f"Slot {slot.get('slot')} tooltip should be empty"


@pytest.mark.asyncio
async def test_google_drive_client_mock_upload(monkeypatch):
    """Tests Google Drive client auth flow and sync of data sheet & image folder (no individual survey upload)."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
    from services.google_drive_delivery import GoogleDriveClient, upload_to_google_drive, sync_all_approved_to_drive
    import httpx

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode("utf-8")

    fake_sa = {
        "client_email": "test-service-account@test-proj.iam.gserviceaccount.com",
        "private_key": pem,
        "token_uri": "https://oauth2.googleapis.com/token",
    }

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if "oauth2.googleapis.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "fake_token_123", "expires_in": 3600})
        elif "/drive/v3/files" in request.url.path and request.method == "GET":
            return httpx.Response(200, json={"files": []})
        elif "/drive/v3/files" in request.url.path and request.method == "POST":
            return httpx.Response(200, json={"id": "folder_123", "name": "DEV-TEST-GDRIVE"})
        elif "/upload/drive/v3/files" in request.url.path:
            return httpx.Response(200, json={"id": "file_uploaded_999"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    # Monkeypatch AsyncClient in google_drive_delivery
    orig_init = httpx.AsyncClient.__init__

    def mock_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", mock_init)
    monkeypatch.setattr("services.google_drive_delivery.get_google_service_account_info", lambda: fake_sa)

    # 1. Individual survey submission records locally but does NOT call remote Google Drive endpoints
    ok = await upload_to_google_drive(
        submission_id="VN-GDRIVE-API-REAL",
        record={"submission_id": "VN-GDRIVE-API-REAL", "status": "APPROVED", "gross_revenue": 1200000},
        images=[{"filename": "slot_0.webp", "bytes": b"mock_img_0"}],
    )
    assert ok is True
    assert len(calls) == 0, "Individual survey submission must not trigger remote Google Drive API calls"

    # 2. Only scheduled / on-demand sync pushes data sheet and image folder
    sync_ok = await sync_all_approved_to_drive()
    assert sync_ok is True
    assert "/token" in calls[0]
    assert any("/drive/v3/files" in c for c in calls)

@pytest.mark.asyncio
async def test_oauth2_token_refresh():
    """Tests direct OAuth2 token refresh with httpx."""
    from services.google_drive_delivery import refresh_google_oauth_token
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        assert "oauth2.googleapis.com" in str(request.url)
        content = request.content.decode("utf-8")
        assert "grant_type=refresh_token" in content
        assert "refresh_token=test_refresh_123" in content
        assert "client_id=test_client_id" in content
        return httpx.Response(200, json={"access_token": "refreshed_ya29_token_abc", "expires_in": 3600})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        token = await refresh_google_oauth_token(
            refresh_token="test_refresh_123",
            client_id="test_client_id",
            client_secret="test_client_secret",
            client=client,
        )
        assert token == "refreshed_ya29_token_abc"


@pytest.mark.asyncio
async def test_google_drive_client_oauth2_preference(monkeypatch):
    """Tests that GoogleDriveClient.get_token() prioritizes OAuth2 over Service Account."""
    from services.google_drive_delivery import GoogleDriveClient
    import httpx

    fake_oauth = {
        "refresh_token": "oauth_rf_token",
        "client_id": "oauth_client_id",
        "client_secret": "oauth_secret",
    }
    fake_sa = {
        "client_email": "sa@fake.iam.gserviceaccount.com",
        "private_key": "fake_key",
    }

    calls = []

    def mock_refresh(r_token, c_id, c_sec, client=None):
        calls.append("oauth_refresh")
        async def _res():
            return "access_from_oauth"
        return _res()

    def mock_sa(sa_info, client=None):
        calls.append("sa_token")
        async def _res():
            return "access_from_sa"
        return _res()

    monkeypatch.setattr("services.google_drive_delivery.refresh_google_oauth_token", mock_refresh)
    monkeypatch.setattr("services.google_drive_delivery.get_google_access_token", mock_sa)

    client = GoogleDriveClient(sa_info=fake_sa, oauth_creds=fake_oauth)
    token = await client.get_token()
    assert token == "access_from_oauth"
    assert calls == ["oauth_refresh"], "OAuth refresh must be called first, bypassing service account"


def test_google_drive_oauth_token_json_parsing(tmp_path, monkeypatch):
    """Tests reading OAuth credentials from token.json file."""
    from services.google_drive_delivery import get_google_oauth_credentials
    import json

    token_json_path = tmp_path / "token.json"
    token_data = {
        "account": "admin@trustbridgevn.com",
        "client_id": "cid_json_123",
        "client_secret": "csec_json_456",
        "refresh_token": "rtoken_json_789",
    }
    token_json_path.write_text(json.dumps(token_data))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOOGLE_DRIVE_REFRESH_TOKEN", "")
    monkeypatch.setenv("GOOGLE_DRIVE_CLIENT_ID", "")
    monkeypatch.setenv("GOOGLE_DRIVE_CLIENT_SECRET", "")
    from config import settings
    monkeypatch.setattr(settings, "GOOGLE_DRIVE_REFRESH_TOKEN", "")
    monkeypatch.setattr(settings, "GOOGLE_DRIVE_CLIENT_ID", "")
    monkeypatch.setattr(settings, "GOOGLE_DRIVE_CLIENT_SECRET", "")
    creds = get_google_oauth_credentials()
    assert creds is not None
    assert creds["refresh_token"] == "rtoken_json_789"
    assert creds["client_id"] == "cid_json_123"
    assert creds["client_secret"] == "csec_json_456"


@pytest.mark.asyncio
async def test_r2_archive_sheet_fallback(tmp_path, monkeypatch):
    """Tests R2StorageClient.archive_sheet fallback to data/archives/sheets."""
    from services.storage_r2 import R2StorageClient
    client = R2StorageClient()
    client.is_configured = False
    monkeypatch.chdir(tmp_path)

    csv_data = b"col1,col2\nval1,val2\n"
    archive_name = "Master_Survey_Deliverables_20261108_120000.csv"
    saved_path = await client.archive_sheet(archive_name, csv_data)

    expected_path = os.path.join(os.getcwd(), "data", "archives", "sheets", archive_name)
    assert saved_path == expected_path
    assert os.path.exists(expected_path)
    with open(expected_path, "rb") as f:
        assert f.read() == csv_data


@pytest.mark.asyncio
async def test_google_drive_cleanup_dev_folders():
    """Tests GoogleDriveClient.cleanup_dev_folders_from_drive permanently deletes DEV-* and TEST-* folders."""
    from services.google_drive_delivery import GoogleDriveClient
    import httpx

    deleted_file_ids = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "GET" and "/drive/v3/files" in url_str:
            if "name+%3D+%27Images%27" in url_str or "name = 'Images'" in url_str:
                return httpx.Response(200, json={"files": []})
            if "Master_Survey_Deliverables.csv" in url_str or "submission_" in url_str:
                return httpx.Response(200, json={"files": []})
            files = [
                {"id": "folder_dev_1", "name": "DEV-A1B2C3D4"},
                {"id": "folder_test_1", "name": "TEST-SUB-99"},
                {"id": "folder_prod_1", "name": "VN-REAL-8888"},
                {"id": "folder_mock_1", "name": "MOCK-DEV-001"},
            ]
            return httpx.Response(200, json={"files": files})
        elif request.method == "DELETE" and "/drive/v3/files/" in url_str:
            fid = url_str.split("/drive/v3/files/")[1]
            deleted_file_ids.append(fid)
            return httpx.Response(204)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        gdrive = GoogleDriveClient(root_folder_id="root_folder_123")
        deleted_count = await gdrive.cleanup_dev_folders_from_drive(client, "dummy_token")

    assert deleted_count == 3
    assert "folder_dev_1" in deleted_file_ids
    assert "folder_test_1" in deleted_file_ids
    assert "folder_mock_1" in deleted_file_ids
    assert "folder_prod_1" not in deleted_file_ids

@pytest.mark.asyncio
async def test_google_drive_cleanup_trashes_duplicate_root_folders():
    """Duplicate root folders keep the newest copy; obsolete test folders are trashed/deleted."""
    from services.google_drive_delivery import GoogleDriveClient
    import httpx

    deleted_file_ids = []
    trashed_file_ids = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "GET" and "/drive/v3/files" in url_str:
            if "name = 'Images'" in url_str or "name+%3D+%27Images%27" in url_str:
                return httpx.Response(200, json={"files": []})
            if "Master_Survey_Deliverables.csv" in url_str or "submission_" in url_str:
                return httpx.Response(200, json={"files": []})
            return httpx.Response(200, json={"files": [
                {"id": "internal-old", "name": "Internal", "mimeType": "application/vnd.google-apps.folder", "modifiedTime": "2026-11-18T02:47:14.249Z"},
                {"id": "internal-new", "name": "Internal", "mimeType": "application/vnd.google-apps.folder", "modifiedTime": "2026-11-20T02:18:12.500Z"},
                {"id": "income-test", "name": "income_survey_test", "mimeType": "application/vnd.google-apps.folder", "modifiedTime": "2026-11-18T09:18:37.331Z"},
                {"id": "active-form", "name": "TB_GSM_2026_Nov_Test", "mimeType": "application/vnd.google-apps.folder", "modifiedTime": "2026-11-20T02:13:14.916Z"},
            ]})
        if request.method == "DELETE" and "/drive/v3/files/" in url_str:
            deleted_file_ids.append(url_str.split("/drive/v3/files/")[1])
            return httpx.Response(204)
        if request.method == "PATCH" and "/drive/v3/files/" in url_str:
            trashed_file_ids.append(url_str.split("/drive/v3/files/")[1])
            return httpx.Response(200, json={"id": url_str.rsplit("/", 1)[-1], "trashed": True})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        gdrive = GoogleDriveClient(root_folder_id="root_folder_123")
        cleaned = await gdrive.cleanup_drive_clutter(
            client, "dummy_token", {"TB_GSM_2026_Nov_Test"}
        )

    assert cleaned == 2
    assert "income-test" in deleted_file_ids
    assert trashed_file_ids == ["internal-old"]
    assert "internal-new" not in trashed_file_ids
    assert "active-form" not in deleted_file_ids


@pytest.mark.asyncio
async def test_upload_file_patch_when_existing():
    """Tests that upload_file performs PATCH with media when file already exists."""
    from services.google_drive_delivery import GoogleDriveClient
    import httpx

    patch_called = []
    post_called = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "GET" and "/drive/v3/files" in url_str:
            # find_file_or_folder returns an existing file id
            return httpx.Response(200, json={"files": [{"id": "existing_csv_file_456", "name": "Master_Survey_Deliverables.csv"}]})
        elif request.method == "PATCH" and "/upload/drive/v3/files/existing_csv_file_456" in url_str:
            patch_called.append({
                "url": url_str,
                "headers": dict(request.headers),
                "body": request.content,
            })
            return httpx.Response(200, json={"id": "existing_csv_file_456"})
        elif request.method == "POST":
            post_called.append(url_str)
            return httpx.Response(200, json={"id": "new_file_789"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        gdrive = GoogleDriveClient()
        fid = await gdrive.upload_file(
            client=client,
            token="test_bearer_token",
            filename="Master_Survey_Deliverables.csv",
            content_bytes=b"header1,header2\nval1,val2\n",
            mime_type="text/csv; charset=UTF-8",
            parent_id="parent_folder_123",
        )

    assert fid == "existing_csv_file_456"
    assert len(patch_called) == 1
    assert len(post_called) == 0
    assert "uploadType=media" in patch_called[0]["url"]
    assert patch_called[0]["body"] == b"header1,header2\nval1,val2\n"


@pytest.mark.asyncio
async def test_execute_delivery_no_summary_csv(monkeypatch):
    """execute_delivery uploads images plus per-form CSV/Sheet, never submission_*_summary.csv."""
    from services.google_drive_delivery import GoogleDriveClient
    import httpx

    uploaded_files = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "oauth2.googleapis.com" in url_str:
            return httpx.Response(200, json={"access_token": "token_abc"})
        elif request.method == "POST" and "/drive/v3/files" in url_str and "upload" not in url_str:
            return httpx.Response(200, json={"id": "sub_folder_id_111"})
        elif request.method == "GET" and "/drive/v3/files" in url_str:
            return httpx.Response(200, json={"files": []})
        elif "/upload/drive/v3/files" in url_str:
            body = request.content.decode("latin1", errors="ignore")
            uploaded_files.append(body)
            return httpx.Response(200, json={"id": "uploaded_id_222"})
        return httpx.Response(404)
    transport = httpx.MockTransport(handler)
    orig_init = httpx.AsyncClient.__init__

    def mock_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", mock_init)

    gdrive = GoogleDriveClient()
    # Mock token
    async def mock_token(c=None):
        return "token_abc"
    monkeypatch.setattr(gdrive, "get_token", mock_token)
    success = await gdrive.execute_delivery(
        submission_id="VN-LIVE-12345",
        record={"submission_id": "VN-LIVE-12345", "status": "APPROVED", "form_id": "income_survey"},
        images=[{"filename": "slot_0.webp", "bytes": b"slot0_bytes"}],
    )
    assert success is True
    assert len(uploaded_files) == 3
    assert any("slot_0.webp" in body for body in uploaded_files)
    assert any('filename="income_survey.csv"' in body for body in uploaded_files)
    assert any("application/vnd.google-apps.spreadsheet" in body for body in uploaded_files)
    assert not any("summary.csv" in body for body in uploaded_files)

def test_manual_drive_sync_endpoint(monkeypatch):
    """Tests POST /api/v1/admin/sync-drive endpoint triggers sync background task."""
    from config import settings
    client = TestClient(app)
    # Unauthorized without credentials
    unauth_resp = client.post("/api/v1/admin/sync-drive")
    assert unauth_resp.status_code == 401

    # Mock has_google_credentials
    monkeypatch.setattr("main.has_google_credentials", lambda: True)
    sync_called = []
    async def mock_sync(db_pool=None):
        sync_called.append(True)
        return True
    monkeypatch.setattr("main.sync_all_approved_to_drive", mock_sync)

    resp = client.post("/api/v1/admin/sync-drive", auth=(settings.ADMIN_USERNAME, settings.ADMIN_PASSWORD))
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "QUEUED"
    assert "Đã kích hoạt đồng bộ Google Drive" in data["message"]
    assert len(sync_called) == 1
