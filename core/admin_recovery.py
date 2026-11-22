"""Database-fenced administrator recovery and authenticated TLS mail delivery."""

import asyncio
import hmac
import ipaddress
import json
import logging
import re
import secrets
import smtplib
import ssl
from dataclasses import dataclass
from datetime import timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from psycopg import Error
from psycopg.rows import dict_row
from psycopg_pool import PoolTimeout

from config import BUILTIN_APP_SECRET, settings
from core.passwords import hash_password

logger = logging.getLogger(__name__)
RESET = "reset"
EMAIL = "email"
DB_ERRORS = (Error, PoolTimeout)
CHALLENGE_ID = re.compile(r"[0-9a-f]{32}")
OTP = re.compile(r"[0-9]{8}")
LOCAL_PART = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*")
DOMAIN_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
LIMITS = {"request_ip": 20, "request_account": 5, "confirm_ip": 60, "confirm_account": 25}


@dataclass(frozen=True, repr=False)
class SMTPConfig:
    host: str
    port: int
    mode: str
    username: str
    password: str
    sender: str


@dataclass(frozen=True, repr=False)
class Delivery:
    challenge_id: str
    purpose: str
    messages: tuple[tuple[str, str], ...]


def normalize_email(value: str) -> str | None:
    value = value.strip()
    local, sep, domain = value.rpartition("@")
    labels = domain.split(".")
    if (
        not sep or len(value) > 254 or len(local) > 64 or not LOCAL_PART.fullmatch(local)
        or len(labels) < 2 or not all(DOMAIN_LABEL.fullmatch(label) for label in labels)
    ):
        return None
    return f"{local}@{domain.lower()}"


def valid_password(value: str) -> bool:
    # FastAPI's HTTP Basic implementation decodes credentials as ASCII.
    return 12 <= len(value) <= 1024 and value.isascii() and value.isprintable()


def smtp_config() -> SMTPConfig | None:
    sender = normalize_email(settings.SMTP_FROM_EMAIL)
    host = settings.SMTP_HOST.strip()
    mode = settings.SMTP_TLS_MODE.strip().lower()
    username = settings.SMTP_USERNAME.strip()
    password = settings.SMTP_PASSWORD.get_secret_value() if settings.SMTP_PASSWORD else ""
    try:
        port = int(settings.SMTP_PORT)
    except ValueError:
        return None
    if (
        not host or not all(DOMAIN_LABEL.fullmatch(label) for label in host.split("."))
        or not 1 <= port <= 65535 or mode not in {"starttls", "implicit"}
        or not username or not password or not sender
        or len(settings.APP_SECRET) < 32 or settings.APP_SECRET == BUILTIN_APP_SECRET
    ):
        return None
    return SMTPConfig(host, port, mode, username, password, sender)


def _send_mail(config: SMTPConfig, delivery: Delivery) -> None:
    context = ssl.create_default_context()
    args = (config.host, config.port)
    connection = (
        smtplib.SMTP_SSL(*args, timeout=8, context=context)
        if config.mode == "implicit" else smtplib.SMTP(*args, timeout=8)
    )
    with connection as smtp:
        if config.mode == "starttls":
            smtp.starttls(context=context)  # Mandatory, with hostname and CA checks.
        smtp.login(config.username, config.password)
        for recipient, code in delivery.messages:
            message = EmailMessage()
            message["From"] = config.sender
            message["To"] = recipient
            message["Date"] = formatdate(usegmt=True)
            message["Message-ID"] = make_msgid(domain=config.sender.rsplit("@", 1)[1])
            message["Subject"] = "Admin password recovery" if delivery.purpose == RESET else "Admin recovery email change"
            message.set_content(
                f"Your one-time code is {code}. It expires after 10 minutes.\n"
                "If you did not request this, ignore this email. Never share the code.\n"
            )
            if smtp.send_message(message, from_addr=config.sender, to_addrs=[recipient]):
                raise smtplib.SMTPException("recipient not accepted")


def _digest(*values) -> bytes:
    payload = json.dumps(values, separators=(",", ":")).encode()
    return hmac.digest(settings.APP_SECRET.encode(), payload, "sha256")


def _account_state(account) -> bytes:
    return _digest(
        "account-state", account["username"], account["password_hash"], account["recovery_email"],
        account["role"], account["is_active"], account["deleted_at"] is not None,
        account["created_at"].astimezone(timezone.utc).isoformat(),
        account["updated_at"].astimezone(timezone.utc).isoformat(),
    )


