"""Recovery invariants against disposable PostgreSQL; SMTP never leaves the test process."""

import asyncio
import hashlib
import os
import re
import smtplib
import ssl
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from core import admin_recovery as recovery
from core.passwords import hash_password, verify_password
from routers import admin_recovery as routes
from tests.postgres_harness import SCHEMA_PATH, resolve_test_database_url

OLD = "owner@example.test"
NEW = "other@example.test"
PASSWORD = "a long replacement password"
AUTH_HASH = hash_password("old administrator password", iterations=1000)
IP = "192.0.2.12"
SMTP = recovery.SMTPConfig("smtp.example.test", 587, "starttls", "sender", "mock-credential", "sender@example.test")
ORIGIN = {"Origin": "https://testserver"}
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.setattr(recovery.settings, "APP_SECRET", "test-only-recovery-secret-in-unit-tests")


@pytest.fixture
def database():
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("disposable PostgreSQL is required")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL")
    dsn = resolve_test_database_url()
    name = "recovery_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            yield make_conninfo(dsn, options=f"-c search_path={name}")
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest_asyncio.fixture
async def store(database, monkeypatch):
    schema = SCHEMA_PATH.read_text()
    accounts = schema.split("CREATE TABLE IF NOT EXISTS admin_accounts", 1)[1].split("-- 7.", 1)[0]
    audit = schema.split("CREATE TABLE IF NOT EXISTS system_audit_log", 1)[1].split("CREATE INDEX", 1)[0]
    with psycopg.connect(database, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS admin_accounts" + accounts)
        conn.execute("CREATE TABLE IF NOT EXISTS system_audit_log" + audit)
        conn.execute(
            "INSERT INTO admin_accounts(username,password_hash,role,recovery_email) VALUES ('owner',%s,'superadmin',%s)",
            (AUTH_HASH, OLD),
        )
        deliveries = []
        monkeypatch.setattr(recovery, "_send_mail", lambda config, batch: deliveries.append(batch))
        async with AsyncConnectionPool(database, open=False, min_size=1, max_size=8) as pool:
            assert await recovery.ensure_admin_recovery(pool)
            yield pool, conn, deliveries


async def _issue(pool, purpose=recovery.RESET, *, host=IP):
    return await recovery.issue_challenge(
        pool, host, purpose, "owner", new_email=NEW, auth_hash=AUTH_HASH,
    )


async def _request(pool, purpose=recovery.RESET):
    challenge, batch = await _issue(pool, purpose)
    assert batch and await recovery.deliver_challenge(pool, SMTP, batch)
    return challenge, [code for _, code in batch.messages]


async def _confirm(pool, challenge, code, *, purpose=recovery.RESET, new_code="", auth_hash=AUTH_HASH):
    return await recovery.confirm_challenge(
        pool, IP, purpose, challenge, code, new_password=PASSWORD,
        new_code=new_code, username="owner", auth_hash=auth_hash,
    )


def _wrong(code):
    return "0" * 8 if code != "0" * 8 else "1" * 8


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_wrong_purpose_password_reset_replay_and_audit(store):
    pool, conn, deliveries = store
    challenge, (code,) = await _request(pool)
    assert deliveries[0].messages == ((OLD, code),)
    row = conn.execute("SELECT * FROM admin_recovery_challenges").fetchone()
    assert row["old_code_hash"] == recovery._code_hash(challenge, recovery.RESET, "old", code)
    assert code.encode() not in row["old_code_hash"] and row["new_email"] is None
    assert (row["expires_at"] - row["created_at"]).total_seconds() == pytest.approx(600, abs=0.05)
    assert not await _confirm(pool, challenge, code, purpose=recovery.EMAIL, new_code=code)
    assert not await _confirm(pool, challenge, _wrong(code))
    assert conn.execute("SELECT attempts FROM admin_recovery_challenges").fetchone()["attempts"] == 1
    assert sorted(await asyncio.gather(_confirm(pool, challenge, code), _confirm(pool, challenge, code))) == [False, True]
    assert not await _confirm(pool, challenge, code)
    assert verify_password(PASSWORD, conn.execute("SELECT password_hash FROM admin_accounts").fetchone()["password_hash"])
    assert conn.execute("SELECT action, details FROM system_audit_log").fetchall() == [
        {"action": "ADMIN_PASSWORD_RECOVERED", "details": {"method": "email_otp"}},
    ]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_resend_expiry_and_five_attempts(store):
    pool, conn, _ = store
    challenge, (code,) = await _request(pool)
    dummy, delivery = await _issue(pool)
    assert delivery is None and dummy != challenge
    assert conn.execute("SELECT id::text FROM admin_recovery_challenges").fetchone()["id"].replace("-", "") == challenge
    conn.execute("UPDATE admin_recovery_challenges SET expires_at=clock_timestamp()-interval '1 second'")
    assert not await _confirm(pool, challenge, code)
    conn.execute("UPDATE admin_recovery_challenges SET created_at=clock_timestamp()-interval '61 seconds'")
    replacement, (fresh,) = await _request(pool)
    assert replacement != challenge and not await _confirm(pool, challenge, code)
    results = await asyncio.gather(*(_confirm(pool, replacement, _wrong(fresh)) for _ in range(8)))
    assert not any(results)
    assert conn.execute("SELECT attempts FROM admin_recovery_challenges").fetchone()["attempts"] == 5
    assert not await _confirm(pool, replacement, fresh)
    assert not conn.execute("SELECT 1 FROM system_audit_log").fetchone()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_email_rotation_requires_both_codes_and_fences_old_reset(store):
    pool, conn, deliveries = store
    reset, (prior,) = await _request(pool)
    challenge, (old_code, new_code) = await _request(pool, recovery.EMAIL)
    assert deliveries[-1].messages == ((OLD, old_code), (NEW, new_code)) and old_code != new_code
    assert not await _confirm(pool, challenge, old_code)
    assert not await _confirm(pool, challenge, old_code, purpose=recovery.EMAIL, new_code=old_code)
    assert not await _confirm(pool, challenge, old_code, purpose=recovery.EMAIL, new_code=new_code, auth_hash="stale")
    assert await _confirm(pool, challenge, old_code, purpose=recovery.EMAIL, new_code=new_code)
    assert not await _confirm(pool, challenge, old_code, purpose=recovery.EMAIL, new_code=new_code)
    assert not await _confirm(pool, reset, prior)
    assert conn.execute("SELECT recovery_email FROM admin_accounts").fetchone()["recovery_email"] == NEW
    assert conn.execute("SELECT action FROM system_audit_log").fetchone()["action"] == "ADMIN_RECOVERY_EMAIL_CHANGED"


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", (
    "password_hash='replaced'", "recovery_email='new@example.test'", "role='viewer'", "is_active=FALSE",
    "deleted_at=clock_timestamp()", "updated_at=updated_at+interval '1 second'",
))
async def test_stale_account_state(store, mutation):
    pool, conn, _ = store
    challenge, (code,) = await _request(pool)
    conn.execute("UPDATE admin_accounts SET " + mutation)
    assert not await _confirm(pool, challenge, code)
    assert not conn.execute("SELECT 1 FROM system_audit_log").fetchone()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_partial_delivery_and_audit_failure_leave_code_unusable_or_unconsumed(store, monkeypatch, caplog):
    pool, conn, _ = store
    challenge, batch = await _issue(pool, recovery.EMAIL)
    assert batch
    old_code, new_code = (code for _, code in batch.messages)

    def partial_fail(config, delivery):
        raise smtplib.SMTPDataError(554, f"provider echoed {new_code} {NEW}".encode())

    with monkeypatch.context() as patch:
        patch.setattr(recovery, "_send_mail", partial_fail)
        assert not await recovery.deliver_challenge(pool, SMTP, batch)
    assert not await _confirm(pool, challenge, old_code, purpose=recovery.EMAIL, new_code=new_code)
    assert new_code not in caplog.text and NEW not in caplog.text
    assert not conn.execute("SELECT ready FROM admin_recovery_challenges").fetchone()["ready"]
    reset, (code,) = await _request(pool)
    conn.execute("ALTER TABLE system_audit_log ADD CHECK (false) NOT VALID")
    with pytest.raises(psycopg.errors.CheckViolation):
        await _confirm(pool, reset, code)
    assert conn.execute("SELECT password_hash FROM admin_accounts").fetchone()["password_hash"] == AUTH_HASH
    assert conn.execute("SELECT consumed_at FROM admin_recovery_challenges WHERE purpose='reset'").fetchone()["consumed_at"] is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_ip_and_account_budgets_serialize_across_connections(store, monkeypatch):
    pool, conn, _ = store
    monkeypatch.setitem(recovery.LIMITS, "request_ip", 3)
    monkeypatch.setitem(recovery.LIMITS, "request_account", 2)
    results = await asyncio.gather(*(_issue(pool, host=f"192.0.2.{n}") for n in range(31, 38)))
    assert sum(batch is not None for _, batch in results) == 1
    account = recovery._digest("admin-recovery-limit", "request_account", "owner")
    assert conn.execute("SELECT hits FROM admin_recovery_limits WHERE bucket=%s", (account,)).fetchone()["hits"] == 2
    results = await asyncio.gather(*(_issue(pool, host=IP) for _ in range(8)))
    assert all(batch is None for _, batch in results)
    ip = recovery._digest("admin-recovery-limit", "request_ip", IP)
    assert conn.execute("SELECT hits FROM admin_recovery_limits WHERE bucket=%s", (ip,)).fetchone()["hits"] == 3
    conn.execute("UPDATE admin_recovery_limits SET expires_at=clock_timestamp()-interval '1 second'")
    conn.execute("UPDATE admin_recovery_challenges SET created_at=clock_timestamp()-interval '61 seconds'")
    assert (await _issue(pool))[1]


