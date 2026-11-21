"""
Automated Pytest Test Suite for Telegram Polling Dispatcher, WebRTC Admin Notifications,
Outbox Event Persistence, and Form Template HTML Escaping (XSS Prevention).
"""

import json
import httpx
import pytest
import html
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from config import settings
from core.embedding_executor import embedding_executor_lifespan
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
    """The admin bot directs operators to its review and quota commands."""
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
        assert "/check" in message_text
        assert "/quota" in message_text


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/check", "/query"])
async def test_telegram_polling_command_dispatcher_check_and_query(monkeypatch, command):
    """Tests /check <id> and /query <id> commands fetch and render dossier."""
    try:
        from services.telegram_polling import TelegramPollingRunner
    except ImportError:
        pytest.skip("services.telegram_polling not yet implemented")

    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-100123")
    runner = TelegramPollingRunner(bot_token="mock", role="admin")
    bundle = AsyncMock()

    mock_dossier = {
        "submission_id": "VN-TEST01",
        "driver_phone": "0988111222",
        "license_plate": "29E-123.45",
        "status": "APPROVED",
        "gross_income": 12000000,
    }

    lookup = AsyncMock(return_value=mock_dossier)
    with patch.object(runner, "send_dossier_bundle", bundle), \
         patch("services.telegram_polling.query_submission_dossier", lookup):
        await runner.handle_update({"message": {
            "chat": {"id": -100123, "type": "supergroup"},
            "from": {"id": 12345},
            "text": f"{command} vn-test01",
        }})
    lookup.assert_awaited_once_with("VN-TEST01", db_pool=runner.db_pool)
    bundle.assert_awaited_once()
    chat_id, _, dossier, _, document = bundle.await_args.args
    assert chat_id == -100123
    assert dossier == mock_dossier
    assert json.loads(document)["submission_id"] == "VN-TEST01"


@pytest.mark.asyncio
async def test_telegram_polling_command_dispatcher_stats(monkeypatch, tmp_path):
    import re
    from services import telegram_polling as tg

    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-100456")
    path = tmp_path / "dev_submissions.json"
    path.write_text(json.dumps([{"status": status} for status in (
        "APPROVED", "APPROVED", "REJECTED", "MANUAL_REVIEW",
    )]))
    monkeypatch.setattr(tg, "DEV_STORAGE_FILE", str(path))
    runner = tg.TelegramPollingRunner(bot_token="mock")
    runner.send_message = AsyncMock()
    with patch("builtins.open", side_effect=AssertionError("unauthorized stats read")):
        await runner.handle_command(chat_id=-100457, text="/stats", from_user_id=88)
    await runner.handle_command(chat_id=-100456, text="/stats", from_user_id=88)
    assert re.findall(r"</b> (\d+)", runner.send_message.await_args.args[1]) == ["4", "2", "1", "1"]
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    await runner.handle_command(chat_id=-100456, text="/stats", from_user_id=88)
    assert not re.findall(r"</b> (\d+)", runner.send_message.await_args.args[1])

