"""Telegram moderation producers with schema 002, real row locks, and no provider I/O."""
import asyncio
import io
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from PIL import Image
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from config import settings
from core import outbox_delivery as queue
from core.durable_work import enqueue_submission_work
from core.phone_links import PHONE_LINK_TABLE_STATEMENTS
from services import submission_media, telegram_polling as telegram
from tests.test_outbox_delivery import database

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]
ADMIN_GROUP = -1001234567890
ADMIN = 712301


def callback(data, *, actor=ADMIN, chat_id=ADMIN_GROUP):
    return {
        "id": uuid4().hex, "data": data, "from": {"id": actor},
        "message": {"chat": {"id": chat_id}, "message_id": 1},
    }


@pytest.fixture
def moderation(database, monkeypatch, tmp_path):
    dsn, conn, submission, _, _, users = database
    for statement in PHONE_LINK_TABLE_STATEMENTS:
        conn.execute(statement)
    monkeypatch.setattr(settings, "DURABLE_MODERATION_ENABLED", True)
    monkeypatch.setattr(settings, "DURABLE_APPEALS_ENABLED", True)
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", str(ADMIN_GROUP))
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", "101:not-a-live-token")
    monkeypatch.setattr(settings, "BILLING_PAYOUT_VND", 12500)
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(tmp_path))
    ids, phones = [], []

    def make(*, status="MANUAL_REVIEW", owner="outbox_v2", linked=True):
        user = 80_000_000_000 + uuid4().int % 10_000_000_000
        phone = f"09{uuid4().int % 100_000_000:08d}"
        sid = submission(owner=owner, status=status)
        ids.append(sid)
        phones.append(phone)
        if linked:
            conn.execute(
                "INSERT INTO phone_telegram_links(phone_normalized, telegram_user_id, source, updated_at) "
                "VALUES (%s,%s,'verified_contact',NOW()-INTERVAL '2 hours')", (phone, user),
            )
        conn.execute(
            "UPDATE survey_submissions SET driver_phone=%s, form_data=%s WHERE submission_id=%s",
            (phone, Jsonb({"_delivery_policy": {"source": "ctv", "version": "test-v1"}}), sid),
        )
        return sid, phone, user

    try:
        yield dsn, conn, make, users
    finally:
        conn.execute("DELETE FROM outbox_delivery_attempts WHERE event_id IN (SELECT id FROM outbox_events WHERE aggregate_id=ANY(%s))", (ids,))
        conn.execute("DELETE FROM outbox_events WHERE aggregate_id=ANY(%s)", (ids,))
        conn.execute("DELETE FROM system_audit_log WHERE entity_id=ANY(%s)", (ids,))
        conn.execute("DELETE FROM driver_registered_vehicles WHERE driver_phone=ANY(%s)", (phones,))
        conn.execute("DELETE FROM phone_telegram_links WHERE phone_normalized=ANY(%s)", (phones,))


def bot(pool, *, driver=False):
    cls = telegram.DriverBotRunner if driver else telegram.TelegramPollingRunner
    runner = cls(bot_token="mock_tg_token", db_pool=pool)
    runner.send_message = AsyncMock()
    runner.answer_callback_query = AsyncMock()
    runner.edit_message_reply_markup = AsyncMock()
    runner.get_client = AsyncMock(side_effect=AssertionError("unexpected direct provider call"))
    runner._notify_parties_of_decision = AsyncMock(side_effect=AssertionError("unexpected direct party dispatch"))
    return runner


def events(conn, sid):
    return conn.execute("SELECT * FROM outbox_events WHERE aggregate_id=%s ORDER BY created_at,id", (sid,)).fetchall()


def decision(conn, sid):
    row = conn.execute(
        "SELECT status,decision_version FROM survey_submissions WHERE submission_id=%s", (sid,),
    ).fetchone()
    return row["status"], row["decision_version"]


