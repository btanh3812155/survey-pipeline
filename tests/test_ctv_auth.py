"""Isolated CTV auth contracts; optional real Redis/PG targets are disposable only."""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from starlette.requests import Request

from config import settings
from core import admin_sessions, ctv_auth as auth

CODE, PHONE, USER = "CTV_AUTH_TEST", "0912345678", 71001
STAMP = datetime(2026, 11, 25, tzinfo=timezone.utc)


def request(*, challenge="", session="", host="127.0.0.1"):
    cookies = []
    if challenge:
        cookies.append(f"{auth.CHALLENGE_COOKIE_NAME}={challenge}")
    if session:
        cookies.append(f"{auth.COOKIE_NAME}={session}")
    return Request({
        "type": "http", "method": "POST", "path": "/ctv/login/verify",
        "scheme": "http", "server": ("testserver", 80), "client": (host, 1234),
        "headers": [(b"cookie", "; ".join(cookies).encode())],
    })


class MemoryRedis:
    """Atomic Redis substitute; the optional Redis parametrization runs the actual Lua."""

    def __init__(self, clock):
        self.clock, self.values, self.expires = clock, {}, {}
        self.lock = asyncio.Lock()

    def _get(self, key):
        if self.expires.get(key, float("inf")) <= self.clock[0]:
            self.values.pop(key, None)
        return self.values.get(key)

    async def get(self, key):
        return self._get(key)

    async def set(self, key, value, *, ex, nx=False):
        async with self.lock:
            if nx and self._get(key) is not None:
                return None
            self.values[key], self.expires[key] = value, self.clock[0] + ex
            return True

    async def ttl(self, key):
        return int(self.expires[key] - self.clock[0]) if self._get(key) else -2

    def _incr(self, key, ttl):
        value = int(self._get(key) or 0) + 1
        self.values[key] = str(value)
        if value == 1:
            self.expires[key] = self.clock[0] + ttl
        return value

    async def eval(self, script, count, key, *args):
        assert count == 1
        async with self.lock:
            if script == auth._LIMIT_SCRIPT:
                return int(self._incr(key, int(args[0])) <= int(args[1]))
            assert script == auth._REDEEM_SCRIPT
            raw = self._get(key)
            if raw is None:
                return [0]
            challenge = json.loads(raw)
            now, digest, ttl, account_max, attempts_max = args
            if challenge["exp"] <= now:
                self.values.pop(key)
                return [0]
            if self._incr(challenge["attempt_key"], int(ttl)) > int(account_max):
                return [-1]
            challenge["attempts"] += 1
            if challenge["attempts"] > int(attempts_max):
                self.values.pop(key)
                return [0]
            if challenge["digest"] == digest:
                self.values.pop(key)
                return [1, challenge["code"], challenge["binding"]]
            if challenge["attempts"] >= int(attempts_max):
                self.values.pop(key)
            else:
                self.values[key] = json.dumps(challenge)
            return [0]


class NamespacedRedis:
    def __init__(self, client, prefix):
        self.client, self.prefix = client, prefix

    async def get(self, key):
        return await self.client.get(self.prefix + key)

    async def ttl(self, key):
        return await self.client.ttl(self.prefix + key)

    async def set(self, key, value, **kwargs):
        payload = json.loads(value)
        payload["attempt_key"] = self.prefix + payload["attempt_key"]
        return await self.client.set(self.prefix + key, json.dumps(payload), **kwargs)

    async def eval(self, script, count, key, *args):
        return await self.client.eval(script, count, self.prefix + key, *args)


@pytest_asyncio.fixture(params=["memory", "redis"])
async def store(request, monkeypatch):
    clock = [1806105600]
    monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: clock[0]))
    if request.param == "memory":
        yield MemoryRedis(clock), clock
        return
    url = os.environ.get("UPGRADE_TEST_REDIS_URL", "")
    if not url:
        pytest.skip("set UPGRADE_TEST_REDIS_URL for actual Lua concurrency checks")
    from urllib.parse import urlsplit
    import redis.asyncio as redis

    target = urlsplit(url)
    if target.scheme != "redis" or target.hostname not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("auth tests require an explicit local disposable Redis target")
    prefix = "ctv-auth-test:" + uuid4().hex + ":"
    client = redis.from_url(url, decode_responses=True)
    try:
        yield NamespacedRedis(client, prefix), clock
    finally:
        keys = [key async for key in client.scan_iter(match=prefix + "*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


class Directory:
    def __init__(self):
        self.rows = [(CODE, "CTV thử nghiệm", PHONE, USER, True, STAMP, "verified_contact", uuid4())]
        self.calls = []
        self.error = False

    @asynccontextmanager
    async def connection(self):
        if self.error:
            raise OSError("private database error")
        yield self

    @asynccontextmanager
    async def cursor(self):
        yield self

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))

    async def fetchall(self):
        return self.rows