def _code_hash(challenge_id: str, purpose: str, slot: str, code: str) -> bytes:
    return _digest("admin-recovery-otp", challenge_id, purpose, slot, code)


def _ip_key(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "unknown"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((int(address), 64), strict=False))
    return str(address)


async def ensure_admin_recovery(pool) -> bool:
    """Check migration/fresh schema availability without mutating the schema or sending mail."""
    if not pool:
        return False
    try:
        async with pool.connection() as conn:
            await conn.execute("SELECT id, ready, attempts, state_hash FROM admin_recovery_challenges LIMIT 0")
            await conn.execute("SELECT bucket, hits, expires_at FROM admin_recovery_limits LIMIT 0")
            await conn.execute("SELECT action FROM system_audit_log LIMIT 0")
    except DB_ERRORS as exc:
        logger.warning("Admin recovery schema unavailable; apply migration 007 (%s)", type(exc).__name__)
        return False
    return True


async def _take_limit(cur, scope: str, subject: str) -> bool:
    bucket = _digest("admin-recovery-limit", scope, subject)
    await cur.execute(
        """INSERT INTO admin_recovery_limits AS limits (bucket,hits,expires_at)
           VALUES (%s,1,statement_timestamp()+interval '1 hour')
           ON CONFLICT (bucket) DO UPDATE SET
               hits=CASE WHEN limits.expires_at <= statement_timestamp() THEN 1 ELSE limits.hits+1 END,
               expires_at=CASE WHEN limits.expires_at <= statement_timestamp() THEN EXCLUDED.expires_at ELSE limits.expires_at END
           WHERE limits.expires_at <= statement_timestamp() OR limits.hits < %s
           RETURNING hits""",
        (bucket, LIMITS[scope]),
    )
    return bool(await cur.fetchone())


async def _allow_ip(pool, scope: str, host: str) -> bool:
    # Short separate transaction: IP cleanup only locks expired buckets and
    # never holds an account/challenge lock or waits for SMTP.
    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        allowed = await _take_limit(cur, scope, _ip_key(host))
        await cur.execute(
            """DELETE FROM admin_recovery_limits WHERE bucket IN (
               SELECT bucket FROM admin_recovery_limits WHERE expires_at <= statement_timestamp()
               ORDER BY expires_at LIMIT 32 FOR UPDATE SKIP LOCKED)"""
        )
        return allowed


async def _lock_account(cur, username: str):
    await cur.execute(
        """SELECT username,password_hash,recovery_email,role,is_active,deleted_at,created_at,updated_at
           FROM admin_accounts WHERE username=%s AND role='superadmin' AND is_active
           AND deleted_at IS NULL FOR UPDATE""",
        (username,),
    )
    return await cur.fetchone()


async def issue_challenge(
    pool, host: str, purpose: str, username: str, *, new_email: str | None = None, auth_hash: str | None = None,
) -> tuple[str, Delivery | None]:
    """Reserve an inactive challenge. The public caller also gets a random ID on denial."""
    challenge_id = secrets.token_hex(16)
    if not await _allow_ip(pool, "request_ip", host) or not username or len(username) > 64 or "\x00" in username:
        return challenge_id, None
    async with pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        account = await _lock_account(cur, username)
        if not account or (purpose == EMAIL and account["password_hash"] != auth_hash):
            return challenge_id, None
        if not await _take_limit(cur, "request_account", username):
            return challenge_id, None
        old_email = normalize_email(account["recovery_email"] or "")
        if not old_email:
            return challenge_id, None
        if purpose == EMAIL:
            new_email = normalize_email(new_email or "")
            if not new_email or new_email.lower() == old_email.lower():
                return challenge_id, None
        else:
            new_email = None
        old_code = f"{secrets.randbelow(10**8):08d}"
        messages = ((old_email, old_code),)
        new_hash = None
        if new_email:
            new_code = f"{(int(old_code) + 1 + secrets.randbelow(10**8 - 1)) % 10**8:08d}"
            new_hash = _code_hash(challenge_id, purpose, "new", new_code)
            messages += ((new_email, new_code),)
        await cur.execute(
            """INSERT INTO admin_recovery_challenges AS challenges
                   (id, username, purpose, state_hash, old_code_hash, new_code_hash, new_email, created_at, expires_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,clock_timestamp(),clock_timestamp()+interval '10 minutes')
               ON CONFLICT (username,purpose) DO UPDATE SET
                   id=EXCLUDED.id,state_hash=EXCLUDED.state_hash,old_code_hash=EXCLUDED.old_code_hash,
                   new_code_hash=EXCLUDED.new_code_hash,new_email=EXCLUDED.new_email,created_at=EXCLUDED.created_at,
                   expires_at=EXCLUDED.expires_at,ready=FALSE,attempts=0,consumed_at=NULL
               WHERE challenges.created_at <= clock_timestamp()-interval '60 seconds'
               RETURNING id""",
            (challenge_id, username, purpose, _account_state(account),
             _code_hash(challenge_id, purpose, "old", old_code), new_hash, new_email),
        )
        if not await cur.fetchone():
            return challenge_id, None
    return challenge_id, Delivery(challenge_id, purpose, messages)


