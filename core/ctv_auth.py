"""CTV browser authentication: trusted directory bindings plus single-use Telegram OTPs."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime, timezone
from urllib.request import Request as HTTPRequest, urlopen
from uuid import UUID

from fastapi import HTTPException, Request

from config import settings
from core.phone_links import VERIFIED_PHONE_LINK_SOURCES, normalize_vn_phone, phone_normalization_sql
from core.state import get_db_pool, get_redis_client

COOKIE_NAME = "tb_ctv_session"
CHALLENGE_COOKIE_NAME = "tb_ctv_challenge"
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
OTP_TTL_SECONDS = 300
MAX_OTP_ATTEMPTS = 5

_UNAVAILABLE = "Đăng nhập CTV tạm thời không khả dụng. Vui lòng thử lại sau."
_DENIED = "Không thể xác minh tài khoản CTV hoặc mã xác nhận."
_CHALLENGE_ID = re.compile(r"[0-9a-f]{32}")
_OTP = re.compile(r"[0-9]{6}")

# Every counter is incremented and given its fixed-window expiry atomically.
_LIMIT_SCRIPT = """
local hits=redis.call('INCR',KEYS[1])
if hits == 1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end
return hits <= tonumber(ARGV[2]) and 1 or 0
"""

# A successful comparison and deletion are one operation across all web workers.
_REDEEM_SCRIPT = """
local raw=redis.call('GET',KEYS[1])
if not raw then return {0} end
local challenge=cjson.decode(raw)
if tonumber(challenge.exp) <= tonumber(ARGV[1]) then
    redis.call('DEL',KEYS[1]); return {0}
end
local hits=redis.call('INCR',challenge.attempt_key)
if hits == 1 then redis.call('EXPIRE',challenge.attempt_key,ARGV[3]) end
if hits > tonumber(ARGV[4]) then return {-1} end
challenge.attempts=challenge.attempts+1
if challenge.attempts > tonumber(ARGV[5]) then
    redis.call('DEL',KEYS[1]); return {0}
end
if challenge.digest == ARGV[2] then
    redis.call('DEL',KEYS[1]); return {1,challenge.code,challenge.binding}
end
if challenge.attempts >= tonumber(ARGV[5]) then
    redis.call('DEL',KEYS[1])
else
    redis.call('SET',KEYS[1],cjson.encode(challenge),'KEEPTTL')
