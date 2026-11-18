# -*- coding: utf-8 -*-
"""Google Drive delivery helpers."""

import pytest

from services.google_drive_delivery import _guess_mime, _list_remote_file_names


def test_drive_mime_uses_real_image_types():
    assert _guess_mime("slot_0.jpg") == "image/jpeg"
    assert _guess_mime("slot_1.png") == "image/png"
    assert _guess_mime("slot_2.webp") == "image/webp"
    assert _guess_mime("dossier.txt") == "text/plain"
    assert _guess_mime("unknown.bin") == "application/octet-stream"


@pytest.mark.asyncio
async def test_drive_folder_listing_returns_existing_names():
    class Resp:
        status_code = 200
        def json(self): return {"files": [{"name": "slot_0.jpg"}, {"name": "slot_1.jpg"}]}

    class Client:
        async def get(self, url, params=None, headers=None):
            return Resp()

    names = await _list_remote_file_names(Client(), "token", "folder-id")
    assert names == {"slot_0.jpg", "slot_1.jpg"}


def test_local_mirror_uses_form_folder(monkeypatch, tmp_path):
    import services.google_drive_delivery as gd

    monkeypatch.setattr(gd, "LOCAL_IMAGES_DIR", str(tmp_path))
    form_folder = tmp_path / "TB_GSM_2026_Nov_Test" / "submissions"
    path = gd.save_submission_images_locally(
        "VN-ABC123",
        [{"filename": "slot_0.jpg", "bytes": b"x"}],
        form_id="TB_GSM_2026_Nov_Test",
    )
    assert path == str(form_folder / "VN-ABC123")
    assert (form_folder / "VN-ABC123" / "slot_0.jpg").read_bytes() == b"x"


def test_local_form_folder_rename(monkeypatch, tmp_path):
    import services.google_drive_delivery as gd

    monkeypatch.setattr(gd, "LOCAL_IMAGES_DIR", str(tmp_path / "Images"))
    monkeypatch.setattr(gd, "LOCAL_FORMS_SHEETS_DIR", str(tmp_path / "Sheets" / "forms"))
    old = tmp_path / "Images" / "old_form"
    (old / "submissions" / "VN-ABC123").mkdir(parents=True)
    (old / "submissions" / "VN-ABC123" / "slot_0.jpg").write_bytes(b"x")
    (tmp_path / "Sheets" / "forms").mkdir(parents=True)
    (tmp_path / "Sheets" / "forms" / "old_form.csv").write_text("a,b\n", encoding="utf-8")

    gd.rename_local_form_folder("old_form", "new_form")

    assert not old.exists()
    new_file = tmp_path / "Images" / "new_form" / "submissions" / "VN-ABC123" / "slot_0.jpg"
    assert new_file.read_bytes() == b"x"
    assert not (tmp_path / "Sheets" / "forms" / "old_form.csv").exists()
    assert (tmp_path / "Sheets" / "forms" / "new_form.csv").read_text(encoding="utf-8") == "a,b\n"


def _approved_record(sub_id: str, form_id: str, submitted_at: str) -> dict:
    return {
        "submission_id": sub_id,
        "form_id": form_id,
        "status": "APPROVED",
        "form_data": {"car_model": "VF 8", "daily_trips": "28"},
        "membership_type": "Bạch Kim",
        "submitted_at": submitted_at,
    }


def test_local_form_csv_mirror_is_idempotent(monkeypatch, tmp_path):
    import csv
    import services.google_drive_delivery as gd

    master = tmp_path / "Master_Survey_Deliverables.csv"
    forms_dir = tmp_path / "Sheets" / "forms"
    monkeypatch.setattr(gd, "MASTER_CSV_FILE", str(master))
    monkeypatch.setattr(gd, "LOCAL_FORMS_SHEETS_DIR", str(forms_dir))

    record = _approved_record("VN-FORM-1", "form_alpha", "2026-11-17T09:00:00Z")
    gd.append_to_local_sheets_mirror(record)
    gd.append_to_local_sheets_mirror(record)
    # Same submission re-synced with a fresh timestamp must still not duplicate the row.
    gd.append_to_local_sheets_mirror(dict(record, submitted_at="2026-11-17T10:00:00Z"))

    form_csv = forms_dir / "form_alpha.csv"
    with form_csv.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == gd.CSV_HEADERS
    assert len(rows) == 2, rows
    with master.open("r", encoding="utf-8-sig", newline="") as f:
        assert len(list(csv.reader(f))) == 2


