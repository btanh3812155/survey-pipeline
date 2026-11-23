# -*- coding: utf-8 -*-
"""
Forms Manager & Schema Router.
Decouples survey form definition, visual 3-tab editor, CTV draft persistence,
and schema validation from submission ingress.
"""

import html
import json
import os
import shutil
import time
import logging
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, ValidationError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from config import settings
from core.state import (
    verify_admin_auth,
    FORM_REGISTRY,
    FORMS_STORAGE_DIR,
    STATIC_DIR,
    get_db_pool,
    refresh_form_registry_from_disk,
)
from services.form_engine import FormSchema
from services.form_manager_html import render_forms_list_html, render_form_editor_html

logger = logging.getLogger("routers.admin_forms")
router = APIRouter(tags=["Forms Management"])


class DraftPayload(BaseModel):
    ctv_slug: str
    draft_data: dict
    ctv_id: Optional[str] = None


@router.get("/admin", include_in_schema=False)
async def admin_root(admin_user: str = Depends(verify_admin_auth)):
    """Redirects /admin directly to /admin/forms."""
    return RedirectResponse(url="/admin/forms", status_code=status.HTTP_302_FOUND)


@router.get("/admin/samples", include_in_schema=False)
async def admin_samples_page(admin_user: str = Depends(verify_admin_auth)):
    """Direct alias to the media manager gallery in admin."""
    return RedirectResponse(url="/admin/media", status_code=status.HTTP_302_FOUND)


@router.get("/admin/forms", response_class=HTMLResponse)
async def get_forms_list(
    page: int = 1,
    size: int = 6,
    sort: str = "updated_desc",
    admin_user: str = Depends(verify_admin_auth),
):
    """Admin Web UI with pagination to view and manage forms."""
    refresh_form_registry_from_disk()
    return HTMLResponse(
        content=render_forms_list_html(
            FORM_REGISTRY, page=page, page_size=size,
            environment=settings.ENVIRONMENT, sort=sort,
        ),
        status_code=200,
    )


@router.get("/admin/forms/create", response_class=HTMLResponse)
async def create_new_form_page(admin_user: str = Depends(verify_admin_auth)):
    """Dedicated 3-tab creation page."""
    return HTMLResponse(
        content=render_form_editor_html(None, is_new=True, environment=settings.ENVIRONMENT),
        status_code=200,
    )


@router.get("/admin/forms/edit/{form_id}", response_class=HTMLResponse)
async def edit_form_page(form_id: str, admin_user: str = Depends(verify_admin_auth)):
    """Dedicated 3-tab edit page for a specific form."""
    refresh_form_registry_from_disk()
    schema = FORM_REGISTRY.get(form_id)
    if not schema:
        raise HTTPException(status_code=404, detail="Không tìm thấy biểu mẫu")
    return HTMLResponse(
        content=render_form_editor_html(schema, is_new=False, environment=settings.ENVIRONMENT),
        status_code=200,
    )

