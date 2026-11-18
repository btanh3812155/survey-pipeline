# -*- coding: utf-8 -*-
"""Internal CTV matrix sheet generation."""
import types

import pytest

import services.ctv_matrix as cm


class FakeCursor:
    def __init__(self, rows, columns):
        self._rows = rows
        self.description = [types.SimpleNamespace(name=name) for name in columns]

    async def execute(self, sql, params=None):
        return None

    async def fetchall(self):
        return self._rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeConn:
    def __init__(self, rows, columns):
        self._cursor = FakeCursor(rows, columns)

    def cursor(self):
        return self._cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakePool:
    def __init__(self, rows, columns):
        self._conn = FakeConn(rows, columns)

    def connection(self):
        return self._conn


class FakeDriveClient:
    def __init__(self, existing_sheet="sheet_123", column_count=6, row_count=50):
        self.existing_sheet = existing_sheet
        self.column_count = column_count
        self.row_count = row_count
        self.created_sheets = []
        self.clears = []
        self.values_updates = []
        self.batch_requests = []
        self.uploads = []
        self.public_files = []

    async def get_or_create_folder(self, client, token, name, parent=None):
        return {"Internal": "internal_folder", "QR": "qr_folder"}.get(name, f"folder_{name}")

    async def find_file_or_folder(self, client, token, name, parent=None, mime_type=None):
        return self.existing_sheet if name == "CTV_Info" else None

    async def create_sheet(self, client, token, name, parent=None):
        self.created_sheets.append((name, parent))
        return "sheet_123"

    async def get_spreadsheet_metadata(self, client, token, spreadsheet_id):
        return {"sheet_id": 7, "row_count": self.row_count, "column_count": self.column_count}

    async def upload_file(self, client, token, filename, content_bytes, mime_type="application/octet-stream", parent_id=None):
        self.uploads.append((filename, content_bytes, mime_type, parent_id))
        return f"qr_file_{len(self.uploads)}"

    async def make_file_public(self, client, token, file_id):
        self.public_files.append(file_id)
        return True

    async def clear_sheet_values(self, client, token, spreadsheet_id, range_a1="A1:ZZ1000"):
        self.clears.append(range_a1)
        return True

    async def update_sheet_values(self, client, token, sheet_id, rows, value_input_option="RAW"):
        self.values_updates.append((rows, value_input_option))
        return True

    async def batch_update_spreadsheet(self, client, token, spreadsheet_id, requests):
        self.batch_requests.append(requests)
        return True


def _merge_requests(drive):
    return [
        request
        for batch in drive.batch_requests
        for request in batch
        if "mergeCells" in request
    ]


def _unmerge_requests(drive):
    return [
        request
        for batch in drive.batch_requests
        for request in batch
        if "unmergeCells" in request
    ]


@pytest.mark.asyncio
async def test_fetch_active_ctvs_filters_inactive(monkeypatch):
    rows = [
        ("CTV-A", "An", "0901", True),
        ("CTV-B", "Bình", "0902", False),
    ]
    pool = FakePool(rows, ["code", "display_name", "phone", "is_active"])

    ctvs = await cm.fetch_active_ctvs(pool)

    assert [ctv["code"] for ctv in ctvs] == ["CTV-A"]


def _patch_qr(monkeypatch, calls=None):
    async def fake_qr(url, http_client=None):
        if calls is not None:
            calls.append(url)
        return b"png-bytes"

    monkeypatch.setattr(cm, "generate_qr_png", fake_qr)


