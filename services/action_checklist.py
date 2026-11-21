"""Submission action projection, operator transactions, and the flag-off legacy caller."""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from psycopg.errors import LockNotAvailable, UndefinedColumn, UndefinedTable
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from core.outbox_delivery import _eligibility
from core.state import DEV_STORAGE_FILE

logger = logging.getLogger(__name__)

ACTION_NAMES = {
    "R2_UPLOAD": "r2_upload", "TELEGRAM_NOTIFY": "telegram_notify", "LARK_SYNC": "lark_sync",
    "GDRIVE_EXPORT": "gdrive_sync", "BILLING": "billing_ledger",
}
LEGACY_WHERE = "aggregate_kind='submission' AND aggregate_id=%s AND (flow_target='ALL' OR action_key IS NULL)"
DURABLE_WHERE = "aggregate_kind='submission' AND aggregate_id=%s AND flow_target<>'ALL' AND action_key IS NOT NULL"


def _get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc_now() -> str:
    return _get_utc_now().isoformat()


def init_action_checklist(is_approved: bool, has_collaborator: bool) -> dict:
    """Generates the initial clean checklist state for a submission."""
    now_str = _format_utc_now()
    checklist = {
        "r2_upload": {"status": "PENDING", "created_at": now_str, "attempts": 0},
        "telegram_notify": {"status": "PENDING", "created_at": now_str, "attempts": 0},
    }

    if is_approved:
        checklist["lark_sync"] = {"status": "PENDING", "created_at": now_str, "attempts": 0}
        checklist["gdrive_sync"] = {"status": "PENDING", "created_at": now_str, "attempts": 0}
        if has_collaborator:
            checklist["billing_ledger"] = {"status": "PENDING", "created_at": now_str, "attempts": 0}
        else:
            checklist["billing_ledger"] = {"status": "SKIPPED", "reason": "No collaborator ID", "updated_at": now_str}
    else:
        checklist["lark_sync"] = {"status": "SKIPPED", "reason": "Submission rejected", "updated_at": now_str}
        checklist["gdrive_sync"] = {"status": "SKIPPED", "reason": "Only approved synced to Drive", "updated_at": now_str}
        checklist["billing_ledger"] = {"status": "SKIPPED", "reason": "Rejected submission has no payout", "updated_at": now_str}

    return checklist


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def read_dev_submission(submission_id):
    for path in (Path(DEV_STORAGE_FILE), Path(DEV_STORAGE_FILE).with_name("dev_test_submissions.json")):
        try:
            with path.open(encoding="utf-8") as file:
                records = json.load(file)
            if isinstance(records, list):
                for record in records:
                    if isinstance(record, dict) and record.get("submission_id") == submission_id:
                        return record
        except (OSError, ValueError):
            continue
    return None


async def _query(conn, sql, params=(), *, one=False):
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await (cur.fetchone() if one else cur.fetchall())


async def _submission(conn, submission_id, *, lock=False):
    return await _query(
        conn,
        "SELECT submission_id,status,to_jsonb(s)->'action_status' AS action_status, (to_jsonb(s)->>'media_version')::bigint AS media_version, "
        "to_jsonb(s)->>'work_owner' AS work_owner, (to_jsonb(s)->>'decision_version')::bigint AS decision_version "
        "FROM survey_submissions s WHERE submission_id=%s" + (" FOR NO KEY UPDATE NOWAIT" if lock else ""),
        (submission_id,), one=True,
    )


class ActionConflict(Exception):
    pass


class ActionNotFound(Exception):
    pass


class NoEligibleLegacyActions(Exception):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _uncertain(event):
    return (event["terminal_reason"] == "DELIVERY_UNCERTAIN"
            or _object(event["result"]).get("outcome") == "UNCERTAIN")


