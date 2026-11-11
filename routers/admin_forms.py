# -*- coding: utf-8 -*-
"""
Forms Manager & Schema Router.
Decouples survey form definition, visual 3-tab editor, CTV draft persistence,
and schema validation from submission ingress.
"""

import os
import logging
from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from config import settings
from core.state import (
    verify_admin_auth,
    FORM_REGISTRY,
    FORMS_STORAGE_DIR,
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
async def get_forms_list(page: int = 1, size: int = 6, admin_user: str = Depends(verify_admin_auth)):
    """Admin Web UI with pagination to view and manage forms."""
    return HTMLResponse(
        content=render_forms_list_html(FORM_REGISTRY, page=page, page_size=size, environment=settings.ENVIRONMENT),
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
async def upload_form_schema(schema: FormSchema, admin_user: str = Depends(verify_admin_auth)):
    """Allows form owners to upload arbitrary survey schemas with custom rules and constraints."""
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


@router.get("/api/v1/forms/draft/{slug}")
async def get_form_draft(slug: str):
    """Retrieves saved draft responses for a specific CTV link."""
    db_pool = get_db_pool()
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT draft_data, ctv_id FROM survey_drafts WHERE ctv_slug = %s LIMIT 1;",
                        (slug,)
                    )
                    row = await cur.fetchone()
                    if row:
                        return {"status": "success", "slug": slug, "ctv_id": row[1], "draft": row[0]}
        except Exception as e:
            logger.warning(f"Error fetching draft from DB: {e}")
    return {"status": "not_found", "slug": slug, "draft": {}}


@router.post("/api/v1/forms/draft")
async def save_form_draft(payload: DraftPayload):
    """Persists filled-in data per CTV link across page reloads and cross-device access."""
    slug = payload.ctv_slug.strip()
    if not slug:
        raise HTTPException(status_code=400, detail="Mã liên kết CTV không được để trống")
    db_pool = get_db_pool()
    if db_pool:
        try:
            import json
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO survey_drafts (ctv_slug, ctv_id, draft_data, updated_at)
                        VALUES (%s, %s, %s, NOW())
                        ON CONFLICT (ctv_slug) DO UPDATE
                        SET draft_data = EXCLUDED.draft_data,
                            ctv_id = COALESCE(EXCLUDED.ctv_id, survey_drafts.ctv_id),
                            updated_at = NOW();
                        """,
                        (slug, payload.ctv_id, json.dumps(payload.draft_data))
                    )
                await conn.commit()
            return {"status": "success", "slug": slug}
        except Exception as e:
            logger.warning(f"Failed to save survey draft: {e}")
            raise HTTPException(status_code=500, detail="Không thể lưu bản nháp")
    return {"status": "skipped", "reason": "db_not_configured"}
