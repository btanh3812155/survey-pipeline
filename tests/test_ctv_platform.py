# -*- coding: utf-8 -*-
"""CTV platform layer tests: directory seeding/read, options, quotas, TG lookup, bots."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core import ctv as ctv_module
from core import quotas as quota_module
from core.ctv import (
    build_ctv_form_url,
    get_ctv_options_for_form,
    get_ctv_codes,
    clear_ctv_directory_cache,
    ensure_ctv_platform_schema,
    set_ctv_directory_cache,
    set_ctv_form_links_cache,
)
from core.quotas import evaluate_quota, format_quota_report
from services.form_engine import DEFAULT_TRUST_BRIDGE_SCHEMA, FormSettings, FormSchema
from services.telegram_polling import DriverBotRunner, TelegramPollingRunner


NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


class FakeCursor:
    def __init__(self, pool):
        self.pool = pool
        self._rows = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.pool.executed.append((sql, params))
        normalized = " ".join(sql.split())
        self._rows = []
        if "FROM authorized_users" in normalized:
            self._rows = list(self.pool.authorized_rows)
        elif "FROM ctv_form_links" in normalized:
            self._rows = list(self.pool.link_rows)
        elif "FROM ctv_directory" in normalized:
            self._rows = list(self.pool.directory_rows)

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def cursor(self):
        return FakeCursor(self.pool)

    async def commit(self):
        self.pool.commits += 1


class FakePool:
    def __init__(self, directory_rows=None, authorized_rows=None, link_rows=None):
        self.executed = []
        self.commits = 0
        self.directory_rows = directory_rows or []
        self.authorized_rows = authorized_rows or []
        self.link_rows = link_rows or []

    def connection(self):
        return FakeConn(self)


@pytest.fixture(autouse=True)
def _reset_ctv_cache():
    clear_ctv_directory_cache()
    yield
    clear_ctv_directory_cache()


@pytest.mark.asyncio
async def test_schema_seed_and_cache_read_helpers():
    pool = FakePool(
        authorized_rows=[
            (111, "TUAN_LM_HCM_001", "Tuấn LM", "0901111111", "COLLABORATOR"),
            (222, "new_ctv_x", "CTV Mới", "0902222222", "COLLABORATOR"),
        ],
        directory_rows=[
            ("TUAN_LM_HCM_001", "Tuấn LM", "0901111111", 111, True, "seed:static"),
            ("NEW_CTV_X", "CTV Mới", "0902222222", 222, True, "seed:authorized_users"),
        ],
    )

    assert await ensure_ctv_platform_schema(pool) is True

    statements = [" ".join(sql.split()) for sql, _ in pool.executed]
    assert any("CREATE TABLE IF NOT EXISTS ctv_directory" in s for s in statements)
    assert any("CREATE TABLE IF NOT EXISTS form_tags" in s for s in statements)
    assert any("CREATE TABLE IF NOT EXISTS ctv_form_links" in s for s in statements)
    assert any("CREATE TABLE IF NOT EXISTS ctv_quotas" in s for s in statements)

    static_inserts = [
        params for sql, params in pool.executed
        if "INSERT INTO ctv_directory" in sql and "seed:static" in sql
    ]
    # 4 static seed codes + the two authorized_users CTV rows.
    assert len(static_inserts) == 4
    assert {p[0] for p in static_inserts} == set(ctv_module.CTV_CODES)
    authorized_upserts = [
        params for sql, params in pool.executed
        if "INSERT INTO ctv_directory" in sql and "seed:authorized_users" in sql
    ]
    assert {p[0] for p in authorized_upserts} == {"TUAN_LM_HCM_001", "NEW_CTV_X"}

    assert get_ctv_codes() == ["TUAN_LM_HCM_001", "NEW_CTV_X"]
    assert get_ctv_options_for_form(DEFAULT_TRUST_BRIDGE_SCHEMA) == [
        "TUAN_LM_HCM_001",
        "NEW_CTV_X",
    ]


def test_directory_driven_options_and_links_override_static():
    set_ctv_directory_cache(
        [
            {"code": "alpha_001", "display_name": "Alpha", "is_active": True},
            {"code": "beta_002", "display_name": "Beta", "is_active": False},
        ]
    )
    schema = FormSchema(
        form_id="tag_demo",
        title="Tag Demo",
        settings=FormSettings(tags=["income"]),
        questions=[],
    )
    assert get_ctv_options_for_form(schema) == ["ALPHA_001"]
    assert get_ctv_codes(active_only=False) == ["ALPHA_001", "BETA_002"]

    set_ctv_form_links_cache(
        [{"ctv_code": "alpha_001", "form_id": "tag_demo", "url": "https://x.test/ref/alpha"}]
    )
    assert build_ctv_form_url("ALPHA_001", "tag_demo") == "https://x.test/ref/alpha"
    assert build_ctv_form_url("BETA_002", "tag_demo") == (
        "https://survey.trustbridgevn.com/forms/tag_demo?ref=BETA_002"
    )


def test_quota_computation_form_tag_global_with_window():
    submissions = [
        {"form_id": "income_survey", "created_at": NOW - timedelta(days=2)},
        {"form_id": "income_survey", "created_at": NOW - timedelta(days=9)},
        {"form_id": "customer_feedback", "created_at": NOW - timedelta(days=1)},
        {"form_id": "income_survey", "created_at": NOW - timedelta(days=45)},  # outside window
    ]
    form_tags = {
        "income_survey": {"income", "gsm"},
        "customer_feedback": {"feedback"},
    }

    form_quota = evaluate_quota(
        {"id": 1, "scope_type": "form", "scope_value": "income_survey", "max_submissions": 5, "period_days": 30},
        submissions,
        form_tags,
        now=NOW,
    )
    assert form_quota["used"] == 2
    assert form_quota["remaining"] == 3

    tag_quota = evaluate_quota(
        {"id": 2, "scope_type": "tag", "scope_value": "income", "max_submissions": 1, "period_days": 30},
        submissions,
        form_tags,
        now=NOW,
    )
    assert tag_quota["used"] == 2
    assert tag_quota["remaining"] == -1

    global_quota = evaluate_quota(
        {"id": 3, "scope_type": "global", "scope_value": None, "max_submissions": 10, "period_days": 30},
        submissions,
        form_tags,
        now=NOW,
    )
    assert global_quota["used"] == 3
    assert global_quota["remaining"] == 7
    # Oldest counted submission (9 days ago) + 30-day rolling window.
    assert global_quota["reset_at"] == submissions[1]["created_at"] + timedelta(days=30)

    report = format_quota_report("ALPHA_001", "Alpha", [form_quota, tag_quota, global_quota])
    assert "ALPHA_001" in report
    assert "2/5" in report
    assert "2/1" in report
    assert "1/1" not in report
    assert "3/10" in report


@pytest.mark.asyncio
async def test_lookup_telegram_user_for_collaborator_prefers_directory():
    from routers import survey as survey_router

    pool = FakePool(directory_rows=[(555, "0901111111")])
    with patch.object(survey_router, "get_db_pool", return_value=pool):
        assert await survey_router.lookup_telegram_user_for_collaborator("tuan_lm_hcm_001") == 555

    # Directory phone-only row falls back to authorized_users phone resolution.
    pool = FakePool(directory_rows=[(None, "0901111111")])
    with patch.object(survey_router, "get_db_pool", return_value=pool), patch.object(
        survey_router, "lookup_telegram_user_by_phone", AsyncMock(return_value=777)
    ):
        assert await survey_router.lookup_telegram_user_for_collaborator("TUAN_LM_HCM_001") == 777


@pytest.mark.asyncio
async def test_driver_bot_quota_shows_own_quotas():
    runner = DriverBotRunner(bot_token="mock_driver_token")
    send = AsyncMock()
    with patch.object(runner, "send_message", send), patch(
        "core.quotas.resolve_ctv_code_for_telegram", AsyncMock(return_value="TUAN_LM_HCM_001")
    ), patch(
        "core.quotas.build_ctv_quota_report",
        AsyncMock(return_value="📊 <b>Hạn mức CTV</b> TUAN_LM_HCM_001"),
    ) as report:
        await runner.handle_driver_command(
            chat_id=42, user_id=111, text="/quota", from_user={"first_name": "CTV"}
        )
    report.assert_awaited_once_with(runner.db_pool, "TUAN_LM_HCM_001")
    assert "Hạn mức CTV" in send.await_args[0][1]


@pytest.mark.asyncio
async def test_admin_bot_quota_accepts_explicit_code():
    runner = TelegramPollingRunner(bot_token="mock_admin_token", role="admin")
    send = AsyncMock()
    with patch.object(runner, "send_message", send), patch(
        "core.quotas.resolve_ctv_code_from_ref", AsyncMock(return_value="ALPHA_001")
    ), patch(
        "core.quotas.build_ctv_quota_report",
        AsyncMock(return_value="📊 <b>Hạn mức CTV</b> ALPHA_001"),
    ) as report:
        await runner.handle_command(
            chat_id=-100, text="/quota @alpha_001", from_user_id=999
        )
    report.assert_awaited_once_with(runner.db_pool, "ALPHA_001")
    assert "ALPHA_001" in send.await_args[0][1]


def test_form_settings_tags_validation_and_serialization():
    settings = FormSettings(tags="Income, GSM ,income, Hồ Sơ")
    assert settings.tags == ["income", "gsm", "ho_so"]

    schema = FormSchema(
        form_id="tags_demo",
        title="Tags demo",
        settings=settings,
        questions=[],
    )
    dumped = schema.model_dump()
    assert dumped["settings"]["tags"] == ["income", "gsm", "ho_so"]

    with pytest.raises(ValueError):
        FormSettings(tags=["x" * 33])


def test_admin_ctv_directory_page_requires_auth_and_shows_static_fallback():
    from fastapi.testclient import TestClient
    from config import settings as app_settings
    from main import app

    client = TestClient(app)
    assert client.get("/admin/ctvs").status_code == 401

    response = client.get(
        "/admin/ctvs",
        auth=(app_settings.ADMIN_USERNAME, app_settings.ADMIN_PASSWORD),
    )
    assert response.status_code == 200
    assert "TUAN_LM_HCM_001" in response.text
    assert "Quản Lý CTV" in response.text
    assert "/admin/ctvs" in response.text
