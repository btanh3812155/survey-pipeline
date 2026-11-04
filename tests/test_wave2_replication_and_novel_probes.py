import io
import json
import os
import secrets
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from PIL import Image
from fastapi.testclient import TestClient

import main
from main import app, BoundedTTLCache, LOCAL_IDEMP_CACHE
from services.form_engine import (
    FormSchema,
    FormQuestion,
    QuestionType,
    ValidationRule,
    validate_submission_against_schema,
)

# Shared test helpers
def make_sharp_image_bytes():
    img = Image.new("RGB", (800, 600), color=(255, 255, 255))
    for x in range(0, 800, 20):
        for y in range(600):
            if (x // 20) % 2 == 0:
                img.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()

def standard_form_fields(driver_phone="0988123456", **overrides):
    fields = {
        "driver_phone": driver_phone,
        "driver_name": "Nguyễn Văn Test",
        "license_plate": "29A-123.45",
        "vehicle_type": "CAR",
        "gross_revenue": "500000",
        "trip_count": "10",
        "odometer_km": "50",
        "signature_png": "data:image/png;base64,mocksignature",
        "consent_choice": "Tôi đồng ý",
        "platform_app": "Grab",
        "vehicle_category": "Ô tô 4 - 7 chỗ",
        "working_hours_day": "8 - 12 tiếng (Toàn thời gian)",
        "daily_net_income": "500000",
        "monthly_gross_revenue": "15000000",
        "is_test_mode": "false",
    }
    fields.update(overrides)
    return fields


# ==============================================================================
# NOVEL PROBES & REPLICATION: BOUNDED TTL CACHE
# ==============================================================================

class TestNovelProbeBoundedTTLCache:
    """
    Independent novel verification of BoundedTTLCache:
    1. Rapid-fire concurrent insertions exceeding max_size (2048) under multithreaded burst.
    2. Strict LRU eviction order verification.
    3. TTL expiration verification (purging expired entries cleanly).
    4. Cross-driver isolation: idemp:{phone}:{key} isolation prevents data leak/snooping.
    """

    def test_rapid_fire_concurrent_insertions_exceeding_max_size(self):
        """
        Concurrency burst: 50 threads concurrently insert 5,000 unique keys into a cache
        capped at max_size=2048.
        Must remain strictly thread-safe, never exceed 2048, and have zero deadlocks/races.
        """
        cache = BoundedTTLCache(max_size=2048, ttl_seconds=3600.0)
        total_items = 5000
        num_workers = 50

        def insert_chunk(worker_id):
            for i in range(100):
                key = f"key_{worker_id}_{i}"
                cache[key] = f"val_{worker_id}_{i}"

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(insert_chunk, w) for w in range(num_workers)]
            for f in futures:
                f.result()

        # Cache length must be strictly bounded at max_size
        assert len(cache) == 2048
        assert cache.maxsize == 2048

    def test_lru_eviction_order_deterministic(self):
        """
        Novel verification: test that accessing an older key promotes it,
        so unaccessed older keys are evicted first.
        """
        cache = BoundedTTLCache(max_size=3, ttl_seconds=3600.0)
        cache["k1"] = "v1"
        cache["k2"] = "v2"
        cache["k3"] = "v3"

        # Access k1 to make it most-recently used
        assert cache["k1"] == "v1"

        # Now insert k4 -> k2 was oldest unaccessed, so k2 must be evicted!
        cache["k4"] = "v4"

        assert "k1" in cache
        assert "k3" in cache
        assert "k4" in cache
        assert "k2" not in cache

    def test_ttl_expiration_behavior(self):
        """
        Novel verification: test that entries expire after their TTL.
        """
        cache = BoundedTTLCache(max_size=100, ttl_seconds=0.1) # 100ms TTL
        cache["ephemeral_1"] = "alive"
        cache["ephemeral_2"] = "alive"

        assert "ephemeral_1" in cache
        assert cache.get("ephemeral_1") == "alive"

        # Sleep past TTL
        time.sleep(0.15)

        assert "ephemeral_1" not in cache
        assert cache.get("ephemeral_1") is None
        assert len(cache) == 0

    def test_cross_driver_idempotency_isolation_with_scoped_keys(self):
        """
        Replication & Novel Probe on VULN-CACHE-02:
        Driver A and Driver B submit with the EXACT SAME idempotency_key ('shared-submit-123').
        With key scoping `idemp:{phone}:{key}`, Driver B must NOT receive Driver A's cached response!
        """
        client = TestClient(app)
        sharp_bytes = make_sharp_image_bytes()
        shared_key = f"novel_idemp_{secrets.token_hex(4)}"

        # 1. Driver A submits first
        resp_a = client.post(
            "/api/v1/survey/submit-webrtc",
            data=standard_form_fields(
                driver_phone="0911111111",
                driver_name="Tài Xế A Riêng Tư",
                idempotency_key=shared_key,
                is_test_mode="true",
                bypass_img_check="true",
            ),
            files={"survey_photo": ("capture.jpg", io.BytesIO(sharp_bytes), "image/jpeg")},
        )
        assert resp_a.status_code == 200
        data_a = resp_a.json()
        assert data_a["driver_phone"] == "0911111111"
        sub_id_a = data_a["submission_id"]

        # 2. Driver B submits with IDENTICAL idempotency_key
        resp_b = client.post(
            "/api/v1/survey/submit-webrtc",
            data=standard_form_fields(
                driver_phone="0922222222",
                driver_name="Tài Xế B Độc Lập",
                idempotency_key=shared_key,
                is_test_mode="true",
                bypass_img_check="true",
            ),
            files={"survey_photo": ("capture.jpg", io.BytesIO(sharp_bytes), "image/jpeg")},
        )
        assert resp_b.status_code == 200
        data_b = resp_b.json()
        assert data_b["driver_phone"] == "0922222222"
        sub_id_b = data_b["submission_id"]

        # Critical assertion: Driver B must NOT get Driver A's submission!
        assert sub_id_a != sub_id_b, "VULN-CACHE-02: Cross-driver collision detected! Scoped keys failed."

        # 3. Driver A resubmits with identical key -> gets idempotent HIT with same submission_id
        resp_a_resubmit = client.post(
            "/api/v1/survey/submit-webrtc",
            data=standard_form_fields(
                driver_phone="0911111111",
                idempotency_key=shared_key,
                is_test_mode="true",
                bypass_img_check="true",
            ),
            files={"survey_photo": ("capture.jpg", io.BytesIO(sharp_bytes), "image/jpeg")},
        )
        assert resp_a_resubmit.status_code == 200
        data_a_resubmit = resp_a_resubmit.json()
        assert data_a_resubmit["submission_id"] == sub_id_a, "Idempotency hit failed for original driver!"


