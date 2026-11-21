"""Token-fenced durable claim/execute/finish boundary for transferred producers."""

import asyncio
import hashlib
import json
import logging
import re
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from core.schema_gate import MIGRATION_001_CHECKSUM
from services.outbox_types import Outcome

logger = logging.getLogger(__name__)
MIGRATION_002 = Path(__file__).resolve().parents[1] / "scripts/migrations/002_outbox_delivery.sql"
LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 15
EXECUTION_SLOTS = 2
REPLAY_SAFE = {"BILLING", "R2_UPLOAD"}
EXPORTS = {"BILLING", "R2_UPLOAD", "GDRIVE_EXPORT", "LARK_SYNC"}


async def _connect(conn_str):
    return await psycopg.AsyncConnection.connect(conn_str, row_factory=dict_row)


async def schema_ready(conn):
    try:
        checksum = hashlib.sha256(MIGRATION_002.read_bytes()).hexdigest()
        cur = await conn.execute("SELECT version, checksum FROM schema_migrations WHERE version IN ('001','002')")
        versions = {row["version"]: row["checksum"] for row in await cur.fetchall()}
        if versions != {"001": MIGRATION_001_CHECKSUM, "002": checksum}:
            return False
        await conn.execute(
            "SELECT available_at, claimed_by, max_attempts, terminal_reason, lease_until, "
            "attempts, action_key, aggregate_kind, result FROM outbox_events LIMIT 0"
        )
        await conn.execute("SELECT work_owner, decision_version FROM survey_submissions LIMIT 0")
        await conn.execute("SELECT event_id, claim_token, request_started, receipt FROM outbox_delivery_attempts LIMIT 0")
        return True
    except (OSError, psycopg.Error):
        return False


async def worker_health(conn_str):
    from config import settings

    async with await _connect(conn_str) as conn:
        ready = await schema_ready(conn)
        if not ready:
            return {"ready": False, "reason": "SCHEMA_002_REQUIRED"}
        operations = MIGRATION_002.with_name("003_outbox_operations.sql")
        cur = await conn.execute("SELECT checksum FROM schema_migrations WHERE version='003'")
        version = await cur.fetchone()
        if not version or version["checksum"] != hashlib.sha256(operations.read_bytes()).hexdigest():
            return {"ready": False, "reason": "OPERATIONS_SCHEMA_REQUIRED"}
        cur = await conn.execute(
            "SELECT count(*) FILTER (WHERE (action_key IS NULL OR flow_target='ALL') AND status IN ('PENDING','PROCESSING')) AS legacy, "
            "count(*) FILTER (WHERE status='FAILED') AS failed, "
            "count(*) FILTER (WHERE terminal_reason='DELIVERY_UNCERTAIN') AS uncertain, "
            "extract(epoch FROM NOW()-min(created_at) FILTER (WHERE status='PENDING')) AS oldest FROM outbox_events"
        )
        counts = await cur.fetchone()
        cur = await conn.execute("SELECT extract(epoch FROM NOW()-last_poll_at) AS age,build_id FROM outbox_worker_heartbeats WHERE worker_id=%s", (settings.OUTBOX_WORKER_ID,))
        heartbeat = await cur.fetchone()
        return {"ready": True, "schema_version": "002", "health_schema_version": "003",
                "supported_targets": ["BILLING", "R2_UPLOAD", "GDRIVE_EXPORT", "LARK_SYNC", "TELEGRAM_NOTIFY"],
                "worker_id": settings.OUTBOX_WORKER_ID,
                "build_id": heartbeat["build_id"] if heartbeat else None,
                "heartbeat_age_seconds": float(heartbeat["age"]) if heartbeat else None,
                "legacy_unreconciled": counts["legacy"], "failed_count": counts["failed"], "uncertain_count": counts["uncertain"],
                "queue_oldest_pending_seconds": max(0.0, float(counts["oldest"] or 0))}


async def record_worker_poll(conn_str):
    from config import settings

    async with await _connect(conn_str) as conn:
        await conn.execute(
            "INSERT INTO outbox_worker_heartbeats(worker_id,last_poll_at,build_id) VALUES (%s,NOW(),%s) "
            "ON CONFLICT(worker_id) DO UPDATE SET last_poll_at=EXCLUDED.last_poll_at,build_id=EXCLUDED.build_id",
            (settings.OUTBOX_WORKER_ID, settings.APP_BUILD_ID),
        )


