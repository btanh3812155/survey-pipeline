# -*- coding: utf-8 -*-
"""
Action Checklist & Idempotent Downstream Task Executor.
Replaces monolithic polling loops with granular, per-action status tracking,
expiring leases (30s timeout), and independent fault isolation.
"""

import os
import json
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, Tuple

from config import settings

logger = logging.getLogger("action_checklist")

DEV_STORAGE_FILE = os.path.join(os.getcwd(), "data", "dev_test_submissions.json")


def _get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc_now() -> str:
    return _get_utc_now().isoformat()


def init_action_checklist(is_approved: bool, has_collaborator: bool) -> Dict[str, Any]:
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


async def fetch_action_status(db_pool: Optional[Any], submission_id: str, is_test_mode: bool = False) -> Dict[str, Any]:
    """Retrieves current action_status dictionary from PostgreSQL or dev storage."""
    if is_test_mode or not db_pool:
        if os.path.exists(DEV_STORAGE_FILE):
            try:
                with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                    recs = json.load(f)
                for r in recs:
                    if r.get("submission_id") == submission_id:
                        return r.get("action_status") or {}
            except Exception:
                pass
        return {}

    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT action_status FROM survey_submissions WHERE submission_id = %s;",
                    (submission_id,),
                )
                row = await cur.fetchone()
                if row and row[0]:
                    val = row[0]
                    return val if isinstance(val, dict) else json.loads(val)
    except Exception as e:
        logger.debug(f"Failed to fetch action_status for #{submission_id}: {e}")
    return {}


async def acquire_action_lease(
    db_pool: Optional[Any],
    submission_id: str,
    action: str,
    lease_seconds: int = 45,
    is_test_mode: bool = False,
) -> bool:
    """
    Attempts to acquire an exclusive expiring lease to execute an action.
    Returns True if lease acquired; False if already COMPLETED, SKIPPED,
    or currently RUNNING with a non-expired lease.
    """
    now = _get_utc_now()
    lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
    now_str = now.isoformat()

    current_status = await fetch_action_status(db_pool, submission_id, is_test_mode=is_test_mode)
    act_state = current_status.get(action) or {}

    # 1. Do not re-run if already completed or skipped
    if act_state.get("status") in ("COMPLETED", "SKIPPED"):
        return False

    # 2. Check if currently active lease has not expired yet
    if act_state.get("status") == "RUNNING":
        exp_str = act_state.get("lease_until")
        if exp_str:
            try:
                exp_dt = datetime.fromisoformat(exp_str)
                if exp_dt > now:
                    return False  # Active lease still held by another task
            except Exception:
                pass

    # 3. Acquire lease
    attempts = int(act_state.get("attempts") or 0) + 1
    new_entry = {
        "status": "RUNNING",
        "lease_until": lease_until,
        "started_at": now_str,
        "attempts": attempts,
    }

    if is_test_mode or not db_pool:
        try:
            recs = []
            if os.path.exists(DEV_STORAGE_FILE):
                with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                    recs = json.load(f)
            found = False
            for r in recs:
                if r.get("submission_id") == submission_id:
                    if "action_status" not in r:
                        r["action_status"] = {}
                    r["action_status"][action] = new_entry
                    found = True
                    break
            if not found:
                recs.append({"submission_id": submission_id, "action_status": {action: new_entry}})
            os.makedirs(os.path.dirname(DEV_STORAGE_FILE), exist_ok=True)
            with open(DEV_STORAGE_FILE, "w", encoding="utf-8") as f:
                json.dump(recs, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.debug(f"Dev storage lease error: {e}")
            return False

    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE survey_submissions
                    SET action_status = jsonb_set(
                        COALESCE(action_status, '{}'::jsonb),
                        ARRAY[%s],
                        %s::jsonb,
                        true
                    )
                    WHERE submission_id = %s;
                    """,
                    (action, json.dumps(new_entry), submission_id),
                )
                await conn.commit()
                return True
    except Exception as e:
        logger.error(f"Failed to acquire lease for #{submission_id} [{action}]: {e}")
        return False


async def update_action_record(
    db_pool: Optional[Any],
    submission_id: str,
    action: str,
    status: str,
    meta: Optional[Dict[str, Any]] = None,
    is_test_mode: bool = False,
) -> None:
    """Updates action outcome (COMPLETED, FAILED, SKIPPED) with metadata."""
    now_str = _format_utc_now()
    entry = {"status": status, "updated_at": now_str}
    if meta:
        entry.update(meta)

    if is_test_mode or not db_pool:
        try:
            recs = []
            if os.path.exists(DEV_STORAGE_FILE):
                with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                    recs = json.load(f)
            found = False
            for r in recs:
                if r.get("submission_id") == submission_id:
                    if "action_status" not in r:
                        r["action_status"] = {}
                    r["action_status"][action] = entry
                    found = True
                    break
            if not found:
                recs.append({"submission_id": submission_id, "action_status": {action: entry}})
            os.makedirs(os.path.dirname(DEV_STORAGE_FILE), exist_ok=True)
            with open(DEV_STORAGE_FILE, "w", encoding="utf-8") as f:
                json.dump(recs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"Dev storage update error: {e}")
        return

    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE survey_submissions
                    SET action_status = jsonb_set(
                        COALESCE(action_status, '{}'::jsonb),
                        ARRAY[%s],
                        %s::jsonb,
                        true
                    )
                    WHERE submission_id = %s;
                    """,
                    (action, json.dumps(entry), submission_id),
                )
                await conn.commit()
    except Exception as e:
        logger.error(f"Failed to record action outcome for #{submission_id} [{action}]: {e}")


