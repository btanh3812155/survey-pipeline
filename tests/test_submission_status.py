# -*- coding: utf-8 -*-
"""Public submission-status endpoint + bottom status bar wiring."""

import json
import os

from fastapi.testclient import TestClient

from main import app
from services.dynamic_form_html import render_dynamic_form_html
from services.form_engine import DEFAULT_TRUST_BRIDGE_SCHEMA

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


def test_form_has_bottom_status_bar_and_shared_polling_renderer():
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)

    assert 'id="submissionStatusBar"' in html
    assert 'id="submissionStatusBadge"' in html
    assert 'id="submissionStatusReason"' in html
    assert 'id="submissionStatusRefresh"' in html

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

    # Terminal statuses stop polling; manual refresh stays wired.
    assert "isTerminalSubmissionStatus" in html
    assert "stopStatusPolling" in html
    assert "refreshSubmissionStatus(true)" in html
