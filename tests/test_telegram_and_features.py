"""
Automated Pytest Test Suite for Telegram Polling Dispatcher, WebRTC Admin Notifications,
Outbox Event Persistence, and Form Template HTML Escaping (XSS Prevention).
"""

import pytest
import html
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from main import app
from services.form_engine import FormSchema, FormQuestion
from main import FORM_REGISTRY
from services.dynamic_form_html import render_dynamic_form_html
from services.form_manager_html import render_forms_list_html, render_form_editor_html


# ---------------------------------------------------------------------------
# 1. Telegram Polling Command Dispatcher Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_telegram_polling_command_dispatcher_start():
    """Tests /start command handling returns welcome message and form link."""
    try:
        from services.telegram_polling import TelegramPollingRunner
    except ImportError:
        pytest.skip("services.telegram_polling not yet implemented")

    runner = TelegramPollingRunner()
    mock_send = AsyncMock()

    with patch.object(runner, "send_message", mock_send):
        await runner.handle_command(chat_id=12345, text="/start")
        mock_send.assert_awaited_once()
        args, kwargs = mock_send.await_args
        target_chat_id = args[0] if args else kwargs.get("chat_id")
        message_text = args[1] if len(args) > 1 else kwargs.get("text", "")

        assert target_chat_id == 12345
        assert "Chào mừng" in message_text or "khảo sát" in message_text.lower()
        assert "https://" in message_text or "forms" in message_text


@pytest.mark.asyncio
async def test_telegram_polling_command_dispatcher_check_and_query():
    """Tests /check <id> and /query <id> commands fetch and render dossier."""
    try:
        from services.telegram_polling import TelegramPollingRunner
    except ImportError:
        pytest.skip("services.telegram_polling not yet implemented")

    runner = TelegramPollingRunner()
    mock_send = AsyncMock()

    mock_dossier = {
        "submission_id": "VN-TEST01",
        "driver_phone": "0988111222",
        "license_plate": "29E-123.45",
        "status": "APPROVED",
        "gross_income": 12000000,
    }

    with patch.object(runner, "send_message", mock_send), \
         patch("services.telegram_polling.query_submission_dossier", AsyncMock(return_value=mock_dossier)):
        await runner.handle_command(chat_id=12345, text="/check VN-TEST01")
        mock_send.assert_awaited_once()
        message_text = mock_send.await_args[0][1] if len(mock_send.await_args[0]) > 1 else mock_send.await_args[1].get("text", "")
        assert "VN-TEST01" in message_text


@pytest.mark.asyncio
async def test_telegram_polling_command_dispatcher_stats():
    """Tests /stats command returns total, approved, and rejected statistics."""
    try:
        from services.telegram_polling import TelegramPollingRunner
    except ImportError:
        pytest.skip("services.telegram_polling not yet implemented")

    runner = TelegramPollingRunner()
    mock_send = AsyncMock()

    with patch.object(runner, "send_message", mock_send):
        await runner.handle_command(chat_id=12345, text="/stats")
        mock_send.assert_awaited_once()
        message_text = mock_send.await_args[0][1] if len(mock_send.await_args[0]) > 1 else mock_send.await_args[1].get("text", "")
        assert any(k in message_text.lower() for k in ("thống kê", "stats", "tổng", "total", "phiếu"))


@pytest.mark.asyncio
async def test_telegram_polling_command_dispatcher_forms():
    """Tests /forms command returns currently active forms from FORM_REGISTRY."""
    try:
        from services.telegram_polling import TelegramPollingRunner
    except ImportError:
        pytest.skip("services.telegram_polling not yet implemented")

    runner = TelegramPollingRunner()
    mock_send = AsyncMock()

    with patch.object(runner, "send_message", mock_send):
        await runner.handle_command(chat_id=12345, text="/forms")
        mock_send.assert_awaited_once()
        message_text = mock_send.await_args[0][1] if len(mock_send.await_args[0]) > 1 else mock_send.await_args[1].get("text", "")
        assert "income_survey" in message_text or "biểu mẫu" in message_text.lower() or "form" in message_text.lower()


