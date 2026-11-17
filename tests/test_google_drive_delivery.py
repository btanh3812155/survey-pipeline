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
    form_folder = tmp_path / "TB_GSM_2026_Nov_Test"
    path = gd.save_submission_images_locally(
        "VN-ABC123",
        [{"filename": "slot_0.jpg", "bytes": b"x"}],
        form_id="TB_GSM_2026_Nov_Test",
    )
    assert path == str(form_folder / "VN-ABC123")
    assert (form_folder / "VN-ABC123" / "slot_0.jpg").read_bytes() == b"x"


def test_local_form_folder_rename(monkeypatch, tmp_path):
    import services.google_drive_delivery as gd

    monkeypatch.setattr(gd, "LOCAL_IMAGES_DIR", str(tmp_path))
    old = tmp_path / "old_form"
    (old / "VN-ABC123").mkdir(parents=True)
    (old / "VN-ABC123" / "slot_0.jpg").write_bytes(b"x")
    gd.rename_local_form_folder("old_form", "new_form")
    assert not old.exists()
    assert (tmp_path / "new_form" / "VN-ABC123" / "slot_0.jpg").read_bytes() == b"x"
