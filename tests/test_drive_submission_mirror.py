# -*- coding: utf-8 -*-
"""Status-aware Drive mirror: customer APPROVED-only, internal every status."""

import re
from collections import defaultdict

import pytest

from services.drive_submission_mirror import (
    MANIFEST_HEADERS,
    customer_publish_enabled,
    manifest_rows,
    mirror_submissions_to_drive,
    plan_submission_mirror,
)


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttp:
    """Minimal Drive listing API used by _list_remote_files."""

    def __init__(self):
        self.folder_files = defaultdict(set)

    async def get(self, url, params=None, headers=None):
        query = (params or {}).get("q", "")
        match = re.search(r"'([^']+)' in parents", query)
        folder_id = match.group(1) if match else ""
        return FakeResponse({"files": [{"name": name} for name in sorted(self.folder_files[folder_id])]})


class FakeDriveClient:
    root_folder_id = "ROOT"

    def __init__(self):
        self.folders = set()
        self.uploads = {}
        self.http = None

    async def get_or_create_folder(self, http_client, token, name, parent_id=None):
        folder_id = f"{parent_id}/{name}"
        self.folders.add(folder_id)
        return folder_id

    async def upload_file(self, http_client, token, filename, content_bytes, mime_type="", parent_id=None):
        file_id = f"file:{parent_id}/{filename}"
        self.uploads[file_id] = {"parent": parent_id, "name": filename, "bytes": content_bytes, "mime": mime_type}
        return file_id


def _record(submission_id, status="APPROVED", form_id="form_alpha", **extra):
    return {
        "submission_id": submission_id,
        "form_id": form_id,
        "status": status,
        "ctv_code": "TEST_CTV_01",
        "driver_name": "TEST_DRIVER",
        "total_gross_revenue": 1000,
        "total_net_income": 800,
        "total_trips": 10,
        **extra,
    }


def _stage_images(root, form_id, submission_id, count=1):
    folder = root / form_id / "submissions" / submission_id
    folder.mkdir(parents=True)
    for index in range(count):
        (folder / f"slot_{index}.webp").write_bytes(b"x" * (index + 1))


def _shot(tmp_path):
    root = tmp_path / "Images"
    _stage_images(root, "form_alpha", "VN-APPROVED", 2)
    _stage_images(root, "form_alpha", "VN-REJECTED", 1)
    _stage_images(root, "form_alpha", "VN-APPEAL", 1)
    _stage_images(root, "form_alpha", "VN-REVIEW", 2)
    return root


@pytest.mark.asyncio
async def test_normal_mirror_customer_approved_only_and_internal_all_statuses(tmp_path):
    images_root = _shot(tmp_path)
    records = [
        _record("VN-APPROVED", "APPROVED"),
        _record("VN-REJECTED", "REJECTED"),
        _record("VN-APPEAL", "APPEAL_PENDING"),
        _record("VN-REVIEW", "MANUAL_REVIEW"),
    ]
    client = FakeDriveClient()
    http_client = FakeHttp()
    report = await mirror_submissions_to_drive(
        records,
        client=client,
        http_client=http_client,
        token="token",
        root_folder_id="ROOT",
        images_root=str(images_root),
        publish_customer=True,
        customer_statuses=("APPROVED",),
        internal_root_segments=(),
        skip_empty=True,
    )
    form = report["forms"]["form_alpha"]
    assert set(form["customer"]) == {"VN-APPROVED"}
    assert set(form["internal"]) == {"APPROVED", "REJECTED", "APPEAL_PENDING", "MANUAL_REVIEW"}
    assert form["internal"]["APPROVED"]["VN-APPROVED"]["files"]
    assert form["internal"]["REJECTED"]["VN-REJECTED"]["files"]
    assert report["uploaded_files"] == 8  # 6 internal + 2 approved customer
    assert not any("VN-REJECTED" in folder for folder in form["customer"])


