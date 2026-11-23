import io
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from PIL import Image
from fastapi.testclient import TestClient

import main
from main import app, BoundedTTLCache
from routers import survey
from services import submission_pipeline
from tests.test_outbox_delivery import database
from services.form_engine import (
    FormSchema,
    FormQuestion,
    FormSettings,
    QuestionType,
)
from tests.module_checks import check_module, run_checks_sync

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

    def test_cross_driver_idempotency_isolation_with_scoped_keys(self, monkeypatch, tmp_path):
        monkeypatch.setattr(survey.settings, "ENVIRONMENT", "test")
        monkeypatch.setattr(survey, "get_db_pool", lambda: None)
        monkeypatch.setattr(survey, "get_redis_client", lambda: None)
        monkeypatch.setattr(survey, "LOCAL_IDEMP_CACHE", BoundedTTLCache(max_size=8, ttl_seconds=60))
        monkeypatch.setattr(survey, "SUBMISSION_PHOTOS_DIR", str(tmp_path))
        records = []
        monkeypatch.setattr(survey, "append_dev_submission", records.append)
        monkeypatch.setattr(survey, "_schedule_legacy_submission", lambda *_: None)
        client = TestClient(app)
        image = make_sharp_image_bytes()

        def submit(phone, name):
            return client.post(
                "/api/v1/survey/submit-webrtc",
                data=standard_form_fields(
                    driver_phone=phone, driver_name=name, idempotency_key="shared-submit",
                    is_test_mode="true", bypass_img_check="true",
                ),
                files={"survey_photo": ("capture.jpg", image, "image/jpeg")},
            )

        phone_a, phone_b = "0911111111", "0922222222"
        name_a, name_b = "Tài Xế A Riêng Tư", "Tài Xế B Độc Lập"
        first_a, first_b = submit(phone_a, name_a), submit(phone_b, name_b)
        assert first_a.status_code == first_b.status_code == 200
        assert first_a.json()["driver_phone"] == phone_a
        assert first_b.json()["driver_phone"] == phone_b
        assert first_a.json()["submission_id"] != first_b.json()["submission_id"]

        changed = submit(phone_a, "Tên đã thay đổi")
        assert changed.status_code == 409
        replay = submit(phone_a, name_a)
        assert replay.status_code == 200
        assert replay.json() == first_a.json()
        assert len(records) == 2


# ==============================================================================
# NOVEL PROBES & REPLICATION: VULN-AUTH-05 VEHICLE REGISTRATION
# ==============================================================================

