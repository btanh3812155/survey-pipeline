# -*- coding: utf-8 -*-
"""
Core Application State & Dependency Injection.
Decouples runtime singletons (PostgreSQL connection pool, Redis client, HTTP client)
and authentication guards from route definitions.
"""

import os
import json
import logging
import secrets
from typing import Optional, Dict, Any
from psycopg_pool import AsyncConnectionPool
import redis.asyncio as aioredis
import httpx
from services.vision_ai import DualModelVisionClient
from fastapi import HTTPException, status, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from config import settings
from services.form_engine import FormSchema, DEFAULT_TRUST_BRIDGE_SCHEMA

logger = logging.getLogger("core.state")

# Shared runtime singletons
db_pool: Optional[AsyncConnectionPool] = None
redis_client: Optional[aioredis.Redis] = None
http_client: Optional[httpx.AsyncClient] = None

vision_client = DualModelVisionClient(
    gemini_api_key=settings.GEMINI_API_KEY,
    gemini_base_url=settings.GEMINI_BASE_URL,
    deepseek_api_key=settings.DEEPSEEK_API_KEY,
    deepseek_base_url=settings.DEEPSEEK_BASE_URL,
    timeout_seconds=settings.AI_TIMEOUT_SECONDS,
)
# Storage paths
FORMS_STORAGE_DIR = os.path.join(os.getcwd(), "data", "forms")
os.makedirs(FORMS_STORAGE_DIR, exist_ok=True)

MEDIA_STORAGE_DIR = os.path.join(os.getcwd(), "data", "media")
os.makedirs(MEDIA_STORAGE_DIR, exist_ok=True)
for sub in ("guide", "forms", "general"):
    os.makedirs(os.path.join(MEDIA_STORAGE_DIR, sub), exist_ok=True)

DEV_STORAGE_FILE = os.path.join(os.getcwd(), "data", "dev_submissions.json")
STATIC_DIR = os.path.join(os.getcwd(), "static")


def load_persisted_forms() -> Dict[str, FormSchema]:
    registry: Dict[str, FormSchema] = {
        DEFAULT_TRUST_BRIDGE_SCHEMA.form_id: DEFAULT_TRUST_BRIDGE_SCHEMA
    }
    default_path = os.path.join(FORMS_STORAGE_DIR, f"{DEFAULT_TRUST_BRIDGE_SCHEMA.form_id}.json")
    if not os.path.exists(default_path):
        try:
            with open(default_path, "w", encoding="utf-8") as f:
                f.write(DEFAULT_TRUST_BRIDGE_SCHEMA.model_dump_json(indent=2))
        except Exception as e:
            logger.warning(f"Failed to persist default schema: {e}")

    try:
        for fname in os.listdir(FORMS_STORAGE_DIR):
            if fname.endswith(".json"):
                fpath = os.path.join(FORMS_STORAGE_DIR, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    schema = FormSchema.model_validate(data)
                    registry[schema.form_id] = schema
                except Exception as err:
                    logger.warning(f"Error loading schema from {fname}: {err}")
    except Exception as e:
        logger.warning(f"Error scanning forms directory: {e}")

    return registry


FORM_REGISTRY: Dict[str, FormSchema] = load_persisted_forms()

admin_security = HTTPBasic()


def verify_admin_auth(credentials: HTTPBasicCredentials = Depends(admin_security)) -> str:
    """Protects administrative endpoints with HTTP Basic Authentication."""
    is_user_ok = secrets.compare_digest(credentials.username, settings.ADMIN_USERNAME)
    is_pass_ok = secrets.compare_digest(credentials.password, settings.ADMIN_PASSWORD)
    if not (is_user_ok and is_pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Thông tin đăng nhập quản trị viên không chính xác",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def get_db_pool() -> Optional[AsyncConnectionPool]:
    import sys
    main_mod = sys.modules.get("main")
    if main_mod and getattr(main_mod, "db_pool", None) is not None:
        return main_mod.db_pool
    return db_pool


def get_redis_client() -> Optional[aioredis.Redis]:
    import sys
    main_mod = sys.modules.get("main")
    if main_mod and getattr(main_mod, "redis_client", None) is not None:
        return main_mod.redis_client
    return redis_client


def get_http_client() -> Optional[httpx.AsyncClient]:
    import sys
    main_mod = sys.modules.get("main")
    if main_mod and getattr(main_mod, "http_client", None) is not None:
        return main_mod.http_client
    return http_client