@pytest.mark.asyncio
async def test_test_data_mirror_uses_prefix_and_stays_out_of_normal_tree(tmp_path):
    images_root = tmp_path / "Images"
    _stage_images(images_root, "form_alpha", "TEST_APPROVED")
    _stage_images(images_root, "form_alpha", "VN-REAL")
    client = FakeDriveClient()
    report = await mirror_submissions_to_drive(
        [_record("TEST_APPROVED"), _record("VN-REAL")],
        client=client,
        http_client=FakeHttp(),
        token="token",
        root_folder_id="ROOT",
        images_root=str(images_root),
        publish_customer=True,
        internal_root_segments=("TEST_DATA",),
        require_test=True,
        skip_empty=True,
    )
    assert report["internal_root_folder_id"] == "ROOT/Internal/TEST_DATA"
    assert list(report["forms"]) == ["form_alpha"]
    assert list(report["forms"]["form_alpha"]["customer"]) == ["TEST_APPROVED"]
    assert not any("VN-REAL" in folder for folder in client.folders)


def test_planner_filters_test_and_real_records(tmp_path):
    plan = plan_submission_mirror(
        [_record("VN-REAL"), _record("TEST_ONLY")],
        images_root=str(tmp_path),
        internal_root_segments=("TEST_DATA",),
        publish_customer=True,
        require_test=True,
    )
    assert [item["submission_id"] for item in plan] == ["TEST_ONLY"]

    plan = plan_submission_mirror(
        [_record("VN-REAL"), _record("TEST_ONLY")],
        images_root=str(tmp_path),
        internal_root_segments=(),
        exclude_test=True,
    )
    assert [item["submission_id"] for item in plan] == ["VN-REAL"]


def test_unknown_status_and_form_are_sanitized(tmp_path):
    plan = plan_submission_mirror(
        [_record("TEST_WEIRD", form_id="../../etc", status="SUSPICIOUS NEEDS REVIEW")],
        images_root=str(tmp_path),
        internal_root_segments=("TEST_DATA",),
    )
    assert plan[0]["form_id"] == "etc"
    assert plan[0]["internal_path"][3] == "UNKNOWN"


def test_only_non_empty_image_files_are_planned(tmp_path):
    images_root = tmp_path / "Images"
    folder = images_root / "form_alpha" / "submissions" / "VN-IMAGES"
    folder.mkdir(parents=True)
    (folder / "slot_0.webp").write_bytes(b"x")
    (folder / "notes.txt").write_bytes(b"x")
    (folder / "slot_1.webp").write_bytes(b"")
    plan = plan_submission_mirror([_record("VN-IMAGES")], images_root=str(images_root))
    assert [image["name"] for image in plan[0]["images"]] == ["slot_0.webp"]


def test_manifest_contains_every_status_without_pii_columns():
    plan = plan_submission_mirror(
        [
            _record("VN-M1", status="MANUAL_REVIEW"),
            _record("VN-M2", status="REJECTED", rejection_reason="TEST_DATA: blur"),
            _record("VN-M3", status="APPROVED"),
        ],
        images_root="/nonexistent",
    )
    rows = manifest_rows(plan)
    assert [row[0] for row in rows] == ["APPROVED", "REJECTED", "MANUAL_REVIEW"]
    assert rows[1][10] == "TEST_DATA: blur"
    assert len(MANIFEST_HEADERS) == len(rows[0])
    assert "Số Điện Thoại" not in MANIFEST_HEADERS


def test_customer_publish_flag_is_opt_in():
    assert customer_publish_enabled({}) is False
    assert customer_publish_enabled({"GOOGLE_DRIVE_PUBLISH_TEST_SUBMISSIONS": "no"}) is False
    for value in ("1", "true", "TRUE", "yes", "on"):
        assert customer_publish_enabled({"GOOGLE_DRIVE_PUBLISH_TEST_SUBMISSIONS": value}) is True


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = None

    async def execute(self, *args):
        self.executed = args

    async def fetchall(self):
        return self.rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, rows):
        self.cursor = FakeCursor(rows)

    def connection(self):
        return FakeConnection(self.cursor)


