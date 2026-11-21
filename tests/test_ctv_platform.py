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
    get_ctv_form_link,
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
        if normalized.startswith("UPDATE ctv_directory SET phone = COALESCE"):
            code = str(params[-1]) if params else ""
            updated_rows = []
            for row in self.pool.directory_rows:
                if str(row[0]) == code:
                    updated_rows.append(
                        (
                            row[0],
                            row[1],
                            row[2] if row[2] is not None else params[0],
                            row[3] if row[3] is not None else params[1],
                            row[4],
                            row[5] if len(row) > 5 else None,
                        )
                    )
                    self.rowcount = 1
                else:
                    updated_rows.append(row)
            if self.rowcount:
                self.pool.directory_rows = updated_rows
        elif normalized.startswith("UPDATE ctv_directory SET"):
            code = str(params[-1]) if params else ""
            target = code.upper()
            updated_rows = []
            for row in self.pool.directory_rows:
                if str(row[0]).upper() == target:
                    updated_rows.append(
                        (
                            row[0],
                            params[0],
                            params[1],
                            row[3] if len(row) > 3 else None,
                            params[2],
                            row[5] if len(row) > 5 else None,
                        )
                    )
                    self.rowcount = 1
                else:
                    updated_rows.append(row)
            if self.rowcount:
                self.pool.directory_rows = updated_rows
        elif normalized.startswith("DELETE FROM ctv_form_links"):
            legacy = set(params[0]) if params else set()
            before = len(self.pool.link_rows)
            self.pool.link_rows = [
                row for row in self.pool.link_rows if str(row[0]) not in legacy
            ]
            self.rowcount = before - len(self.pool.link_rows)
        elif normalized.startswith("DELETE FROM ctv_quotas"):
            legacy = set(params[0]) if params else set()
            before = len(self.pool.quota_rows)
            self.pool.quota_rows = [
                row for row in self.pool.quota_rows if str(row[0]) not in legacy
            ]
            self.rowcount = before - len(self.pool.quota_rows)
        elif normalized.startswith("DELETE FROM ctv_directory"):
            legacy = set(params[0]) if params else set()
            before = len(self.pool.directory_rows)
            self.pool.directory_rows = [
                row for row in self.pool.directory_rows if str(row[0]) not in legacy
            ]
            self.rowcount = before - len(self.pool.directory_rows)
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
        quota_rows=None,
    ):
        self.executed = []
        self.commits = 0
        self.directory_rows = directory_rows or []
        self.authorized_rows = authorized_rows or []
        self.link_rows = link_rows or []
        self.quota_rows = quota_rows or []

    def connection(self):
        return FakeConn(self)


@pytest.fixture(autouse=True)
def _reset_ctv_cache():
    clear_ctv_directory_cache()
    yield
    clear_ctv_directory_cache()


