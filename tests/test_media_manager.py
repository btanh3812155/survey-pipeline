# -*- coding: utf-8 -*-
import io
import base64
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from config import settings
from main import app
from routers import admin_media

client = TestClient(app)
@pytest.fixture
def auth_header(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ADMIN_USERNAME", "media-test-admin")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "media-test-password")
    monkeypatch.setattr(admin_media, "MEDIA_STORAGE_DIR", str(tmp_path))
    credentials = base64.b64encode(b"media-test-admin:media-test-password").decode("ascii")
    return {"Authorization": f"Basic {credentials}"}


def test_media_manager_html_and_dir_tree(auth_header):
    resp = client.get("/admin/media", headers=auth_header)
    assert resp.status_code == 200
    assert "Quản Lý Media" in resp.text
    assert "dirTreeRoot" in resp.text
    assert "uploadFolderSelect" in resp.text
    assert "propFolderSelect" in resp.text
    assert "saveFileChanges" in resp.text
    # Verify emojis replaced with SVGs
    assert "📁" not in resp.text
    assert "⊞" not in resp.text
    assert "☰" not in resp.text
    assert "⚡" not in resp.text
    assert "🗑" not in resp.text


def test_media_manager_api_crud_lifecycle(auth_header):
    # 1. Create a dummy image buffer
    img_buf = io.BytesIO()
    Image.new("RGB", (300, 200), (200, 100, 50)).save(img_buf, format="JPEG")
    img_bytes = img_buf.getvalue()

    # 2. Upload into nested folder "guide/tests"
    resp_upload = client.post(
        "/api/v1/media/upload",
        headers=auth_header,
        data={"folder": "guide/tests"},
        files={"file": ("test_banner.jpg", img_bytes, "image/jpeg")}
    )
    assert resp_upload.status_code == 200
    data = resp_upload.json()
    assert data["status"] == "success"
    item = data["item"]
    assert item["name"] == "test_banner.webp"
    assert item["folder"] == "guide/tests"
    file_id = item["id"]

    # 3. List media API must discover the new folder and file
    resp_list = client.get("/api/v1/media", headers=auth_header)
    assert resp_list.status_code == 200
    list_data = resp_list.json()
    assert "guide/tests" in list_data["folders"]
    assert any(f["id"] == file_id for f in list_data["files"])

    # 4. Rename and move to "forms/banners"
    resp_rename = client.patch(
        "/api/v1/media/rename",
        headers=auth_header,
        json={
            "id": file_id,
            "new_name": "renamed_banner.webp",
            "new_folder": "forms/banners"
        }
    )
    assert resp_rename.status_code == 200
    rename_data = resp_rename.json()
    assert rename_data["status"] == "success"
    renamed_item = rename_data["item"]
    assert renamed_item["name"] == "renamed_banner.webp"
    assert renamed_item["folder"] == "forms/banners"
    new_id = renamed_item["id"]

    # 5. Delete the file
    resp_del = client.delete(f"/api/v1/media?id={new_id}", headers=auth_header)
    assert resp_del.status_code == 200
    assert resp_del.json()["status"] == "success"

    # 6. Path traversal must be blocked
    resp_bad = client.delete("/api/v1/media?id=../../etc/passwd", headers=auth_header)
    assert resp_bad.status_code == 400
