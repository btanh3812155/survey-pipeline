# -*- coding: utf-8 -*-
"""Standalone phone <-> Telegram chat mapping.

This table is deliberately independent of ``authorized_users`` and
``ctv_directory``: a phone answer in a question-source form can resolve to a
Telegram chat without requiring a driver record, CTV code, or role.
"""

import logging
import re
from datetime import datetime
from typing import Optional, Tuple

from core.state import get_db_pool

logger = logging.getLogger("core.phone_links")
VERIFIED_PHONE_LINK_SOURCES = ("verified_contact", "admin_verified")
CONFLICTED_PHONE_LINK_SOURCE = "ownership_conflict"


PHONE_LINK_TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS phone_telegram_links (
        phone_normalized TEXT PRIMARY KEY,
        telegram_user_id BIGINT NOT NULL,
        source TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_phone_telegram_links_tg ON phone_telegram_links (telegram_user_id);",
)


def normalize_vn_phone(phone) -> Optional[str]:
    """Normalize a Vietnamese mobile number to local 0xxxxxxxxx form.

    Accepts spaces/dashes, +84, 84, 0084 and bare 9-digit forms. Returns None
    when the value is not a plausible VN mobile number.
    """
    raw = str(phone or "").strip()
    if not raw:
        return None
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("+84"):
        digits = "0" + digits[3:]
    elif digits.startswith("0084"):
        digits = "0" + digits[4:]
    elif digits.startswith("84") and len(digits) == 11:
        digits = "0" + digits[2:]
    elif len(digits) == 9 and digits.isdigit():
        digits = "0" + digits
    if not re.fullmatch(r"0\d{9,10}", digits):
        return None
    return digits




def phone_normalization_sql(column: str = "driver_phone") -> str:
    """SQL expression with the same accepted VN forms as normalize_vn_phone.

    Keeps '+' while removing spaces/dashes, then canonicalizes only exact
    country-code/bare forms. It deliberately does not use suffix matching.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", column or ""):
        raise ValueError("invalid SQL phone column")
    raw = f"regexp_replace(COALESCE({column}, ''), '[^0-9+]', '', 'g')"
    return (
        "CASE "
        f"WHEN {raw} LIKE '+84' || chr(37) THEN '0' || substr({raw}, 4) "
        f"WHEN {raw} LIKE '0084' || chr(37) THEN '0' || substr({raw}, 5) "
        f"WHEN {raw} LIKE '84' || chr(37) AND length({raw}) = 11 THEN '0' || substr({raw}, 3) "
        f"WHEN {raw} ~ '^[0-9]{{9}}$' THEN '0' || {raw} "
        f"ELSE {raw} END"
    )


async def ensure_phone_link_schema(db_pool=None) -> bool:
    """Create the standalone mapping table idempotently."""
    pool = db_pool or get_db_pool()
    if not pool:
        logger.info("phone_telegram_links schema skipped (no DB pool).")
        return False
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                for statement in PHONE_LINK_TABLE_STATEMENTS:
                    await cur.execute(statement)
            await conn.commit()
    except Exception as e:
        logger.warning(f"phone_telegram_links schema creation failed: {e}")
        return False
    return True


async def get_telegram_id_for_phone(phone, db_pool=None, *, submitted_at: Optional[datetime] = None) -> Optional[int]:
    """Trusted directory lookup; delivery also supplies the original record's aware timestamp."""
    normalized = normalize_vn_phone(phone)
    if not normalized or (submitted_at is not None and (
        not isinstance(submitted_at, datetime) or submitted_at.utcoffset() is None
    )):
        return None
    pool = db_pool or get_db_pool()
    if not pool:
        return None
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT telegram_user_id FROM phone_telegram_links "
                    "WHERE phone_normalized = %s AND source IN (%s, %s) "
                    "AND telegram_user_id > 0 "
                    "AND (%s::timestamptz IS NULL OR updated_at <= %s) LIMIT 1;",
                    (normalized, *VERIFIED_PHONE_LINK_SOURCES, submitted_at, submitted_at),
                )
                row = await cur.fetchone()
                if row and row[0] and int(row[0]) > 0:
                    return int(row[0])
    except Exception as e:
        logger.debug("phone_telegram_links lookup failed (%s)", type(e).__name__)
    return None


async def get_verified_phone_for_telegram_user(
    telegram_user_id, db_pool=None
) -> Optional[str]:
    """Return the latest trusted phone; history must use the binding's time window."""
    binding = await get_verified_phone_binding_for_telegram_user(telegram_user_id, db_pool=db_pool)
    return binding[0] if binding else None


