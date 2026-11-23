"""
Test Suite for Privacy Hashing, PII Pseudonymization, Public History Protection,
and Standalone Telegram Bot Service.
"""

import json
import io
import httpx
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from PIL import Image

from config import settings
from core.embedding_executor import embedding_executor_lifespan
from main import app
from services.privacy_crypto import (
    hash_phone,
    hash_plate,
    generate_driver_tag,
    mask_phone,
    mask_license_plate,
)
from services.vehicle_tracker import (
    VehiclePersistenceTracker,
    normalize_license_plate,
)
from services.telegram_polling import TelegramPollingRunner


# ===========================================================================
# 1. Privacy Hashing & PII Pseudonymization Tests
# ===========================================================================

def test_hash_phone_deterministic_and_salted():
    """Verifies HMAC-SHA256 phone hashing is deterministic and normalized."""
    phone1 = "0988123456"
    phone2 = " 0988-123-456 "
    phone3 = "+84988123456"

    h1 = hash_phone(phone1)
    h2 = hash_phone(phone2)
    h3 = hash_phone(phone3)

    assert isinstance(h1, str) and len(h1) == 64
    assert h1 == h2  # Strips formatting
    assert h1 == h3  # Normalizes +84 to 0
    assert hash_phone("") == ""
    assert hash_phone(None) == ""

    # Distinct phone yields distinct hash
    assert hash_phone("0912345678") != h1


def test_hash_plate_deterministic():
    """Verifies license plate HMAC-SHA256 hashing handles various plate styles."""
    plate1 = "29E-123.45"
    plate2 = "29e12345"
    plate3 = " 29E - 123.45 "

    h1 = hash_plate(plate1)
    h2 = hash_plate(plate2)
    h3 = hash_plate(plate3)

    assert isinstance(h1, str) and len(h1) == 64
    assert h1 == h2 == h3
    assert hash_plate("") == ""
    assert hash_plate(None) == ""


def test_generate_driver_tag():
    """Verifies driver tag generation: DRV- + first 8 characters of phone hash."""
    phone = "0988123456"
    tag = generate_driver_tag(phone)
    phone_hash = hash_phone(phone)

    assert tag.startswith("DRV-")
    assert tag == f"DRV-{phone_hash[:8].upper()}"
    assert len(tag) == 12

    # Empty fallback
    assert generate_driver_tag("") == "DRV-ANON"
    assert generate_driver_tag(None) == "DRV-ANON"


def test_mask_phone():
    """Verifies masking logic for telephone numbers (e.g. 098****456)."""
    assert mask_phone("0988123456") == "098****456"
    assert mask_phone("0912-345-678") == "091****678"
    assert mask_phone("+84988123456") == "+84****456"
    assert mask_phone("12345") == "****"
    assert mask_phone("") == ""
    assert mask_phone(None) == ""


def test_mask_license_plate():
    """Verifies masking logic for Vietnamese license plates (e.g. 29E-***.45)."""
    assert mask_license_plate("29E-123.45") == "29E-***.45"
    assert mask_license_plate("51A-999.88") == "51A-***.88"
    assert mask_license_plate("29AA-012.34") == "29AA-***.34"
    assert mask_license_plate("29E1-123.45") == "29E1-***.45"
    # Normalized input without hyphens
    assert mask_license_plate("29E12345") == "29E-***.45"
    assert mask_license_plate("51H99988") == "51H-***.88"
    assert mask_license_plate("") == ""
    assert mask_license_plate(None) == ""


# ===========================================================================
# 2. Public History Leak Prevention (Privacy Protection) Tests
# ===========================================================================

def test_survey_history_unauthorized_returns_401(monkeypatch, tmp_path):
    """
    Prevents scraping of driver survey histories by unauthenticated outsiders.
    Access to /api/v1/surveys/history without valid credentials MUST return 401 Unauthorized.
    """
    from core.cache import BoundedTTLCache
    from core.passwords import hash_password
    import core.state as state
    from routers import survey as survey_router

    encoded = hash_password("history_test_password", iterations=1_000)

    async def load_admin(username):
        if username == "history_test_admin":
            return {"password_hash": encoded, "is_active": True, "role": "manager", "deleted_at": None}
        return None

    monkeypatch.setattr(state, "_load_admin_account", load_admin)
    monkeypatch.setattr(state, "get_redis_client", lambda: None)
    storage = tmp_path / "dev_submissions.json"
    storage.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(settings, "ADMIN_USERNAME", "history_test_admin")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "history_test_password")
    monkeypatch.setattr(survey_router, "DEV_STORAGE_FILE", str(storage))
    monkeypatch.setattr(survey_router, "get_db_pool", lambda: None)
    monkeypatch.setattr(survey_router, "get_redis_client", lambda: None)
    monkeypatch.setattr(survey_router, "HISTORY_RATE_LIMITER", BoundedTTLCache(max_size=10, ttl_seconds=60))
    client = TestClient(app)

    # 1. Unauthenticated request without Authorization header
    resp = client.get("/api/v1/surveys/history?phone=0988123456")
    assert resp.status_code == 401
    assert "Not authenticated" in resp.text or "401" in str(resp.status_code)

    # 2. Invalid credentials return 401
    bad_auth_resp = client.get(
        "/api/v1/surveys/history?phone=0988123456",
        auth=("attacker", "wrong_password"),
    )
    assert bad_auth_resp.status_code == 401

    # 3. Authenticated admin request succeeds
    admin_auth = (settings.ADMIN_USERNAME, settings.ADMIN_PASSWORD)
    ok_resp = client.get(
        "/api/v1/surveys/history?phone=0988123456",
        auth=admin_auth,
    )
    assert ok_resp.status_code == 200
    assert ok_resp.json()["phone"] == "0988123456"


