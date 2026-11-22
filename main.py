# -*- coding: utf-8 -*-
"""
High-Performance Survey Verification & Anti-Fraud Pipeline.
Modular decoupled architecture orchestrating domain routers:
- routers/system: Caddy TLS verification, health checks, geo IP fallback, root redirects.
- routers/admin_forms: Dynamic form builder, 3-tab visual editor, CTV draft persistence.
- routers/admin_data: Submissions Data Hub, query/sort/filter table, Excel CSV/JSON export.
- routers/admin_audit: Immutable audit logs, cryptographic hop chains, Decree 13/GDPR compliance.
- routers/admin_media: Decoupled Google Drive-style media manager with dir tree and WebP pipeline.
- routers/actions: Idempotent Action Checklist status inspector and manual retry dispatcher.
- routers/webhooks: Bytedance Lark Base and Telegram bot push webhook ingress.
- routers/survey: Public WebRTC survey ingress, blur pre-checks, dual-AI audits, fraud screening.
"""

import asyncio
import os
import logging
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote

import httpx
import redis.asyncio as aioredis
from psycopg_pool import AsyncConnectionPool
from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles

from config import settings
import core.state as state
from core.state import (
    FORM_REGISTRY,
    FORMS_STORAGE_DIR,
    MEDIA_STORAGE_DIR,
    DEV_STORAGE_FILE,
    STATIC_DIR,
    verify_admin_auth,
    get_db_pool,
    get_redis_client,
    get_http_client,
)
from core.cache import BoundedTTLCache
from core.repositories import (
    fetch_aggregated_submissions,
    append_dev_submission,
    fetch_aggregated_audit_logs,
    append_dev_audit_log,
)
from routers import (
    system_router,
    admin_forms_router,
    admin_data_router,
    admin_audit_router,
    admin_media_router,
    admin_ctvs_router,
    admin_image_objects_router,
    admin_accounts_router,
    admin_modules_router,
    admin_exports_router,
    admin_recovery_router,
    actions_router,
    webhooks_router,
    survey_router,
)
from routers.admin_media import scan_media_storage_files, seed_media_storage
from services.google_drive_delivery import has_google_credentials, sync_all_approved_to_drive
vision_client = state.vision_client
from routers.system import DEFAULT_GEO_FALLBACK, get_sample_image, caddy_on_demand_tls_check
from routers.survey import (
    submit_webrtc_survey,
    get_dynamic_form,
    send_telegram_notification,
    dispatch_submission_telegram_notifications,
    render_driver_submission_verdict_message,
    lookup_telegram_user_by_phone,
    lookup_telegram_user_for_collaborator,
    send_telegram_direct_message,
    LOCAL_IDEMP_CACHE,
    PRE_CHECK_RATE_LIMITER,
    AppealRequest,
)
# Re-export module-level attributes for backward compatibility with test suites
db_pool: Optional[AsyncConnectionPool] = None
redis_client: Optional[aioredis.Redis] = None
http_client: Optional[httpx.AsyncClient] = None

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main_api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, redis_client, db_pool
    startup_pool = None
    trash_compaction_task = None

    try:
        # 1. Initialize HTTP client pool
        state.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=3.0),
            limits=httpx.Limits(max_keepalive_connections=50, max_connections=200),
        )
        http_client = state.http_client

        # Redis supplies shared caches and the production admin-auth failure limiter.
        # Admin authentication fails closed if Redis cannot provide that limiter.
        state.redis_client = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
            max_connections=50,
        )
        try:
            await state.redis_client.ping()
            redis_client = state.redis_client
            logger.info("[REDIS] Successfully connected to Redis pool.")
        except Exception as e:
            logger.warning("[REDIS] unavailable: %s. Production admin authentication will fail closed.", e)
            try:
                await state.redis_client.aclose()
            except Exception:
                pass
            state.redis_client = None
            redis_client = None

        # 3. Initialize PostgreSQL. Production fails closed; development/test
        # retains the explicit labelled fallback for local workflows.
        try:
            startup_pool = AsyncConnectionPool(
                conninfo=settings.DATABASE_URL,
                min_size=2,
                max_size=20,
                timeout=10.0,
                open=False,
            )
            await startup_pool.open()
            async with startup_pool.connection() as conn:
                await conn.execute("SELECT 1")
            state.db_pool = startup_pool
            db_pool = startup_pool
            startup_pool = None
            logger.info("[POSTGRES] Successfully connected to PostgreSQL pool.")
        except Exception as e:
            if startup_pool is not None:
                try:
                    await startup_pool.close()
                except Exception:
                    pass
                startup_pool = None
            state.db_pool = None
            db_pool = None
            if settings.ENVIRONMENT == "production":
                logger.error("[POSTGRES] production startup failed; refusing to start without the writer database.")
                raise RuntimeError("production PostgreSQL startup failed") from e
            logger.warning("[POSTGRES] PostgreSQL unavailable: %s. Falling back to dev storage.", e)

        # 3b. CTV platform tables + directory cache (idempotent; optional in dev)
        try:
            from core.ctv import ensure_ctv_platform_schema
            await ensure_ctv_platform_schema(state.db_pool)
        except Exception as e:
            logger.warning(f"[CTV] Platform schema/cache init failed: {e}")

        # 3c. Standalone phone <-> Telegram mapping (no driver/CTV dependency)
        try:
            from core.phone_links import ensure_phone_link_schema
            await ensure_phone_link_schema(state.db_pool)
        except Exception as e:
            logger.warning(f"[PHONE LINKS] Schema init failed: {e}")

        # 3d. Admin accounts: schema, one-time environment admin seed, and 30-day bin purge.
        try:
            from core.admin_accounts import ensure_admin_accounts, purge_expired_admin_accounts, seed_env_admin
            await ensure_admin_accounts(state.db_pool)
            await seed_env_admin(state.db_pool)
            await purge_expired_admin_accounts(state.db_pool)
            from core.admin_recovery import ensure_admin_recovery
            if not await ensure_admin_recovery(state.db_pool):
                logger.warning("[ADMIN RECOVERY] Migration 007 schema is unavailable")
        except Exception as e:
            logger.warning(f"[ADMIN ACCOUNTS] init failed: {e}")

        # 3e. Registry-backed image/logic modules (migration 008).
        try:
            from core.admin_modules import ensure_admin_module_registry
            if not await ensure_admin_module_registry(state.db_pool):
                logger.warning("[ADMIN MODULES] migration 008 is unavailable")
        except Exception as e:
            logger.warning("[ADMIN MODULES] registry init failed: %s", type(e).__name__)

        # 3f. Google Drive export configuration (migration 009).
        try:
            from core.drive_exports import ensure_drive_exports
            if not await ensure_drive_exports(state.db_pool):
                logger.warning("[DRIVE EXPORTS] migration 009 is unavailable")
        except Exception as e:
            logger.warning("[DRIVE EXPORTS] init failed: %s", type(e).__name__)

        # 3g. Non-blocking DB trash compaction scheduler (migration 014).
        try:
            from core.trash import start_trash_compaction
            trash_compaction_task = start_trash_compaction(
                state.db_pool,
                settings.TRASH_COMPACT_INTERVAL_SEC,
                settings.TRASH_COMPACT_INITIAL_DELAY_SEC,
            )
            if trash_compaction_task:
                logger.info("[TRASH] compaction scheduler started")
        except Exception as e:
            logger.warning("[TRASH] scheduler init failed: %s", type(e).__name__)

        # 4. Seed media directory
        seed_media_storage()

        # 4b. Load and validate file-defined image objects + check engines.
        try:
            from services.image_objects import load_registry
            registry = load_registry(force=True)
            logger.info(
                "[IMAGE OBJECTS] loaded %d object type(s) and %d check engine(s)",
                len(registry["objects"]),
                len(registry["engines"]),
            )
        except Exception as e:
            logger.error("[IMAGE OBJECTS] registry load failed: %s", e)

        # Webhook mode never calls start_polling to install the command menus.
        try:
            from services.telegram_polling import configure_bot_menus
            await configure_bot_menus()
        except Exception as e:
            logger.debug(f"Bot command menu setup failed: {e}")

        from core.embedding_executor import embedding_executor_lifespan

        async with embedding_executor_lifespan():
            yield
    finally:
        # Cancellation during pool.open()/connection() bypasses `except Exception`;
        # this local reference guarantees the unpublished pool is still closed.
        if startup_pool is not None:
            try:
                await startup_pool.close()
            except Exception as e:
                logger.warning("[POSTGRES] unpublished pool cleanup failed: %s", e)
            startup_pool = None
        if trash_compaction_task is not None:
            trash_compaction_task.cancel()
            try:
                await trash_compaction_task
            except (asyncio.CancelledError, Exception):
                pass
            trash_compaction_task = None
        # Partial startup and normal shutdown share this cleanup path.
        if state.db_pool:
            try:
                await state.db_pool.close()
            except Exception as e:
                logger.warning("[POSTGRES] pool cleanup failed: %s", e)
            state.db_pool = None
            db_pool = None
        if state.redis_client:
            try:
                await state.redis_client.aclose()
            except Exception:
                pass
            state.redis_client = None
            redis_client = None
        if state.http_client:
            try:
                await state.http_client.aclose()
            except Exception:
                pass
            state.http_client = None
            http_client = None


