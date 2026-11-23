import io
import time
import math
import random
import secrets
import asyncio
import threading
from typing import Dict, Any
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from PIL import Image
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport

import main
from main import app, BoundedTTLCache
from services.form_engine import (
    FormSchema,
    FormQuestion,
    FormSettings,
    QuestionType,
    ConditionalRule,
    validate_submission_against_schema,
)
from tests.module_checks import check_module, run_checks_sync
from services.audit_and_privacy import scrub_driver_pii
@pytest.fixture
def numeric_schema():
    return FormSchema(
        form_id="wave3_numeric", title="Numeric contract",
        settings=FormSettings(modules=[
            check_module("revenue_range", "number_range",
                         {"field": "gross_revenue", "min": 0, "max": 50_000_000,
                          "message": "Doanh thu ngoài khoảng cho phép"},
                         "Doanh thu ngoài khoảng cho phép")
        ]),
        questions=[FormQuestion(id="gross_revenue", title="Revenue", type=QuestionType.NUMBER)],
    )




# --- Test Helpers ---
def make_sharp_image_bytes() -> bytes:
    """Generates a valid, sharp JPEG image buffer."""
    img = Image.new("RGB", (800, 600), color=(255, 255, 255))
    for x in range(0, 800, 20):
        for y in range(600):
            img.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


# ==============================================================================
# TEAM 1 (REGRESSION)
# ==============================================================================
class TestTeam1Regression:
    """
    Team 1: Regression Test Suite.
    - Test /samples/{filename} route with all 4 sample images.
    - Test BoundedTTLCache under concurrent read/write stress.
    """

    @pytest.mark.parametrize(
        "filename",
        [
            "gsm_sample_bike.jpg",
            "gsm_sample_car_crop.jpg",
            "gsm_sample_car_photo.jpg",
            "sample_income_report.jpg",
        ],
    )
    def test_sample_images_route_status_and_content_type(self, filename: str):
        """
        Verify that /samples/{filename} serves each of the 4 sample images
        with status 200 and 'image/jpeg' Content-Type.
        """
        client = TestClient(app)
        response = client.get(f"/samples/{filename}")
        assert response.status_code == 200, f"Failed to retrieve sample image: {filename}"
        assert "image/jpeg" in response.headers.get("content-type", "").lower(), (
            f"Expected image/jpeg Content-Type for {filename}, got {response.headers.get('content-type')}"
        )
        assert len(response.content) > 0, f"Sample image {filename} returned empty body"

    def test_sample_images_route_not_found_and_traversal_blocked(self):
        """Regression: verify non-existent files and path traversals return 404."""
        client = TestClient(app)
        assert client.get("/samples/non_existent.jpg").status_code == 404
        assert client.get("/samples/../../main.py").status_code == 404

    def test_bounded_ttl_cache_concurrent_read_write_stress(self):
        """
        Verify BoundedTTLCache under concurrent read/write stress:
        - 30 concurrent threads
        - 3,000 randomized supported operations (get, set, contains, len)
        - Strict capacity bounds enforced without exceptions or race condition corruption.
        """
        cache = BoundedTTLCache(max_size=100, ttl_seconds=2.0)
        errors = []
        operations_count = 100
        num_threads = 30

        def worker(worker_id: int):
            for i in range(operations_count):
                try:
                    k = f"key_{random.randint(0, 150)}"
                    op = random.choice(["get", "set", "contains", "len"])
                    if op == "set":
                        cache[k] = f"worker_{worker_id}_val_{i}"
                    elif op == "get":
                        _ = cache.get(k)
                    elif op == "contains":
                        _ = k in cache
                    elif op == "len":
                        curr_len = len(cache)
                        assert curr_len <= 100
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Concurrent cache operations encountered errors: {errors}"
        assert len(cache) <= 100, f"Cache exceeded max_size limit: {len(cache)}"