async def _expire(conn):
    cur = await conn.execute(
        "SELECT e.*, COALESCE(a.request_started, FALSE) AS request_started "
        "FROM outbox_events e LEFT JOIN outbox_delivery_attempts a "
        "ON a.event_id=e.id AND a.claim_token=e.claimed_by "
        "WHERE e.action_key IS NOT NULL AND e.flow_target <> 'ALL' "
        "AND e.status='PROCESSING' AND e.lease_until <= NOW() "
        "ORDER BY e.lease_until LIMIT 100 FOR UPDATE OF e SKIP LOCKED"
    )
    for event in await cur.fetchall():
        uncertain = event["request_started"] and event["flow_target"] not in REPLAY_SAFE
        exhausted = event["attempts"] >= event["max_attempts"]
        reason = "DELIVERY_UNCERTAIN" if uncertain else "ATTEMPTS_EXHAUSTED" if exhausted else "LEASE_EXPIRED"
        status = "FAILED" if uncertain or exhausted else "PENDING"
        await conn.execute(
            "UPDATE outbox_events SET status=%s, terminal_reason=%s, claimed_by=NULL, lease_until=NULL, "
            "available_at=NOW(), result=COALESCE(result,'{}'::jsonb) || %s::jsonb WHERE id=%s AND claimed_by=%s AND status='PROCESSING'",
            (status, reason if status == "FAILED" else None,
             json.dumps({"outcome": "UNCERTAIN" if uncertain else "RETRYABLE", "reason": reason}),
             event["id"], event["claimed_by"]),
        )
        await conn.execute(
            "UPDATE outbox_delivery_attempts SET finished_at=NOW(), outcome=%s, reason=%s "
            "WHERE event_id=%s AND claim_token=%s AND finished_at IS NULL",
            ("UNCERTAIN" if uncertain else "RETRYABLE", reason,
             event["id"], event["claimed_by"]),
        )
        if event["aggregate_kind"] == "submission":
            await conn.execute(
                "UPDATE survey_submissions SET action_status=jsonb_set(COALESCE(action_status,'{}'), "
                "'{outbox_v2}', COALESCE(action_status->'outbox_v2','{}') || jsonb_build_object(%s::text,%s::jsonb),true) "
                "WHERE submission_id=%s AND work_owner='outbox_v2'",
                (event["action_key"], json.dumps({"status": status, "reason": reason}), event["aggregate_id"]),
            )
    # A fifth definitive failure must not leave a pending row forever.
    await conn.execute(
        "UPDATE outbox_events SET status='FAILED', terminal_reason='ATTEMPTS_EXHAUSTED', "
        "result=COALESCE(result,'{}'::jsonb) || jsonb_build_object('outcome','RETRYABLE','reason','ATTEMPTS_EXHAUSTED') "
        "WHERE action_key IS NOT NULL AND flow_target <> 'ALL' AND status='PENDING' AND attempts >= max_attempts"
    )


async def claim(conn_str, slots=EXECUTION_SLOTS, flow_target=None):
    if slots <= 0:
        return []
    async with await _connect(conn_str) as conn:
        if not await schema_ready(conn):
            raise RuntimeError("outbox migration 002 is absent or mismatched")
        await _expire(conn)
        cur = await conn.execute(
            """
            WITH target_history AS (
                SELECT flow_target, max(locked_at) AS last_claim FROM outbox_events GROUP BY flow_target
            ), eligible AS (
                SELECT e.id, history.last_claim,
                       row_number() OVER (PARTITION BY e.flow_target ORDER BY e.available_at,e.created_at,e.id) AS turn
                FROM outbox_events e LEFT JOIN target_history history ON history.flow_target=e.flow_target
                WHERE e.status='PENDING' AND e.available_at <= NOW() AND e.attempts < e.max_attempts
                  AND e.action_key IS NOT NULL AND e.flow_target <> 'ALL'
                  AND (%s::text IS NULL OR e.flow_target=%s)
                  AND (e.aggregate_kind <> 'submission' OR EXISTS (
                      SELECT 1 FROM survey_submissions s WHERE s.submission_id=e.aggregate_id AND s.work_owner='outbox_v2'))
            )
            SELECT e.* FROM outbox_events e JOIN eligible candidate ON candidate.id=e.id
            ORDER BY candidate.turn, candidate.last_claim NULLS FIRST, e.available_at, e.created_at, e.id
            LIMIT %s FOR UPDATE OF e SKIP LOCKED
            """,
            (flow_target, flow_target, min(slots, EXECUTION_SLOTS)),
        )
        events = await cur.fetchall()
        for event in events:
            token = uuid4().hex
            event["claimed_by"] = token
            event["attempts"] += 1
            await conn.execute(
                "UPDATE outbox_events SET status='PROCESSING', claimed_by=%s, attempts=attempts+1, "
                "locked_at=NOW(), lease_until=NOW()+make_interval(secs => %s), terminal_reason=NULL "
                "WHERE id=%s AND status='PENDING'",
                (token, LEASE_SECONDS, event["id"]),
            )
            await conn.execute(
                "INSERT INTO outbox_delivery_attempts(event_id,claim_token,attempt_number) VALUES (%s,%s,%s)",
                (event["id"], token, event["attempts"]),
            )
            logger.info("outbox_claim event_id=%s target=%s attempt=%s queue_age_seconds=%.3f",
                        event["id"], event["flow_target"], event["attempts"],
                        (datetime.now(timezone.utc) - event["created_at"]).total_seconds())
        return events


