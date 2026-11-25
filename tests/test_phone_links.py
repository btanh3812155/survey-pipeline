# -*- coding: utf-8 -*-
"""Standalone phone <-> Telegram mapping tests."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
import os
import secrets
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from psycopg_pool import AsyncConnectionPool

from core.phone_links import (
    VERIFIED_PHONE_LINK_SOURCES,
    CONFLICTED_PHONE_LINK_SOURCE,
    ensure_phone_link_schema,
    get_telegram_id_for_phone,
    get_verified_phone_binding_for_telegram_user,
    get_verified_phone_for_telegram_user,
    normalize_vn_phone,
    revoke_phone_telegram_link,
    upsert_phone_telegram_link,
)
from services.telegram_polling import DriverBotRunner
from tests.postgres_harness import SCHEMA_PATH, resolve_test_database_url


class FakeCursor:
    def __init__(self, row=None):
        self.row = row
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return self.row


class FakeConn:
    def __init__(self, row=None):
        self.cur = FakeCursor(row)
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def cursor(self):
        return self.cur

    async def commit(self):
        self.commits += 1


class FakePool:
    def __init__(self, row=None):
        self.conn = FakeConn(row)

    def connection(self):
        return self.conn


@pytest.mark.asyncio
async def test_notification_target_accepts_unverified_binding():
    from core.phone_links import get_notification_telegram_target

    aware = datetime(2026, 11, 21, 8, 42, tzinfo=timezone.utc)
    pool = FakePool(row=(8094247707, "contact_share"))
    assert await get_notification_telegram_target(
        "0961281310", db_pool=pool, submitted_at=aware
    ) == (8094247707, False)
    pool = FakePool(row=(8094247707, "verified_contact"))
    assert await get_notification_telegram_target(
        "0961281310", db_pool=pool, submitted_at=aware
    ) == (8094247707, True)
    assert await get_notification_telegram_target(
        "0961281310", db_pool=FakePool(row=None), submitted_at=aware
    ) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0932090967", "0932090967"),
        ("+84 932 090 967", "0932090967"),
        ("+84932090967", "0932090967"),
        ("84932090967", "0932090967"),
        ("0084932090967", "0932090967"),
        ("932 090 967", "0932090967"),
        ("0961281318", "0961281318"),
        ("12345", None),
        ("abc", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_vn_phone(raw, expected):
    assert normalize_vn_phone(raw) == expected


@pytest.mark.asyncio
async def test_mapping_lookup_is_trusted_and_independent_of_driver_and_ctv_rows():
    pool = FakePool(row=(9241303232,))
    resolved = await get_telegram_id_for_phone("+84 932 090 967", db_pool=pool)

    assert resolved == 9241303232
    statements = [sql for sql, _ in pool.conn.cur.calls]
    assert any("phone_telegram_links" in sql for sql in statements)
    assert not any("authorized_users" in sql for sql in statements)
    assert not any("ctv_directory" in sql for sql in statements)
    lookup = [(sql, params) for sql, params in pool.conn.cur.calls if "SELECT telegram_user_id" in sql]
    assert lookup[0][1][:3] == ("0932090967", *VERIFIED_PHONE_LINK_SOURCES)
    assert "source IN" in lookup[0][0]
    assert "telegram_user_id > 0" in lookup[0][0]


@pytest.mark.asyncio
async def test_reverse_lookup_uses_trusted_sources_and_latest_mapping_order():
    pool = FakePool(row=("0932090967", datetime(2020, 1, 1, tzinfo=timezone.utc)))

    resolved = await get_verified_phone_for_telegram_user(9241303232, db_pool=pool)

    assert resolved == "0932090967"
    lookup = pool.conn.cur.calls[0]
    assert lookup[1] == (9241303232, list(VERIFIED_PHONE_LINK_SOURCES))
    await get_verified_phone_binding_for_telegram_user(9241303232, db_pool=pool, sources=("verified_contact",))
    assert pool.conn.cur.calls[-1][1] == (9241303232, ["verified_contact"])
    assert "telegram_user_id > 0" in lookup[0]
    assert "ORDER BY updated_at DESC, phone_normalized ASC" in lookup[0]
    assert await get_verified_phone_for_telegram_user(0, db_pool=pool) is None


@pytest.mark.asyncio
async def test_upsert_normalizes_and_conditionally_returns_stored_row():
    pool = FakePool(row=("0932090967",))
    ok = await upsert_phone_telegram_link(
        "+84 932 090 967", 9241303232, source="command", db_pool=pool
    )

    assert ok is True
    assert pool.conn.commits == 1
    insert = pool.conn.cur.calls[0]
    assert "INSERT INTO phone_telegram_links" in insert[0]
    assert insert[1][:3] == ("0932090967", 9241303232, "command")
    assert "command" not in VERIFIED_PHONE_LINK_SOURCES
    assert not any(
        "authorized_users" in sql or "ctv_directory" in sql
        for sql, _ in pool.conn.cur.calls
    )


@pytest.mark.asyncio
async def test_upsert_reports_refused_conflict_when_returning_no_row():
    pool = FakePool()

    assert await upsert_phone_telegram_link(
        "0932090967", 9241303233, source="command", db_pool=pool
    ) is False
    assert pool.conn.commits == 1

@pytest.mark.asyncio
async def test_schema_creation_is_idempotent_sql():
    pool = FakePool()
    assert await ensure_phone_link_schema(pool) is True
    statements = [sql for sql, _ in pool.conn.cur.calls]
    assert any("CREATE TABLE IF NOT EXISTS phone_telegram_links" in sql for sql in statements)
    assert any("idx_phone_telegram_links_tg" in sql for sql in statements)
    assert pool.conn.commits == 1


@pytest.mark.asyncio
async def test_contact_share_trusts_only_matching_user_identity():
    runner = DriverBotRunner(bot_token="mock")
    runner.db_pool = object()
    upsert = AsyncMock(return_value=True)

    with patch.object(runner, "send_message", AsyncMock()) as send_message, \
         patch("core.ctv_onboarding.complete_ctv_onboarding", AsyncMock(return_value=None)), \
         patch("core.phone_links.upsert_phone_telegram_link", upsert):
        await runner.handle_contact_sharing(
            chat_id=9241303232,
            user_id=9241303232,
            contact={"phone_number": "+84 932 090 967", "first_name": "Jerry", "user_id": 9241303232},
            from_user={"first_name": "Jerry", "id": 9241303232},
        )

    upsert.assert_awaited_once_with(
        "0932090967", 9241303232, source="verified_contact", db_pool=runner.db_pool
    )
    send_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "contact,from_user,user_id",
    [
        ({"phone_number": "0932090967"}, {"id": 9241303232}, 9241303232),
        ({"phone_number": "0932090967", "user_id": 9241303233}, {"id": 9241303232}, 9241303232),
        ({"phone_number": "0932090967", "user_id": 9241303232}, {}, 9241303232),
        ({"phone_number": "0932090967", "user_id": 9241303232}, {"id": 9241303233}, 9241303232),
        ({"phone_number": "0932090967", "user_id": 9241303232}, {"id": 9241303232}, 9241303233),
    ],
)
async def test_contact_share_rejects_missing_or_nonmatching_identity(contact, from_user, user_id):
    runner = DriverBotRunner(bot_token="mock")
    runner.db_pool = object()
    upsert = AsyncMock(return_value=True)

    with patch.object(runner, "send_message", AsyncMock()), \
         patch("core.phone_links.upsert_phone_telegram_link", upsert):
        await runner.handle_contact_sharing(
            chat_id=1, user_id=user_id, contact=contact, from_user=from_user
        )

    upsert.assert_not_awaited()

@pytest.mark.asyncio
async def test_linkphone_command_upserts_normalized_phone():
    runner = DriverBotRunner(bot_token="mock")
    runner.db_pool = object()
    upsert = AsyncMock(return_value=True)

    with patch.object(runner, "send_message", AsyncMock()) as send_message, \
         patch("core.phone_links.upsert_phone_telegram_link", upsert):
        await runner.handle_driver_command(
            chat_id=1,
            user_id=9241303232,
            text="/linkphone +84 932 090 967",
            from_user={"first_name": "Jerry"},
        )

    upsert.assert_awaited_once_with(
        "0932090967", 9241303232, source="command", db_pool=runner.db_pool
    )
    assert "chưa xác thực" in send_message.await_args[0][1].lower()
    assert "không cho phép nhận kết quả hay tra cứu" in send_message.await_args[0][1]
    assert "command" not in VERIFIED_PHONE_LINK_SOURCES


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_real_postgres_enforces_phone_link_trust_boundary(monkeypatch):
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("UPGRADE_TEST_DATABASE_URL missing while UPGRADE_TEST_REQUIRE=1")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL to run phone-link PostgreSQL coverage")

    dsn = resolve_test_database_url()
    pool = AsyncConnectionPool(conninfo=dsn, min_size=1, max_size=2, open=False)
    phone_start = secrets.randbelow(100_000_000)
    phones = [f"09{(phone_start + i) % 100_000_000:08d}" for i in range(7)]
    user_ids = [secrets.randbelow(2**60) + 1 for _ in range(7)]
    try:
        await pool.open()
        await pool.wait()
        assert await ensure_phone_link_schema(pool) is True
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM phone_telegram_links WHERE phone_normalized = ANY(%s);",
                (phones,),
            )
            await conn.commit()

        for phone, user_id, source in (
            (phones[0], user_ids[0], "contact_share"),
            (phones[1], user_ids[1], "command"),
            (phones[2], user_ids[2], "getChat"),
            (phones[6], user_ids[4], "legacy"),
        ):
            assert await upsert_phone_telegram_link(
                phone, user_id, source=source, db_pool=pool
            ) is True
            assert await get_telegram_id_for_phone(phone, db_pool=pool) is None
            assert await get_verified_phone_for_telegram_user(user_id, db_pool=pool) is None

        assert await upsert_phone_telegram_link(
            phones[3], user_ids[3], source="verified_contact", db_pool=pool
        ) is True
        assert await upsert_phone_telegram_link(
            phones[4], user_ids[3], source="admin_verified", db_pool=pool
        ) is True
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE phone_telegram_links SET updated_at = '2020-01-01T00:00:00Z' "
                "WHERE phone_normalized = %s;",
                (phones[3],),
            )
            await conn.execute(
                "UPDATE phone_telegram_links SET updated_at = '2021-01-01T00:00:00Z' "
                "WHERE phone_normalized = %s;",
                (phones[4],),
            )
            await conn.commit()
        assert await get_telegram_id_for_phone(phones[4], db_pool=pool) == user_ids[3]
        assert await get_verified_phone_for_telegram_user(user_ids[3], db_pool=pool) == phones[4]
        # New numbers retain the same account's earlier trusted bindings.
        assert await upsert_phone_telegram_link(
            phones[3], user_ids[3], source="verified_contact", db_pool=pool
        ) is True
        assert await get_telegram_id_for_phone(phones[3], db_pool=pool) == user_ids[3]
        assert await get_verified_phone_for_telegram_user(user_ids[3], db_pool=pool) == phones[4]
        assert (await get_verified_phone_binding_for_telegram_user(user_ids[3], db_pool=pool))[1] == datetime(2021, 1, 1, tzinfo=timezone.utc)
        from core import phone_links
        from routers import survey

        sender = AsyncMock(return_value=True)
        monkeypatch.setattr(phone_links, "get_db_pool", lambda: pool)
        monkeypatch.setattr(
            survey,
            "settings",
            SimpleNamespace(
                TELEGRAM_DRIVER_BOT_TOKEN="test-driver-token",
                TELEGRAM_BOT_TOKEN="test-bot-token",
                TELEGRAM_ADMIN_BOT_TOKEN=None,
            ),
        )
        monkeypatch.setattr(survey, "send_telegram_direct_message", sender)
        main_module = sys.modules.get("main")
        if main_module is not None and hasattr(main_module, "send_telegram_direct_message"):
            monkeypatch.setattr(main_module, "send_telegram_direct_message", sender)

        for phone in (*phones[:3], phones[6]):
            await survey.dispatch_submission_telegram_notifications(
                submission_id="phone-link-test",
                status_str="APPROVED",
                collaborator_id=phone,
                recipient_kind="driver",
                submitted_at=datetime(2021, 1, 1, tzinfo=timezone.utc),
            )
        sender.assert_not_awaited()
        await survey.dispatch_submission_telegram_notifications(
            submission_id="phone-link-test",
            status_str="APPROVED",
            collaborator_id=phones[3],
            recipient_kind="driver",
            submitted_at=datetime(2021, 1, 1, tzinfo=timezone.utc),
        )
        sender.assert_awaited_once()
        assert sender.await_args.kwargs["chat_id"] == user_ids[3]

        for source in ("command", "unknown", "legacy", "getChat", "contact_share"):
            for claimed_user_id in (user_ids[3], user_ids[4]):
                assert await upsert_phone_telegram_link(
                    phones[3], claimed_user_id, source=source, db_pool=pool
                ) is False
        assert await get_telegram_id_for_phone(phones[3], db_pool=pool) == user_ids[3]
        assert await get_verified_phone_for_telegram_user(user_ids[4], db_pool=pool) is None

        assert await upsert_phone_telegram_link(
            phones[5], user_ids[5], source="verified_contact", db_pool=pool
        ) is True
        for source in VERIFIED_PHONE_LINK_SOURCES:
            assert await upsert_phone_telegram_link(
                phones[5], user_ids[6], source=source, db_pool=pool
            ) is False
        assert await get_telegram_id_for_phone(phones[5], db_pool=pool) is None
        assert await upsert_phone_telegram_link(phones[5], user_ids[5], source="verified_contact", db_pool=pool) is False
        assert await upsert_phone_telegram_link(phones[5], user_ids[6], source="command", db_pool=pool) is False
        async with pool.connection() as conn:
            row = await conn.execute(
                "SELECT telegram_user_id, source FROM phone_telegram_links WHERE phone_normalized = %s;", (phones[5],)
            )
            assert await row.fetchone() == (user_ids[5], CONFLICTED_PHONE_LINK_SOURCE)
        assert await revoke_phone_telegram_link(phones[5], user_ids[6], db_pool=pool) is False
        assert await revoke_phone_telegram_link(phones[5], user_ids[5], db_pool=pool) is True
        assert await upsert_phone_telegram_link(
            phones[5], user_ids[6], source="admin_verified", db_pool=pool
        ) is True
        assert await get_telegram_id_for_phone(phones[5], db_pool=pool) == user_ids[6]
        assert await get_verified_phone_for_telegram_user(user_ids[5], db_pool=pool) is None
        assert await get_verified_phone_for_telegram_user(user_ids[6], db_pool=pool) == phones[5]

        # Simultaneous first verified claims cannot both become the trusted owner.
        results = await asyncio.gather(
            upsert_phone_telegram_link(phones[6], user_ids[0], source="verified_contact", db_pool=pool),
            upsert_phone_telegram_link(phones[6], user_ids[1], source="verified_contact", db_pool=pool),
        )
        assert sorted(results) == [False, True]
        assert await get_telegram_id_for_phone(phones[6], db_pool=pool) is None
        async with pool.connection() as conn:
            row = await conn.execute(
                "SELECT telegram_user_id, source FROM phone_telegram_links WHERE phone_normalized = %s;", (phones[6],)
            )
            assert await row.fetchone() == (user_ids[0 if results[0] else 1], CONFLICTED_PHONE_LINK_SOURCE)
    finally:
        try:
            async with pool.connection() as conn:
                await conn.execute(
                    "DELETE FROM phone_telegram_links WHERE phone_normalized = ANY(%s);",
                    (phones,),
                )
                await conn.commit()
        finally:
            await pool.close()


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_real_postgres_reassignment_hides_previous_owner_history(monkeypatch, tmp_path):
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("UPGRADE_TEST_DATABASE_URL required for phone ownership tests")
        pytest.skip("requires the disposable PostgreSQL harness")

    pool = AsyncConnectionPool(conninfo=resolve_test_database_url(), min_size=1, max_size=2, open=False)
    phone = f"09{secrets.randbelow(100_000_000):08d}"
    owner_a, owner_b = (secrets.randbelow(2**48) + 1 for _ in range(2))
    old_id, new_id = f"TEST-H1-{phone}-A", f"TEST-H1-{phone}-B"
    policy = json.dumps({"_delivery_policy": {"source": "question", "question_id": "notify_phone"}, "notify_phone": phone})
    history_file = tmp_path / "dev_submissions.json"
    history_file.write_text(json.dumps([{
        "submission_id": "DEV-PREVIOUS-OWNER", "driver_phone": phone,
        "status": "APPROVED", "submitted_at": "2021-01-01T00:00:00+00:00",
    }]), encoding="utf-8")
    monkeypatch.setattr("services.telegram_polling.DEV_STORAGE_FILE", str(history_file))
    runner = DriverBotRunner(bot_token="mock", db_pool=pool)
    send, tracked = AsyncMock(), AsyncMock()
    monkeypatch.setattr(runner, "send_message", send)
    monkeypatch.setattr(runner, "send_tracked", tracked)
    await pool.open()
    try:
        await pool.wait()
        async with pool.connection() as conn:
            await conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
            await conn.commit()
        assert await ensure_phone_link_schema(pool)
        assert await upsert_phone_telegram_link(phone, owner_a, source="verified_contact", db_pool=pool)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE phone_telegram_links SET created_at = '2020-01-01T00:00:00Z', "
                "updated_at = '2020-01-01T00:00:00Z' WHERE phone_normalized = %s;", (phone,),
            )
            await conn.execute(
                "INSERT INTO survey_submissions (submission_id, driver_phone, status, created_at, form_data) "
                "VALUES (%s, %s, 'APPROVED', '2021-01-01T00:00:00Z', %s::jsonb);", (old_id, phone, policy),
            )
            await conn.commit()
        await runner.handle_driver_surveys_lookup(owner_a, owner_a)
        assert old_id in tracked.await_args.args[2]
        tracked.reset_mock()

        # Current possession is not permission to replace another verified identity.
        contact = {"user_id": owner_b, "phone_number": phone, "first_name": "New account"}
        sender = {"id": owner_b, "first_name": "New account"}
        await runner.handle_contact_sharing(owner_b, owner_b, contact, sender)
        assert "thành công" not in send.await_args.args[1]
        assert await get_telegram_id_for_phone(phone, db_pool=pool) is None
        async with pool.connection() as conn:
            row = await conn.execute(
                "SELECT telegram_user_id, source FROM phone_telegram_links WHERE phone_normalized = %s;", (phone,)
            )
            assert await row.fetchone() == (owner_a, CONFLICTED_PHONE_LINK_SOURCE)
        await runner.handle_driver_surveys_lookup(owner_b, owner_b)
        tracked.assert_not_awaited()

        # An operator explicitly revokes A; B then completes new self-verification.
        assert await revoke_phone_telegram_link(phone, owner_a, db_pool=pool)
        await runner.handle_contact_sharing(owner_b, owner_b, contact, sender)
        assert "thành công" in send.await_args.args[1]
        assert await get_verified_phone_for_telegram_user(owner_a, db_pool=pool) is None
        binding = await get_verified_phone_binding_for_telegram_user(owner_b, db_pool=pool)
        assert binding is not None and binding[0] == phone
        await runner.handle_driver_surveys_lookup(owner_b, owner_b)
        tracked.assert_not_awaited()  # Neither the old SQL row nor DEV fallback leaks.
        assert old_id not in send.await_args.args[1]
        assert "DEV-PREVIOUS-OWNER" not in send.await_args.args[1]

        # A later moderation verdict must not retarget old-owner data to B.
        from core import phone_links
        from routers import survey
        delivery = AsyncMock(return_value=True)
        monkeypatch.setattr(phone_links, "get_db_pool", lambda: pool)
        monkeypatch.setattr(survey.settings, "TELEGRAM_DRIVER_BOT_TOKEN", "123:offline-transfer")
        monkeypatch.setattr(survey, "send_telegram_direct_message", delivery)
        main_module = sys.modules.get("main")
        if main_module is not None:
            monkeypatch.setattr(main_module, "send_telegram_direct_message", delivery)
        await survey.dispatch_submission_telegram_notifications(
            submission_id=old_id, status_str="APPROVED", collaborator_id=phone,
            recipient_kind="driver", submitted_at=datetime(2021, 1, 1, tzinfo=timezone.utc),
        )
        delivery.assert_not_awaited()
        await survey.dispatch_submission_telegram_notifications(
            submission_id=new_id, status_str="APPROVED", collaborator_id=phone,
            recipient_kind="driver", submitted_at=binding[1] + timedelta(seconds=1),
        )
        delivery.assert_awaited_once()
        assert delivery.await_args.kwargs["chat_id"] == owner_b

        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO survey_submissions (submission_id, driver_phone, status, created_at, form_data) "
                "VALUES (%s, %s, 'APPROVED', %s, %s::jsonb);", (new_id, phone, binding[1] + timedelta(seconds=1), policy),
            )
            await conn.commit()
        await runner.handle_driver_surveys_lookup(owner_b, owner_b)
        assert new_id in tracked.await_args.args[2]
        assert old_id not in tracked.await_args.args[2]

        # H1 readiness observes the actual required relation, not just a fake row.
        from routers import system
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO schema_migrations(version, checksum) VALUES ('001', %s) "
                "ON CONFLICT (version) DO NOTHING;",
                (__import__("routers.system", fromlist=["MIGRATION_001_CHECKSUM"]).MIGRATION_001_CHECKSUM,),
            )
            await conn.commit()
        monkeypatch.setattr(system, "get_db_pool", lambda: pool)
        assert (await system.readiness_check())["status"] == "ready"
        async with pool.connection() as conn:
            await conn.execute("ALTER TABLE phone_telegram_links RENAME TO h1_mapping_temporarily_unavailable;")
            await conn.commit()
        try:
            response = await system.readiness_check()
            assert response.status_code == 503
            assert "phone_telegram_links" in json.loads(response.body)["missing"]
        finally:
            async with pool.connection() as conn:
                await conn.execute("ALTER TABLE h1_mapping_temporarily_unavailable RENAME TO phone_telegram_links;")
                await conn.commit()
    finally:
        try:
            async with pool.connection() as conn:
                await conn.execute("DELETE FROM survey_submissions WHERE submission_id = ANY(%s);", ([old_id, new_id],))
                await conn.execute("DELETE FROM phone_telegram_links WHERE phone_normalized = %s;", (phone,))
                await conn.commit()
        finally:
            await pool.close()