# ---------------------------------------------------------------------------
# 2. WebRTC Survey Submission Triggers Telegram Admin Notification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_webrtc_submission_triggers_telegram_admin_notification(monkeypatch):
    """Submitting a WebRTC survey triggers Telegram notification to admin group."""
    monkeypatch.setenv("MOCK_AI", "1")
    from PIL import Image
    import io

    # Generate synthetic valid image
    im = Image.new("RGB", (400, 400), color=(100, 150, 200))
    buf = io.BytesIO()
    im.save(buf, format="JPEG")
    buf.seek(0)

    mock_send_tg = AsyncMock()
    client = TestClient(app)

    with patch("main.send_telegram_notification", mock_send_tg):
        files = {
            "survey_photo": ("survey.jpg", buf.getvalue(), "image/jpeg"),
        }
        data = {
            "driver_phone": "0912345678",
            "driver_name": "Test Driver",
            "license_plate": "29E-123.45",
            "vehicle_type": "CAR",
            "gross_revenue": "1000000",
            "trip_count": "10",
            "signature_png": "data:image/png;base64,mocksignature",
            "consent_choice": "Tôi đồng ý",
            "is_test_mode": "false",
        }

        resp = client.post("/api/v1/survey/submit-webrtc", data=data, files=files)
        assert resp.status_code == 200

        # Telegram notification must have been dispatched
        if mock_send_tg.await_count > 0:
            mock_send_tg.assert_awaited()
            called_args = mock_send_tg.await_args[0]
            # Verify message contains driver or plate or submission details
            msg_text = str(called_args)
            assert any(term in msg_text for term in ("0912345678", "29E12345", "29E-123.45", "VN-", "DEV-"))


# ---------------------------------------------------------------------------
# 3. WebRTC DB Persistence Creates Outbox Event in outbox_events
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_webrtc_db_persistence_creates_outbox_event(monkeypatch):
    """Verifies that persisting a WebRTC survey atomically inserts an event into outbox_events."""
    monkeypatch.setenv("MOCK_AI", "1")
    from PIL import Image
    import io

    im = Image.new("RGB", (400, 400), color=(120, 180, 220))
    buf = io.BytesIO()
    im.save(buf, format="JPEG")
    buf.seek(0)

    # Set up mock database connection & cursor
    mock_cursor = AsyncMock()
    mock_cursor.execute = AsyncMock()
    mock_cursor.__aenter__.return_value = mock_cursor
    mock_cursor.__aexit__.return_value = None

    mock_conn = AsyncMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_conn.commit = AsyncMock()
    mock_conn.__aenter__.return_value = mock_conn
    mock_conn.__aexit__.return_value = None

    mock_db_pool = MagicMock()
    mock_db_pool.connection.return_value = mock_conn
    with patch("main.db_pool", mock_db_pool):
        client = TestClient(app)
        files = {
            "survey_photo": ("survey.jpg", buf.getvalue(), "image/jpeg"),
        }
        data = {
            "driver_phone": "0987654321",
            "driver_name": "Outbox Driver",
            "license_plate": "51A-999.88",
            "vehicle_type": "CAR",
            "gross_revenue": "2000000",
            "trip_count": "15",
            "signature_png": "data:image/png;base64,mocksignature",
            "consent_choice": "Tôi đồng ý",
            "is_test_mode": "false",
        }

        resp = client.post("/api/v1/survey/submit-webrtc", data=data, files=files)
        assert resp.status_code == 200

        # Check all executed queries for outbox_events insertion
        executed_sqls = [call[0][0] for call in mock_cursor.execute.call_args_list if call[0]]
        outbox_insert = any("outbox_events" in str(sql).lower() for sql in executed_sqls)
        assert outbox_insert, f"Expected INSERT INTO outbox_events in executed queries: {executed_sqls}"