def test_migrate_legacy_local_layout(monkeypatch, tmp_path):
    import services.google_drive_delivery as gd

    images = tmp_path / "Images"
    monkeypatch.setattr(gd, "LOCAL_IMAGES_DIR", str(images))

    flat = images / "VN-FLAT-1"
    flat.mkdir(parents=True)
    (flat / "slot_0.jpg").write_bytes(b"a")

    per_form = images / "form_alpha" / "VN-NESTED-1"
    per_form.mkdir(parents=True)
    (per_form / "slot_0.jpg").write_bytes(b"b")

    gd.migrate_local_deliverables_layout({"VN-FLAT-1": "form_alpha"})

    assert (images / "form_alpha" / "submissions" / "VN-FLAT-1" / "slot_0.jpg").read_bytes() == b"a"
    assert (images / "form_alpha" / "submissions" / "VN-NESTED-1" / "slot_0.jpg").read_bytes() == b"b"
    assert not flat.exists() and not per_form.exists()


def test_cursor_rows_to_records_maps_psycopg_tuples():
    import services.google_drive_delivery as gd

    class Desc:
        def __init__(self, name):
            self.name = name

    class Cur:
        description = [Desc("submission_id"), Desc("status"), Desc("form_id")]

    records = gd._cursor_rows_to_records(Cur(), [("VN-1", "APPROVED", "income_survey")])
    assert records == [{"submission_id": "VN-1", "status": "APPROVED", "form_id": "income_survey"}]


class _FakeCursor:
    def __init__(self, value):
        self.value = value

    async def fetchone(self):
        return (self.value,)


class _FakeLockConn:
    def __init__(self, value):
        self.value = value
        self.statements = []

    async def execute(self, sql, params=None):
        self.statements.append(sql)
        return _FakeCursor(self.value)


class _FakeConnectionCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


class _FakePool:
    def __init__(self, lock_acquired):
        self.conn = _FakeLockConn(lock_acquired)

    def connection(self):
        return _FakeConnectionCtx(self.conn)


@pytest.mark.asyncio
async def test_sync_skips_when_advisory_lock_is_held(monkeypatch):
    import services.google_drive_delivery as gd

    calls = []

    async def fake_impl(db_pool=None):
        calls.append(db_pool)
        return True

    monkeypatch.setattr(gd, "_sync_all_approved_to_drive_unlocked", fake_impl)
    pool = _FakePool(lock_acquired=False)

    assert await gd.sync_all_approved_to_drive(pool) is False
    assert calls == []


@pytest.mark.asyncio
async def test_sync_runs_and_releases_advisory_lock(monkeypatch):
    import services.google_drive_delivery as gd

    calls = []

    async def fake_impl(db_pool=None):
        calls.append(db_pool)
        return True

    monkeypatch.setattr(gd, "_sync_all_approved_to_drive_unlocked", fake_impl)
    pool = _FakePool(lock_acquired=True)

    assert await gd.sync_all_approved_to_drive(pool) is True
    assert calls == [pool]
    assert any("pg_try_advisory_lock" in s for s in pool.conn.statements)
    assert any("pg_advisory_unlock" in s for s in pool.conn.statements)


@pytest.mark.asyncio
async def test_drive_sync_daemon_disabled_when_interval_is_zero():
    import workers.outbox_worker as ow

    assert await ow.run_drive_sync_daemon("postgresql://unused", 0) is None


class _FakeHttpResponse:
    status_code = 200

    def json(self):
        return {"files": []}


class _FakeHttpClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):
        return _FakeHttpResponse()


