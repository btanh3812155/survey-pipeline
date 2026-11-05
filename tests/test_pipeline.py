import io
import hashlib
import numpy as np
from PIL import Image, ImageDraw

from services.auth_lark import (
    verify_lark_signature,
    handle_lark_challenge_handshake,
)
from services.normalizer import (
    sanitize_and_normalize_image,
    compute_laplacian_blur_score,
)
from services.vision_ai import (
    ExtractedReceiptFacts,
    ReceiptLineItem,
    DualModelVisionClient,
)
from services.evaluator import evaluate_survey_compliance
from services.templates_vn import render_telegram_verdict


def test_lark_signature_raw_sha256_verification():
    import time
    timestamp = str(int(time.time()))
    nonce = "abc123nonce"
    encrypt_key = "my_secret_key"
    body = b'{"type":"event_callback","event":{"submission_id":"sub_001"}}'

    # calculate expected raw SHA256 digest
    raw_content = timestamp.encode() + nonce.encode() + encrypt_key.encode() + body
    expected_sig = hashlib.sha256(raw_content).hexdigest()

    assert verify_lark_signature(timestamp, nonce, encrypt_key, body, expected_sig) is True
    assert verify_lark_signature(timestamp, nonce, encrypt_key, body, "wrong_sig") is False


def test_lark_url_challenge_handshake():
    challenge_payload = b'{"type":"url_verification","token":"test_token","challenge":"challenge_token_xyz"}'
    is_challenge, token = handle_lark_challenge_handshake(challenge_payload)
    assert is_challenge is True
    assert token == "challenge_token_xyz"

    event_payload = b'{"type":"event_callback","event":{}}'
    is_challenge, token = handle_lark_challenge_handshake(event_payload)
    assert is_challenge is False
    assert token is None


def test_image_sanitization_and_alpha_flattening():
    # 1. create an RGBA image with transparent regions and black text
    rgba_img = Image.new("RGBA", (800, 600), (0, 0, 0, 0))  # transparent
    draw = ImageDraw.Draw(rgba_img)
    draw.rectangle([50, 50, 750, 550], fill=(255, 255, 255, 255))  # white receipt body
    draw.text((100, 100), "HOA DON BAN LE", fill=(0, 0, 0, 255))  # black text

    buf = io.BytesIO()
    rgba_img.save(buf, format="PNG")
    raw_bytes = buf.getvalue()

    # 2. sanitize and normalize
    webp_bytes, pil_normalized = sanitize_and_normalize_image(raw_bytes, max_dimension=512)

    assert pil_normalized.mode == "RGB"
    assert max(pil_normalized.size) <= 512
    assert webp_bytes[:4] == b"RIFF"  # valid WebP header


def test_palette_p_mode_with_transparency_flattening():
    # create a mode P image with transparency
    p_img = Image.new("P", (400, 400))
    p_img.info["transparency"] = 0

    buf = io.BytesIO()
    p_img.save(buf, format="PNG")
    raw_bytes = buf.getvalue()

    webp_bytes, pil_normalized = sanitize_and_normalize_image(raw_bytes, max_dimension=512)
    assert pil_normalized.mode == "RGB"


def test_blur_detection_sharp_vs_blurry():
    # 1. create a sharp synthetic image with sharp contrasting high-frequency edges
    sharp_arr = np.zeros((300, 300), dtype=np.uint8)
    for i in range(0, 300, 10):
        sharp_arr[i:i+5, :] = 255  # alternating sharp black & white bars
    sharp_pil = Image.fromarray(sharp_arr)

    is_sharp, sharp_score = compute_laplacian_blur_score(sharp_pil)
    assert is_sharp is True
    assert sharp_score > 100.0

    # 2. create a completely flat/blurry image
    blurry_arr = np.ones((300, 300), dtype=np.uint8) * 128
    blurry_pil = Image.fromarray(blurry_arr)

    is_sharp_blurry, blur_score = compute_laplacian_blur_score(blurry_pil)
    assert is_sharp_blurry is False
    assert blur_score < 10.0


def test_deterministic_evaluation_engine():
    # valid survey receipt
    valid_facts = ExtractedReceiptFacts(
        vendor_name="Cua Hang Phuc Long",
        tax_id="0100109106",
        receipt_serial="HD-99201",
        line_items=[
            ReceiptLineItem(item_name="Tra sua", amount=50000),
            ReceiptLineItem(item_name="Banh mi", amount=30000),
        ],
        raw_subtotal=80000,
        vat_amount=8000,
        raw_total=88000,
        has_signature=True,
        unchecked_boxes_count=0,
        is_screen_photo=False,
    )
    res = evaluate_survey_compliance(valid_facts)
    assert res.passed is True
    assert res.reason_code == "APPROVED"

    # missing signature
    no_sig_facts = valid_facts.model_copy(update={"has_signature": False})
    res_no_sig = evaluate_survey_compliance(no_sig_facts)
    assert res_no_sig.passed is False
    assert "chữ ký khách hàng" in res_no_sig.missing_fields

    # arithmetic mismatch: 50k + 30k + 8k VAT != 99k total!
    math_error_facts = valid_facts.model_copy(update={"raw_total": 99000})
    res_math = evaluate_survey_compliance(math_error_facts)
    assert res_math.passed is False
    assert res_math.reason_code == "ARITHMETIC_MISMATCH"

    # screen photo of other device (driver app / odometer) is permitted for collaborators
    screen_facts = valid_facts.model_copy(update={"is_screen_photo": True})
    res_screen = evaluate_survey_compliance(screen_facts)
    assert res_screen.passed is True
    assert res_screen.reason_code == "APPROVED"

    # digital tampering / photoshop
    tampered_facts = valid_facts.model_copy(update={"is_manipulated_or_photoshopped": True, "tamper_evidence": "font mismatch on total"})
    res_tampered = evaluate_survey_compliance(tampered_facts)
    assert res_tampered.passed is False
    assert res_tampered.reason_code == "DIGITAL_TAMPERING"


def test_telegram_vietnamese_template_rendering():
    text_approved = render_telegram_verdict("VN-8821", "APPROVED")
    assert "PHIẾU KHẢO SÁT ĐÃ ĐƯỢC DUYỆT THÀNH CÔNG" in text_approved
    assert "VN-8821" in text_approved

    text_rejected = render_telegram_verdict(
        "VN-8822", "MISSING_FIELDS", ["chữ ký khách hàng", "mục 4b"]
    )
    assert "PHIẾU KHẢO SÁT CHƯA ĐẠT YÊU CẦU" in text_rejected
    assert "chữ ký khách hàng" in text_rejected
    assert "mục 4b" in text_rejected

def test_fastapi_health_endpoint():
    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"

def test_fastapi_lark_webhook_full_flow(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    import base64
    from fastapi.testclient import TestClient
    from main import app

    # create a clean synthetic receipt image
    img = Image.new("RGB", (600, 800), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for i in range(100, 700, 20):
        draw.line([(50, i), (550, i)], fill=(0, 0, 0), width=3)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    img_b64 = base64.b64encode(buf.getvalue()).decode()

    client = TestClient(app)
    payload = {
        "type": "event_callback",
        "event": {
            "submission_id": "test_e2e_8841",
            "collaborator_tg_id": 999888,
            "image_base64": img_b64
        }
    }
    resp = client.post("/webhook/lark-survey", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "processed"
    assert data["submission_id"] == "test_e2e_8841"
    assert data["verdict"] == "APPROVED"
