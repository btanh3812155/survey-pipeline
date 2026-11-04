"""
Automated Adversarial Security Test Suite - Round 3
Target: Survey Verification Pipeline (/home/ebylxn/projects/survey-pipeline)

Focus Areas:
1. Probe /api/v1/dev/submissions (unauthenticated data leakage of driver phone/revenue, unauthorized DELETE wipe).
2. Probe /api/v1/forms/upload-schema for path traversal via schema.form_id (e.g. attempting "../evil_form").
3. Probe /api/v1/survey/submit-webrtc for vehicle profile hijacking (attacker submitting victim's phone number with a bogus plate).
4. Probe /api/v1/survey/submit-webrtc for client-side validation bypass (submitting values that violate schema constraints directly to the API).
5. Probe ReDoS patterns against schema validation in services/form_engine.py.
"""

import os
import io
import re
import json
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from PIL import Image, ImageDraw
from fastapi.testclient import TestClient

import main
from main import app, DEV_STORAGE_FILE, FORMS_STORAGE_DIR, FORM_REGISTRY, append_dev_submission
from services.form_engine import (
    FormSchema,
    FormQuestion,
    QuestionType,
    ValidationRule,
    ConditionalRule,
    FormSettings,
    DEFAULT_TRUST_BRIDGE_SCHEMA,
    validate_submission_against_schema,
)
from services.vehicle_tracker import (
    VehiclePersistenceTracker,
    VehicleMatchResult,
)


# ==============================================================================
# FIXTURES & TEST HELPERS
# ==============================================================================

@pytest.fixture
def test_client():
    """FastAPI TestClient fixture."""
    return TestClient(app)