@pytest.fixture
def env(store, monkeypatch):
    client, clock = store
    directory = Directory()
    calls = []
    monkeypatch.setattr(settings, "APP_SECRET", "ctv-test-secret-with-at-least-32-characters")
    monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", "123456:unit_test_token")
    monkeypatch.setattr(auth, "get_db_pool", lambda: directory)
    monkeypatch.setattr(auth, "get_redis_client", lambda: client)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, maximum):
            assert maximum == 65536
            return b'{"ok":true}'

    def send(request, *, timeout):
        import threading

        calls.append((request, timeout, threading.get_ident()))
        return Response()

    monkeypatch.setattr(auth, "urlopen", send)
    monkeypatch.setattr(auth.secrets, "randbelow", lambda maximum: 123456)
    return SimpleNamespace(client=client, clock=clock, directory=directory, calls=calls, response=Response)


async def login(env, **identifier):
    challenge = await auth.start_login(request(), **(identifier or {"code": CODE}))
    token = await auth.finish_login(request(challenge=challenge), challenge, "123456")
    return challenge, token


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", [{"code": CODE}, {"phone": "+84 912 345 678"}])
async def test_both_identifiers_require_telegram_otp_and_issue_scoped_session(env, identifier):
    import threading

    challenge = await auth.start_login(request(), **identifier)
    assert re.fullmatch(r"[0-9a-f]{32}", challenge)
    assert await auth.ctv_from_session(request(session=CODE)) is None
    sent, timeout, worker = env.calls[0]
    assert worker != threading.get_ident()
    assert timeout == 8
    assert sent.full_url == "https://api.telegram.org/bot123456:unit_test_token/sendMessage"
    assert sent.get_method() == "POST"
    assert sent.get_header("Content-type") == "application/json"
    payload = json.loads(sent.data)
    assert payload["chat_id"] == USER
    assert "123456" in payload["text"] and "5 phút" in payload["text"]
    raw = await env.client.get(f"ctv-auth:challenge:{challenge}")
    stored = json.loads(raw)
    assert PHONE not in raw and "123456" not in raw
    assert stored["digest"] == auth._digest("ctv-otp-v1", challenge, "123456")
    assert 0 < await env.client.ttl(f"ctv-auth:challenge:{challenge}") <= 300
    token = await auth.finish_login(request(challenge=challenge), challenge, "123456")
    identity = await auth.ctv_from_session(request(session=token))
    assert identity == {"code": CODE, "display_name": "CTV thử nghiệm", "phone": PHONE,
                        "telegram_user_id": USER, "is_active": True}
    assert set(identity) == {"code", "display_name", "phone", "telegram_user_id", "is_active"}
    assert all("notes" not in sql.lower() for sql, _ in env.directory.calls)
    body = json.loads(auth.base64.urlsafe_b64decode(token.split(".")[0] + "=="))
    assert body["exp"] == env.clock[0] + 30 * 24 * 60 * 60
    assert admin_sessions.read(token) is None


@pytest.mark.asyncio
async def test_dropdown_queries_only_database_active_codes(env, monkeypatch):
    env.directory.rows = [("ONLY_ACTIVE",)]
    assert await auth.list_login_codes() == ["ONLY_ACTIVE"]
    assert env.directory.calls[-1][0] == "SELECT code FROM ctv_directory WHERE is_active = TRUE ORDER BY code"
    env.directory.rows = []
    assert await auth.list_login_codes() == []
    monkeypatch.setattr(auth, "get_db_pool", lambda: None)
    with pytest.raises(HTTPException) as failure:
        await auth.list_login_codes()
    assert failure.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", [{}, {"code": CODE, "phone": PHONE}, {"phone": "invalid"}])
async def test_missing_duplicate_or_invalid_identifier_never_sends(env, identifier):
    with pytest.raises(HTTPException) as failure:
        await auth.start_login(request(), **identifier)
    assert failure.value.status_code in {400, 401}
    assert not env.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["contact_share", "command", "ownership_conflict", None])