# ==============================================================================
# TEAM 2 (EXPLORATORY BUG HUNTING & CONCURRENCY STRESS)
# ==============================================================================
class TestTeam2ExploratoryAndConcurrency:
    """
    Team 2: Exploratory Bug Hunting & Concurrency Stress.
    - Concurrent async load hitting /api/v1/survey/submit-webrtc.
    - Payload boundaries: zero-byte, truncated JPEG, phone formats, 1e8, NaN.
    - Form engine recursion: cyclic or deeply chained conditional dependencies.
    """

    @pytest.mark.asyncio
    async def test_concurrent_async_load_submit_webrtc(self):
        """
        Simulate concurrent asynchronous requests hitting /api/v1/survey/submit-webrtc.
        Verifies that simultaneous submissions are isolated and do not crash or deadlock.
        """
        sharp_bytes = make_sharp_image_bytes()
        concurrency = 15

        async def send_submission(idx: int):
            phone = f"0988{idx:06d}"
            payload = {
                "driver_phone": phone,
                "driver_name": f"Concurrency Driver {idx}",
                "consent_choice": "Tôi đồng ý",
                "license_plate": "29E-123.45",
                "vehicle_type": "Ô tô điện VinFast (GSM)",
                "car_model": "VF 5 Plus",
                "gross_revenue": "650000",
                "odometer_km": "50",
                "camera_receipt": "uploaded",
                "signature_png": "data:image/png;base64,mock",
                "is_test_mode": "true",
                "bypass_img_check": "true",
            }
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/survey/submit-webrtc",
                    data=payload,
                    files={"survey_photo": (f"capture_{idx}.jpg", io.BytesIO(sharp_bytes), "image/jpeg")},
                )
                return resp

        tasks = [send_submission(i) for i in range(concurrency)]
        responses = await asyncio.gather(*tasks)

        assert len(responses) == concurrency
        for r in responses:
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "APPROVED"
            assert data["submission_id"].startswith("DEV-")

    def test_payload_boundary_zero_byte_image(self):
        """Zero-byte image buffer upload must be rejected safely with image decoding error."""
        client = TestClient(app)
        payload = {
            "driver_phone": "0988123456",
            "driver_name": "Zero Byte Driver",
            "consent_choice": "Tôi đồng ý",
            "license_plate": "29E-123.45",
            "vehicle_type": "Ô tô điện VinFast (GSM)",
            "car_model": "VF 5 Plus",
            "gross_revenue": "650000",
            "odometer_km": "50",
            "camera_receipt": "uploaded",
            "signature_png": "data:image/png;base64,mock",
            "is_test_mode": "true",
            "bypass_img_check": "false",
        }
        resp = client.post(
            "/api/v1/survey/submit-webrtc",
            data=payload,
            files={"survey_photo": ("zero.jpg", b"", "image/jpeg")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "REJECTED"
        assert "Không thể giải mã ảnh" in data["reason"]

    def test_payload_boundary_truncated_jpeg_buffer(self):
        """Truncated JPEG buffer must be rejected safely by image sanitizer."""
        client = TestClient(app)
        sharp_bytes = make_sharp_image_bytes()
        truncated_jpeg = sharp_bytes[:64]  # Truncate to first 64 bytes

        payload = {
            "driver_phone": "0988123456",
            "driver_name": "Truncated Driver",
            "consent_choice": "Tôi đồng ý",
            "license_plate": "29E-123.45",
            "vehicle_type": "Ô tô điện VinFast (GSM)",
            "car_model": "VF 5 Plus",
            "gross_revenue": "650000",
            "odometer_km": "50",
            "camera_receipt": "uploaded",
            "signature_png": "data:image/png;base64,mock",
            "is_test_mode": "true",
            "bypass_img_check": "false",
        }
        resp = client.post(
            "/api/v1/survey/submit-webrtc",
            data=payload,
            files={"survey_photo": ("truncated.jpg", truncated_jpeg, "image/jpeg")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "REJECTED"
        assert "Không thể giải mã ảnh" in data["reason"]

    @pytest.mark.parametrize(
        "phone_str,valid",
        [
            ("+84 988 123 456", False),
            ("0988-123-456", False),
            ("+84988123456", True),
            ("0988 123 456", False),
        ],
    )
    def test_payload_boundary_extreme_vietnamese_phone_strings(self, phone_str: str, valid: bool):
        """
        Formatted strings with spaces or dashes are rejected; local 0+9 and
        +84+9 forms are accepted by the phone module.
        """
        answers = {
            "driver_signature": "data:image/png;base64,mock",
            "consent_choice": "Tôi đồng ý",
            "driver_phone": phone_str,
            "driver_name": "Test Driver",
            "license_plate": "29E-123.45",
            "vehicle_type": "Ô tô điện VinFast (GSM)",
            "car_model": "VF 5 Plus",
            "gross_revenue": "650000",
            "odometer_km": "50",
            "camera_receipt": "uploaded",
        }
        schema = FormSchema(
            form_id="wave3_phone", title="Phone contract",
            settings=FormSettings(modules=[
                check_module("phone", "regex",
                             {"field": "driver_phone",
                              "pattern": r"^(?:0[0-9]{9}|[+]84[0-9]{9})$",
                              "message": "Số điện thoại không hợp lệ"},
                             "Số điện thoại không hợp lệ")
            ]),
            questions=[FormQuestion(id="driver_phone", title="Phone", type=QuestionType.TEXT)],
        )
        is_valid, err = run_checks_sync(schema, answers)
        assert is_valid is valid
        assert (err is None) is valid

    def test_payload_boundary_exponential_income_figure(self, numeric_schema):
        """
        Test exponential income figure ('1e8' = 100,000,000).
        The configured maximum is 50,000,000; exponential notation must not bypass it.
        """
        answers = {
            "driver_signature": "data:image/png;base64,mock",
            "consent_choice": "Tôi đồng ý",
            "driver_phone": "0988123456",
            "driver_name": "Test Driver",
            "license_plate": "29E-123.45",
            "vehicle_type": "Ô tô điện VinFast (GSM)",
            "car_model": "VF 5 Plus",
            "gross_revenue": "1e8",
            "odometer_km": "50",
            "camera_receipt": "uploaded",
        }
        is_valid, err = run_checks_sync(numeric_schema, answers)
        assert is_valid is False
        assert "Doanh thu ngoài khoảng cho phép" in err
    @pytest.mark.parametrize("value", ["NaN", float("nan"), "inf", "-inf", {}, 10 ** 400])
    @pytest.mark.parametrize("bounded", [False, True])
    def test_payload_boundary_nan_string_behavior(self, numeric_schema, value, bounded):
        """
        Exploratory Finding / Edge Case:
        When a number field receives 'NaN', float('NaN') succeeds without ValueError,
        but comparisons `nan < min_value` and `nan > max_value` both evaluate to False in Python!
        Verify the current behavior and document this boundary edge case.
        """
        answers = {
            "driver_signature": "data:image/png;base64,mock",
            "consent_choice": "Tôi đồng ý",
            "driver_phone": "0988123456",
            "driver_name": "Test Driver",
            "license_plate": "29E-123.45",
            "vehicle_type": "Ô tô điện VinFast (GSM)",
            "car_model": "VF 5 Plus",
            "gross_revenue": value,
            "odometer_km": "50",
            "camera_receipt": "uploaded",
        }
        if not bounded:
            numeric_schema.settings.modules = []
        is_valid, err = validate_submission_against_schema(numeric_schema, answers)
        assert is_valid is False
        assert err
    def test_form_engine_conditional_cyclic_dependencies(self):
        """
        Test schema conditional logic with cyclic dependencies (Q1 depends on Q2, Q2 depends on Q1).
        Verifies that form schema construction rejects cyclic/forward dependencies upfront.
        """
        q1 = FormQuestion(
            id="q_alpha",
            title="Question Alpha",
            type=QuestionType.TEXT,
            required=True,
            conditional=ConditionalRule(depends_on_question_id="q_beta", operator="equals", expected_value="val_beta"),
        )
        q2 = FormQuestion(
            id="q_beta",
            title="Question Beta",
            type=QuestionType.TEXT,
            required=True,
            conditional=ConditionalRule(depends_on_question_id="q_alpha", operator="equals", expected_value="val_alpha"),
        )
        with pytest.raises(ValueError) as excinfo:
            FormSchema(
                form_id="test_cyclic_form",
                title="Cyclic Conditional Form",
                questions=[q1, q2],
            )
        assert "phụ thuộc vào câu hỏi chưa được khai báo trước đó" in str(excinfo.value)
    def test_form_engine_deeply_chained_conditional_dependencies(self):
        """
        Test deeply chained conditional logic:
        Q1 -> Q2 -> Q3 -> ... -> Q50 (each depending on the previous question answering 'step_ok').
        Verifies deterministic resolution and zero stack recursion errors.
        """
        depth = 50
        questions = []
        for i in range(1, depth + 1):
            cond = (
                ConditionalRule(
                    depends_on_question_id=f"q_{i-1}",
                    operator="equals",
                    expected_value="step_ok",
                )
                if i > 1
                else None
            )
            questions.append(
                FormQuestion(
                    id=f"q_{i}",
                    title=f"Question Step {i}",
                    type=QuestionType.TEXT,
                    required=True,
                    conditional=cond,
                )
            )

        chained_schema = FormSchema(
            form_id="deep_chained_form",
            title="Deeply Chained Form",
            questions=questions,
        )

        # Incomplete chain: stopped at step 10 without answering Q11
        answers_incomplete = {f"q_{i}": "step_ok" for i in range(1, 11)}
        is_valid, err = validate_submission_against_schema(chained_schema, answers_incomplete)
        assert is_valid is False
        assert "Question Step 11" in (err or "")

        # Complete chain: all 50 steps answered
        answers_complete = {f"q_{i}": "step_ok" for i in range(1, depth + 1)}
        is_valid_full, err_full = validate_submission_against_schema(chained_schema, answers_complete)
        assert is_valid_full is True
        assert err_full is None


# ==============================================================================
# TEAM 4 (DECREE 13 LEGALITY & CONSENT ENFORCEMENT)
# ==============================================================================
class TestTeam4Decree13LegalityAndConsent:
    """
    Team 4: Decree 13 Legality & Consent Enforcement.
    - Choosing consent dissent ('Tôi không đồng ý') returns status REJECTED upfront
      with legal warning, without vision AI calls or database submission records.
    - scrub_driver_pii wipes all driver identifiers from driver_identity_vault
      and deletes vehicle associations from driver_registered_vehicles.
    """

    @pytest.mark.parametrize("gate_id", ["consent_choice", "privacy_choice", None])
    @pytest.mark.parametrize("test_mode", [False, True])
    def test_consent_dissent_immediate_rejection_no_ai_no_db(self, monkeypatch, tmp_path, gate_id, test_mode):
        """
        Verify that consent dissent ('Tôi không đồng ý'):
        1. Returns status REJECTED upfront.
        2. Includes the legal warning in rejection reason: 'Nghị định 13'.
        3. Never calls vision AI (vision_client.extract_facts).
        4. In production mode with db_pool, exploratory audit probes whether
           the rejected submission writes PII (driver_phone) to survey_submissions.
        """
        client = TestClient(app)
        sharp_bytes = make_sharp_image_bytes()
        if gate_id:
            schema = FormSchema(form_id="wave3_consent", title="Consent contract", questions=[
                FormQuestion(id=gate_id, title="Consent", type=QuestionType.SINGLE_CHOICE,
                             is_consent_gate=True, consent_accept_values=["Tôi đồng ý"]),
            ])
            monkeypatch.setitem(main.FORM_REGISTRY, schema.form_id, schema)
        from services import submission_pipeline as pipeline
        monkeypatch.setattr("routers.survey.SUBMISSION_PHOTOS_DIR", str(tmp_path))

        # Mock vision client and database pool
        mock_db_pool = MagicMock()
        mock_conn = MagicMock()
        mock_conn.commit = AsyncMock()
        mock_cursor = AsyncMock()
        mock_db_pool.connection.return_value.__aenter__.return_value = mock_conn
        mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor

        payload = {
            **({"form_id": schema.form_id} if gate_id else {}),
            "driver_phone": "0988999888",
            "driver_name": "Dissenting Driver",
            gate_id or "consent_choice": "Tôi không đồng ý",
            "signature_png": "data:image/png;base64,mock",
            "is_test_mode": str(test_mode).lower(),
            "bypass_img_check": str(test_mode).lower(),
        }

        with patch.object(main, "db_pool", mock_db_pool), patch.object(
            main.vision_client, "extract_facts", new_callable=AsyncMock
        ) as mock_vision, patch.object(pipeline, "_stage_photos") as stage, patch.object(
            pipeline.embedding_executor, "run_inference", new_callable=AsyncMock
        ) as inference, patch("routers.survey.append_dev_submission") as append:

            resp = client.post(
                "/api/v1/survey/submit-webrtc",
                data=payload,
                files={"survey_photo": ("capture.jpg", io.BytesIO(sharp_bytes), "image/jpeg")},
            )

            # 1 & 2: REJECTED upfront with Decree 13 warning
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "REJECTED"
            assert "Nghị định 13" in data["reason"]

            # 3: Vision AI call must NOT be triggered
            mock_vision.assert_not_called()

            mock_db_pool.connection.assert_not_called()
            stage.assert_not_called()
            inference.assert_not_awaited()
            append.assert_not_called()
            assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_scrub_driver_pii_vault_and_vehicles_erasure(self):
        """
        Verify scrub_driver_pii under Decree 13 Article 16 (Right to Erasure):
        1. Wipes driver identifiers from driver_identity_vault (sets driver_phone & driver_name to '[SCRUBBED_DECREE13]').
        2. Deletes vehicle associations from driver_registered_vehicles.
        3. Anonymizes survey_submissions and vehicle_change_history.
        4. Logs audit hop for RIGHT_TO_ERASURE with tamper-evident seal.
        """
        target_phone = "0988777666"
        target_driver_id = "DRV-DECREE13-TEST"

        mock_db_pool = MagicMock()
        mock_conn = MagicMock()
        mock_conn.commit = AsyncMock()
        mock_cursor = AsyncMock()
        mock_db_pool.connection.return_value.__aenter__.return_value = mock_conn
        mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor

        # Mock driver_id resolution in identity vault
        mock_cursor.fetchone.return_value = (target_driver_id,)

        result = await scrub_driver_pii(mock_db_pool, target_phone)

        assert result["status"] == "success"
        assert result["driver_id"] == target_driver_id
        assert "audit_seal" in result
        assert "Nghị định 13" in result["message"]

        # Inspect all executed queries
        executed_queries = [call_args[0][0] for call_args in mock_cursor.execute.call_args_list]

        # 1. Identity vault must be scrubbed
        vault_query = next((q for q in executed_queries if "driver_identity_vault" in q and "UPDATE" in q), None)
        assert vault_query is not None, "driver_identity_vault was not updated!"
        assert "driver_phone = '[SCRUBBED_DECREE13]'" in vault_query
        assert "driver_name = '[SCRUBBED_DECREE13]'" in vault_query
        # 2. Registered vehicles must be DELETED
        vehicle_query = next((q for q in executed_queries if "driver_registered_vehicles" in q and "DELETE" in q), None)
        assert vehicle_query is not None, "driver_registered_vehicles was not deleted!"
        assert "DELETE FROM driver_registered_vehicles" in vehicle_query
        assert "WHERE driver_phone = %s" in vehicle_query
        survey_query = next((q for q in executed_queries if "survey_submissions" in q and "UPDATE" in q), None)
        assert survey_query is not None
        assert "driver_phone = NULL" in survey_query

        # 4. system_audit_log entry recorded
        audit_query = next((q for q in executed_queries if "system_audit_log" in q and "INSERT" in q), None)
        assert audit_query is not None
        assert "HOP_RIGHT_TO_ERASURE" in audit_query
