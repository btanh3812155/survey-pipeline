import io
import os

import pytest
from PIL import Image

from routers.survey import _store_submission_photos
from services.normalizer import normalize_image_to_webp
from services.telegram_polling import DriverBotRunner

WEBP_MAGIC = b"RIFF"


def _image_bytes(fmt: str, size=(320, 240), color=(200, 120, 80), mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format=fmt)
    return buf.getvalue()


def test_jpeg_upload_is_stored_as_webp():
    raw = _image_bytes("JPEG")
    out = normalize_image_to_webp(raw)
    assert out and out.startswith(WEBP_MAGIC) and out[8:12] == b"WEBP"
    assert Image.open(io.BytesIO(out)).format == "WEBP"


def test_png_with_alpha_is_flattened_into_webp():
    buf = io.BytesIO()
    Image.new("RGBA", (200, 200), (10, 200, 120, 90)).save(buf, format="PNG")
    out = normalize_image_to_webp(buf.getvalue())
    assert out and out[8:12] == b"WEBP"
    assert Image.open(io.BytesIO(out)).mode == "RGB"


def test_webp_payload_is_not_double_encoded():
    raw = _image_bytes("WEBP")
    assert normalize_image_to_webp(raw) == raw


def test_undecodable_payload_returns_none():
    assert normalize_image_to_webp(b"not an image at all") is None
    assert normalize_image_to_webp(b"") is None


def test_store_writes_only_webp_and_quarantines_garbage(tmp_path):
    jpeg = _image_bytes("JPEG")
    png = _image_bytes("PNG")
    webp = _image_bytes("WEBP")
    garbage = b"\x00\x01not-an-image"

    _store_submission_photos(str(tmp_path), [jpeg, png, webp, garbage])

    names = sorted(os.listdir(tmp_path))
    assert names == ["slot_0.webp", "slot_1.webp", "slot_2.webp", "slot_3.bin"]
    for name in names[:3]:
        with open(tmp_path / name, "rb") as f:
            assert f.read()[8:12] == b"WEBP"
    assert (tmp_path / "slot_2.webp").read_bytes() == webp
    assert (tmp_path / "slot_3.bin").read_bytes() == garbage


@pytest.mark.asyncio
async def test_telegram_replacement_is_persisted_as_webp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = DriverBotRunner(bot_token="mock_driver_token")
    slot_dir = tmp_path / "data" / "submission_photos" / "VN-TG0001"
    slot_dir.mkdir(parents=True)
    (slot_dir / "slot_0.jpg").write_bytes(_image_bytes("JPEG"))
    (slot_dir / "slot_1.jpg").write_bytes(_image_bytes("JPEG"))

    await runner._store_replacement_photo("VN-TG0001", "slot_0", _image_bytes("JPEG"))

    names = sorted(os.listdir(slot_dir))
    assert names == ["slot_0.webp", "slot_1.jpg"]
    with open(slot_dir / "slot_0.webp", "rb") as f:
        assert f.read()[8:12] == b"WEBP"


@pytest.mark.asyncio
async def test_telegram_generic_slot_placeholder_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = DriverBotRunner(bot_token="mock_driver_token")
    await runner._store_replacement_photo("VN-TG0002", "slot_photo", _image_bytes("JPEG"))
    assert not (tmp_path / "data" / "submission_photos" / "VN-TG0002").exists()


def test_dynamic_form_never_sends_unencoded_uploads():
    html_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "static", "ui", "dynamic_form.html",
    )
    html = open(html_path, encoding="utf-8").read()
    assert "file.size < 350_000" not in html, "gallery uploads must not bypass WebP re-encoding"
    compress_body = html.split("async function compressImageFile", 1)[1].split("function applyFileToSlot", 1)[0]
    assert "canvasWebpBlob(cvs, quality)" in compress_body
    snap_body = html.split("function snapPhoto", 1)[1].split("let lastPreCheckTimestamp", 1)[0]
    assert "canvasWebpBlob(cvs, 0.82)" in snap_body