@pytest.mark.asyncio
async def test_matrix_generates_missing_and_regenerates_outdated_links(monkeypatch):
    ctvs = [
        {"code": "CTV-A", "display_name": "An", "phone": "0901", "is_active": True},
        {"code": "CTV-B", "display_name": "Bình", "phone": "0902", "is_active": True},
    ]
    forms = ["form_one", "form_two"]
    url_a1 = cm.form_link_url("form_one", "CTV-A")
    current_qr = {
        "type": "drive_image",
        "file_id": "existing_qr",
        "image_url": cm.DRIVE_IMAGE_URL.format(file_id="existing_qr"),
        "target": url_a1,
    }

    async def fake_active(pool):
        return ctvs

    async def fake_forms(pool):
        return forms

    async def fake_links(pool):
        return {
            ("CTV-A", "form_one"): {"url": url_a1, "qr": current_qr},
            ("CTV-A", "form_two"): {"url": "https://old.example/form?ref=CTV-A", "qr": "old-qr"},
        }

    upserts = []

    async def fake_upsert(pool, links):
        upserts.extend(links)

    qr_calls = []
    monkeypatch.setattr(cm, "ensure_ctv_tables", _noop)
    monkeypatch.setattr(cm, "fetch_active_ctvs", fake_active)
    monkeypatch.setattr(cm, "fetch_forms", fake_forms)
    monkeypatch.setattr(cm, "fetch_links", fake_links)
    monkeypatch.setattr(cm, "upsert_links", fake_upsert)
    _patch_qr(monkeypatch, qr_calls)

    drive = FakeDriveClient(existing_sheet="sheet_123")
    assert await cm.sync_internal_ctv_matrix(drive, None, "token", "root", object()) is True

    upserted = {(code, form): (url, qr) for code, form, url, qr in upserts}
    assert set(upserted) == {
        ("CTV-A", "form_two"),
        ("CTV-B", "form_one"),
        ("CTV-B", "form_two"),
    }
    assert len(qr_calls) == 3
    form_two_url, form_two_qr = upserted[("CTV-A", "form_two")]
    assert form_two_url == cm.form_link_url("form_two", "CTV-A")
    form_two_record = cm.parse_qr_record(form_two_qr)
    assert form_two_record["type"] == "drive_image"
    assert form_two_record["file_id"] == "qr_file_1"
    assert form_two_record["target"] == form_two_url
    assert form_two_record["image_url"] == cm.DRIVE_IMAGE_URL.format(file_id="qr_file_1")

    assert len(drive.uploads) == 3
    assert all(mime == "image/png" and parent == "qr_folder" for _, _, mime, parent in drive.uploads)
    assert drive.public_files == ["qr_file_1", "qr_file_2", "qr_file_3"]

    rows, value_input = drive.values_updates[0]
    assert value_input == "USER_ENTERED"
    assert rows[0][-2:] == ["Phone", "CTV Code"]
    assert rows[2][-2:] == ["0901", "CTV-A"]
    assert rows[2][0] == f'=IMAGE("{cm.DRIVE_IMAGE_URL.format(file_id="existing_qr")}")'
    assert rows[2][1] == url_a1
    assert rows[2][2] == f'=IMAGE("{cm.DRIVE_IMAGE_URL.format(file_id="qr_file_1")}")'
    assert drive.clears == ["A1:F50"]

    unmerge_ranges = [r["unmergeCells"]["range"] for r in _unmerge_requests(drive)]
    assert len(unmerge_ranges) == 1
    assert unmerge_ranges[0]["endColumnIndex"] == 6
    assert unmerge_ranges[0]["endRowIndex"] == 50

    merge_ranges = [r["mergeCells"]["range"] for r in _merge_requests(drive)]
    assert len(merge_ranges) == 2
    assert [(r["startColumnIndex"], r["endColumnIndex"]) for r in merge_ranges] == [(0, 2), (2, 4)]
    assert all(r["startRowIndex"] == 0 and r["endRowIndex"] == 1 for r in merge_ranges)