async def test_approval_rederives_old_skips_once_without_direct_dispatch(moderation, monkeypatch):
    dsn, conn, make, users = moderation
    monkeypatch.setattr(settings, "LARK_APP_ID", "cli_mock_id")
    sid, _, ctv = make()
    users.append(ctv)
    conn.execute("INSERT INTO authorized_users(telegram_user_id,full_name,role) VALUES (%s,'Test','COLLABORATOR')", (ctv,))
    conn.execute("UPDATE survey_submissions SET collaborator_id=%s WHERE submission_id=%s", (ctv, sid))
    async with AsyncConnectionPool(dsn, open=False, min_size=1, max_size=4) as pool:
        async with pool.connection() as tx:
            await enqueue_submission_work(tx, sid)
        await queue.process(dsn, flow_target="BILLING")
        old_billing = next(e for e in events(conn, sid) if e["flow_target"] == "BILLING")
        assert old_billing["result"]["outcome"] == "SKIPPED"

        # Existing ownership remains authoritative with the producer flag off.
        monkeypatch.setattr(settings, "DURABLE_MODERATION_ENABLED", False)
        runner = bot(pool)
        await runner.handle_callback_query(callback(f"admin_req_approve:{sid}"))
        assert decision(conn, sid) == ("MANUAL_REVIEW", 0)
        confirm = runner.edit_message_reply_markup.await_args.kwargs["reply_markup"]["inline_keyboard"][1][0]["callback_data"]
        assert confirm == f"admin_approve:{sid.upper()}"
        await asyncio.gather(*(runner.handle_callback_query(callback(confirm)) for _ in range(2)))
        assert decision(conn, sid) == ("APPROVED", 1)
        runner._notify_parties_of_decision.assert_not_awaited()
        runner.send_message.assert_not_awaited()
        runner.get_client.assert_not_awaited()

    derived = [e for e in events(conn, sid) if e["payload"]["decision_version"] == 1]
    assert sorted(e["flow_target"] for e in derived) == [
        "BILLING", "GDRIVE_EXPORT", "LARK_SYNC", "R2_UPLOAD", "TELEGRAM_NOTIFY", "TELEGRAM_NOTIFY",
    ]
    lark = next(e for e in derived if e["flow_target"] == "LARK_SYNC")
    assert lark["payload"]["policy"]["enabled"] is False
    assert lark["payload"]["policy"]["skip_reason"] == "INELIGIBLE_OR_UNCONFIGURED"
    assert {e["payload"]["chat_id"] for e in derived if e["flow_target"] == "TELEGRAM_NOTIFY"} == {str(ADMIN_GROUP), ctv}
    assert conn.execute("SELECT count(*) AS n FROM system_audit_log WHERE entity_id=%s AND action='STATUS_TRANSITION'", (sid,)).fetchone()["n"] == 1
    await queue.process(dsn, flow_target="BILLING")
    await queue.process(dsn, flow_target="BILLING")
    ledger = conn.execute("SELECT payout_amount_vnd FROM collaborator_billing_ledger WHERE submission_id=%s", (sid,)).fetchall()
    assert ledger == [{"payout_amount_vnd": 12500}]
    assert next(e for e in events(conn, sid) if e["id"] == old_billing["id"])["result"]["outcome"] == "SKIPPED"