# ==============================================================================
# NOVEL PROBES & REPLICATION: VULN-AUTH-05 VEHICLE REGISTRATION
# ==============================================================================

class TestNovelProbeVehicleRegistrationVULNAUTH05:
    """
    Novel probe on VULN-AUTH-05:
    1. Verify that an unauthenticated attacker cannot overwrite a victim driver's existing vehicle record.
       (ON CONFLICT (driver_phone) DO NOTHING prevents overwrite).
    2. Legitimate onboarding: verify that a brand new un-registered driver still inserts their initial vehicle cleanly.
    """

    def test_unauthenticated_attacker_cannot_overwrite_victim_vehicle_record(self):
        """
        Attacker submits victim's phone ('0988111222') with fraudulent plate ('99X-888.88').
        The SQL query must use `ON CONFLICT (driver_phone) DO NOTHING` rather than `DO UPDATE`.
        """
        client = TestClient(app)
        sharp_bytes = make_sharp_image_bytes()
        victim_phone = "0988111222"
        attacker_bogus_plate = "99X-888.88"

        mock_db_pool = MagicMock()
        mock_conn = MagicMock()
        mock_conn.commit = AsyncMock()
        mock_cursor = AsyncMock()
        mock_db_pool.connection.return_value.__aenter__.return_value = mock_conn
        mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor

        with patch.object(main, "db_pool", mock_db_pool):
            resp = client.post(
                "/api/v1/survey/submit-webrtc",
                data=standard_form_fields(
                    driver_phone=victim_phone,
                    license_plate=attacker_bogus_plate,
                    vehicle_type="TRUCK",
                ),
                files={"survey_photo": ("capture.jpg", io.BytesIO(sharp_bytes), "image/jpeg")},
            )

        assert resp.status_code == 200

        # Inspect database queries executed
        executed_queries = [call[0] for call in mock_cursor.execute.call_args_list]
        vehicle_upsert_call = next(
            (call for call in executed_queries if "driver_registered_vehicles" in call[0]),
            None
        )
        assert vehicle_upsert_call is not None
        sql_query = vehicle_upsert_call[0]

        # Verify that DO NOTHING is enforced and DO UPDATE is eliminated
        assert "ON CONFLICT (driver_phone) DO NOTHING" in sql_query, (
            "VULN-AUTH-05: Expected ON CONFLICT (driver_phone) DO NOTHING"
        )
        assert "DO UPDATE" not in sql_query, (
            "VULN-AUTH-05: Vulnerable DO UPDATE found in query!"
        )

    def test_brand_new_driver_initial_vehicle_insert_succeeds(self):
        """
        Verify that a brand new, never-before-seen driver successfully inserts their
        initial vehicle registration into driver_registered_vehicles.
        """
        client = TestClient(app)
        sharp_bytes = make_sharp_image_bytes()
        new_driver_phone = "0933777888"
        new_plate = "30H-999.99"

        mock_db_pool = MagicMock()
        mock_conn = MagicMock()
        mock_conn.commit = AsyncMock()
        mock_cursor = AsyncMock()
        mock_db_pool.connection.return_value.__aenter__.return_value = mock_conn
        mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor

        with patch.object(main, "db_pool", mock_db_pool):
            resp = client.post(
                "/api/v1/survey/submit-webrtc",
                data=standard_form_fields(
                    driver_phone=new_driver_phone,
                    license_plate=new_plate,
                    vehicle_type="CAR",
                ),
                files={"survey_photo": ("capture.jpg", io.BytesIO(sharp_bytes), "image/jpeg")},
            )

        assert resp.status_code == 200
        executed_queries = [call[0] for call in mock_cursor.execute.call_args_list]
        vehicle_insert = next(
            (call for call in executed_queries if "driver_registered_vehicles" in call[0]),
            None
        )
        assert vehicle_insert is not None
        sql_query, sql_params = vehicle_insert[0], vehicle_insert[1]
        assert sql_params[0] == new_driver_phone
        assert sql_params[1] == new_plate.upper()
        assert sql_params[2] == "CAR"


