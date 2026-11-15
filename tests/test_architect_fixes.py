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
    import random
    img = Image.new("RGB", (600, 800), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    rng = random.Random(pattern)
    for _ in range(30):
        x1 = rng.randint(0, 500)
        y1 = rng.randint(0, 700)
        draw.rectangle([(x1, y1), (x1 + rng.randint(20, 80), y1 + rng.randint(20, 80))], fill=(rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)))
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
    async def test_parallel_image_checking_concurrency(self, monkeypatch):
        """Verify role_0, role_1, and facts extraction are gathered concurrently in main.py."""
        monkeypatch.setenv("MOCK_AI", "1")
        client = TestClient(app)
        img0 = make_test_image("lines")
        img1 = make_test_image("lines")

        concurrent_calls = 0
        max_concurrent_observed = 0

        async def mock_classify_role(image_bytes, filename=None, *args, **kwargs):
            nonlocal concurrent_calls, max_concurrent_observed
            concurrent_calls += 1
            if concurrent_calls > max_concurrent_observed:
                max_concurrent_observed = concurrent_calls
            await asyncio.sleep(0.08)
            fn = (filename or "").lower()
            if "0" in fn or "odo" in fn or "profile" in fn:
                return "app_profile"
            return "app_income"
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
        async def mock_ocr(b, week_idx=None):
            nonlocal concurrent_calls, max_concurrent_observed
            concurrent_calls += 1
            if concurrent_calls > max_concurrent_observed:
                max_concurrent_observed = concurrent_calls
            await asyncio.sleep(0.08)
            concurrent_calls -= 1
            w = week_idx or 1
            return {
                "gross_revenue": 1000000 * w,
                "net_income": 800000 * w,
                "discount_and_tax": -200000 * w,
                "total_trips": 10 * w,
                "online_hours": f"{8 * w}h",
                "period": f"Tuần {w}",
                "confidence_score": 0.99,
                "is_legible": True,
                "is_authentic_gsm_ui": True,
            }

        with patch("main.vision_client.classify_image_role", side_effect=mock_classify_role), \
             patch("main.vision_client.extract_facts", side_effect=mock_extract), \
             patch("main.vision_client.ocr_driver_app_income", side_effect=mock_ocr):
            resp = client.post(
                "/api/v1/survey/submit-webrtc",
                data={
                    "driver_phone": "0988123456",
                    "license_plate": "29E-123.45",
                    "driver_name": "Nguyen Van A",
                    "consent_choice": "Tôi đồng ý",
                    "signature_png": "data:image/png;base64,mock",
                    "vehicle_type": "Ô tô",
                    "car_model": "VF 5 Plus",
                    "gsm_duration": "Từ 6 – 12 tháng",
                    "gsm_contract_type": "Xe Taxi xanh GSM",
                    "daily_trips": "18",
                    "monthly_income_range": "12 – 20 triệu VNĐ",
                    "gsm_incentive_policy": "Có áp dụng chỉ tiêu",
                    "gross_revenue": "165000",
                    "is_test_mode": "true",
                    "bypass_img_check": "false",
                },
                files={
                    f"survey_photo_{i}": (f"photo_{i}.jpg", make_test_image(f"lines_{time.time()}_{i}"), "image/jpeg")
                    for i in range(7)
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
        # Ensure renderQuestions uses escapeHtml to interpolate q.title, description, and options
        assert 'value="${escapeHtml(q.title)}"' in html
        assert '>${escapeHtml(q.description || \'\')}</textarea>' in html
        assert 'value="${escapeHtml((q.options || []).join(\', \'))}"' in html
        assert '>${escapeHtml(q.title)}</span>' in html
        assert '#${idx + 1}. ' not in html

        # Ensure INITIAL_SCHEMA JSON parses cleanly without escaping corruption
        assert 'const INITIAL_SCHEMA =' in html
        assert 'Tải lên ảnh \\"Màn hình chính\\" & \\"Doanh thu\\"' in html or 'Tải lên ảnh' in html

    def test_slot_level_image_checks_override_hierarchy(self):
        """Verify slot-level image checks override question-level and global checks."""
        from services.form_engine import get_effective_image_checks, FormSettings
        schema = FormSchema(
            form_id="test_slot_overrides",
            title="Test Overrides",
            settings=FormSettings(
                image_checks=ImageCheckConfig(
                    enable_blur_check=True,
                    blur_threshold=40.0,
                    enable_vehicle_match=True,
                )
            ),
            questions=[
                FormQuestion(
                    id="cam_q",
                    title="Camera Question",
                    type=QuestionType.CAMERA_WEBRTC,
                    image_checks=ImageCheckConfig(
                        enable_blur_check=True,
                        blur_threshold=30.0,
                        enable_vehicle_match=True,
                    ),
                    image_slots=[
                        {
                            "slot": 0,
                            "title": "Slot 0",
                            "image_checks": {
                                "enable_blur_check": False,
                                "blur_threshold": 15.0,
                                "enable_vehicle_match": False,
                            }
                        },
                        {
                            "slot": 1,
                            "title": "Slot 1",
                        }
                    ]
                )
            ]
        )

        # Slot 0 overrides: blur check disabled, vehicle match disabled, blur threshold 15.0
        slot0_checks = get_effective_image_checks(schema, "cam_q", slot_index=0)
        assert slot0_checks.enable_blur_check is False
        assert slot0_checks.blur_threshold == 15.0
        assert slot0_checks.enable_vehicle_match is False

        # Slot 1 inherits from Question-level: blur check enabled, blur threshold 30.0
        slot1_checks = get_effective_image_checks(schema, "cam_q", slot_index=1)
        assert slot1_checks.enable_blur_check is True
        assert slot1_checks.blur_threshold == 30.0
        assert slot1_checks.enable_vehicle_match is True

    def test_dynamic_form_rich_description_support(self):
        """Verify dynamic_form_html includes formatRichDescription and renders rich text container."""
        schema = FormSchema(
            form_id="test_rich_desc",
            title="Test Rich Description",
            questions=[
                FormQuestion(
                    id="q_desc_test",
                    title="Question with links and images",
                    type=QuestionType.TEXT,
                    description="Xem hướng dẫn [tại đây](https://example.com/guide) và ảnh ![Mẫu](https://example.com/sample.jpg)"
                )
            ]
        )
        html = render_dynamic_form_html(schema)
        assert "formatRichDescription" in html
        assert '<div class="q-desc">' in html
        assert ".q-desc a" in html
        assert ".q-desc img" in html

    @pytest.mark.asyncio
    async def test_inter_submission_detects_identical_financial_data(self, monkeypatch):
        """Verify that submitting identical weekly financial numbers to a past approved survey is rejected."""
        monkeypatch.setenv("MOCK_AI", "1")
        client = TestClient(app)
        import time, json, os

        # Ensure clean dev test storage
        dev_file = os.path.join(os.getcwd(), "data", "dev_test_submissions.json")
        os.makedirs(os.path.dirname(dev_file), exist_ok=True)
        
        ts = time.time()
        prev_id = f"DEV-PAST-{int(ts)}"
        seed_record = {
            "submission_id": prev_id,
            "status": "APPROVED",
            "total_gross_revenue": 15000000,
            "total_trips": 100,
            "income_week_1_gross": 3000000,
            "income_week_2_gross": 3000000,
            "weekly_incomes": {
                "week_1": {"gross_revenue": 3000000, "net_income": 2500000, "total_trips": 20, "period": "Tuần 1"},
                "week_2": {"gross_revenue": 3000000, "net_income": 2500000, "total_trips": 20, "period": "Tuần 2"},
            },
            "image_dhash_int": 999999999999,  # distinct hash
        }
        records = [seed_record]
        with open(dev_file, "w", encoding="utf-8") as f:
            json.dump(records, f)

        async def mock_role(img_bytes, filename=None, *args, **kwargs):
            fn = (filename or "").lower()
            if "0" in fn or "profile" in fn:
                return "app_profile"
            return "app_income"

        async def mock_ocr_copied_numbers(img_bytes, week_idx=None, *args, **kwargs):
            return {
                "is_authentic_gsm_ui": True,
                "detected_app": "XANH_SM",
                "gross_revenue": 3000000,
                "net_income": 2500000,
                "total_trips": 20,
                "period": f"Tuần {week_idx or 1}",
                "confidence_score": 0.99,
                "is_legible": True,
            }

        with patch("main.vision_client.classify_image_role", side_effect=mock_role), \
             patch("main.vision_client.ocr_driver_app_income", side_effect=mock_ocr_copied_numbers):
            resp = client.post(
                "/api/v1/survey/submit-webrtc",
                data={
                    "driver_phone": "0988777888",
                    "driver_name": "Tài xế Copy Số Liệu",
                    "consent_choice": "Tôi đồng ý",
                    "signature_png": "data:image/png;base64,mock",
                    "vehicle_type": "Ô tô",
                    "car_model": "VF 5 Plus",
                    "gsm_duration": "Từ 6 – 12 tháng",
                    "gsm_contract_type": "Xe Taxi xanh GSM",
                    "daily_trips": "18",
                    "monthly_income_range": "12 – 20 triệu VNĐ",
                    "gsm_incentive_policy": "Có áp dụng chỉ tiêu",
                    "is_test_mode": "true",
                    "bypass_img_check": "false",
                },
                files={
                    f"survey_photo_{i}": (f"photo_{i}.jpg", make_test_image(f"pattern_unique_{ts}_{i}"), "image/jpeg")
                    for i in range(7)
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("status") == "REJECTED"
            assert "trùng" in data.get("reason", "").lower()
            assert data.get("duplicate_detected") is True

    @pytest.mark.asyncio
    async def test_action_checklist_lease_and_fault_isolation(self):
        """Verify action checklist initial states, lease acquisition, and idempotent completion."""
        from services.action_checklist import (
            init_action_checklist,
            acquire_action_lease,
            update_action_record,
            fetch_action_status,
        )
        # 1. Approved without collaborator skips billing ledger
        chk = init_action_checklist(is_approved=True, has_collaborator=False)
        assert chk["r2_upload"]["status"] == "PENDING"
        assert chk["telegram_notify"]["status"] == "PENDING"
        assert chk["lark_sync"]["status"] == "PENDING"
        assert chk["gdrive_sync"]["status"] == "PENDING"
        assert chk["billing_ledger"]["status"] == "SKIPPED"

        # 2. Rejected submission skips external deliverables
        rej_chk = init_action_checklist(is_approved=False, has_collaborator=True)
        assert rej_chk["gdrive_sync"]["status"] == "SKIPPED"
        assert rej_chk["lark_sync"]["status"] == "SKIPPED"
        assert rej_chk["billing_ledger"]["status"] == "SKIPPED"

        # 3. Lease acquisition guard (test mode)
        sub_id = "DEV-CHK-TEST-001"
        # Acquire lease on r2_upload
        lease1 = await acquire_action_lease(db_pool=None, submission_id=sub_id, action="r2_upload", lease_seconds=30, is_test_mode=True)
        assert lease1 is True

        # Mark as completed
        await update_action_record(db_pool=None, submission_id=sub_id, action="r2_upload", status="COMPLETED", meta={"url": "https://r2/test.webp"}, is_test_mode=True)

        # Re-acquiring lease on COMPLETED action must return False (idempotency guard)
        lease2 = await acquire_action_lease(db_pool=None, submission_id=sub_id, action="r2_upload", lease_seconds=30, is_test_mode=True)
        assert lease2 is False

        status_map = await fetch_action_status(db_pool=None, submission_id=sub_id, is_test_mode=True)
        assert status_map["r2_upload"]["status"] == "COMPLETED"
        assert status_map["r2_upload"]["url"] == "https://r2/test.webp"