async def test_unverified_and_conflicted_bindings_cannot_login(env, source):
    row = list(env.directory.rows[0])
    row[6] = source
    env.directory.rows = [tuple(row)]
    with pytest.raises(HTTPException) as failure:
        await auth.start_login(request(), code=CODE)
    assert failure.value.status_code == 401
    assert not env.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [[], [Directory().rows[0], Directory().rows[0]]])
async def test_non_ctv_or_ambiguous_identity_is_denied(env, rows):
    env.directory.rows = rows
    with pytest.raises(HTTPException) as failure:
        await auth.start_login(request(), phone=PHONE)
    assert failure.value.status_code == 401
    assert not env.calls


@pytest.mark.asyncio
async def test_wrong_malformed_and_exhausted_otp_attempts(env):
    challenge = await auth.start_login(request(), code=CODE)
    for otp in ["000000", "bad", "１２３４５６", "12345", "999999", "123456"]:
        with pytest.raises(HTTPException) as failure:
            await auth.finish_login(request(challenge=challenge), challenge, otp)
        assert failure.value.status_code == 401
    assert await env.client.get(f"ctv-auth:challenge:{challenge}") is None


@pytest.mark.asyncio
async def test_browser_mismatch_does_not_consume_correct_otp(env):
    challenge = await auth.start_login(request(), code=CODE)
    for browser in [request(), request(challenge="a" * 32)]:
        with pytest.raises(HTTPException) as failure:
            await auth.finish_login(browser, challenge, "123456")
        assert failure.value.status_code == 401
    assert await auth.finish_login(request(challenge=challenge), challenge, "123456")


@pytest.mark.asyncio
async def test_expired_challenge_denied_even_before_redis_ttl_elapses(env):
    challenge = await auth.start_login(request(), code=CODE)
    env.clock[0] += 300
    with pytest.raises(HTTPException) as failure:
        await auth.finish_login(request(challenge=challenge), challenge, "123456")
    assert failure.value.status_code == 401


@pytest.mark.asyncio
async def test_correct_otp_is_atomic_single_use_under_concurrent_redemption(env):
    challenge = await auth.start_login(request(), code=CODE)
    results = await asyncio.gather(*(
        auth.finish_login(request(challenge=challenge), challenge, "123456") for _ in range(16)
    ), return_exceptions=True)
    assert sum(isinstance(result, str) for result in results) == 1
    assert all(isinstance(result, str) or isinstance(result, HTTPException) and result.status_code == 401
               for result in results)
    with pytest.raises(HTTPException) as failure:
        await auth.finish_login(request(challenge=challenge), challenge, "123456")
    assert failure.value.status_code == 401


@pytest.mark.asyncio
async def test_send_limits_are_atomic_per_ctv_and_ip(env):
    results = await asyncio.gather(*(
        auth.start_login(request(host=f"127.0.0.{index + 1}"), code=CODE) for index in range(12)
    ), return_exceptions=True)
    assert sum(isinstance(result, str) for result in results) == 3
    assert len(env.calls) == 3
    assert all(isinstance(result, str) or isinstance(result, HTTPException) and result.status_code == 429
               for result in results)
    env.directory.rows = []
    results = await asyncio.gather(*(
        auth.start_login(request(host="127.0.1.1"), code="NON_CTV") for _ in range(20)
    ), return_exceptions=True)
    assert sum(isinstance(result, HTTPException) and result.status_code == 401 for result in results) == 10
    assert sum(isinstance(result, HTTPException) and result.status_code == 429 for result in results) == 10


@pytest.mark.asyncio
async def test_total_verification_limits_span_challenges(env):
    challenge = await auth.start_login(request(), code=CODE)
    template = json.loads(await env.client.get(f"ctv-auth:challenge:{challenge}"))
    # Namespaced Redis adds its namespace on each set, not on the template's reuse.
    if isinstance(env.client, NamespacedRedis):
        template["attempt_key"] = template["attempt_key"].removeprefix(env.client.prefix)
    for index in range(5):
        identifier = f"{index + 1:032x}"
        payload = dict(template, digest=auth._digest("ctv-otp-v1", identifier, "123456"))
        await env.client.set(f"ctv-auth:challenge:{identifier}", json.dumps(payload), ex=300)
        results = await asyncio.gather(*(
            auth.finish_login(request(challenge=identifier), identifier, "000000") for _ in range(5)
        ), return_exceptions=True)
        expected = 401 if index < 4 else 429
        assert all(isinstance(result, HTTPException) and result.status_code == expected for result in results)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    (2, "0987654321"), (3, USER + 1), (4, False),
    (5, STAMP + timedelta(seconds=1)), (6, "ownership_conflict"), (6, "admin_verified"),
    (7, uuid4()), (7, None),
])
async def test_binding_changes_or_deactivation_revoke_session_and_pending_otp(env, field, value):
    _, token = await login(env)
    pending = await auth.start_login(request(), code=CODE)
    row = list(env.directory.rows[0])
    row[field] = value
    env.directory.rows = [tuple(row)]
    assert await auth.ctv_from_session(request(session=token)) is None
    with pytest.raises(HTTPException) as failure:
        await auth.finish_login(request(challenge=pending), pending, "123456")
    assert failure.value.status_code == 401


