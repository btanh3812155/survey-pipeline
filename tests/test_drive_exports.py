"""Drive export configuration, filtering, CSV, and deduplicated image shortcuts."""
import asyncio
import csv
import hashlib
import io
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from core import drive_exports
from services import submission_media
from services.google_drive_delivery import GoogleDriveClient
from services.drive_cleanup import purge_form_folders
from tests.postgres_harness import SCHEMA_PATH, apply_schema, resolve_test_database_url

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database():
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("disposable PostgreSQL is required")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL")
    dsn = resolve_test_database_url()
    name = "exports_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
        apply_schema(conn)
        try:
            yield make_conninfo(dsn, options=f"-c search_path={name},public")
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest_asyncio.fixture
async def store(database):
    async with AsyncConnectionPool(database, open=False, min_size=1, max_size=4) as pool:
        assert await drive_exports.ensure_drive_exports(pool)
        with psycopg.connect(database, autocommit=True, row_factory=dict_row) as conn:
            yield pool, conn


def _insert(conn, submission_id, *, form_id="TB_TEST", status="APPROVED", vehicle="CAR",
            created_at=datetime(2026, 11, 1, tzinfo=timezone.utc), name="Driver", phone="0909000111"):
    conn.execute(
        "INSERT INTO survey_submissions(submission_id,form_id,status,vehicle_type,created_at,"
        "driver_name,driver_phone,total_gross_revenue,total_net_income,total_trips,form_data,media_manifest) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,1000,800,4,%s::jsonb,'[]'::jsonb)",
        (submission_id, form_id, status, vehicle, created_at, name, phone,
         json.dumps({"answer": submission_id})),
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_settings_and_client_configuration(store):
    pool, _conn = store
    settings = await drive_exports.save_settings(
        pool, "tester", master_folder_name="Master Export", client_folder_name="Client Exports",
        internal_folder_name="Internal Exports", images_folder_name="Images Master",
    )
    assert settings["master_folder_name"] == "Master Export"
    client = await drive_exports.save_client(pool, {
        "slug": "acme",
        "display_name": "ACME Client",
        "kind": "client",
        "folder_name": "ACME",
        "enabled": True,
        "date_from": "2026-11-01",
        "date_to": "2026-11-30",
        "form_ids": ["TB_TEST"],
        "fields": ["submission_id", "created_at", "vehicle_type"],
        "statuses": ["APPROVED"],
        "vehicle_types": ["CAR"],
        "include_images": True,
    }, "tester")
    assert client["fields"] == ["submission_id", "created_at", "vehicle_type"]
    assert client["date_from"] == date(2026, 11, 1)
    listed = await drive_exports.list_clients(pool)
    assert [row["slug"] for row in listed] == ["acme"]
    with pytest.raises(drive_exports.DriveExportError):
        await drive_exports.save_client(pool, {
            "slug": "internal", "display_name": "Internal", "kind": "internal",
            "category": "", "fields": ["submission_id"],
        }, "tester")
    assert await drive_exports.delete_client(pool, "acme", "tester")
    assert await drive_exports.list_clients(pool) == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_row_filters_and_csv(store):
    pool, conn = store
    _insert(conn, "VN-CAR-1", created_at=datetime(2026, 11, 10, tzinfo=timezone.utc))
    _insert(conn, "VN-CAR-OLD", created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    _insert(conn, "VN-BIKE-1", vehicle="BIKE", created_at=datetime(2026, 11, 11, tzinfo=timezone.utc))
    _insert(conn, "VN-REJECTED", status="REJECTED", created_at=datetime(2026, 11, 12, tzinfo=timezone.utc))
    _insert(conn, "DEV-CAR-1", created_at=datetime(2026, 11, 13, tzinfo=timezone.utc))
    client = {
        "statuses": ["APPROVED"], "form_ids": ["TB_TEST"], "vehicle_types": ["CAR"],
        "date_from": date(2026, 11, 1), "date_to": date(2026, 11, 30),
    }
    rows = await drive_exports.select_rows(pool, client)
    assert [row["submission_id"] for row in rows] == ["VN-CAR-1"]
    payload = drive_exports.build_export_csv(rows, ["submission_id", "vehicle_type", "created_at"])
    parsed = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
    assert parsed[0] == ["Submission ID", "Vehicle type", "Submitted at"]
    assert parsed[1][0] == "VN-CAR-1"
    assert parsed[1][1] == "CAR"
    assert "2026-11-10" in parsed[1][2]


class FakeDrive:
    def __init__(self):
        self.root_folder_id = "root"
        self.folders = {}
        self.files = {}
        self.uploads = []
        self.shortcuts = []
        self.sheets = []
        self.deleted_files = []

    async def list_children(self, http, token, folder_id):
        folders = [
            {"id": child_id, "name": name, "mimeType": "application/vnd.google-apps.folder"}
            for (parent, name), child_id in self.folders.items()
            if parent == folder_id
        ]
        files = [
            {"id": file_id, "name": name, "mimeType": "image/webp"}
            for (parent, name), file_id in self.files.items()
            if parent == folder_id
        ]
        return folders + files

    async def delete_file(self, http, token, file_id):
        for key, value in list(self.folders.items()):
            if value == file_id:
                del self.folders[key]
                self.deleted_files.append(file_id)
                return True
        for key, value in list(self.files.items()):
            if value == file_id:
                del self.files[key]
                self.deleted_files.append(file_id)
                return True
        return False

    async def get_token(self, http):
        return "token"

    async def get_or_create_folder(self, http, token, name, parent):
        key = (parent, name)
        if key not in self.folders:
            self.folders[key] = f"folder:{parent}/{name}"
        return self.folders[key]

    async def find_file_or_folder(self, http, token, name, parent, mime_type=None):
        return self.files.get((parent, name)) or self.folders.get((parent, name))

    async def upload_file(self, http, token, name, payload, mime, parent):
        key = (parent, name)
        self.files[key] = f"file:{parent}/{name}"
        self.uploads.append((name, parent))
        return self.files[key]

    async def create_shortcut(self, http, token, name, target_id, parent):
        key = (parent, name)
        self.files[key] = f"shortcut:{parent}/{name}"
        self.shortcuts.append((name, target_id, parent))
        return self.files[key]

    async def import_csv_as_sheet(self, http, token, name, csv_bytes, parent):
        self.files[(parent, name)] = f"sheet:{parent}/{name}"
        self.sheets.append((name, parent, csv_bytes))
        return self.files[(parent, name)]

    async def update_sheet_values(self, http, token, sheet_id, rows, value_input_option="RAW"):
        return True


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_export_uploads_images_once_and_shortcuts_each_client(store, monkeypatch, tmp_path):
    pool, conn = store
    media_root = tmp_path / "submission_photos"
    media_root.mkdir()
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(media_root))
    sid = "VN-IMG-1"
    image_dir = media_root / sid
    image_dir.mkdir()
    Image.new("RGB", (12, 12), (200, 20, 20)).save(image_dir / "slot_1.png")
    _insert(conn, sid, created_at=datetime(2026, 11, 10, tzinfo=timezone.utc))
    for slug, name in (("client-a", "Client A"), ("client-b", "Client B")):
        await drive_exports.save_client(pool, {
            "slug": slug, "display_name": name, "kind": "client", "folder_name": name,
            "enabled": True, "form_ids": ["TB_TEST"], "fields": ["submission_id", "vehicle_type"],
            "statuses": ["APPROVED"], "vehicle_types": ["CAR"], "include_images": True,
        }, "tester")
    fake = FakeDrive()
    master_sub = f"folder:folder:folder:folder:root/Master Export/Images Master/TB_TEST/{sid}"
    client_a_images = "folder:folder:folder:folder:folder:root/Master Export/Client Exports/Client A/TB_TEST/Images"
    client_a_sub = f"folder:{client_a_images}/{sid}"
    fake.files[(master_sub, "slot_1_oldversion.webp")] = "stale-master"
    fake.files[(client_a_sub, "slot_1_oldversion.webp")] = "stale-client"
    fake.folders[(client_a_images, "VN-OLD-SUB")] = "stale-submission-folder"
    report = await drive_exports.run_drive_exports(
        pool, client=fake, http_client=object(), token="token", root_folder_id="root"
    )
    assert report["status"] == "ok", report
    assert report["images_uploaded"] == 1
    assert report["images_pruned"] == 2
    assert report["submission_folders_pruned"] == 1
    assert set(fake.deleted_files) == {
        "stale-master", "stale-client", "stale-submission-folder",
    }
    assert report["shortcuts_created"] == 2
    assert len(fake.uploads) == 1
    assert len(fake.shortcuts) == 2
    target_ids = {target for _name, target, _parent in fake.shortcuts}
    assert len(target_ids) == 1
    assert all(name.startswith("slot_1_") and name.endswith(".webp") for name, _target, _parent in fake.shortcuts)
    assert all("/Images/" in parent for _name, _target, parent in fake.shortcuts)
    assert len(fake.sheets) == 2
    client_a_sheet = next(item for item in fake.sheets if "Client A" in item[1])
    assert sid.encode() in client_a_sheet[2]
    settings = await drive_exports.get_settings(pool)
    assert settings["last_report"]["status"] == "ok"


@pytest.mark.asyncio
async def test_purge_form_folders_removes_current_and_legacy_layouts():
    fake = FakeDrive()
    settings = {
        "master_folder_name": "Master Export",
        "client_folder_name": "Client Exports",
        "internal_folder_name": "Internal Exports",
        "images_folder_name": "Images Master",
    }
    fake.folders.update({
        ("root", "Master Export"): "master",
        ("master", "Client Exports"): "clients",
        ("master", "Internal Exports"): "internals",
        ("master", "Images Master"): "images",
        ("clients", "Client A"): "client-a",
        ("client-a", "KEEP_FORM"): "keep-form",
        ("client-a", "OLD_FORM"): "old-client-form",
        ("old-client-form", "Images"): "old-client-images",
        ("old-client-images", "SUB"): "old-client-sub",
        ("internals", "Category"): "internal-cat",
        ("internal-cat", "OLD_FORM"): "old-internal-form",
        ("images", "OLD_FORM"): "old-master-form",
        ("old-master-form", "SUB"): "old-master-sub",
        ("root", "OLD_FORM"): "old-legacy-form",
        ("root", "Internal"): "legacy-internal",
        ("legacy-internal", "OLD_FORM"): "old-legacy-internal-form",
    })
    fake.files[("old-client-sub", "photo.webp")] = "old-client-file"
    fake.files[("old-master-sub", "photo.webp")] = "old-master-file"

    deleted = await purge_form_folders(
        fake, object(), "token", "root", settings, "OLD_FORM"
    )

    assert deleted >= 10
    assert {
        "old-client-form", "old-client-images", "old-client-sub", "old-client-file",
        "old-internal-form", "old-master-form", "old-master-sub", "old-master-file",
        "old-legacy-form", "old-legacy-internal-form",
    }.issubset(set(fake.deleted_files))
    assert "keep-form" not in fake.deleted_files
    assert "client-a" not in fake.deleted_files
    assert "master" not in fake.deleted_files


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _ShortcutHTTP:
    def __init__(self):
        self.captured = None

    async def get(self, *args, **kwargs):
        return _Resp({"files": []})

    async def post(self, *args, **kwargs):
        self.captured = kwargs.get("json")
        return _Resp({"id": "shortcut-1"})


@pytest.mark.asyncio
async def test_shortcut_uses_drive_shortcut_metadata():
    http = _ShortcutHTTP()
    drive = GoogleDriveClient(root_folder_id="root")
    shortcut_id = await drive.create_shortcut(http, "token", "slot.webp", "master-file", "client-folder")
    assert shortcut_id == "shortcut-1"
    assert http.captured["mimeType"] == "application/vnd.google-apps.shortcut"
    assert http.captured["shortcutDetails"] == {"targetId": "master-file"}
    assert http.captured["parents"] == ["client-folder"]


@pytest.mark.postgres
def test_migration_009_creates_export_tables(database):
    migrations = ROOT / "scripts" / "migrations"
    previous = hashlib.sha256((migrations / "008_admin_module_registry.sql").read_bytes()).hexdigest()
    path = migrations / "009_drive_exports.sql"
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    migration = path.read_text().replace(":'migration_008_sha256'", f"'{previous}'").replace(":'migration_sha256'", f"'{checksum}'")
    schema = "export_migration_" + uuid4().hex
    with psycopg.connect(database, autocommit=True) as root:
        root.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            dsn = make_conninfo(database, options=f"-c search_path={schema}")
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, checksum TEXT NOT NULL)")
                conn.execute("INSERT INTO schema_migrations VALUES ('008',%s)", (previous,))
                conn.execute(migration)
                assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [("008",), ("009",)]
                assert conn.execute("SELECT count(*) FROM drive_export_settings").fetchone() == (1,)
                conn.execute(migration)
                assert conn.execute("SELECT count(*) FROM drive_export_settings").fetchone() == (1,)
        finally:
            root.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_exports_page_renders(store, monkeypatch):
    from routers import admin_exports as routes
    from core.state import verify_admin_auth

    pool, _conn = store
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[verify_admin_auth] = lambda: "admin"
    monkeypatch.setattr(routes, "get_db_pool", lambda: pool)
    with TestClient(app) as client:
        response = client.get("/admin/exports")
    assert response.status_code == 200
    assert "Google Drive Exports" in response.text
    assert "Create configuration" in response.text
    assert "Images Master" in response.text