@pytest.mark.postgres
def test_migration_007_promotes_legacy_owner_once(database):
    migrations = ROOT / "scripts" / "migrations"
    previous = hashlib.sha256((migrations / "006_admin_accounts.sql").read_bytes()).hexdigest()
    path = migrations / "007_admin_account_bin.sql"
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    migration = path.read_text().replace(":'migration_006_sha256'", f"'{previous}'").replace(":'migration_sha256'", f"'{checksum}'")
    schema = "admin_recovery_migration_" + uuid4().hex
    with psycopg.connect(database, autocommit=True) as root:
        root.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            dsn = make_conninfo(database, options=f"-c search_path={schema}")
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, checksum TEXT NOT NULL)")
                conn.execute("INSERT INTO schema_migrations VALUES ('006',%s)", (previous,))
                conn.execute("""CREATE TABLE admin_accounts (
                    username VARCHAR(64) PRIMARY KEY, password_hash TEXT NOT NULL, is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    role VARCHAR(16) NOT NULL DEFAULT 'admin' CHECK (role IN ('admin','manager'))
                )""")
                conn.execute("INSERT INTO admin_accounts (username,password_hash) VALUES ('admin','old'),('other','old')")
                conn.execute(migration)
                assert conn.execute("SELECT username,role,recovery_email FROM admin_accounts ORDER BY username").fetchall() == [
                    ("admin", "superadmin", "admin@trustbridgevn.com"), ("other", "reviewer", None),
                ]
                assert conn.execute("SELECT checksum FROM schema_migrations WHERE version='007'").fetchone() == (checksum,)
                conn.execute("UPDATE admin_accounts SET role='reviewer', recovery_email='changed@example.test' WHERE username='admin'")
                conn.execute(migration)
                assert conn.execute("SELECT role,recovery_email FROM admin_accounts WHERE username='admin'").fetchone() == (
                    "reviewer", "changed@example.test",
                )
        finally:
            root.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.parametrize("mode", ("starttls", "implicit"))
