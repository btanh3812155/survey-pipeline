"""Signed admin web sessions for the login form (not HTTP Basic popups)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Optional

from config import settings

COOKIE_NAME = "tb_admin_session"
SESSION_TTL_SECONDS = 12 * 60 * 60


def password_fingerprint(encoded_hash: object) -> str:
    return hashlib.sha256(str(encoded_hash or "").encode("utf-8")).hexdigest()[:32]


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue(username: str, password_hash: object, *, ttl: int = SESSION_TTL_SECONDS) -> str:
    payload = json.dumps(
        {
            "u": str(username),
            "ph": password_fingerprint(password_hash),
            "exp": int(time.time()) + max(60, int(ttl)),
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    body = _b64encode(payload)
    signature = hmac.new(
        str(settings.APP_SECRET).encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{body}.{signature}"


def read(token: Optional[str]) -> Optional[dict[str, Any]]:
    if not token or len(token) > 4096 or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    expected = hmac.new(
        str(settings.APP_SECRET).encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_b64decode(body))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not payload.get("u") or not payload.get("ph"):
        return None
    try:
        if int(payload.get("exp") or 0) <= int(time.time()):
            return None
    except (TypeError, ValueError):
        return None
    return payload