class _FakeDriveClient:
    instances = []
    root_folder_id = "root_folder"

    def __init__(self, *args, **kwargs):
        self.folders = {}
        self.uploads = []
        self.sheet_imports = []
        _FakeDriveClient.instances.append(self)

    async def get_token(self, client=None):
        return "fake-token"

    async def get_or_create_folder(self, client, token, name, parent=None):
        key = (name, parent)
        if key not in self.folders:
            self.folders[key] = f"folder_{len(self.folders)}"
        return self.folders[key]

    async def find_file_or_folder(self, *args, **kwargs):
        return None

    async def upload_file(self, client, token, filename, content_bytes, mime_type="application/octet-stream", parent_id=None):
        self.uploads.append((parent_id, filename, content_bytes, mime_type))
        return f"file_{len(self.uploads)}"

    async def import_csv_as_sheet(self, client, token, sheet_name, csv_bytes, parent_id=None):
        self.sheet_imports.append((parent_id, sheet_name))
        return "sheet_1"

    async def update_sheet_values(self, *args, **kwargs):
        return True

    async def cleanup_drive_clutter(self, *args, **kwargs):
        return 0


def _patch_sync_env(monkeypatch, tmp_path):
    import json
    import types
    import services.google_drive_delivery as gd

    _FakeDriveClient.instances = []
    monkeypatch.setattr(gd, "LOCAL_IMAGES_DIR", str(tmp_path / "Images"))
    monkeypatch.setattr(gd, "LOCAL_FORMS_SHEETS_DIR", str(tmp_path / "Sheets" / "forms"))
    monkeypatch.setattr(gd, "MASTER_CSV_FILE", str(tmp_path / "Sheets" / "Master.csv"))
    monkeypatch.setattr(gd, "SUBMISSION_PHOTOS_DIR", str(tmp_path / "photos"))
    monkeypatch.setattr(gd, "DEV_SUBMISSIONS_FILE", str(tmp_path / "dev.json"))
    monkeypatch.setattr(gd, "has_google_credentials", lambda: True)
    monkeypatch.setattr(gd, "GoogleDriveClient", _FakeDriveClient)
    monkeypatch.setattr(gd, "httpx", types.SimpleNamespace(AsyncClient=_FakeHttpClient))

    async def archive_sheet(*args, **kwargs):
        return True

    monkeypatch.setattr(gd, "r2_client", types.SimpleNamespace(archive_sheet=archive_sheet))

    def write_dev(records):
        with open(gd.DEV_SUBMISSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f)

    def add_photo(sub_id, name="slot_0.jpg", image_format="WEBP"):
        import io
        from PIL import Image

        photo_dir = tmp_path / "photos" / sub_id
        photo_dir.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), (10, 20, 30)).save(buffer, format=image_format)
        payload = buffer.getvalue()
        (photo_dir / name).write_bytes(payload)
        return payload

    return gd, write_dev, add_photo


def test_sync_backfills_missing_mirror_images_and_uploads_them(monkeypatch, tmp_path):
    import asyncio

    gd, write_dev, add_photo = _patch_sync_env(monkeypatch, tmp_path)
    record = {
        "submission_id": "VN-BACKFILL-OK",
        "form_id": "form_alpha",
        "status": "APPROVED",
        "submitted_at": "2026-11-17T09:00:00Z",
        "form_data": {"car_model": "VF 8"},
    }
    write_dev([record])
    photo_bytes = add_photo("VN-BACKFILL-OK", name="slot_0.jpg", image_format="WEBP")

    assert asyncio.run(gd.sync_all_approved_to_drive()) is True

    mirrored = tmp_path / "Images" / "form_alpha" / "submissions" / "VN-BACKFILL-OK" / "slot_0.webp"
    assert mirrored.read_bytes() == photo_bytes
    assert gd.detect_image_mime(mirrored.read_bytes()) == "image/webp"
    assert not (mirrored.parent / "slot_0.jpg").exists()

    client = _FakeDriveClient.instances[0]
    assert any(
        name == "slot_0.webp" and data == photo_bytes and mime == "image/webp"
        for _, name, data, mime in client.uploads
    )
    form_folder_id = client.folders[("form_alpha", "root_folder")]
    assert ("submissions", form_folder_id) in client.folders


