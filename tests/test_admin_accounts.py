"""Staff role, account lifecycle, and database-backed Basic auth contracts."""
import asyncio
from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote
from uuid import uuid4
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from fastapi.security import HTTPBasicCredentials
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool

import core.state as state
from core.admin_accounts import (
    ADMIN_ROLES,
    LastActiveSuperadminError,
    count_active_superadmins,
    create_admin_account,
    ensure_admin_accounts,
    get_admin_account,
    list_deleted_admin_accounts,
    restore_admin_account,
    seed_env_admin,
    set_admin_active,
    soft_delete_admin_account,
    update_admin_role,
)
from core.passwords import hash_password, verify_password
from routers import admin_accounts as routes
from tests.test_outbox_delivery import database

ORIGIN = "https://admin.test"
ACCOUNTS = "/admin/accounts"
ACCOUNT_PAGE = "/admin/account"
ACCOUNT_PASSWORD_PAGE = f"{ACCOUNT_PAGE}/password"
PASSWORD_PAGE = f"{ACCOUNTS}/password"
TEST_PASSWORD = "fixture-password"
SAME_ORIGIN = {"Origin": ORIGIN}
postgres = pytest.mark.postgres
async_test = pytest.mark.asyncio


def _fixture_hash(password=TEST_PASSWORD):
    return hash_password(password, iterations=1_000)


@pytest_asyncio.fixture
async def accounts_db(database, monkeypatch):
    dsn, conn, *_ = database
    schema = "accounts_test_" + uuid4().hex
    conn.execute(f"CREATE SCHEMA {schema}")
    try:
        conn.execute(f"CREATE TABLE {schema}.system_audit_log (LIKE public.system_audit_log INCLUDING ALL)")
        dsn = make_conninfo(dsn, options=f"-c search_path={schema},public")
        async with AsyncConnectionPool(dsn, open=False, min_size=2, max_size=3) as pool:
            assert await ensure_admin_accounts(pool)
            monkeypatch.setattr(state, "get_db_pool", lambda: pool)
            monkeypatch.setattr(routes, "get_db_pool", lambda: pool)
            redis = SimpleNamespace(eval=AsyncMock(return_value=0))
            monkeypatch.setattr(state, "get_redis_client", lambda: redis)
            monkeypatch.setattr(routes, "hash_password", _fixture_hash)
            yield pool
    finally:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


def _client():
    app = FastAPI()
    app.include_router(routes.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)


async def _authenticate(username, password=TEST_PASSWORD):
    request = Request({
        "type": "http", "method": "GET", "scheme": "https", "path": PASSWORD_PAGE,
        "headers": [(b"host", b"admin.test")],
    })
    result = await state.verify_admin_auth(request, HTTPBasicCredentials(username=username, password=password))
    return result, request.state.admin_role


@postgres
@async_test
async def test_database_auth_without_environment_fallback(accounts_db, monkeypatch):
    pool = accounts_db
    monkeypatch.setattr(state.settings, "ADMIN_USERNAME", "env-only")
    monkeypatch.setattr(state.settings, "ADMIN_PASSWORD_HASH", _fixture_hash())
    with pytest.raises(HTTPException) as denied:
        await _authenticate("env-only")
    assert denied.value.status_code == 401

    assert await create_admin_account(pool, "ops", _fixture_hash(), "reviewer")
    assert await _authenticate("ops") == ("ops", "reviewer")
    with pytest.raises(HTTPException) as denied:
        await _authenticate("ops", "wrong")
    assert denied.value.status_code == 401
    assert await set_admin_active(pool, "ops", False)
    with pytest.raises(HTTPException) as denied:
        await _authenticate("ops")
    assert denied.value.status_code == 401

    monkeypatch.setattr(state, "get_db_pool", lambda: None)
    with pytest.raises(HTTPException) as unavailable:
        await _authenticate("env-only")
    assert unavailable.value.status_code == 503


