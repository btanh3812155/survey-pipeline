"""Persistent CTV bot invitations; contact verification is the binding boundary."""

import re
import json
import secrets

from config import settings
from core.phone_links import (
    CONFLICTED_PHONE_LINK_SOURCE, normalize_vn_phone, phone_normalization_sql,
    store_phone_telegram_link,
)

CTV_ONBOARDING_STATEMENTS = (
    """
    DO $$
    BEGIN
        IF to_regclass(format('%I.authorized_users', current_schema())) IS NOT NULL THEN
            ALTER TABLE authorized_users ADD COLUMN IF NOT EXISTS roles TEXT[];
            UPDATE authorized_users SET roles = ARRAY[role]::text[] WHERE roles IS NULL OR cardinality(roles) = 0;
            ALTER TABLE authorized_users ALTER COLUMN roles SET NOT NULL;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'authorized_users_roles_check'
                  AND conrelid = format('%I.authorized_users', current_schema())::regclass
            ) THEN
                ALTER TABLE authorized_users ADD CONSTRAINT authorized_users_roles_check
                CHECK (cardinality(roles) >= 1 AND roles <@ ARRAY['COLLABORATOR', 'DRIVER', 'ADMIN']::text[]);
            END IF;
        END IF;
    END
    $$;
    """,
    """
    CREATE OR REPLACE FUNCTION sync_authorized_user_roles() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE normalized TEXT[];
    BEGIN
        IF TG_OP = 'UPDATE' AND NEW.role IS DISTINCT FROM OLD.role AND NEW.roles IS NOT DISTINCT FROM OLD.roles THEN
            NEW.roles := ARRAY[NEW.role]::text[];
        ELSIF TG_OP = 'INSERT' AND NEW.roles IS NULL THEN
            NEW.roles := ARRAY[NEW.role]::text[];
        ELSE
            SELECT COALESCE(
                array_agg(r ORDER BY array_position(ARRAY['ADMIN', 'COLLABORATOR', 'DRIVER'], r), r),
                '{}'::text[]
            ) INTO normalized
            FROM (SELECT DISTINCT unnest(NEW.roles) AS r) s;
            NEW.roles := normalized;
            IF cardinality(NEW.roles) >= 1 AND NEW.roles[1] = ANY(ARRAY['ADMIN', 'COLLABORATOR', 'DRIVER']) THEN
                NEW.role := NEW.roles[1];
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$;
    """,
    """
    DO $$
    BEGIN
        IF to_regclass(format('%I.authorized_users', current_schema())) IS NOT NULL THEN
            CREATE OR REPLACE TRIGGER authorized_users_sync_roles
            BEFORE INSERT OR UPDATE ON authorized_users
            FOR EACH ROW EXECUTE FUNCTION sync_authorized_user_roles();
        END IF;
    END
    $$;
    """,
    "ALTER TABLE ctv_directory ADD COLUMN IF NOT EXISTS bot_invite_token TEXT;",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ctv_bot_invite_token ON ctv_directory (bot_invite_token);",
    """
    CREATE TABLE IF NOT EXISTS ctv_bot_pending (
        telegram_user_id BIGINT PRIMARY KEY,
        ctv_code TEXT NOT NULL REFERENCES ctv_directory(code) ON DELETE CASCADE,
        token TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
)


async def ensure_ctv_bot_links(db_pool) -> dict[str, str]:
    """Keep each active CTV's opaque invitation stable across sheet refreshes."""
    username = settings.TELEGRAM_DRIVER_BOT_USERNAME.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]+", username):
        raise ValueError("invalid CTV bot username")
    links = {}
    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT code, bot_invite_token FROM ctv_directory WHERE is_active = TRUE ORDER BY code"
            )
            for code, token in await cur.fetchall():
                if not token:
                    await cur.execute(
                        "UPDATE ctv_directory SET bot_invite_token = COALESCE(bot_invite_token, %s) "
                        "WHERE code = %s RETURNING bot_invite_token",
                        (secrets.token_urlsafe(24), code),
                    )
                    token = (await cur.fetchone())[0]
                links[code] = f"https://t.me/{username}?start=ctv_{token}"
    return links