@pytest.mark.asyncio
async def test_session_tampering_expiry_and_admin_token_are_denied(env):
    _, token = await login(env)
    body, signature = token.rsplit(".", 1)
    for invalid in [f"{body}x.{signature}", f"{body}.{'0' * 64}", "é.invalid",
                    admin_sessions.issue("admin", "password-hash")]:
        assert await auth.ctv_from_session(request(session=invalid)) is None
    env.clock[0] += auth.SESSION_TTL_SECONDS
    assert await auth.ctv_from_session(request(session=token)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["db_missing", "db_error", "redis_missing", "redis_error", "redis_store_error", "token_missing", "provider_error"])
async def test_login_outages_fail_closed_without_private_errors(env, monkeypatch, failure_mode, caplog):
    def private_error(*args, **kwargs):
        raise OSError("123456:unit_test_token private provider reply " + PHONE)

    if failure_mode == "db_missing":
        monkeypatch.setattr(auth, "get_db_pool", lambda: None)
    elif failure_mode == "db_error":
        env.directory.error = True
    elif failure_mode == "redis_missing":
        monkeypatch.setattr(auth, "get_redis_client", lambda: None)
    elif failure_mode == "redis_error":
        monkeypatch.setattr(env.client, "eval", private_error)
    elif failure_mode == "redis_store_error":
        monkeypatch.setattr(env.client, "set", private_error)
    elif failure_mode == "token_missing":
        monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None)
    else:
        monkeypatch.setattr(auth, "urlopen", private_error)
    with pytest.raises(HTTPException) as failure:
        await auth.start_login(request(), code=CODE)
    assert failure.value.status_code == 503
    assert PHONE not in str(failure.value.detail) + caplog.text
    assert "unit_test_token" not in str(failure.value.detail) + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [(500, b'{"ok":true}'), (200, b'{"ok":false}'),
                                        (200, b'{"ok":"true"}'), (200, b'not-json')])
async def test_delivery_requires_http_and_telegram_success(env, monkeypatch, status, body):
    env.response.status = status
    monkeypatch.setattr(env.response, "read", lambda self, maximum: body)
    with pytest.raises(HTTPException) as failure:
        await auth.start_login(request(), code=CODE)
    assert failure.value.status_code == 503


@pytest.mark.asyncio
async def test_redemption_and_session_database_outages_return_safe_503(env, monkeypatch):
    _, token = await login(env)
    challenge = await auth.start_login(request(), code=CODE)
    env.directory.error = True
    for operation in [auth.ctv_from_session(request(session=token)),
                      auth.finish_login(request(challenge=challenge), challenge, "123456")]:
        with pytest.raises(HTTPException) as failure:
            await operation
        assert failure.value.status_code == 503
    env.directory.error = False
    challenge = await auth.start_login(request(), code=CODE)
    monkeypatch.setattr(auth, "get_redis_client", lambda: None)
    with pytest.raises(HTTPException) as failure:
        await auth.finish_login(request(challenge=challenge), challenge, "123456")
    assert failure.value.status_code == 503

@pytest.mark.asyncio
async def test_verification_ip_limit_is_atomic_even_for_missing_challenges(env):
    challenge = "a" * 32
    results = await asyncio.gather(*(
        auth.finish_login(request(challenge=challenge), challenge, "123456") for _ in range(75)
    ), return_exceptions=True)
    assert sum(isinstance(result, HTTPException) and result.status_code == 401 for result in results) == 60
    assert sum(isinstance(result, HTTPException) and result.status_code == 429 for result in results) == 15


@pytest.mark.asyncio
async def test_redis_redemption_outage_fails_closed(env, monkeypatch):
    challenge = await auth.start_login(request(), code=CODE)
    original = env.client.eval

    async def unavailable(script, *args):
        if script == auth._REDEEM_SCRIPT:
            raise OSError("private Redis connection error")
        return await original(script, *args)

    monkeypatch.setattr(env.client, "eval", unavailable)
    with pytest.raises(HTTPException) as failure:
        await auth.finish_login(request(challenge=challenge), challenge, "123456")
    assert failure.value.status_code == 503