async def test_appeal_and_cancel_race_approval_without_legacy_sends(moderation):
    dsn, conn, make, _ = moderation
    sid, _, user = make(status="REJECTED")
    legacy, _, legacy_user = make(status="REJECTED", owner="legacy")
    async with AsyncConnectionPool(dsn, open=False, min_size=1, max_size=4) as pool:
        driver, admin = bot(pool, driver=True), bot(pool)
        await driver.handle_driver_appeal(user, user, sid.lower())
        assert decision(conn, sid) == ("APPEAL_PENDING", 1)
        await driver.handle_cancel_request(user + 1, user + 1, sid)
        assert decision(conn, sid) == ("APPEAL_PENDING", 1)
        await asyncio.gather(
            driver.handle_cancel_request(user, user, sid),
            admin.handle_callback_query(callback(f"admin_approve:{sid}")),
        )
        terminal, version = decision(conn, sid)
        assert terminal in {"APPROVED", "REJECTED"} and version == 2
        assert {e["event_type"] for e in events(conn, sid)} == {"APPEAL_PENDING", terminal}
        assert all(e["flow_target"] != "ALL" for e in events(conn, sid))
        await driver.handle_driver_appeal(legacy_user, legacy_user, legacy)
        assert "đối soát" in driver.send_message.await_args.args[1]
        assert decision(conn, legacy) == ("REJECTED", 0) and not events(conn, legacy)
        assert await driver._get_submission_status(legacy, requester_telegram_id=legacy_user) == "REJECTED"
        driver.get_client.assert_not_awaited()
        admin._notify_parties_of_decision.assert_not_awaited()


async def test_repair_uses_binding_epoch_and_commits_media_with_approval(moderation, monkeypatch, tmp_path):
    dsn, conn, make, _ = moderation
    sid, phone, user = make(status="REJECTED")
    folder = tmp_path / sid
    folder.mkdir()
    (folder / "slot_0.webp").write_bytes(b"initial")
    async with AsyncConnectionPool(dsn, open=False, min_size=1, max_size=4) as pool:
        with pytest.raises(PermissionError):
            await submission_media.write_replacement(pool, sid, 0, b"wrong", requester_telegram_id=user + 1, approve_reason="test")
        conn.execute("UPDATE phone_telegram_links SET updated_at=NOW() WHERE phone_normalized=%s", (phone,))
        with pytest.raises(PermissionError):
            await submission_media.write_replacement(pool, sid, 0, b"stale", requester_telegram_id=user, approve_reason="test")
        assert decision(conn, sid) == ("REJECTED", 0)
        assert not events(conn, sid) and not (folder / "slot_0_v2.webp").exists()
        conn.execute("UPDATE phone_telegram_links SET updated_at=NOW()-INTERVAL '2 hours' WHERE phone_normalized=%s", (phone,))
        runner = bot(pool, driver=True)
        runner.pending_fixes[user] = {"sub_id": sid, "slot": "flat_0"}
        image = io.BytesIO()
        Image.new("RGB", (8, 8)).save(image, format="JPEG")
        runner.download_telegram_photo = AsyncMock(return_value=image.getvalue())
        monkeypatch.setattr(telegram, "compute_laplacian_blur_score", lambda *a, **kw: (True, 50.0))
        monkeypatch.setattr(telegram, "normalize_image_to_webp", lambda _: b"repaired")
        await runner.handle_photo_upload(user, user, [{"file_id": "test"}])
        runner.get_client.assert_not_awaited()
    assert decision(conn, sid) == ("APPROVED", 1)
    assert (folder / "slot_0_v2.webp").read_bytes() == b"repaired"
    queued = events(conn, sid)
    assert queued and all(e["flow_target"] != "ALL" for e in queued)
    assert all(e["payload"]["media_version"] == 2 and e["payload"]["decision_version"] == 1 for e in queued)