async def _lock_user(cur, telegram_user_id):
    await cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"ctv_onboarding:{telegram_user_id}",),
    )


async def _eligible_ctv(cur, telegram_user_id, token):
    await cur.execute(
        "SELECT code FROM ctv_directory WHERE bot_invite_token = %s AND is_active = TRUE FOR UPDATE",
        (token,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    code = row[0]
    await cur.execute(
        "SELECT code FROM ctv_directory WHERE telegram_user_id = %s AND code <> %s LIMIT 1",
        (telegram_user_id, code),
    )
    return None if await cur.fetchone() else code


async def begin_ctv_onboarding(db_pool, telegram_user_id: int, payload: str) -> str:
    """Remember invitation intent, never grant CTV identity on /start alone."""
    if not db_pool or type(telegram_user_id) is not int or telegram_user_id <= 0:
        raise ValueError("CTV onboarding unavailable")
    match = re.fullmatch(r"ctv_([A-Za-z0-9_-]{32})", payload)
    code = None
    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await _lock_user(cur, telegram_user_id)
            await cur.execute("LOCK TABLE ctv_directory IN SHARE ROW EXCLUSIVE MODE")
            # A bad replacement link must not leave an older invitation armed.
            await cur.execute("DELETE FROM ctv_bot_pending WHERE telegram_user_id = %s", (telegram_user_id,))
            if match:
                token = match[1]
                code = await _eligible_ctv(cur, telegram_user_id, token)
                if code:
                    await cur.execute(
                        "INSERT INTO ctv_bot_pending (telegram_user_id, ctv_code, token) VALUES (%s, %s, %s)",
                        (telegram_user_id, code, token),
                    )
    if not code:
        raise ValueError("invalid or unavailable CTV invitation")
    return code


async def complete_ctv_onboarding(db_pool, telegram_user_id: int, phone: str, full_name: str) -> str | None:
    """Bind only a verified own-contact; registered CTV phones are admin-managed."""
    if not db_pool:
        return None
    normalized = normalize_vn_phone(phone)
    if not normalized or type(telegram_user_id) is not int or telegram_user_id <= 0:
        raise ValueError("invalid verified contact")
    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await _lock_user(cur, telegram_user_id)
            # ponytail: serialize rare CTV bindings against directory edits/inserts;
            # a normalized unique phone constraint can replace this table lock at scale.
            await cur.execute("LOCK TABLE ctv_directory IN SHARE ROW EXCLUSIVE MODE")
            await cur.execute(
                "SELECT ctv_code, token FROM ctv_bot_pending WHERE telegram_user_id = %s FOR UPDATE",
                (telegram_user_id,),
            )
            pending = await cur.fetchone()
            await cur.execute(
                f"SELECT code, phone, telegram_user_id, is_active, bot_invite_token, auth_revision FROM ctv_directory "
                f"WHERE ({phone_normalization_sql('phone')}) = %s OR telegram_user_id = %s OR code = %s "
                "ORDER BY code FOR UPDATE",
                (normalized, telegram_user_id, pending[0] if pending else None),
            )
            rows = await cur.fetchall()
            matches = [row for row in rows if normalize_vn_phone(row[1]) == normalized]
            if len(matches) > 1:
                raise ValueError("ambiguous registered CTV phone")
            if pending:
                target = next((row for row in rows if row[0] == pending[0]), None)
                if not target or target[4] != pending[1] or not target[3]:
                    raise ValueError("CTV invitation no longer available")
                if target[1] and normalize_vn_phone(target[1]) != normalized:
                    raise ValueError("registered CTV phone changes require administrator support")
                if matches and matches[0][0] != target[0]:
                    raise ValueError("verified phone belongs to another CTV")
            elif matches:
                target = matches[0]
            else:
                if rows:
                    raise ValueError("registered CTV phone changes require administrator support")
                return None
            code, registered_phone, old_user_id, active, _, old_revision = target
            if not active or any(row[2] == telegram_user_id and row[0] != code for row in rows):
                raise ValueError("CTV identity unavailable")
            replacing = old_user_id is not None and old_user_id != telegram_user_id
            if replacing and not registered_phone:
                raise ValueError("CTV account replacement requires a registered phone")
            await cur.execute(
                "SELECT roles, is_active FROM authorized_users WHERE telegram_user_id = %s FOR UPDATE",
                (telegram_user_id,),
            )
            incoming_user = await cur.fetchone()
            if incoming_user and not incoming_user[1]:
                raise ValueError("Telegram account is administratively inactive")
            await cur.execute(
                "SELECT telegram_user_id, source FROM phone_telegram_links WHERE phone_normalized = %s FOR UPDATE",
                (normalized,),
            )
            binding = await cur.fetchone()
            if binding and (
                binding[1] == CONFLICTED_PHONE_LINK_SOURCE
                or binding[0] not in (telegram_user_id, old_user_id)
            ):
                raise ValueError("verified phone ownership conflict")
            if binding and replacing and binding[0] == old_user_id:
                # A new ownership epoch never grants the new account older driver history.
                await cur.execute(
                    "UPDATE phone_telegram_links SET telegram_user_id = %s, source = 'verified_contact', "
                    "updated_at = clock_timestamp() WHERE phone_normalized = %s",
                    (telegram_user_id, normalized),
                )
            elif not await store_phone_telegram_link(cur, normalized, telegram_user_id, "verified_contact"):
                raise ValueError("verified phone ownership conflict")
            await cur.execute(
                "INSERT INTO authorized_users (telegram_user_id, username, full_name, role, roles, is_active) "
                "VALUES (%s, %s, %s, 'COLLABORATOR', ARRAY['COLLABORATOR']::text[], TRUE) "
                "ON CONFLICT (telegram_user_id) DO UPDATE SET "
                "username = CASE WHEN 'ADMIN' = ANY(authorized_users.roles) THEN authorized_users.username ELSE EXCLUDED.username END, "
                "full_name = CASE WHEN 'ADMIN' = ANY(authorized_users.roles) THEN authorized_users.full_name ELSE EXCLUDED.full_name END, "
                "roles = CASE "
                "WHEN 'COLLABORATOR' = ANY(authorized_users.roles) THEN authorized_users.roles "
                "ELSE array_append(authorized_users.roles, 'COLLABORATOR') END "
                "WHERE authorized_users.is_active RETURNING 1",
                (telegram_user_id, code, full_name),
            )
            if await cur.fetchone() is None:
                raise ValueError("Telegram account is administratively inactive")
            if replacing:
                await cur.execute(
                    "UPDATE authorized_users "
                    "SET is_active = CASE WHEN array_remove(roles, 'COLLABORATOR') = '{}'::text[] THEN FALSE ELSE is_active END, "
                    "roles = CASE WHEN array_remove(roles, 'COLLABORATOR') = '{}'::text[] THEN roles ELSE array_remove(roles, 'COLLABORATOR') END, "
                    "username = CASE WHEN array_remove(roles, 'COLLABORATOR') <> '{}'::text[] "
                    "AND UPPER(TRIM(COALESCE(username, ''))) = UPPER(%s) THEN NULL ELSE username END "
                    "WHERE telegram_user_id = %s AND 'COLLABORATOR' = ANY(roles)",
                    (code, old_user_id),
                )
            await cur.execute(
                "UPDATE ctv_directory SET telegram_user_id = %s, phone = COALESCE(NULLIF(phone, ''), %s), "
                "updated_at = NOW() WHERE code = %s RETURNING auth_revision",
                (telegram_user_id, normalized, code),
            )
            new_revision = (await cur.fetchone())[0]
            if replacing:
                await cur.execute(
                    "INSERT INTO system_audit_log(component_name,action,entity_type,entity_id,actor_type,actor_id,details,event_timestamp) "
                    "VALUES ('ctv_onboarding','CTV_TELEGRAM_REPLACED','CTV',%s,'DRIVER',%s,%s::jsonb,clock_timestamp())",
                    (code, str(telegram_user_id), json.dumps({
                        "registered_phone_verified": True,
                        "previous_telegram_user_id": old_user_id,
                        "telegram_user_id": telegram_user_id,
                        "previous_auth_revision": str(old_revision),
                        "auth_revision": str(new_revision),
                    })),
                )
            await cur.execute(
                "DELETE FROM ctv_bot_pending WHERE telegram_user_id = %s OR telegram_user_id = %s",
                (telegram_user_id, old_user_id),
            )
    from core.ctv import refresh_ctv_directory_cache

    await refresh_ctv_directory_cache(db_pool)
    return code


async def billing_collaborator_is_eligible(conn, telegram_user_id: int, *, ctv_code=None, submitted_at=None) -> bool:
    """Retired credentials retain payee eligibility only through verified rebinds."""
    cur = await conn.execute(
        """WITH RECURSIVE edges AS (
            SELECT entity_id, event_timestamp, details->>'previous_telegram_user_id' AS old_id,
                details->>'telegram_user_id' AS new_id,
                details->>'previous_auth_revision' AS old_revision,
                details->>'auth_revision' AS new_revision
            FROM system_audit_log
            WHERE component_name = 'ctv_onboarding' AND action = 'CTV_TELEGRAM_REPLACED'
                AND entity_type = 'CTV' AND details->'registered_phone_verified' = 'true'::jsonb
                AND jsonb_typeof(details->'previous_telegram_user_id') = 'number'
                AND jsonb_typeof(details->'telegram_user_id') = 'number'
                AND details->>'previous_telegram_user_id' ~ '^[1-9][0-9]*$'
                AND details->>'telegram_user_id' ~ '^[1-9][0-9]*$'
                AND details->>'previous_auth_revision' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
                AND details->>'auth_revision' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
                AND details->>'previous_telegram_user_id' <> details->>'telegram_user_id'
        ), replacements AS (
            SELECT entity_id, new_id, new_revision, event_timestamp, ARRAY[old_revision, new_revision] AS path,
                old_revision = new_revision AS cycle
            FROM edges WHERE old_id = %s AND entity_id = %s AND event_timestamp >= %s::timestamptz
            UNION ALL
            SELECT e.entity_id, e.new_id, e.new_revision, e.event_timestamp, r.path || e.new_revision,
                e.new_revision = ANY(r.path)
            FROM edges e JOIN replacements r ON e.entity_id = r.entity_id
                AND e.old_revision = r.new_revision AND e.old_id = r.new_id
            WHERE NOT r.cycle AND e.event_timestamp >= r.event_timestamp
        )
        SELECT 1 FROM authorized_users u
        WHERE u.telegram_user_id = %s
            AND (
                (u.is_active AND 'COLLABORATOR' = ANY(u.roles))
                OR (NOT EXISTS (SELECT 1 FROM replacements WHERE cycle) AND EXISTS (
                    SELECT 1 FROM replacements r JOIN ctv_directory d
                        ON d.code = r.entity_id AND d.auth_revision::text = r.new_revision
                        AND d.telegram_user_id::text = r.new_id
                    JOIN authorized_users current_owner ON current_owner.telegram_user_id = d.telegram_user_id
                        AND current_owner.is_active AND 'COLLABORATOR' = ANY(current_owner.roles)
                    WHERE d.is_active = TRUE AND d.created_at <= %s::timestamptz
                ))
            )""",
        (str(telegram_user_id), ctv_code, submitted_at, telegram_user_id, submitted_at),
    )
    return bool(await cur.fetchone())