def test_sync_backfill_skips_rejected_and_test_submissions(monkeypatch, tmp_path):
    import asyncio

    gd, write_dev, add_photo = _patch_sync_env(monkeypatch, tmp_path)
    for sid in ("VN-BACKFILL-APPROVED", "VN-BACKFILL-REJECTED", "VN-BACKFILL-MANUAL", "VN-BACKFILL-TEST"):
        add_photo(sid)

    # A non-approved submission that already has mirrored images must not be uploaded.
    import io
    from PIL import Image

    manual_dir = tmp_path / "Images" / "form_alpha" / "submissions" / "VN-MANUAL-EXISTING"
    manual_dir.mkdir(parents=True)
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (1, 2, 3)).save(buffer, format="WEBP")
    (manual_dir / "slot_0.webp").write_bytes(buffer.getvalue())
    write_dev([
        {"submission_id": "VN-BACKFILL-APPROVED", "form_id": "form_alpha", "status": "APPROVED"},
        {"submission_id": "VN-BACKFILL-REJECTED", "form_id": "form_alpha", "status": "REJECTED"},
        {"submission_id": "VN-BACKFILL-MANUAL", "form_id": "form_alpha", "status": "MANUAL_REVIEW"},
        {"submission_id": "VN-BACKFILL-TEST", "form_id": "form_alpha", "status": "APPROVED"},
        {"submission_id": "VN-MANUAL-EXISTING", "form_id": "form_alpha", "status": "MANUAL_REVIEW"},
    ])

    assert asyncio.run(gd.sync_all_approved_to_drive()) is True

    base = tmp_path / "Images" / "form_alpha" / "submissions"
    assert (base / "VN-BACKFILL-APPROVED" / "slot_0.webp").exists()
    assert not (base / "VN-BACKFILL-REJECTED").exists()
    assert not (base / "VN-BACKFILL-MANUAL").exists()
    assert not (base / "VN-BACKFILL-TEST").exists()
    client = _FakeDriveClient.instances[0]
    assert all("VN-MANUAL-EXISTING" not in parent for parent, _, _, _ in client.uploads)

    import csv

    with open(tmp_path / "Sheets" / "forms" / "form_alpha.csv", "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == gd.CSV_HEADERS
    assert [row[-1] for row in rows[1:]] == ["VN-BACKFILL-APPROVED"]


def test_csv_keeps_distinct_submissions_with_identical_answers(monkeypatch, tmp_path):
    import csv
    import services.google_drive_delivery as gd

    forms_dir = tmp_path / "forms"
    monkeypatch.setattr(gd, "MASTER_CSV_FILE", str(tmp_path / "Master.csv"))
    monkeypatch.setattr(gd, "LOCAL_FORMS_SHEETS_DIR", str(forms_dir))

    first = _approved_record("VN-IDENTICAL-1", "form_alpha", "2026-11-17T09:00:00Z")
    second = dict(first, submission_id="VN-IDENTICAL-2")
    gd.append_to_local_sheets_mirror(first)
    gd.append_to_local_sheets_mirror(second)
    gd.append_to_local_sheets_mirror(first)  # same id again: no new row

    with (forms_dir / "form_alpha.csv").open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0][-1] == "Mã Phiếu"
    assert len(rows) == 3
    assert [row[-1] for row in rows[1:]] == ["VN-IDENTICAL-1", "VN-IDENTICAL-2"]


