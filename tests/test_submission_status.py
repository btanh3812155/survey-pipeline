# -*- coding: utf-8 -*-
"""Public submission-status endpoint + bottom status bar wiring."""

import json
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from config import settings
from main import app
from services.dynamic_form_html import render_dynamic_form_html
from services.form_engine import DEFAULT_TRUST_BRIDGE_SCHEMA
from services.telegram_polling import TelegramPollingRunner

DEV_FILE = os.path.join(os.getcwd(), "data", "dev_test_submissions.json")


def _read_dev_file():
    if not os.path.exists(DEV_FILE):
        return None
    with open(DEV_FILE, "r", encoding="utf-8") as f:
        return f.read()


def _write_dev_file(records):
    os.makedirs(os.path.dirname(DEV_FILE), exist_ok=True)
    with open(DEV_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)


def test_status_endpoint_reads_dev_storage_and_reflects_admin_decision():
    original = _read_dev_file()
    submission_id = "DEV-STATUS-TEST01"
    record = {
        "submission_id": submission_id,
        "status": "MANUAL_REVIEW",
        "reason": "Chờ điều phối viên duyệt thủ công.",
        "driver_phone": "0988123456",
        "driver_name": "Nguyen Van A",
        "submitted_at": "2026-11-18T10:00:00+00:00",
    }
    try:
        _write_dev_file([record])
        client = TestClient(app)

        resp = client.get(f"/api/v1/survey/status/{submission_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "submission_id": submission_id,
            "status": "MANUAL_REVIEW",
            "reason": "Chờ điều phối viên duyệt thủ công.",
            "updated_at": "2026-11-18T10:00:00+00:00",
            "is_test_mode": True,
        }
        assert "driver_phone" not in data and "driver_name" not in data

        # Admin accept/reject later rewrites the dev record; the endpoint must reflect it.
        record["status"] = "APPROVED"
        record["reason"] = "Ảnh rõ nét, số liệu khớp."
        record["moderated_at"] = "2026-11-18T11:30:00+00:00"
        _write_dev_file([record])

        resp = client.get(f"/api/v1/survey/status/{submission_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "APPROVED"
        assert data["reason"] == "Ảnh rõ nét, số liệu khớp."
        assert data["updated_at"] == "2026-11-18T11:30:00+00:00"
    finally:
        if original is None:
            if os.path.exists(DEV_FILE):
                os.remove(DEV_FILE)
        else:
            with open(DEV_FILE, "w", encoding="utf-8") as f:
                f.write(original)


def test_status_endpoint_ignores_action_checklist_stub():
    """dev_test_submissions.json also stores {submission_id, action_status} stubs.

    A stub must never mask the real submission record in dev_submissions.json.
    """
    test_file = DEV_FILE
    main_file = os.path.join(os.getcwd(), "data", "dev_submissions.json")
    original_test = open(test_file, "r", encoding="utf-8").read() if os.path.exists(test_file) else None
    original_main = open(main_file, "r", encoding="utf-8").read() if os.path.exists(main_file) else None
    submission_id = "DEV-STATUS-TEST02"
    try:
        os.makedirs(os.path.dirname(test_file), exist_ok=True)
        os.makedirs(os.path.dirname(main_file), exist_ok=True)
        with open(test_file, "w", encoding="utf-8") as f:
            json.dump([{"submission_id": submission_id, "action_status": {"r2_upload": {"status": "PENDING"}}}], f)
        with open(main_file, "w", encoding="utf-8") as f:
            json.dump([{
                "submission_id": submission_id,
                "status": "REJECTED",
                "reason": "Ảnh không khớp số liệu.",
                "submitted_at": "2026-11-18T12:00:00+00:00",
            }], f)

        resp = TestClient(app).get(f"/api/v1/survey/status/{submission_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "REJECTED"
        assert data["reason"] == "Ảnh không khớp số liệu."
    finally:
        for path, original in ((test_file, original_test), (main_file, original_main)):
            if original is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(original)


def test_status_endpoint_unknown_submission_returns_404():
    client = TestClient(app)
    resp = client.get("/api/v1/survey/status/DEV-DOES-NOT-EXIST")
    assert resp.status_code == 404
    assert "Không tìm thấy" in resp.json()["detail"]


MAIN_DEV_FILE = os.path.join(os.getcwd(), "data", "dev_submissions.json")


def _backup(path):
    return open(path, "r", encoding="utf-8").read() if os.path.exists(path) else None


def _restore(path, content):
    if content is None:
        if os.path.exists(path):
            os.remove(path)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


@pytest.mark.asyncio
async def test_appeal_reopens_rejected_submission_and_admin_can_approve():
    submission_id = "DEV-APPEAL-TEST01"
    backups = {p: _backup(p) for p in (DEV_FILE, MAIN_DEV_FILE)}
    try:
        _write_json(DEV_FILE, [{"submission_id": submission_id, "action_status": {"r2_upload": {"status": "PENDING"}}}])
        _write_json(MAIN_DEV_FILE, [{
            "submission_id": submission_id,
            "status": "REJECTED",
            "reason": "Ảnh mờ, không đọc được.",
            "submitted_at": "2026-11-18T10:00:00+00:00",
        }])

        with patch("routers.survey.get_db_pool", return_value=None), \
             patch.object(settings, "TELEGRAM_ADMIN_GROUP_ID", None):
            resp = TestClient(app).post("/api/v1/survey/appeal", json={
                "submission_id": submission_id,
                "driver_phone": "0987000001",
                "reason": "Ảnh gốc rõ nét, đề nghị kiểm tra lại.",
            })
        assert resp.status_code == 200
        assert resp.json()["status"] == "APPEAL_SUBMITTED"

        record = next(r for r in json.load(open(MAIN_DEV_FILE, encoding="utf-8")) if r["submission_id"] == submission_id)
        assert record["status"] == "APPEAL_PENDING"
        assert "Ảnh gốc rõ nét" in record["reason"]
        assert record["rejection_reason"] == record["reason"]

        # Status bar endpoint sees a pending/manual-review state, never REJECTED.
        status_resp = TestClient(app).get(f"/api/v1/survey/status/{submission_id}")
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] == "APPEAL_PENDING"

        runner = TelegramPollingRunner(bot_token="mock_token", role="admin", db_pool=None)
        success, final_status = await runner._update_submission_status(
            submission_id, "APPROVED", reason="Quản trị viên duyệt sau phúc khảo"
        )
        assert success is True and final_status == "APPROVED"
        record = next(r for r in json.load(open(MAIN_DEV_FILE, encoding="utf-8")) if r["submission_id"] == submission_id)
        assert record["status"] == "APPROVED"
        assert record["reason"] == "Quản trị viên duyệt sau phúc khảo"

        # Terminal guard still applies after the appealed submission is resolved again.
        success, current_status = await runner._update_submission_status(submission_id, "REJECTED")
        assert success is False and current_status == "APPROVED"
    finally:
        for path, content in backups.items():
            _restore(path, content)


@pytest.mark.asyncio
async def test_telegram_appeal_reopens_rejected_and_ignores_checklist_stub():
    submission_id = "DEV-APPEAL-TEST02"
    backups = {p: _backup(p) for p in (DEV_FILE, MAIN_DEV_FILE)}
    try:
        _write_json(DEV_FILE, [{"submission_id": submission_id, "action_status": {"r2_upload": {"status": "PENDING"}}}])
        _write_json(MAIN_DEV_FILE, [{
            "submission_id": submission_id,
            "status": "REJECTED",
            "reason": "Số liệu không khớp.",
            "submitted_at": "2026-11-18T10:00:00+00:00",
        }])

        runner = TelegramPollingRunner(bot_token="mock_token", role="admin", db_pool=None)
        success, final_status = await runner._update_submission_status(
            submission_id, "APPEAL_PENDING", reason="Thành viên gửi yêu cầu phúc khảo qua Telegram DM"
        )
        assert success is True and final_status == "APPEAL_PENDING"

        record = next(r for r in json.load(open(MAIN_DEV_FILE, encoding="utf-8")) if r["submission_id"] == submission_id)
        assert record["status"] == "APPEAL_PENDING"
        assert "phúc khảo" in record["reason"]

        # The action-checklist stub must stay a stub, not absorb the status update.
        stub = next(r for r in json.load(open(DEV_FILE, encoding="utf-8")) if r["submission_id"] == submission_id)
        assert "status" not in stub and "reason" not in stub
    finally:
        for path, content in backups.items():
            _restore(path, content)


def test_form_has_bottom_status_bar_and_shared_polling_renderer():
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)

    assert 'id="submissionStatusBar"' in html
    assert 'id="submissionStatusBadge"' in html
    assert 'id="submissionStatusReason"' in html
    # No manual refresh control: the bar must update itself.
    assert 'submissionStatusRefresh' not in html
    assert 'status_refresh' not in html

    assert "/api/v1/survey/status/" in html
    assert "tb_last_submission_" in html
    assert "STATUS_POLL_MS = 15000" in html
    assert "setInterval" in html
    assert "initSubmissionStatusBar" in html
    assert "rememberSubmissionStatus(res)" in html
    assert "body.classList.toggle('has-submission-status'" in html

    # One render function must drive both the bottom bar and the popup.
    start = html.index("function renderSubmissionStatus(")
    end = html.index("function showSubmissionResultModal(")
    block = html[start:end]
    assert "submissionStatusBadge" in block and "subResultBadge" in block
    assert "submissionStatusReason" in block and "subResultReason" in block
    assert "submissionStatusBar" in block and "submissionResultBox" in block

    # Terminal statuses stop polling; transient failures back off and retry.
    assert "isTerminalSubmissionStatus" in html
    assert "stopStatusPolling" in html
    assert "scheduleStatusRetry" in html
    assert "refreshSubmissionStatus(true)" not in html

    # Reset tears the bar down completely: timer, state, storage, DOM, layout offset.
    reset_start = html.index("function executeResetForm(")
    reset_end = html.index("function getLastSubmissionKey(")
    reset_block = html[reset_start:reset_end]
    assert "clearSubmissionStatus();" in reset_block
    clear_start = html.index("function clearSubmissionStatus(")
    clear_end = html.index("let lastSubmittedData", clear_start)
    clear_block = html[clear_start:clear_end]
    assert "stopStatusPolling();" in clear_block
    assert "lastSubmissionStatus = null;" in clear_block
    assert "localStorage.removeItem(getLastSubmissionKey())" in clear_block
    assert "classList.remove('visible')" in clear_block
    assert "'--status-bar-offset', '0px'" in clear_block
    assert "closeSubmissionResultModal();" in clear_block