@pytest.mark.asyncio
async def test_schema_seed_and_cache_read_helpers():
    static_rows = [
        (code, ctv_module.STATIC_CTV_NAMES[code], None, None, True, "seed:static")
        for code in ctv_module.CTV_CODES
    ]
    pool = FakePool(
        authorized_rows=[
            (111, "TUAN_LM_HCM_001", "Tuấn LM", "0901111111", "COLLABORATOR"),
            (222, "new_ctv_x", "CTV Mới", "0902222222", "COLLABORATOR"),
        ],
        directory_rows=static_rows,
        link_rows=[("TUAN_LM_HCM_001", "income_survey", "https://x.test", "qr")],
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
    assert len(static_inserts) == 4
    assert [p[0] for p in static_inserts] == list(ctv_module.CTV_CODES)
    assert [p[1] for p in static_inserts] == [
        ctv_module.STATIC_CTV_NAMES[code] for code in ctv_module.CTV_CODES
    ]
    assert [p[1] for p in static_inserts] == [
        "Tuấn_LM_HCM_001",
        "Dũng_NV_HCM_002",
        "Khuyên_TL_HCM_003",
        "Vỹ_PV_HCM_004",
    ]

    # authorized_users only fills phone/telegram on known static codes.
    authorized_updates = [
        params for sql, params in pool.executed
        if "phone = COALESCE(ctv_directory.phone" in sql
    ]
    assert [p[-1] for p in authorized_updates] == ["TUAN_LM_HCM_001"]
    assert not any(
        params and "NEW_CTV_X" in str(params) for _, params in pool.executed
    )

    assert get_ctv_codes() == list(ctv_module.CTV_CODES)
    assert get_ctv_options_for_form(DEFAULT_TRUST_BRIDGE_SCHEMA) == list(ctv_module.CTV_CODES)


@pytest.mark.asyncio
async def test_seed_restricts_directory_to_exactly_four_static_codes():
    pool = FakePool(
        directory_rows=[
            (code, ctv_module.STATIC_CTV_NAMES[code], None, None, True, "seed:static")
            for code in ctv_module.CTV_CODES
        ],
        authorized_rows=[(222, "new_ctv_x", "CTV Mới", "0902222222", "COLLABORATOR")],
    )

    stats = await seed_ctv_directory(FakeCursor(pool))

    assert stats["static"] == 4
    assert stats["legacy_deleted"] == {"directory": 0, "links": 0, "quotas": 0}
    assert all("survey_submissions" not in sql for sql, _ in pool.executed)
    static_inserts = [
        params for sql, params in pool.executed
        if "INSERT INTO ctv_directory" in sql and "seed:static" in sql
    ]
    assert [p[0] for p in static_inserts] == list(ctv_module.CTV_CODES)
    assert not any(
        "seed:authorized_users" in sql or "seed:survey_submissions" in sql
        for sql, _ in pool.executed
    )

    pool.executed.clear()
    second_stats = await seed_ctv_directory(FakeCursor(pool))
    second_inserts = [
        params[0] for sql, params in pool.executed
        if "INSERT INTO ctv_directory" in sql and "seed:static" in sql
    ]
    assert second_stats["static"] == 4
    assert second_inserts == list(ctv_module.CTV_CODES)


@pytest.mark.asyncio
async def test_seed_cleans_known_legacy_codes_but_keeps_admin_codes():
    legacy = list(ctv_module.LEGACY_SUBMISSION_CODES)
    static_rows = [
        (code, ctv_module.STATIC_CTV_NAMES[code], None, None, True, "seed:static")
        for code in ctv_module.CTV_CODES
    ]
    pool = FakePool(
        directory_rows=static_rows
        + [(code, code, None, None, True, "seed:survey_submissions") for code in legacy]
        + [("ADMIN_EXTRA", "Admin Extra", "0909", 999, True, "admin")],
        link_rows=[
            (code, "income_survey", f"https://x.test/{code}", "qr") for code in legacy
        ]
        + [("ADMIN_EXTRA", "income_survey", "https://x.test/admin", "qr")],
        quota_rows=[
            (code, "global", None, 10, 30, True) for code in legacy
        ]
        + [("ADMIN_EXTRA", "global", None, 5, 30, True)],
    )

    assert await ensure_ctv_platform_schema(pool) is True

    delete_statements = [
        (sql, params) for sql, params in pool.executed if sql.lstrip().startswith("DELETE")
    ]
    assert len(delete_statements) == 3
    for _, params in delete_statements:
        assert params == (legacy,)

    assert {row[0] for row in pool.directory_rows} == set(ctv_module.CTV_CODES) | {"ADMIN_EXTRA"}
    assert [row[0] for row in pool.link_rows] == ["ADMIN_EXTRA"]
    assert [row[0] for row in pool.quota_rows] == ["ADMIN_EXTRA"]

    assert get_ctv_entry("CTV001_Dũng_HN") is None
    assert get_ctv_entry("ADMIN_EXTRA")["code"] == "ADMIN_EXTRA"


@pytest.mark.asyncio
async def test_form_options_and_links_only_contain_static_four():
    legacy = list(ctv_module.LEGACY_SUBMISSION_CODES)
    pool = FakePool(
        directory_rows=[
            (code, ctv_module.STATIC_CTV_NAMES[code], None, None, True, "seed:static")
            for code in ctv_module.CTV_CODES
        ]
        + [(code, code, None, None, True, "seed:survey_submissions") for code in legacy],
        link_rows=[
            (code, "income_survey", f"https://x.test/{code}", "qr") for code in legacy
        ]
        + [("TUAN_LM_HCM_001", "income_survey", "https://x.test/tuan", "qr")],
    )

    assert await ensure_ctv_platform_schema(pool) is True

    assert get_ctv_codes() == list(ctv_module.CTV_CODES)
    assert get_ctv_options_for_form(DEFAULT_TRUST_BRIDGE_SCHEMA) == list(ctv_module.CTV_CODES)
    assert get_ctv_form_link("CTV001_Dũng_HN", "income_survey") is None
    assert get_ctv_form_link("TUAN_LM_HCM_001", "income_survey") is not None


@pytest.mark.asyncio
async def test_admin_update_refreshes_directory_cache():
    pool = FakePool(
        directory_rows=[("ALPHA_001", "Alpha", None, 77777, True, "seed:static")],
    )

    assert await update_ctv_directory_entry(
        pool,
        "alpha_001",
        display_name="Alpha Updated",
        phone="0909 123 456",
        is_active=False,
    ) is True

    entry = get_ctv_entry("ALPHA_001")
    assert entry is not None
    assert entry["display_name"] == "Alpha Updated"
    assert entry["phone"] == "0909123456"
    assert entry["telegram_user_id"] == 77777
    assert entry["is_active"] is False

    assert await update_ctv_directory_entry(pool, "MISSING", display_name="X") is False


def test_directory_driven_options_and_links_override_static():
    set_ctv_directory_cache(
        [
            {"code": "ALPHA_001", "display_name": "Alpha", "is_active": True},
            {"code": "BETA_002", "display_name": "Beta", "is_active": False},
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
async def test_lookup_telegram_user_for_collaborator_uses_directory_phone():
    from routers import survey as survey_router

    # Directory phone wins even when an internally cached tg id is present.
    pool = FakePool(directory_rows=[(555, "0901 111 111")])
    phone_lookup = AsyncMock(return_value=777)
    with patch.object(survey_router, "get_db_pool", return_value=pool), patch.object(
        survey_router, "lookup_telegram_user_by_phone", phone_lookup
    ):
        assert await survey_router.lookup_telegram_user_for_collaborator("tuan_lm_hcm_001") == 777
    phone_lookup.assert_awaited_once_with("0901 111 111")

    # Cached telegram_user_id is only a fallback when phone lookup finds nothing.
    pool = FakePool(directory_rows=[(555, None)])
    with patch.object(survey_router, "get_db_pool", return_value=pool):
        assert await survey_router.lookup_telegram_user_for_collaborator("TUAN_LM_HCM_001") == 555

    pool = FakePool(directory_rows=[(555, "0901111111")])
    with patch.object(survey_router, "get_db_pool", return_value=pool), patch.object(
        survey_router, "lookup_telegram_user_by_phone", AsyncMock(return_value=None)
    ):
        assert await survey_router.lookup_telegram_user_for_collaborator("TUAN_LM_HCM_001") == 555

    # Real path: directory phone -> authorized_users phone lookup (+84 normalization).
    pool = FakePool(
        directory_rows=[(None, "+84 901 111 111")],
        authorized_rows=[(888,)],
    )
    with patch.object(survey_router, "get_db_pool", return_value=pool):
        assert await survey_router.lookup_telegram_user_for_collaborator("TUAN_LM_HCM_001") == 888


@pytest.mark.asyncio
async def test_driver_bot_quota_shows_own_quotas():
    runner = DriverBotRunner(bot_token="mock_driver_token")
    send = AsyncMock()
    with patch.object(runner, "send_message", send), patch(
        "core.quotas.resolve_ctv_code_for_telegram", AsyncMock(return_value="TUAN_LM_HCM_001")
    ), patch(
        "core.quotas.build_ctv_quota_report",
        AsyncMock(return_value="📊 <b>Mục tiêu CTV</b> TUAN_LM_HCM_001"),
    ) as report:
        await runner.handle_driver_command(
            chat_id=42, user_id=111, text="/quota", from_user={"first_name": "CTV"}
        )
    report.assert_awaited_once_with(runner.db_pool, "TUAN_LM_HCM_001")
    assert "Mục tiêu CTV" in send.await_args[0][1]


@pytest.mark.asyncio
async def test_admin_bot_quota_accepts_explicit_code(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-100")
    runner = TelegramPollingRunner(bot_token="mock_admin_token", role="admin")
    send = AsyncMock()
    with patch.object(runner, "send_message", send), patch(
        "core.quotas.resolve_ctv_code_from_ref", AsyncMock(return_value="ALPHA_001")
    ), patch(
        "core.quotas.build_ctv_quota_report",
        AsyncMock(return_value="📊 <b>Mục tiêu CTV</b> ALPHA_001"),
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
    assert 'name="telegram_user_id"' not in response.text
    assert 'action="/admin/ctvs/TUAN_LM_HCM_001"' in response.text
    assert "/admin/ctvs" in response.text


@pytest.mark.asyncio
async def test_admin_update_endpoint_redirects_and_refreshes_cache():
    from routers import admin_ctvs

    pool = FakePool(
        directory_rows=[("ALPHA_001", "Alpha", None, 66666, True, "seed:static")],
    )
    with patch.object(admin_ctvs, "get_db_pool", return_value=pool):
        response = await admin_ctvs.update_admin_ctv(
            "ALPHA_001",
            display_name="Alpha Endpoint",
            phone="0909 000 111",
            is_active="1",
            admin_user="admin",
        )

    assert response.status_code == 303
    entry = get_ctv_entry("ALPHA_001")
    assert entry["display_name"] == "Alpha Endpoint"
    assert entry["phone"] == "0909000111"
    assert entry["telegram_user_id"] == 66666
    assert entry["is_active"] is True

    # A legacy client sending telegram_user_id must not change it.
    from fastapi.testclient import TestClient
    from config import settings as app_settings
    from main import app

    pool = FakePool(
        directory_rows=[("ALPHA_001", "Alpha", None, 99999, True, "seed:static")],
    )
    with patch.object(admin_ctvs, "get_db_pool", return_value=pool):
        client = TestClient(app)
        response = client.post(
            "/admin/ctvs/ALPHA_001",
            data={
                "display_name": "Alpha Form",
                "phone": "0909 222 333",
                "telegram_user_id": "11111",
                "is_active": "1",
            },
            auth=(app_settings.ADMIN_USERNAME, app_settings.ADMIN_PASSWORD),
        )
    assert response.status_code == 200
    entry = get_ctv_entry("ALPHA_001")
    assert entry["display_name"] == "Alpha Form"
    assert entry["phone"] == "0909222333"
    assert entry["telegram_user_id"] == 99999


@pytest.mark.asyncio
async def test_phone_shaped_collaborator_never_becomes_a_chat_id():
    """Regression: phone strings must not be cast to Telegram chat ids."""
    from routers import survey as survey_router

    pool = FakePool(authorized_rows=[])
    with patch.object(survey_router, "get_db_pool", return_value=pool):
        assert await survey_router.lookup_telegram_user_for_collaborator("0961281318") is None

    pool = FakePool(authorized_rows=[(9241303232,)])
    with patch.object(survey_router, "get_db_pool", return_value=pool):
        assert await survey_router.lookup_telegram_user_for_collaborator("9241303232") == 9241303232
