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

import os
import logging
from contextlib import asynccontextmanager
from typing import Optional

import httpx
import redis.asyncio as aioredis
from psycopg_pool import AsyncConnectionPool
from fastapi import FastAPI
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

    try:
        # 1. Initialize HTTP client pool
        state.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=3.0),
            limits=httpx.Limits(max_keepalive_connections=50, max_connections=200),
        )
        http_client = state.http_client

        # 2. Initialize Redis async connection pool. Redis is an optional
        # accelerator; PostgreSQL is the required production dependency.
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
            logger.warning("[REDIS] Redis unavailable: %s. Falling back to in-memory cache.", e)
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

        # 5. Register the CTV bot command menu (webhook mode never calls start_polling)
        try:
            from services.telegram_polling import configure_driver_bot_menu
            await configure_driver_bot_menu()
        except Exception as e:
            logger.debug(f"Driver bot command menu setup failed: {e}")

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
app.include_router(actions_router)
app.include_router(webhooks_router)
app.include_router(survey_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT, workers=1)