def test_status_rate_limiter_uses_a_fixed_window(monkeypatch):
    """The limiter must not extend its own window on every hit.

    A page polling every 15s used to refresh the TTL on every request, so the
    counter never expired and the client locked itself out after 60 hits (~15 min)
    while the pending verdict was still pending. Once limited, the frontend used to
    stop polling for good, so a later admin/Telegram verdict never reached the UI.
    """
    import core.cache as cache_mod
    from core.cache import BoundedTTLCache

    now = [1000.0]
    monkeypatch.setattr(cache_mod.time, "time", lambda: now[0])
    limiter = BoundedTTLCache(max_size=4, ttl_seconds=60.0)

    assert limiter.incr("ip") == 1
    now[0] += 15
    assert limiter.incr("ip") == 2
    now[0] += 15
    assert limiter.incr("ip") == 3
    # Still inside the window opened by the very first hit: no silent extension.
    now[0] = 1059.0
    assert limiter.incr("ip") == 4
    # Window finally expires -> a fresh counter, not a permanent lockout.
    now[0] = 1061.0
    assert limiter.incr("ip") == 1


def test_status_endpoint_rate_limit_recovers_after_window(monkeypatch):
    from routers import survey as survey_mod

    survey_mod.STATUS_RATE_LIMITER.clear()
    try:
        client = TestClient(app)
        codes = [client.get("/api/v1/survey/status/DEV-NO-SUCH-ID").status_code for _ in range(61)]
        assert codes[59] == 404
        assert codes[60] == 429
    finally:
        survey_mod.STATUS_RATE_LIMITER.clear()