async def _persist_form_schema(
    schema: FormSchema, previous_form_id: str, admin_user: str
) -> dict:
    previous_form_id = str(previous_form_id or "").strip()
    if (
        previous_form_id
        and previous_form_id != schema.form_id
        and previous_form_id in FORM_REGISTRY
    ):
        old_path = os.path.join(FORMS_STORAGE_DIR, f"{previous_form_id}.json")
        FORM_REGISTRY.pop(previous_form_id, None)
        if os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass
        db_pool = get_db_pool()
        if db_pool:
            try:
                async with db_pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "UPDATE survey_submissions SET form_id = %s WHERE form_id = %s;",
                            (schema.form_id, previous_form_id),
                        )
                    await conn.commit()
            except Exception as e:
                logger.warning(f"Failed to migrate submissions for form rename {previous_form_id}->{schema.form_id}: {e}")
        try:
            from services.google_drive_delivery import rename_local_form_folder, rename_form_folder
            rename_local_form_folder(previous_form_id, schema.form_id)
            await rename_form_folder(previous_form_id, schema.form_id)
        except Exception as e:
            logger.warning(f"Failed to rename Drive mirror for form {previous_form_id}->{schema.form_id}: {e}")

    schema_path = os.path.join(FORMS_STORAGE_DIR, f"{schema.form_id}.json")
    target_path = os.path.realpath(schema_path)
    base_path = os.path.realpath(FORMS_STORAGE_DIR)
    if not (target_path == base_path or target_path.startswith(base_path + os.sep)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Mã biểu mẫu không hợp lệ")

    FORM_REGISTRY[schema.form_id] = schema
    try:
        # Atomic replace so sibling workers never read a half-written JSON on refresh.
        tmp_path = f"{schema_path}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(schema.model_dump_json(indent=2))
        os.replace(tmp_path, schema_path)
    except Exception as e:
        logger.error(f"Failed to persist schema to disk: {e}")
    else:
        try:
            refresh_form_registry_from_disk(force=True)
        except Exception as e:
            logger.warning(f"Registry refresh after schema save failed: {e}")

    db_pool = get_db_pool()
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    if previous_form_id and previous_form_id != schema.form_id:
                        await cur.execute(
                            "DELETE FROM form_tags WHERE form_id = %s;", (previous_form_id,)
                        )
                    await cur.execute(
                        "DELETE FROM form_tags WHERE form_id = %s;", (schema.form_id,)
                    )
                    for tag in schema.settings.tags:
                        await cur.execute(
                            "INSERT INTO form_tags (form_id, tag) VALUES (%s, %s) "
                            "ON CONFLICT (form_id, tag) DO NOTHING;",
                            (schema.form_id, tag),
                        )
                await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to persist form tags for {schema.form_id}: {e}")

    return {
        "status": "success",
        "form_id": schema.form_id,
        "title": schema.title,
        "form_url": f"/forms/{schema.form_id}",
        "question_count": len(schema.questions),
        "message": f"Biểu mẫu '{schema.title}' đã sẵn sàng hoạt động!",
    }


@router.post("/api/v1/forms/upload-schema")
async def upload_form_schema(request: Request, admin_user: str = Depends(verify_admin_auth)):
    """Allows form owners to upload arbitrary survey schemas with custom rules and constraints."""
    raw_payload = await request.json()
    try:
        schema = FormSchema.model_validate(raw_payload)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(), body=raw_payload) from exc
    previous_form_id = str(raw_payload.get("previous_form_id") or "").strip()
    return await _persist_form_schema(schema, previous_form_id, admin_user)


@router.get("/api/v1/forms/export")
async def export_forms(admin_user: str = Depends(verify_admin_auth)):
    """Download every active form as one versioned JSON bundle."""
    refresh_form_registry_from_disk()
    payload = {
        "format": "survey-pipeline-forms",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "forms": [
            json.loads(schema.model_dump_json())
            for _form_id, schema in sorted(FORM_REGISTRY.items())
        ],
    }
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": 'attachment; filename="survey-pipeline-forms.json"'},
    )


@router.get("/api/v1/forms/{form_id}/distributions")
async def form_distributions(form_id: str, admin_user: str = Depends(verify_admin_auth)):
    """Read-only answer distributions for enabled distribution modules on one form."""
    refresh_form_registry_from_disk()
    schema = FORM_REGISTRY.get(form_id)
    if not schema:
        raise HTTPException(status_code=404, detail="Không tìm thấy biểu mẫu")
    pool = get_db_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="Cơ sở dữ liệu chưa sẵn sàng")
    from services.form_distributions import run_form_distributions
    try:
        reports = await run_form_distributions(schema, pool)
    except Exception as exc:
        logger.warning(f"Distribution report failed for {form_id}: {type(exc).__name__}")
        raise HTTPException(status_code=503, detail="Không thể tính phân bố câu trả lời.") from exc
    return {"form_id": form_id, "modules": reports}