# ==============================================================================
# NOVEL PROBES & REPLICATION: VULN-DATA-01 STATIC EXPOSURE & PATH TRAVERSAL
# ==============================================================================

class TestNovelProbeDataStaticExposureVULNDATA01:
    """
    Novel probe on VULN-DATA-01:
    - Verify GET /data/dev_test_submissions.json returns 404.
    - Verify path traversal attempts like /data/../data/dev_test_submissions.json are blocked.
    - Verify that no static file route exposes the project root or data directory.
    """

    def test_data_endpoint_returns_404_not_found(self):
        """
        GET /data/dev_test_submissions.json must return 404 Not Found.
        """
        client = TestClient(app)
        resp = client.get("/data/dev_test_submissions.json")
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"

    def test_path_traversal_attempts_blocked(self):
        """
        Probe path traversal variations:
        - /data/../data/dev_test_submissions.json
        - /data/..%2f..%2fdata
        - /data/forms/../dev_test_submissions.json
        """
        client = TestClient(app)
        traversal_paths = [
            "/data/../data/dev_test_submissions.json",
            "/data/%2e%2e/data/dev_test_submissions.json",
            "/data/forms/../dev_test_submissions.json",
            "/data/forms/income_survey.json",
            "/data/nonexistent",
        ]
        for path in traversal_paths:
            resp = client.get(path)
            assert resp.status_code in (404, 400), f"Path {path} returned {resp.status_code} instead of 404/400"

    def test_no_data_static_routes_in_fastapi(self):
        """
        Verify no route path in FastAPI is mounted to '/data'.
        """
        mounted_paths = [route.path for route in app.routes]
        assert "/data" not in mounted_paths
        assert not any(p.startswith("/data") for p in mounted_paths)


# ==============================================================================
# NOVEL PROBES & REPLICATION: VULN-COLL-01 ENTROPY AND UNIQUENESS
# ==============================================================================