@pytest.mark.asyncio
async def test_admin_decision_stamps_moderated_at_for_status_bar():
    submission_id = "DEV-STATUS-STAMP01"
    backups = {p: _backup(p) for p in (DEV_FILE, MAIN_DEV_FILE)}
    try:
        _write_json(MAIN_DEV_FILE, [{
            "submission_id": submission_id,
            "status": "MANUAL_REVIEW",
            "reason": "Chờ duyệt.",
            "submitted_at": "2026-11-18T10:00:00+00:00",
        }])
        runner = TelegramPollingRunner(bot_token="mock_token", role="admin", db_pool=None)
        success, final_status = await runner._update_submission_status(
            submission_id, "APPROVED", reason="Duyệt thủ công"
        )
        assert success is True and final_status == "APPROVED"

        record = next(r for r in json.load(open(MAIN_DEV_FILE, encoding="utf-8")) if r["submission_id"] == submission_id)
        assert record["status"] == "APPROVED"
        assert record.get("moderated_at"), "admin decisions must stamp moderated_at"

        status_resp = TestClient(app).get(f"/api/v1/survey/status/{submission_id}")
        assert status_resp.status_code == 200
        assert status_resp.json()["updated_at"] == record["moderated_at"]
    finally:
        for path, content in backups.items():
            _restore(path, content)