@router.get("/api/v1/forms/{form_id}/export")
async def export_form(form_id: str, admin_user: str = Depends(verify_admin_auth)):
    """Download one form in the same JSON shape used by the import endpoint."""
    refresh_form_registry_from_disk()
    schema = FORM_REGISTRY.get(form_id)
    if not schema:
        raise HTTPException(status_code=404, detail="Không tìm thấy biểu mẫu")
    payload = {
        "format": "survey-pipeline-forms",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "forms": [json.loads(schema.model_dump_json())],
    }
    filename = f"survey-form-{form_id}.json"
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/v1/forms/import")
async def import_forms(
    request: Request,
    overwrite: bool = False,
    admin_user: str = Depends(verify_admin_auth),
):
    """Import one schema or a versioned bundle. Existing IDs require overwrite=true."""
    raw_payload = await request.json()
    if isinstance(raw_payload, list):
        raw_forms = raw_payload
    elif isinstance(raw_payload, dict) and isinstance(raw_payload.get("forms"), list):
        raw_forms = raw_payload["forms"]
    elif isinstance(raw_payload, dict) and isinstance(raw_payload.get("form"), dict):
        raw_forms = [raw_payload["form"]]
    elif isinstance(raw_payload, dict):
        raw_forms = [raw_payload]
    else:
        raw_forms = []
    if not raw_forms:
        raise HTTPException(status_code=400, detail="Tệp không chứa biểu mẫu hợp lệ")

    schemas: list[FormSchema] = []
    errors: list[dict] = []
    for index, item in enumerate(raw_forms):
        try:
            schemas.append(FormSchema.model_validate(item))
        except ValidationError as exc:
            errors.append({"index": index, "errors": exc.errors()})
    if errors:
        raise HTTPException(status_code=400, detail={"message": "Schema không hợp lệ", "forms": errors})
    seen_ids: set[str] = set()
    duplicate_ids: set[str] = set()
    for schema in schemas:
        if schema.form_id in seen_ids:
            duplicate_ids.add(schema.form_id)
        seen_ids.add(schema.form_id)
    if duplicate_ids:
        raise HTTPException(
            status_code=400,
            detail={"message": "Tệp chứa mã biểu mẫu trùng lặp", "duplicates": sorted(duplicate_ids)},
        )

    refresh_form_registry_from_disk()
    conflicts = sorted({schema.form_id for schema in schemas if schema.form_id in FORM_REGISTRY})
    if conflicts and not overwrite:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Biểu mẫu đã tồn tại; gửi lại với overwrite=true để ghi đè.",
                "conflicts": conflicts,
            },
        )

    backup_dir = os.path.join("data", "form_backups")
    imported = []
    for schema in schemas:
        existing = FORM_REGISTRY.get(schema.form_id)
        if existing:
            os.makedirs(backup_dir, exist_ok=True)
            backup_path = os.path.join(
                backup_dir, f"{schema.form_id}_import_{int(time.time())}.json"
            )
            with open(backup_path, "w", encoding="utf-8") as handle:
                handle.write(existing.model_dump_json(indent=2))
        result = await _persist_form_schema(schema, "", admin_user)
        imported.append(result)
    return {"status": "success", "imported": imported, "count": len(imported)}