def _project(event):
    result = _object(event["result"])
    state = event["status"]
    reason = event["terminal_reason"] or result.get("reason")
    if state == "PROCESSING":
        state = "RUNNING"
    elif state == "PROCESSED":
        if result.get("outcome") == "SUCCEEDED" and result.get("receipt"):
            state = "COMPLETED"
        elif result.get("outcome") in {"SKIPPED", "POLICY_SKIPPED"} and reason:
            state = "SKIPPED"
        else:
            state, reason = "UNCERTAIN", "UNVERIFIED_PROCESSED"
    elif state == "FAILED" and _uncertain(event):
        state = "UNCERTAIN"
    return {"status": state, "event_id": str(event["id"]), "action_key": event["action_key"],
            "target": event["flow_target"], "event_type": event["event_type"],
            "message_class": _object(event["payload"]).get("message_class"),
            "resend_of": _object(event["payload"]).get("operator_resend_of"),
            "attempts": event["attempts"], "max_attempts": event["max_attempts"], "reason": reason,
            "created_at": event["created_at"], "available_at": event["available_at"]}


async def _events(conn, where, params, *, lock=False):
    return await _query(conn, f"SELECT * FROM outbox_events WHERE {where} ORDER BY created_at,id"
                        + (" FOR UPDATE NOWAIT" if lock else ""), params)


def _legacy_version(row, parents):
    return _digest([row, parents])


def _legacy_projection(row, parents):
    checklist = _object(row["action_status"])
    log_only = any(_object(a).get("status") in {"RUNNING", "FAILED", "COMPLETED"} for a in checklist.values())
    projected = []
    for parent in parents:
        recorded = _object(_object(parent["result"]).get("legacy_reconciliation"))
        ambiguous = (log_only or parent["flow_target"] != "ALL" or parent["status"] != "PENDING"
                     or parent["retry_count"] or parent["attempts"] or parent["locked_at"] or parent["processed_at"])
        projected.append({"event_id": str(parent["id"]), "target": parent["flow_target"],
                          "source_status": recorded.get("source_status", parent["status"]),
                          "classification": recorded.get("classification") or
                          ("AMBIGUOUS" if ambiguous else "EVIDENCE_REQUIRED"),
                          **{k: recorded[k] for k in ("disposition", "child_ids", "evidence", "blocked") if k in recorded}})
    return {"version": _legacy_version(row, parents), "parents": projected}


async def fetch_action_status(db_pool, submission_id, is_test_mode=False) -> dict:
    """Read canonical event rows; stale worker/checklist JSON never masks durable state."""
    if db_pool is not None and not is_test_mode:
        async with db_pool.connection() as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            row = await _submission(conn, submission_id)
            if row:
                actions = {
                    key: {k: v for k, v in item.items() if k != "lease_token"} if isinstance(item, dict) else item
                    for key, item in _object(row["action_status"]).items()
                }
                if not row["work_owner"]:
                    return actions
                parents = await _events(conn, LEGACY_WHERE, (submission_id,))
                if row["work_owner"] == "outbox_v2":
                    actions = {"outbox_v2": {}}
                    summaries = {}
                    events = await _events(conn, DURABLE_WHERE, (submission_id,))
                    superseded = {_object(event["payload"]).get("operator_resend_of"): str(event["id"]) for event in events
                                  if _object(event["payload"]).get("operator_resend_of")}
                    for event in events:
                        entry = _project(event)
                        if entry["event_id"] in superseded:
                            entry["resend_event_id"] = superseded[entry["event_id"]]
                        key = event["action_key"]
                        actions["outbox_v2"][entry["event_id"] if key in actions["outbox_v2"] else key] = entry
                        target = event["flow_target"]
                        payload = _object(event["payload"])
                        if (entry["event_id"] not in superseded and target in ACTION_NAMES
                                and payload.get("decision_version") == row["decision_version"]
                                and (target not in {"R2_UPLOAD", "GDRIVE_EXPORT"} or payload.get("media_version") == row["media_version"])):
                            summaries.setdefault(target, []).append(entry)
                    # Preserve the old single-target keys without claiming partial fan-out was delivered.
                    priority = {"UNCERTAIN": 0, "FAILED": 1, "RUNNING": 2, "PENDING": 3, "COMPLETED": 4, "SKIPPED": 5}
                    for target, entries in summaries.items():
                        actions[ACTION_NAMES[target]] = min(entries, key=lambda entry: priority[entry["status"]])
                if parents:
                    if row["work_owner"] == "legacy":
                        for key, entry in actions.items():
                            if key in ACTION_NAMES.values() and _object(entry).get("status") == "COMPLETED":
                                actions[key] = {**entry, "status": "UNCERTAIN", "reason": "LEGACY_LOG_ONLY"}
                    actions["legacy_reconciliation"] = _legacy_projection(row, parents)
                return actions
    return _object((read_dev_submission(submission_id) or {}).get("action_status"))