@pytest_asyncio.fixture
async def real_directory(env, monkeypatch):
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("disposable PostgreSQL is required")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from psycopg_pool import AsyncConnectionPool
    from core.ctv import CTV_TABLE_STATEMENTS
    from core.phone_links import PHONE_LINK_TABLE_STATEMENTS
    from tests.postgres_harness import resolve_test_database_url

    dsn = resolve_test_database_url()
    schema = "ctv_auth_" + uuid4().hex
    monkeypatch.setattr(auth, "get_db_pool", lambda: None)
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            isolated = make_conninfo(dsn, options=f"-c search_path={schema}")
            with psycopg.connect(isolated, autocommit=True) as conn:
                for statement in CTV_TABLE_STATEMENTS:
                    conn.execute(statement)
                for statement in PHONE_LINK_TABLE_STATEMENTS:
                    conn.execute(statement)
                conn.execute("INSERT INTO ctv_directory(code,display_name,phone,telegram_user_id,notes) "
                             "VALUES (%s,'CTV thử nghiệm',%s,%s,'private admin notes')", (CODE, "+84 912 345 678", USER))
                conn.execute("INSERT INTO phone_telegram_links(phone_normalized,telegram_user_id,source) "
                             "VALUES (%s,%s,'verified_contact')", (PHONE, USER))
                async with AsyncConnectionPool(isolated, open=False, min_size=1, max_size=2) as pool:
                    monkeypatch.setattr(auth, "get_db_pool", lambda: pool)
                    yield conn
        finally:
            setup.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_real_database_identity_requires_unique_active_trusted_matching_ctv(real_directory):
    conn = real_directory
    for source, allowed in [("verified_contact", True), ("admin_verified", True),
                            ("contact_share", False), ("ownership_conflict", False)]:
        conn.execute("UPDATE phone_telegram_links SET source=%s", (source,))
        identity = await auth._identity(code=CODE)
        assert bool(identity) == allowed
        assert bool(await auth._identity(phone=PHONE)) == allowed
        if identity:
            assert identity[0]["phone"] == PHONE and "notes" not in identity[0]
    conn.execute("UPDATE phone_telegram_links SET source='verified_contact'")
    assert await auth._identity(code="NON_CTV") is None
    for statement in [
        "UPDATE ctv_directory SET telegram_user_id=71002",
        "UPDATE ctv_directory SET telegram_user_id=0",
        "UPDATE ctv_directory SET telegram_user_id=71001,is_active=FALSE",
    ]:
        conn.execute(statement)
        assert await auth._identity(code=CODE) is None
    conn.execute("UPDATE ctv_directory SET telegram_user_id=71001,is_active=TRUE")
    conn.execute("INSERT INTO ctv_directory(code,phone,telegram_user_id,is_active) "
                 "VALUES ('DUPLICATE',%s,%s,FALSE)", (PHONE, USER))
    assert await auth._identity(code=CODE) is None
    assert await auth._identity(phone=PHONE) is None
    conn.execute("DELETE FROM ctv_directory WHERE code='DUPLICATE'")
    assert await auth._identity(code=CODE)


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disable", "phone", "telegram", "code", "recreate"])
async def test_real_identity_restoration_never_resurrects_session_or_pending_otp(env, real_directory, change):
    conn = real_directory
    _, token = await login(env)
    pending = await auth.start_login(request(), code=CODE)
    original_revision = conn.execute("SELECT auth_revision FROM ctv_directory").fetchone()[0]
    original_link = conn.execute("SELECT * FROM phone_telegram_links").fetchall()
    if change == "recreate":
        conn.execute("DELETE FROM ctv_directory WHERE code=%s", (CODE,))
        assert await auth.ctv_from_session(request(session=token)) is None
        conn.execute("INSERT INTO ctv_directory(code,display_name,phone,telegram_user_id) "
                     "VALUES (%s,'CTV thử nghiệm',%s,%s)", (CODE, "+84 912 345 678", USER))
    else:
        column, away, restored = {
            "disable": ("is_active", False, True),
            "phone": ("phone", "0987654321", "+84 912 345 678"),
            "telegram": ("telegram_user_id", USER + 1, USER),
            "code": ("code", "CTV_AUTH_CHANGED", CODE),
        }[change]
        from psycopg import sql

        update = sql.SQL("UPDATE ctv_directory SET {}=%s").format(sql.Identifier(column))
        conn.execute(update, (away,))
        away_revision = conn.execute("SELECT auth_revision FROM ctv_directory").fetchone()[0]
        assert away_revision != original_revision
        assert await auth.ctv_from_session(request(session=token)) is None
        conn.execute(update, (restored,))
        assert conn.execute("SELECT auth_revision FROM ctv_directory").fetchone()[0] != away_revision
    assert conn.execute("SELECT auth_revision FROM ctv_directory").fetchone()[0] != original_revision
    assert conn.execute("SELECT * FROM phone_telegram_links").fetchall() == original_link
    assert await auth._identity(code=CODE)
    assert await auth.ctv_from_session(request(session=token)) is None
    with pytest.raises(HTTPException) as failure:
        await auth.finish_login(request(challenge=pending), pending, "123456")
    assert failure.value.status_code == 401


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_real_harmless_edits_and_schema_replay_preserve_session_and_pending_otp(env, real_directory):
    from core.ctv import CTV_TABLE_STATEMENTS

    conn = real_directory
    _, token = await login(env)
    pending = await auth.start_login(request(), code=CODE)
    original_revision = conn.execute("SELECT auth_revision FROM ctv_directory").fetchone()[0]
    conn.execute("UPDATE ctv_directory SET display_name='Tên mới',notes='Ghi chú mới',updated_at=NOW(),"
                 "code=code,phone=phone,telegram_user_id=telegram_user_id,is_active=is_active")
    for statement in CTV_TABLE_STATEMENTS:
        conn.execute(statement)
    assert conn.execute("SELECT auth_revision FROM ctv_directory").fetchone()[0] == original_revision
    identity = await auth.ctv_from_session(request(session=token))
    assert identity and identity["display_name"] == "Tên mới"
    assert await auth.finish_login(request(challenge=pending), pending, "123456")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_real_missing_revision_schema_fails_closed_for_session_and_pending_otp(env, real_directory):
    conn = real_directory
    _, token = await login(env)
    pending = await auth.start_login(request(), code=CODE)
    conn.execute("DROP TRIGGER ctv_directory_auth_revision ON ctv_directory")
    conn.execute("ALTER TABLE ctv_directory DROP COLUMN auth_revision")
    for operation in [auth.ctv_from_session(request(session=token)),
                      auth.finish_login(request(challenge=pending), pending, "123456")]:
        with pytest.raises(HTTPException) as failure:
            await operation
        assert failure.value.status_code == 503


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_real_revision_migration_upgrades_legacy_rows_and_replays_without_revoking(env, real_directory):
    import hashlib
    from pathlib import Path

    conn = real_directory
    conn.execute("DROP TRIGGER ctv_directory_auth_revision ON ctv_directory")
    conn.execute("ALTER TABLE ctv_directory DROP COLUMN auth_revision")
    conn.execute("CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,checksum TEXT NOT NULL)")
    migrations = Path(__file__).resolve().parents[1] / "scripts" / "migrations"
    previous = hashlib.sha256((migrations / "014_trash_compaction_runs.sql").read_bytes()).hexdigest()
    path = migrations / "015_ctv_auth_revision.sql"
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    migration = path.read_text().replace(":'migration_014_sha256'", f"'{previous}'").replace(
        ":'migration_sha256'", f"'{checksum}'")
    conn.execute("INSERT INTO schema_migrations VALUES ('014',%s)", (previous,))
    conn.execute(migration)
    revision = conn.execute("SELECT auth_revision FROM ctv_directory").fetchone()[0]
    assert revision is not None
    _, token = await login(env)
    pending = await auth.start_login(request(), code=CODE)
    conn.execute(migration)
    assert conn.execute("SELECT auth_revision FROM ctv_directory").fetchone()[0] == revision
    assert conn.execute("SELECT version,checksum FROM schema_migrations ORDER BY version").fetchall() == [
        ("014", previous), ("015", checksum),
    ]
    assert await auth.ctv_from_session(request(session=token))
    assert await auth.finish_login(request(challenge=pending), pending, "123456")
    conn.execute("UPDATE ctv_directory SET is_active=FALSE")
    conn.execute("UPDATE ctv_directory SET is_active=TRUE")
    assert conn.execute("SELECT auth_revision FROM ctv_directory").fetchone()[0] != revision
    assert await auth.ctv_from_session(request(session=token)) is None
