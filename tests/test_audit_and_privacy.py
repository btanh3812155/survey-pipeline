import pytest
import io
from PIL import Image
from services.audit_and_privacy import (
    compute_audit_hash,
    tokenize_or_get_driver_id,
    sanitize_rejection_image_and_metadata,
    persist_rejected_submission,
    scrub_driver_pii,
)

def test_audit_hash_chaining():
    h1 = compute_audit_hash("GENESIS", "2026-11-03T00:00:00Z", "HOP_1_INGEST", {"ip": "1.2.3.4"})
    assert isinstance(h1, str) and len(h1) == 64
    
    # Next hop chains to h1
    h2 = compute_audit_hash(h1, "2026-11-03T00:00:01Z", "HOP_2_NORMALIZER", {"blur_var": 210.5})
    assert isinstance(h2, str) and len(h2) == 64
    assert h1 != h2

    # Tampering with h1 alters h2
    h2_tampered = compute_audit_hash("TAMPERED_PREV", "2026-11-03T00:00:01Z", "HOP_2_NORMALIZER", {"blur_var": 210.5})
    assert h2 != h2_tampered

@pytest.mark.asyncio
async def test_tokenize_driver_id():
    phone = "0988123456"
    driver_id = await tokenize_or_get_driver_id(None, phone)
    assert driver_id.startswith("DRV-")
    assert phone not in driver_id

def test_sanitize_rejection_image():
    # Create test image with red square
    img = Image.new("RGB", (200, 200), color=(255, 0, 0))
    # Add a mock face/plate region (green box)
    for x in range(50, 100):
        for y in range(50, 100):
            img.putpixel((x, y), (0, 255, 0))
    
    in_buf = io.BytesIO()
    img.save(in_buf, format="JPEG")
    raw_bytes = in_buf.getvalue()

    sanitized = sanitize_rejection_image_and_metadata(raw_bytes, mask_boxes=[[50, 50, 100, 100]])
    assert sanitized is not None
    assert len(sanitized) > 0

    # Verify sanitized image can be read and is WEBP
    res_img = Image.open(io.BytesIO(sanitized))
    assert res_img.format == "WEBP"
    assert res_img.size == (200, 200)

@pytest.mark.asyncio
async def test_persist_rejected_submission_mock():
    sub_id = "SUB-TEST-REJECT-001"
    res = await persist_rejected_submission(
        db_pool=None,
        submission_id=sub_id,
        driver_id="DRV-A1B2C",
        rejection_reason="BLURRY_IMAGE",
        image_dhash="1" * 64,
        raw_facts={"blur_score": 45.2}
    )
    assert res == sub_id

@pytest.mark.asyncio
async def test_scrub_driver_pii_mock():
    res = await scrub_driver_pii(db_pool=None, driver_phone="0912345678")
    assert res["status"] == "scrubbed"
    assert res["driver_phone"] == "[REDACTED_DECREE13]"
