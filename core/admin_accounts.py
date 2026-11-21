"""Admin account storage and the one-time environment-admin seed."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from config import settings
from core.passwords import hash_password

logger = logging.getLogger("core.admin_accounts")

TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS admin_accounts (
        username VARCHAR(64) PRIMARY KEY,
        password_hash TEXT NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
)


def _row_dict(row: Any) -> Dict[str, Any]:
    if isinstance(row, dict):
        return row
    return {"username": row[0], "password_hash": row[1], "is_active": row[2],
            "created_at": row[3], "updated_at": row[4]}


async def ensure_admin_accounts(pool) -> bool:
    if not pool:
        return False
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                for statement in TABLE_STATEMENTS:
                    await cur.execute(statement)
            await conn.commit()
    except Exception as exc:
        logger.warning("admin_accounts schema init failed: %s", type(exc).__name__)
        return False
    return True


async def seed_env_admin(pool) -> bool:
    """Create the first database account from the environment credential."""
    if not pool:
        return False
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT count(*) FROM admin_accounts")
                row = await cur.fetchone()
                count = (row[0] if not isinstance(row, dict) else next(iter(row.values()))) or 0
                if int(count) > 0:
                    return False
                encoded = str(settings.ADMIN_PASSWORD_HASH or "").strip()
                if not encoded:
                    encoded = hash_password(settings.ADMIN_PASSWORD)
                await cur.execute(
                    "INSERT INTO admin_accounts(username,password_hash) VALUES (%s,%s) "
                    "ON CONFLICT(username) DO NOTHING",
                    (settings.ADMIN_USERNAME, encoded),
                )
            await conn.commit()
    except Exception as exc:
        logger.warning("admin account seed failed: %s", type(exc).__name__)
        return False
    logger.info("[ADMIN ACCOUNTS] seeded the environment administrator account")
    return True


async def list_admin_accounts(pool) -> List[Dict[str, Any]]:
    if not pool:
        return []
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT username,password_hash,is_active,created_at,updated_at "
                    "FROM admin_accounts ORDER BY username"
                )
                rows = await cur.fetchall()
    except Exception as exc:
        logger.warning("admin account list failed: %s", type(exc).__name__)
        return []
    accounts = []
    for row in rows:
        data = _row_dict(row)
        data.pop("password_hash", None)
        accounts.append(data)
    return accounts


async def get_admin_account(pool, username: str) -> Optional[Dict[str, Any]]:
    if not pool:
        return None
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT username,password_hash,is_active,created_at,updated_at "
                    "FROM admin_accounts WHERE username=%s",
                    (username,),
                )
                row = await cur.fetchone()
    except Exception as exc:
        logger.debug("admin account lookup failed: %s", type(exc).__name__)
        return None
    return _row_dict(row) if row else None


async def create_admin_account(pool, username: str, password_hash: str) -> bool:
    if not pool:
        return False
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO admin_accounts(username,password_hash) VALUES (%s,%s) "
                    "ON CONFLICT(username) DO NOTHING RETURNING username",
                    (username, password_hash),
                )
                created = await cur.fetchone()
            await conn.commit()
    except Exception as exc:
        logger.warning("admin account create failed: %s", type(exc).__name__)
        return False
    return bool(created)


async def update_admin_password(pool, username: str, password_hash: str) -> bool:
    if not pool:
        return False
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE admin_accounts SET password_hash=%s, updated_at=NOW() "
                    "WHERE username=%s RETURNING username",
                    (password_hash, username),
                )
                updated = await cur.fetchone()
            await conn.commit()
    except Exception as exc:
        logger.warning("admin password update failed: %s", type(exc).__name__)
        return False
    return bool(updated)


async def set_admin_active(pool, username: str, active: bool) -> bool:
    if not pool:
        return False
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE admin_accounts SET is_active=%s, updated_at=NOW() "
                    "WHERE username=%s RETURNING username",
                    (bool(active), username),
                )
                updated = await cur.fetchone()
            await conn.commit()
    except Exception as exc:
        logger.warning("admin account activation update failed: %s", type(exc).__name__)
        return False
    return bool(updated)


async def count_active_admins(pool) -> int:
    if not pool:
        return 0
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT count(*) FROM admin_accounts WHERE is_active")
                row = await cur.fetchone()
    except Exception:
        return 0
    return int(row[0] if not isinstance(row, dict) else next(iter(row.values())))
