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
