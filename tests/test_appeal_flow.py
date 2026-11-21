# -*- coding: utf-8 -*-
"""Appeal flow: case-insensitive ids, strict existence, and stale-check cleanup."""

import pytest
from unittest.mock import AsyncMock, patch
from starlette.background import BackgroundTasks

from core.submission_ids import normalize_submission_id
from services.telegram_polling import DriverBotRunner, TelegramPollingRunner


LOWER = "vn-fd45d21608417a33"
UPPER = "VN-FD45D21608417A33"


class FakeCursor:
    def __init__(self, row=None):
        self.row = row
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return self.row


class FakeConn:
    def __init__(self, row=None):
        self.cur = FakeCursor(row)
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def cursor(self, *, row_factory=None):
        return self.cur

    async def commit(self):
        self.commits += 1


class FakePool:
    def __init__(self, row=None):
        self.conn = FakeConn(row)

    def connection(self):
        return self.conn


def test_normalize_submission_id_lowercase_and_hash():
    assert normalize_submission_id("vn-fd45d21608417a33") == UPPER
    assert normalize_submission_id(" #vn-fd45d21608417a33 ") == UPPER
    assert normalize_submission_id("VN-FD45D21608417A33") == UPPER


@pytest.mark.asyncio
async def test_unknown_submission_is_rejected_without_forward_or_update():
    runner = DriverBotRunner(bot_token="mock")
    send = AsyncMock()
    update = AsyncMock(return_value=(True, "APPEAL_PENDING"))

    with patch.object(runner, "send_message", send), \
         patch.object(runner, "_get_submission_status", AsyncMock(return_value=None)), \
         patch.object(runner, "_update_submission_status", update):
        await runner.handle_driver_appeal(9241303232, 9241303232, "vn-no-such-id")

    send.assert_awaited_once()
    assert "Không tìm thấy" in send.await_args[0][1]
    update.assert_not_awaited()
    assert 9241303232 not in runner.active_appeals


@pytest.mark.asyncio
async def test_lowercase_appeal_after_ai_rejection_clears_stale_check():
    runner = DriverBotRunner(bot_token="mock")
    runner.active_appeals[9241303232] = UPPER  # stale in-memory check from an older appeal
    send = AsyncMock()
    update = AsyncMock(return_value=(True, "APPEAL_PENDING"))

    with patch.object(runner, "send_message", send), \
         patch.object(runner, "_get_submission_status", AsyncMock(return_value="REJECTED")), \
         patch.object(runner, "_update_submission_status", update), \
         patch.object(runner, "get_driver_phone", AsyncMock(return_value="0932090967")):
        await runner.handle_driver_appeal(9241303232, 9241303232, LOWER)

    assert "đã được gửi thành công" in send.await_args[0][1]
    update.assert_awaited_once()
    assert update.await_args.args == (UPPER, "APPEAL_PENDING")
    assert update.await_args.kwargs["actor"] == "driver:9241303232"
    assert update.await_args.kwargs["requester_telegram_id"] == 9241303232
    assert runner.active_appeals[9241303232] == UPPER


@pytest.mark.asyncio
async def test_duplicate_open_appeal_is_still_blocked():
    runner = DriverBotRunner(bot_token="mock")
    runner.active_appeals[9241303232] = UPPER
    send = AsyncMock()
    update = AsyncMock(return_value=(True, "APPEAL_PENDING"))

    with patch.object(runner, "send_message", send), \
         patch.object(runner, "_get_submission_status", AsyncMock(return_value="APPEAL_PENDING")), \
         patch.object(runner, "_update_submission_status", update):
        await runner.handle_driver_appeal(9241303232, 9241303232, LOWER)

    send.assert_awaited_once()
    assert "đang hoạt động" in send.await_args[0][1]
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_approved_submission_is_not_reopened():
    runner = DriverBotRunner(bot_token="mock")
    send = AsyncMock()
    update = AsyncMock(return_value=(True, "APPEAL_PENDING"))

    with patch.object(runner, "send_message", send), \
         patch.object(runner, "_get_submission_status", AsyncMock(return_value="APPROVED")), \
         patch.object(runner, "_update_submission_status", update):
        await runner.handle_driver_appeal(9241303232, 9241303232, LOWER)

    assert "đã được DUYỆT" in send.await_args[0][1]
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_prune_stale_active_appeals_keeps_only_open_status():
    runner = DriverBotRunner(bot_token="mock")
    runner.active_appeals = {1: "vn-old-check", 2: UPPER}

    async def status_for(sub_id):
        return "APPEAL_PENDING" if normalize_submission_id(sub_id) == UPPER else "REJECTED"

    with patch.object(runner, "_get_submission_status", side_effect=status_for):
        await runner._prune_stale_active_appeals()

    assert 1 not in runner.active_appeals
    assert runner.active_appeals[2] == UPPER