def test_form_exposes_appeal_and_redo_state():
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)

    assert 'id="submissionStatusRedo"' in html
    assert "status_appeal_pending" in html
    assert "APPEAL_PENDING" in html
    assert "statusSettleTimer" in html
    assert "statusStateEpoch" in html
    assert "statusRefreshInFlight" in html


class _FakeCursor:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *args, **kwargs):
        return None

    async def fetchone(self):
        return None


class _FakeConn:
    def cursor(self, **kwargs):
        return _FakeCursor()

    async def commit(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def connection(self):
        return _FakeConn()


@pytest.mark.asyncio
async def test_dev_submission_verdict_falls_through_when_db_row_is_missing():
    """Production has a DB pool; DEV/TEST ids have no DB row and must hit dev storage."""
    submission_id = "DEV-FALLTHROUGH01"
    backups = {p: _backup(p) for p in (DEV_FILE, MAIN_DEV_FILE)}
    try:
        _write_json(DEV_FILE, [])
        _write_json(MAIN_DEV_FILE, [{
            "submission_id": submission_id,
            "status": "MANUAL_REVIEW",
            "reason": "Chờ duyệt.",
            "submitted_at": "2026-11-18T10:00:00+00:00",
        }])

        runner = TelegramPollingRunner(bot_token="mock_token", role="admin", db_pool=_FakePool())
        success, final_status = await runner._update_submission_status(
            submission_id, "APPROVED", reason="Duyệt bản thử nghiệm"
        )
        assert success is True and final_status == "APPROVED"
        record = next(r for r in json.load(open(MAIN_DEV_FILE, encoding="utf-8")) if r["submission_id"] == submission_id)
        assert record["status"] == "APPROVED"
        assert record["reason"] == "Duyệt bản thử nghiệm"
        assert record.get("moderated_at")
    finally:
        for path, content in backups.items():
            _restore(path, content)