@postgres
@async_test
async def test_seed_once_even_if_config_changes_or_accounts_disappear(accounts_db, monkeypatch):
    pool = accounts_db
    monkeypatch.setattr(state.settings, "ADMIN_USERNAME", "seeded")
    monkeypatch.setattr(state.settings, "ADMIN_PASSWORD_HASH", _fixture_hash())
    assert sorted(await asyncio.gather(seed_env_admin(pool), seed_env_admin(pool))) == [False, True]
    assert await _authenticate("seeded") == ("seeded", "superadmin")

    assert await create_admin_account(pool, "replacement", _fixture_hash(), "superadmin")
    assert await update_admin_role(pool, "seeded", "viewer")
    assert not await seed_env_admin(pool)
    assert await _authenticate("seeded") == ("seeded", "viewer")

    async with pool.connection() as conn:
        await conn.execute("DELETE FROM admin_accounts")
    monkeypatch.setattr(state.settings, "ADMIN_USERNAME", "new-env-admin")
    assert not await seed_env_admin(pool)
    assert await get_admin_account(pool, "new-env-admin") is None


@postgres
@async_test
async def test_seed_does_not_elevate_an_existing_account(accounts_db, monkeypatch):
    pool = accounts_db
    monkeypatch.setattr(state.settings, "ADMIN_USERNAME", "existing")
    monkeypatch.setattr(state.settings, "ADMIN_PASSWORD_HASH", _fixture_hash("replacement-password"))
    assert await create_admin_account(pool, "existing", _fixture_hash(), "manager")
    assert not await seed_env_admin(pool)
    assert await _authenticate("existing") == ("existing", "manager")


@postgres
@async_test
async def test_last_superadmin_held_under_concurrent_changes(accounts_db):
    pool = accounts_db
    assert await create_admin_account(pool, "admin-a", _fixture_hash(), "superadmin")
    assert await create_admin_account(pool, "admin-b", _fixture_hash(), "superadmin")
    assert await create_admin_account(pool, "manager-a", _fixture_hash(), "manager")
    results = await asyncio.gather(
        set_admin_active(pool, "admin-a", False),
        update_admin_role(pool, "admin-b", "viewer"),
        return_exceptions=True,
    )
    assert sum(result is True for result in results) == 1
    assert sum(isinstance(result, LastActiveSuperadminError) for result in results) == 1
    assert await count_active_superadmins(pool) == 1
    remaining = "admin-a" if results[1] is True else "admin-b"
    with pytest.raises(LastActiveSuperadminError):
        await set_admin_active(pool, remaining, False)
    with pytest.raises(LastActiveSuperadminError):
        await update_admin_role(pool, remaining, "manager")
    with pytest.raises(LastActiveSuperadminError):
        await soft_delete_admin_account(pool, remaining, "manager-a")
    assert await count_active_superadmins(pool) == 1


@postgres
@async_test
async def test_staff_routes_require_superadmin_except_own_password(accounts_db):
    pool = accounts_db
    async with _client() as client:
        for role in ADMIN_ROLES:
            assert await create_admin_account(pool, role, _fixture_hash(), role)
            auth = (role, TEST_PASSWORD)
            page = await client.get(ACCOUNT_PAGE, auth=auth)
            assert page.status_code == 200
            assert 'href="/admin/recovery"' not in page.text
            assert 'name="current_password"' in page.text
            legacy = await client.get(PASSWORD_PAGE, auth=auth)
            assert legacy.status_code == 303
            assert legacy.headers["location"] == ACCOUNT_PAGE
            staff = await client.get(ACCOUNTS, auth=auth)
            assert staff.status_code == (200 if role == "superadmin" else 403)
            if role == "superadmin":
                assert 'href="/admin/recovery/email"' not in staff.text
            else:
                form = {"username": "denied", "password": TEST_PASSWORD, "confirm_password": TEST_PASSWORD, "role": role}
                denied = await client.post(ACCOUNTS, headers=SAME_ORIGIN, auth=auth, data=form)
                assert denied.status_code == 403
                for operation in ("role", "activate", "deactivate", "delete", "restore"):
                    response = await client.post(
                        f"{ACCOUNTS}/superadmin/{operation}", headers=SAME_ORIGIN, auth=auth, data={"role": role}
                    )
                    assert response.status_code == 403
                section = await client.post(
                    f"{ACCOUNTS}/role", headers=SAME_ORIGIN, auth=auth,
                    data={"username": "superadmin", "role": role},
                )
                assert section.status_code == 403

        auth = ("viewer", TEST_PASSWORD)
        change = {"current_password": TEST_PASSWORD, "new_password": "x" * 1024, "confirm_password": "x" * 1024}
        csrf_change = dict(change, current_password="wrong-password")
        for headers in ({}, {"Origin": "https://admin.test"}):
            response = await client.post(ACCOUNT_PASSWORD_PAGE, auth=auth, data=csrf_change, headers=headers)
            assert response.status_code == 303, (response.status_code, response.text[:300])
        response = await client.post(
            ACCOUNT_PASSWORD_PAGE, auth=auth, data=csrf_change,
            headers={"Origin": "https://other.test"},
        )
        assert response.status_code == 403
        for length in (11, 1025):
            invalid = dict(change, new_password="x" * length, confirm_password="x" * length)
            response = await client.post(ACCOUNT_PASSWORD_PAGE, auth=auth, data=invalid, headers=SAME_ORIGIN)
            assert response.status_code == 303
            assert response.headers["location"] == ACCOUNT_PAGE + "?err=password_policy"
        wrong = dict(change, current_password="wrong")
        response = await client.post(ACCOUNT_PASSWORD_PAGE, auth=auth, data=wrong, headers=SAME_ORIGIN)
        assert response.headers["location"] == ACCOUNT_PAGE + "?err=current_password"
        changed = await client.post(ACCOUNT_PASSWORD_PAGE, auth=auth, data=change, headers=SAME_ORIGIN)
        assert changed.status_code == 200
        assert (await client.get(ACCOUNT_PAGE, auth=auth)).status_code == 401
        assert await _authenticate("viewer", change["new_password"]) == ("viewer", "viewer")
        assert await _authenticate("superadmin") == ("superadmin", "superadmin")