@pytest.mark.asyncio
async def test_admin_approve_callback_normalizes_lowercase_id(monkeypatch):
    monkeypatch.setattr("services.telegram_polling.settings.TELEGRAM_ADMIN_GROUP_ID", "-100123")
    runner = TelegramPollingRunner(bot_token="mock", role="admin")
    update = AsyncMock(return_value=(True, "APPROVED"))
    notify = AsyncMock()
    cq = {
        "id": "cb1",
        "data": f"admin_approve:{LOWER}",
        "message": {"chat": {"id": -100123, "type": "supergroup"}, "message_id": 7},
        "from": {"id": 1},
    }

    with patch.object(runner, "_update_submission_status", update), \
         patch.object(runner, "_notify_parties_of_decision", notify), \
         patch.object(runner, "send_message", AsyncMock()), \
         patch.object(runner, "answer_callback_query", AsyncMock()), \
         patch.object(runner, "edit_message_reply_markup", AsyncMock()):
        await runner.handle_callback_query(cq)

    update.assert_awaited_once()
    assert update.await_args.args == (UPPER, "APPROVED")
    assert update.await_args.kwargs["actor"] == "admin:1"
    notify.assert_awaited_once_with(UPPER, "APPROVED", reason=update.await_args.kwargs["reason"])


@pytest.mark.asyncio
async def test_driver_appeal_button_normalizes_lowercase_id():
    runner = DriverBotRunner(bot_token="mock")
    appeal = AsyncMock()
    update = {
        "callback_query": {
            "id": "cb2",
            "data": f"driver_appeal:{LOWER}",
            "message": {"chat": {"id": 9241303232, "type": "private"}},
            "from": {"id": 9241303232},
        }
    }

    with patch.object(runner, "handle_driver_appeal", appeal), \
         patch.object(runner, "answer_callback_query", AsyncMock()):
        await runner.handle_update(update)

    appeal.assert_awaited_once_with(9241303232, 9241303232, UPPER)


@pytest.mark.asyncio
async def test_api_appeal_lowercase_updates_canonical_and_unknown_is_404():
    from routers import survey as survey_mod

    pool = FakePool(row=("REJECTED",))
    with patch("routers.survey.get_db_pool", return_value=pool), \
         patch("routers.survey.get_redis_client", return_value=None), \
         patch("routers.survey.send_telegram_notification", AsyncMock()):
        resp = await survey_mod.handle_survey_appeal(
            survey_mod.AppealRequest(
                submission_id=LOWER,
                driver_phone="0932090967",
                reason="Kiểm tra lại ảnh gốc",
            ),
            BackgroundTasks(),
        )
    assert resp.status_code == 200
    assert '"APPEAL_SUBMITTED"' in resp.body.decode()
    updates = [(sql, params) for sql, params in pool.conn.cur.calls if sql.startswith("UPDATE")]
    assert updates
    assert updates[0][1][1] == UPPER

    empty_pool = FakePool(row=None)
    with patch("routers.survey.get_db_pool", return_value=empty_pool), \
         patch("routers.survey.get_redis_client", return_value=None):
        missing = await survey_mod.handle_survey_appeal(
            survey_mod.AppealRequest(submission_id="vn-does-not-exist", driver_phone="0932090967"),
            BackgroundTasks(),
        )
    assert missing.status_code == 404
    assert '"NOT_FOUND"' in missing.body.decode()
    assert not any(sql.startswith("UPDATE") for sql, _ in empty_pool.conn.cur.calls)


@pytest.mark.asyncio
async def test_update_submission_status_is_case_insensitive_and_canonicalizes():
    runner = TelegramPollingRunner(bot_token="mock", role="admin", db_pool=FakePool(row=("REJECTED",)))
    success, status = await runner._update_submission_status(LOWER, "APPEAL_PENDING", reason="appeal", actor="admin:1")
    assert success is True and status == "APPEAL_PENDING"
    updates = [(sql, params) for sql, params in runner.db_pool.conn.cur.calls if sql.startswith("UPDATE")]
    assert updates
    assert updates[0][1][-1] == UPPER
    assert runner.db_pool.conn.commits == 1


def test_ai_verdict_clears_active_appeal_lock_on_driver_runner():
    from routers import survey as survey_mod

    class FakeRunner:
        def __init__(self):
            self.active_appeals = {9241303232: LOWER}

    fake = FakeRunner()
    with patch("routers.webhooks.get_driver_bot_runner", return_value=fake):
        survey_mod._clear_active_appeal_after_verdict(LOWER, "REJECTED")

    assert fake.active_appeals == {}
