"""Real PostgreSQL contracts for inactive-producer Wave 4A workers."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from core import outbox_delivery as queue
from services.outbox_types import Outcome
from tests.postgres_harness import apply_schema, resolve_test_database_url

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database():
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("disposable PostgreSQL is required")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL")
    dsn = resolve_test_database_url()
    with psycopg.connect(dsn, autocommit=True) as conn:
        apply_schema(conn)
        for version in ("001_submission_attempts", "002_outbox_delivery", "003_outbox_operations"):
            path = ROOT / "scripts/migrations" / f"{version}.sql"
            sql = path.read_text().replace(":'migration_sha256'", "'" + hashlib.sha256(path.read_bytes()).hexdigest() + "'")
            sql = sql.replace(":'migration_001_sha256'", "'" + queue.MIGRATION_001_CHECKSUM + "'")
            sql = sql.replace(":'migration_002_sha256'", "'" + hashlib.sha256(queue.MIGRATION_002.read_bytes()).hexdigest() + "'")
            conn.execute(sql)
    ids = []
    users = []
    events = []
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        def submission(*, owner="outbox_v2", status="APPROVED", collaborator=None):
            sid = "VN-QUEUE-" + uuid4().hex
            conn.execute(
                "INSERT INTO survey_submissions(submission_id,status,work_owner,collaborator_id) VALUES (%s,%s,%s,%s)",
                (sid, status, owner, collaborator),
            )
            ids.append(sid)
            return sid

        def event(target="TELEGRAM_NOTIFY", sid=None, payload=None, key=True):
            sid = sid or submission()
            data = {"decision_version": 0, "media_version": 1, "policy": {"enabled": True, "version": "test"}}
            data.update(payload or {})
            row = conn.execute(
                "INSERT INTO outbox_events(event_type,flow_target,aggregate_id,payload,action_key) "
                "VALUES ('survey.validated',%s,%s,%s::jsonb,%s) RETURNING id",
                (target, sid, json.dumps(data), uuid4().hex if key else None),
            ).fetchone()
            events.append(row["id"])
            return row["id"]

        def read(eid):
            return conn.execute("SELECT * FROM outbox_events WHERE id=%s", (eid,)).fetchone()

        try:
            yield dsn, conn, submission, event, read, users
        finally:
            conn.execute("DELETE FROM outbox_delivery_attempts WHERE event_id=ANY(%s)", (events,))
            conn.execute("DELETE FROM outbox_events WHERE id=ANY(%s)", (events,))
            conn.execute("DELETE FROM collaborator_billing_ledger WHERE submission_id=ANY(%s)", (ids,))
            conn.execute("DELETE FROM survey_submissions WHERE submission_id=ANY(%s)", (ids,))
            conn.execute("DELETE FROM authorized_users WHERE telegram_user_id=ANY(%s)", (users,))


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_unique_claim_fencing_retry_exhaustion_and_uncertain_expiry(database):
    dsn, conn, submission, event, read, _ = database
    eid = event()
    batches = await asyncio.gather(queue.claim(dsn, 1), queue.claim(dsn, 1))
    claimed = [e for batch in batches for e in batch if e["id"] == eid]
    assert len(claimed) == 1
    first = claimed[0]
    assert read(eid)["attempts"] == 1
    assert await queue.finish(dsn, first, Outcome("RETRYABLE", "RATE_LIMITED", retry_after=30))
    row = read(eid)
    assert row["status"] == "PENDING" and row["available_at"] > row["locked_at"]
    assert not await queue.claim(dsn, 1)
    for attempt in range(2, 6):
        conn.execute("UPDATE outbox_events SET available_at=NOW()-interval '1 second' WHERE id=%s", (eid,))
        (current,) = await queue.claim(dsn, 1)
        assert current["attempts"] == attempt
        assert not await queue.finish(dsn, first, Outcome("SUCCEEDED", receipt={"message_id": 999}))
        assert not await queue.renew(dsn, first)
        assert await queue.finish(dsn, current, Outcome("RETRYABLE", "RATE_LIMITED"))
    assert read(eid)["status"] == "FAILED"
    assert read(eid)["terminal_reason"] == "ATTEMPTS_EXHAUSTED"
    assert not await queue.claim(dsn)

    telegram = event()
    (send,) = await queue.claim(dsn, 1)
    assert await queue.renew(dsn, send, request_started=True)
    conn.execute("UPDATE outbox_events SET lease_until=NOW()-interval '1 second' WHERE id=%s", (telegram,))
    assert not await queue.claim(dsn)
    assert read(telegram)["terminal_reason"] == "DELIVERY_UNCERTAIN"
    assert read(telegram)["attempts"] == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_replay_safe_expiry_heartbeat_and_legacy_untouched(database):
    dsn, conn, submission, event, read, _ = database
    legacy = event("ALL", submission(owner="legacy"), key=False)
    safe = event("R2_UPLOAD")
    (first,) = await queue.claim(dsn, 1)
    assert first["id"] == safe
    assert await queue.renew(dsn, first, request_started=True)
    conn.execute("UPDATE outbox_events SET lease_until=NOW()-interval '1 second' WHERE id=%s", (safe,))
    (second,) = await queue.claim(dsn, 1)
    assert second["claimed_by"] != first["claimed_by"] and second["attempts"] == 2
    assert not await queue.finish(dsn, first, Outcome("TERMINAL", "LATE_FAILURE"))
    assert read(safe)["status"] == "PROCESSING"
    assert await queue.renew(dsn, second)
    assert await queue.finish(dsn, second, Outcome("SUCCEEDED", receipt={"key": "private/key", "sha256": "a" * 64}))
    assert read(legacy)["status"] == "PENDING" and read(legacy)["attempts"] == 0
    assert not await queue.claim(dsn)
    assert (await queue.worker_health(dsn))["legacy_unreconciled"] >= 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_billing_replay_preserves_existing_paid_ledger_and_current_policy(database):
    dsn, conn, submission, event, read, users = database
    collaborator = int(uuid4().int % 1_000_000_000) + 8_000_000_000
    users.append(collaborator)
    conn.execute("INSERT INTO authorized_users(telegram_user_id,full_name,role) VALUES (%s,'Test','COLLABORATOR')", (collaborator,))
    sid = submission(collaborator=collaborator)
    payload = {"collaborator_id": collaborator, "amount_vnd": 12500}
    first = event("BILLING", sid, payload)
    assert await queue.process(dsn) == 1
    ledger = conn.execute("SELECT * FROM collaborator_billing_ledger WHERE submission_id=%s", (sid,)).fetchone()
    assert read(first)["result"]["receipt"]["ledger_id"] == str(ledger["transaction_id"])
    conn.execute("UPDATE collaborator_billing_ledger SET payout_status='PAID' WHERE submission_id=%s", (sid,))
    second = event("BILLING", sid, payload)
    assert await queue.process(dsn) == 1
    rows = conn.execute("SELECT * FROM collaborator_billing_ledger WHERE submission_id=%s", (sid,)).fetchall()
    assert len(rows) == 1 and rows[0]["payout_status"] == "PAID"
    assert read(second)["result"]["receipt"] == read(first)["result"]["receipt"]
    stale = event("BILLING", sid, {**payload, "decision_version": 99})
    assert await queue.process(dsn) == 1
    assert read(stale)["result"]["outcome"] == "SKIPPED"
    conflict = event("BILLING", sid, {**payload, "amount_vnd": 500})
    assert await queue.process(dsn) == 1
    assert read(conflict)["terminal_reason"] == "BILLING_RECEIPT_CONFLICT"
    assert conn.execute("SELECT payout_amount_vnd FROM collaborator_billing_ledger WHERE submission_id=%s", (sid,)).fetchone()["payout_amount_vnd"] == 12500


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_target_fairness_and_unknown_target_never_succeed(database):
    dsn, conn, submission, event, read, _ = database
    for _ in range(4):
        event("GDRIVE_EXPORT")
    telegram = event("TELEGRAM_NOTIFY")
    billing = event("BILLING")
    first = await queue.claim(dsn, 2)
    assert len(first) == 2 and len({e["flow_target"] for e in first}) == 2
    for e in first:
        await queue.finish(dsn, e, Outcome("POLICY_SKIPPED", "POLICY_DISABLED"))
    second = await queue.claim(dsn, 2)
    assert {telegram, billing} <= {e["id"] for e in first + second}
    for e in second:
        await queue.finish(dsn, e, Outcome("POLICY_SKIPPED", "POLICY_DISABLED"))
    unknown = event("ZALO_ZNS")
    assert await queue.process(dsn, flow_target="ZALO_ZNS") == 1
    assert read(unknown)["status"] == "FAILED"
    assert read(unknown)["terminal_reason"] == "UNSUPPORTED_TARGET"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_heartbeat_prevents_reclaim_and_failure_does_not_block_other_target(database, monkeypatch):
    from services import outbox_notifications as notifications

    dsn, conn, submission, event, read, _ = database
    failed = event("LARK_SYNC")
    sent = event("TELEGRAM_NOTIFY")
    entered = asyncio.Event()
    renewed = asyncio.Event()
    original = queue.renew

    async def renew(*args, **kwargs):
        result = await original(*args, **kwargs)
        if result and not kwargs.get("request_started"):
            renewed.set()
        return result

    async def execute(item, current, conn_str, guard):
        assert await guard()
        if item["flow_target"] == "LARK_SYNC":
            entered.set()
            await renewed.wait()
            return Outcome("RETRYABLE", "RATE_LIMITED", retry_after=60)
        await entered.wait()
        return Outcome("SUCCEEDED", receipt={"bot_id": "123", "chat_id": 77, "message_id": 88})

    monkeypatch.setattr(queue, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(queue, "renew", renew)
    monkeypatch.setattr(notifications, "execute", execute)
    assert await asyncio.wait_for(queue.process(dsn), 5) == 2
    assert renewed.is_set()
    assert read(failed)["status"] == "PENDING"
    assert read(sent)["result"]["outcome"] == "SUCCEEDED"
    assert not await queue.claim(dsn)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_expired_exhausted_claim_and_long_retry_after_are_operator_visible(database):
    dsn, conn, submission, event, read, _ = database
    exhausted = event("R2_UPLOAD")
    conn.execute("UPDATE outbox_events SET attempts=4 WHERE id=%s", (exhausted,))
    (item,) = await queue.claim(dsn, 1)
    assert item["attempts"] == 5
    conn.execute("UPDATE outbox_events SET lease_until=NOW()-interval '1 second' WHERE id=%s", (exhausted,))
    assert not await queue.claim(dsn)
    assert read(exhausted)["status"] == "FAILED"
    assert read(exhausted)["terminal_reason"] == "ATTEMPTS_EXHAUSTED"
    deferred = event("TELEGRAM_NOTIFY")
    (item,) = await queue.claim(dsn, 1)
    assert await queue.finish(dsn, item, Outcome("RETRYABLE", "RATE_LIMITED", retry_after=901))
    assert read(deferred)["status"] == "FAILED"
    assert read(deferred)["terminal_reason"] == "OPERATOR_DEFERRED"
    due = read(deferred)["available_at"]
    assert (due - read(deferred)["locked_at"]).total_seconds() >= 901
    from psycopg_pool import AsyncConnectionPool
    from services.action_checklist import retry_actions
    async with AsyncConnectionPool(dsn, min_size=1, max_size=1, open=False) as pool:
        async with pool.connection() as retry_conn:
            assert await retry_actions(retry_conn, read(exhausted)["aggregate_id"], "test-operator") == [str(exhausted)]
            assert await retry_actions(retry_conn, read(deferred)["aggregate_id"], "test-operator") == [str(deferred)]
            await retry_conn.commit()
    assert read(exhausted)["status"] == "PENDING" and read(exhausted)["attempts"] == 0
    assert read(deferred)["status"] == "PENDING" and read(deferred)["available_at"] == due
    claimed = await queue.claim(dsn)
    assert [item["id"] for item in claimed] == [exhausted]
    assert await queue.finish(dsn, claimed[0], Outcome("POLICY_SKIPPED", "POLICY_DISABLED"))


@pytest.mark.asyncio
async def test_worker_disabled_never_connects_or_claims(monkeypatch):
    from config import settings
    from workers import outbox_worker

    monkeypatch.setattr(settings, "OUTBOX_V2_WORKER_ENABLED", False)

    async def forbidden(*args, **kwargs):
        pytest.fail("disabled worker must not connect or claim")

    monkeypatch.setattr(outbox_worker, "process_outbox_batch", forbidden)
    monkeypatch.setattr(queue, "worker_health", forbidden)
    task = asyncio.create_task(outbox_worker.run_outbox_daemon("invalid://must-not-connect"))
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_provider_acceptance_before_lost_ack_never_resends(database, monkeypatch):
    from services import outbox_notifications as notifications

    dsn, conn, submission, event, read, _ = database
    eid = event("TELEGRAM_NOTIFY")
    sends = []

    async def accepted(item, current, conn_str, guard):
        assert await guard()
        sends.append(item["id"])
        return Outcome("SUCCEEDED", receipt={"bot_id": 123, "chat_id": 77, "message_id": 88})

    original_finish = queue.finish

    async def lost_ack(*args):
        raise ConnectionError("local acknowledgement unavailable")

    monkeypatch.setattr(notifications, "execute", accepted)
    monkeypatch.setattr(queue, "finish", lost_ack)
    with pytest.raises(ConnectionError):
        await queue.process(dsn)
    assert read(eid)["status"] == "PROCESSING"
    monkeypatch.setattr(queue, "finish", original_finish)
    conn.execute("UPDATE outbox_events SET lease_until=NOW()-interval '1 second' WHERE id=%s", (eid,))
    assert await queue.process(dsn) == 0
    assert sends == [eid]
    assert read(eid)["terminal_reason"] == "DELIVERY_UNCERTAIN"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_drive_action_respects_existing_bulk_advisory_exclusion(database):
    from services import outbox_exports
    from services.google_drive_delivery import DRIVE_SYNC_LOCK_KEY

    dsn, conn, submission, event, read, _ = database
    conn.execute("SELECT pg_advisory_lock(%s)", (DRIVE_SYNC_LOCK_KEY,))

    async def forbidden():
        pytest.fail("busy Drive lock must prevent provider operations")

    action = {"aggregate_id": "VN-LOCK", "flow_target": "GDRIVE_EXPORT", "aggregate_kind": "submission",
              "payload": {"policy": {"enabled": True, "version": "test"}, "media_version": 1,
                          "media_manifest": [], "destination": {"private": True, "folder_id": "folder"}}}
    try:
        result = await outbox_exports.execute(action, {"status": "APPROVED"}, dsn, forbidden)
        assert (result.outcome, result.reason) == ("RETRYABLE", "DRIVE_SYNC_BUSY")
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (DRIVE_SYNC_LOCK_KEY,))


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_guard_progress_merges_evidence_and_rejects_stale_writes(database, monkeypatch):
    from services import outbox_notifications as notifications

    dsn, conn, submission, event, read, _ = database
    eid = event("TELEGRAM_NOTIFY")
    guards = []
    async def interrupted(item, current, conn_str, guard):
        guards.append(guard)
        assert await guard()
        assert await guard.record({"files": [{"file_id": "remote-file"}]})
        assert await guard.record({"tables": [{"file_id": "remote-sheet"}]})
        raise ConnectionError("provider result unavailable")
    monkeypatch.setattr(notifications, "execute", interrupted)
    assert await queue.process(dsn) == 1
    assert read(eid)["terminal_reason"] == "DELIVERY_UNCERTAIN"
    receipt = conn.execute("SELECT receipt FROM outbox_delivery_attempts WHERE event_id=%s", (eid,)).fetchone()["receipt"]
    assert receipt == {"files": [{"file_id": "remote-file"}], "tables": [{"file_id": "remote-sheet"}]}
    assert read(eid)["result"]["receipt"] == receipt
    assert not await guards[0].record({"files": []})
    assert conn.execute("SELECT receipt FROM outbox_delivery_attempts WHERE event_id=%s", (eid,)).fetchone()["receipt"] == receipt


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("target,started,expected", [("R2_UPLOAD", True, "PENDING"), ("TELEGRAM_NOTIFY", False, "PENDING"), ("TELEGRAM_NOTIFY", True, "FAILED")])
async def test_cancellation_uses_persisted_attempt_evidence(database, monkeypatch, target, started, expected):
    from services import outbox_notifications, outbox_exports
    dsn, conn, submission, event, read, _ = database
    eid = event(target)
    ready = asyncio.Event()
    async def paused(item, current, conn_str, guard):
        if started:
            assert await guard()
        ready.set()
        await asyncio.Future()
    monkeypatch.setattr(outbox_notifications, "execute", paused)
    monkeypatch.setattr(outbox_exports, "execute", paused)
    task = asyncio.create_task(queue.process(dsn))
    await asyncio.wait_for(ready.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert read(eid)["status"] == expected
    assert read(eid)["terminal_reason"] == ("DELIVERY_UNCERTAIN" if expected == "FAILED" else None)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_worker_health_is_read_only_and_reports_only_actual_activity(database, monkeypatch):
    from config import settings
    dsn, conn, submission, event, read, _ = database
    worker_id = "health-" + uuid4().hex
    monkeypatch.setattr(settings, "OUTBOX_WORKER_ID", worker_id)
    before = await queue.worker_health(dsn)
    assert before["ready"] and before["heartbeat_age_seconds"] is None
    assert conn.execute("SELECT count(*) AS count FROM outbox_worker_heartbeats WHERE worker_id=%s", (worker_id,)).fetchone()["count"] == 0
    await queue.record_worker_poll(dsn)
    after = await queue.worker_health(dsn)
    assert 0 <= after["heartbeat_age_seconds"] < 5
    assert after["worker_id"] == worker_id and after["schema_version"] == "002"
    conn.execute("UPDATE outbox_worker_heartbeats SET last_poll_at=NOW()+interval '1 minute' WHERE worker_id=%s", (worker_id,))
    assert (await queue.worker_health(dsn))["heartbeat_age_seconds"] < 0
    conn.execute("DELETE FROM outbox_worker_heartbeats WHERE worker_id=%s", (worker_id,))


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_worker_resumes_after_database_poll_failure_without_false_heartbeat(database, monkeypatch):
    from config import settings
    from workers import outbox_worker

    dsn, conn, _, event, read, _ = database
    eid = event(payload={"policy": {"enabled": False, "version": "test"}})
    worker_id = "recovery-" + uuid4().hex
    monkeypatch.setattr(settings, "OUTBOX_WORKER_ID", worker_id)
    monkeypatch.setattr(settings, "OUTBOX_V2_WORKER_ENABLED", True)
    process = outbox_worker.process_outbox_batch
    failed = False

    async def interrupted_poll(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise psycopg.OperationalError("disposable poll failure")
        if read(eid)["status"] == "PROCESSED":
            raise asyncio.CancelledError
        assert (await queue.worker_health(dsn))["heartbeat_age_seconds"] is None
        return await process(*args, **kwargs)

    monkeypatch.setattr(outbox_worker, "process_outbox_batch", interrupted_poll)
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(outbox_worker.run_outbox_daemon(dsn, poll_interval_sec=0), 5)
        assert read(eid)["result"]["outcome"] == "SKIPPED"
        assert (await queue.worker_health(dsn))["heartbeat_age_seconds"] is not None
    finally:
        conn.execute("DELETE FROM outbox_worker_heartbeats WHERE worker_id=%s", (worker_id,))


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["submission", "binding", "renew"])
async def test_pre_send_database_failure_is_retryable_not_delivery_uncertainty(database, monkeypatch, boundary):
    from services import outbox_notifications

    dsn, _, _, event, read, _ = database
    eid = event()
    original_submission, original_binding, original_renew = queue._submission, queue._binding_valid, queue.renew
    sent = []
    entered_handler = False

    async def fail_once(original, *args, **kwargs):
        nonlocal entered_handler
        if entered_handler:
            entered_handler = False
            raise psycopg.OperationalError("disposable pre-send failure")
        return await original(*args, **kwargs)

    async def send(item, current, conn_str, guard):
        nonlocal entered_handler
        entered_handler = True
        if await guard():
            sent.append(item["id"])
        return Outcome("UNCERTAIN", "LEASE_LOST")

    original = {"submission": original_submission, "binding": original_binding, "renew": original_renew}[boundary]
    async def flaky(*args, **kwargs):
        if boundary == "binding" and entered_handler:
            from unittest.mock import AsyncMock
            item = args[1]
            item = {**item, "payload": {**item["payload"], "chat_id": 17,
                    "recipient_binding": {"telegram_user_id": 17, "phone": "0900000017",
                                          "verified_at": "2026-01-01", "submitted_at": "2026-01-02"}}}
            with monkeypatch.context() as patch:
                patch.setattr(queue, "_connect", AsyncMock(side_effect=psycopg.OperationalError("disposable binding failure")))
                return await original_binding(args[0], item)
        return await fail_once(original, *args, **kwargs)

    monkeypatch.setattr(queue, {"submission": "_submission", "binding": "_binding_valid", "renew": "renew"}[boundary], flaky)
    monkeypatch.setattr(outbox_notifications, "execute", send)
    assert await queue.process(dsn) == 1
    assert sent == []
    assert read(eid)["status"] == "PENDING"
    assert read(eid)["result"]["outcome"] == "RETRYABLE"
    assert read(eid)["terminal_reason"] is None