async def _operator_submission(conn, submission_id, *, lock=True):
    row = await _submission(conn, submission_id, lock=lock)
    if row is None:
        raise ActionNotFound(submission_id)
    if row["work_owner"] is None:
        raise ActionConflict("SCHEMA_002_REQUIRED")
    if lock:
        from core.durable_work import require_delivery_schema

        try:
            await require_delivery_schema(conn)
        except (UndefinedColumn, UndefinedTable) as exc:
            raise ActionConflict("SCHEMA_002_REQUIRED") from exc
        except ValueError as exc:
            if str(exc) != "DELIVERY_SCHEMA_REQUIRED":
                raise
            raise ActionConflict("SCHEMA_002_REQUIRED") from exc
    return row


async def _ledger_exists(conn, submission_id):
    return bool(await _query(conn, "SELECT 1 FROM collaborator_billing_ledger WHERE submission_id=%s",
                             (submission_id,), one=True))


async def _attempts(conn, event_id):
    return await _query(conn, "SELECT * FROM outbox_delivery_attempts WHERE event_id=%s ORDER BY started_at,claim_token",
                        (event_id,))


def _can_run(event, row):
    return (row["work_owner"] == "outbox_v2" and event["flow_target"] in ACTION_NAMES
            and _object(_object(event["payload"]).get("policy")).get("enabled") is True
            and not _eligibility(event, row))


async def _requeue(conn, event):
    # Incrementing a claim remains worker-only. Renew the budget only when the old one is spent.
    await conn.execute(
        "UPDATE outbox_events SET status='PENDING',terminal_reason=NULL,processed_at=NULL,"
        "available_at=GREATEST(available_at,NOW()), attempts=CASE WHEN attempts>=max_attempts THEN 0 ELSE attempts END "
        "WHERE id=%s", (event["id"],),
    )


async def retry_actions(conn, submission_id, actor):
    from core.durable_work import audit

    row = await _operator_submission(conn, submission_id)
    if row["work_owner"] != "outbox_v2":
        raise ActionConflict("LEGACY_RECONCILIATION_REQUIRED")
    changed = []
    for event in await _events(conn, DURABLE_WHERE + " AND status='FAILED'", (submission_id,), lock=True):
        if (not _can_run(event, row) or _uncertain(event)
                or _object(event["result"]).get("outcome") != "RETRYABLE"):
            continue
        if event["flow_target"] == "BILLING" and await _ledger_exists(conn, submission_id):
            continue
        if any(a["outcome"] in {"SUCCEEDED", "LATE_SUCCEEDED"} for a in await _attempts(conn, event["id"])):
            continue
        await _requeue(conn, event)
        changed.append(str(event["id"]))
    if changed:
        await audit(conn, "OUTBOX_RETRY", submission_id, actor, {"event_ids": changed})
    return changed


def _evidence_version(event, attempts):
    return _digest([event, attempts])


async def action_evidence(conn, submission_id, event_id, *, lock=False):
    row = await _operator_submission(conn, submission_id, lock=lock)
    events = await _events(conn, DURABLE_WHERE + " AND id=%s", (submission_id, event_id), lock=lock)
    if not events or row["work_owner"] != "outbox_v2":
        raise ActionNotFound(event_id)
    event = events[0]
    return row, event, await _attempts(conn, event_id)


def _resend_key(event):
    # The original server-generated action ID is the nonce: one authorized resend per uncertain row.
    return _digest(["operator-resend-v1", event["id"], event["action_key"]])


