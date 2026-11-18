# -*- coding: utf-8 -*-
"""Browser-level regression test for the submission status bar state machine.

Runs the real ``static/ui/dynamic_form.html`` inline script in Chromium with the
status endpoint stubbed, so the transition-to-final / poll-stop / appeal / redo /
stale-response-race behaviour is exercised exactly as the browser sees it.

Skips cleanly when Playwright or a Chromium build is unavailable.
"""

import glob
import json
import os
import time
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]
FORM_HTML = ROOT / "static" / "ui" / "dynamic_form.html"
ORIGIN = "http://status-smoke.test"
SCHEMA = {
    "form_id": "temp_status_smoke",
    "title": "Status Smoke",
    "description": "",
    "settings": {},
    "questions": [
        {"id": "driver_phone", "title": "Phone", "type": "text", "required": True},
    ],
}


def _chromium_executable():
    env = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if env and os.path.exists(env):
        return env
    patterns = (
        "chromium-*/chrome-linux*/chrome",
        "chromium_headless_shell-*/chrome-linux*/chrome-headless-shell",
    )
    for pattern in patterns:
        hits = sorted(glob.glob(os.path.expanduser(f"~/.cache/ms-playwright/{pattern}")))
        if hits:
            return hits[-1]
    return None


def _render_html():
    html = FORM_HTML.read_text(encoding="utf-8")
    return (
        html.replace("{escaped_title}", SCHEMA["title"])
        .replace("{escaped_desc}", "")
        .replace("{test_banner_html}", "")
        .replace("{schema_json}", json.dumps(SCHEMA))
        .replace("{locked_ctv}", "")
        .replace("{active_ctv_slug}", "")
        .replace("{active_ref}", "")
        .replace("{tg_bot}", "SmokeBot")
        .replace("{'flex' if tg_bot else 'none'}", "flex")
    )


@pytest.fixture()
def page():
    executable = _chromium_executable()
    responses = {"queue": [], "delay": 0.0}

    def handle_status(route):
        if responses["delay"]:
            time.sleep(responses["delay"])
        if responses["queue"]:
            payload = responses["queue"].pop(0)
        else:
            payload = {"submission_id": "T-1", "status": "MANUAL_REVIEW", "reason": "waiting"}
        status = payload.pop("_status", 200) if isinstance(payload, dict) else 200
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(payload),
        )

    with sync_api.sync_playwright() as pw:
        launch_kwargs = {"executable_path": executable} if executable else {}
        try:
            browser = pw.chromium.launch(**launch_kwargs)
        except Exception as exc:  # no chromium build installed
            pytest.skip(f"Chromium unavailable: {exc}")
        ctx = browser.new_context()
        page = ctx.new_page()
        # Register the catch-all first: Playwright checks routes in reverse order.
        page.route(
            f"{ORIGIN}/**",
            lambda route: route.fulfill(status=200, content_type="text/html", body=_render_html())
            if "/forms/" in route.request.url
            else route.fulfill(status=200, content_type="application/javascript", body=""),
        )
        page.route(
            f"{ORIGIN}/api/v1/survey/status/**",
            handle_status,
        )
        page.goto(f"{ORIGIN}/forms/temp_status_smoke")
        page.wait_for_function("typeof rememberSubmissionStatus === 'function'")
        page.responses = responses
        yield page
        ctx.close()
        browser.close()


def _bar_state(page):
    return page.evaluate(
        """() => ({
            className: document.getElementById('submissionStatusBar').className,
            badge: document.getElementById('submissionStatusBadge').textContent.trim(),
            popupBadge: document.getElementById('subResultBadge').textContent.trim(),
            popupDisplay: document.getElementById('submissionResultModal').style.display,
            storage: localStorage.getItem('tb_last_submission_temp_status_smoke'),
            last: lastSubmissionStatus,
            pollTimer: statusPollTimer !== null,
            settleTimer: statusSettleTimer !== null,
            epoch: statusStateEpoch,
        })"""
    )