# ===========================================================================
# 3. Outbox Event & Pseudonymized driver_tag Persistence Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_webrtc_outbox_event_contains_pseudonymized_driver_tag(monkeypatch, tmp_path):
    """
    Verifies that persisting a WebRTC survey writes an event to outbox_events
    containing the pseudonymized driver_tag, shielding raw phone numbers from downstream workers.
    """
    monkeypatch.setenv("MOCK_AI", "1")
    monkeypatch.setattr(settings, "DURABLE_WEBRTC_ENABLED", False)
    monkeypatch.setattr("routers.survey.SUBMISSION_PHOTOS_DIR", str(tmp_path))
    monkeypatch.setattr("routers.survey.get_redis_client", lambda: None)
    monkeypatch.setattr("services.submission_pipeline.compute_dinov2_embedding", lambda _image: None)

    # Generate synthetic image
    im = Image.new("RGB", (400, 400), color=(100, 150, 200))
    buf = io.BytesIO()
    im.save(buf, format="JPEG")
    buf.seek(0)

    # Mock DB cursor and connection
    mock_cursor = AsyncMock()
    mock_cursor.execute = AsyncMock()
    mock_cursor.__aenter__.return_value = mock_cursor
    mock_cursor.__aexit__.return_value = None
    mock_cursor.fetchone.return_value = None
    mock_cursor.rowcount = 1

    mock_conn = AsyncMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.commit = AsyncMock()
    mock_conn.__aenter__.return_value = mock_conn
    mock_conn.__aexit__.return_value = None

    mock_pool = MagicMock()
    mock_pool.connection.return_value = mock_conn

    driver_phone = "0977888999"
    expected_driver_tag = generate_driver_tag(driver_phone)

    with patch("routers.survey.get_db_pool", return_value=mock_pool):
        data = {
            "driver_phone": driver_phone,
            "driver_name": "Test Driver",
            "license_plate": "29E-123.45",
            "vehicle_type": "CAR",
            "gross_revenue": "1500000",
            "trip_count": "10",
            "signature_png": "data:image/png;base64,mock",
            "consent_choice": "Tôi đồng ý",
            "is_test_mode": "false",
        }
        files = {
            "survey_photo": ("survey.jpg", buf.getvalue(), "image/jpeg"),
        }

        async with embedding_executor_lifespan(), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            resp = await client.post("/api/v1/survey/submit-webrtc", data=data, files=files)
        assert resp.status_code == 200
        resp_json = resp.json()
        assert resp_json.get("driver_tag") == expected_driver_tag

        # Check all executed SQL statements
        executed_calls = [c for c in mock_cursor.execute.call_args_list if c[0]]
        outbox_call = next(
            (c for c in executed_calls if "outbox_events" in str(c[0][0]).lower()),
            None
        )
        assert outbox_call is not None, "outbox_events INSERT was not executed!"

        # Extract JSON payload passed to outbox_events
        sql_params = outbox_call[0][1]
        payload_str = sql_params[-1]
        payload = json.loads(payload_str)

        # Crucial assertions: driver_tag is present and matches pseudonymized identifier
        assert "driver_tag" in payload
        assert payload["driver_tag"] == expected_driver_tag
        assert payload["driver_tag"].startswith("DRV-")
        assert "driver_phone_hash" in payload
        # Raw phone number must not be exposed as driver identifier in outbox payload
        assert driver_phone not in json.dumps(payload)