def test_rebuild_local_csvs_writes_one_row_per_submission(monkeypatch, tmp_path):
    import csv
    import services.google_drive_delivery as gd

    forms_dir = tmp_path / "forms"
    monkeypatch.setattr(gd, "MASTER_CSV_FILE", str(tmp_path / "Master.csv"))
    monkeypatch.setattr(gd, "LOCAL_FORMS_SHEETS_DIR", str(forms_dir))

    base = _approved_record("VN-REBUILD-1", "form_alpha", "2026-11-17T09:00:00Z")
    records = {
        "VN-REBUILD-1": base,
        "VN-REBUILD-2": dict(base, submission_id="VN-REBUILD-2"),
        "VN-REBUILD-3": _approved_record("VN-REBUILD-3", "form_beta", "2026-11-17T10:00:00Z"),
    }

    assert gd.rebuild_local_csvs(records) == 3
    assert gd.rebuild_local_csvs(records) == 3  # idempotent rewrite

    with (tmp_path / "Master.csv").open("r", encoding="utf-8-sig", newline="") as f:
        master = list(csv.reader(f))
    assert master[0] == gd.CSV_HEADERS
    assert [row[-1] for row in master[1:]] == ["VN-REBUILD-1", "VN-REBUILD-2", "VN-REBUILD-3"]

    with (forms_dir / "form_alpha.csv").open("r", encoding="utf-8-sig", newline="") as f:
        alpha = list(csv.reader(f))
    with (forms_dir / "form_beta.csv").open("r", encoding="utf-8-sig", newline="") as f:
        beta = list(csv.reader(f))
    assert [row[-1] for row in alpha[1:]] == ["VN-REBUILD-1", "VN-REBUILD-2"]
    assert [row[-1] for row in beta[1:]] == ["VN-REBUILD-3"]


def test_legacy_csv_header_migration_adds_empty_submission_id(monkeypatch, tmp_path):
    import csv
    import services.google_drive_delivery as gd

    path = tmp_path / "legacy.csv"
    old_headers = gd.CSV_HEADERS[:-1]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows([old_headers, ["x"] * len(old_headers)])

    gd._ensure_csv_schema(str(path))

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == gd.CSV_HEADERS
    assert len(rows[1]) == len(gd.CSV_HEADERS)
    assert rows[1][-1] == ""


class _CleanupResponse:
    def __init__(self, data=None, status_code=200):
        self._data = data or {}
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._data

    def raise_for_status(self):
        return None


class _CleanupHttpClient:
    def __init__(self, folders):
        self.folders = folders
        self.deleted = []

    async def get(self, url, params=None, headers=None):
        query = (params or {}).get("q", "")
        if "name = 'Images'" in query:
            return _CleanupResponse({"files": []})
        if "mimeType = 'application/vnd.google-apps.folder'" in query:
            return _CleanupResponse({"files": list(self.folders)})
        return _CleanupResponse({"files": []})

    async def delete(self, url, headers=None):
        self.deleted.append(url.rsplit("/", 1)[-1])
        return _CleanupResponse({}, 204)


def test_explicit_test_folder_matcher_keeps_real_form_ids():
    import services.google_drive_delivery as gd

    assert gd.is_explicit_test_folder("TEST-old")
    assert gd.is_explicit_test_folder("TEST_old")
    assert gd.is_explicit_test_folder("DEV-foo")
    assert gd.is_explicit_test_folder("MOCK-x")
    assert gd.is_explicit_test_folder("SAMPLE-y")
    assert gd.is_explicit_test_folder("DEMO-z")
    assert gd.is_explicit_test_folder("VN-DRIVE-TEST-01")
    assert not gd.is_explicit_test_folder("TB_GSM_2026_Nov_Test")
    assert not gd.is_explicit_test_folder("income_survey")


@pytest.mark.asyncio
async def test_cleanup_deletes_only_explicit_test_folders():
    import services.google_drive_delivery as gd

    folders = [
        {"id": "tb", "name": "TB_GSM_2026_Nov_Test"},
        {"id": "t1", "name": "TEST-old"},
        {"id": "d1", "name": "DEV-foo"},
        {"id": "m1", "name": "MOCK-x"},
        {"id": "s1", "name": "SAMPLE-y"},
        {"id": "de1", "name": "DEMO-z"},
        {"id": "v1", "name": "VN-DRIVE-TEST-01"},
        {"id": "real", "name": "income_survey"},
    ]
    http_client = _CleanupHttpClient(folders)
    client = gd.GoogleDriveClient(root_folder_id="root_folder")

    deleted = await client.cleanup_drive_clutter(http_client, "token")

    assert deleted == 6
    assert set(http_client.deleted) == {"t1", "d1", "m1", "s1", "de1", "v1"}
    assert "tb" not in http_client.deleted
    assert "real" not in http_client.deleted