async def get_verified_phone_binding_for_telegram_user(
    telegram_user_id, db_pool=None
) -> Optional[Tuple[str, datetime]]:
    """Return (phone, verified_since), using the stable trusted-binding updated_at."""
    # ponytail: legacy submissions have no immutable Telegram owner. History is
    # limited to this binding's verification period; older access needs an
    # explicit per-submission ownership contract, not phone-number possession.
    try:
        telegram_user_id = int(telegram_user_id)
    except (TypeError, ValueError, OverflowError):
        return None
    if telegram_user_id <= 0:
        return None
    pool = db_pool or get_db_pool()
    if not pool:
        return None
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT phone_normalized, updated_at FROM phone_telegram_links "
                    "WHERE telegram_user_id = %s AND telegram_user_id > 0 "
                    "AND source IN (%s, %s) "
                    "ORDER BY updated_at DESC, phone_normalized ASC LIMIT 1;",
                    (telegram_user_id, *VERIFIED_PHONE_LINK_SOURCES),
                )
                row = await cur.fetchone()
                if row and len(row) >= 2:
                    verified_since = row[1]
                    if isinstance(verified_since, datetime) and verified_since.utcoffset() is not None:
                        return str(row[0]), verified_since
    except Exception as e:
        logger.debug("phone_telegram_links reverse lookup failed (%s)", type(e).__name__)
    return None


async def upsert_phone_telegram_link(
    phone,
    telegram_user_id,
    source: str = "unknown",
    db_pool=None,
) -> bool:
    """Create/refresh a binding, but never transfer a trusted phone to another account."""
    normalized = normalize_vn_phone(phone)
    try:
        telegram_user_id = int(telegram_user_id)
    except (TypeError, ValueError, OverflowError):
        return False
    if not normalized or telegram_user_id <= 0:
        return False
    pool = db_pool or get_db_pool()
    if not pool:
        return False
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO phone_telegram_links
                        (phone_normalized, telegram_user_id, source)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (phone_normalized) DO UPDATE SET
                        telegram_user_id = EXCLUDED.telegram_user_id,
                        source = EXCLUDED.source,
                        updated_at = CASE
                            WHEN phone_telegram_links.source IN (%s, %s)
                            THEN phone_telegram_links.updated_at
                            ELSE NOW()
                        END
                    WHERE COALESCE(phone_telegram_links.source, '') <> %s
                      AND (phone_telegram_links.source IS NULL
                           OR phone_telegram_links.source NOT IN (%s, %s)
                           OR (EXCLUDED.source IN (%s, %s)
                               AND phone_telegram_links.telegram_user_id = EXCLUDED.telegram_user_id))
                    RETURNING phone_normalized;
                    """,
                    (
                        normalized,
                        telegram_user_id,
                        str(source or "unknown"),
                        *VERIFIED_PHONE_LINK_SOURCES,
                        CONFLICTED_PHONE_LINK_SOURCE,
                        *VERIFIED_PHONE_LINK_SOURCES,
                        *VERIFIED_PHONE_LINK_SOURCES,
                    ),
                )
                row = await cur.fetchone()
                if not row and source in VERIFIED_PHONE_LINK_SOURCES:
                    # Only a proved conflicting claim pauses a disputed number.
                    # Unverified command/seed writes cannot suspend its owner.
                    await cur.execute(
                        "UPDATE phone_telegram_links SET source = %s "
                        "WHERE phone_normalized = %s AND telegram_user_id <> %s "
                        "AND source IN (%s, %s);",
                        (CONFLICTED_PHONE_LINK_SOURCE, normalized, telegram_user_id, *VERIFIED_PHONE_LINK_SOURCES),
                    )
            await conn.commit()
        if not row and source in VERIFIED_PHONE_LINK_SOURCES:
            logger.warning(
                "Verified phone ownership conflict refused; the disputed binding is paused. "
                "Administrator revocation and re-verification are required; no owner was reassigned."
            )
        return bool(row)
    except Exception as e:
        logger.warning("phone_telegram_links upsert failed (%s)", type(e).__name__)
        return False


async def revoke_phone_telegram_link(phone, expected_telegram_user_id, db_pool=None) -> bool:
    """Privileged maintenance primitive; callers must authorize/audit the expected owner."""
    normalized = normalize_vn_phone(phone)
    try:
        expected_telegram_user_id = int(expected_telegram_user_id)
    except (TypeError, ValueError, OverflowError):
        return False
    if not normalized or expected_telegram_user_id <= 0:
        return False
    pool = db_pool or get_db_pool()
    if not pool:
        return False
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM phone_telegram_links WHERE phone_normalized = %s "
                    "AND telegram_user_id = %s RETURNING phone_normalized;",
                    (normalized, expected_telegram_user_id),
                )
                revoked = await cur.fetchone()
            await conn.commit()
        return bool(revoked)
    except Exception as e:
        logger.warning("phone_telegram_links revocation failed (%s)", type(e).__name__)
        return False