@pytest.mark.asyncio
async def test_production_sync_mirrors_all_statuses_internally_only(tmp_path, monkeypatch):
    import services.drive_submission_mirror as mirror

    images_root = _shot(tmp_path)
    _stage_images(images_root, "form_alpha", "TEST_SHOULD_STAY_OUT")
    monkeypatch.setattr(mirror, "LOCAL_IMAGES_DIR", str(images_root))

    records = [
        _record("VN-APPROVED", "APPROVED"),
        _record("VN-REJECTED", "REJECTED"),
        _record("VN-APPEAL", "APPEAL_PENDING"),
        _record("VN-REVIEW", "MANUAL_REVIEW"),
        _record("TEST_SHOULD_STAY_OUT", "REJECTED"),
    ]
    client = FakeDriveClient()
    from services.google_drive_delivery import sync_internal_status_mirror

    report = await sync_internal_status_mirror(
        FakePool(records), client, FakeHttp(), "token", "ROOT"
    )
    form = report["forms"]["form_alpha"]
    assert report["publish_customer"] is False
    assert form["customer"] == {}
    assert set(form["internal"]) == {"APPROVED", "REJECTED", "APPEAL_PENDING", "MANUAL_REVIEW"}
    assert not any("TEST_SHOULD_STAY_OUT" in folder for folder in client.folders)
    assert not any("/form_alpha/submissions/VN-" in folder for folder in client.folders)
    internal_base = "ROOT/Internal/form_alpha/submissions"
    assert all(
        folder == internal_base or folder.startswith(internal_base + "/")
        for folder in client.folders
        if folder.count("/") > 2
    )


def test_raw_photo_store_is_used_when_mirror_is_empty(tmp_path):
    images_root = tmp_path / "Images"
    photo_root = tmp_path / "photos"
    photo_dir = photo_root / "VN-REJECTED"
    photo_dir.mkdir(parents=True)
    (photo_dir / "slot_0.jpg").write_bytes(b"x")

    plan = plan_submission_mirror(
        [_record("VN-REJECTED", "REJECTED")],
        images_root=str(images_root),
        photo_store_dir=str(photo_root),
    )
    assert [image["name"] for image in plan[0]["images"]] == ["slot_0.jpg"]

    _stage_images(images_root, "form_alpha", "VN-REJECTED", 1)
    plan = plan_submission_mirror(
        [_record("VN-REJECTED", "REJECTED")],
        images_root=str(images_root),
        photo_store_dir=str(photo_root),
    )
    assert [image["name"] for image in plan[0]["images"]] == ["slot_0.webp"]


@pytest.mark.asyncio
async def test_production_sync_uploads_rejected_images_from_raw_store(tmp_path, monkeypatch):
    import services.drive_submission_mirror as mirror
    import services.google_drive_delivery as delivery

    images_root = tmp_path / "Images"
    images_root.mkdir()
    photo_root = tmp_path / "photos"
    photo_dir = photo_root / "VN-REJECTED"
    photo_dir.mkdir(parents=True)
    (photo_dir / "slot_0.jpg").write_bytes(b"x")
    monkeypatch.setattr(mirror, "LOCAL_IMAGES_DIR", str(images_root))
    monkeypatch.setattr(delivery, "SUBMISSION_PHOTOS_DIR", str(photo_root))

    client = FakeDriveClient()
    report = await delivery.sync_internal_status_mirror(
        FakePool([_record("VN-REJECTED", "REJECTED")]), client, FakeHttp(), "token", "ROOT"
    )
    files = report["forms"]["form_alpha"]["internal"]["REJECTED"]["VN-REJECTED"]["files"]
    assert len(files) == 1
    # b"x" is not a decodable image, so the original .jpg name is kept.
    assert files[0]["name"] == "slot_0.jpg"
    assert files[0]["status"] == "uploaded"
