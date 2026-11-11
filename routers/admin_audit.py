# -*- coding: utf-8 -*-
"""
Immutable Audit Trail Router.
Decouples compliance reporting, Decree 13/GDPR audit hop inspection,
and cryptographic hash verification.
"""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from config import settings
from core.state import verify_admin_auth
from core.repositories import fetch_aggregated_audit_logs
from services.audit_viewer_html import render_audit_viewer_html

logger = logging.getLogger("routers.admin_audit")
router = APIRouter(tags=["Audit Logs"])


@router.get("/admin/audit", response_class=HTMLResponse)
@router.get("/audit", response_class=HTMLResponse)
async def admin_audit_dashboard(
    request: Request,
    admin_user: str = Depends(verify_admin_auth),
):
    """Web UI dashboard to view all immutable audit logs and cryptographic hop chains."""
    logs = await fetch_aggregated_audit_logs()
    return HTMLResponse(
        content=render_audit_viewer_html(logs, environment=settings.ENVIRONMENT),
        status_code=200,
    )


@router.get("/api/v1/audit/logs")
async def get_all_audit_logs_api(
    component: Optional[str] = None,
    entity: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 500,
    admin_user: str = Depends(verify_admin_auth),
):
    """API endpoint providing aggregated JSON audit logs with stats summary."""
    logs = await fetch_aggregated_audit_logs(
        component_filter=component, entity_filter=entity, search=search, limit=limit
    )
    return {
        "count": len(logs),
        "logs": logs,
        "summary": {
            "total": len(logs),
            "ingress_normalizer": sum(
                1 for l in logs if l.get("hop_number") in (1, 2) or l.get("component_name") in ("INGRESS", "NORMALIZER")
            ),
            "ai_fraud": sum(
                1 for l in logs if l.get("hop_number") in (3, 4, 5) or l.get("component_name") in ("ANTI_FRAUD", "AI_VISION", "EVALUATOR")
            ),
            "persistence_admin": sum(
                1 for l in logs if l.get("hop_number") in (6, 7, 8) or l.get("component_name") in ("PERSISTENCE", "MODERATION", "OUTBOX", "PRIVACY_ENGINE", "FORM_ENGINE")
            ),
        },
    }
