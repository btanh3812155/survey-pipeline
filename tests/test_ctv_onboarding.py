"""CTV invite contracts against isolated PostgreSQL; Telegram stays mocked."""

import asyncio
import json
import time
import os
import re
from unittest.mock import AsyncMock
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from fastapi import HTTPException

from config import settings
from core import ctv, ctv_auth, ctv_onboarding as onboarding, durable_work, outbox_delivery, quotas
from core.phone_links import PHONE_LINK_TABLE_STATEMENTS
from services.telegram_polling import DriverBotRunner
from tests.postgres_harness import SCHEMA_PATH, resolve_test_database_url
from tests.test_ctv_auth import MemoryRedis, request

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]
CODE, OTHER_CODE, DISABLED_CODE = ctv.CTV_CODES[:3]
USER, OTHER_USER = 71001, 71002
PHONE, OTHER_PHONE = "0912345678", "0987654321"


@pytest.fixture
def database():
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("disposable PostgreSQL is required")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL")
    dsn = resolve_test_database_url()
    name = "ctv_onboarding_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            yield make_conninfo(dsn, options=f"-c search_path={name},public")
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest_asyncio.fixture
async def store(database, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_USERNAME", "CTVContractTest_bot")
    # A runner may refresh the directory cache; do not leak test CTVs into later tests.
    monkeypatch.setattr(ctv, "_CTV_DIRECTORY", {})
    monkeypatch.setattr(ctv, "_CTV_FORM_LINKS", {})
    schema = SCHEMA_PATH.read_text()
    with psycopg.connect(database, autocommit=True, row_factory=dict_row) as conn:
        for table in ("authorized_users", "ctv_directory", "system_audit_log"):
            prefix = f"CREATE TABLE IF NOT EXISTS {table}"
            conn.execute(prefix + schema.split(prefix, 1)[1].split(");", 1)[0] + ");")
        # Exercise the runtime upgrade of an existing, pre-invite directory too.
        conn.execute("ALTER TABLE ctv_directory DROP COLUMN IF EXISTS bot_invite_token")
        for statement in (*ctv.CTV_TABLE_STATEMENTS, *PHONE_LINK_TABLE_STATEMENTS):
            conn.execute(statement)
        conn.execute(
            "INSERT INTO ctv_directory(code,display_name,is_active) VALUES "
            "(%s,'CTV chính',TRUE),(%s,'CTV khác',TRUE),(%s,'CTV tạm khóa',FALSE)",
            (CODE, OTHER_CODE, DISABLED_CODE),
        )
        async with AsyncConnectionPool(database, open=False, min_size=1, max_size=8) as pool:
            yield pool, conn


@pytest.fixture
def runner(store, monkeypatch):
    instance = DriverBotRunner(bot_token="mock_ctv_onboarding_token", db_pool=store[0])
    monkeypatch.setattr(instance, "send_message", AsyncMock())
    return instance


def _update(user=USER, *, text=None, contact=None, chat_type="private"):
    message = {
        "chat": {"id": user if chat_type == "private" else -100123, "type": chat_type},
        "from": {"id": user, "username": "not_the_ctv_code", "first_name": "Nguyễn", "last_name": "An"},
    }
    if text is not None:
        message["text"] = text
    if contact is not None:
        message["contact"] = contact
    return {"message": message}


def _contact(user=USER, phone="+84 912 345 678"):
    return {"user_id": user, "phone_number": phone, "first_name": "Nguyễn", "last_name": "An"}


async def _payload(pool, code=CODE):
    return (await onboarding.ensure_ctv_bot_links(pool))[code].split("?start=", 1)[1]


def _assert_unlinked(conn, code=CODE, owner=None):
    row = conn.execute("SELECT phone,telegram_user_id FROM ctv_directory WHERE code=%s", (code,)).fetchone()
    assert row == {"phone": None, "telegram_user_id": owner}
    assert not conn.execute("SELECT 1 FROM authorized_users").fetchone()
    assert not conn.execute("SELECT 1 FROM phone_telegram_links").fetchone()


async def test_start_only_prompts_then_self_contact_completes_after_runner_restart(store, runner, monkeypatch):
    pool, conn = store
    payload = await _payload(pool, OTHER_CODE)
    await runner.handle_update(_update(text=f"/start {payload}"))

    _assert_unlinked(conn, OTHER_CODE)
    pending = conn.execute("SELECT * FROM ctv_bot_pending WHERE telegram_user_id=%s", (USER,)).fetchone()
    assert pending["ctv_code"] == OTHER_CODE
    assert pending["token"] == payload.removeprefix("ctv_")
    assert pending["created_at"] is not None
    prompt = runner.send_message.await_args
    assert OTHER_CODE in prompt.args[1]
    assert any(
        button.get("request_contact") and "Xác Thực Số Điện Thoại" in button["text"]
        for row in prompt.kwargs["reply_markup"]["keyboard"] for button in row
    )

    restarted = DriverBotRunner(bot_token="mock_ctv_onboarding_token", db_pool=pool)
    monkeypatch.setattr(restarted, "send_message", AsyncMock())
    await restarted.handle_update(_update(contact=_contact()))

    assert conn.execute(
        "SELECT phone,telegram_user_id FROM ctv_directory WHERE code=%s", (OTHER_CODE,),
    ).fetchone() == {"phone": PHONE, "telegram_user_id": USER}
    assert conn.execute(
        "SELECT username,role,full_name,is_active FROM authorized_users WHERE telegram_user_id=%s", (USER,),
    ).fetchone() == {"username": OTHER_CODE, "role": "COLLABORATOR", "full_name": "Nguyễn An", "is_active": True}
    assert conn.execute(
        "SELECT phone_normalized,telegram_user_id,source FROM phone_telegram_links",
    ).fetchall() == [{"phone_normalized": PHONE, "telegram_user_id": USER, "source": "verified_contact"}]
    assert not conn.execute("SELECT 1 FROM ctv_bot_pending").fetchone()
    assert conn.execute("SELECT telegram_user_id FROM ctv_directory WHERE code=%s", (CODE,)).fetchone()["telegram_user_id"] is None
    assert OTHER_CODE in restarted.send_message.await_args.args[1]


async def test_invalid_and_legacy_code_only_links_never_create_intent_or_binding(store, runner):
    pool, conn = store
    payload = await _payload(pool)
    tokens = {row["bot_invite_token"] for row in conn.execute("SELECT bot_invite_token FROM ctv_directory")}
    unknown = "ctv_" + next(char * 32 for char in "ABC" if char * 32 not in tokens)
    invalid_payloads = (CODE, f"ctv_{CODE}", payload[:-1], "ctv_" + "!" * 32, unknown)
    for invalid in invalid_payloads:
        with pytest.raises(ValueError):
            await onboarding.begin_ctv_onboarding(pool, USER, invalid)
        await runner.handle_update(_update(text=f"/start {invalid}"))
        assert not conn.execute("SELECT 1 FROM ctv_bot_pending").fetchone()
        _assert_unlinked(conn)
    assert runner.send_message.await_count == len(invalid_payloads)


async def test_foreign_contact_and_group_messages_cannot_complete_pending_intent(store, runner):
    pool, conn = store
    payload = await _payload(pool)
    await runner.handle_update(_update(text=f"/start {payload}"))
    for update in (
        _update(contact=_contact(OTHER_USER)),
        _update(contact={"phone_number": PHONE, "first_name": "Danh bạ ngoài"}),
        _update(contact=_contact(), chat_type="group"),
        _update(text=f"/start {payload}", chat_type="supergroup"),
    ):
        await runner.handle_update(update)
        _assert_unlinked(conn)
        assert conn.execute("SELECT ctv_code FROM ctv_bot_pending WHERE telegram_user_id=%s", (USER,)).fetchone()["ctv_code"] == CODE
    await runner.handle_update(_update(contact=_contact()))
    assert conn.execute("SELECT telegram_user_id FROM ctv_directory WHERE code=%s", (CODE,)).fetchone()["telegram_user_id"] == USER


@pytest.mark.parametrize("mutation,owner", [
    ("is_active=FALSE", None),
    (f"telegram_user_id={OTHER_USER}", OTHER_USER),
])
async def test_completion_rechecks_active_flag_and_owner_before_any_binding(store, runner, mutation, owner):
    pool, conn = store
    await runner.handle_update(_update(text=f"/start {await _payload(pool)}"))
    conn.execute(f"UPDATE ctv_directory SET {mutation} WHERE code=%s", (CODE,))
    with pytest.raises(ValueError):
        await onboarding.complete_ctv_onboarding(pool, USER, PHONE, "Nguyễn An")
    await runner.handle_update(_update(contact=_contact()))
    _assert_unlinked(conn, owner=owner)


async def test_phone_owned_by_another_account_leaves_ctv_unlinked(store, runner):
    pool, conn = store
    conn.execute(
        "INSERT INTO phone_telegram_links(phone_normalized,telegram_user_id,source) VALUES (%s,%s,'verified_contact')",
        (PHONE, OTHER_USER),
    )
    await runner.handle_update(_update(text=f"/start {await _payload(pool)}"))
    with pytest.raises(ValueError):
        await onboarding.complete_ctv_onboarding(pool, USER, PHONE, "Nguyễn An")
    await runner.handle_update(_update(contact=_contact()))
    assert conn.execute("SELECT phone,telegram_user_id FROM ctv_directory WHERE code=%s", (CODE,)).fetchone() == {"phone": None, "telegram_user_id": None}
    assert not conn.execute("SELECT 1 FROM authorized_users").fetchone()
    assert conn.execute("SELECT phone_normalized,telegram_user_id FROM phone_telegram_links").fetchall() == [
        {"phone_normalized": PHONE, "telegram_user_id": OTHER_USER},
    ]


async def test_core_rejects_invalid_phone_disabled_invite_and_cross_ctv_account_reuse(store):
    pool, conn = store
    assert await onboarding.complete_ctv_onboarding(pool, USER, PHONE, "Nguyễn An") is None
    links = await onboarding.ensure_ctv_bot_links(pool)
    payload = links[CODE].split("?start=", 1)[1]
    conn.execute("UPDATE ctv_directory SET is_active=FALSE WHERE code=%s", (CODE,))
    with pytest.raises(ValueError):
        await onboarding.begin_ctv_onboarding(pool, USER, payload)
    conn.execute("UPDATE ctv_directory SET is_active=TRUE WHERE code=%s", (CODE,))
    assert await onboarding.begin_ctv_onboarding(pool, USER, payload) == CODE
    with pytest.raises(ValueError):
        await onboarding.complete_ctv_onboarding(pool, USER, "not-a-phone", "Nguyễn An")
    _assert_unlinked(conn)
    assert await onboarding.complete_ctv_onboarding(pool, USER, "+84 912 345 678", "Nguyễn An") == CODE
    assert await onboarding.begin_ctv_onboarding(pool, OTHER_USER, payload) == CODE
    with pytest.raises(ValueError):
        await onboarding.begin_ctv_onboarding(pool, USER, links[OTHER_CODE].split("?start=", 1)[1])
    assert conn.execute("SELECT telegram_user_id FROM ctv_bot_pending").fetchall() == [{"telegram_user_id": OTHER_USER}]
    assert conn.execute("SELECT username FROM authorized_users WHERE telegram_user_id=%s", (USER,)).fetchone()["username"] == CODE


async def test_concurrent_link_generation_keeps_one_stable_secret_per_active_code(store):
    pool, conn = store
    results = await asyncio.gather(*(onboarding.ensure_ctv_bot_links(pool) for _ in range(8)))
    links = results[0]
    assert all(result == links for result in results)
    assert set(links) == {CODE, OTHER_CODE}
    assert len(set(links.values())) == 2
    for code, url in links.items():
        match = re.fullmatch(r"https://t\.me/CTVContractTest_bot\?start=ctv_([A-Za-z0-9_-]{32})", url)
        assert match is not None
        assert conn.execute("SELECT bot_invite_token FROM ctv_directory WHERE code=%s", (code,)).fetchone()["bot_invite_token"] == match[1]
    assert await onboarding.ensure_ctv_bot_links(pool) == links


async def test_concurrent_claims_commit_exactly_one_complete_owner(store):
    pool, conn = store
    payload = await _payload(pool)
    assert await onboarding.begin_ctv_onboarding(pool, USER, payload) == CODE
    assert await onboarding.begin_ctv_onboarding(pool, OTHER_USER, payload) == CODE
    results = await asyncio.gather(
        onboarding.complete_ctv_onboarding(pool, USER, PHONE, "Nguyễn An"),
        onboarding.complete_ctv_onboarding(pool, OTHER_USER, OTHER_PHONE, "Nguyễn Bình"),
        return_exceptions=True,
    )
    assert sum(result == CODE for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    winner, phone = (USER, PHONE) if results[0] == CODE else (OTHER_USER, OTHER_PHONE)
    assert conn.execute("SELECT phone,telegram_user_id FROM ctv_directory WHERE code=%s", (CODE,)).fetchone() == {"phone": phone, "telegram_user_id": winner}
    assert conn.execute("SELECT telegram_user_id,username,role FROM authorized_users").fetchall() == [
        {"telegram_user_id": winner, "username": CODE, "role": "COLLABORATOR"},
    ]
    assert conn.execute("SELECT phone_normalized,telegram_user_id,source FROM phone_telegram_links").fetchall() == [
        {"phone_normalized": phone, "telegram_user_id": winner, "source": "verified_contact"},
    ]
    assert not conn.execute("SELECT 1 FROM ctv_bot_pending WHERE telegram_user_id=%s", (winner,)).fetchone()


async def _enroll(pool):
    await onboarding.begin_ctv_onboarding(pool, USER, await _payload(pool))
    assert await onboarding.complete_ctv_onboarding(pool, USER, PHONE, "CTV original") == CODE


def _identity_state(conn):
    return {
        table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        for table in ("ctv_directory", "authorized_users", "phone_telegram_links", "ctv_bot_pending", "system_audit_log")
    }


@pytest.mark.parametrize("invitation", [False, True])
async def test_registered_own_contact_replaces_account_without_old_account_access(store, runner, invitation, monkeypatch):
    pool, conn = store
    await _enroll(pool)
    # Keep the admin's stored representation, not just the normalized identity.
    assert await ctv.update_ctv_directory_entry(pool, CODE, "CTV original", "+84 912 345 678")
    conn.execute("INSERT INTO ctv_quotas(ctv_code,scope_type,max_submissions) VALUES (%s,'global',10)", (CODE,))
    quota = conn.execute("SELECT * FROM ctv_quotas").fetchall()
    previous = conn.execute("SELECT * FROM ctv_directory WHERE code=%s", (CODE,)).fetchone()
    old_binding = conn.execute("SELECT * FROM phone_telegram_links").fetchone()
    if invitation:
        assert await onboarding.begin_ctv_onboarding(pool, OTHER_USER, await _payload(pool)) == CODE
    before = _identity_state(conn)
    for update in (
        _update(OTHER_USER, contact=_contact(USER)),
        _update(OTHER_USER, contact={"phone_number": PHONE}),
        _update(OTHER_USER, contact=_contact(OTHER_USER), chat_type="group"),
        _update(OTHER_USER, text=PHONE),
    ):
        await runner.handle_update(update)
        assert _identity_state(conn) == before
    await runner.handle_update(_update(OTHER_USER, contact=_contact(OTHER_USER)))
    current = conn.execute("SELECT * FROM ctv_directory WHERE code=%s", (CODE,)).fetchone()
    assert current["telegram_user_id"] == OTHER_USER
    assert current["phone"] == previous["phone"]
    assert current["auth_revision"] != previous["auth_revision"]
    assert current["code"] == CODE and current["created_at"] == previous["created_at"]
    assert conn.execute("SELECT * FROM ctv_quotas").fetchall() == quota
    old_user = conn.execute("SELECT * FROM authorized_users WHERE telegram_user_id=%s", (USER,)).fetchone()
    assert old_user["is_active"] is False and old_user["username"] == CODE
    new_binding = conn.execute("SELECT * FROM phone_telegram_links").fetchone()
    assert new_binding["phone_normalized"] == PHONE and new_binding["telegram_user_id"] == OTHER_USER
    assert new_binding["updated_at"] > old_binding["updated_at"]
    # Stale process cache/legacy username must not revive the old account.
    monkeypatch.setattr(ctv, "_CTV_DIRECTORY", {CODE: {"telegram_user_id": USER}})
    assert await quotas.resolve_ctv_code_for_telegram(pool, USER) is None
    assert await quotas.resolve_ctv_code_for_telegram(pool, OTHER_USER) == CODE
    assert await runner.get_driver_phone(USER) is None
    audit = conn.execute("SELECT * FROM system_audit_log").fetchone()
    assert audit["action"] == "CTV_TELEGRAM_REPLACED" and audit["entity_id"] == CODE
    assert audit["details"]["previous_auth_revision"] == str(previous["auth_revision"])
    assert audit["details"]["auth_revision"] == str(current["auth_revision"])
    assert PHONE not in json.dumps(audit["details"])


async def test_replacement_invalidates_old_browser_session_and_pending_otp(store, runner, monkeypatch):
    pool, conn = store
    await _enroll(pool)
    monkeypatch.setattr(settings, "APP_SECRET", "ctv-rebind-test-secret-at-least-32-characters")
    monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", "123456:rebind_test_token")
    monkeypatch.setattr(ctv_auth, "get_db_pool", lambda: pool)
    redis = MemoryRedis([int(time.time())])
    monkeypatch.setattr(ctv_auth, "get_redis_client", lambda: redis)
    monkeypatch.setattr(ctv_auth.secrets, "randbelow", lambda _: 123456)
    deliveries = []
    monkeypatch.setattr(ctv_auth, "_send_otp", lambda token, user, otp: deliveries.append((user, otp)))
    identity, binding = await ctv_auth._identity(code=CODE)
    old_session = ctv_auth._issue_session(CODE, binding)
    challenge = await ctv_auth.start_login(request(), phone=PHONE)
    await runner.handle_update(_update(OTHER_USER, contact=_contact(OTHER_USER)))
    assert await ctv_auth.ctv_from_session(request(session=old_session)) is None
    with pytest.raises(HTTPException) as failure:
        await ctv_auth.finish_login(request(challenge=challenge), challenge, "123456")
    assert failure.value.status_code == 401
    new_challenge = await ctv_auth.start_login(request(), code=CODE)
    new_session = await ctv_auth.finish_login(request(challenge=new_challenge), new_challenge, "123456")
    assert (await ctv_auth.ctv_from_session(request(session=new_session)))["telegram_user_id"] == OTHER_USER
    assert deliveries == [(USER, "123456"), (OTHER_USER, "123456")]


@pytest.mark.parametrize("denial", [
    "phone_change", "invited_phone_change", "inactive", "duplicate", "inactive_duplicate",
    "other_ctv", "foreign_mapping", "paused_mapping", "unregistered_bound_account",
])
async def test_ctv_replacement_denials_roll_back_all_identity_state(store, denial):
    pool, conn = store
    await _enroll(pool)
    phone = PHONE
    user = OTHER_USER
    if denial in {"phone_change", "invited_phone_change"}:
        user, phone = USER, OTHER_PHONE
        if denial == "invited_phone_change":
            await onboarding.begin_ctv_onboarding(pool, user, await _payload(pool))
    elif denial == "inactive":
        conn.execute("UPDATE ctv_directory SET is_active=FALSE WHERE code=%s", (CODE,))
    elif denial in {"duplicate", "inactive_duplicate"}:
        conn.execute("UPDATE ctv_directory SET phone=%s,is_active=%s WHERE code=%s", ("+84 912 345 678", denial == "duplicate", OTHER_CODE))
    elif denial == "other_ctv":
        conn.execute("UPDATE ctv_directory SET telegram_user_id=%s WHERE code=%s", (OTHER_USER, OTHER_CODE))
    elif denial == "foreign_mapping":
        conn.execute("UPDATE phone_telegram_links SET telegram_user_id=79999")
    elif denial == "paused_mapping":
        conn.execute("UPDATE phone_telegram_links SET source='ownership_conflict'")
    elif denial == "unregistered_bound_account":
        conn.execute("UPDATE ctv_directory SET phone=NULL WHERE code=%s", (CODE,))
        await onboarding.begin_ctv_onboarding(pool, user, await _payload(pool))
    before = _identity_state(conn)
    with pytest.raises(ValueError):
        await onboarding.complete_ctv_onboarding(pool, user, phone, "Replacement")
    assert _identity_state(conn) == before


async def test_replacement_audit_failure_rolls_back_phone_and_authority(store):
    pool, conn = store
    await _enroll(pool)
    conn.execute("""CREATE FUNCTION reject_rebind_audit() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'audit unavailable'; END; $$""")
    conn.execute("CREATE TRIGGER reject_rebind BEFORE INSERT ON system_audit_log FOR EACH ROW EXECUTE FUNCTION reject_rebind_audit()")
    before = _identity_state(conn)
    with pytest.raises(psycopg.Error):
        await onboarding.complete_ctv_onboarding(pool, OTHER_USER, PHONE, "Replacement")
    assert _identity_state(conn) == before


@pytest.mark.parametrize("admin_user", [USER, OTHER_USER])
async def test_replacement_preserves_unrelated_telegram_administrator_privileges(store, admin_user):
    pool, conn = store
    await _enroll(pool)
    conn.execute(
        "INSERT INTO authorized_users(telegram_user_id,username,full_name,role,driver_phone,is_active) "
        "VALUES (%s,'administrator','Admin','ADMIN',%s,TRUE) ON CONFLICT(telegram_user_id) DO UPDATE "
        "SET username='administrator',full_name='Admin',role='ADMIN',driver_phone=EXCLUDED.driver_phone",
        (admin_user, OTHER_PHONE),
    )
    admin = conn.execute("SELECT * FROM authorized_users WHERE telegram_user_id=%s", (admin_user,)).fetchone()
    assert await onboarding.complete_ctv_onboarding(pool, OTHER_USER, PHONE, "Replacement") == CODE
    expected = {**admin, "roles": ["ADMIN", "COLLABORATOR"]} if admin_user == OTHER_USER else admin
    assert conn.execute("SELECT * FROM authorized_users WHERE telegram_user_id=%s", (admin_user,)).fetchone() == expected
    assert await quotas.resolve_ctv_code_for_telegram(pool, USER) is None


@pytest_asyncio.fixture
async def billing_store(store):
    pool, conn = store
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    schema = SCHEMA_PATH.read_text()
    for table in ("driver_identity_vault", "survey_submissions", "outbox_events", "collaborator_billing_ledger"):
        prefix = f"CREATE TABLE IF NOT EXISTS {table}"
        conn.execute(prefix + schema.split(prefix, 1)[1].split(");", 1)[0] + ");")
    await _enroll(pool)
    conn.execute(
        "INSERT INTO survey_submissions(submission_id,ctv_code,collaborator_id,status,work_owner) "
        "VALUES ('REBIND-PENDING',%s,%s,'APPROVED','outbox_v2'),('REBIND-SETTLED',%s,%s,'APPROVED','outbox_v2')",
        (CODE, USER, CODE, USER),
    )
    conn.execute(
        "INSERT INTO collaborator_billing_ledger(collaborator_id,submission_id,payout_amount_vnd,payout_status) "
        "VALUES (%s,'REBIND-SETTLED',1000,'PAID')", (USER,),
    )
    return pool, conn


def _billing_event(conn, sid="REBIND-PENDING", amount=1000):
    event_id = conn.execute(
        "INSERT INTO outbox_events(event_type,flow_target,aggregate_id,payload,status,claimed_by,lease_until) "
        "VALUES ('TEST_CTV_REBIND','BILLING',%s,'{}','PROCESSING','rebind-test',NOW()+INTERVAL '5 minutes') RETURNING id",
        (sid,),
    ).fetchone()["id"]
    return {
        "id": event_id, "claimed_by": "rebind-test", "aggregate_kind": "submission",
        "aggregate_id": sid, "flow_target": "BILLING", "event_type": "TEST_CTV_REBIND",
        "payload": {"collaborator_id": USER, "amount_vnd": amount, "decision_version": 0,
                    "policy": {"enabled": True, "version": "rebind-test"}},
    }


async def test_repeated_replacement_preserves_outstanding_billing_once_and_settled_receipts(billing_store, database, monkeypatch):
    pool, conn = billing_store
    settled = conn.execute("SELECT * FROM collaborator_billing_ledger").fetchall()
    attribution = conn.execute("SELECT submission_id,ctv_code,collaborator_id FROM survey_submissions ORDER BY submission_id").fetchall()
    event = _billing_event(conn)
    await onboarding.complete_ctv_onboarding(pool, OTHER_USER, PHONE, "Replacement")
    third_user = OTHER_USER + 1
    await onboarding.complete_ctv_onboarding(pool, third_user, PHONE, "Replacement again")
    async with pool.connection() as connection:
        row = dict(conn.execute("SELECT * FROM survey_submissions WHERE submission_id='REBIND-PENDING'").fetchone())
        assert await durable_work._billing_collaborator(connection, row) == USER
        assert await durable_work._recipient(connection, row, {"source": "ctv"}) == (third_user, None)
    result = await outbox_delivery._billing(database, event)
    assert result.outcome == "SUCCEEDED"
    assert (await outbox_delivery._billing(database, event)).receipt == result.receipt
    rows = conn.execute("SELECT * FROM collaborator_billing_ledger ORDER BY submission_id").fetchall()
    assert len(rows) == 2
    assert next(row for row in rows if row["submission_id"] == "REBIND-SETTLED") == settled[0]
    assert all(row["collaborator_id"] == USER for row in rows)
    assert conn.execute("SELECT submission_id,ctv_code,collaborator_id FROM survey_submissions ORDER BY submission_id").fetchall() == attribution
    # Billing never changes a settled receipt, amount, or status on retry.
    settled_event = _billing_event(conn, "REBIND-SETTLED")
    settled_result = await outbox_delivery._billing(database, settled_event)
    assert settled_result.outcome == "SUCCEEDED"
    assert conn.execute("SELECT * FROM collaborator_billing_ledger WHERE submission_id='REBIND-SETTLED'").fetchone() == settled[0]
    conflict = await outbox_delivery._billing(database, _billing_event(conn, "REBIND-SETTLED", 2000))
    assert conflict.reason == "BILLING_RECEIPT_CONFLICT"
    assert conn.execute("SELECT * FROM collaborator_billing_ledger WHERE submission_id='REBIND-SETTLED'").fetchone() == settled[0]
    # A pending CTV verdict snapshot cannot disclose to a retired chat.
    notification = {"aggregate_id": "REBIND-PENDING", "payload": {"chat_id": USER, "message_class": "PARTY_VERDICT"}}
    assert not await outbox_delivery._binding_valid(database, notification)
    notification["payload"]["chat_id"] = third_user
    assert await outbox_delivery._binding_valid(database, notification)
    from routers import survey
    monkeypatch.setattr(survey, "get_db_pool", lambda: pool)
    assert await survey.lookup_telegram_user_for_collaborator(CODE) == third_user


@pytest.mark.parametrize("invalidation", [
    "ordinary_inactive", "directory_disabled", "directory_reenabled", "phone_changed_back",
    "telegram_changed_back", "deleted_recreated", "current_user_inactive", "current_user_not_collaborator",
    "wrong_ctv", "missing_provenance", "malformed_provenance", "broken_id_edge", "broken_revision_edge", "cyclic_provenance",
])
async def test_outstanding_billing_exception_fails_closed_outside_valid_rebind(billing_store, database, invalidation):
    pool, conn = billing_store
    settled = conn.execute("SELECT * FROM collaborator_billing_ledger").fetchall()
    if invalidation == "ordinary_inactive":
        conn.execute("UPDATE authorized_users SET is_active=FALSE WHERE telegram_user_id=%s", (USER,))
    else:
        await onboarding.complete_ctv_onboarding(pool, OTHER_USER, PHONE, "Replacement")
        if invalidation in {"directory_disabled", "directory_reenabled"}:
            assert await ctv.update_ctv_directory_entry(pool, CODE, "CTV", PHONE, is_active=False)
            if invalidation == "directory_reenabled":
                assert await ctv.update_ctv_directory_entry(pool, CODE, "CTV", PHONE, is_active=True)
        elif invalidation == "phone_changed_back":
            assert await ctv.update_ctv_directory_entry(pool, CODE, "CTV", OTHER_PHONE)
            assert await ctv.update_ctv_directory_entry(pool, CODE, "CTV", PHONE)
        elif invalidation == "telegram_changed_back":
            conn.execute("UPDATE ctv_directory SET telegram_user_id=79999 WHERE code=%s", (CODE,))
            conn.execute("UPDATE ctv_directory SET telegram_user_id=%s WHERE code=%s", (OTHER_USER, CODE))
        elif invalidation == "deleted_recreated":
            assert await ctv.delete_ctv_directory_entry(pool, CODE)
            assert await ctv.create_ctv_directory_entry(pool, CODE, "Recreated", PHONE, OTHER_USER)
        elif invalidation == "current_user_inactive":
            conn.execute("UPDATE authorized_users SET is_active=FALSE WHERE telegram_user_id=%s", (OTHER_USER,))
        elif invalidation == "current_user_not_collaborator":
            conn.execute("UPDATE authorized_users SET role='DRIVER' WHERE telegram_user_id=%s", (OTHER_USER,))
        elif invalidation == "wrong_ctv":
            conn.execute("UPDATE survey_submissions SET ctv_code=%s WHERE submission_id='REBIND-PENDING'", (OTHER_CODE,))
        elif invalidation == "missing_provenance":
            conn.execute("DELETE FROM system_audit_log")
        elif invalidation == "malformed_provenance":
            conn.execute("UPDATE system_audit_log SET details=details-'previous_auth_revision'")
        elif invalidation in {"broken_id_edge", "broken_revision_edge"}:
            await onboarding.complete_ctv_onboarding(pool, OTHER_USER + 1, PHONE, "Third account")
            key, value = ("previous_telegram_user_id", "79999") if invalidation == "broken_id_edge" else ("previous_auth_revision", json.dumps(str(uuid4())))
            conn.execute(
                "UPDATE system_audit_log SET details=jsonb_set(details,ARRAY[%s],%s::jsonb) "
                "WHERE details->>'telegram_user_id'=%s", (key, value, str(OTHER_USER + 1)),
            )
        elif invalidation == "cyclic_provenance":
            audit = conn.execute("SELECT * FROM system_audit_log").fetchone()
            detail = audit["details"]
            conn.execute(
                "INSERT INTO system_audit_log(component_name,action,entity_type,entity_id,actor_type,details) "
                "VALUES ('ctv_onboarding','CTV_TELEGRAM_REPLACED','CTV',%s,'SYSTEM',%s::jsonb)",
                (CODE, json.dumps({
                    "registered_phone_verified": True, "previous_telegram_user_id": OTHER_USER,
                    "telegram_user_id": USER, "previous_auth_revision": detail["auth_revision"],
                    "auth_revision": detail["previous_auth_revision"],
                })),
            )
    row = dict(conn.execute("SELECT * FROM survey_submissions WHERE submission_id='REBIND-PENDING'").fetchone())
    async with pool.connection() as connection:
        assert await durable_work._billing_collaborator(connection, row) is None
    result = await outbox_delivery._billing(database, _billing_event(conn))
    assert result.reason == "COLLABORATOR_INACTIVE"
    assert conn.execute("SELECT * FROM collaborator_billing_ledger").fetchall() == settled


async def test_replacement_never_reactivates_an_administratively_disabled_incoming_account(store):
    pool, conn = store
    await _enroll(pool)
    conn.execute(
        "INSERT INTO authorized_users(telegram_user_id,full_name,role,is_active) VALUES (%s,'Disabled','COLLABORATOR',FALSE)",
        (OTHER_USER,),
    )
    before = _identity_state(conn)
    with pytest.raises(ValueError, match="inactive"):
        await onboarding.complete_ctv_onboarding(pool, OTHER_USER, PHONE, "Replacement")
    assert _identity_state(conn) == before


async def test_numeric_ctv_code_resolves_via_directory_not_telegram_id(store):
    pool, conn = store
    numeric_code = "1001"
    conn.execute(
        "INSERT INTO authorized_users(telegram_user_id,full_name,role,is_active) VALUES (%s,'Other TG','COLLABORATOR',TRUE)",
        (int(numeric_code),),
    )
    conn.execute(
        "INSERT INTO authorized_users(telegram_user_id,full_name,role,is_active) VALUES (%s,'Actual CTV','COLLABORATOR',TRUE)",
        (OTHER_USER,),
    )
    assert await ctv.create_ctv_directory_entry(pool, numeric_code, "Numeric CTV", OTHER_PHONE, OTHER_USER)
    async with pool.connection() as connection:
        assert await durable_work._recipient(
            connection, {"ctv_code": numeric_code, "collaborator_id": USER}, {"source": "ctv"},
        ) == (OTHER_USER, None)
        assert await durable_work._recipient(
            connection, {"ctv_code": None, "collaborator_id": int(numeric_code)}, {"source": "ctv"},
        ) == (int(numeric_code), None)
    assert await ctv.update_ctv_directory_entry(pool, numeric_code, "Numeric CTV", OTHER_PHONE, is_active=False)
    async with pool.connection() as connection:
        assert await durable_work._recipient(
            connection, {"ctv_code": numeric_code, "collaborator_id": int(numeric_code)}, {"source": "ctv"},
        ) == (None, "UNLINKED_RECIPIENT")


async def test_multi_role_admin_and_driver_ctv_rebind_and_billing(billing_store, database):
    pool, conn = billing_store
    admin_user = OTHER_USER
    driver_user = OTHER_USER + 1
    conn.execute(
        "INSERT INTO authorized_users(telegram_user_id,username,full_name,role,is_active) "
        "VALUES (%s,'ops_admin','Ops Admin','ADMIN',TRUE),(%s,'drv_one','Driver One','DRIVER',TRUE)",
        (admin_user, driver_user),
    )
    # Rebinding onto an active ADMIN grants COLLABORATOR while keeping ADMIN and billing eligibility.
    assert await onboarding.complete_ctv_onboarding(pool, admin_user, PHONE, "CTV Name") == CODE
    admin_row = conn.execute("SELECT username,full_name,role,roles,is_active FROM authorized_users WHERE telegram_user_id=%s", (admin_user,)).fetchone()
    assert admin_row == {
        "username": "ops_admin", "full_name": "Ops Admin", "role": "ADMIN",
        "roles": ["ADMIN", "COLLABORATOR"], "is_active": True,
    }
    # Outstanding submission from old CTV account remains billable through the admin+CTV owner.
    result = await outbox_delivery._billing(database, _billing_event(conn))
    assert result.outcome == "SUCCEEDED"
    # New submission under the dual-role admin+CTV is directly billable while active.
    conn.execute(
        "INSERT INTO survey_submissions(submission_id,ctv_code,collaborator_id,status,work_owner) "
        "VALUES ('REBIND-ADMIN-DIRECT',%s,%s,'APPROVED','outbox_v2'),('REBIND-ADMIN-NEW',%s,%s,'APPROVED','outbox_v2')",
        (CODE, admin_user, CODE, admin_user),
    )
    direct_event = _billing_event(conn, "REBIND-ADMIN-DIRECT")
    direct_event["payload"]["collaborator_id"] = admin_user
    assert (await outbox_delivery._billing(database, direct_event)).outcome == "SUCCEEDED"
    admin_event = _billing_event(conn, "REBIND-ADMIN-NEW")
    admin_event["payload"]["collaborator_id"] = admin_user
    # Rebinding away from the admin onto a DRIVER preserves ADMIN on admin_user and grants COLLABORATOR+DRIVER on driver_user.
    assert await onboarding.complete_ctv_onboarding(pool, driver_user, PHONE, "Driver CTV") == CODE
    assert conn.execute("SELECT role,roles,is_active FROM authorized_users WHERE telegram_user_id=%s", (admin_user,)).fetchone() == {
        "role": "ADMIN", "roles": ["ADMIN"], "is_active": True,
    }
    assert conn.execute("SELECT role,roles,is_active FROM authorized_users WHERE telegram_user_id=%s", (driver_user,)).fetchone() == {
        "role": "COLLABORATOR", "roles": ["COLLABORATOR", "DRIVER"], "is_active": True,
    }
    # Outstanding submission made while admin held the CTV still bills through the verified rebind chain.
    assert (await outbox_delivery._billing(database, admin_event)).outcome == "SUCCEEDED"
    # Emptying roles via UPDATE must fail closed instead of silently restoring the revoked role.
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE authorized_users SET roles = '{}'::text[] WHERE telegram_user_id=%s", (admin_user,))
    # Rebinding away from DRIVER+COLLABORATOR clears the CTV code username on the retained DRIVER row.
    third_user = OTHER_USER + 2
    assert await onboarding.complete_ctv_onboarding(pool, third_user, PHONE, "Third CTV") == CODE
    assert conn.execute("SELECT username,role,roles,is_active FROM authorized_users WHERE telegram_user_id=%s", (driver_user,)).fetchone() == {
        "username": None, "role": "DRIVER", "roles": ["DRIVER"], "is_active": True,
    }


async def test_inactive_admin_account_cannot_bind_as_ctv(store):
    pool, conn = store
    await _enroll(pool)
    conn.execute(
        "INSERT INTO authorized_users(telegram_user_id,full_name,role,is_active) VALUES (%s,'Disabled Admin','ADMIN',FALSE)",
        (OTHER_USER,),
    )
    before = _identity_state(conn)
    with pytest.raises(ValueError, match="inactive"):
        await onboarding.complete_ctv_onboarding(pool, OTHER_USER, PHONE, "Replacement")
    assert _identity_state(conn) == before