async def deliver_challenge(pool, config: SMTPConfig, delivery: Delivery) -> bool:
    """Only activate the challenge after every required message is accepted by SMTP."""
    try:
        await asyncio.to_thread(_send_mail, config, delivery)
    except (OSError, ValueError, smtplib.SMTPException) as exc:
        # Provider replies can echo addresses or message data. Log only the class.
        logger.warning("Admin recovery SMTP delivery unavailable (%s)", type(exc).__name__)
        return False
    try:
        async with pool.connection() as conn:
            result = await conn.execute(
                "UPDATE admin_recovery_challenges SET ready=TRUE WHERE id=%s AND expires_at > clock_timestamp()",
                (delivery.challenge_id,),
            )
            return result.rowcount == 1
    except DB_ERRORS as exc:
        logger.warning("Admin recovery challenge activation unavailable (%s)", type(exc).__name__)
        return False


async def confirm_challenge(
    pool, host: str, purpose: str, challenge_id: str, code: str, *,
    new_password: str = "", new_code: str = "", username: str | None = None, auth_hash: str | None = None,
) -> bool:
    """Consume a valid code and commit the credential update and audit together."""
    if not await _allow_ip(pool, "confirm_ip", host) or not CHALLENGE_ID.fullmatch(challenge_id):
        return False
    async with pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT username FROM admin_recovery_challenges WHERE id=%s AND purpose=%s",
            (challenge_id, purpose),
        )
        owner = await cur.fetchone()
        if not owner or (purpose == EMAIL and owner["username"] != username):
            return False
        username = owner["username"]
        # Issue and confirm both lock the account before touching the challenge.
        account = await _lock_account(cur, username)
        if not account or (purpose == EMAIL and account["password_hash"] != auth_hash):
            return False
        if not await _take_limit(cur, "confirm_account", username):
            return False
        await cur.execute(
            """SELECT state_hash,old_code_hash,new_code_hash,new_email FROM admin_recovery_challenges
               WHERE id=%s AND ready AND consumed_at IS NULL AND attempts < 5
                 AND expires_at > clock_timestamp() FOR UPDATE""",
            (challenge_id,),
        )
        challenge = await cur.fetchone()
        if not challenge or not hmac.compare_digest(challenge["state_hash"], _account_state(account)):
            return False
        old_ok = bool(OTP.fullmatch(code)) and hmac.compare_digest(
            challenge["old_code_hash"], _code_hash(challenge_id, purpose, "old", code),
        )
        new_ok = purpose == RESET or (bool(OTP.fullmatch(new_code)) and hmac.compare_digest(
            challenge["new_code_hash"], _code_hash(challenge_id, purpose, "new", new_code),
        ))
        if not (old_ok and new_ok):
            await cur.execute("UPDATE admin_recovery_challenges SET attempts=attempts+1 WHERE id=%s", (challenge_id,))
            return False
        if purpose == RESET:
            if not valid_password(new_password):
                return False
            value = await asyncio.to_thread(hash_password, new_password)
            action = "ADMIN_PASSWORD_RECOVERED"
            update = "UPDATE admin_accounts SET password_hash=%s, updated_at=clock_timestamp() WHERE username=%s"
        else:
            value = challenge["new_email"]
            action = "ADMIN_RECOVERY_EMAIL_CHANGED"
            update = "UPDATE admin_accounts SET recovery_email=%s, updated_at=clock_timestamp() WHERE username=%s"
        await cur.execute(
            "UPDATE admin_recovery_challenges SET consumed_at=clock_timestamp() WHERE id=%s AND expires_at > clock_timestamp()",
            (challenge_id,),
        )
        if cur.rowcount != 1:
            return False
        await cur.execute(update, (value, username))
        await cur.execute(
            """INSERT INTO system_audit_log(component_name,action,entity_type,entity_id,actor_type,actor_id,details)
               VALUES ('admin_recovery',%s,'ADMIN',%s,'ADMIN',%s,'{"method":"email_otp"}')""",
            (action, username, username),
        )
        return True