@pytest.fixture
def sharp_image_bytes():
    """Generates a high-contrast sharp JPEG to pass Laplacian blur checks."""
    img = Image.new("RGB", (600, 800), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for i in range(100, 700, 20):
        draw.line([(50, i), (550, i)], fill=(0, 0, 0), width=4)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def clean_dev_storage():
    """Ensures dev storage is isolated and cleaned up around tests."""
    backup = None
    if os.path.exists(DEV_STORAGE_FILE):
        try:
            with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                backup = f.read()
        except Exception:
            backup = None

    yield

    if backup is not None:
        try:
            with open(DEV_STORAGE_FILE, "w", encoding="utf-8") as f:
                f.write(backup)
        except Exception:
            pass
    elif os.path.exists(DEV_STORAGE_FILE):
        try:
            os.remove(DEV_STORAGE_FILE)
        except Exception:
            pass


# ==============================================================================
# 1. DEV SUBMISSIONS LEAKAGE & UNAUTHORIZED WIPE (VULN-AUTH-04)
# ==============================================================================

class TestDevSubmissionsVulnerabilities:
    """
    HARDENED GUARD: VULN-AUTH-04 (Admin Auth Enforcement on Dev Submissions)
    Target: GET /api/v1/dev/submissions and DELETE /api/v1/dev/submissions
    
    Both developer endpoints must strictly require administrative authentication
    via verify_admin_auth (HTTP Basic Auth). Unauthenticated calls are rejected with 401.
    """

    ADMIN_AUTH = ("admin", "Legendofzeus2@")

    def test_unauthenticated_get_dev_submissions_rejected_with_401(self, test_client):
        """
        SECURITY ASSERTION: Unauthenticated GET /api/v1/dev/submissions returns 401.
        Authenticated GET with admin credentials succeeds with 200.
        """
        # 1. Unauthenticated request must return 401 Unauthorized
        response = test_client.get("/api/v1/dev/submissions")
        assert response.status_code == 401, f"Expected 401 Unauthorized, got {response.status_code}"

        # 2. Authenticated request with valid admin credentials succeeds
        auth_resp = test_client.get("/api/v1/dev/submissions", auth=self.ADMIN_AUTH)
        assert auth_resp.status_code == 200
        assert "submissions" in auth_resp.json()

    def test_unauthenticated_delete_dev_submissions_rejected_with_401(self, test_client):
        """
        SECURITY ASSERTION: Unauthenticated DELETE /api/v1/dev/submissions returns 401.
        Authenticated DELETE with admin credentials succeeds and purges records.
        """
        # 1. Unauthenticated request must return 401
        del_unauth = test_client.delete("/api/v1/dev/submissions")
        assert del_unauth.status_code == 401, f"Expected 401 Unauthorized, got {del_unauth.status_code}"

        # 2. Authenticated request succeeds
        del_auth = test_client.delete("/api/v1/dev/submissions", auth=self.ADMIN_AUTH)
        assert del_auth.status_code == 200
        assert del_auth.json()["status"] == "cleared"

    def test_dev_submissions_rejects_invalid_auth_credentials(self, test_client):
        """
        SECURITY ASSERTION: Bogus or forged authentication headers are rejected with 401.
        """
        for bad_header in [
            {"Authorization": "Basic invalid_base64_payload"},
            {"Authorization": "Bearer totally_fake_token_12345"},
            {"Authorization": "Basic YmFkdXNlcjpiYWRwYXNz"},
        ]:
            resp = test_client.get("/api/v1/dev/submissions", headers=bad_header)
            assert resp.status_code == 401, f"Expected 401 for bad header {bad_header}"

            del_resp = test_client.delete("/api/v1/dev/submissions", headers=bad_header)
            assert del_resp.status_code == 401, f"Expected 401 for bad header {bad_header}"

# ==============================================================================
# 2. FORM SCHEMA PATH TRAVERSAL (VULN-FS-01)
# ==============================================================================

class TestFormSchemaPathTraversal:
    """
    VULNERABILITY: VULN-FS-01 (Path Traversal in Form Schema Persistence)
    Target: POST /api/v1/forms/upload-schema
    
    The schema persistence logic builds paths via:
        schema_path = os.path.join(FORMS_STORAGE_DIR, f"{schema.form_id}.json")
    Because schema.form_id lacks slug validation or directory containment checks,
    an attacker can supply '../evil_form' to traverse outside the designated forms directory.
    """

    ADMIN_AUTH = ("admin", "Legendofzeus2@")

    def test_upload_schema_path_traversal_parent_escape(self, test_client):
        """
        SECURITY ASSERTION: Uploading a schema with form_id containing path traversal
        (e.g. '../evil_traversal_schema') is rejected with 422 (Pydantic pattern)
        or 400 (canonical path check). No file is written outside FORMS_STORAGE_DIR.
        """
        traversal_form_id = "../evil_traversal_schema"
        expected_escape_file = os.path.abspath(
            os.path.join(FORMS_STORAGE_DIR, f"{traversal_form_id}.json")
        )

        schema_payload = {
            "form_id": traversal_form_id,
            "title": "Exploit Path Traversal Form",
            "description": "Payload attempting parent directory traversal",
            "settings": {
                "access_control": "public",
                "allow_anonymous": True,
            },
            "questions": [
                {
                    "id": "q1",
                    "title": "Injected Question",
                    "type": "text",
                    "required": False,
                }
            ],
        }

        resp = test_client.post(
            "/api/v1/forms/upload-schema",
            json=schema_payload,
            auth=self.ADMIN_AUTH,
        )

        assert resp.status_code in (400, 422), f"Expected 400/422 rejection, got {resp.status_code}"
        assert not os.path.exists(expected_escape_file), "Escaped schema file must NOT be written to disk!"

    def test_upload_schema_deep_path_traversal_relative_parent(self, test_client):
        """
        SECURITY ASSERTION: Multi-level path traversal attempts are rejected.
        """
        deep_form_id = "../../deep_evil_escape"
        target_path = os.path.abspath(os.path.join(FORMS_STORAGE_DIR, f"{deep_form_id}.json"))

        schema_payload = {
            "form_id": deep_form_id,
            "title": "Deep Traversal Test",
            "questions": [
                {
                    "id": "q_deep",
                    "title": "Deep Question",
                    "type": "text",
                    "required": False,
                }
            ],
        }

        resp = test_client.post(
            "/api/v1/forms/upload-schema",
            json=schema_payload,
            auth=self.ADMIN_AUTH,
        )
        assert resp.status_code in (400, 422)
        assert not os.path.exists(target_path)

    def test_form_id_slug_validation_enforced_in_pydantic_model(self):
        """
        SECURITY ASSERTION: FormSchema enforces strict regex slug pattern ^[a-zA-Z0-9_-]{1,64}$.
        Instantiating FormSchema with path separators or null bytes raises ValidationError.
        """
        from pydantic import ValidationError
        malicious_ids = [
            "../traversal",
            "../../etc/passwd",
            "dir/sub/nested_form",
            "form\x00nullbyte",
            "a" * 65,  # Exceeds 64 chars
            "invalid space slug",
        ]
        for mid in malicious_ids:
            with pytest.raises(ValidationError):
                FormSchema(
                    form_id=mid,
                    title="Validation Test",
                    questions=[]
                )

# ==============================================================================
# 3. WEBRTC VEHICLE PROFILE HIJACKING (VULN-AUTH-05)
# ==============================================================================

class TestWebRTCVehicleProfileHijacking:
    """
    VULNERABILITY: VULN-AUTH-05 (Unauthenticated Driver Vehicle Profile Overwrite)
    Target: POST /api/v1/survey/submit-webrtc
    
    The backend executes an unauthenticated upsert into driver_registered_vehicles:
        INSERT INTO driver_registered_vehicles (driver_phone, license_plate, vehicle_type)
        VALUES (%s, %s, %s)
        ON CONFLICT (driver_phone) DO UPDATE 
        SET license_plate = EXCLUDED.license_plate,
            vehicle_type = EXCLUDED.vehicle_type;
    An adversary submitting an arbitrary victim's phone number with a bogus plate
    unconditionally overwrites the registered vehicle profile in the database.
    """

    def test_unauthenticated_vehicle_profile_hijacking_db_upsert(self, test_client, sharp_image_bytes):
        """
        EXPLOIT: Attacker submits victim's phone number ('0988111222') with fraudulent plate ('99X-888.88').
        Vulnerability: No authentication or ownership verification of driver_phone.
        Impact: Overwrites victim's registered vehicle record in driver_registered_vehicles.
        """
        victim_phone = "0988111222"
        attacker_bogus_plate = "99X-888.88"
        attacker_vehicle_type = "TRUCK"

        mock_db_pool = MagicMock()
        mock_conn = MagicMock()
        mock_conn.commit = AsyncMock()
        mock_cursor = AsyncMock()
        mock_db_pool.connection.return_value.__aenter__.return_value = mock_conn
        mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor

        with patch.object(main, "db_pool", mock_db_pool):
            # Unauthenticated submission directly targeting victim's phone
            resp = test_client.post(
                "/api/v1/survey/submit-webrtc",
                data={
                    "driver_phone": victim_phone,
                    "license_plate": attacker_bogus_plate,
                    "vehicle_type": attacker_vehicle_type,
                    "is_test_mode": "false",
                    "gross_revenue": "500000",
                    "consent_choice": "Tôi đồng ý",
                    "signature_png": "data:image/png;base64,mock",
                    "driver_name": "Tài xế Nạn Nhân",
                    "platform_app": "Grab",
                    "vehicle_category": "Xe máy 2 bánh",
                    "working_hours_day": "8 - 12 tiếng (Toàn thời gian)",
                    "daily_net_income": "500000",
                },
                files={"survey_photo": ("capture.jpg", io.BytesIO(sharp_image_bytes), "image/jpeg")},
            )

        assert resp.status_code == 200

        # Inspect database queries executed
        executed_queries = [call[0] for call in mock_cursor.execute.call_args_list]
        upsert_call = next(
            (call for call in executed_queries if "driver_registered_vehicles" in call[0]),
            None
        )
        assert upsert_call is not None, "Expected vehicle upsert query to be executed"

        sql_query, sql_params = upsert_call[0], upsert_call[1]
        assert "ON CONFLICT (driver_phone) DO UPDATE" in sql_query

        # Verify attacker's plate was injected against the victim's phone
        assert sql_params[0] == victim_phone, "Victim phone targeted in DB upsert"
        assert sql_params[1] == attacker_bogus_plate.upper(), "Victim's plate overwritten with attacker plate"
        assert sql_params[2] == attacker_vehicle_type, "Victim's vehicle type overwritten"

    @pytest.mark.asyncio
    async def test_vehicle_hijack_causes_victim_compliance_rejection(self):
        """
        END-TO-END IMPACT: Simulates the cascading audit failure.
        After an attacker hijacks the victim's profile to '99X-888.88',
        the victim's legitimate subsequent survey with real plate '29E-123.45'
        is flagged as VEHICLE_MISMATCH and rejected.
        """
        mock_db_pool = MagicMock()
        mock_conn = MagicMock()
        mock_conn.commit = AsyncMock()
        mock_cursor = AsyncMock()
        mock_db_pool.connection.return_value.__aenter__.return_value = mock_conn
        mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (
            "99X-888.88",
            "TRUCK",
            "BLACK",
        )
        tracker = VehiclePersistenceTracker(db_pool=mock_db_pool)

        # Victim submits genuine survey with legitimate vehicle plate
        victim_real_plate = "29E-123.45"
        victim_phone = "0988111222"

        result = await tracker.verify_and_persist_vehicle(
            driver_phone=victim_phone,
            detected_plate=victim_real_plate,
            detected_vehicle_type="CAR",
        )

        # Audit rejected due to hijacked registration
        assert result.is_match is False
        assert result.status_code == "VEHICLE_UNREGISTERED"
        assert "99X-888.88" in result.flag_reason
        assert "29E-123.45" in result.flag_reason

# ==============================================================================
# 4. WEBRTC CLIENT-SIDE VALIDATION BYPASS (VULN-API-01)
# ==============================================================================

class TestWebRTCClientSideValidationBypass:
    """
    VULNERABILITY: VULN-API-01 (Missing Server-Side Schema Enforcement on WebRTC)
    Target: POST /api/v1/survey/submit-webrtc
    
    Validation rules defined in FormSchema (min/max numeric limits, required fields,
    regex patterns for phone numbers) are only enforced by frontend JavaScript.
    The submit_webrtc_survey endpoint accepts raw form fields directly without calling
    validate_submission_against_schema(), allowing adversaries to bypass all constraints.
    """

    def test_webrtc_rejects_numeric_range_violations(self, test_client, sharp_image_bytes):
        """
        SECURITY ASSERTION: Negative revenue and out-of-range figures are rejected
        by server-side validate_submission_against_schema().
        """
        # 1. Negative revenue rejection
        resp_neg = test_client.post(
            "/api/v1/survey/submit-webrtc",
            data={
                "driver_phone": "0988123456",
                "gross_revenue": "-99999999",
                "trip_count": "10",
                "odometer_km": "50",
                "is_test_mode": "true",
                "consent_choice": "Tôi đồng ý",
                "signature_png": "data:image/png;base64,mock",
                "driver_name": "Tài xế Test",
                "platform_app": "Grab",
                "vehicle_category": "Xe máy 2 bánh",
                "working_hours_day": "8 - 12 tiếng (Toàn thời gian)",
            },
            files={"survey_photo": ("test.jpg", io.BytesIO(sharp_image_bytes), "image/jpeg")},
        )
        assert resp_neg.status_code == 200
        neg_data = resp_neg.json()
        assert neg_data["status"] == "REJECTED"
        assert "không được nhỏ hơn 0" in neg_data["reason"]

        # 2. Astronomical revenue rejection (exceeds schema maximum limit)
        resp_astron = test_client.post(
            "/api/v1/survey/submit-webrtc",
            data={
                "driver_phone": "0988123456",
                "gross_revenue": "999999999999999",
                "trip_count": "10",
                "is_test_mode": "true",
                "consent_choice": "Tôi đồng ý",
                "signature_png": "data:image/png;base64,mock",
                "driver_name": "Tài xế Test",
                "platform_app": "Grab",
                "vehicle_category": "Xe máy 2 bánh",
                "working_hours_day": "8 - 12 tiếng (Toàn thời gian)",
            },
            files={"survey_photo": ("test.jpg", io.BytesIO(sharp_image_bytes), "image/jpeg")},
        )
        assert resp_astron.status_code == 200
        assert resp_astron.json()["status"] == "REJECTED"
        assert "không được lớn hơn 10000000" in resp_astron.json()["reason"]

    def test_webrtc_rejects_driver_phone_regex_violations(self, test_client, sharp_image_bytes):
        """
        SECURITY ASSERTION: Non-numeric phone strings violating regex ^0[0-9]{9,10}$
        are rejected by server-side schema validation.
        """
        invalid_phone_payloads = [
            "NOT_A_PHONE_NUMBER",
            "<script>alert(1)</script>",
            "123",
            "++84999999999999999999",
        ]
        for bad_phone in invalid_phone_payloads:
            resp = test_client.post(
                "/api/v1/survey/submit-webrtc",
                data={
                    "driver_phone": bad_phone,
                    "is_test_mode": "true",
                    "consent_choice": "Tôi đồng ý",
                    "signature_png": "data:image/png;base64,mock",
                    "driver_name": "Tài xế Test",
                    "platform_app": "Grab",
                    "vehicle_category": "Xe máy 2 bánh",
                    "working_hours_day": "8 - 12 tiếng (Toàn thời gian)",
                    "gross_revenue": "500000",
                },
                files={"survey_photo": ("test.jpg", io.BytesIO(sharp_image_bytes), "image/jpeg")},
            )
            assert resp.status_code == 200
            res_json = resp.json()
            assert res_json["status"] == "REJECTED"
            assert "Số điện thoại" in res_json["reason"]

    def test_webrtc_rejects_missing_mandatory_signature_and_consent(self, test_client, sharp_image_bytes):
        """
        SECURITY ASSERTION: Submissions omitting mandatory signature or consent
        are rejected with status REJECTED.
        """
        resp = test_client.post(
            "/api/v1/survey/submit-webrtc",
            data={
                "driver_phone": "0988123456",
                "is_test_mode": "true",
                # signature_png omitted, consent choice omitted
            },
            files={"survey_photo": ("test.jpg", io.BytesIO(sharp_image_bytes), "image/jpeg")},
        )
        assert resp.status_code == 200
        res_json = resp.json()
        assert res_json["status"] == "REJECTED"
        assert "Vui lòng trả lời câu hỏi bắt buộc" in res_json["reason"]
    def test_schema_engine_contrast_proves_missing_server_validation(self):
        """
        Demonstrates that services.form_engine.validate_submission_against_schema()
        DOES catch these exact adversarial payloads, proving that the vulnerability
        is the failure of main.py to invoke the validator on incoming requests.
        """
        base_valid_answers = {
            "driver_signature": "data:image/png;base64,mock",
            "consent_choice": "Tôi đồng ý",
            "driver_phone": "0988123456",
            "driver_name": "Nguyễn Văn A",
            "platform_app": "Grab",
            "vehicle_category": "Xe máy 2 bánh",
            "working_hours_day": "8 - 12 tiếng (Toàn thời gian)",
            "daily_net_income": 650000,
            "monthly_gross_revenue": 18500000,
            "camera_receipt": "data:image/jpeg;base64,mock",
        }

        # 1. Missing signature is caught
        no_sig = dict(base_valid_answers, driver_signature=None)
        valid_sig, err_sig = validate_submission_against_schema(
            DEFAULT_TRUST_BRIDGE_SCHEMA,
            no_sig,
        )
        assert valid_sig is False
        assert "Chữ ký" in err_sig

        # 2. Invalid phone format is caught
        bad_phone = dict(base_valid_answers, driver_phone="INVALID_ALPHA_PHONE")
        valid_phone, err_phone = validate_submission_against_schema(
            DEFAULT_TRUST_BRIDGE_SCHEMA,
            bad_phone,
        )
        assert valid_phone is False
        assert "Số điện thoại" in err_phone

        # 3. Negative net income is caught
        neg_income = dict(base_valid_answers, daily_net_income=-99999999)
        valid_income, err_income = validate_submission_against_schema(
            DEFAULT_TRUST_BRIDGE_SCHEMA,
            neg_income,
        )
        assert valid_income is False
        assert "nhỏ hơn 0" in err_income

        # 4. Out of bounds revenue is caught
        astron_rev = dict(base_valid_answers, monthly_gross_revenue=999999999999999)
        valid_rev, err_rev = validate_submission_against_schema(
            DEFAULT_TRUST_BRIDGE_SCHEMA,
            astron_rev,
        )
        assert valid_rev is False
        assert "lớn hơn 100000000" in err_rev
# ==============================================================================

class TestFormEngineReDoSAttacks:
    """
    VULNERABILITY: VULN-DOS-01 (Regular Expression Denial of Service)
    Target: services/form_engine.py:88-91 and POST /api/v1/forms/upload-schema
    
    The schema validation engine uses Python's standard backtracking 're' module:
        if q.validation.regex_pattern:
            if not re.match(q.validation.regex_pattern, val_str):
    Custom form schemas accept arbitrary un-sanitized regex patterns.
    Pathological patterns with nested quantifiers (e.g. (a+)+$) or overlapping alternations
    cause exponential backtracking when evaluated against non-matching strings,
    locking the synchronous event loop and denying service to all concurrent users.
    """

    def test_redos_nested_quantifiers_catastrophic_backtracking(self):
        """
        EXPLOIT: Nested quantifier pattern r'^(a+)+$' with payload 'a'*N + '!'.
        Vulnerability: O(2^N) state space search in backtracking NFA.
        Impact: Rapid CPU saturation and thread freeze.
        """
        evil_pattern = r"^(a+)+$"
        schema = FormSchema(
            form_id="redos_nested_quantifier",
            title="ReDoS Nested Quantifier Test",
            questions=[
                FormQuestion(
                    id="q_redos",
                    title="Evil Regex Question",
                    type=QuestionType.TEXT,
                    required=True,
                    validation=ValidationRule(regex_pattern=evil_pattern)
                )
            ]
        )

        # Baseline: matching input evaluates near-instantaneously (< 1ms)
        t0 = time.perf_counter()
        valid_match, _ = validate_submission_against_schema(schema, {"q_redos": "a" * 20})
        t_match = time.perf_counter() - t0
        assert valid_match is True
        assert t_match < 0.01

        # Adversarial payload: 22 'a's followed by mismatch character '!'
        # 2^22 states causes significant backtracking latency
        adversarial_input = "a" * 22 + "!"
        t1 = time.perf_counter()
        valid_evil, _ = validate_submission_against_schema(schema, {"q_redos": adversarial_input})
        t_evil = time.perf_counter() - t1

        assert valid_evil is False
        # Catastrophic backtracking demonstrates super-linear growth (> 20x slower than matching)
        assert t_evil > t_match * 20, (
            f"Expected catastrophic backtracking delay: match={t_match:.5f}s, evil={t_evil:.5f}s"
        )

    def test_redos_overlapping_alternation_backtracking(self):
        """
        EXPLOIT: Overlapping alternation pattern r'^(a|aa)+$' with non-matching suffix.
        Vulnerability: Ambiguous branch resolution causes combinatorial exploration.
        """
        evil_pattern = r"^(a|aa)+$"
        schema = FormSchema(
            form_id="redos_alternation",
            title="ReDoS Alternation Test",
            questions=[
                FormQuestion(
                    id="q_alt",
                    title="Alternation Question",
                    type=QuestionType.TEXT,
                    required=True,
                    validation=ValidationRule(regex_pattern=evil_pattern)
                )
            ]
        )

        # Matching input
        t0 = time.perf_counter()
        res_match, _ = validate_submission_against_schema(schema, {"q_alt": "a" * 22})
        dur_match = time.perf_counter() - t0
        assert res_match is True

        # Non-matching input
        t1 = time.perf_counter()
        res_evil, _ = validate_submission_against_schema(schema, {"q_alt": "a" * 22 + "x"})
        dur_evil = time.perf_counter() - t1
        assert res_evil is False

        assert dur_evil > dur_match * 10, (
            f"Overlapping alternation backtracking confirmed: match={dur_match:.5f}s vs evil={dur_evil:.5f}s"
        )

    def test_schema_validation_unhandled_regex_syntax_error(self):
        """
        EXPLOIT: Uploading an invalid/unclosed regular expression (e.g. r'[a-z').
        Vulnerability: services/form_engine.py does not wrap re.match in try/except re.error.
        Impact: Unhandled internal exception crashes request handler with 500 error.
        """
        invalid_pattern = r"[a-z"  # Unterminated character class
        schema = FormSchema(
            form_id="invalid_regex_schema",
            title="Invalid Regex Crash Test",
            questions=[
                FormQuestion(
                    id="q_crash",
                    title="Syntax Error Question",
                    type=QuestionType.TEXT,
                    required=True,
                    validation=ValidationRule(regex_pattern=invalid_pattern)
                )
            ]
        )

        # Validating input against this schema raises unhandled re.error
        with pytest.raises(re.error):
            validate_submission_against_schema(schema, {"q_crash": "test_input"})

    def test_upload_schema_accepts_redos_pattern_without_linting(self, test_client):
        """
        Validates that /api/v1/forms/upload-schema lacks any pattern complexity linter
        or safe-regex verification before persisting custom schemas.
        """
        admin_auth = ("admin", "Legendofzeus2@")
        redos_schema_payload = {
            "form_id": "survey_with_evil_pattern",
            "title": "Unvetted Regex Survey",
            "questions": [
                {
                    "id": "driver_code",
                    "title": "Pathological Code",
                    "type": "text",
                    "required": True,
                    "validation": {
                        "regex_pattern": r"^(([a-zA-Z0-9])+)+$",
                        "regex_error_message": "Invalid code"
                    }
                }
            ]
        }

        try:
            resp = test_client.post(
                "/api/v1/forms/upload-schema",
                json=redos_schema_payload,
                auth=admin_auth,
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "success"
            assert "survey_with_evil_pattern" in FORM_REGISTRY
        finally:
            schema_file = os.path.join(FORMS_STORAGE_DIR, "survey_with_evil_pattern.json")
            if os.path.exists(schema_file):
                os.remove(schema_file)
            if "survey_with_evil_pattern" in FORM_REGISTRY:
                del FORM_REGISTRY["survey_with_evil_pattern"]