@pytest.mark.asyncio
async def test_matrix_clears_previous_width_when_forms_shrink(monkeypatch):
    async def fake_active(pool):
        return [{"code": "CTV-A", "phone": "0901", "is_active": True}]

    async def fake_forms(pool):
        return ["form_one"]

    async def fake_links(pool):
        return {}

    async def fake_upsert(pool, links):
        return None

    monkeypatch.setattr(cm, "ensure_ctv_tables", _noop)
    monkeypatch.setattr(cm, "fetch_active_ctvs", fake_active)
    monkeypatch.setattr(cm, "fetch_forms", fake_forms)
    monkeypatch.setattr(cm, "fetch_links", fake_links)
    monkeypatch.setattr(cm, "upsert_links", fake_upsert)
    _patch_qr(monkeypatch)

    # Previous sheet had 2 forms (6 columns); new matrix has 1 form (4 columns).
    drive = FakeDriveClient(existing_sheet="sheet_123", column_count=6, row_count=50)
    assert await cm.sync_internal_ctv_matrix(drive, None, "token", "root", object()) is True

    assert drive.clears == ["A1:F50"]
    unmerge_ranges = [r["unmergeCells"]["range"] for r in _unmerge_requests(drive)]
    assert unmerge_ranges[0]["endColumnIndex"] == 6
    rows, _ = drive.values_updates[0]
    assert len(rows[0]) == 4
    merge_ranges = [r["mergeCells"]["range"] for r in _merge_requests(drive)]
    assert len(merge_ranges) == 1
    assert (merge_ranges[0]["startColumnIndex"], merge_ranges[0]["endColumnIndex"]) == (0, 2)


@pytest.mark.asyncio
async def test_matrix_merges_two_columns_per_form_beyond_26_columns():
    forms = [f"form_{index:02d}" for index in range(14)]
    ctvs = [{"code": "CTV-A", "phone": "0901", "is_active": True}]

    links = {
        ("CTV-A", form_id): {"url": cm.form_link_url(form_id, "CTV-A"), "qr": cm.qr_image_url(cm.form_link_url(form_id, "CTV-A"))}
        for form_id in forms
    }
    values, merge_ranges = cm.build_matrix_payload(ctvs, forms, links)

    assert len(values[0]) == len(forms) * 2 + 2
    assert len(merge_ranges) == len(forms)
    assert merge_ranges[-1] == (26, 28)
    assert values[0][-2:] == ["Phone", "CTV Code"]
    assert values[1][-2:] == ["", ""]


@pytest.mark.asyncio
async def test_matrix_creates_sheet_when_missing(monkeypatch):
    async def fake_active(pool):
        return [{"code": "CTV-A", "phone": "0901", "is_active": True}]

    async def fake_forms(pool):
        return ["form_one"]

    async def fake_links(pool):
        return {}

    async def fake_upsert(pool, links):
        return None

    monkeypatch.setattr(cm, "ensure_ctv_tables", _noop)
    monkeypatch.setattr(cm, "fetch_active_ctvs", fake_active)
    monkeypatch.setattr(cm, "fetch_forms", fake_forms)
    monkeypatch.setattr(cm, "fetch_links", fake_links)
    monkeypatch.setattr(cm, "upsert_links", fake_upsert)
    _patch_qr(monkeypatch)

    drive = FakeDriveClient(existing_sheet=None)
    assert await cm.sync_internal_ctv_matrix(drive, None, "token", "root", object()) is True
    assert drive.created_sheets == [("CTV_Info", "internal_folder")]

    rows, _ = drive.values_updates[0]
    assert rows[2][-2:] == ["0901", "CTV-A"]
    assert drive.uploads and drive.uploads[0][2] == "image/png"
    assert drive.public_files == ["qr_file_1"]


async def _noop(*args, **kwargs):
    return None


def test_column_letter_handles_beyond_26_columns():
    assert cm.column_letter(0) == "A"
    assert cm.column_letter(25) == "Z"
    assert cm.column_letter(26) == "AA"
    assert cm.column_letter(27) == "AB"
    assert cm.column_letter(51) == "AZ"
    assert cm.column_letter(52) == "BA"
