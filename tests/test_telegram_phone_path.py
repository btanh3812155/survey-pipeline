# -*- coding: utf-8 -*-
"""Telegram phone-target invariants: CTV identifiers never attach to driver rows."""

import inspect

import pytest

from core.ctv import CTV_CODES, seed_ctv_directory
from services.telegram_polling import DriverBotRunner


class RecordingCursor:
    def __init__(self):
        self.calls = []
        self.rowcount = 0

    async def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        self.rowcount = 0

    async def fetchall(self):
        return []


@pytest.mark.asyncio
async def test_seed_clears_ctv_code_from_non_collaborator_rows():
    """A canonical CTV code may only remain on an explicit COLLABORATOR row."""
    cur = RecordingCursor()
    await seed_ctv_directory(cur)

    updates = [
        (sql, params)
        for sql, params in cur.calls
        if "UPDATE authorized_users" in sql
    ]
    assert len(updates) == 1
    sql, params = updates[0]
    assert "UPPER(TRIM(username)) = ANY(%s)" in sql
    assert "UPPER(COALESCE(role, '')) <> 'COLLABORATOR'" in sql
    assert params == (list(CTV_CODES),)


def test_bind_collaborator_sets_role_in_the_same_upsert():
    """The only CTV-code writer must promote the row to COLLABORATOR atomically."""
    src = inspect.getsource(DriverBotRunner.bind_collaborator)
    assert "username = EXCLUDED.username" in src
    assert "role = 'COLLABORATOR'" in src
