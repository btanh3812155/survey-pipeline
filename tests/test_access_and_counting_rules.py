"""Real-PostgreSQL & unit regression tests for private form auth, approved-only CTV quotas, and approved-only campaign response_limit."""
import os
from datetime import datetime, timezone
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

from core import admin_sessions, ctv, quotas, state
from core.passwords import hash_password
from routers import ctv_portal, survey
from services.form_engine import FormQuestion, FormSchema, FormSettings, QuestionType
from services.submission_pipeline import SubmissionCommit
from services.telegram_polling import DriverBotRunner
from tests.postgres_harness import SCHEMA_PATH, resolve_test_database_url

CTV_A = "CTV_RULE_A"
TG_A = 83001
STAFF_PASSWORD = "staff-secret-password"


def _schema(
    form_id: str,
    title: str,
    *,
    access: str = "public",
    limit=None,
    count: int = 0,
    tracking: bool = False,
) -> FormSchema:
    return FormSchema(
        form_id=form_id,
        title=title,
        settings=FormSettings(
            access_control=access,
            response_limit=limit,
            current_responses=count,
            ctv_ref_enabled=tracking,
            decree13_required=False,
        ),
        questions=[FormQuestion(id="q1", title="Cau 1", type=QuestionType.TEXT)],
    )


@pytest_asyncio.fixture
async def pg_env(monkeypatch):
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("disposable PostgreSQL is required")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL")
    dsn = resolve_test_database_url()
    schema_name = "rules_" + uuid4().hex
    monkeypatch.setattr(ctv, "_CTV_DIRECTORY", {})
    monkeypatch.setattr(ctv, "_CTV_FORM_LINKS", {})
    monkeypatch.setattr(state, "get_redis_client", lambda: None)
    monkeypatch.setattr(state.settings, "ENVIRONMENT", "test")
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    with psycopg.connect(dsn, autocommit=True) as root:
        root.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
    try:
        isolated = make_conninfo(dsn, options=f"-c search_path={schema_name},public")
        with psycopg.connect(isolated, autocommit=True, row_factory=dict_row) as conn:
            for table in (
                "authorized_users",
                "driver_identity_vault",
                "survey_submissions",
                "admin_accounts",
                "ctv_directory",
                "form_tags",
                "ctv_quotas",
                "system_audit_log",
            ):
                prefix = f"CREATE TABLE IF NOT EXISTS {table}"
                conn.execute(prefix + schema_sql.split(prefix, 1)[1].split(");", 1)[0] + ");")
            for stmt in ctv.CTV_TABLE_STATEMENTS:
                conn.execute(stmt)

            pw_hash = hash_password(STAFF_PASSWORD, iterations=1_000)
            conn.execute(
                "INSERT INTO admin_accounts(username, password_hash, role, is_active) VALUES "
                "('staff_viewer', %s, 'viewer', TRUE), "
                "('staff_manager', %s, 'manager', TRUE), "
                "('staff_disabled', %s, 'manager', FALSE)",
                (pw_hash, pw_hash, pw_hash),
            )
            conn.execute(
                "INSERT INTO ctv_directory(code, display_name, telegram_user_id, is_active) "
                "VALUES (%s, 'CTV Rules', %s, TRUE)",
                (CTV_A, TG_A),
            )

            async with AsyncConnectionPool(isolated, open=False, min_size=1, max_size=4) as pool:
                await ctv.refresh_ctv_directory_cache(pool)
                monkeypatch.setattr(state, "get_db_pool", lambda: pool)
                monkeypatch.setattr(survey, "get_db_pool", lambda: pool)
                monkeypatch.setattr(ctv_portal, "get_db_pool", lambda: pool)
                yield pool, conn, pw_hash
    finally:
        with psycopg.connect(dsn, autocommit=True) as root:
            root.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name)))