@postgres
@async_test
async def test_own_account_recovery_block_is_superadmin_only_and_hardened(accounts_db):
    pool = accounts_db
    assert await create_admin_account(pool, "owner", _fixture_hash(), "superadmin")
    assert await create_admin_account(pool, "manager", _fixture_hash(), "manager")
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE admin_accounts SET recovery_email=%s WHERE username='owner'",
            ("owner@example.test",),
        )
        await conn.commit()

    async with _client() as client:
        owner = await client.get(ACCOUNT_PAGE, auth=("owner", TEST_PASSWORD))
        assert owner.status_code == 200
        assert "owner@example.test" in owner.text
        assert "/admin/recovery/email" in owner.text
        assert owner.headers["cache-control"] == "no-store"
        assert owner.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in owner.headers["content-security-policy"]

        manager = await client.get(ACCOUNT_PAGE, auth=("manager", TEST_PASSWORD))
        assert manager.status_code == 200
        assert "owner@example.test" not in manager.text
        assert "/admin/recovery/email" not in manager.text


@postgres
@async_test
async def test_staff_management_and_bin(accounts_db):
    pool = accounts_db
    assert await create_admin_account(pool, "owner", _fixture_hash(), "superadmin")
    auth = ("owner", TEST_PASSWORD)
    password = "x" * 12
    form = {"username": "member", "password": password, "confirm_password": password}
    async with _client() as client:
        async def post(suffix="", data=None):
            return await client.post(ACCOUNTS + suffix, auth=auth, data=data, headers=SAME_ORIGIN)

        missing_role = await post(data=form)
        assert missing_role.status_code == 422
        for length in (11, 1025):
            invalid = dict(form, role="manager", password="x" * length, confirm_password="x" * length)
            assert (await post(data=invalid)).headers["location"].endswith("?err=password_policy")
        assert (await post(data=dict(form, role="admin"))).headers["location"].endswith("?err=invalid_role")
        wrong_origin = await client.post(
            ACCOUNTS, auth=auth, data=dict(form, role="manager"),
            headers={"Origin": "https://other.test"},
        )
        assert wrong_origin.status_code == 403
        assert (await post(data=dict(form, role="manager"))).headers["location"].endswith("?msg=account_created")
        account = await get_admin_account(pool, "member")
        assert account["role"] == "manager"
        assert verify_password(password, account["password_hash"])

        for operation in ("deactivate", "delete", "role"):
            denied = await post(f"/owner/{operation}", {"role": "viewer"})
            assert denied.headers["location"].endswith("?err=self_change")
        denied_section = await post("/role", {"username": "owner", "role": "viewer"})
        assert denied_section.headers["location"].endswith("?err=self_change")
        role_section = await post("/role", {"username": "member", "role": "superadmin"})
        assert role_section.status_code == 303
        assert role_section.headers["location"].endswith("?msg=account_role_changed")
        assert await _authenticate("member", password) == ("member", "superadmin")
        assert (await post("/member/deactivate")).headers["location"].endswith("?msg=account_deactivated")
        assert not (await get_admin_account(pool, "member"))["is_active"]
        assert (await post("/member/activate")).headers["location"].endswith("?msg=account_activated")
        assert (await get_admin_account(pool, "member"))["is_active"]
        assert (await post("/member/delete")).headers["location"].endswith("?msg=account_deleted")
        (deleted,) = await list_deleted_admin_accounts(pool)
        assert deleted["username"] == "member" and deleted["deleted_by"] == "owner"
        assert "password_hash" not in deleted
        assert (await client.get(PASSWORD_PAGE, auth=("member", password))).status_code == 401
        assert (await post("/member/activate")).headers["location"].endswith("?err=not_found")

        async with pool.connection() as conn:
            await conn.execute("UPDATE admin_accounts SET deleted_at=NOW()-INTERVAL '29 days' WHERE username='member'")
        assert (await post("/member/restore")).headers["location"].endswith("?msg=account_restored")
        assert await _authenticate("member", password) == ("member", "superadmin")
        assert (await get_admin_account(pool, "member"))["deleted_by"] is None

        assert (await post("/member/delete")).status_code == 303
        async with pool.connection() as conn:
            await conn.execute("UPDATE admin_accounts SET deleted_at=NOW()-INTERVAL '30 days' WHERE username='member'")
        assert not await restore_admin_account(pool, "member")
        assert await get_admin_account(pool, "member") is not None
        assert (await client.get(ACCOUNTS, auth=auth)).status_code == 200
        assert await get_admin_account(pool, "member") is None
        async with pool.connection() as conn:
            events = await conn.execute(
                "SELECT action FROM system_audit_log WHERE entity_id='member' AND component_name='admin_accounts'"
            )
            assert {event[0] for event in await events.fetchall()} >= {"ADMIN_ACCOUNT_RESTORED", "ADMIN_ACCOUNT_PURGED"}