# ===========================================================================
# 4. VehiclePersistenceTracker Backward Compatibility & Phone Hash Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_vehicle_persistence_tracker_with_phone_hash_and_plates():
    """
    Verifies that VehiclePersistenceTracker looks up and matches vehicles
    using normalized plates and phone hashes while maintaining backward compatibility.
    """
    mock_cursor = AsyncMock()
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor
    mock_conn.commit = AsyncMock()

    mock_pool = MagicMock()
    mock_pool.connection.return_value.__aenter__.return_value = mock_conn

    tracker = VehiclePersistenceTracker(db_pool=mock_pool)
    phone = "0988123456"
    p_hash = hash_phone(phone)

    # 1. First registration: DB returns None -> registers normalized plate
    mock_cursor.fetchone.return_value = None
    res1 = await tracker.verify_and_persist_vehicle(
        driver_phone=phone,
        detected_plate="29E-123.45",
        detected_vehicle_type="CAR",
        driver_phone_hash=p_hash,
    )
    assert res1.is_match is True
    assert res1.is_first_registration is True
    assert res1.status_code == "FIRST_REGISTRATION"
    assert res1.registered_plate == "29E12345"


    # 2. Subsequent survey with matching vehicle
    mock_cursor.fetchone.return_value = ("29E12345", "CAR", None)
    res2 = await tracker.verify_and_persist_vehicle(
        driver_phone=p_hash,  # Passing phone hash as identifier
        detected_plate="29E-123.45",
        detected_vehicle_type="CAR",
    )
    assert res2.is_match is True
    assert res2.status_code == "APPROVED"

    # 3. Subsequent survey with mismatching vehicle
    mock_cursor.fetchone.return_value = ("29E12345", "CAR", None)
    res3 = await tracker.verify_and_persist_vehicle(
        driver_phone=phone,
        detected_plate="51A-999.88",
        detected_vehicle_type="CAR",
    )
    assert res3.is_match is False
    assert res3.status_code == "VEHICLE_UNREGISTERED"


# ===========================================================================
# 5. Telegram Polling Standalone Service Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_telegram_polling_audit_command(monkeypatch):
    """Only authorized audit requests get a closed dossier with an override control."""
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-100123")
    runner = TelegramPollingRunner(bot_token="mock", db_pool=object())
    bundle = AsyncMock()

    mock_dossier = {
        "submission_id": "VN-AUDIT01",
        "driver_phone": "0988111222",
        "license_plate": "29E-123.45",
        "status": "APPROVED",
        "gross_income": 15000000,
        "audit_seal_hash": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
    }

    with patch.object(runner, "send_dossier_bundle", bundle), \
         patch("services.telegram_polling.query_submission_dossier", AsyncMock(return_value=mock_dossier)) as lookup:
        runner.send_message = AsyncMock()
        runner.schedule_submission_review = MagicMock()
        runner.answer_callback_query = AsyncMock()
        await runner.handle_command(chat_id=-100124, text="/audit VN-AUDIT01", from_user_id=99999)
        await runner.handle_callback_query({"id": "deny", "data": "admin_review:VN-AUDIT01", "from": {"id": 99999},
                                            "message": {"chat": {"id": -100124}, "message_id": 1}})
        lookup.assert_not_awaited()
        runner.schedule_submission_review.assert_not_called()
        await runner.handle_command(chat_id=-100123, text="/check VN-AUDIT01", from_user_id=99999)
        assert [b["callback_data"] for row in bundle.await_args.kwargs["reply_markup"]["inline_keyboard"]
                for b in row] == ["resolved_notice"]
        await runner.handle_command(chat_id=-100123, text="/audit VN-AUDIT01", from_user_id=99999)
        assert "VN-AUDIT01" in bundle.await_args.args[3]
        assert any(b["callback_data"].startswith("admin_override:")
                   for row in bundle.await_args.kwargs["reply_markup"]["inline_keyboard"] for b in row)


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "status"), [("approve", "APPROVED"), ("reject", "REJECTED")])
async def test_telegram_polling_callback_queries(monkeypatch, action, status):
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-100123")
    runner = TelegramPollingRunner(bot_token="mock", role="admin")
    update = AsyncMock(return_value=(True, status))
    ack = AsyncMock()
    edit = AsyncMock()
    with patch.object(runner, "_update_submission_status", update), \
         patch.object(runner, "answer_callback_query", ack), \
         patch.object(runner, "edit_message_reply_markup", edit), \
         patch.object(runner, "_legacy_delivery_allowed", AsyncMock(return_value=False)):
        await runner.handle_update({"callback_query": {
            "id": "decision",
            "data": f"admin_{action}:VN-100",
            "message": {"chat": {"id": -100123, "type": "supergroup"}, "message_id": 7},
            "from": {"id": 12345},
        }})

    update.assert_awaited_once()
    assert update.await_args.args == ("VN-100", status)
    assert update.await_args.kwargs["actor"] == "admin:12345"
    ack.assert_awaited_once()
    assert ack.await_args.args[0] == "decision"
    assert not ack.await_args.kwargs.get("show_alert", False)
    edit.assert_awaited_once()
    assert edit.await_args.args == (-100123, 7)
    assert edit.await_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "resolved_notice"


@pytest.mark.asyncio
async def test_telegram_polling_mock_token_sleep_backoff():
    """
    Verifies that when TELEGRAM_BOT_TOKEN is mock or unset,
    poll_once does not crash and performs sleep with exponential backoff.
    """
    runner = TelegramPollingRunner(bot_token="mock_tg_token")
    runner.backoff_delay = 2.0

    with patch("asyncio.sleep", AsyncMock()) as mock_sleep:
        res = await runner.poll_once()
        assert res == 0
        mock_sleep.assert_awaited_once_with(2.0)
        assert runner.backoff_delay == 3.0  # 2.0 * 1.5