async def test_vehicle_confirmation_and_warning_truthful_outcomes(moderation, monkeypatch):
    dsn, conn, make, _ = moderation
    sid, phone, user = make()
    unlinked, _, _ = make(linked=False)
    conn.execute("INSERT INTO driver_registered_vehicles(driver_phone,license_plate,vehicle_type) VALUES (%s,'29E12345','BIKE')", (phone,))
    async with AsyncConnectionPool(dsn, open=False, min_size=1, max_size=4) as pool:
        runner = bot(pool)
        await runner.handle_callback_query(callback(f"admin_change_veh:{unlinked}:0912345678"))
        assert "bổ sung" in runner.answer_callback_query.await_args.args[1]
        runner.send_message.assert_not_awaited()
        conn.execute(
            "UPDATE survey_submissions SET vehicle_type='BIKE',raw_extracted_facts='{\"detected_plate\":\"30F-456.78\"}'::jsonb WHERE submission_id=%s",
            (sid,),
        )
        await runner.handle_callback_query(callback(f"admin_change_veh:{sid}:0912345678", chat_id=-123))
        runner.send_message.assert_not_awaited()
        await runner.handle_callback_query(callback(f"admin_change_veh:{sid}:0912345678"))
        assert not conn.execute("SELECT 1 FROM vehicle_change_history WHERE driver_phone=%s", (phone,)).fetchone()
        confirm = runner.send_message.await_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        await runner.handle_callback_query(callback(confirm, actor=ADMIN + 1))
        assert not conn.execute("SELECT 1 FROM vehicle_change_history WHERE driver_phone=%s", (phone,)).fetchone()
        await runner.handle_callback_query(callback(confirm))
        await runner.handle_callback_query(callback(confirm))
        history = conn.execute("SELECT previous_plate,new_plate,changed_by_admin_id FROM vehicle_change_history WHERE driver_phone=%s", (phone,)).fetchall()
        assert history == [{"previous_plate": "29E12345", "new_plate": "30F45678", "changed_by_admin_id": ADMIN}]
        assert conn.execute("SELECT license_plate FROM driver_registered_vehicles WHERE driver_phone=%s", (phone,)).fetchone()["license_plate"] == "30F45678"

        await runner.handle_callback_query(callback(f"admin_warn:{sid}:0912345678"))
        assert "xếp hàng" in runner.answer_callback_query.await_args.args[1]
        await runner.handle_callback_query(callback(f"admin_warn:{sid}:0912345678"))
        assert "không gửi thêm" in runner.answer_callback_query.await_args.args[1]
        await runner.handle_callback_query(callback(f"admin_warn:{unlinked}:0912345678"))
        assert "không gửi" in runner.answer_callback_query.await_args.args[1]
        runner.get_client.assert_not_awaited()

    (linked_warning,), (skipped_warning,) = events(conn, sid), events(conn, unlinked)
    assert linked_warning["payload"]["chat_id"] == user
    assert linked_warning["payload"]["recipient_binding"]["phone"] == phone
    assert skipped_warning["payload"]["policy"]["skip_reason"] == "UNLINKED_RECIPIENT"
    for warning in (linked_warning, skipped_warning):
        audit = conn.execute("SELECT details FROM system_audit_log WHERE entity_id=%s AND action='DRIVER_WARNING'", (warning["aggregate_id"],)).fetchall()
        assert len(audit) == 1 and audit[0]["details"]["intent_id"] == str(warning["id"])

    # Disable the linked test row rather than attempting any Telegram request.
    conn.execute("UPDATE outbox_events SET available_at=NOW()+INTERVAL '1 day' WHERE id=%s", (linked_warning["id"],))
    transport = AsyncMock(side_effect=AssertionError("unlinked warning must not send"))
    monkeypatch.setattr("services.outbox_notifications._telegram", transport)
    await queue.process(dsn, flow_target="TELEGRAM_NOTIFY")
    assert events(conn, unlinked)[0]["result"]["outcome"] == "SKIPPED"
    transport.assert_not_awaited()