async def _resend_child(conn, event):
    return await _query(conn, f"SELECT id FROM outbox_events WHERE {DURABLE_WHERE} AND action_key=%s",
                        (event["aggregate_id"], _resend_key(event)), one=True)


async def inspect_action(conn, submission_id, event_id):
    _, event, attempts = await action_evidence(conn, submission_id, event_id)
    child = await _resend_child(conn, event)
    return {"action": _project(event), "version": _evidence_version(event, attempts),
            "resend_event_id": str(child["id"]) if child else None, "result": event["result"],
            "attempts": [{k: v for k, v in a.items() if k != "claim_token"} for a in attempts]}


async def resolve_action(conn, submission_id, event_id, actor, *, command, version, evidence_sha256,
                         accept_duplicate_risk=False):
    from core.durable_work import audit, enqueue_action

    row, event, attempts = await action_evidence(conn, submission_id, event_id, lock=True)
    if version != _evidence_version(event, attempts):
        raise ActionConflict("ACTION_EVIDENCE_CHANGED")
    if _project(event)["status"] != "UNCERTAIN":
        raise ActionConflict("ACTION_NOT_UNCERTAIN")
    if event["flow_target"] == "BILLING":
        raise ActionConflict("BILLING_REQUIRES_LEDGER_REVIEW")
    if await _resend_child(conn, event):
        raise ActionConflict("RESEND_ALREADY_QUEUED")
    if command in {"resend", "confirm_not_delivered"} and any(
        a["outcome"] in {"SUCCEEDED", "LATE_SUCCEEDED"} for a in attempts
    ):
        raise ActionConflict("DELIVERY_RECEIPT_EXISTS")
    resolution = {"command": command, "evidence_sha256": evidence_sha256,
                  "accept_duplicate_risk": accept_duplicate_risk, "previous_version": version}
    current = _object(event["result"])
    child_id = None
    if command == "resend":
        if accept_duplicate_risk is not True or not _can_run(event, row):
            raise ActionConflict("RESEND_NOT_AUTHORIZED")
        child_id = await enqueue_action(
            conn, submission_id, event["flow_target"], event["event_type"],
            {**event["payload"], "operator_resend_of": str(event_id)}, action_key=_resend_key(event),
        )
        resolution.update(resend_event_id=child_id, prior_receipt_sha256=_digest(current.get("receipt")))
    else:
        delivered = command == "confirm_delivered"
        reason = "OPERATOR_CONFIRMED_DELIVERED" if delivered else "OPERATOR_CONFIRMED_NOT_DELIVERED"
        result = {"outcome": "SUCCEEDED" if delivered else "RETRYABLE", "reason": reason,
                  "receipt": {"evidence_sha256": evidence_sha256} if delivered else {},
                  "prior_result": current}
        await conn.execute(
            "UPDATE outbox_events SET status=%s,terminal_reason=%s,result=%s, "
            "processed_at=CASE WHEN %s THEN NOW() ELSE NULL END WHERE id=%s",
            ("PROCESSED" if delivered else "FAILED", None if delivered else reason, Jsonb(result), delivered, event_id),
        )
    await audit(conn, "OUTBOX_OPERATOR_RESOLUTION", submission_id, actor,
                {"event_id": str(event_id), "previous_reason": event["terminal_reason"], **resolution})
    return child_id


def _active_legacy_entry(state):
    if state.get("status") != "RUNNING":
        return False
    try:
        return datetime.fromisoformat(state.get("lease_until", "")) > _get_utc_now()
    except (ValueError, TypeError):
        return False  # Expiry is not proof of absence; transfer still requires evidence and drain attestation.


