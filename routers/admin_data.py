# -*- coding: utf-8 -*-
"""
Submissions & Data Hub Router.
Decouples survey reporting, table filtering, Excel CSV export,
and temporary dev test persistence.
"""

import os
import re
import logging
import shutil
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks, status
from fastapi.responses import HTMLResponse, FileResponse
from config import settings
from core.state import verify_admin_auth, FORM_REGISTRY, DEV_STORAGE_FILE, STATIC_DIR, get_db_pool
from core.repositories import fetch_aggregated_submissions, delete_submission_record
from services.submissions_viewer_html import render_submissions_viewer_html
from services.google_drive_delivery import has_google_credentials, sync_all_approved_to_drive
from services.submission_media import load_manifest, manifest_paths, safe_media_path

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
        "r2_enabled": bool(
            settings.R2_ACCOUNT_ID
            and settings.R2_ACCESS_KEY_ID
            and settings.R2_SECRET_ACCESS_KEY
        ),
        "forms": {fid: {"title": schema.title} for fid, schema in FORM_REGISTRY.items()},
        "summary": {
            "total": len(subs),
            "approved": sum(1 for s in subs if s.get("status") == "APPROVED"),
            "rejected": sum(1 for s in subs if s.get("status") == "REJECTED"),
            "manual_review": sum(1 for s in subs if s.get("status") in ("MANUAL_REVIEW", "APPEAL_PENDING")),
        },
    }


@router.delete("/api/v1/submissions/{sub_id}")
async def delete_submission(
    sub_id: str,
    request: Request,
    admin_user: str = Depends(verify_admin_auth),
):
    """Delete one submission and its outbox artifacts; financial rows block it."""
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", sub_id or "")
    if not safe_id or safe_id != sub_id:
        raise HTTPException(status_code=400, detail="Mã phiếu không hợp lệ")
    try:
        details = await delete_submission_record(
            safe_id,
            admin_user,
            ip_address=request.client.host if request.client else None,
        )
    except ValueError as exc:
        code = str(exc)
        if code == "NOT_FOUND":
            raise HTTPException(status_code=404, detail="Không tìm thấy phiếu.") from None
        if code == "BILLING_RECORD":
            raise HTTPException(
                status_code=409,
                detail="Phiếu đã có sổ cái thù lao; không thể xóa.",
            ) from None
        raise HTTPException(status_code=503, detail="Cơ sở dữ liệu không khả dụng.") from None

    media_root = os.path.realpath(os.path.join(os.getcwd(), "data", "submission_photos"))
    target = os.path.realpath(os.path.join(media_root, safe_id))
    if target.startswith(media_root + os.sep):
        shutil.rmtree(target, ignore_errors=True)
    upload = os.path.join(os.getcwd(), "data", "uploads", "surveys", f"{safe_id}.webp")
    try:
        if os.path.isfile(upload):
            os.remove(upload)
    except OSError as exc:
        logger.warning("Could not remove fallback media for %s: %s", safe_id, exc)
    try:
        from services.drive_cleanup import cleanup_submission_drive
        details["drive_cleanup"] = await cleanup_submission_drive(
            str(details.get("form_id") or ""), safe_id, get_db_pool()
        )
    except Exception as exc:
        logger.warning("Drive cleanup skipped for submission %s: %s", safe_id, type(exc).__name__)
        details["drive_cleanup"] = {"status": "error", "error": type(exc).__name__}
    return {"status": "deleted", "submission_id": safe_id, "details": details}


@router.get("/api/v1/submissions/{sub_id}/photos")
async def list_submission_photos(sub_id: str, admin_user: str = Depends(verify_admin_auth)):
    """Lists stored photos for one submission (all camera questions)."""
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", sub_id)
    manifest = await load_manifest(get_db_pool(), safe_id)
    paths = manifest_paths(safe_id, manifest)
    return {"photos": [f"/admin/submissions/{safe_id}/photos/{os.path.basename(path)}" for path in paths]}


@router.get("/admin/submissions/{sub_id}/photos/{filename}")
async def serve_submission_photo(sub_id: str, filename: str, admin_user: str = Depends(verify_admin_auth)):
    """Serves a stored submission photo to the admin viewer."""
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", sub_id)
    try:
        target = safe_media_path(safe_id, filename)
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="Ảnh không tồn tại")
    return FileResponse(target)


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
    """Queue a full Google Drive sync, or use the legacy background sync before cutover."""
    import sys
    main_mod = sys.modules.get("main")
    _has_creds = getattr(main_mod, "has_google_credentials", has_google_credentials) if main_mod else has_google_credentials
    _sync_all = getattr(main_mod, "sync_all_approved_to_drive", sync_all_approved_to_drive) if main_mod else sync_all_approved_to_drive
    if not _has_creds():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google Drive credentials (OAuth2 / Service Account) are not configured.",
        )
    if settings.DURABLE_DRIVE_ENABLED:
        from core.durable_work import queue_drive_sync

        try:
            pool = get_db_pool()
            if pool is None:
                raise RuntimeError("database pool unavailable")
            async with pool.connection() as conn:
                job_id = await queue_drive_sync(conn, actor=admin_user)
                await conn.commit()
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Google Drive sync queue is unavailable.",
            ) from None
        return {
            "status": "QUEUED",
            "message": "Đã kích hoạt đồng bộ Google Drive thành công trong nền.",
            "target_account": settings.GOOGLE_ADMIN_EMAIL,
            "job_id": job_id,
        }
    background_tasks.add_task(_sync_all, db_pool=get_db_pool())
    return {
        "status": "QUEUED",
        "message": "Đã kích hoạt đồng bộ Google Drive thành công trong nền.",
        "target_account": settings.GOOGLE_ADMIN_EMAIL,
    }