async def test_audit_overrides_once_and_rejects_stale_decisions(moderation, monkeypatch):
    dsn, conn, make, _ = moderation
    sid, _, _ = make(status="APPROVED")
    stale_sid, _, _ = make(status="REJECTED")
    monkeypatch.setattr(telegram, "get_redis_client", lambda: None)
    async with AsyncConnectionPool(dsn, open=False, min_size=1, max_size=2) as pool:
        runner = bot(pool)
        runner.send_dossier_bundle = AsyncMock()

        async def open_audit(submission_id):
            await runner.handle_command(ADMIN_GROUP, f"/audit {submission_id}", from_user_id=ADMIN)
            request = runner.send_dossier_bundle.await_args.kwargs["reply_markup"]["inline_keyboard"][1][0]["callback_data"]
            runner.edit_message_reply_markup.reset_mock()
            await runner.handle_callback_query(callback(request, actor=ADMIN + 1))
            runner.edit_message_reply_markup.assert_not_awaited()
            await runner.handle_callback_query(callback(request))
            return runner.edit_message_reply_markup.await_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"]

        confirmed = await open_audit(sid)
        assert decision(conn, sid) == ("APPROVED", 0)
        await asyncio.gather(*(runner.handle_callback_query(callback(confirmed)) for _ in range(2)))
        assert decision(conn, sid) == ("REJECTED", 1)
        assert {event["event_type"] for event in events(conn, sid)} == {"REJECTED"}
        assert conn.execute("SELECT actor_id,details FROM system_audit_log WHERE entity_id=%s AND action='VERDICT_OVERRIDE'", (sid,)).fetchall() == [
            {"actor_id": f"admin:{ADMIN}", "details": {"from": "APPROVED", "to": "REJECTED"}}]

        confirm_stale = await open_audit(stale_sid)
        conn.execute("UPDATE survey_submissions SET moderated_at=NOW() WHERE submission_id=%s", (stale_sid,))
        await runner.handle_callback_query(callback(confirm_stale))
        assert decision(conn, stale_sid) == ("REJECTED", 0)
        assert not events(conn, stale_sid)
        runner._notify_parties_of_decision.assert_not_awaited()


async def test_admin_history_finds_ctv_and_message_target_phone(moderation):
    from core.ctv import CTV_TABLE_STATEMENTS

    dsn, conn, make, _ = moderation
    for statement in CTV_TABLE_STATEMENTS:
        conn.execute(statement)
    sid, phone, _ = make()
    ctv = "AUDIT_CTV_" + uuid4().hex[:8]
    ctv_phone = "07" + f"{uuid4().int % 100_000_000:08d}"
    conn.execute("INSERT INTO ctv_directory (code,phone) VALUES (%s,%s)", (ctv, ctv_phone))
    conn.execute("UPDATE survey_submissions SET ctv_code=%s,driver_phone=NULL,form_data=%s WHERE submission_id=%s",
                 (ctv, Jsonb({"_delivery_policy": {"source": "question", "question_id": "target"}, "target": phone}), sid))
    try:
        async with AsyncConnectionPool(dsn, open=False, min_size=1, max_size=2) as pool:
            runner = bot(pool)
            for ref in (ctv.lower(), "+84 " + phone[1:], ctv_phone):
                await runner.handle_command(ADMIN_GROUP, f"/surveys {ref}", from_user_id=ADMIN)
                assert sid in runner.send_message.await_args.args[1]
            await runner.handle_command(ADMIN + 1, f"/surveys {phone}", from_user_id=ADMIN + 1)
            assert sid not in runner.send_message.await_args.args[1]
    finally:
        conn.execute("DELETE FROM ctv_directory WHERE code=%s", (ctv,))


async def test_stats_uses_database_even_when_zero_and_fails_closed(moderation, monkeypatch, tmp_path):
    import re

    dsn, conn, _, _ = moderation
    path = tmp_path / "dev_submissions.json"
    path.write_text('[{"status":"APPROVED"}]')
    monkeypatch.setattr(telegram, "DEV_STORAGE_FILE", str(path))
    async with AsyncConnectionPool(dsn, open=False, min_size=1, max_size=1) as pool:
        runner = bot(pool)
        await runner.handle_command(ADMIN_GROUP, "/stats", from_user_id=ADMIN)
        total = conn.execute("SELECT count(*) AS n FROM survey_submissions").fetchone()["n"]
        assert re.findall(r"</b> (\d+)", runner.send_message.await_args.args[1])[0] == str(total)
    await runner.handle_command(ADMIN_GROUP, "/stats", from_user_id=ADMIN)
    assert not re.findall(r"</b> (\d+)", runner.send_message.await_args.args[1])