def _insert_submission(conn, submission_id: str, form_id: str, status: str, ctv_code: str = CTV_A):
    conn.execute(
        "INSERT INTO survey_submissions(submission_id, form_id, ctv_code, status) "
        "VALUES (%s, %s, %s, %s)",
        (submission_id, form_id, ctv_code, status),
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_private_form_blocks_unauthenticated_and_allows_staff(pg_env, monkeypatch):
    _pool, _conn, pw_hash = pg_env
    forms = {
        "income_survey": _schema("income_survey", "Private Default", access="private", tracking=False),
        "priv_form": _schema("priv_form", "Internal Only", access="private", tracking=False),
        "pub_form": _schema("pub_form", "Public Survey", access="public", tracking=False),
    }
    monkeypatch.setattr(survey, "FORM_REGISTRY", forms)
    monkeypatch.setattr(survey, "refresh_form_registry_from_disk", lambda: False)

    submit_spy = AsyncMock(
        return_value=SubmissionCommit(
            submission_id="VN-PRIV-OK",
            status="APPROVED",
            response={"status": "APPROVED", "submission_id": "VN-PRIV-OK"},
        )
    )
    monkeypatch.setattr(survey, "process_submission", submit_spy)

    app = FastAPI()
    app.include_router(survey.router)
    client = TestClient(app, base_url="http://testserver")

    # Catalog still excludes private forms.
    catalog = client.get("/api/v1/forms")
    assert catalog.status_code == 200
    assert [f["form_id"] for f in catalog.json()["forms"]] == ["pub_form"]

    # Unauthenticated access is blocked on form HTML, camera-form, schema API, and submit.
    for path in ("/forms/priv_form", "/camera-form"):
        resp = client.get(path)
        assert resp.status_code == 403
        assert "Internal Only" not in resp.text and "Private Default" not in resp.text
        assert "Biểu mẫu riêng tư" in resp.text

    schema_unauth = client.get("/api/v1/forms/priv_form")
    assert schema_unauth.status_code == 403
    assert "Internal Only" not in schema_unauth.text

    submit_unauth = client.post("/api/v1/survey/submit-webrtc", data={"form_id": "priv_form", "q1": "hi"})
    assert submit_unauth.status_code == 403
    assert submit_spy.await_count == 0

    # Inactive staff cookie or bad Basic credentials remain blocked.
    disabled_cookie = admin_sessions.issue("staff_disabled", pw_hash)
    assert client.get(
        "/forms/priv_form",
        headers={"Cookie": f"{admin_sessions.COOKIE_NAME}={disabled_cookie}"},
    ).status_code == 403
    assert client.get("/api/v1/forms/priv_form", auth=("staff_viewer", "wrong-pass")).status_code == 403

    # Authenticated staff via signed session cookie (even viewer role) can preview, fetch schema, and submit.
    viewer_cookie = admin_sessions.issue("staff_viewer", pw_hash)
    cookie_header = {"Cookie": f"{admin_sessions.COOKIE_NAME}={viewer_cookie}"}
    page_ok = client.get("/forms/priv_form", headers=cookie_header)
    assert page_ok.status_code == 200
    assert "Internal Only" in page_ok.text

    cam_ok = client.get("/camera-form", headers=cookie_header)
    assert cam_ok.status_code == 200
    assert "Private Default" in cam_ok.text

    api_ok = client.get("/api/v1/forms/priv_form", headers=cookie_header)
    assert api_ok.status_code == 200
    assert api_ok.json()["form_id"] == "priv_form"

    submit_cookie_ok = client.post(
        "/api/v1/survey/submit-webrtc",
        data={"form_id": "priv_form", "q1": "staff preview", "gps_latitude": "10.7769", "gps_longitude": "106.7009"},
        headers={**cookie_header, "Origin": "http://testserver"},
    )
    assert submit_cookie_ok.status_code == 200
    assert submit_spy.await_count == 1

    # Authenticated staff via HTTP Basic Auth is also permitted.
    assert client.get("/api/v1/forms/priv_form", auth=("staff_manager", STAFF_PASSWORD)).status_code == 200
    submit_basic_ok = client.post(
        "/api/v1/survey/submit-webrtc",
        data={"form_id": "priv_form", "q1": "basic submit", "gps_latitude": "10.7769", "gps_longitude": "106.7009"},
        auth=("staff_manager", STAFF_PASSWORD),
    )
    assert submit_basic_ok.status_code == 200
    assert submit_spy.await_count == 2


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_ctv_quota_counts_only_approved_and_never_blocks_submissions(pg_env, monkeypatch):
    pool, conn, _ = pg_env
    conn.execute(
        "INSERT INTO ctv_quotas(ctv_code, scope_type, scope_value, max_submissions, period_days, active) "
        "VALUES (%s, 'form', 'quota_form', 2, 7, TRUE), (%s, 'global', NULL, 2, NULL, TRUE)",
        (CTV_A, CTV_A),
    )
    conn.execute(
        "INSERT INTO ctv_form_assignments(ctv_code, form_id) VALUES (%s, 'quota_form')",
        (CTV_A,),
    )
    _insert_submission(conn, "SUB-Q-1", "quota_form", "APPROVED", CTV_A)
    _insert_submission(conn, "SUB-Q-2", "quota_form", "REJECTED", CTV_A)
    _insert_submission(conn, "SUB-Q-3", "quota_form", "MANUAL_REVIEW", CTV_A)
    _insert_submission(conn, "SUB-Q-4", "quota_form", "APPEAL_PENDING", CTV_A)

    evals = await quotas.get_ctv_quota_evaluations(pool, CTV_A, strict=True)
    assert [e["used"] for e in evals] == [1, 1]
    assert [e["remaining"] for e in evals] == [1, 1]

    # Reaching and exceeding CTV quota still never blocks opening or submitting the form.
    _insert_submission(conn, "SUB-Q-5", "quota_form", "APPROVED", CTV_A)
    _insert_submission(conn, "SUB-Q-6", "quota_form", "APPROVED", CTV_A)
    evals_after = await quotas.get_ctv_quota_evaluations(pool, CTV_A, strict=True)
    assert [e["used"] for e in evals_after] == [3, 3]

    forms = {"quota_form": _schema("quota_form", "Quota Tracked", tracking=True)}
    monkeypatch.setattr(survey, "FORM_REGISTRY", forms)
    monkeypatch.setattr(survey, "refresh_form_registry_from_disk", lambda: False)
    submit_spy = AsyncMock(
        return_value=SubmissionCommit(
            submission_id="VN-QUOTA-PASS",
            status="APPROVED",
            response={"status": "APPROVED", "submission_id": "VN-QUOTA-PASS"},
        )
    )
    monkeypatch.setattr(survey, "process_submission", submit_spy)

    app = FastAPI()
    app.include_router(survey.router)
    client = TestClient(app)

    assert client.get(f"/forms/quota_form?ref={CTV_A}").status_code == 200
    resp = client.post(
        "/api/v1/survey/submit-webrtc",
        data={
            "form_id": "quota_form",
            "collaborator_id": CTV_A,
            "q1": "still allowed",
            "gps_latitude": "10.7769",
            "gps_longitude": "106.7009",
        },
    )
    assert resp.status_code == 200
    assert submit_spy.await_count == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_campaign_response_limit_counts_only_approved_and_blocks_when_hit(pg_env, monkeypatch):
    pool, conn, _ = pg_env
    forms = {
        "income_survey": _schema("income_survey", "Default Capped", limit=2, tracking=False),
        "capped_form": _schema("capped_form", "Limited Campaign", limit=2, tracking=True),
        "closed_form": _schema("closed_form", "Negative Limit Closed", limit=-1, tracking=False),
        "unlimited_form": _schema("unlimited_form", "Zero Limit Unlimited", limit=0, tracking=False),
    }
    conn.execute(
        "INSERT INTO ctv_form_assignments(ctv_code, form_id) VALUES (%s, 'capped_form')",
        (CTV_A,),
    )
    monkeypatch.setattr(survey, "FORM_REGISTRY", forms)
    monkeypatch.setattr(survey, "refresh_form_registry_from_disk", lambda: False)
    monkeypatch.setattr("services.telegram_polling.FORM_REGISTRY", forms)
    monkeypatch.setattr("services.telegram_polling.refresh_form_registry_from_disk", lambda: False)

    submit_spy = AsyncMock(
        return_value=SubmissionCommit(
            submission_id="VN-LIM-OK",
            status="APPROVED",
            response={"status": "APPROVED", "submission_id": "VN-LIM-OK"},
        )
    )
    monkeypatch.setattr(survey, "process_submission", submit_spy)

    app = FastAPI()
    app.include_router(survey.router)
    client = TestClient(app)

    # 1 APPROVED + multiple non-approved rows must NOT trigger response_limit=2.
    _insert_submission(conn, "SUB-L-1", "capped_form", "APPROVED")
    _insert_submission(conn, "SUB-L-2", "capped_form", "REJECTED")
    _insert_submission(conn, "SUB-L-3", "capped_form", "MANUAL_REVIEW")
    _insert_submission(conn, "SUB-L-4", "capped_form", "APPEAL_PENDING")

    open_page = client.get(f"/forms/capped_form?ref={CTV_A}")
    assert open_page.status_code == 200
    assert "Khảo sát đã đủ số lượng phản hồi" not in open_page.text
    assert "Limited Campaign" in open_page.text

    open_submit = client.post(
        "/api/v1/survey/submit-webrtc",
        data={
            "form_id": "capped_form",
            "collaborator_id": CTV_A,
            "q1": "ok",
            "gps_latitude": "10.7769",
            "gps_longitude": "106.7009",
        },
    )
    assert open_submit.status_code == 200
    assert submit_spy.await_count == 1

    # Telegram /forms still lists capped_form while under APPROVED cap.
    runner = DriverBotRunner(bot_token="mock_token", db_pool=pool)
    sent = []
    monkeypatch.setattr(runner, "send_message", AsyncMock(side_effect=lambda cid, text, **kw: sent.append(text)))
    await runner.handle_forms(TG_A, TG_A)
    assert "Limited Campaign" in sent[-1]

    # Second APPROVED submission hits limit (2/2).
    _insert_submission(conn, "SUB-L-5", "capped_form", "APPROVED")

    # Form page (both direct ref and selector landing) shows limit notice, never the form or CTV selector.
    for path in (f"/forms/capped_form?ref={CTV_A}", "/forms/capped_form"):
        full_page = client.get(path)
        assert full_page.status_code == 200
        assert "Khảo sát đã đủ số lượng phản hồi" in full_page.text
        assert "đạt giới hạn" in full_page.text
        assert "Vui lòng chọn CTV" not in full_page.text
        assert "submitSurvey" not in full_page.text

    # POST submit rejects BEFORE running pipeline/vision/storage/outbox.
    full_submit = client.post(
        "/api/v1/survey/submit-webrtc",
        data={"form_id": "capped_form", "collaborator_id": CTV_A, "q1": "too late"},
    )
    assert full_submit.status_code == 409
    assert "đạt giới hạn" in full_submit.json()["detail"]
    assert submit_spy.await_count == 1

    # Telegram /forms now excludes capped_form.
    await runner.handle_forms(TG_A, TG_A)
    assert "Limited Campaign" not in sent[-1]

    # /camera-form also enforces APPROVED-only response_limit on default income_survey.
    _insert_submission(conn, "SUB-DEF-1", "income_survey", "APPROVED")
    _insert_submission(conn, "SUB-DEF-2", "income_survey", "REJECTED")
    _insert_submission(conn, "SUB-DEF-3", "income_survey", "MANUAL_REVIEW")
    assert "Khảo sát đã đủ số lượng phản hồi" not in client.get("/camera-form").text
    _insert_submission(conn, "SUB-DEF-4", "income_survey", "APPROVED")
    cam_full = client.get("/camera-form")
    assert cam_full.status_code == 200
    assert "Khảo sát đã đủ số lượng phản hồi" in cam_full.text
    assert "đạt giới hạn" in cam_full.text

    # Negative limit is closed; zero limit is unlimited even with approved rows.
    assert "Khảo sát đã đủ số lượng phản hồi" in client.get("/forms/closed_form").text
    assert client.post("/api/v1/survey/submit-webrtc", data={"form_id": "closed_form", "q1": "x"}).status_code == 409

    for idx in range(3):
        _insert_submission(conn, f"SUB-U-{idx}", "unlimited_form", "APPROVED")
    assert "Khảo sát đã đủ số lượng phản hồi" not in client.get("/forms/unlimited_form").text
    assert (
        client.post(
            "/api/v1/survey/submit-webrtc",
            data={"form_id": "unlimited_form", "q1": "x", "gps_latitude": "10.7769", "gps_longitude": "106.7009"},
        ).status_code
        == 200
    )
    assert submit_spy.await_count == 2


def test_evaluate_quota_excludes_non_approved_status_in_memory():
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    quota = {"id": 1, "scope_type": "form", "scope_value": "f1", "max_submissions": 5, "period_days": 7}
    rows = [
        {"form_id": "f1", "created_at": now, "status": "APPROVED"},
        {"form_id": "f1", "created_at": now, "status": "REJECTED"},
        {"form_id": "f1", "created_at": now, "status": "MANUAL_REVIEW"},
        {"form_id": "f1", "created_at": now, "status": "APPEAL_PENDING"},
    ]
    result = quotas.evaluate_quota(quota, rows, now=now)
    assert result["used"] == 1
    assert result["remaining"] == 4
