"""Admin account storage, activation, and database-backed Basic auth."""
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials
from psycopg_pool import AsyncConnectionPool

import core.state as state
from core.admin_accounts import (
    count_active_admins,
    create_admin_account,
    ensure_admin_accounts,
    get_admin_account,
    set_admin_active,
    update_admin_password,
)
from core.passwords import hash_password, verify_password
from tests.test_outbox_delivery import database

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_lifecycle_and_database_auth(database, monkeypatch):
    dsn, conn, _submission, _event, _read, _users = database
    await ensure_admin_accounts(None)  # no-op safety
    try:
        async with AsyncConnectionPool(dsn, open=False, min_size=1, max_size=2) as pool:
            await ensure_admin_accounts(pool)
            assert await create_admin_account(
                pool, "ops", hash_password("ops-password-123", iterations=1_000)
            )
            account = await get_admin_account(pool, "ops")
            assert verify_password("ops-password-123", account["password_hash"])

            monkeypatch.setattr(state, "get_db_pool", lambda: pool)
            assert await state.verify_admin_auth(
                HTTPBasicCredentials(username="ops", password="ops-password-123")
            ) == "ops"
            with pytest.raises(HTTPException):
                await state.verify_admin_auth(
                    HTTPBasicCredentials(username="ops", password="wrong")
                )

            assert await update_admin_password(
                pool, "ops", hash_password("new-password-123", iterations=1_000)
            )
            with pytest.raises(HTTPException):
                await state.verify_admin_auth(
                    HTTPBasicCredentials(username="ops", password="ops-password-123")
                )
            assert await state.verify_admin_auth(
                HTTPBasicCredentials(username="ops", password="new-password-123")
            ) == "ops"

            assert await count_active_admins(pool) == 1
            assert await set_admin_active(pool, "ops", False)
            with pytest.raises(HTTPException):
                await state.verify_admin_auth(
                    HTTPBasicCredentials(username="ops", password="new-password-123")
                )
            assert await count_active_admins(pool) == 0
    finally:
        conn.execute("DELETE FROM admin_accounts WHERE username='ops'")