async def dispatch_action_checklist(
    submission_id: str,
    db_pool: Optional[Any],
    is_approved: bool,
    raw_bytes: bytes,
    driver_phone: Optional[str],
    driver_name: Optional[str],
    license_plate: Optional[str],
    collab_param: Optional[str],
    answers: dict,
    ai_res: dict,
    tot_gross: int,
    tot_net: int,
    tot_trips: int,
    reason_str: Optional[str],
    blur_var: Optional[float] = None,
    is_test_mode: bool = False,
    raw_bytes_list: Optional[list] = None,
) -> Dict[str, Any]:
    """
    Executes all 5 downstream side-effects asynchronously with individual
    lease locks, fault isolation, and full status auditing.
    """
    logger.info(f"🚀 [CHECKLIST] Dispatching downstream actions for #{submission_id} (Approved: {is_approved})")

    # 1. Action: Cloudflare R2 Upload
    if await acquire_action_lease(db_pool, submission_id, "r2_upload", is_test_mode=is_test_mode):
        try:
            from services.storage_r2 import R2StorageClient
            r2 = R2StorageClient()
            if raw_bytes:
                r2_key = f"surveys/{submission_id}.webp"
                r2_url = await r2.upload_image(file_name=r2_key, webp_bytes=raw_bytes)
                await update_action_record(
                    db_pool, submission_id, "r2_upload", "COMPLETED",
                    meta={"r2_url": r2_url, "key": r2_key}, is_test_mode=is_test_mode
                )
            else:
                await update_action_record(
                    db_pool, submission_id, "r2_upload", "SKIPPED",
                    meta={"reason": "raw_bytes empty"}, is_test_mode=is_test_mode
                )
        except Exception as e:
            logger.error(f"[CHECKLIST] R2 upload failed for #{submission_id}: {e}")
            await update_action_record(
                db_pool, submission_id, "r2_upload", "FAILED",
                meta={"error": str(e)}, is_test_mode=is_test_mode
            )

    # 2. Action: Telegram Admin Group Notification
    # The submission ingress already posts the review card (with the "Mở Phiếu Xét
    # Duyệt" button) to the admin group, so this task must not post a second message.
    if await acquire_action_lease(db_pool, submission_id, "telegram_notify", is_test_mode=is_test_mode):
        await update_action_record(
            db_pool, submission_id, "telegram_notify", "SKIPPED",
            meta={"reason": "Handled by submission ingress review card"},
            is_test_mode=is_test_mode,
        )

    # 3. Action: Lark Base Sync
    if is_approved:
        if await acquire_action_lease(db_pool, submission_id, "lark_sync", is_test_mode=is_test_mode):
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
                await update_action_record(
                    db_pool, submission_id, "lark_sync", "COMPLETED" if lark_ok else "FAILED",
                    meta={"synced": lark_ok}, is_test_mode=is_test_mode
                )
            except Exception as e:
                logger.error(f"[CHECKLIST] Lark sync failed for #{submission_id}: {e}")
                await update_action_record(
                    db_pool, submission_id, "lark_sync", "FAILED",
                    meta={"error": str(e)}, is_test_mode=is_test_mode
                )

    # 4. Action: Google Drive Deliverables Sync
    if is_approved:
        if await acquire_action_lease(db_pool, submission_id, "gdrive_sync", is_test_mode=is_test_mode):
            try:
                from services.google_drive_delivery import upload_to_google_drive, has_google_credentials
                if has_google_credentials():
                    sample_imgs = []
                    if raw_bytes_list:
                        for s_idx, b_item in enumerate(raw_bytes_list):
                            sample_imgs.append({"filename": f"slot_{s_idx}.webp", "bytes": b_item})
                    elif raw_bytes:
                        sample_imgs.append({"filename": "slot_0.webp", "bytes": raw_bytes})
                    record = {
                        "submission_id": submission_id,
                        "status": "APPROVED",
                        "gross_revenue": tot_gross,
                        "net_income": tot_net,
                        "trip_count": tot_trips,
                        "driver_phone": driver_phone,
                        "driver_name": driver_name,
                        "license_plate": license_plate or ai_res.get("detected_plate"),
                        "form_data": answers,
                        "weekly_incomes": ai_res.get("weekly_incomes") or {},
                        "ai_result": ai_res,
                    }
                    gdrive_ok = await upload_to_google_drive(
                        submission_id=submission_id,
                        record=record,
                        images=sample_imgs,
                    )
                    await update_action_record(
                        db_pool, submission_id, "gdrive_sync", "COMPLETED" if gdrive_ok else "FAILED",
                        meta={"synced": gdrive_ok}, is_test_mode=is_test_mode
                    )
                else:
                    await update_action_record(
                        db_pool, submission_id, "gdrive_sync", "SKIPPED",
                        meta={"reason": "Google Drive credentials not configured"}, is_test_mode=is_test_mode
                    )
            except Exception as e:
                logger.error(f"[CHECKLIST] Google Drive sync failed for #{submission_id}: {e}")
                await update_action_record(
                    db_pool, submission_id, "gdrive_sync", "FAILED",
                    meta={"error": str(e)}, is_test_mode=is_test_mode
                )

    # 5. Action: Collaborator Billing Ledger
    if is_approved:
        if await acquire_action_lease(db_pool, submission_id, "billing_ledger", is_test_mode=is_test_mode):
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
                    await update_action_record(
                        db_pool, submission_id, "billing_ledger", "COMPLETED",
                        meta={"collab_id": collab_param, "amount_vnd": 0}, is_test_mode=is_test_mode
                    )
                else:
                    await update_action_record(
                        db_pool, submission_id, "billing_ledger", "SKIPPED",
                        meta={"reason": "No numeric collaborator ID or in test mode"}, is_test_mode=is_test_mode
                    )
            except Exception as e:
                logger.error(f"[CHECKLIST] Billing ledger write failed for #{submission_id}: {e}")
                await update_action_record(
                    db_pool, submission_id, "billing_ledger", "FAILED",
                    meta={"error": str(e)}, is_test_mode=is_test_mode
                )

    final_status = await fetch_action_status(db_pool, submission_id, is_test_mode=is_test_mode)
    logger.info(f"✅ [CHECKLIST] Completed downstream actions for #{submission_id}: {list(final_status.keys())}")
    return final_status