async def renew(conn_str, event, request_started=False):
    async with await _connect(conn_str) as conn:
        cur = await conn.execute(
            "UPDATE outbox_events SET lease_until=NOW()+make_interval(secs => %s) "
            "WHERE id=%s AND claimed_by=%s AND status='PROCESSING' AND lease_until > NOW() RETURNING id",
            (LEASE_SECONDS, event["id"], event["claimed_by"]),
        )
        if not await cur.fetchone():
            return False
        if request_started:
            await conn.execute(
                "UPDATE outbox_delivery_attempts SET request_started=TRUE WHERE event_id=%s AND claim_token=%s",
                (event["id"], event["claimed_by"]),
            )
        return True


def _reason(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z0-9_]{1,96}", value) else "EFFECT_FAILED"


async def finish(conn_str, event, result):
    if result.outcome not in {"SUCCEEDED", "POLICY_SKIPPED", "RETRYABLE", "TERMINAL", "UNCERTAIN"}:
        result = Outcome("TERMINAL", "INVALID_HANDLER_OUTCOME")
    if result.outcome == "SUCCEEDED" and not result.receipt:
        result = Outcome("UNCERTAIN", "MISSING_PROVIDER_RECEIPT")
    reason = _reason(result.reason) if result.reason else ""
    delay = min(300, 2 ** (event["attempts"] - 1))
    if result.outcome == "POLICY_SKIPPED" and not reason:
        result = Outcome("TERMINAL", "MISSING_SKIP_REASON")
        reason = result.reason
    status = "PROCESSED" if result.outcome in {"SUCCEEDED", "POLICY_SKIPPED"} else "FAILED"
    terminal = None if status == "PROCESSED" else "DELIVERY_UNCERTAIN" if result.outcome == "UNCERTAIN" else reason or "EFFECT_FAILED"
    if result.outcome == "RETRYABLE":
        if result.retry_after is not None and result.retry_after > 900:
            terminal = "OPERATOR_DEFERRED"
            delay = max(delay, result.retry_after)
        elif event["attempts"] < event["max_attempts"]:
            status, terminal = "PENDING", None
            delay = max(delay, max(0, result.retry_after or 0))
        else:
            terminal = "ATTEMPTS_EXHAUSTED"
    record = {"outcome": "SKIPPED" if result.outcome == "POLICY_SKIPPED" else result.outcome,
              "reason": reason, "receipt": result.receipt}
    async with await _connect(conn_str) as conn:
        cur = await conn.execute(
            "UPDATE outbox_events SET status=%s, terminal_reason=%s, result=COALESCE(result,'{}'::jsonb) || %s::jsonb || jsonb_build_object('receipt',COALESCE(result->'receipt','{}'::jsonb) || %s::jsonb), "
            "last_error=%s, retry_count=retry_count+%s, available_at=NOW()+make_interval(secs => %s), "
            "processed_at=CASE WHEN %s='PROCESSED' THEN NOW() ELSE NULL END, "
            "claimed_by=NULL,lease_until=NULL WHERE id=%s AND claimed_by=%s AND status='PROCESSING' "
            "AND lease_until > NOW() RETURNING aggregate_id, aggregate_kind, action_key,result",
            (status, terminal, json.dumps(record), json.dumps(result.receipt), reason or None, int(result.outcome == "RETRYABLE"),
             delay, status, event["id"], event["claimed_by"]),
        )
        changed = await cur.fetchone()
        if changed:
            record = changed["result"]
        # Preserve late receipts in the immutable claim's evidence, never a newer row.
        await conn.execute(
            "UPDATE outbox_delivery_attempts SET finished_at=NOW(),outcome=%s,reason=%s,receipt=COALESCE(receipt,'{}'::jsonb) || %s::jsonb "
            "WHERE event_id=%s AND claim_token=%s",
            (result.outcome if changed else "LATE_" + result.outcome, reason,
             json.dumps(result.receipt), event["id"], event["claimed_by"]),
        )
        if changed and changed["aggregate_kind"] == "submission":
            await conn.execute(
                "UPDATE survey_submissions SET action_status=jsonb_set(COALESCE(action_status,'{}'), "
                "'{outbox_v2}', COALESCE(action_status->'outbox_v2','{}') || jsonb_build_object(%s::text,%s::jsonb),true) "
                "WHERE submission_id=%s AND work_owner='outbox_v2'",
                (changed["action_key"], json.dumps({**record, "status": status}), changed["aggregate_id"]),
            )
        logger.info("outbox_finish event_id=%s target=%s attempt=%s outcome=%s reason=%s accepted=%s",
                    event["id"], event["flow_target"], event["attempts"], result.outcome, reason, bool(changed))
        return bool(changed)


