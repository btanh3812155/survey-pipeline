"""Admin account storage and the one-time environment-admin seed."""

import logging
from typing import Any, Dict, List, Optional

from psycopg.rows import dict_row

from config import settings
from core.passwords import hash_password

logger = logging.getLogger("core.admin_accounts")

ADMIN_ROLES = ("superadmin", "manager", "reviewer", "viewer")
_ACCOUNT_LOCK = "SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_admin_accounts'))"
_BIN_CUTOFF = "statement_timestamp() - INTERVAL '30 days'"


class LastActiveSuperadminError(Exception):
    """The change would leave no active superadmin."""


TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS admin_accounts (
        username VARCHAR(64) PRIMARY KEY,
        password_hash TEXT NOT NULL,
        role VARCHAR(16) NOT NULL DEFAULT 'reviewer' CHECK (role IN ('superadmin','manager','reviewer','viewer')),
        recovery_email VARCHAR(254),
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        deleted_at TIMESTAMPTZ,
        deleted_by VARCHAR(64)
    );
    """,
    "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS role VARCHAR(16) NOT NULL DEFAULT 'reviewer';",
    "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS recovery_email VARCHAR(254);",
    "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;",
    "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS deleted_by VARCHAR(64);",
    """
    CREATE TABLE IF NOT EXISTS admin_accounts_seed (
        singleton BOOLEAN PRIMARY KEY CHECK (singleton)
    );
    """,
)


async def ensure_admin_accounts(pool) -> bool:
    if not pool:
        return False
    try:
        async with pool.connection() as conn:
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute(_ACCOUNT_LOCK)
                for statement in TABLE_STATEMENTS:
                    await cur.execute(statement)
    except Exception as exc:
        logger.warning("admin_accounts schema init failed: %s", type(exc).__name__)
        return False
    return True


async def seed_env_admin(pool) -> bool:
    """Bootstrap an empty install once; never rewrite an existing account."""
    if not pool:
        return False
    try:
        async with pool.connection() as conn:
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                await cur.execute(_ACCOUNT_LOCK)
                await cur.execute(
                    "INSERT INTO admin_accounts_seed(singleton) VALUES (TRUE) "
                    "ON CONFLICT DO NOTHING RETURNING singleton"
                )
                if not await cur.fetchone():
                    return False
                await cur.execute("SELECT 1 FROM admin_accounts LIMIT 1")
                if await cur.fetchone():
                    return False
                encoded = str(settings.ADMIN_PASSWORD_HASH or "").strip()
                if not encoded:
                    encoded = hash_password(settings.ADMIN_PASSWORD)
                await cur.execute(
                    "INSERT INTO admin_accounts(username,password_hash,role,recovery_email) "
                    "VALUES (%s,%s,'superadmin',%s)",
                    (settings.ADMIN_USERNAME, encoded, settings.ADMIN_RECOVERY_EMAIL),
                )
    except Exception as exc:
        logger.warning("admin account seed failed: %s", type(exc).__name__)
        return False
    logger.info("[ADMIN ACCOUNTS] seeded the environment administrator account")
    return True


_LIST_COLUMNS = "username,role,is_active,created_at,updated_at,deleted_at,deleted_by"


async def _list_accounts(pool, *, deleted: bool) -> List[Dict[str, Any]]:
    if not pool:
        return []
    query_tail = (
        f"deleted_at > {_BIN_CUTOFF} ORDER BY deleted_at DESC"
        if deleted else "deleted_at IS NULL ORDER BY username"
    )
    try:
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(f"SELECT {_LIST_COLUMNS} FROM admin_accounts WHERE {query_tail}")
                return await cur.fetchall()
    except Exception as exc:
        logger.warning("admin account list failed: %s", type(exc).__name__)
        return []


async def list_admin_accounts(pool) -> List[Dict[str, Any]]:
    return await _list_accounts(pool, deleted=False)


async def list_deleted_admin_accounts(pool) -> List[Dict[str, Any]]:
    return await _list_accounts(pool, deleted=True)


async def get_admin_account(pool, username: str) -> Optional[Dict[str, Any]]:
    if not pool:
        return None
    try:
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"SELECT {_LIST_COLUMNS},password_hash,recovery_email FROM admin_accounts WHERE username=%s",
                    (username,),
                )
                return await cur.fetchone()
    except Exception as exc:
        logger.debug("admin account lookup failed: %s", type(exc).__name__)
        return None


async def create_admin_account(
    pool, username: str, password_hash: str, role: str,
    recovery_email: Optional[str] = None,
) -> bool:
    if not pool or role not in ADMIN_ROLES:
        return False
    if role == "superadmin" and not recovery_email:
        recovery_email = settings.ADMIN_RECOVERY_EMAIL
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO admin_accounts(username,password_hash,role,recovery_email) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT(username) DO NOTHING RETURNING username",
                    (username, password_hash, role, recovery_email),
                )
                created = await cur.fetchone()
            await conn.commit()
    except Exception as exc:
        logger.warning("admin account create failed: %s", type(exc).__name__)
        return False
    return bool(created)


async def update_admin_password(
    pool, username: str, password_hash: str, *, expected_hash: str
) -> bool:
    if not pool:
        return False
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE admin_accounts SET password_hash=%s, updated_at=NOW() "
                    "WHERE username=%s AND is_active AND deleted_at IS NULL AND password_hash=%s RETURNING username",
                    (password_hash, username, expected_hash),
                )
                updated = await cur.fetchone()
            await conn.commit()
    except Exception as exc:
        logger.warning("admin password update failed: %s", type(exc).__name__)
        return False
    return bool(updated)


async def _change_account(
    pool, username: str, assignments: str, values: tuple = (), *,
    removes_superadmin: bool = False, restoring: bool = False,
) -> bool:
    if not pool:
        return False
    eligible = f"deleted_at > {_BIN_CUTOFF}" if restoring else "deleted_at IS NULL"
    guard = ""
    if removes_superadmin:
        guard = (
            " AND (target.role <> 'superadmin' OR NOT target.is_active OR EXISTS ("
            "SELECT 1 FROM admin_accounts AS other WHERE other.username <> target.username "
            "AND other.role='superadmin' AND other.is_active AND other.deleted_at IS NULL))"
        )
    try:
        async with pool.connection() as conn:
            async with conn.transaction(), conn.cursor() as cur:
                # The UPDATE must take a fresh snapshot after acquiring the shared lock.
                await cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                await cur.execute(_ACCOUNT_LOCK)
                await cur.execute(
                    f"UPDATE admin_accounts AS target SET {assignments}, updated_at=NOW() "
                    f"WHERE username=%s AND {eligible}{guard} RETURNING username",
                    (*values, username),
                )
                if await cur.fetchone():
                    return True
                if removes_superadmin:
                    await cur.execute(
                        "SELECT 1 FROM admin_accounts WHERE username=%s AND deleted_at IS NULL", (username,)
                    )
                    if await cur.fetchone():
                        raise LastActiveSuperadminError
    except LastActiveSuperadminError:
        raise
    except Exception as exc:
        logger.warning("admin account change failed: %s", type(exc).__name__)
    return False


async def update_admin_role(pool, username: str, role: str) -> bool:
    if not pool or role not in ADMIN_ROLES:
        return False
    return await _change_account(
        pool, username,
        "role=%s, recovery_email=CASE WHEN %s='superadmin' AND NULLIF(btrim(recovery_email),'') IS NULL THEN %s ELSE recovery_email END",
        (role, role, settings.ADMIN_RECOVERY_EMAIL),
        removes_superadmin=role != "superadmin",
    )




async def set_admin_active(pool, username: str, active: bool) -> bool:
    return await _change_account(
        pool, username, "is_active=%s", (active,), removes_superadmin=not active
    )


async def soft_delete_admin_account(pool, username: str, deleted_by: str) -> bool:
    if username == deleted_by:
        return False
    return await _change_account(
        pool, username, "is_active=FALSE, deleted_at=statement_timestamp(), deleted_by=%s",
        (deleted_by,), removes_superadmin=True,
    )


async def restore_admin_account(pool, username: str) -> bool:
    return await _change_account(
        pool, username, "is_active=TRUE, deleted_at=NULL, deleted_by=NULL", restoring=True
    )


async def count_active_superadmins(pool) -> int:
    if not pool:
        return 0
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT count(*) FROM admin_accounts WHERE is_active AND deleted_at IS NULL AND role='superadmin'")
                row = await cur.fetchone()
    except Exception:
        return 0
    return int(row[0] if not isinstance(row, dict) else next(iter(row.values())))


async def purge_expired_admin_accounts(pool) -> int:
    """Purge bin entries only after their full 30-day recovery window."""
    if not pool:
        return 0
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "WITH expired AS ("
                    f" DELETE FROM admin_accounts WHERE deleted_at <= {_BIN_CUTOFF} "
                    " RETURNING username,deleted_at,deleted_by"
                    ") INSERT INTO system_audit_log(component_name,action,entity_type,entity_id,actor_type,actor_id,details) "
                    "SELECT 'admin_accounts','ADMIN_ACCOUNT_PURGED','ADMIN',username,'SYSTEM','scheduler',"
                    "jsonb_build_object('deleted_at',deleted_at,'deleted_by',deleted_by) FROM expired "
                    "RETURNING entity_id"
                )
                rows = await cur.fetchall()
            await conn.commit()
    except Exception as exc:
        logger.warning("expired admin account purge failed: %s", type(exc).__name__)
        return 0
    return len(rows)