def test_staff_html_escapes_legacy_account_names_without_javascript():
    username = "bad'\"><svg onload=alert(1)>"
    owner = "<owner>"
    account = {"username": username, "role": "reviewer", "is_active": True, "created_at": None}
    deleted = {**account, "deleted_at": None, "deleted_by": owner}
    page = routes._render_accounts(owner, [account], [deleted], "", "")
    assert username not in page and owner not in page
    assert escape(username) in page and escape(owner) in page
    assert f'action="{ACCOUNTS}/{quote(username, safe="")}/deactivate"' in page
    assert f'action="{ACCOUNTS}/{quote(username, safe="")}/restore"' in page
    assert "onsubmit=" not in page


def test_accounts_page_has_role_section_and_manage_panel():
    owner = {"username": "owner", "role": "superadmin", "is_active": True, "created_at": None}
    member = {"username": "member", "role": "viewer", "is_active": False, "created_at": None}
    page = routes._render_accounts("owner", [owner, member], [], "", "")
    assert "Phân quyền tài khoản" in page
    assert f'action="{ACCOUNTS}/role"' in page
    assert 'name="username"' in page
    assert "<summary data-i18n=\"manage\">Quản lý</summary>" in page
    assert 'data-i18n="role_section_heading"' in page
    assert 'data-i18n="manage"' in page
    assert 'data-role-label="superadmin"' in page
    assert "Quản trị tối cao" in page
    assert f'action="{ACCOUNT_PASSWORD_PAGE}"' in page
    assert 'name="current_password"' in page
    assert f'action="{ACCOUNTS}/member/activate"' in page


def test_admin_language_toggle_assets_are_wired():
    static_dir = Path(__file__).resolve().parents[1] / "static" / "ui"
    nav = (static_dir / "admin_nav.js").read_text(encoding="utf-8")
    i18n = (static_dir / "admin_i18n.js").read_text(encoding="utf-8")
    assert "adminLangToggle" in nav
    assert "AdminI18n.toggle" in nav
    assert "localStorage.getItem('tb_lang')" in i18n
    assert "accounts_title" in i18n and "Admin account management" in i18n
    assert "role_superadmin" in i18n and "Quản trị tối cao" in i18n