end
return {0}
"""


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail=_UNAVAILABLE)


def _denied() -> HTTPException:
    return HTTPException(status_code=401, detail=_DENIED)


def _digest(purpose: str, *values: object) -> str:
    if not settings.APP_SECRET:
        raise _unavailable()
    data = json.dumps([purpose, *values], ensure_ascii=False, separators=(",", ":")).encode()
    return hmac.new(settings.APP_SECRET.encode(), data, hashlib.sha256).hexdigest()


def _ip_bucket(request: Request, purpose: str) -> str:
    # Do not trust user-supplied forwarding headers; the ASGI server owns proxy trust.
    host = request.client.host if request.client else "unknown"
    return f"ctv-auth:{purpose}:ip:{_digest('ctv-rate-ip-v1', host)}"


def _redis():
    client = get_redis_client()
    if client is None:
        raise _unavailable()
    return client


async def _take_limit(client, key: str, maximum: int) -> None:
    try:
        allowed = int(await client.eval(_LIMIT_SCRIPT, 1, key, OTP_TTL_SECONDS, maximum))
    except Exception:
        raise _unavailable() from None
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Bạn đã thử quá nhiều lần. Vui lòng thử lại sau 5 phút.",
            headers={"Retry-After": str(OTP_TTL_SECONDS)},
        )


async def list_login_codes() -> list[str]:
    """Public dropdown: active database codes only, with no cached/static fallback."""
    pool = get_db_pool()
    if pool is None:
        raise _unavailable()
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT code FROM ctv_directory WHERE is_active = TRUE ORDER BY code")
            return [str(row[0]) for row in await cur.fetchall()]
    except Exception:
        raise _unavailable() from None


async def _identity(*, code: str = "", phone: str = "") -> tuple[dict, str] | None:
    pool = get_db_pool()
    if pool is None:
        raise _unavailable()
    normalized = phone_normalization_sql("d.phone")
    other_phone = phone_normalization_sql("other.phone")
    # Phone ambiguity is rejected even if a duplicate directory entry is inactive.
    selector = "d.code = %s" if code else f"({normalized}) = %s"
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                f"""SELECT d.code, d.display_name, p.phone_normalized,
                           d.telegram_user_id, d.is_active, p.updated_at, p.source, d.auth_revision
                    FROM ctv_directory d
                    JOIN phone_telegram_links p ON p.phone_normalized = ({normalized})
                        AND p.telegram_user_id = d.telegram_user_id
                    WHERE {selector} AND d.is_active = TRUE AND d.telegram_user_id > 0
                        AND p.source IN (%s, %s)
                        AND NOT EXISTS (
                            SELECT 1 FROM ctv_directory other
                            WHERE other.code <> d.code AND ({other_phone}) = p.phone_normalized
                        )
                    LIMIT 2""",
                (code or phone, *VERIFIED_PHONE_LINK_SOURCES),
            )
            rows = await cur.fetchall()
    except Exception:
        raise _unavailable() from None
    if len(rows) != 1:
        return None
    code, name, phone, telegram_id, active, updated_at, source, auth_revision = rows[0]
    if (
        not active or not normalize_vn_phone(phone) or normalize_vn_phone(phone) != phone
        or not telegram_id or int(telegram_id) <= 0 or source not in VERIFIED_PHONE_LINK_SOURCES
        or not isinstance(updated_at, datetime) or updated_at.utcoffset() is None
        or not isinstance(auth_revision, UUID)
    ):
        return None
    identity = {
        "code": str(code), "display_name": str(name or ""), "phone": phone,
        "telegram_user_id": int(telegram_id), "is_active": True,
    }
    binding = _digest(
        "ctv-binding-v2", code, phone, int(telegram_id), source,
        updated_at.astimezone(timezone.utc).isoformat(), str(auth_revision),
    )
    return identity, binding


def _send_otp(token: str, telegram_id: int, otp: str) -> None:
    # urllib does not log Telegram's secret-bearing URL (httpx logs it at INFO).
    request = HTTPRequest(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({
            "chat_id": telegram_id,
            "text": f"Mã đăng nhập cổng CTV của bạn: {otp}\n"
                    "Mã có hiệu lực 5 phút và chỉ dùng một lần. Không chia sẻ mã này.",
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=8) as response:
        if response.status != 200 or json.loads(response.read(65536)).get("ok") is not True:
            raise ValueError("OTP delivery rejected")


async def start_login(request: Request, *, code: str = "", phone: str = "") -> str:
    """Send an OTP for exactly one public code or verified directory phone."""
    code, phone = code.strip(), phone.strip()
    if bool(code) == bool(phone) or len(code) > 256 or len(phone) > 64 or "\x00" in code:
        raise HTTPException(status_code=400, detail="Chọn mã CTV hoặc nhập số điện thoại, không nhập cả hai.")
    client = _redis()
    await _take_limit(client, _ip_bucket(request, "send"), 10)
    normalized = normalize_vn_phone(phone) if phone else ""
    if phone and not normalized:
        raise _denied()
    resolved = await _identity(code=code, phone=normalized)
    if resolved is None:
        raise _denied()
    identity, binding = resolved
    account = _digest("ctv-rate-account-v1", identity["code"])
    await _take_limit(client, f"ctv-auth:send:ctv:{account}", 3)
    token = str(settings.TELEGRAM_DRIVER_BOT_TOKEN or "").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise _unavailable()
    challenge_id = secrets.token_hex(16)
    otp = f"{secrets.randbelow(10**6):06d}"
    try:
        await asyncio.to_thread(_send_otp, token, identity["telegram_user_id"], otp)
        challenge = json.dumps({
            "code": identity["code"], "binding": binding,
            "digest": _digest("ctv-otp-v1", challenge_id, otp),
            "exp": int(time.time()) + OTP_TTL_SECONDS, "attempts": 0,
            "attempt_key": f"ctv-auth:verify:ctv:{account}",
        }, separators=(",", ":"))
        saved = await client.set(f"ctv-auth:challenge:{challenge_id}", challenge, ex=OTP_TTL_SECONDS, nx=True)
        if not saved:
            raise _unavailable()
    except Exception:
        # Never log provider replies/exceptions: they may contain the bot token.
        raise _unavailable() from None
    return challenge_id


def _issue_session(code: str, binding: str) -> str:
    payload = json.dumps({
        "purpose": "ctv-session-v1", "code": code, "binding": binding,
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
    }, ensure_ascii=False, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{body}.{_digest('ctv-session-signature-v1', body)}"


async def finish_login(request: Request, challenge_id: str, otp: str) -> str:
    """Consume one browser-bound challenge, then repeat trusted identity validation."""
    if (
        not _CHALLENGE_ID.fullmatch(challenge_id)
        or request.cookies.get(CHALLENGE_COOKIE_NAME) != challenge_id
    ):
        raise _denied()
    client = _redis()
    await _take_limit(client, _ip_bucket(request, "verify"), 60)
    candidate = otp if _OTP.fullmatch(otp) else "invalid"
    try:
        result = await client.eval(
            _REDEEM_SCRIPT, 1, f"ctv-auth:challenge:{challenge_id}", int(time.time()),
            _digest("ctv-otp-v1", challenge_id, candidate), OTP_TTL_SECONDS, 20, MAX_OTP_ATTEMPTS,
        )
        status = int(result[0])
        if status == 1:
            code, binding = (value.decode() if isinstance(value, bytes) else value for value in result[1:])
    except Exception:
        raise _unavailable() from None
    if status == -1:
        raise HTTPException(status_code=429, detail="Bạn đã thử quá nhiều lần. Vui lòng thử lại sau 5 phút.",
                            headers={"Retry-After": str(OTP_TTL_SECONDS)})
    if status != 1:
        raise _denied()
    resolved = await _identity(code=code)
    if resolved is None or not hmac.compare_digest(binding, resolved[1]):
        raise _denied()
    return _issue_session(code, binding)


async def ctv_from_session(request: Request) -> dict | None:
    """Revalidate the live directory and stable trusted binding on every request."""
    token = request.cookies.get(COOKIE_NAME, "")
    if not token or len(token) > 4096 or not token.isascii() or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _digest("ctv-session-signature-v1", body)):
        return None
    try:
        payload = json.loads(base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_", validate=True))
        if (
            not isinstance(payload, dict) or payload.get("purpose") != "ctv-session-v1"
            or type(payload.get("exp")) is not int or payload["exp"] <= int(time.time())
            or not isinstance(payload.get("code"), str) or not payload["code"]
            or not isinstance(payload.get("binding"), str) or not re.fullmatch(r"[0-9a-f]{64}", payload["binding"])
        ):
            return None
    except (ValueError, TypeError, UnicodeError):
        return None
    resolved = await _identity(code=payload["code"])
    if resolved is None or not hmac.compare_digest(payload["binding"], resolved[1]):
        return None
    return resolved[0]
