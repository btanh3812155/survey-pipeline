# -*- coding: utf-8 -*-
"""Standalone phone <-> Telegram chat mapping.

This table is deliberately independent of ``authorized_users`` and
``ctv_directory``: a phone answer in a question-source form can resolve to a
Telegram chat without requiring a driver record, CTV code, or role.
"""

import logging
import re
from typing import Optional

from core.state import get_db_pool

logger = logging.getLogger("core.phone_links")

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


async def get_telegram_id_for_phone(phone, db_pool=None) -> Optional[int]:
    """Look up a Telegram chat id by phone. No driver/CTV tables are consulted."""
    normalized = normalize_vn_phone(phone)
    if not normalized:
        return None
    pool = db_pool or get_db_pool()
    if not pool:
        return None
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT telegram_user_id FROM phone_telegram_links "
                    "WHERE phone_normalized = %s LIMIT 1;",
                    (normalized,),
                )
                row = await cur.fetchone()
                if row and row[0]:
                    return int(row[0])
    except Exception as e:
        logger.debug(f"phone_telegram_links lookup failed for {normalized}: {e}")
    return None


async def upsert_phone_telegram_link(
    phone,
    telegram_user_id,
    source: str = "unknown",
    db_pool=None,
) -> bool:
    """Upsert a phone -> Telegram chat link. Independent of driver/CTV rows."""
    normalized = normalize_vn_phone(phone)
    if not normalized or not telegram_user_id:
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
                        updated_at = NOW();
                    """,
                    (normalized, int(telegram_user_id), str(source or "unknown")),
                )
            await conn.commit()
        return True
    except Exception as e:
        logger.warning(f"phone_telegram_links upsert failed for {normalized}: {e}")
        return False


async def delete_phone_telegram_link(phone, db_pool=None) -> bool:
    """Remove one phone -> Telegram link."""
    normalized = normalize_vn_phone(phone)
    if not normalized:
        return False
    pool = db_pool or get_db_pool()
    if not pool:
        return False
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM phone_telegram_links WHERE phone_normalized = %s;",
                    (normalized,),
                )
            await conn.commit()
        return True
    except Exception as e:
        logger.warning(f"phone_telegram_links delete failed for {normalized}: {e}")
        return False