# Create FastAPI application
app = FastAPI(title="Survey Verification Pipeline", lifespan=lifespan)


@app.middleware("http")
async def inject_admin_nav(request, call_next):
    """Add the shared admin top bar and the authenticated account role."""
    response = await call_next(request)
    if request.url.path.startswith("/admin") or getattr(request.state, "admin_user", None):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/admin") and response.headers.get(
        "content-type", ""
    ).startswith("text/html"):
        if hasattr(response, "body_iterator"):
            body = b"".join([chunk async for chunk in response.body_iterator])
        else:
            body = getattr(response, "body", b"")
        role = getattr(request.state, "admin_role", "viewer")
        if role not in {"superadmin", "manager", "reviewer", "viewer"}:
            role = "viewer"
        meta = f'<meta name="admin-role" content="{role}">'.encode()
        if b"</head>" in body:
            body = body.replace(b"</head>", meta + b"</head>", 1)
        if b"</body>" in body:
            nav_script = (
                f'<script src="/static/ui/admin_nav.js?v={quote(settings.APP_BUILD_ID, safe="")}" defer></script>'
            ).encode()
            body = body.replace(b"</body>", nav_script + b"</body>", 1)
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() != "content-length"
        }
        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )
    return response

# Mount static asset endpoints
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if os.path.exists(MEDIA_STORAGE_DIR):
    app.mount("/media", StaticFiles(directory=MEDIA_STORAGE_DIR), name="media")

# Include decoupled domain routers
app.include_router(system_router)
app.include_router(admin_forms_router)
app.include_router(admin_data_router)
app.include_router(admin_audit_router)
app.include_router(admin_media_router)
app.include_router(admin_ctvs_router)
app.include_router(admin_image_objects_router)
app.include_router(admin_accounts_router)
app.include_router(admin_modules_router)
app.include_router(admin_exports_router)
app.include_router(admin_recovery_router)
app.include_router(actions_router)
app.include_router(webhooks_router)
app.include_router(survey_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT, workers=1)
