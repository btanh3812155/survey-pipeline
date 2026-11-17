# -*- coding: utf-8 -*-
"""
Forms Manager & Schema Router.
Decouples survey form definition, visual 3-tab editor, CTV draft persistence,
and schema validation from submission ingress.
"""

import os
import time
import logging
from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from config import settings
from core.state import (
    verify_admin_auth,
    FORM_REGISTRY,
    FORMS_STORAGE_DIR,
    STATIC_DIR,
    get_db_pool,
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
    schema = FORM_REGISTRY.get(form_id)
    if not schema:
        raise HTTPException(status_code=404, detail="Không tìm thấy biểu mẫu")
    return HTMLResponse(
        content=render_form_editor_html(schema, is_new=False, environment=settings.ENVIRONMENT),
        status_code=200,
    )

@router.post("/api/v1/forms/upload-schema")
async def upload_form_schema(request: Request, admin_user: str = Depends(verify_admin_auth)):
    """Allows form owners to upload arbitrary survey schemas with custom rules and constraints."""
    raw_payload = await request.json()
    schema = FormSchema.model_validate(raw_payload)
    previous_form_id = str(raw_payload.get("previous_form_id") or "").strip()

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
        with open(schema_path, "w", encoding="utf-8") as f:
            f.write(schema.model_dump_json(indent=2))
    except Exception as e:
        logger.error(f"Failed to persist schema to disk: {e}")
    return {
        "status": "success",
        "form_id": schema.form_id,
        "title": schema.title,
        "form_url": f"/forms/{schema.form_id}",
        "question_count": len(schema.questions),
        "message": f"Biểu mẫu '{schema.title}' đã sẵn sàng hoạt động!",
    }


@router.delete("/api/v1/forms/{form_id}")
async def delete_form(form_id: str, admin_user: str = Depends(verify_admin_auth)):
    """Deletes a form after archiving its full schema in the database."""
    schema = FORM_REGISTRY.get(form_id)
    if not schema:
        raise HTTPException(status_code=404, detail="Không tìm thấy biểu mẫu")

    backup_id = None
    schema_json = schema.model_dump_json()
    db_pool = get_db_pool()
    if db_pool:
        try:
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
    return {"status": "success", "form_id": form_id, "backup": backup_id}


@router.get("/api/v1/forms/draft/{slug}")
async def get_form_draft(slug: str):
    """Drafts are stored strictly client-side in localStorage (YAGNI / Privacy)."""
    return {"status": "client_only", "slug": slug, "draft": {}}


@router.post("/api/v1/forms/draft")
async def save_form_draft(payload: DraftPayload):
    """Drafts are stored strictly client-side in localStorage (YAGNI / Privacy)."""
    return {"status": "client_only", "slug": payload.ctv_slug}
