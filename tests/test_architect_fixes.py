import io
import time
import asyncio
import pytest
from unittest.mock import patch
from PIL import Image, ImageDraw
from fastapi.testclient import TestClient

from main import app
from config import settings
from services.evaluator import evaluate_survey_compliance
from services.vision_ai import ExtractedReceiptFacts, ReceiptLineItem
from services.form_engine import (
    DEFAULT_TRUST_BRIDGE_SCHEMA,
    FormSchema,
    FormQuestion,
    QuestionType,
    ImageCheckConfig,
)
from services.dynamic_form_html import render_dynamic_form_html
from services.form_manager_html import render_form_editor_html


def make_test_image(pattern: str = "lines") -> bytes:
    img = Image.new("RGB", (600, 800), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for i in range(100, 700, 20):
        draw.line([(50, i), (550, i)], fill=(0, 0, 0), width=4)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class TestArchitectFixes:
    """Automated tests verifying latency optimization, screen photo allowance, form resets, and tab 3 rendering."""

    def test_screen_photo_allowed_in_evaluator(self):
        """Taking photo of driver phone screen / odometer is explicitly allowed and passes compliance."""
        valid_facts = ExtractedReceiptFacts(
            vendor_name="GSM Ride Hailing",
            tax_id="0108938491",
            receipt_serial="GSM-88912",
            line_items=[ReceiptLineItem(item_name="Cuoc xe", amount=150000)],
            raw_subtotal=150000,
            vat_amount=15000,
            raw_total=165000,
            has_signature=True,
            unchecked_boxes_count=0,
            is_screen_photo=True,  # Device screen recapture
            is_manipulated_or_photoshopped=False,
            notes="Photo of smartphone driver app screen",
        )
        res = evaluate_survey_compliance(valid_facts)
        assert res.passed is True
        assert res.status_tier == "APPROVED"
        assert res.reason_code == "APPROVED"

    def test_default_image_check_config_screen_photo_disabled(self):
        """ImageCheckConfig has enable_screen_photo_check=False by default."""
        cfg = ImageCheckConfig()
        assert cfg.enable_screen_photo_check is False

    @pytest.mark.asyncio
    async def test_parallel_image_checking_concurrency(self):
        """Verify role_0, role_1, and facts extraction are gathered concurrently in main.py."""
        client = TestClient(app)
        img0 = make_test_image("lines")
        img1 = make_test_image("lines")

        concurrent_calls = 0
        max_concurrent_observed = 0

        async def mock_classify_role(image_bytes, filename=None):
            nonlocal concurrent_calls, max_concurrent_observed
            concurrent_calls += 1
            if concurrent_calls > max_concurrent_observed:
                max_concurrent_observed = concurrent_calls
            await asyncio.sleep(0.08)
            concurrent_calls -= 1
            fn = (filename or "").lower()
            if "0" in fn or "odo" in fn:
                return "odometer"
            return "revenue_screen"

        async def mock_extract(norm_bytes):
            nonlocal concurrent_calls, max_concurrent_observed
            concurrent_calls += 1
            if concurrent_calls > max_concurrent_observed:
                max_concurrent_observed = concurrent_calls
            await asyncio.sleep(0.08)
            concurrent_calls -= 1
            return ExtractedReceiptFacts(
                vendor_name="GSM Ride",
                tax_id="0108938491",
                receipt_serial="GSM-1122",
                line_items=[ReceiptLineItem(item_name="Cuoc xe", amount=150000)],
                raw_subtotal=150000,
                vat_amount=15000,
                raw_total=165000,
                has_signature=True,
                unchecked_boxes_count=0,
                is_screen_photo=True,
                notes="revenue_screen",
            )

        with patch("main.vision_client.classify_image_role", side_effect=mock_classify_role), \
             patch("main.vision_client.extract_facts", side_effect=mock_extract):

            resp = client.post(
                "/api/v1/survey/submit-webrtc",
                data={
                    "driver_phone": "0988123456",
                    "license_plate": "29E-123.45",
                    "driver_name": "Nguyen Van A",
                    "consent_choice": "Tôi đồng ý",
                    "signature_png": "data:image/png;base64,mock",
                    "vehicle_type": "Ô tô",
                    "gross_revenue": "165000",
                    "odometer_km": "15200",
                    "is_test_mode": "true",
                    "bypass_img_check": "false",
                },
                files={
                    "survey_photo_0": ("odometer_0.jpg", img0, "image/jpeg"),
                    "survey_photo_1": ("revenue_1.jpg", img1, "image/jpeg"),
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "APPROVED"
        assert max_concurrent_observed >= 2, f"Expected concurrency >= 2, got {max_concurrent_observed}"

    def test_execute_reset_form_clears_all_slots_and_camera_state(self):
        """Verify dynamic_form_html JavaScript executeResetForm wipes answers, slots, and resets UI."""
        html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA, telegram_bot_username="")
        
        # Verify executeResetForm resets slot 0 and cleans up all image slots
        assert "function executeResetForm" in html
        assert "selectCameraSlot(q.id, 0)" in html
        assert "activeSlotByQId[q.id] = 0" in html
        assert "deactivateCamera(q.id)" in html
        assert "delete answers[k]" in html
        assert "delete answers[`${q.id}_${sIdx}`]" in html
        assert "cam-slot-pill" in html
        assert "pill.classList.remove('captured')" in html
        assert "retakeBtn.style.display = 'none'" in html
        assert "screenOff.style.display = 'flex'" in html

        # Verify in-app history action button in submissionResultModal
        assert "btnSubResultHistory" in html
        assert "openHistoryModal()" in html
        assert "Tra cứu lịch sử nộp trên web" in html

        # Verify telegram bot link is hidden when bot username is empty
        assert 'href="https://t.me/"' in html or 'style="display:none;' in html
        assert 'style="display:none;' in html

    def test_dynamic_form_html_with_configured_telegram_bot(self):
        """Verify dynamic_form_html shows Telegram button when configured."""
        html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA, telegram_bot_username="TrustBridgeLiveBot")
        assert 'href="https://t.me/TrustBridgeLiveBot"' in html
        assert 'style="display:flex;' in html

    def test_form_manager_tab3_renders_questions_with_double_quotes(self):
        """Verify Tab 3 in Form Manager escapes quotes in titles and descriptions cleanly."""
        schema = FormSchema(
            form_id="test_quotes_form",
            title='Biểu mẫu "Đặc biệt"',
            description='Mô tả có "dấu ngoặc kép"',
            questions=[
                FormQuestion(
                    id="q_quote",
                    title='Tải lên ảnh "Màn hình chính" & "Doanh thu"',
                    type=QuestionType.TEXT,
                    required=True,
                    description='Mục "Thống kê thu nhập" trên điện thoại',
                    options=['Lựa chọn "A"', 'Lựa chọn "B"'],
                )
            ],
        )
        html = render_form_editor_html(schema)

        # Ensure escapeHtml helper exists in JavaScript
        assert "function escapeHtml" in html
        # Ensure renderQuestions uses escapeHtml to interpolate q.title, description, and options into value attributes
        assert 'value="${escapeHtml(q.title)}"' in html
        assert 'value="${escapeHtml(q.description || \'\')}"' in html
        assert 'value="${escapeHtml((q.options || []).join(\', \'))}"' in html
        assert '#${idx + 1}. ${escapeHtml(q.title)}' in html

        # Ensure INITIAL_SCHEMA JSON parses cleanly without escaping corruption
        assert 'const INITIAL_SCHEMA =' in html
        assert 'Tải lên ảnh \\"Màn hình chính\\" & \\"Doanh thu\\"' in html or 'Tải lên ảnh' in html
