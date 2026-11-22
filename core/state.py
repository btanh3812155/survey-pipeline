# -*- coding: utf-8 -*-
"""
Core Application State & Dependency Injection.
Decouples runtime singletons (PostgreSQL connection pool, Redis client, HTTP client)
and authentication guards from route definitions.
"""

import os
import json
import logging
import hashlib
import time
from urllib.parse import urlsplit
from typing import Any, Dict, Optional
from psycopg_pool import AsyncConnectionPool
import redis.asyncio as aioredis
import httpx
from services.vision_ai import DualModelVisionClient
from fastapi import HTTPException, status, Depends, Request
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



def require_admin_same_origin(request: Request) -> None:
    """Reject unsafe browser requests without an exact same-origin source."""
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    source = request.headers.get("origin") or request.headers.get("referer")
    host = request.headers.get("host", "")
    if not source or not host:
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    parsed = urlsplit(source)
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip().lower()
    allowed_schemes = {request.url.scheme.lower()}
    allowed_hosts = {host.lower()}
    if forwarded_proto:
        allowed_schemes.add(forwarded_proto)
    if forwarded_host:
        allowed_hosts.add(forwarded_host)
    if (
        parsed.username
        or parsed.password
        or parsed.scheme.lower() not in allowed_schemes
        or parsed.netloc.lower() not in allowed_hosts
    ):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")


async def _check_admin_auth_rate_limit(request: Request, username: str) -> tuple[Any, str, str]:
    redis = get_redis_client()
    if redis is None:
        if settings.ENVIRONMENT == "production":
            raise HTTPException(status_code=503, detail="Admin authentication unavailable")
        return None, "", ""
    ip = request.client.host if request.client else "unknown"
    account_key = hashlib.sha256(username[:64].casefold().encode()).hexdigest()
    ip_key = hashlib.sha256(ip.encode()).hexdigest()
    account_bucket = f"admin-auth:account:{account_key}"
    ip_bucket = f"admin-auth:ip:{ip_key}"
    try:
        limited = await redis.eval(
            "return (tonumber(redis.call('GET', KEYS[1]) or '0') >= 10 "
            "or tonumber(redis.call('GET', KEYS[2]) or '0') >= 60) and 1 or 0",
            2,
            account_bucket,
            ip_bucket,
        )
    except Exception as exc:
        logger.warning("admin auth limiter unavailable: %s", type(exc).__name__)
        if settings.ENVIRONMENT == "production":
            raise HTTPException(status_code=503, detail="Admin authentication unavailable")
        return None, "", ""
    if int(limited or 0):
        raise HTTPException(
            status_code=429,
            detail="Too many failed admin login attempts; retry in 60 seconds.",
            headers={"Retry-After": "60"},
        )
    return redis, account_bucket, ip_bucket


async def _record_admin_auth_failure(redis, account_bucket: str, ip_bucket: str) -> None:
    if redis is None:
        return
    try:
        await redis.eval(
            "for i=1,2 do local n=redis.call('INCR', KEYS[i]); "
            "if n == 1 then redis.call('EXPIRE', KEYS[i], 60) end end; return 1",
            2,
            account_bucket,
            ip_bucket,
        )
    except Exception as exc:
        logger.warning("admin auth limiter update failed: %s", type(exc).__name__)
        if settings.ENVIRONMENT == "production":
            raise HTTPException(status_code=503, detail="Admin authentication unavailable")


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
        raise HTTPException(status_code=503, detail="Admin authentication unavailable")
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT password_hash, is_active, role, deleted_at FROM admin_accounts WHERE username=%s",
                    (username,),
                )
                row = await cur.fetchone()
    except Exception as exc:
        logger.warning("admin account lookup failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Admin authentication unavailable") from exc
    if not row:
        return None
    if isinstance(row, dict):
        return {
            "password_hash": row.get("password_hash"),
            "is_active": row.get("is_active"),
            "role": row.get("role"),
            "deleted_at": row.get("deleted_at"),
        }
    return {"password_hash": row[0], "is_active": row[1], "role": row[2], "deleted_at": row[3]}


def _auth_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Thông tin đăng nhập quản trị viên không chính xác",
        headers={"WWW-Authenticate": "Basic"},
    )


def _admin_roles_for_path(path: str, method: str) -> set[str]:
    path = (path or "/").rstrip("/") or "/"
    method = (method or "GET").upper()
    everyone = {"superadmin", "manager", "reviewer", "viewer"}
    operators = {"superadmin", "manager"}
    reviewers = {"superadmin", "manager", "reviewer"}
    if path == "/admin/accounts/password":
        return everyone
    if path.startswith(("/admin/accounts", "/admin/recovery", "/api/v1/admin/recovery")):
        return {"superadmin"}
    if path.startswith("/api/v1/dev/submissions"):
        return {"superadmin"}
    if path == "/api/v1/telegram/set-webhooks":
        return {"superadmin"}
    if path in {"/admin/submissions", "/admin/data", "/submissions", "/data"} or path.startswith("/admin/data/"):
        return everyone if method == "GET" else operators
    if path.startswith("/admin/submissions/"):
        return everyone if method == "GET" else operators
    if path == "/api/v1/submissions":
        return everyone if method == "GET" else operators
    if path.startswith("/api/v1/submissions/"):
        if "/photos" in path:
            return everyone if method == "GET" else operators
        if path.endswith("/actions"):
            return reviewers if method == "GET" else operators
        return reviewers if method == "GET" else operators
    if path == "/api/v1/surveys/history":
        return operators
    if path in {"/admin/audit", "/audit"} or path.startswith("/api/v1/audit/"):
        return operators
    if path.startswith(("/admin/forms", "/admin/samples", "/api/v1/forms")):
        return operators
    if path.startswith(("/admin/ctvs", "/api/v1/ctvs")):
        return operators
    if path.startswith(("/admin/media", "/api/v1/media")):
        return operators
    if path.startswith(("/admin/image-objects", "/api/v1/image-objects")):
        return operators
    if path.startswith(("/admin/modules", "/api/v1/admin/modules")):
        return operators
    if path.startswith(("/admin/exports", "/api/v1/admin/exports")):
        return operators
    return {"superadmin"}


admin_security = HTTPBasic()
async def verify_admin_auth(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(admin_security),
) -> str:
    """Authenticate through the database and enforce route roles."""
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        require_admin_same_origin(request)
    redis, account_bucket, ip_bucket = await _check_admin_auth_rate_limit(
        request, credentials.username
    )
    account = await _load_admin_account(credentials.username)
    if account is None or not account.get("is_active") or account.get("deleted_at") is not None:
        await _record_admin_auth_failure(redis, account_bucket, ip_bucket)
        raise _auth_error()
    password_hash = str(account.get("password_hash") or "")
    if not _verify_admin_password(credentials.password, password_hash):
        await _record_admin_auth_failure(redis, account_bucket, ip_bucket)
        raise _auth_error()
    role = str(account.get("role") or "viewer")
    if role not in _admin_roles_for_path(request.url.path, request.method):
        raise HTTPException(status_code=403, detail="Tài khoản không có quyền truy cập mục này.")
    request.state.admin_user = credentials.username
    request.state.admin_role = role
    request.state.admin_password_hash = password_hash
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