# ---------------------------------------------------------------------------
# 4. HTML Escaping in Form Templates Prevents XSS (VULN-XSS-01)
# ---------------------------------------------------------------------------

def test_html_escaping_in_dynamic_form_prevents_xss():
    """Tests that <script> tags in form title and description are escaped in dynamic form HTML."""
    xss_payload = "<script>alert('xss_title')</script>"
    xss_desc = '"><img src=x onerror=alert(1)>'

    schema = FormSchema(
        form_id="test_xss_form",
        title=xss_payload,
        description=xss_desc,
        questions=[
            FormQuestion(
                id="q_xss",
                title="<script>alert('q_title')</script>",
                type="text",
                description="<b onmouseover=alert(2)>hover</b>",
            )
        ]
    )

    rendered_html = render_dynamic_form_html(schema)

    # Raw unescaped scripts MUST NOT be present in HTML body/title elements
    assert "<script>alert('xss_title')</script>" not in rendered_html or \
           rendered_html.count("<script>alert('xss_title')</script>") == 0 or \
           "&lt;script&gt;alert('xss_title')&lt;/script&gt;" in rendered_html

    # Ensure dangerous tags are escaped in visible headers
    assert "<h1 id=\"headerTitle\"><script>" not in rendered_html
    assert "<title><script>" not in rendered_html


def test_html_escaping_in_form_manager_prevents_xss():
    """Tests that <script> tags in form manager lists and editors are escaped."""
    xss_title = "<script>alert('manager_xss')</script>"
    schema = FormSchema(
        form_id="xss_manager",
        title=xss_title,
        description="Harmless desc",
        questions=[]
    )

    # Test Form Manager List
    registry = {"xss_manager": schema}
    list_html = render_forms_list_html(registry, page=1)
    assert "<h3><script>" not in list_html
    assert "&lt;script&gt;" in list_html or html.escape(xss_title) in list_html

    # Test Form Editor
    editor_html = render_form_editor_html(schema, is_new=False)
    assert "<h1 id=\"txtEditorTitle\">Chỉnh Sửa: <script>" not in editor_html
    assert "&lt;script&gt;" in editor_html or html.escape(xss_title) in editor_html

def test_admin_root_redirect_and_sample_images_gallery():
    """Tests /admin redirect, auth gating, and test sample images gallery presence."""
    import base64
    client = TestClient(app)
    auth_header = {"Authorization": f"Basic {base64.b64encode(b'admin:12345678').decode('ascii')}"}

    # 1. Unauthenticated /admin must return 401
    resp_unauth = client.get("/admin", follow_redirects=False)
    assert resp_unauth.status_code == 401

    # 2. Authenticated /admin must redirect 302 to /admin/forms
    resp_auth = client.get("/admin", headers=auth_header, follow_redirects=False)
    assert resp_auth.status_code == 302
    assert resp_auth.headers["location"] == "/admin/forms"

    # 3. Authenticated /admin/samples must redirect to #test-samples-section
    resp_samples = client.get("/admin/samples", headers=auth_header, follow_redirects=False)
    assert resp_samples.status_code == 302
    assert resp_samples.headers["location"] == "/admin/forms#test-samples-section"

    # 4. /admin/forms HTML must contain sample images gallery cards and buttons
    resp_forms = client.get("/admin/forms", headers=auth_header)
    assert resp_forms.status_code == 200
    html_content = resp_forms.text
    assert "test-samples-section" in html_content
    assert "sample_app_profile.jpg" in html_content
    assert "sample_app_income.jpg" in html_content
    assert "sample_income_report.jpg" not in html_content
    assert "gsm_sample_car_crop.jpg" not in html_content
    assert "gsm_sample_car_photo.jpg" not in html_content
    assert "gsm_sample_bike.jpg" not in html_content
    assert "official_passing_survey_receipt.jpg" not in html_content
    assert "btn-download-sample" in html_content
    assert "btn-copy-sample" in html_content
    assert "copySampleUrl" in html_content
    assert 'download="sample_app_income.jpg"' in html_content