async def _submission(conn_str, event):
    if event["aggregate_kind"] != "submission":
        return None
    async with await _connect(conn_str) as conn:
        cur = await conn.execute("SELECT * FROM survey_submissions WHERE submission_id=%s", (event["aggregate_id"],))
        return await cur.fetchone()


def _eligibility(event, submission):
    if event["aggregate_kind"] != "submission":
        return None
    if not submission or submission["work_owner"] != "outbox_v2":
        return Outcome("POLICY_SKIPPED", "NOT_DURABLE_OWNER")
    payload = event["payload"]
    independent = event["event_type"] == "survey.warning" and payload.get("message_class") == "DRIVER_WARNING" and payload.get("decision_independent") is True
    if not independent and payload.get("decision_version") != submission["decision_version"]:
        return Outcome("POLICY_SKIPPED", "STALE_DECISION")
    if event["flow_target"] in EXPORTS:
        if submission["status"] != "APPROVED":
            return Outcome("POLICY_SKIPPED", "NOT_APPROVED")
        if event["flow_target"] in {"R2_UPLOAD", "GDRIVE_EXPORT"} and payload.get("media_version") != submission["media_version"]:
            return Outcome("POLICY_SKIPPED", "STALE_MEDIA")
    return None


async def _billing(conn_str, event):
    payload = event["payload"]
    policy = payload.get("policy") or {}
    amount = payload.get("amount_vnd")
    if not isinstance(policy, dict) or policy.get("enabled") is not True:
        return Outcome("POLICY_SKIPPED", "POLICY_DISABLED")
    if type(amount) is not int or amount <= 0 or not policy.get("version"):
        return Outcome("TERMINAL", "INVALID_BILLING_POLICY")
    async with await _connect(conn_str) as conn:
        token = await conn.execute(
            "SELECT id FROM outbox_events WHERE id=%s AND claimed_by=%s AND status='PROCESSING' "
            "AND lease_until>NOW() FOR UPDATE", (event["id"], event["claimed_by"])
        )
        if not await token.fetchone():
            return Outcome("UNCERTAIN", "LEASE_LOST")
        cur = await conn.execute("SELECT * FROM survey_submissions WHERE submission_id=%s FOR UPDATE", (event["aggregate_id"],))
        submission = await cur.fetchone()
        skip = _eligibility(event, submission)
        if skip:
            return skip
        collaborator = submission.get("collaborator_id")
        if not collaborator or collaborator != payload.get("collaborator_id"):
            return Outcome("TERMINAL", "COLLABORATOR_NOT_MAPPED")
        cur = await conn.execute("SELECT telegram_user_id FROM authorized_users WHERE telegram_user_id=%s AND is_active AND role='COLLABORATOR'", (collaborator,))
        if not await cur.fetchone():
            return Outcome("TERMINAL", "COLLABORATOR_INACTIVE")
        await conn.execute(
            "INSERT INTO collaborator_billing_ledger(collaborator_id,submission_id,payout_amount_vnd,payout_status) "
            "VALUES (%s,%s,%s,'UNPAID') ON CONFLICT(submission_id) DO NOTHING",
            (collaborator, event["aggregate_id"], amount),
        )
        cur = await conn.execute(
            "SELECT transaction_id,collaborator_id,payout_amount_vnd FROM collaborator_billing_ledger WHERE submission_id=%s",
            (event["aggregate_id"],),
        )
        row = await cur.fetchone()
        if row["collaborator_id"] != collaborator or row["payout_amount_vnd"] != amount:
            return Outcome("TERMINAL", "BILLING_RECEIPT_CONFLICT")
        return Outcome("SUCCEEDED", receipt={"ledger_id": str(row["transaction_id"]), "amount_vnd": amount})