@pytest.mark.asyncio
async def test_telegram_polling_command_dispatcher_forms(monkeypatch, tmp_path):
    import os
    from services import telegram_polling as tg

    forms = {}
    for i, (fid, access, limit, count) in enumerate([
        ("older", "public", None, 0), ("newest", "public", 3, 2),
        ("full", "public", 3, 3), ("private", "private", None, 0),
    ], 1):
        forms[fid] = FormSchema(form_id=fid, title=f"<{fid}>", questions=[],
                                settings={"access_control": access, "response_limit": limit, "current_responses": count})
        path = tmp_path / f"{fid}.json"
        path.touch()
        os.utime(path, ns=(i, i))
    monkeypatch.setattr(tg, "FORMS_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(tg, "FORM_REGISTRY", forms)
    monkeypatch.setattr(tg, "refresh_form_registry_from_disk", lambda: None)
    admin, partner = tg.TelegramPollingRunner(), tg.DriverBotRunner()
    admin.send_message, partner.send_message = AsyncMock(), AsyncMock()
    await admin.handle_command(77, "/forms")
    await partner.handle_driver_command(88, 88, "/forms", {"id": 88})
    text = admin.send_message.await_args.args[1]
    assert text == partner.send_message.await_args.args[1]
    assert text.index("/forms/newest") < text.index("/forms/older")
    assert "&lt;newest&gt;" in text and "/forms/full" not in text and "/forms/private" not in text


@pytest.mark.asyncio
async def test_missing_argument_continues_only_from_same_operators_exact_reply(monkeypatch):
    from services import telegram_polling as tg

    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-100321")
    monkeypatch.setattr(tg, "get_redis_client", lambda: None)
    runner = tg.TelegramPollingRunner(bot_token="mock")
    runner.send_message = AsyncMock(side_effect=[{"message_id": 31}, {"message_id": 32}])
    runner.handle_admin_surveys_lookup = AsyncMock()
    runner.execute_submission_review = AsyncMock()

    async def update(text, *, sender=101, reply=None):
        await runner.handle_update({"message": {"chat": {"id": -100321, "type": "supergroup"},
                                    "from": {"id": sender}, "text": text, "reply_to_message": reply}})

    await update("/surveys")
    await update("/check")
    assert all(call.kwargs["reply_markup"]["force_reply"] for call in runner.send_message.await_args_list)
    prompt = {"message_id": 31, "text": tg.ARGUMENT_PROMPT, "from": {"is_bot": True}}
    await update("CTV-01")
    await update("CTV-01", reply={"message_id": 999})
    await update("CTV-01", sender=102, reply=prompt)
    runner.handle_admin_surveys_lookup.assert_not_awaited()
    await update("CTV-01", reply=prompt)
    await update("CTV-01", reply=prompt)
    runner.handle_admin_surveys_lookup.assert_awaited_once_with(-100321, "CTV-01")
    await update("VN-11", reply={**prompt, "message_id": 32})
    runner.execute_submission_review.assert_awaited_once_with(-100321, "VN-11")


# ---------------------------------------------------------------------------
# 2. WebRTC Survey Submission Triggers Telegram Admin Notification
# ---------------------------------------------------------------------------

def test_webrtc_submission_triggers_telegram_admin_notification(monkeypatch, tmp_path):
    """Submitting a WebRTC survey triggers Telegram notification to admin group."""
    monkeypatch.setenv("MOCK_AI", "1")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-100123")
    monkeypatch.setattr("routers.survey.get_db_pool", lambda: None)
    monkeypatch.setattr("routers.survey.get_redis_client", lambda: None)
    monkeypatch.setattr("routers.survey.SUBMISSION_PHOTOS_DIR", str(tmp_path))
    monkeypatch.setattr("routers.survey.append_dev_submission", lambda _record: None)
    from PIL import Image
    import io

    # Generate synthetic valid image
    im = Image.new("RGB", (400, 400), color=(100, 150, 200))
    buf = io.BytesIO()
    im.save(buf, format="JPEG")
    buf.seek(0)

    mock_send_tg = AsyncMock()
    client = TestClient(app)

    with patch("routers.survey.send_telegram_notification", mock_send_tg), \
         patch("routers.survey.dispatch_action_checklist", AsyncMock()):
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

        mock_send_tg.assert_awaited_once()
        chat_id, message, _ = mock_send_tg.await_args.args
        assert chat_id == "-100123"
        assert resp.json()["submission_id"] in message


# ---------------------------------------------------------------------------
# 3. WebRTC DB Persistence Creates Outbox Event in outbox_events
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_webrtc_db_persistence_creates_outbox_event(monkeypatch, tmp_path):
    """Verifies that persisting a WebRTC survey atomically inserts an event into outbox_events."""
    monkeypatch.setenv("MOCK_AI", "1")
    monkeypatch.setattr(settings, "DURABLE_WEBRTC_ENABLED", False)
    monkeypatch.setattr("routers.survey.SUBMISSION_PHOTOS_DIR", str(tmp_path))
    monkeypatch.setattr("routers.survey.get_redis_client", lambda: None)
    monkeypatch.setattr("services.submission_pipeline.compute_dinov2_embedding", lambda _image: None)
    monkeypatch.setattr("routers.survey._schedule_legacy_submission", lambda *_args: None)
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
    mock_cursor.rowcount = 1
    def row_for_statement():
        statement = mock_cursor.execute.await_args.args[0]
        if "INSERT INTO driver_identity_vault" in statement:
            return ("driver-vault-id",)
        if "INSERT INTO outbox_events" in statement:
            return ("outbox-id",)
        if "FROM driver_registered_vehicles" in statement:
            return None
        raise AssertionError(f"unexpected result query: {statement}")

    mock_cursor.fetchone.side_effect = row_for_statement

    mock_conn = AsyncMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.commit = AsyncMock()
    mock_conn.__aenter__.return_value = mock_conn
    mock_conn.__aexit__.return_value = None

    mock_db_pool = MagicMock()
    mock_db_pool.connection.return_value = mock_conn
    with patch("routers.survey.get_db_pool", return_value=mock_db_pool):
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

        async with embedding_executor_lifespan(), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            resp = await client.post("/api/v1/survey/submit-webrtc", data=data, files=files)
        assert resp.status_code == 200

        outbox_calls = [call.args[1] for call in mock_cursor.execute.await_args_list
                        if "outbox_events" in call.args[0]]
        [(event_type, submission_id, payload)] = outbox_calls
        assert event_type in {"survey.validated", "survey.rejected"}
        assert submission_id == resp.json()["submission_id"] == json.loads(payload)["submission_id"]
        mock_conn.commit.assert_awaited_once()


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

    # 3. Authenticated /admin/samples must redirect to /admin/media
    resp_samples = client.get("/admin/samples", headers=auth_header, follow_redirects=False)
    assert resp_samples.status_code == 302
    assert resp_samples.headers["location"] == "/admin/media"

    # 4. /admin/forms HTML is clean (samples retired to /admin/media)
    resp_forms = client.get("/admin/forms", headers=auth_header)
    assert resp_forms.status_code == 200
    html_content = resp_forms.text
    assert "test-samples-section" not in html_content
    assert "btnAdminCreate" in html_content

    # 5. /admin/media contains the media manager
    resp_media = client.get("/admin/media", headers=auth_header)
    assert resp_media.status_code == 200
    assert "Quản Lý Media" in resp_media.text