async def reconcile_legacy(conn, submission_id, actor, *, version, legacy_drained, evidence):
    from core.durable_work import audit, enqueue_submission_work

    row = await _operator_submission(conn, submission_id)
    parents = await _events(conn, LEGACY_WHERE, (submission_id,), lock=True)
    if not parents:
        raise ActionNotFound(submission_id)
    projected = _legacy_projection(row, parents)
    if version != projected["version"]:
        raise ActionConflict("LEGACY_EVIDENCE_CHANGED")
    if row["work_owner"] != "legacy":
        raise ActionConflict("ALREADY_DURABLE_OWNED")

    missing = sorted(effect for effect, item in evidence.items() if item["finding"] == "missing")
    child_ids = []
    blocked = []
    if missing:
        if not legacy_drained or any(parent["status"] == "PROCESSING" for parent in parents):
            raise ActionConflict("LEGACY_DRAIN_REQUIRED")
        if await _events(conn, DURABLE_WHERE, (submission_id,), lock=True):
            raise ActionConflict("DURABLE_ACTION_ALREADY_EXISTS")
        checklist = _object(row["action_status"])
        if any(_active_legacy_entry(_object(state)) for state in checklist.values()):
            raise ActionConflict("LEGACY_ACTION_STILL_RUNNING")
        if "BILLING" in missing and await _ledger_exists(conn, submission_id):
            raise ActionConflict("BILLING_LEDGER_EXISTS")
        if any(_object(checklist.get(ACTION_NAMES.get(effect))).get("status") == "COMPLETED" for effect in missing):
            raise ActionConflict("LEGACY_COMPLETION_CONFLICT")
        try:
            # The helper also updates ownership. Roll that back if it found no eligible children.
            async with conn.transaction():
                child_ids = [str(child) for child in await enqueue_submission_work(
                    conn, submission_id, transition="LEGACY_RECONCILED", transfer=True,
                    context={"verified_missing_actions": missing},
                )]
                if not child_ids:
                    raise NoEligibleLegacyActions
                children = await _events(conn, DURABLE_WHERE, (submission_id,))
                queued = {(_object(child["payload"]).get("message_class") if child["flow_target"] == "TELEGRAM_NOTIFY"
                           else child["flow_target"]) for child in children}
                if ({str(child["id"]) for child in children} != set(child_ids) or not queued <= set(missing)
                        or any(not _can_run(child, {**row, "work_owner": "outbox_v2"}) for child in children)):
                    raise ActionConflict("LEGACY_CHILD_PLAN_CONFLICT")
                blocked = sorted(set(missing) - queued)
        except NoEligibleLegacyActions:
            blocked = missing

    for parent, classification in zip(parents, projected["parents"]):
        result = _object(parent["result"])
        marker = {"source_status": classification["source_status"], "classification": classification["classification"],
                  "source_terminal_reason": _object(result.get("legacy_reconciliation")).get(
                      "source_terminal_reason", parent["terminal_reason"]),
                  "disposition": "RECONCILED" if child_ids else "QUARANTINED", "child_ids": child_ids,
                  "blocked": blocked,
                  "evidence": {**classification.get("evidence", {}), **evidence}}
        await conn.execute(
            "UPDATE outbox_events SET status=%s,terminal_reason=%s,result=%s WHERE id=%s",
            ("PROCESSED" if child_ids else "FAILED", "LEGACY_RECONCILED" if child_ids else "LEGACY_QUARANTINED",
             Jsonb({**result, "legacy_reconciliation": marker}), parent["id"]),
        )
    await audit(conn, "OUTBOX_LEGACY_RECONCILIATION", submission_id, actor,
                {"parents": projected["parents"], "child_ids": child_ids, "legacy_drained": legacy_drained,
                 "evidence": evidence, "blocked": blocked})
    return child_ids


async def _claim_legacy_action(db_pool, submission_id, action):
    """The sole flag-off ingress fallback: claim a never-started DB action once."""
    try:
        async with db_pool.connection() as conn:
            row = await _submission(conn, submission_id, lock=True)
            if not row or row["work_owner"] not in (None, "legacy"):
                return None
            state = _object(_object(row["action_status"]).get(action))
            if state.get("status") not in (None, "PENDING") or state.get("attempts") or state.get("lease_token"):
                return None
            if action in {"lark_sync", "gdrive_sync", "billing_ledger"} and row["status"] != "APPROVED":
                return None
            if row["work_owner"] == "legacy" and await _query(
                conn, f"SELECT 1 FROM outbox_events WHERE {LEGACY_WHERE} "
                "AND result ? 'legacy_reconciliation' LIMIT 1", (submission_id,), one=True,
            ):
                return None
            token = uuid4().hex
            now = _get_utc_now()
            entry = {**state, "status": "RUNNING", "started_at": now.isoformat(), "attempts": 1,
                     "lease_until": (now + timedelta(seconds=45)).isoformat(), "lease_token": token}
            await conn.execute(
                "UPDATE survey_submissions SET action_status=jsonb_set(COALESCE(action_status,'{}'),ARRAY[%s::text],%s) "
                "WHERE submission_id=%s", (action, Jsonb(entry), submission_id),
            )
            await conn.commit()
            return token
    except LockNotAvailable:
        return None
    except Exception:
        logger.exception("Legacy action claim failed for %s [%s]", submission_id, action)
        return None


