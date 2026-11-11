# -*- coding: utf-8 -*-
"""
Action Checklist & Downstream Retry Router.
Handles operational queries and idempotent manual retries of asynchronous
downstream actions (R2, Telegram, Lark, Drive, Billing).
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from core.state import verify_admin_auth, get_db_pool
from core.repositories import fetch_aggregated_submissions
from services.action_checklist import fetch_action_status, dispatch_action_checklist

logger = logging.getLogger("routers.actions")
router = APIRouter(tags=["Action Checklist"])


@router.get("/api/v1/submissions/{submission_id}/actions")
async def get_submission_actions_endpoint(
    submission_id: str,
    admin_user: str = Depends(verify_admin_auth),
):
    """Retrieves current action checklist status for a submission."""
    db_pool = get_db_pool()
    status = await fetch_action_status(db_pool, submission_id)
    return {"submission_id": submission_id, "actions": status}


@router.post("/api/v1/submissions/{submission_id}/retry-actions")
async def retry_submission_actions_endpoint(
    submission_id: str,
    background_tasks: BackgroundTasks,
    admin_user: str = Depends(verify_admin_auth),
):
    """Triggers asynchronous idempotent retry of any uncompleted downstream actions."""
    subs = await fetch_aggregated_submissions(search=submission_id, limit=5)
    rec = next((s for s in subs if s.get("submission_id") == submission_id), None)
    if not rec:
        raise HTTPException(status_code=404, detail="Không tìm thấy hồ sơ khảo sát")

    is_appr = rec.get("status") == "APPROVED"
    background_tasks.add_task(
        dispatch_action_checklist,
        submission_id=submission_id,
        db_pool=get_db_pool(),
        is_approved=is_appr,
        raw_bytes=b"",
        driver_phone=rec.get("driver_phone"),
        driver_name=rec.get("driver_name"),
        license_plate=rec.get("license_plate"),
        collab_param=rec.get("ctv_code"),
        answers=rec.get("form_data") or {},
        ai_res=rec.get("ai_result") or {},
        tot_gross=int(rec.get("total_gross_revenue") or 0),
        tot_net=int(rec.get("total_net_income") or 0),
        tot_trips=int(rec.get("total_trips") or 0),
        reason_str=rec.get("rejection_reason"),
        is_test_mode=rec.get("mode") == "TEST_DEV_MODE",
        raw_bytes_list=[],
    )
    return {"status": "retry_dispatched", "submission_id": submission_id}
