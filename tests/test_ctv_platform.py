# -*- coding: utf-8 -*-
"""CTV platform layer tests: directory seeding/read, options, quotas, TG lookup, bots."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core import ctv as ctv_module
from core import quotas as quota_module
from core.ctv import (
    build_ctv_form_url,
    get_ctv_entry,
    get_ctv_options_for_form,
    get_ctv_codes,
    clear_ctv_directory_cache,
    ensure_ctv_platform_schema,
    seed_ctv_directory,
    set_ctv_directory_cache,
    set_ctv_form_links_cache,
    update_ctv_directory_entry,
)
from core.quotas import evaluate_quota, format_quota_report
from services.form_engine import DEFAULT_TRUST_BRIDGE_SCHEMA, FormSettings, FormSchema
from services.telegram_polling import DriverBotRunner, TelegramPollingRunner


NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


class FakeCursor:
    def __init__(self, pool):
        self.pool = pool
        self._rows = []
        self.rowcount = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.pool.executed.append((sql, params))
        normalized = " ".join(sql.split())
        self._rows = []
        self.rowcount = 0
        if normalized.startswith("UPDATE ctv_directory SET"):
            code = str(params[-1]) if params else ""
            target = code.upper()
            updated_rows = []
            for row in self.pool.directory_rows:
                if str(row[0]).upper() == target:
                    updated_rows.append(
                        (
                            row[0],
                            params[0] if params[0] is not None else None,
                            params[1],
                            params[2],
                            params[3],
                            row[5] if len(row) > 5 else None,
                        )
                    )
                    self.rowcount = 1
                else:
                    updated_rows.append(row)
            if self.rowcount:
                self.pool.directory_rows = updated_rows
        elif "SELECT DISTINCT ctv_code FROM survey_submissions" in normalized:
            self._rows = list(self.pool.submission_ctv_codes)
        elif "SELECT DISTINCT collaborator_id FROM survey_submissions" in normalized:
            self._rows = list(self.pool.submission_collaborators)
        elif "SELECT ctv_code, collaborator_id, form_data FROM survey_submissions" in normalized:
            self._rows = list(self.pool.submission_contact_rows)
        elif "FROM authorized_users" in normalized:
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
    def __init__(
        self,
        directory_rows=None,
        authorized_rows=None,
        link_rows=None,
        submission_ctv_codes=None,
        submission_collaborators=None,
        submission_contact_rows=None,
    ):
        self.executed = []
        self.commits = 0
        self.directory_rows = directory_rows or []
        self.authorized_rows = authorized_rows or []
        self.link_rows = link_rows or []
        self.submission_ctv_codes = submission_ctv_codes or []
        self.submission_collaborators = submission_collaborators or []
        self.submission_contact_rows = submission_contact_rows or []

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


@pytest.mark.asyncio
async def test_seed_submission_codes_idempotent_and_no_overwrite(monkeypatch):
    monkeypatch.setattr(ctv_module, "_iter_dev_submission_rows", lambda: iter(()))
    pool = FakePool(
        directory_rows=[
            ("CTV001_DUNG_HN", "Existing Name", "0901234567", 555, True, "seed:static"),
        ],
        submission_ctv_codes=[
            ("CTV001_Dũng_HN",),
            ("CTV001_Dũng_HN",),
            ("CTV002_Tuấn_HN",),
        ],
        submission_collaborators=[(999,), (999,), (None,)],
    )

    first_stats = await seed_ctv_directory(FakeCursor(pool))
    submission_inserts = [
        params for sql, params in pool.executed
        if "INSERT INTO ctv_directory" in sql and params and params[-1] == "seed:survey_submissions"
    ]
    assert [p[0] for p in submission_inserts] == [
        "CTV001_DUNG_HN",
        "CTV002_TUAN_HN",
        "999",
    ]
    assert first_stats["submission_codes"] == 3

    # Existing directory fields are only filled when NULL, never overwritten.
    submission_sql = [
        sql for sql, params in pool.executed
        if "INSERT INTO ctv_directory" in sql and params and params[-1] == "seed:survey_submissions"
    ]
    for sql in submission_sql:
        assert "ON CONFLICT (code) DO UPDATE SET" in sql
        assert "display_name = COALESCE(ctv_directory.display_name, EXCLUDED.display_name)" in sql
        assert "phone = COALESCE(ctv_directory.phone, EXCLUDED.phone)" in sql
        assert "telegram_user_id = COALESCE(ctv_directory.telegram_user_id, EXCLUDED.telegram_user_id)" in sql

    # Re-running stays idempotent: one upsert per distinct code, never duplicates.
    pool.executed.clear()
    second_stats = await seed_ctv_directory(FakeCursor(pool))
    second_inserts = [
        params[0] for sql, params in pool.executed
        if "INSERT INTO ctv_directory" in sql and params and params[-1] == "seed:survey_submissions"
    ]
    assert second_inserts == ["CTV001_DUNG_HN", "CTV002_TUAN_HN", "999"]
    assert second_stats["submission_codes"] == 3


@pytest.mark.asyncio
async def test_seed_phone_and_telegram_propagation_sources(monkeypatch):
    monkeypatch.setattr(
        ctv_module,
        "_iter_dev_submission_rows",
        lambda: iter([
            {"ctv_code": "DEV_001", "form_data": {"ctv_phone": "0908 111 222"}},
        ]),
    )
    pool = FakePool(
        authorized_rows=[
            (777, "CTV_AUTH", "CTV Auth", "0907 333 444", "COLLABORATOR"),
        ],
        submission_contact_rows=[
            ("CTV001_Dũng_HN", None, {"ctv_phone": "0901 234 567", "ctv_telegram": "88888"}),
            ("CTV002_Tuấn_HN", None, {"phone": "0908 888 888"}),
            (None, 42, {"ctv_telegram": "0907 777 777"}),
        ],
    )

    stats = await seed_ctv_directory(FakeCursor(pool))

    by_code = {}
    for sql, params in pool.executed:
        if "INSERT INTO ctv_directory" not in sql or not params:
            continue
        if "seed:authorized_users" in sql:
            by_code[params[0]] = params
        elif params[-1] in ("seed:survey_submissions", "seed:dev_submissions"):
            by_code[params[0]] = params

    assert by_code["CTV_AUTH"][2] == "0907333444"
    assert by_code["CTV001_DUNG_HN"][2] == "0901234567"
    assert by_code["CTV001_DUNG_HN"][3] == 88888
    assert by_code["CTV002_TUAN_HN"][2] == "0908888888"
    assert by_code["42"][2] == "0907777777"
    assert by_code["DEV_001"][2] == "0908111222"

    assert stats["phone_sources"]["authorized_users"] == 1
    assert stats["phone_sources"]["survey_submissions.form_data.ctv_phone"] == 1
    assert stats["phone_sources"]["survey_submissions.form_data.phone"] == 1
    assert stats["phone_sources"]["survey_submissions.form_data.ctv_telegram"] == 1
    assert stats["phone_sources"]["dev_submissions.form_data.ctv_phone"] == 1


@pytest.mark.asyncio
async def test_admin_update_refreshes_directory_cache():
    pool = FakePool(
        directory_rows=[("ALPHA_001", "Alpha", None, None, True, "seed:static")],
    )

    assert await update_ctv_directory_entry(
        pool,
        "alpha_001",
        display_name="Alpha Updated",
        phone="0909 123 456",
        telegram_user_id="55555",
        is_active=False,
    ) is True

    entry = get_ctv_entry("ALPHA_001")
    assert entry is not None
    assert entry["display_name"] == "Alpha Updated"
    assert entry["phone"] == "0909123456"
    assert entry["telegram_user_id"] == 55555
    assert entry["is_active"] is False

    assert await update_ctv_directory_entry(pool, "MISSING", display_name="X") is False


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
    assert 'method="post"' in response.text
    assert 'action="/admin/ctvs/TUAN_LM_HCM_001"' in response.text
    assert "/admin/ctvs" in response.text


@pytest.mark.asyncio
async def test_admin_update_endpoint_redirects_and_refreshes_cache():
    from routers import admin_ctvs

    pool = FakePool(
        directory_rows=[("ALPHA_001", "Alpha", None, None, True, "seed:static")],
    )
    with patch.object(admin_ctvs, "get_db_pool", return_value=pool):
        response = await admin_ctvs.update_admin_ctv(
            "ALPHA_001",
            display_name="Alpha Endpoint",
            phone="0909 000 111",
            telegram_user_id="66666",
            is_active="1",
            admin_user="admin",
        )

    assert response.status_code == 303
    entry = get_ctv_entry("ALPHA_001")
    assert entry["display_name"] == "Alpha Endpoint"
    assert entry["phone"] == "0909000111"
    assert entry["telegram_user_id"] == 66666
    assert entry["is_active"] is True