@pytest.mark.asyncio
async def test_cleanup_protected_names_are_never_deleted():
    import services.google_drive_delivery as gd

    folders = [
        {"id": "tb", "name": "TB_GSM_2026_Nov_Test"},
        {"id": "t1", "name": "TEST-old"},
        {"id": "d1", "name": "DEV-foo"},
    ]
    http_client = _CleanupHttpClient(folders)
    client = gd.GoogleDriveClient(root_folder_id="root_folder")

    deleted = await client.cleanup_drive_clutter(
        http_client, "token", {"TB_GSM_2026_Nov_Test", "TEST-old"}
    )

    assert deleted == 1
    assert http_client.deleted == ["d1"]


def test_unknown_form_duplicate_moves_to_real_form_and_is_not_uploaded(monkeypatch, tmp_path):
    import asyncio
    import io
    from PIL import Image

    gd, write_dev, add_photo = _patch_sync_env(monkeypatch, tmp_path)
    write_dev([{"submission_id": "VN-DUP-1", "form_id": "form_alpha", "status": "APPROVED"}])
    add_photo("VN-DUP-1", name="slot_0.jpg", image_format="WEBP")

    unknown_dir = tmp_path / "Images" / "unknown_form" / "submissions" / "VN-DUP-1"
    unknown_dir.mkdir(parents=True)
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (1, 2, 3)).save(buffer, format="JPEG")
    (unknown_dir / "slot_0.webp").write_bytes(buffer.getvalue())

    assert asyncio.run(gd.sync_all_approved_to_drive()) is True

    assert not (tmp_path / "Images" / "unknown_form").exists()
    merged = tmp_path / "Images" / "form_alpha" / "submissions" / "VN-DUP-1" / "slot_0.webp"
    assert gd.detect_image_mime(merged.read_bytes()) == "image/webp"

    client = _FakeDriveClient.instances[0]
    assert all(name != "unknown_form" for name, _ in client.folders)
    assert sum(1 for _, name, _, _ in client.uploads if name == "slot_0.webp") == 1


def test_extract_income_uses_raw_facts_when_week_columns_zero():
    import services.google_drive_delivery as gd

    weeks = {
        f"week_{n}": {"gross_revenue": 100 * n, "net_income": 50 * n, "total_trips": n}
        for n in range(1, 6)
    }
    record = {
        "submission_id": "VN-INCOME-RAW",
        "form_id": "form_alpha",
        "status": "APPROVED",
        "income_week_1_gross": 0,
        "income_week_1_net": 0,
        "income_week_1_trips": 0,
        "total_gross_revenue": 0,
        "total_net_income": 0,
        "total_trips": 0,
        "raw_extracted_facts": {"weekly_incomes": weeks},
    }
    row = gd.extract_survey_row_data(record)

    def col(name):
        return row[gd.CSV_HEADERS.index(name)]

    assert col("Doanh Thu Tuần 1 (28/09 - 04/10)") == 100
    assert col("Doanh Thu Tuần 5 (26/10 - 01/11)") == 500
    assert col("Thực Nhận Tuần 5") == 250
    assert col("Số Chuyến Tuần 5") == 5
    assert col("Tổng Doanh Thu 5 Tuần") == 1500
    assert col("Tổng Thực Nhận 5 Tuần") == 750
    assert col("Tổng Số Chuyến 5 Tuần") == 15
    assert row[-1] == "VN-INCOME-RAW"


def test_extract_income_prefers_populated_db_columns():
    import services.google_drive_delivery as gd

    record = {
        "submission_id": "VN-INCOME-DB",
        "status": "APPROVED",
        "income_week_1_gross": 9999,
        "income_week_1_net": 8888,
        "income_week_1_trips": 77,
        "raw_extracted_facts": {
            "weekly_incomes": {
                "week_1": {"gross_revenue": 100, "net_income": 50, "total_trips": 1}
            }
        },
    }
    row = gd.extract_survey_row_data(record)

    def col(name):
        return row[gd.CSV_HEADERS.index(name)]

    assert col("Doanh Thu Tuần 1 (28/09 - 04/10)") == 9999
    assert col("Thực Nhận Tuần 1") == 8888
    assert col("Số Chuyến Tuần 1") == 77
