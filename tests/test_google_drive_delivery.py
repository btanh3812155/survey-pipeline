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