def test_smtp_uses_validated_tls_before_login_and_separate_envelopes(monkeypatch, mode):
    events = []

    class MockSMTP:
        def __init__(self, host, port, *, timeout, context=None):
            assert host == SMTP.host and timeout > 0
            if mode == "implicit":
                self.check_tls(context)
            else:
                assert context is None

        @staticmethod
        def check_tls(context):
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            events.append("tls")

        def starttls(self, *, context):
            self.check_tls(context)

        def login(self, username, password):
            assert events == ["tls"] and (username, password) == (SMTP.username, SMTP.password)
            events.append("login")

        def send_message(self, message, *, from_addr, to_addrs):
            assert from_addr == SMTP.sender and [message["To"]] == to_addrs
            events.append((to_addrs[0], message.get_content()))
            return {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    monkeypatch.setattr(smtplib, "SMTP_SSL" if mode == "implicit" else "SMTP", MockSMTP)
    config = recovery.SMTPConfig(SMTP.host, 465 if mode == "implicit" else 587, mode, SMTP.username, SMTP.password, SMTP.sender)
    recovery._send_mail(config, recovery.Delivery("opaque", recovery.EMAIL, ((OLD, "12345678"), (NEW, "98765432"))))
    assert [event[0] for event in events[2:]] == [OLD, NEW]
    assert "12345678" in events[2][1] and "98765432" not in events[2][1]
    assert "98765432" in events[3][1] and "12345678" not in events[3][1]


def test_smtp_configuration_fails_closed(monkeypatch):
    config = recovery.settings
    for key, value in (
        ("SMTP_HOST", SMTP.host), ("SMTP_PORT", "587"), ("SMTP_USERNAME", SMTP.username),
        ("SMTP_FROM_EMAIL", SMTP.sender), ("SMTP_TLS_MODE", "starttls"), ("SMTP_PASSWORD", None),
    ):
        monkeypatch.setattr(config, key, value)
    assert recovery.smtp_config() is None
    monkeypatch.setattr(config, "SMTP_PASSWORD", SecretStr(SMTP.password))
    assert recovery.smtp_config().sender == SMTP.sender
    monkeypatch.setattr(config, "SMTP_TLS_MODE", "plain")
    assert recovery.smtp_config() is None
    assert recovery.valid_password("x" * 12) and recovery.valid_password("x" * 1024)
    assert not recovery.valid_password("x" * 11) and not recovery.valid_password("x" * 1025)
    assert not recovery.normalize_email("x\nBcc:y@example.test")

@pytest.mark.asyncio
async def test_smtp_protocol_error_does_not_activate_challenge(monkeypatch):
    def fail(*_args):
        raise smtplib.SMTPAuthenticationError(535, b"credentials rejected")

    monkeypatch.setattr(recovery, "_send_mail", fail)
    delivery = recovery.Delivery("a" * 32, recovery.RESET, ((OLD, "12345678"),))
    assert not await recovery.deliver_challenge(object(), SMTP, delivery)


def test_public_routes_generic_with_no_secret_echo_and_strict_post_origin(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    monkeypatch.setattr(routes, "get_db_pool", lambda: object())
    monkeypatch.setattr(recovery, "smtp_config", lambda: None)
    with TestClient(app, base_url="https://testserver") as client:
        base = "/admin/recovery"
        assert client.get(base).status_code == 200
        assert client.get(base + "/email").status_code == 401
        assert client.post(base + "/request", data={"username": "owner"}).status_code == 403
        assert client.post(base + "/confirm", headers={"Origin": "https://other.test"}, data={"code": "12345678"}).status_code == 403
        disabled = client.post(base + "/request", headers=ORIGIN, data={"username": "owner"})
        assert disabled.status_code == 503 and routes.UNAVAILABLE in disabled.text
        assert disabled.headers["Cache-Control"] == "no-store"
        monkeypatch.setattr(recovery, "smtp_config", lambda: SMTP)

        async def issue(pool, host, purpose, username):
            return "a" * 32, recovery.Delivery("a" * 32, purpose, ((OLD, "12345678"),)) if username == "owner" else None

        async def fail_delivery(*args):
            return False

        monkeypatch.setattr(recovery, "issue_challenge", issue)
        monkeypatch.setattr(recovery, "deliver_challenge", fail_delivery)
        unknown, known = (client.post(base + "/request", headers=ORIGIN, data={"username": user}) for user in ("unknown", "owner"))
        assert unknown.status_code == known.status_code == 202 and unknown.text == known.text
        assert routes.PUBLIC_REQUESTED in known.text and "12345678" not in known.text and OLD not in known.text
        assert re.search(r'name="challenge_id" value="[0-9a-f]{32}"', known.text)

        async def authenticate(request: Request):
            if request.headers.get("Authorization") != "Basic test-fixture":
                raise HTTPException(401)
            request.state.admin_password_hash = AUTH_HASH
            return "owner"

        async def issue_email(pool, host, purpose, username, *, new_email, auth_hash):
            assert (purpose, username, new_email, auth_hash) == (recovery.EMAIL, "owner", NEW, AUTH_HASH)
            return "a" * 32, recovery.Delivery("a" * 32, purpose, ((OLD, "12345678"), (NEW, "98765432")))

        app.dependency_overrides[routes.verify_admin_auth] = authenticate
        monkeypatch.setattr(recovery, "issue_challenge", issue_email)
        denied = client.post(base + "/email", headers=ORIGIN, data={"action": "request", "new_email": NEW})
        assert denied.status_code == 401
        failed = client.post(base + "/email", headers={**ORIGIN, "Authorization": "Basic test-fixture"},
                             data={"action": "request", "new_email": NEW})
        assert failed.status_code == 503 and "12345678" not in failed.text and "98765432" not in failed.text
