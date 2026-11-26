"""Assigned-only CTV survey access across portal, direct links, submissions, Telegram, and admin UI."""
import os
from unittest.mock import AsyncMock
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from core import ctv
from routers import admin_ctvs, survey
from services.form_engine import FormQuestion, FormSchema, FormSettings, QuestionType
from services.telegram_polling import DriverBotRunner
from tests.postgres_harness import SCHEMA_PATH, resolve_test_database_url

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]
CTV_A, CTV_B = "CTV_ASSIGN_A", "CTV_ASSIGN_B"
TG_A, TG_B = 82001, 82002


def _schema(form_id: str, title: str, *, tracking: bool = True) -> FormSchema:
    return FormSchema(
        form_id=form_id,
        title=title,
        settings=FormSettings(access_control="public", ctv_ref_enabled=tracking),
        questions=[FormQuestion(id="q1", title="Cau 1", type=QuestionType.TEXT)],
    )


@pytest_asyncio.fixture
async def assign_env(monkeypatch):
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("disposable PostgreSQL is required")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL")
    dsn = resolve_test_database_url()
    name = "ctv_assign_" + uuid4().hex
    monkeypatch.setattr(ctv, "_CTV_DIRECTORY", {})
    monkeypatch.setattr(ctv, "_CTV_FORM_LINKS", {})
    schema_sql = SCHEMA_PATH.read_text()
    with psycopg.connect(dsn, autocommit=True) as root:
        root.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    try:
        isolated = make_conninfo(dsn, options=f"-c search_path={name},public")
        with psycopg.connect(isolated, autocommit=True, row_factory=dict_row) as conn:
            for table in ("authorized_users", "ctv_directory", "system_audit_log"):
                prefix = f"CREATE TABLE IF NOT EXISTS {table}"
                conn.execute(prefix + schema_sql.split(prefix, 1)[1].split(");", 1)[0] + ");")
            for stmt in ctv.CTV_TABLE_STATEMENTS:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO ctv_directory(code,display_name,telegram_user_id,is_active) VALUES "
                "(%s,'CTV A',%s,TRUE),(%s,'CTV B',%s,TRUE)",
                (CTV_A, TG_A, CTV_B, TG_B),
            )
            async with AsyncConnectionPool(isolated, open=False, min_size=1, max_size=4) as pool:
                await ctv.refresh_ctv_directory_cache(pool)
                yield pool, conn
    finally:
        with psycopg.connect(dsn, autocommit=True) as root:
            root.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


async def test_assigned_only_enforced_across_direct_link_submit_revocation_and_telegram(assign_env, monkeypatch):
    pool, _ = assign_env
    forms = {
        "form_alpha": _schema("form_alpha", "Alpha Survey"),
        "form_beta": _schema("form_beta", "Beta Survey"),
        "form_open_no_ctv": _schema("form_open_no_ctv", "Untracked Public", tracking=False),
    }
    monkeypatch.setattr(survey, "FORM_REGISTRY", forms)
    monkeypatch.setattr(survey, "refresh_form_registry_from_disk", lambda: False)
    monkeypatch.setattr(survey, "get_db_pool", lambda: pool)
    monkeypatch.setattr(admin_ctvs, "FORM_REGISTRY", forms)
    monkeypatch.setattr(admin_ctvs, "refresh_form_registry_from_disk", lambda: False)
    monkeypatch.setattr(admin_ctvs, "get_db_pool", lambda: pool)
    monkeypatch.setattr("services.telegram_polling.FORM_REGISTRY", forms)
    monkeypatch.setattr("services.telegram_polling.refresh_form_registry_from_disk", lambda: False)

    app = FastAPI()
    app.include_router(survey.router)
    app.include_router(admin_ctvs.router)
    app.dependency_overrides[admin_ctvs.verify_admin_auth] = lambda: "admin_tester"
    client = TestClient(app)

    # Admin assigns only form_alpha to CTV_A.
    resp = client.post(f"/admin/ctvs/{CTV_A}/assignments", data={"form_ids": ["form_alpha"]}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/ctvs?msg=updated"

    # Direct link: assigned CTV_A can open form_alpha, blocked from form_beta; unassigned CTV_B blocked from form_alpha.
    assert client.get(f"/forms/form_alpha?ref={CTV_A}").status_code == 200
    assert client.get(f"/forms/form_beta?ref={CTV_A}").status_code == 403
    assert client.get(f"/forms/form_alpha?ref={CTV_B}").status_code == 403

    # Selector dropdown without ?ref= lists only CTVs assigned to form_alpha.
    selector = client.get("/forms/form_alpha")
    assert selector.status_code == 200
    assert CTV_A in selector.text and CTV_B not in selector.text

    # Submission: unassigned CTV_B is blocked immediately with 403 before pipeline execution.
    blocked = client.post("/api/v1/survey/submit-webrtc", data={"form_id": "form_alpha", "collaborator_id": CTV_B})
    assert blocked.status_code == 403

    # Telegram /forms for CTV_A lists only assigned form_alpha; for unassigned CTV_B lists none.
    runner = DriverBotRunner(bot_token="mock_token", db_pool=pool)
    sent = []
    monkeypatch.setattr(runner, "send_message", AsyncMock(side_effect=lambda cid, text, **kw: sent.append((cid, text))))
    await runner.handle_forms(TG_A, TG_A)
    assert "Alpha Survey" in sent[-1][1] and "Beta Survey" not in sent[-1][1]
    assert f"ref={CTV_A}" in sent[-1][1]
    await runner.handle_forms(TG_B, TG_B)
    assert "Alpha Survey" not in sent[-1][1] and "Beta Survey" not in sent[-1][1]

    # Revoking CTV_A's assignment mid-survey blocks submission immediately (finish-time check).
    revoke = client.post(f"/admin/ctvs/{CTV_A}/assignments", data={}, follow_redirects=False)
    assert revoke.status_code == 303
    assert client.get(f"/forms/form_alpha?ref={CTV_A}").status_code == 403
    revoked_submit = client.post("/api/v1/survey/submit-webrtc", data={"form_id": "form_alpha", "collaborator_id": CTV_A})
    assert revoked_submit.status_code == 403