@router.delete("/api/v1/forms/{form_id}")
async def delete_form(form_id: str, admin_user: str = Depends(verify_admin_auth)):
    """Deletes a form after archiving its full schema in the database."""
    refresh_form_registry_from_disk()
    schema = FORM_REGISTRY.get(form_id)
    if not schema:
        raise HTTPException(status_code=404, detail="Không tìm thấy biểu mẫu")

    backup_id = None
    trash_id = None
    schema_json = schema.model_dump_json()
    db_pool = get_db_pool()
    if db_pool:
        try:
            from core.trash import add_trash_item
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS deleted_forms (
                            id SERIAL PRIMARY KEY,
                            form_id VARCHAR(64) NOT NULL,
                            title TEXT,
                            schema_json JSONB NOT NULL,
                            deleted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            deleted_by TEXT
                        );
                        """
                    )
                    await cur.execute(
                        "INSERT INTO deleted_forms (form_id, title, schema_json, deleted_by) "
                        "VALUES (%s, %s, %s::jsonb, %s) RETURNING id;",
                        (form_id, schema.title, schema_json, str(admin_user)),
                    )
                    row = await cur.fetchone()
                    backup_id = row[0] if row else None
                    trash_id = await add_trash_item(
                        cur, "form", form_id, schema.title,
                        {"form_id": form_id, "title": schema.title, "schema": json.loads(schema_json)},
                        admin_user,
                        metadata={"legacy_archive_id": backup_id},
                    )
                await conn.commit()
        except Exception as e:
            logger.error(f"DB archive failed for form {form_id}; falling back to file backup: {e}")

    if backup_id is None:
        # Dev/DB-outage fallback: durable file backup before deletion.
        backup_dir = os.path.join("data", "form_backups")
        os.makedirs(backup_dir, exist_ok=True)
        backup_path = os.path.join(backup_dir, f"{form_id}_{int(time.time())}.json")
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(schema_json)
        backup_id = backup_path

    FORM_REGISTRY.pop(form_id, None)
    schema_path = os.path.join(FORMS_STORAGE_DIR, f"{form_id}.json")
    if os.path.exists(schema_path):
        os.remove(schema_path)
    logger.info(f"Form '{form_id}' deleted by admin {admin_user}; backup={backup_id}")
    drive_cleanup = {"status": "skipped"}
    try:
        from services.drive_cleanup import cleanup_form_drive
        drive_cleanup = await cleanup_form_drive(form_id, db_pool)
        ledger = drive_cleanup.pop("ledger", [])
        if db_pool and trash_id:
            from core.trash import record_drive_ledger, update_trash_metadata, compact_trash
            await record_drive_ledger(
                db_pool, "form", form_id, ledger, admin_user,
                trash_id=trash_id, action="DRIVE_TRASHED",
            )
            await update_trash_metadata(
                db_pool, trash_id,
                {"backup": backup_id, "drive_cleanup": drive_cleanup, "drive_ledger_items": len(ledger)},
                admin_user,
            )
            await compact_trash(db_pool)
    except Exception as exc:
        logger.warning("Drive cleanup skipped for deleted form %s: %s", form_id, type(exc).__name__)
        drive_cleanup = {"status": "error", "error": type(exc).__name__}
    return {
        "status": "success",
        "form_id": form_id,
        "backup": backup_id,
        "trash_id": trash_id,
        "drive_cleanup": drive_cleanup,
    }


@router.get("/api/v1/admin/trash")
async def list_deleted_items(
    limit: int = 200, admin_user: str = Depends(verify_admin_auth)
):
    """List recoverable form/submission snapshots; superadmin-only by route policy."""
    from core.trash import list_trash
    return {"items": await list_trash(get_db_pool(), limit=limit)}


@router.get("/admin/trash", response_class=HTMLResponse)
async def admin_trash_page(admin_user: str = Depends(verify_admin_auth)):
    """Minimal trash browser with restore actions."""
    from core.trash import RETENTION_FREQUENCIES, last_compaction, list_trash
    pool = get_db_pool()
    items = await list_trash(pool, limit=500)
    last_run = await last_compaction(pool)
    tier_labels = {
        "hourly": "mỗi giờ", "four_hourly": "mỗi 4 giờ", "daily": "mỗi ngày",
        "weekly": "mỗi tuần", "monthly": "mỗi tháng", "semiannual": "mỗi 6 tháng",
    }
    rows = []
    for item in items:
        meta = item.get("metadata") or {}
        media = meta.get("media_trash_path") or ""
        rows.append(
            f"<tr><td><code>{html.escape(str(item['id']))}</code></td>"
            f"<td>{html.escape(str(item['entity_type']))}</td>"
            f"<td><code>{html.escape(str(item['entity_id']))}</code><br><span class=muted>{html.escape(str(item['label'] or ''))}</span></td>"
            f"<td>{html.escape(str(item['deleted_at']))}<br><span class=muted>{html.escape(tier_labels.get(str(item['retention_tier']), str(item['retention_tier'])))}</span></td>"
            f"<td>{'media nội bộ' if media else 'không có media'}<br><span class=muted>{int(meta.get('drive_ledger_items') or 0)} liên kết Drive</span></td>"
            f"<td><button onclick=\"restoreTrash('{html.escape(str(item['id']))}')\">Khôi phục</button></td></tr>"
        )
    policy = "".join(
        f"<li><b>{html.escape(tier_labels.get(k, k))}</b>: {html.escape(v)}</li>"
        for k, v in RETENTION_FREQUENCIES.items()
    )
    last_html = "Chưa chạy dọn lần nào."
    if last_run:
        last_html = (
            f"#{last_run['id']} {html.escape(str(last_run['status']))} "
            f"lúc {html.escape(str(last_run['finished_at'] or last_run['started_at']))} "
            f"(giữ {last_run['kept']}, xóa {last_run['removed']}, "
            f"bảo vệ {last_run['protected_recent']})"
        )
    return HTMLResponse(f"""<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Thùng rác</title><style>
