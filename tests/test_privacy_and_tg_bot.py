"""
Test Suite for Privacy Hashing, PII Pseudonymization, Public History Protection,
and Standalone Telegram Bot Service.
"""

import json
import io
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from PIL import Image

from config import settings
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

def test_survey_history_unauthorized_returns_401():
    """
    Prevents scraping of driver survey histories by unauthenticated outsiders.
    Access to /api/v1/surveys/history without valid credentials MUST return 401 Unauthorized.
    """
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
async def test_webrtc_outbox_event_contains_pseudonymized_driver_tag(monkeypatch):
    """
    Verifies that persisting a WebRTC survey writes an event to outbox_events
    containing the pseudonymized driver_tag, shielding raw phone numbers from downstream workers.
    """
    monkeypatch.setenv("MOCK_AI", "1")

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

    mock_conn = AsyncMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_conn.commit = AsyncMock()
    mock_conn.__aenter__.return_value = mock_conn
    mock_conn.__aexit__.return_value = None

    mock_pool = MagicMock()
    mock_pool.connection.return_value = mock_conn

    driver_phone = "0977888999"
    expected_driver_tag = generate_driver_tag(driver_phone)

    with patch("main.db_pool", mock_pool):
        client = TestClient(app)
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

        resp = client.post("/api/v1/survey/submit-webrtc", data=data, files=files)
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
        payload_str = sql_params[3]
        payload = json.loads(payload_str)

        # Crucial assertions: driver_tag is present and matches pseudonymized identifier
        assert "driver_tag" in payload
        assert payload["driver_tag"] == expected_driver_tag
        assert payload["driver_tag"].startswith("DRV-")
        assert "driver_phone_hash" in payload
        # Raw phone number must not be exposed as driver identifier in outbox payload
        assert payload.get("driver_phone") is None or "0977888999" not in str(payload.get("driver_phone"))


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

    # Verify query checked both raw phone and phone hash
    first_call_sql = mock_cursor.execute.call_args_list[0][0][0]
    first_call_params = mock_cursor.execute.call_args_list[0][0][1]
    assert "WHERE driver_phone = %s OR driver_phone = %s" in first_call_sql
    assert first_call_params[0] == phone
    assert first_call_params[1] == p_hash

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
async def test_telegram_polling_audit_command():
    """Tests that /audit <id> renders dossier and provides inline keyboard buttons."""
    runner = TelegramPollingRunner()
    mock_send = AsyncMock()

    mock_dossier = {
        "submission_id": "VN-AUDIT01",
        "driver_phone": "0988111222",
        "license_plate": "29E-123.45",
        "status": "APPROVED",
        "gross_income": 15000000,
        "audit_seal_hash": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
    }

    with patch.object(runner, "send_message", mock_send), \
         patch("services.telegram_polling.query_submission_dossier", AsyncMock(return_value=mock_dossier)):
        await runner.handle_command(chat_id=99999, text="/audit VN-AUDIT01")
        mock_send.assert_awaited_once()
        args, kwargs = mock_send.await_args
        msg_text = args[1] if len(args) > 1 else kwargs.get("text", "")
        reply_markup = kwargs.get("reply_markup") or (args[3] if len(args) > 3 else None)

        assert "VN-AUDIT01" in msg_text
        assert reply_markup is not None
        assert "inline_keyboard" in reply_markup


@pytest.mark.asyncio
async def test_telegram_polling_callback_queries():
    """Tests callback button processing for admin_audit, admin_approve, and admin_reject."""
    runner = TelegramPollingRunner()
    mock_send = AsyncMock()
    mock_ack = AsyncMock()

    with patch.object(runner, "send_message", mock_send), \
         patch.object(runner, "answer_callback_query", mock_ack):
        # 1. admin_approve
        cq_approve = {
            "id": "cq_1",
            "data": "admin_approve:VN-100",
            "message": {"chat": {"id": 12345}},
        }
        await runner.handle_callback_query(cq_approve)
        mock_ack.assert_awaited_with("cq_1", "Đã duyệt #VN-100")
        assert any("DUYỆT" in str(c) for c in mock_send.await_args_list)

        # 2. admin_reject
        cq_reject = {
            "id": "cq_2",
            "data": "admin_reject:VN-100",
            "message": {"chat": {"id": 12345}},
        }
        await runner.handle_callback_query(cq_reject)
        mock_ack.assert_awaited_with("cq_2", "Đã từ chối #VN-100")
        assert any("TỪ CHỐI" in str(c) for c in mock_send.await_args_list)


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
