# -*- coding: utf-8 -*-
"""
Submissions & Data Hub Router.
Decouples survey reporting, table filtering, Excel CSV export,
and temporary dev test persistence.
"""

import os
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks, status
from fastapi.responses import HTMLResponse, FileResponse
from config import settings
from core.state import verify_admin_auth, FORM_REGISTRY, DEV_STORAGE_FILE, STATIC_DIR, get_db_pool
from core.repositories import fetch_aggregated_submissions
from services.submissions_viewer_html import render_submissions_viewer_html
from services.google_drive_delivery import has_google_credentials, sync_all_approved_to_drive

logger = logging.getLogger("routers.admin_data")
router = APIRouter(tags=["Submissions Data Hub"])


@router.get("/admin/submissions", response_class=HTMLResponse)
@router.get("/submissions", response_class=HTMLResponse)
@router.get("/admin/data", response_class=HTMLResponse)
@router.get("/data", response_class=HTMLResponse)
@router.get("/admin/data/{form_id}", response_class=HTMLResponse)
async def admin_submissions_dashboard(
    request: Request,
    form_id: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    admin_user: str = Depends(verify_admin_auth),
):
    """Serves pure static Submissions Data Hub client."""
    html_file = os.path.join(STATIC_DIR, "ui", "submissions_viewer.html")
    if os.path.exists(html_file):
        return FileResponse(html_file, media_type="text/html")
    subs = await fetch_aggregated_submissions(form_id=form_id, status_filter=status, search=search, limit=2000)
    forms_dict = {fid: f.model_dump() for fid, f in FORM_REGISTRY.items()}
    return HTMLResponse(
        content=render_submissions_viewer_html(
            subs,
            forms=forms_dict,
            selected_form_id=form_id,
            environment=settings.ENVIRONMENT,
        ),
        status_code=200,
    )


@router.get("/api/v1/submissions")
async def get_all_submissions_api(
    status: Optional[str] = None,
    mode: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 200,
    admin_user: str = Depends(verify_admin_auth),
):
    """API endpoint providing aggregated JSON survey submissions with stats summary."""
    subs = await fetch_aggregated_submissions(status_filter=status, mode_filter=mode, search=search, limit=limit)
    return {
        "count": len(subs),
        "submissions": subs,
        "forms": {fid: {"title": schema.title} for fid, schema in FORM_REGISTRY.items()},
        "summary": {
            "total": len(subs),
            "approved": sum(1 for s in subs if s.get("status") == "APPROVED"),
            "rejected": sum(1 for s in subs if s.get("status") == "REJECTED"),
            "manual_review": sum(1 for s in subs if s.get("status") in ("MANUAL_REVIEW", "APPEAL_PENDING")),
        },
    }


@router.get("/api/v1/dev/submissions")
async def get_dev_submissions(admin_user: str = Depends(verify_admin_auth)):
    """Inspects all surveys currently saved in temporary dev test storage."""
    import json
    if not os.path.exists(DEV_STORAGE_FILE):
        return {"count": 0, "submissions": []}
    try:
        with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"count": len(data), "submissions": data}
    except Exception as e:
        return {"count": 0, "submissions": [], "error": str(e)}


@router.delete("/api/v1/dev/submissions")
async def clear_dev_submissions(admin_user: str = Depends(verify_admin_auth)):
    """Clears all records in temporary dev test storage."""
    if os.path.exists(DEV_STORAGE_FILE):
        os.remove(DEV_STORAGE_FILE)
    return {"status": "cleared", "message": "Bộ nhớ tạm thử nghiệm đã được dọn sạch!"}


@router.post("/api/v1/admin/sync-drive")
async def trigger_manual_drive_sync(
    background_tasks: BackgroundTasks,
    admin_user: str = Depends(verify_admin_auth),
):
    """
    Triggers an immediate Google Drive deliverables synchronization in the background.
    Syncs the Master_Survey_Deliverables.csv data sheet and the Images folder.
    """
    import sys
    main_mod = sys.modules.get("main")
    _has_creds = getattr(main_mod, "has_google_credentials", has_google_credentials) if main_mod else has_google_credentials
    _sync_all = getattr(main_mod, "sync_all_approved_to_drive", sync_all_approved_to_drive) if main_mod else sync_all_approved_to_drive
    if not _has_creds():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google Drive credentials (OAuth2 / Service Account) are not configured.",
        )
    background_tasks.add_task(_sync_all, db_pool=get_db_pool())
    return {
        "status": "QUEUED",
        "message": "Đã kích hoạt đồng bộ Google Drive thành công trong nền.",
        "target_account": settings.GOOGLE_ADMIN_EMAIL,
    }