def test_pending_to_final_stops_polling_and_persists(page):
    page.evaluate(
        """() => rememberSubmissionStatus({
            submission_id: 'T-1', status: 'MANUAL_REVIEW', reason: 'waiting'
        })"""
    )
    state = _bar_state(page)
    assert "visible" in state["className"]
    assert state["badge"] == "Đang chờ duyệt"
    assert state["pollTimer"] is True

    page.responses["queue"].append(
        {"submission_id": "T-1", "status": "APPROVED", "reason": "Quản trị viên phê duyệt thủ công"}
    )
    page.evaluate("() => refreshSubmissionStatus(true)")

    state = _bar_state(page)
    assert state["badge"] == "Đã duyệt"
    assert state["pollTimer"] is False
    assert state["settleTimer"] is False
    assert json.loads(state["storage"])["status"] == "APPROVED"
    assert state["last"]["reason"].startswith("Quản trị viên")


def test_appeal_pending_is_non_terminal_and_rendered(page):
    page.evaluate(
        """() => rememberSubmissionStatus({
            submission_id: 'T-2', status: 'APPEAL_PENDING', reason: 'Tài xế yêu cầu kiểm tra lại'
        })"""
    )
    state = _bar_state(page)
    assert state["badge"] == "Đang chờ phúc khảo"
    assert state["pollTimer"] is True
    assert page.evaluate("() => isTerminalSubmissionStatus('APPEAL_PENDING')") is False


def test_redo_clears_every_response_artifact(page):
    page.evaluate(
        """() => {
            answers['driver_phone'] = '0900000000';
            rememberSubmissionStatus({submission_id: 'T-3', status: 'REJECTED', reason: 'bad'});
        }"""
    )
    before_id = page.evaluate("() => clientSubmissionId")
    page.click("#submissionStatusRedo")
    page.wait_for_timeout(200)

    # The popup's own redo path must tear the same state down.
    page.evaluate(
        """() => {
            answers['driver_phone'] = '0900000001';
            rememberSubmissionStatus({submission_id: 'T-3', status: 'REJECTED', reason: 'bad'});
            showSubmissionResultModal({submission_id: 'T-3', status: 'REJECTED', reason: 'bad'});
        }"""
    )
    page.click("#btnSubResultReset")
    page.wait_for_timeout(200)

    state = _bar_state(page)
    assert "visible" not in state["className"]
    assert state["popupDisplay"] == "none"
    assert state["storage"] is None
    assert state["last"] is None
    assert state["pollTimer"] is False
    assert state["settleTimer"] is False
    assert page.evaluate("() => Object.keys(answers).length") == 0
    assert page.evaluate("() => document.querySelector('#body_driver_phone input').value") == ""
    assert page.evaluate("() => clientSubmissionId") != before_id


def test_in_flight_refresh_cannot_resurrect_a_reset(page):
    page.evaluate(
        """() => rememberSubmissionStatus({
            submission_id: 'T-4', status: 'MANUAL_REVIEW', reason: 'waiting'
        })"""
    )
    page.responses["queue"].append(
        {"submission_id": "T-4", "status": "APPROVED", "reason": "late verdict"}
    )
    page.responses["delay"] = 0.7
    page.evaluate("() => { refreshSubmissionStatus(true); return true; }")
    page.evaluate("() => resetFormFromModal()")
    page.wait_for_timeout(1400)

    state = _bar_state(page)
    assert "visible" not in state["className"]
    assert state["storage"] is None
    assert state["last"] is None


def test_transient_error_keeps_auto_refresh_alive(page):
    page.evaluate(
        """() => rememberSubmissionStatus({
            submission_id: 'T-5', status: 'MANUAL_REVIEW', reason: 'waiting'
        })"""
    )
    page.responses["queue"].append({"_status": 503, "detail": "upstream down"})
    page.evaluate("() => refreshSubmissionStatus(true)")

    state = _bar_state(page)
    assert state["badge"] == "Đang chờ duyệt"
    assert state["pollTimer"] is True, "a single failed poll must not kill auto-refresh"
    assert state["last"]["status"] == "MANUAL_REVIEW"

    page.responses["queue"].append(
        {"submission_id": "T-5", "status": "APPROVED", "reason": "finally approved"}
    )
    page.evaluate("() => refreshSubmissionStatus(true)")
    state = _bar_state(page)
    assert state["badge"] == "Đã duyệt"
    assert state["pollTimer"] is False