async def _finish_legacy_action(db_pool, submission_id, action, token, status, meta=None):
    try:
        async with db_pool.connection() as conn:
            await conn.execute(
                "UPDATE survey_submissions s SET action_status=jsonb_set(action_status,ARRAY[%s::text], "
                "((action_status->%s::text)-'lease_token'-'lease_until') || %s, false) WHERE submission_id=%s "
                "AND COALESCE(to_jsonb(s)->>'work_owner','legacy')='legacy' "
                "AND action_status->%s::text->>'status'='RUNNING' AND action_status->%s::text->>'lease_token'=%s",
                (action, action, Jsonb({"status": status, "updated_at": _format_utc_now(), **(meta or {})}),
                 submission_id, action, action, token),
            )
    except Exception:
        logger.exception("Legacy action finish failed for %s [%s]", submission_id, action)


async def dispatch_action_checklist(
    submission_id: str,
    db_pool,
    is_approved: bool,
    raw_bytes: bytes,
    driver_phone: str | None,
    driver_name: str | None,
    license_plate: str | None,
    collab_param: str | None,
    answers: dict,
    ai_res: dict,
    tot_gross: int,
    tot_net: int,
    tot_trips: int,
    reason_str: str | None,
    blur_var: float | None = None,
    is_test_mode: bool = False,
    raw_bytes_list: list | None = None,
    form_id: str | None = None,
) -> dict:
    """Flag-off WebRTC ingress only; never dispatches test or durable submissions."""
    if is_test_mode or db_pool is None:
        return await fetch_action_status(db_pool, submission_id, is_test_mode=is_test_mode)

    async def claim(action):
        return await _claim_legacy_action(db_pool, submission_id, action)

    async def finish(action, token, status, meta=None):
        await _finish_legacy_action(db_pool, submission_id, action, token, status, meta)

    # 1. Action: Cloudflare R2 Upload
    if token := await claim("r2_upload"):
        try:
            from services.storage_r2 import R2StorageClient
            r2 = R2StorageClient()
            if raw_bytes:
                r2_key = f"surveys/{submission_id}.webp"
                r2_url = await r2.upload_image(file_name=r2_key, webp_bytes=raw_bytes)
                await finish("r2_upload", token, "COMPLETED", {"r2_url": r2_url, "key": r2_key})
            else:
                await finish("r2_upload", token, "SKIPPED", {"reason": "raw_bytes empty"})
        except Exception as e:
            logger.error(f"[CHECKLIST] R2 upload failed for #{submission_id}: {e}")
            await finish("r2_upload", token, "FAILED", {"error": str(e)})

    # 2. Action: Telegram Admin Group Notification
    # The submission ingress already posts the review card (with the "Mở Phiếu Xét
    # Duyệt" button) to the admin group, so this task must not post a second message.
    if token := await claim("telegram_notify"):
        await finish("telegram_notify", token, "SKIPPED", {"reason": "Handled by submission ingress review card"})

    # 3. Action: Lark Base Sync
    if is_approved:
        if token := await claim("lark_sync"):
            try:
                from services.lark_base_sync import sync_submission_to_lark_base
                vehicle_model_val = str(answers.get("car_model") or answers.get("vehicle_model") or answers.get("vehicle_type") or "Xe điện GSM")
                lark_ok = await sync_submission_to_lark_base(
                    submission_id=submission_id,
                    status_str="APPROVED",
                    driver_phone=driver_phone,
                    driver_name=driver_name,
                    license_plate=license_plate or ai_res.get("detected_plate"),
                    gross_revenue=ai_res.get("gross_revenue") or tot_gross,
                    net_income=ai_res.get("net_income") or tot_net,
                    total_trips=ai_res.get("total_trips") or tot_trips,
                    online_hours=ai_res.get("online_hours"),
                    vehicle_model=vehicle_model_val,
                    reason_str=reason_str,
                )
                await finish("lark_sync", token, "COMPLETED" if lark_ok else "FAILED", {"synced": lark_ok})
            except Exception as e:
                logger.error(f"[CHECKLIST] Lark sync failed for #{submission_id}: {e}")
                await finish("lark_sync", token, "FAILED", {"error": str(e)})

    # 4. Action: Google Drive Deliverables Sync
    if is_approved:
        if token := await claim("gdrive_sync"):
            try:
                from services.google_drive_delivery import upload_to_google_drive, has_google_credentials
                from services.image_object_projection import project_income_weeks
                if has_google_credentials():
                    weekly_incomes = project_income_weeks(ai_res.get("image_object_results") or [])
                    if not weekly_incomes:
                        weekly_incomes = ai_res.get("weekly_incomes") or {}
                    sample_imgs = []
                    if raw_bytes_list:
                        for s_idx, b_item in enumerate(raw_bytes_list):
                            sample_imgs.append({"filename": f"slot_{s_idx}.webp", "bytes": b_item})
                    elif raw_bytes:
                        sample_imgs.append({"filename": "slot_0.webp", "bytes": raw_bytes})
                    record = {
                        "submission_id": submission_id,
                        "form_id": form_id,
                        "status": "APPROVED",
                        "gross_revenue": tot_gross,
                        "net_income": tot_net,
                        "trip_count": tot_trips,
                        "driver_phone": driver_phone,
                        "driver_name": driver_name,
                        "license_plate": license_plate or ai_res.get("detected_plate"),
                        "form_data": answers,
                        "weekly_incomes": weekly_incomes,
                        "ai_result": ai_res,
                    }
                    gdrive_ok = await upload_to_google_drive(
                        submission_id=submission_id,
                        record=record,
                        images=sample_imgs,
                    )
                    await finish("gdrive_sync", token, "COMPLETED" if gdrive_ok else "FAILED", {"synced": gdrive_ok})
                else:
                    await finish("gdrive_sync", token, "SKIPPED", {"reason": "Google Drive credentials not configured"})
            except Exception as e:
                logger.error(f"[CHECKLIST] Google Drive sync failed for #{submission_id}: {e}")
                await finish("gdrive_sync", token, "FAILED", {"error": str(e)})

    # 5. Action: Collaborator Billing Ledger
    if is_approved:
        if token := await claim("billing_ledger"):
            try:
                if collab_param and db_pool and not is_test_mode:
                    async with db_pool.connection() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                """
                                INSERT INTO collaborator_billing_ledger (
                                    collaborator_id, submission_id, payout_amount_vnd, payout_status
                                ) VALUES (%s, %s, %s, 'UNPAID')
                                ON CONFLICT (submission_id) DO NOTHING;
                                """,
                                (int(collab_param) if str(collab_param).isdigit() else None, submission_id, 0),
                            )
                            await conn.commit()
                    await finish("billing_ledger", token, "COMPLETED", {"collab_id": collab_param, "amount_vnd": 0})
                else:
                    await finish("billing_ledger", token, "SKIPPED", {"reason": "No numeric collaborator ID or in test mode"})
            except Exception as e:
                logger.error(f"[CHECKLIST] Billing ledger write failed for #{submission_id}: {e}")
                await finish("billing_ledger", token, "FAILED", {"error": str(e)})

    final_status = await fetch_action_status(db_pool, submission_id, is_test_mode=is_test_mode)
    logger.info(f"✅ [CHECKLIST] Completed downstream actions for #{submission_id}: {list(final_status.keys())}")
    return final_status
