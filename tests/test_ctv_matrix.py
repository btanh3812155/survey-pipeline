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
    def __init__(self, existing_sheet="sheet_123"):
        self.existing_sheet = existing_sheet
        self.created_sheets = []
        self.cleared = 0
        self.values_updates = []
        self.batch_requests = []

    async def get_or_create_folder(self, client, token, name, parent=None):
        assert name == "Internal"
        return "internal_folder"

    async def find_file_or_folder(self, client, token, name, parent=None, mime_type=None):
        assert name == "CTV_Info"
        return self.existing_sheet

    async def create_sheet(self, client, token, name, parent=None):
        self.created_sheets.append((name, parent))
        return "sheet_123"

    async def get_spreadsheet_sheet_id(self, client, token, spreadsheet_id):
        return 7

    async def clear_sheet_values(self, client, token, spreadsheet_id, range_a1="A1:ZZ1000"):
        self.cleared += 1
        return True

    async def update_sheet_values(self, client, token, sheet_id, rows, value_input_option="RAW"):
        self.values_updates.append((rows, value_input_option))
        return True

    async def batch_update_spreadsheet(self, client, token, spreadsheet_id, requests):
        self.batch_requests.append(requests)
        return True


def _expected(code, form_id):
    url = cm.form_link_url(form_id, code)
    return url, cm.qr_image_url(url)


@pytest.mark.asyncio
async def test_fetch_active_ctvs_filters_inactive(monkeypatch):
    rows = [
        ("CTV-A", "An", "0901", True),
        ("CTV-B", "Bình", "0902", False),
    ]
    pool = FakePool(rows, ["code", "display_name", "phone", "is_active"])

    ctvs = await cm.fetch_active_ctvs(pool)

    assert [ctv["code"] for ctv in ctvs] == ["CTV-A"]


@pytest.mark.asyncio
async def test_matrix_generates_missing_and_regenerates_outdated_links(monkeypatch):
    ctvs = [
        {"code": "CTV-A", "display_name": "An", "phone": "0901", "is_active": True},
        {"code": "CTV-B", "display_name": "Bình", "phone": "0902", "is_active": True},
    ]
    forms = ["form_one", "form_two"]
    url_a1, qr_a1 = _expected("CTV-A", "form_one")

    async def fake_active(pool):
        return ctvs

    async def fake_forms(pool):
        return forms

    async def fake_links(pool):
        return {
            ("CTV-A", "form_one"): {"url": url_a1, "qr": qr_a1},
            ("CTV-A", "form_two"): {"url": "https://old.example/form?ref=CTV-A", "qr": "old-qr"},
        }

    upserts = []

    async def fake_upsert(pool, links):
        upserts.extend(links)

    monkeypatch.setattr(cm, "ensure_ctv_tables", _noop)
    monkeypatch.setattr(cm, "fetch_active_ctvs", fake_active)
    monkeypatch.setattr(cm, "fetch_forms", fake_forms)
    monkeypatch.setattr(cm, "fetch_links", fake_links)
    monkeypatch.setattr(cm, "upsert_links", fake_upsert)

    drive = FakeDriveClient(existing_sheet="sheet_123")
    assert await cm.sync_internal_ctv_matrix(drive, None, "token", "root", object()) is True

    upserted = {(code, form): (url, qr) for code, form, url, qr in upserts}
    assert set(upserted) == {
        ("CTV-A", "form_two"),
        ("CTV-B", "form_one"),
        ("CTV-B", "form_two"),
    }
    assert upserted[("CTV-A", "form_two")] == _expected("CTV-A", "form_two")
    assert "api.qrserver.com" in upserted[("CTV-B", "form_one")][1]

    rows, value_input = drive.values_updates[0]
    assert value_input == "USER_ENTERED"
    assert rows[0][-2:] == ["Phone", "CTV Code"]
    assert rows[2][-2:] == ["0901", "CTV-A"]
    assert rows[2][0] == f'=IMAGE("{_expected("CTV-A", "form_one")[1]}")'
    assert rows[2][1] == url_a1
    assert drive.cleared == 1

    requests = drive.batch_requests[0]
    ranges = [request["mergeCells"]["range"] for request in requests]
    assert len(ranges) == 2
    assert [(r["startColumnIndex"], r["endColumnIndex"]) for r in ranges] == [(0, 2), (2, 4)]
    assert all(r["startRowIndex"] == 0 and r["endRowIndex"] == 1 for r in ranges)


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

    drive = FakeDriveClient(existing_sheet=None)
    assert await cm.sync_internal_ctv_matrix(drive, None, "token", "root", object()) is True
    assert drive.created_sheets == [("CTV_Info", "internal_folder")]

    rows, _ = drive.values_updates[0]
    assert rows[2][-2:] == ["0901", "CTV-A"]


async def _noop(*args, **kwargs):
    return None


def test_column_letter_handles_beyond_26_columns():
    assert cm.column_letter(0) == "A"
    assert cm.column_letter(25) == "Z"
    assert cm.column_letter(26) == "AA"
    assert cm.column_letter(27) == "AB"
    assert cm.column_letter(51) == "AZ"
    assert cm.column_letter(52) == "BA"