@pytest.fixture
def vehicle_client(database, monkeypatch, tmp_path):
    dsn, observer, *_ = database
    monkeypatch.setattr(main.settings, "DATABASE_URL", dsn)
    monkeypatch.setattr(main.settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(main.settings, "DURABLE_WEBRTC_ENABLED", True)
    monkeypatch.setenv("MOCK_AI", "1")
    monkeypatch.setattr(survey, "SUBMISSION_PHOTOS_DIR", str(tmp_path))
    monkeypatch.setattr(submission_pipeline, "compute_dinov2_embedding", lambda _: None)
    monkeypatch.setattr(main, "seed_media_storage", lambda: None)
    monkeypatch.setattr("services.telegram_polling.configure_bot_menus", AsyncMock())
    redis = AsyncMock()
    redis.ping.side_effect = ConnectionError("Redis disabled for this test")
    monkeypatch.setattr(main.aioredis, "from_url", lambda *_args, **_kwargs: redis)

    phone = f"09{uuid4().int % 100_000_000:08d}"
    # The public route uses the lifespan's pool; observe commits from a separate connection.
    try:
        with TestClient(app) as client:
            yield client, observer, phone
    finally:
        observer.execute(
            "DELETE FROM outbox_events WHERE aggregate_id IN "
            "(SELECT submission_id FROM survey_submissions WHERE driver_phone=%s)", (phone,),
        )
        observer.execute("DELETE FROM survey_submissions WHERE driver_phone=%s", (phone,))
        observer.execute("DELETE FROM driver_identity_vault WHERE driver_phone=%s", (phone,))
        observer.execute("DELETE FROM driver_registered_vehicles WHERE driver_phone IN (%s, %s)", (phone, "+84" + phone[1:]))


@pytest.mark.postgres
class TestNovelProbeVehicleRegistrationVULNAUTH05:
    @staticmethod
    def submit(client, phone, plate):
        response = client.post(
            "/api/v1/survey/submit-webrtc",
            data=standard_form_fields(driver_phone=phone, license_plate=plate),
            files={"survey_photo": ("capture.jpg", make_sharp_image_bytes(), "image/jpeg")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["driver_phone"] == phone
        return response.json()

    @pytest.mark.parametrize("legacy_binding", [False, True])
    def test_unauthenticated_attacker_cannot_overwrite_victim_vehicle_record(self, vehicle_client, legacy_binding):
        client, observer, phone = vehicle_client
        binding_phone = "+84" + phone[1:] if legacy_binding else phone
        observer.execute(
            "INSERT INTO driver_registered_vehicles(driver_phone, license_plate, vehicle_type) VALUES (%s, '29E12345', 'BIKE')",
            (binding_phone,),
        )

        result = self.submit(client, phone, "99X-888.88")
        assert result["status"] == "REJECTED"
        assert "29E12345" not in result["reason"]
        assert observer.execute(
            "SELECT driver_phone, license_plate, vehicle_type FROM driver_registered_vehicles "
            "WHERE driver_phone IN (%s, %s)", (phone, binding_phone),
        ).fetchall() == [{"driver_phone": binding_phone, "license_plate": "29E12345", "vehicle_type": "BIKE"}]

    def test_brand_new_driver_initial_vehicle_insert_succeeds(self, vehicle_client):
        client, observer, phone = vehicle_client
        result = self.submit(client, phone, "30H99999")
        assert observer.execute(
            "SELECT driver_phone FROM survey_submissions WHERE submission_id=%s", (result["submission_id"],),
        ).fetchone() == {"driver_phone": phone}
        assert observer.execute(
            "SELECT license_plate, vehicle_type FROM driver_registered_vehicles WHERE driver_phone=%s", (phone,),
        ).fetchone() == {"license_plate": "30H99999", "vehicle_type": "CAR"}


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
        schema = FormSchema(
            form_id="test_redos_schema",
            title="ReDoS Test",
            settings=FormSettings(modules=[
                check_module("phone", "regex", {"field": "phone_input", "pattern": r"^0[0-9]{9}$",
                                                "message": "Số điện thoại không hợp lệ"},
                             "Số điện thoại không hợp lệ")
            ]),
            questions=[FormQuestion(id="phone_input", title="Phone", type=QuestionType.TEXT, required=True)]
        )

        huge_adversarial_input = "0" + ("9" * 10000)
        start_time = time.time()
        is_valid, err = run_checks_sync(schema, {"phone_input": huge_adversarial_input})
        elapsed = time.time() - start_time

        assert elapsed < 1.0
        assert is_valid is False
        assert "Số điện thoại không hợp lệ" in err

    def test_redos_pattern_boundary_resilience(self):
        """
        Tests pattern validation against ReDoS:
        Checks that standard form questions reject non-matching inputs cleanly,
        and 20-22 char inputs against nested quantifiers fail without crashing.
        """
        schema = FormSchema(
            form_id="nested_quantifier_test",
            title="Nested Quantifier",
            settings=FormSettings(modules=[
                check_module("redos", "regex", {"field": "evil_input", "pattern": r"^(a+)+$",
                                                "message": "Pattern failed"}, "Pattern failed")
            ]),
            questions=[FormQuestion(id="evil_input", title="Evil", type=QuestionType.TEXT, required=True)]
        )

        # Matching input
        is_valid, _ = run_checks_sync(schema, {"evil_input": "a" * 10})
        assert is_valid is True

        # Mismatch input
        is_valid_bad, err = run_checks_sync(schema, {"evil_input": "a" * 15 + "!"})
        assert is_valid_bad is False
        assert "Pattern failed" in err
    def test_malformed_regex_syntax_error_recovery(self):
        """
        Validate schema with an invalid / unclosed regex (e.g. r'[a-z(').
        Must cleanly catch re.error and return a localized Vietnamese error message,
        never crashing the application process with unhandled re.error.
        """
        schema = FormSchema(
            form_id="broken_regex_schema",
            title="Broken Regex",
            settings=FormSettings(modules=[
                check_module("broken", "regex", {"field": "broken_field", "pattern": r"[a-z(",
                                                 "message": "Regex error"}, "Regex error")
            ]),
            questions=[FormQuestion(id="broken_field", title="Trường Lỗi", type=QuestionType.TEXT, required=True)]
        )

        is_valid, err = run_checks_sync(schema, {"broken_field": "test_value"})
        assert is_valid is False
        assert "Không thể chạy module" in err