:root{{color-scheme:dark;--bg:#0b1220;--panel:#131d30;--line:#2a3952;--fg:#e5edf7;--muted:#9aabc1;--accent:#38bdf8}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif}}main{{max-width:1200px;margin:auto;padding:24px}}
table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line)}}th,td{{text-align:left;padding:10px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{color:var(--accent)}}.muted{{color:var(--muted);font-size:.78rem}}code{{color:#67e8f9}}button{{background:#0369a1;color:#fff;border:0;border-radius:6px;padding:7px 12px;cursor:pointer}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;margin:12px 0}}
</style></head><body><main><h1>Thùng rác</h1><p class=muted>Form được khôi phục nguyên vẹn. Submission được đưa về MANUAL_REVIEW; outbox và hiệu ứng Telegram không được phát lại.</p>
<div class=card><b>Thời gian lưu giữ</b><ul>{policy}</ul></div>
<div class=card><b>Lần dọn gần nhất</b><div id=lastCompaction>{last_html}</div>
<button onclick="runCompaction()" style="margin-top:8px;">Dọn ngay</button></div>
<table><thead><tr><th>ID</th><th>Loại</th><th>Thực thể</th><th>Đã xóa</th><th>Tệp lưu</th><th></th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan=6 class=muted>Thùng rác trống.</td></tr>'}</tbody></table>
<script>
async function runCompaction() {{
  if (!confirm('Dọn thùng rác ngay?')) return;
  const resp = await fetch('/api/v1/admin/trash/compact', {{method:'POST', credentials:'include'}});
  const body = await resp.json().catch(() => ({{}}));
  if (resp.ok) window.location.reload();
  else alert(body.detail || 'Dọn thùng rác thất bại');
}}
async function restoreTrash(id) {{
  if (!confirm('Khôi phục mục này? Form được khôi phục nguyên vẹn; submission về MANUAL_REVIEW.')) return;
  const resp = await fetch('/api/v1/admin/trash/' + encodeURIComponent(id) + '/restore', {{method:'POST', credentials:'include'}});
  const body = await resp.json().catch(() => ({{}}));
  if (resp.ok) window.location.reload();
  else alert(body.detail || 'Khôi phục thất bại');
}}
</script></main></body></html>""")


def _restore_media(metadata: dict) -> dict:
    media_path = str((metadata or {}).get("media_trash_path") or "").strip()
    if not media_path:
        return {"status": "none"}
    root = os.path.realpath(os.path.join(os.getcwd(), "data", "trash"))
    source = os.path.realpath(os.path.join(os.getcwd(), media_path))
    if not source.startswith(root + os.sep):
        return {"status": "rejected", "path": media_path}
    photos = os.path.join(source, "photos")
    fallback = os.path.join(source, "fallback.webp")
    warnings = []
    if os.path.isdir(photos):
        dest_root = os.path.realpath(os.path.join(os.getcwd(), "data", "submission_photos"))
        dest = os.path.realpath(os.path.join(dest_root, os.path.basename(os.path.dirname(source))))
        if os.path.exists(dest):
            warnings.append("submission media destination already exists")
        else:
            os.makedirs(dest_root, exist_ok=True)
            shutil.move(photos, dest)
    if os.path.isfile(fallback):
        upload = os.path.join(os.getcwd(), "data", "uploads", "surveys", f"{os.path.basename(os.path.dirname(source))}.webp")
        if os.path.exists(upload):
            warnings.append("fallback upload destination already exists")
        else:
            os.makedirs(os.path.dirname(upload), exist_ok=True)
            shutil.move(fallback, upload)
    return {"status": "restored" if not warnings else "partial", "warnings": warnings}


@router.get("/api/v1/admin/trash/compaction")
async def trash_compaction_report(admin_user: str = Depends(verify_admin_auth)):
    from core.trash import last_compaction
    return {"last_run": await last_compaction(get_db_pool())}


@router.post("/api/v1/admin/trash/compact")
async def run_trash_compaction(admin_user: str = Depends(verify_admin_auth)):
    from core.trash import run_compaction
    report = await run_compaction(get_db_pool(), actor=admin_user)
    if report.get("status") == "busy":
        raise HTTPException(status_code=409, detail="Dọn thùng rác đang chạy.")
    return report


@router.post("/api/v1/admin/trash/{trash_id}/restore")
async def restore_deleted_item(trash_id: str, admin_user: str = Depends(verify_admin_auth)):
    """Restore a form intact or a submission into MANUAL_REVIEW without replaying side effects."""
    from core.trash import get_trash_item, mark_restored, restore_drive_ledger
    pool = get_db_pool()
    item = await get_trash_item(pool, trash_id)
    if not item:
        raise HTTPException(status_code=404, detail="Không tìm thấy mục trong thùng rác.")
    if item.get("restored_at") or item.get("purged_at"):
        raise HTTPException(status_code=409, detail="Mục đã được khôi phục hoặc đã bị xóa.")
    entity_type = str(item["entity_type"])
    entity_id = str(item["entity_id"])
    metadata = item.get("metadata") or {}

    if entity_type == "form":
        snapshot = (item.get("snapshot") or {}).get("schema")
        try:
            schema = FormSchema.model_validate(snapshot)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail={"message": "Schema thùng rác không hợp lệ.", "errors": exc.errors()}) from exc
        refresh_form_registry_from_disk()
        schema_path = os.path.join(FORMS_STORAGE_DIR, f"{schema.form_id}.json")
        if schema.form_id in FORM_REGISTRY or os.path.exists(schema_path):
            raise HTTPException(status_code=409, detail="A form with this ID already exists")
        result = await _persist_form_schema(schema, "", admin_user)
        drive = await restore_drive_ledger(pool, entity_type, entity_id, admin_user)
        if not await mark_restored(pool, trash_id, admin_user, {"drive": drive, "form_id": schema.form_id}):
            raise HTTPException(status_code=409, detail="Mục bị thay đổi bởi thao tác khác.")
        return {"status": "restored", "entity_type": entity_type, "entity_id": schema.form_id, "form": result, "drive": drive}

    if entity_type == "submission":
        snapshot = dict((item.get("snapshot") or {}).get("submission") or {})
        if not snapshot.get("submission_id"):
            raise HTTPException(status_code=400, detail="Bản chụp submission trong thùng rác không hợp lệ.")
        submission_id = str(snapshot["submission_id"])
        snapshot["status"] = "MANUAL_REVIEW"
        snapshot["rejection_reason"] = "Khôi phục từ thùng rác; chờ duyệt lại."
        snapshot["moderated_at"] = None
        snapshot["moderated_by_admin_id"] = None
        snapshot["moderation_notes"] = None
        try:
            async with pool.connection() as conn:
                async with conn.transaction(), conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO survey_submissions SELECT * FROM json_populate_record(NULL::survey_submissions, %s::json) "
                        "ON CONFLICT (submission_id) DO NOTHING RETURNING submission_id",
                        (json.dumps(snapshot, default=str),),
                    )
                    if not await cur.fetchone():
                        raise HTTPException(status_code=409, detail="A submission with this ID already exists")
        except HTTPException:
            raise
        media = _restore_media(metadata)
        drive = await restore_drive_ledger(pool, entity_type, submission_id, admin_user)
        if not await mark_restored(pool, trash_id, admin_user, {"drive": drive, "media": media, "submission_id": submission_id}):
            raise HTTPException(status_code=409, detail="Mục bị thay đổi bởi thao tác khác.")
        return {"status": "restored", "entity_type": entity_type, "entity_id": submission_id, "submission_status": "MANUAL_REVIEW", "media": media, "drive": drive}

    raise HTTPException(status_code=400, detail="Unsupported trash entity type")


@router.get("/api/v1/forms/draft/{slug}")
async def get_form_draft(slug: str):
    """Drafts are stored strictly client-side in localStorage (YAGNI / Privacy)."""
    return {"status": "client_only", "slug": slug, "draft": {}}


@router.post("/api/v1/forms/draft")
async def save_form_draft(payload: DraftPayload):
    """Drafts are stored strictly client-side in localStorage (YAGNI / Privacy)."""
    return {"status": "client_only", "slug": payload.ctv_slug}
