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
import hashlib
import time
from typing import Optional, Dict, Any
from psycopg_pool import AsyncConnectionPool
import redis.asyncio as aioredis
import httpx
from services.vision_ai import DualModelVisionClient
from fastapi import HTTPException, status, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from config import settings
from core.passwords import verify_password
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
    from core.schema_sync import sync_all_persisted_forms
    return sync_all_persisted_forms(FORMS_STORAGE_DIR, DEFAULT_TRUST_BRIDGE_SCHEMA)

FORM_REGISTRY: Dict[str, FormSchema] = load_persisted_forms()


def _forms_disk_signature() -> tuple:
    """Cheap (name, mtime_ns, size) fingerprint of data/forms/*.json."""
    try:
        names = sorted(os.listdir(FORMS_STORAGE_DIR))
    except OSError:
        return ()
    entries = []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(FORMS_STORAGE_DIR, name)
        try:
            file_stat = os.stat(path)
        except OSError:
            continue
        entries.append((name, file_stat.st_mtime_ns, file_stat.st_size))
    return tuple(entries)


_FORMS_SIGNATURE: Optional[tuple] = _forms_disk_signature()
# Only forms that came from data/forms may be pruned when their file disappears;
# schemas registered directly in memory (tests, plugins) must survive a refresh.
_DISK_FORM_IDS: set = set(FORM_REGISTRY.keys())


def refresh_form_registry_from_disk(force: bool = False) -> bool:
    """Reload FORM_REGISTRY when data/forms/*.json changed on disk.

    uvicorn --workers N loads this module once per process, so a save handled by
    one worker is invisible to the others until restart. Every form read that
    matters calls this first; unchanged directories cost only a stat loop.
    Mutates the dict in place so modules holding the reference stay in sync.
    """
    global _FORMS_SIGNATURE
    signature = _forms_disk_signature()
    if not force and signature == _FORMS_SIGNATURE:
        return False

    from core.schema_sync import sync_all_persisted_forms

    loaded = sync_all_persisted_forms(FORMS_STORAGE_DIR, DEFAULT_TRUST_BRIDGE_SCHEMA)
    for form_id in _DISK_FORM_IDS - set(loaded):
        FORM_REGISTRY.pop(form_id, None)
    FORM_REGISTRY.update(loaded)
    _DISK_FORM_IDS.clear()
    _DISK_FORM_IDS.update(loaded.keys())
    _FORMS_SIGNATURE = _forms_disk_signature()
    return True


admin_security = HTTPBasic()


_ADMIN_HASH_CACHE: Dict[str, float] = {}
_ADMIN_HASH_CACHE_TTL = 300.0
_ADMIN_HASH_CACHE_MAX = 32


def _verify_admin_password(password: str, encoded: str) -> bool:
    """PBKDF2 is expensive; cache successful Basic credentials for a short TTL.

    Only a SHA-256 of (password + hash) is stored, never the plaintext.
    """
    key = hashlib.sha256(f"{password}\0{encoded}".encode("utf-8")).hexdigest()
    now = time.monotonic()
    expiry = _ADMIN_HASH_CACHE.get(key)
    if expiry is not None and expiry > now:
        return True
    if not verify_password(password, encoded):
        return False
    if len(_ADMIN_HASH_CACHE) >= _ADMIN_HASH_CACHE_MAX:
        _ADMIN_HASH_CACHE.clear()
    _ADMIN_HASH_CACHE[key] = now + _ADMIN_HASH_CACHE_TTL
    return True


async def _load_admin_account(username: str) -> Optional[Dict[str, Any]]:
    pool = get_db_pool()
    if not pool:
        return None
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT password_hash, is_active FROM admin_accounts WHERE username=%s",
                    (username,),
                )
                row = await cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    if isinstance(row, dict):
        return {"password_hash": row.get("password_hash"), "is_active": row.get("is_active")}
    return {"password_hash": row[0], "is_active": row[1]}


def _auth_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Thông tin đăng nhập quản trị viên không chính xác",
        headers={"WWW-Authenticate": "Basic"},
    )


async def verify_admin_auth(credentials: HTTPBasicCredentials = Depends(admin_security)) -> str:
    """Protects administrative endpoints with HTTP Basic Authentication.

    A database account is authoritative when its row exists; the environment
    credential is a break-glass fallback only when no row exists.
    """
    account = await _load_admin_account(credentials.username)
    if account is not None:
        valid = bool(account.get("is_active")) and _verify_admin_password(
            credentials.password, str(account.get("password_hash") or "")
        )
        if not valid:
            raise _auth_error()
        return credentials.username

    is_user_ok = secrets.compare_digest(credentials.username, settings.ADMIN_USERNAME)
    if settings.ADMIN_PASSWORD_HASH:
        is_pass_ok = _verify_admin_password(credentials.password, settings.ADMIN_PASSWORD_HASH)
    else:
        is_pass_ok = secrets.compare_digest(credentials.password, settings.ADMIN_PASSWORD)
    if not (is_user_ok and is_pass_ok):
        raise _auth_error()
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
