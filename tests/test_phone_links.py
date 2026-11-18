# -*- coding: utf-8 -*-
"""Standalone phone <-> Telegram mapping tests."""

from unittest.mock import AsyncMock, patch

import pytest

from core.phone_links import (
    ensure_phone_link_schema,
    get_telegram_id_for_phone,
    normalize_vn_phone,
    upsert_phone_telegram_link,
)
from services.telegram_polling import DriverBotRunner


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
async def test_mapping_lookup_is_independent_of_driver_and_ctv_rows():
    pool = FakePool(row=(9241303232,))
    resolved = await get_telegram_id_for_phone("+84 932 090 967", db_pool=pool)

    assert resolved == 9241303232
    statements = [sql for sql, _ in pool.conn.cur.calls]
    assert any("phone_telegram_links" in sql for sql in statements)
    assert not any("authorized_users" in sql for sql in statements)
    assert not any("ctv_directory" in sql for sql in statements)
    lookup = [(sql, params) for sql, params in pool.conn.cur.calls if "SELECT telegram_user_id" in sql]
    assert lookup[0][1] == ("0932090967",)


@pytest.mark.asyncio
async def test_upsert_normalizes_and_uses_only_the_mapping_table():
    pool = FakePool()
    ok = await upsert_phone_telegram_link(
        "+84 932 090 967", 9241303232, source="command", db_pool=pool
    )

    assert ok is True
    assert pool.conn.commits == 1
    insert = [(sql, params) for sql, params in pool.conn.cur.calls if "INSERT INTO phone_telegram_links" in sql]
    assert insert
    assert insert[0][1] == ("0932090967", 9241303232, "command")
    assert not any(
        "authorized_users" in sql or "ctv_directory" in sql
        for sql, _ in pool.conn.cur.calls
    )


@pytest.mark.asyncio
async def test_schema_creation_is_idempotent_sql():
    pool = FakePool()
    assert await ensure_phone_link_schema(pool) is True
    statements = [sql for sql, _ in pool.conn.cur.calls]
    assert any("CREATE TABLE IF NOT EXISTS phone_telegram_links" in sql for sql in statements)
    assert any("idx_phone_telegram_links_tg" in sql for sql in statements)
    assert pool.conn.commits == 1


@pytest.mark.asyncio
async def test_contact_share_upserts_phone_telegram_link():
    runner = DriverBotRunner(bot_token="mock")
    runner.db_pool = object()
    upsert = AsyncMock(return_value=True)

    with patch.object(runner, "bind_driver_phone", AsyncMock()) as bind_phone, \
         patch.object(runner, "send_message", AsyncMock()) as send_message, \
         patch("core.phone_links.upsert_phone_telegram_link", upsert):
        await runner.handle_contact_sharing(
            chat_id=1,
            user_id=9241303232,
            contact={"phone_number": "+84 932 090 967", "first_name": "Jerry"},
            from_user={"first_name": "Jerry"},
        )

    bind_phone.assert_awaited_once()
    upsert.assert_awaited_once_with(
        "+84 932 090 967", 9241303232, source="contact_share", db_pool=runner.db_pool
    )
    send_message.assert_awaited_once()


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
    assert "liên kết" in send_message.await_args[0][1].lower()