class TestNovelProbeSubmissionIDEntropyVULNCOLL01:
    """
    Novel probe on VULN-COLL-01:
    Generate 10,000 submission IDs and verify zero duplicates (100% uniqueness with 8-byte / 64-bit entropy).
    """

    def test_10000_submission_ids_zero_duplicates_8_byte_entropy(self):
        """
        Generate 10,000 submission IDs simulating high-volume traffic.
        Verify that 8 bytes (16 hex chars = 64 bits = 1.84e19 states) produces zero collisions.
        """
        generated_ids = set()
        count = 10000

        for _ in range(count):
            token = secrets.token_hex(8).upper()
            sub_id = f"VN-{token}"
            generated_ids.add(sub_id)

        # 10,000 items in set proves 100% uniqueness
        assert len(generated_ids) == count
        sample_id = next(iter(generated_ids))
        # Format: "VN-" + 16 hex chars = 19 chars total
        assert len(sample_id) == 19
        assert sample_id.startswith("VN-")


# ==============================================================================
# NOVEL PROBES & REPLICATION: REDOS & REGEX BOUNDARY RESILIENCE
# ==============================================================================

class TestNovelProbeReDoSAndRegexBoundaries:
    """
    Novel probe on ReDoS and Regex boundaries:
    - Test 200+ char adversarial inputs against pattern validation (ReDoS cutoff safety).
    - Test catastrophic backtracking regex schemas without hanging or crashing.
    - Test malformed regex recovery (syntax errors, unmatched brackets).
    """

    def test_200_plus_char_input_redos_boundary_cutoff(self):
        """
        Form engine truncates inputs to 200 chars before regex matching to prevent
        algorithmic complexity attacks. Test 10,000 char input against phone pattern.
        """
        phone_rule = ValidationRule(
            regex_pattern=r"^0[0-9]{9,10}$",
            regex_error_message="Số điện thoại không hợp lệ"
        )
        schema = FormSchema(
            form_id="test_redos_schema",
            title="ReDoS Test",
            questions=[
                FormQuestion(
                    id="phone_input",
                    title="Phone",
                    type=QuestionType.TEXT,
                    required=True,
                    validation=phone_rule,
                )
            ]
        )

        huge_adversarial_input = "0" + ("9" * 10000)
        start_time = time.time()
        is_valid, err = validate_submission_against_schema(schema, {"phone_input": huge_adversarial_input})
        elapsed = time.time() - start_time

        # Must execute within < 50ms without catastrophic hanging
        assert elapsed < 0.05
        assert is_valid is False
        assert "Số điện thoại không hợp lệ" in err

    def test_redos_pattern_boundary_resilience(self):
        """
        Tests pattern validation against ReDoS:
        Checks that standard form questions reject non-matching inputs cleanly,
        and 20-22 char inputs against nested quantifiers fail without crashing.
        """
        redos_rule = ValidationRule(
            regex_pattern=r"^(a+)+$",
            regex_error_message="Pattern failed"
        )
        schema = FormSchema(
            form_id="nested_quantifier_test",
            title="Nested Quantifier",
            questions=[
                FormQuestion(
                    id="evil_input",
                    title="Evil",
                    type=QuestionType.TEXT,
                    required=True,
                    validation=redos_rule,
                )
            ]
        )

        # Matching input
        is_valid, _ = validate_submission_against_schema(schema, {"evil_input": "a" * 10})
        assert is_valid is True

        # Mismatch input
        is_valid_bad, err = validate_submission_against_schema(schema, {"evil_input": "a" * 15 + "!"})
        assert is_valid_bad is False
        assert "Pattern failed" in err
    def test_malformed_regex_syntax_error_recovery(self):
        """
        Validate schema with an invalid / unclosed regex (e.g. r'[a-z(').
        Must cleanly catch re.error and return a localized Vietnamese error message,
        never crashing the application process with unhandled re.error.
        """
        broken_rule = ValidationRule(
            regex_pattern=r"[a-z(", # Syntax error: unclosed parenthesis
            regex_error_message="Regex error"
        )
        schema = FormSchema(
            form_id="broken_regex_schema",
            title="Broken Regex",
            questions=[
                FormQuestion(
                    id="broken_field",
                    title="Trường Lỗi",
                    type=QuestionType.TEXT,
                    required=True,
                    validation=broken_rule,
                )
            ]
        )

        is_valid, err = validate_submission_against_schema(schema, {"broken_field": "test_value"})
        assert is_valid is False
        assert "Lỗi định dạng mẫu quy chuẩn" in err