async def _local_failure(conn_str, event, reason):
    async with await _connect(conn_str) as conn:
        cur = await conn.execute("SELECT request_started FROM outbox_delivery_attempts WHERE event_id=%s AND claim_token=%s", (event["id"], event["claimed_by"]))
        attempt = await cur.fetchone()
    safe = event["flow_target"] in REPLAY_SAFE or (attempt is not None and not attempt["request_started"])
    return Outcome("RETRYABLE" if safe else "UNCERTAIN", reason)


async def _binding_valid(conn_str, event):
    binding = event["payload"].get("recipient_binding")
    if binding is None:
        return True
    if not isinstance(binding, dict) or binding.get("telegram_user_id") != event["payload"].get("chat_id"):
        return False
    try:
        async with await _connect(conn_str) as conn:
            cur = await conn.execute(
                "SELECT 1 FROM phone_telegram_links WHERE phone_normalized=%s AND telegram_user_id=%s "
                "AND source IN ('verified_contact','admin_verified') AND updated_at=%s::timestamptz AND updated_at<=%s::timestamptz",
                (binding["phone"], binding["telegram_user_id"], binding["verified_at"], binding["submitted_at"]),
            )
            return bool(await cur.fetchone())
    except KeyError:
        return False


async def execute_claim(conn_str, event):
    lost = asyncio.Event()

    async def heartbeat():
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                if not await renew(conn_str, event):
                    lost.set()
                    return
                await record_worker_poll(conn_str)
            except Exception:
                lost.set()
                return

    async def guard():
        if lost.is_set():
            return False
        try:
            current = await _submission(conn_str, event)
            if _eligibility(event, current):
                lost.set()
                return False
            if not await _binding_valid(conn_str, event):
                lost.set()
                return False
            if not await renew(conn_str, event, request_started=True):
                lost.set()
                return False
            return True
        except (OSError, psycopg.Error):
            raise
        except Exception:
            lost.set()
            return False

    async def record(receipt):
        if lost.is_set():
            return False
        try:
            async with await _connect(conn_str) as conn:
                cur = await conn.execute(
                    "UPDATE outbox_events SET result=jsonb_build_object('outcome','PROCESSING','receipt',COALESCE(result->'receipt','{}'::jsonb) || %s::jsonb) "
                    "WHERE id=%s AND claimed_by=%s AND status='PROCESSING' AND lease_until>NOW() RETURNING id",
                    (json.dumps(receipt), event["id"], event["claimed_by"]),
                )
                if not await cur.fetchone():
                    lost.set()
                    return False
                await conn.execute(
                    "UPDATE outbox_delivery_attempts SET receipt=COALESCE(receipt,'{}'::jsonb) || %s::jsonb WHERE event_id=%s AND claim_token=%s",
                    (json.dumps(receipt), event["id"], event["claimed_by"]),
                )
                return True
        except Exception:
            lost.set()
            return False

    guard.record = record

    renewal = asyncio.create_task(heartbeat())
    try:
        submission = await _submission(conn_str, event)
        result = _eligibility(event, submission)
        if not result and not await _binding_valid(conn_str, event):
            result = Outcome("POLICY_SKIPPED", "RECIPIENT_BINDING_CHANGED")
        if not result:
            target = event["flow_target"]
            if target == "BILLING":
                result = await _billing(conn_str, event)
            elif target in {"TELEGRAM_NOTIFY", "LARK_SYNC"}:
                from services.outbox_notifications import execute
                result = await execute(event, submission, conn_str, guard)
            elif target in {"R2_UPLOAD", "GDRIVE_EXPORT"}:
                from services.outbox_exports import execute
                result = await execute(event, submission, conn_str, guard)
            else:
                result = Outcome("TERMINAL", "UNSUPPORTED_TARGET")
    except asyncio.CancelledError:
        try:
            result = await asyncio.shield(_local_failure(conn_str, event, "WORKER_CANCELLED"))
            await asyncio.shield(finish(conn_str, event, result))
        except Exception:
            logger.error("Interrupted claim retained for lease reconciliation: %s", event["id"])
        raise
    except Exception:
        logger.error("Outbox execution failed for event %s", event["id"])
        result = await _local_failure(conn_str, event, "HANDLER_EXCEPTION")
    finally:
        renewal.cancel()
        with suppress(asyncio.CancelledError):
            await renewal
    return await finish(conn_str, event, result)


async def process(conn_str, batch_size=EXECUTION_SLOTS, flow_target=None):
    events = await claim(conn_str, min(batch_size, EXECUTION_SLOTS), flow_target)
    if not events:
        return 0
    return sum(await asyncio.gather(*(execute_claim(conn_str, event) for event in events)))
