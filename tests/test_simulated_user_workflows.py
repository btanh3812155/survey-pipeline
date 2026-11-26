# -*- coding: utf-8 -*-
"""Comprehensive simulated-user-input end-to-end tests (S1-S6) against real
disposable PostgreSQL and Redis.

Covers:
- S1: CTV onboarding -> same-number Telegram rebind -> multi-role
      ADMIN+COLLABORATOR & DRIVER+COLLABORATOR -> numeric ctv_code routing.
- S2: Default auto-assign for new non-test CTV & new form (excluding *TEST*)
      -> manual unassignment preserved across form edit -> mid-survey
      revocation immediately blocks submit (403) -> Telegram /forms shows
      assigned-only links.
- S3: Private form blocked for anon/CTV (403), allowed for staff
      (viewer/manager/superadmin); APPROVED-only CTV quota (tracking-only)
      and APPROVED-only response_limit (limit-reached HTML notice + 409).
- S4: Hard GPS block (400) on non-test submit -> require_manual_review True
      (MANUAL_REVIEW) vs False (APPROVED) -> Decree 13 dissent never enters
      APPROVED/billing/quota/limit -> repeat driver across 2 campaigns &
      retry after REJECTED.
- S5: Web appeal -> /admin/appeals approve/reject + APPEAL_APPROVED /
      APPEAL_REJECTED audit log -> role boundaries (viewer, reviewer, manager).
- S6: Superadmin /admin/payouts status change (UNPAID -> PAID) +
      PAYOUT_STATUS_CHANGED audit log -> superadmin revoke of APPROVED/PAID
      row (status=REJECTED, payout_status=CANCELLED) + PAYOUT_REVOKED audit
      log; manager/reviewer/viewer receive 403; VI/EN i18n & zero emojis.
"""

import hashlib
import io
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import psycopg
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import FastAPI
from PIL import Image, ImageDraw
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from config import settings
from core import (
    admin_accounts,
    admin_sessions,
    ctv,
    ctv_auth,
    ctv_onboarding as onboarding,
    durable_work,
    embedding_executor,
    outbox_delivery,
    phone_links,
    quotas,
    state,
)
from core.passwords import hash_password
from routers import (
    admin_accounts as admin_accounts_router,
    admin_appeals,
    admin_audit,
    admin_ctvs,
    admin_forms,
    admin_payouts,
    ctv_portal,
    survey,
)
from services.form_engine import FormQuestion, FormSchema, FormSettings, ImageCheckConfig, QuestionType
from services.submission_pipeline import (
    SubmissionCommit,
    SubmissionDependencies,
    SubmissionInput,
    SubmissionUpload,
    _evaluate,
    _prepare,
    _record,
)
from services.telegram_polling import DriverBotRunner
from tests.postgres_harness import SCHEMA_PATH, resolve_test_database_url

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

ROOT = Path(__file__).resolve().parents[1]
STAFF_PASSWORD = "e2e-staff-secret-password"
SAME_ORIGIN = {"Origin": "https://e2e.test"}
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")


def _make_sharp_jpeg(seed: int = 1) -> bytes:
    """Create a deterministic high-contrast JPEG that passes image decode/sharpness."""
    img = Image.new("RGB", (240, 240), (245, 245, 245))
    draw = ImageDraw.Draw(img)
    for idx in range(0, 240, 12):
        color = ((idx * 7 + seed * 31) % 200, (idx * 5 + seed * 17) % 200, (idx * 3 + seed * 53) % 200)
        draw.line([(idx, 0), (240 - idx, 240)], fill=color, width=2)
        draw.rectangle([idx // 2, idx // 2, idx // 2 + 18, idx // 2 + 18], outline=color, width=2)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _schema(
    form_id: str,
    title: str,
    *,
    access: str = "public",
    limit=None,
    count: int = 0,
    tracking: bool = True,
    require_manual_review: bool = True,
    with_consent: bool = False,
    with_camera: bool = False,
) -> FormSchema:
    questions = []
    if with_consent:
        questions.append(
            FormQuestion(
                id="consent_choice",
                title="Đồng ý Nghị định 13",
                type=QuestionType.SINGLE_CHOICE,
                required=True,
                options=["Tôi đồng ý", "Tôi không đồng ý"],
                is_consent_gate=True,
                consent_accept_values=["Tôi đồng ý"],
            )
        )
    questions.append(FormQuestion(id="q1", title="Ghi chú khảo sát", type=QuestionType.TEXT, required=False))
    if with_camera:
        questions.append(FormQuestion(id="q_cam", title="Ảnh chụp", type=QuestionType.CAMERA_WEBRTC, required=False))
    return FormSchema(
        form_id=form_id,
        title=title,
        settings=FormSettings(
            access_control=access,
            response_limit=limit,
            current_responses=count,
            ctv_ref_enabled=tracking,
            decree13_required=with_consent,
            require_manual_review=require_manual_review,
            image_checks=ImageCheckConfig(
                enable_blur_check=False,
                enable_ai_vision=False,
                enable_object_detection=False,
                enable_vehicle_match=False,
                enable_anti_fraud=False,
                require_unique=False,
                enable_ai_generated_check=False,
                enable_screen_photo_check=False,
            ),
        ),
        questions=questions,
    )


def _tg_update(user_id: int, *, text=None, contact=None, chat_type="private"):
    msg = {
        "chat": {"id": user_id if chat_type == "private" else -100999, "type": chat_type},
        "from": {"id": user_id, "username": f"tg_user_{user_id}", "first_name": "Nguyen", "last_name": f"User{user_id}"},
    }
    if text is not None:
        msg["text"] = text
    if contact is not None:
        msg["contact"] = contact
    return {"message": msg}


def _tg_contact(user_id: int, phone: str):
    return {"user_id": user_id, "phone_number": phone, "first_name": "Nguyen", "last_name": f"User{user_id}"}


@pytest_asyncio.fixture
async def e2e_env(monkeypatch, tmp_path):
    """Isolated PostgreSQL schema (with full schema.sql + migrations 001-003) + real Redis."""
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("disposable PostgreSQL is required")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL")

    dsn = resolve_test_database_url()
    schema_name = "e2e_sim_" + uuid4().hex[:16]

    # Ensure public schema has extensions + writer schema tables (checked by core.schema_gate.writer_schema_ready),
    # then isolate all test data inside a dedicated per-test schema via search_path.
    with psycopg.connect(dsn, autocommit=True) as root:
        root.execute('CREATE EXTENSION IF NOT EXISTS "vector" WITH SCHEMA public')
        root.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public')
        root.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        for stmt in phone_links.PHONE_LINK_TABLE_STATEMENTS:
            root.execute(stmt)
        for version in ("001_submission_attempts", "002_outbox_delivery", "003_outbox_operations"):
            mpath = ROOT / "scripts/migrations" / f"{version}.sql"
            msql = mpath.read_text(encoding="utf-8")
            msql = msql.replace(":'migration_sha256'", "'" + hashlib.sha256(mpath.read_bytes()).hexdigest() + "'")
            msql = msql.replace(":'migration_001_sha256'", "'" + outbox_delivery.MIGRATION_001_CHECKSUM + "'")
            msql = msql.replace(
                ":'migration_002_sha256'",
                "'" + hashlib.sha256(outbox_delivery.MIGRATION_002.read_bytes()).hexdigest() + "'",
            )
            root.execute(msql)
        root.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))

    isolated_dsn = make_conninfo(dsn, options=f"-c search_path={schema_name},public")
    redis_client = None
    redis_url = (os.environ.get("UPGRADE_TEST_REDIS_URL") or "").strip()
    if redis_url:
        redis_client = aioredis.from_url(redis_url, decode_responses=True, socket_timeout=2.0)
        await redis_client.ping()
    elif os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
        pytest.fail("disposable Redis is required via UPGRADE_TEST_REDIS_URL")

    try:
        with psycopg.connect(isolated_dsn, autocommit=True, row_factory=dict_row) as conn:
            conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
            for version in ("001_submission_attempts", "002_outbox_delivery", "003_outbox_operations"):
                mpath = ROOT / "scripts/migrations" / f"{version}.sql"
                msql = mpath.read_text(encoding="utf-8")
                msql = msql.replace(":'migration_sha256'", "'" + hashlib.sha256(mpath.read_bytes()).hexdigest() + "'")
                msql = msql.replace(":'migration_001_sha256'", "'" + outbox_delivery.MIGRATION_001_CHECKSUM + "'")
                msql = msql.replace(
                    ":'migration_002_sha256'",
                    "'" + hashlib.sha256(outbox_delivery.MIGRATION_002.read_bytes()).hexdigest() + "'",
                )
                conn.execute(msql)

            async with AsyncConnectionPool(isolated_dsn, open=False, min_size=2, max_size=6) as pool:
                await ctv.ensure_ctv_platform_schema(pool)
                await phone_links.ensure_phone_link_schema(pool)
                await admin_accounts.ensure_admin_accounts(pool)

                monkeypatch.setattr(ctv, "_CTV_DIRECTORY", {})
                monkeypatch.setattr(ctv, "_CTV_FORM_LINKS", {})
                monkeypatch.setattr(survey, "LOCAL_APPEAL_TRACKER", {})
                monkeypatch.setattr(survey, "LOCAL_IDEMP_CACHE", {})
                monkeypatch.setattr(state, "_ADMIN_HASH_CACHE", {})
                monkeypatch.setattr(state.settings, "ENVIRONMENT", "test")
                monkeypatch.setattr(settings, "APP_SECRET", "e2e-sim-secret-key-at-least-32-characters-long")
                monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_USERNAME", "E2ESimTest_bot")
                monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", "123456:e2e_test_bot_token")
                monkeypatch.setattr(settings, "DURABLE_WEBRTC_ENABLED", True)
                monkeypatch.setattr(settings, "DURABLE_APPEALS_ENABLED", True)
                monkeypatch.setattr(survey, "SUBMISSION_PHOTOS_DIR", str(tmp_path / "photos"))
                os.makedirs(tmp_path / "photos", exist_ok=True)

                for mod in (
                    state,
                    admin_accounts_router,
                    admin_appeals,
                    admin_ctvs,
                    admin_forms,
                    admin_payouts,
                    ctv_auth,
                    ctv_portal,
                    survey,
                ):
                    if hasattr(mod, "get_db_pool"):
                        monkeypatch.setattr(mod, "get_db_pool", lambda _p=pool: _p)
                    if hasattr(mod, "get_redis_client"):
                        monkeypatch.setattr(mod, "get_redis_client", lambda _r=redis_client: _r)
                import core.repositories as repos_mod
                monkeypatch.setattr(repos_mod, "get_db_pool", lambda _p=pool: _p)
                monkeypatch.setattr(state, "db_pool", pool)
                monkeypatch.setattr(state, "redis_client", redis_client)

                yield SimpleNamespace(pool=pool, conn=conn, dsn=isolated_dsn, redis=redis_client, tmp_path=tmp_path)
    finally:
        if redis_client is not None:
            for pattern in ("admin-auth:*", "rate_limit:appeal:*", "ctv-otp:*", "idemp:*"):
                keys = await redis_client.keys(pattern)
                if keys:
                    await redis_client.delete(*keys)
            await redis_client.aclose()
        with psycopg.connect(dsn, autocommit=True) as root:
            root.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name)))


# ---------------------------------------------------------------------------
# S1: CTV onboarding -> rebind -> multi-role -> numeric ctv_code routing
# ---------------------------------------------------------------------------


async def test_s1_ctv_onboarding_rebind_multirole_and_numeric_code_routing(e2e_env, monkeypatch):
    pool, conn = e2e_env.pool, e2e_env.conn
    tg_initial = 91001
    tg_admin = 91002
    tg_driver = 91003
    tg_numeric_owner = 91004
    phone_ctv = "0912345601"
    phone_numeric = "0987654301"

    assert await ctv.create_ctv_directory_entry(pool, "CTV_S1_MAIN", display_name="CTV S1 Main", is_active=True)
    assert await ctv.create_ctv_directory_entry(
        pool, "8801", display_name="Numeric Code CTV", phone=phone_numeric, telegram_user_id=tg_numeric_owner, is_active=True
    )

    runner = DriverBotRunner(bot_token="mock_s1_token", db_pool=pool)
    monkeypatch.setattr(runner, "send_message", AsyncMock())

    # 1. CTV onboarding via invite token -> self-shared contact completes binding.
    invite_url = (await onboarding.ensure_ctv_bot_links(pool))["CTV_S1_MAIN"]
    payload = invite_url.split("?start=", 1)[1]
    await runner.handle_update(_tg_update(tg_initial, text=f"/start {payload}"))
    assert conn.execute("SELECT ctv_code FROM ctv_bot_pending WHERE telegram_user_id=%s", (tg_initial,)).fetchone()["ctv_code"] == "CTV_S1_MAIN"

    await runner.handle_update(_tg_update(tg_initial, contact=_tg_contact(tg_initial, "+84 912 345 601")))
    dir_row = conn.execute("SELECT phone, telegram_user_id, auth_revision FROM ctv_directory WHERE code='CTV_S1_MAIN'").fetchone()
    assert dir_row["phone"] == phone_ctv
    assert dir_row["telegram_user_id"] == tg_initial
    initial_rev = dir_row["auth_revision"]

    # Issue a CTV web session before rebind so we can verify invalidation on rebind.
    _ident, binding = await ctv_auth._identity(code="CTV_S1_MAIN")
    old_session_cookie = ctv_auth._issue_session("CTV_S1_MAIN", binding)
    fake_req = SimpleNamespace(cookies={ctv_auth.COOKIE_NAME: old_session_cookie}, headers={})
    assert (await ctv_auth.ctv_from_session(fake_req))["telegram_user_id"] == tg_initial

    # 2. Same-number Telegram rebind onto an active ADMIN -> multi-role ADMIN + COLLABORATOR.
    conn.execute(
        "INSERT INTO authorized_users(telegram_user_id, username, full_name, role, is_active) "
        "VALUES (%s, 'ops_admin_s1', 'Ops Admin', 'ADMIN', TRUE), (%s, 'drv_s1', 'Driver S1', 'DRIVER', TRUE)",
        (tg_admin, tg_driver),
    )
    await runner.handle_update(_tg_update(tg_admin, contact=_tg_contact(tg_admin, "+84 912 345 601")))

    rebound_dir = conn.execute("SELECT telegram_user_id, auth_revision FROM ctv_directory WHERE code='CTV_S1_MAIN'").fetchone()
    assert rebound_dir["telegram_user_id"] == tg_admin
    assert rebound_dir["auth_revision"] != initial_rev
    assert await ctv_auth.ctv_from_session(fake_req) is None
    assert conn.execute("SELECT is_active FROM authorized_users WHERE telegram_user_id=%s", (tg_initial,)).fetchone()["is_active"] is False
    admin_auth = conn.execute("SELECT role, roles, is_active FROM authorized_users WHERE telegram_user_id=%s", (tg_admin,)).fetchone()
    assert admin_auth == {"role": "ADMIN", "roles": ["ADMIN", "COLLABORATOR"], "is_active": True}

    rebind_audit = conn.execute(
        "SELECT action, entity_id FROM system_audit_log WHERE action='CTV_TELEGRAM_REPLACED' AND entity_id='CTV_S1_MAIN'"
    ).fetchone()
    assert rebind_audit is not None

    # 3. Rebind away from ADMIN onto DRIVER -> preserves ADMIN on tg_admin, grants COLLABORATOR + DRIVER on tg_driver.
    await runner.handle_update(_tg_update(tg_driver, contact=_tg_contact(tg_driver, phone_ctv)))
    assert conn.execute("SELECT role, roles FROM authorized_users WHERE telegram_user_id=%s", (tg_admin,)).fetchone() == {
        "role": "ADMIN",
        "roles": ["ADMIN"],
    }
    assert conn.execute("SELECT role, roles, is_active FROM authorized_users WHERE telegram_user_id=%s", (tg_driver,)).fetchone() == {
        "role": "COLLABORATOR",
        "roles": ["COLLABORATOR", "DRIVER"],
        "is_active": True,
    }

    # 4. Numeric ctv_code ("8801") routes to ctv_directory owner, NOT unrelated telegram_user_id=8801.
    conn.execute(
        "INSERT INTO authorized_users(telegram_user_id, full_name, role, is_active) "
        "VALUES (8801, 'Unrelated User 8801', 'COLLABORATOR', TRUE), (%s, 'Numeric Owner', 'COLLABORATOR', TRUE)",
        (tg_numeric_owner,),
    )
    async with pool.connection() as aconn:
        recipient, skip = await durable_work._recipient(
            aconn, {"ctv_code": "8801", "collaborator_id": 8801}, {"source": "ctv"}
        )
    assert (recipient, skip) == (tg_numeric_owner, None)
    assert await survey.lookup_telegram_user_for_collaborator("8801") == tg_numeric_owner


# ---------------------------------------------------------------------------
# S2: Auto-assign -> manual unassign preserved -> mid-survey revoke -> /forms
# ---------------------------------------------------------------------------


async def test_s2_default_auto_assign_manual_unassign_revocation_and_telegram_forms(e2e_env, monkeypatch):
    pool, conn = e2e_env.pool, e2e_env.conn
    tg_real = 92001

    forms = {
        "camp_open_1": _schema("camp_open_1", "Open Campaign 1"),
        "camp_open_2": _schema("camp_open_2", "Open Campaign 2"),
        "camp_private": _schema("camp_private", "Private Campaign", access="private"),
    }
    for mod in (ctv, admin_forms, admin_ctvs, survey):
        monkeypatch.setattr(mod, "FORM_REGISTRY", forms)
        if hasattr(mod, "refresh_form_registry_from_disk"):
            monkeypatch.setattr(mod, "refresh_form_registry_from_disk", lambda force=False: False)
    monkeypatch.setattr(admin_forms, "FORMS_STORAGE_DIR", str(e2e_env.tmp_path / "forms"))
    os.makedirs(e2e_env.tmp_path / "forms", exist_ok=True)
    monkeypatch.setattr("services.telegram_polling.FORM_REGISTRY", forms)
    monkeypatch.setattr("services.telegram_polling.refresh_form_registry_from_disk", lambda force=False: False)

    # New active non-test CTV gets open public forms only; *TEST* CTV gets none.
    assert await ctv.create_ctv_directory_entry(
        pool, "CTV_S2_REAL", display_name="Tran Thi Real", telegram_user_id=tg_real, is_active=True
    )
    assert await ctv.create_ctv_directory_entry(
        pool, "CTV_S2_TEST_01", display_name="Real Looking Name", is_active=True
    )
    assert await ctv.get_ctv_assigned_form_ids(pool, "CTV_S2_REAL", strict=True) == {"camp_open_1", "camp_open_2"}
    assert await ctv.get_ctv_assigned_form_ids(pool, "CTV_S2_TEST_01", strict=True) == set()

    app = FastAPI()
    app.include_router(admin_forms.router)
    app.include_router(admin_ctvs.router)
    app.include_router(survey.router)
    app.dependency_overrides[admin_forms.verify_admin_auth] = lambda: "super_e2e"
    app.dependency_overrides[admin_ctvs.verify_admin_auth] = lambda: "super_e2e"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://e2e.test") as client:
        # New form upload auto-assigns active non-test CTVs only.
        new_form_payload = {
            "form_id": "camp_new_s2",
            "title": "Brand New S2",
            "settings": {"access_control": "public", "ctv_ref_enabled": True, "require_manual_review": False},
            "questions": [{"id": "q1", "title": "Cau 1", "type": "text"}],
        }
        assert (await client.post("/api/v1/forms/upload-schema", json=new_form_payload)).status_code == 200
        assert "camp_new_s2" in await ctv.get_ctv_assigned_form_ids(pool, "CTV_S2_REAL", strict=True)
        assert "camp_new_s2" not in await ctv.get_ctv_assigned_form_ids(pool, "CTV_S2_TEST_01", strict=True)

        # Manually unassign CTV_S2_REAL from camp_new_s2 (keep only camp_open_1).
        upd = await client.post(
            "/admin/ctvs/CTV_S2_REAL/assignments",
            data={"form_ids": ["camp_open_1"]},
            follow_redirects=False,
        )
        assert upd.status_code == 303
        assert await ctv.get_ctv_assigned_form_ids(pool, "CTV_S2_REAL", strict=True) == {"camp_open_1"}

        # Editing camp_new_s2 must preserve manual unassignment.
        edit_payload = {**new_form_payload, "title": "Edited Brand New S2", "previous_form_id": "camp_new_s2"}
        assert (await client.post("/api/v1/forms/upload-schema", json=edit_payload)).status_code == 200
        assert "camp_new_s2" not in await ctv.get_ctv_assigned_form_ids(pool, "CTV_S2_REAL", strict=True)

        # CTV_S2_REAL opens camp_open_1 (200), then admin revokes mid-survey -> immediate 403 on submit.
        assert (await client.get("/forms/camp_open_1?ref=CTV_S2_REAL")).status_code == 200
        revoke = await client.post("/admin/ctvs/CTV_S2_REAL/assignments", data={}, follow_redirects=False)
        assert revoke.status_code == 303
        blocked_submit = await client.post(
            "/api/v1/survey/submit-webrtc",
            data={
                "form_id": "camp_open_1",
                "collaborator_id": "CTV_S2_REAL",
                "q1": "late submit after revoke",
                "gps_latitude": "10.7769",
                "gps_longitude": "106.7009",
            },
        )
        assert blocked_submit.status_code == 403

        # Re-assign only camp_open_2 -> Telegram /forms shows assigned-only link.
        assert (
            await client.post(
                "/admin/ctvs/CTV_S2_REAL/assignments",
                data={"form_ids": ["camp_open_2"]},
                follow_redirects=False,
            )
        ).status_code == 303

    runner = DriverBotRunner(bot_token="mock_s2_token", db_pool=pool)
    sent = []
    monkeypatch.setattr(runner, "send_message", AsyncMock(side_effect=lambda cid, text, **kw: sent.append((cid, text))))
    await runner.handle_forms(tg_real, tg_real)
    assert len(sent) == 1
    assert "Open Campaign 2" in sent[0][1]
    assert "ref=CTV_S2_REAL" in sent[0][1]
    assert "Open Campaign 1" not in sent[0][1]
    assert "Private Campaign" not in sent[0][1]
    assert "Edited Brand New S2" not in sent[0][1]


# ---------------------------------------------------------------------------
# S3: Private form auth + APPROVED-only CTV quota & response_limit
# ---------------------------------------------------------------------------


async def test_s3_private_form_staff_auth_approved_only_quota_and_response_limit(e2e_env, monkeypatch):
    pool, conn = e2e_env.pool, e2e_env.conn
    pw_hash = hash_password(STAFF_PASSWORD, iterations=1_000)
    for uname, role in (("s3_viewer", "viewer"), ("s3_manager", "manager"), ("s3_super", "superadmin")):
        assert await admin_accounts.create_admin_account(pool, uname, pw_hash, role)

    assert await ctv.create_ctv_directory_entry(pool, "CTV_S3", display_name="CTV S3", telegram_user_id=93001, is_active=True)

    forms = {
        "priv_s3": _schema("priv_s3", "Private Staff Survey", access="private", tracking=False),
        "quota_s3": _schema("quota_s3", "Quota Tracking Survey", access="public", tracking=True, require_manual_review=False),
        "limit_s3": _schema("limit_s3", "Capped Campaign Survey", access="public", limit=2, tracking=True, require_manual_review=False),
    }
    monkeypatch.setattr(survey, "FORM_REGISTRY", forms)
    monkeypatch.setattr(survey, "refresh_form_registry_from_disk", lambda force=False: False)
    await ctv.set_ctv_form_assignments(pool, "CTV_S3", ["quota_s3", "limit_s3"], assigned_by="s3_super")

    submit_spy = AsyncMock(
        return_value=SubmissionCommit(
            submission_id="VN-S3-OK",
            status="APPROVED",
            response={"status": "APPROVED", "submission_id": "VN-S3-OK"},
        )
    )
    monkeypatch.setattr(survey, "process_submission", submit_spy)

    app = FastAPI()
    app.include_router(survey.router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://e2e.test") as client:
        # 1. Private form blocked (403) for anonymous and CTV; allowed for all staff roles.
        assert (await client.get("/forms/priv_s3")).status_code == 403
        assert (await client.get("/forms/priv_s3?ref=CTV_S3")).status_code == 403
        assert (await client.get("/api/v1/forms/priv_s3")).status_code == 403
        assert (
            await client.post(
                "/api/v1/survey/submit-webrtc",
                data={"form_id": "priv_s3", "collaborator_id": "CTV_S3", "q1": "nope", "gps_latitude": "10.7", "gps_longitude": "106.7"},
            )
        ).status_code == 403

        for staff_user in ("s3_viewer", "s3_manager", "s3_super"):
            cookie = admin_sessions.issue(staff_user, pw_hash)
            hdrs = {"Cookie": f"{admin_sessions.COOKIE_NAME}={cookie}", **SAME_ORIGIN}
            assert (await client.get("/forms/priv_s3", headers=hdrs)).status_code == 200
            assert (await client.get("/api/v1/forms/priv_s3", headers=hdrs)).status_code == 200
            ok_sub = await client.post(
                "/api/v1/survey/submit-webrtc",
                headers=hdrs,
                data={"form_id": "priv_s3", "q1": f"by {staff_user}", "gps_latitude": "10.77", "gps_longitude": "106.70"},
            )
            assert ok_sub.status_code == 200

        # 2. APPROVED-only CTV quota (tracking-only, never blocks).
        conn.execute(
            "INSERT INTO ctv_quotas(ctv_code, scope_type, scope_value, max_submissions, period_days, active) "
            "VALUES ('CTV_S3', 'form', 'quota_s3', 2, 7, TRUE)"
        )
        for sid, st in (
            ("VN-S3-Q1", "APPROVED"),
            ("VN-S3-Q2", "REJECTED"),
            ("VN-S3-Q3", "MANUAL_REVIEW"),
            ("VN-S3-Q4", "APPEAL_PENDING"),
        ):
            conn.execute(
                "INSERT INTO survey_submissions(submission_id, form_id, ctv_code, status) VALUES (%s, 'quota_s3', 'CTV_S3', %s)",
                (sid, st),
            )
        evals = await quotas.get_ctv_quota_evaluations(pool, "CTV_S3", strict=True)
        assert len(evals) == 1 and evals[0]["used"] == 1 and evals[0]["remaining"] == 1

        # Exceed quota (3 APPROVED > max 2): still allowed to open & submit.
        for sid in ("VN-S3-Q5", "VN-S3-Q6"):
            conn.execute(
                "INSERT INTO survey_submissions(submission_id, form_id, ctv_code, status) VALUES (%s, 'quota_s3', 'CTV_S3', 'APPROVED')",
                (sid,),
            )
        assert (await quotas.get_ctv_quota_evaluations(pool, "CTV_S3", strict=True))[0]["used"] == 3
        assert (await client.get("/forms/quota_s3?ref=CTV_S3")).status_code == 200
        assert (
            await client.post(
                "/api/v1/survey/submit-webrtc",
                data={"form_id": "quota_s3", "collaborator_id": "CTV_S3", "q1": "over quota ok", "gps_latitude": "10.77", "gps_longitude": "106.70"},
            )
        ).status_code == 200

        # 3. APPROVED-only campaign response_limit=2: non-approved rows do not trip limit; 2nd APPROVED triggers HTML notice + 409.
        for sid, st in (
            ("VN-S3-L1", "APPROVED"),
            ("VN-S3-L2", "REJECTED"),
            ("VN-S3-L3", "MANUAL_REVIEW"),
            ("VN-S3-L4", "APPEAL_PENDING"),
        ):
            conn.execute(
                "INSERT INTO survey_submissions(submission_id, form_id, ctv_code, status) VALUES (%s, 'limit_s3', 'CTV_S3', %s)",
                (sid, st),
            )
        before_limit_page = await client.get("/forms/limit_s3?ref=CTV_S3")
        assert before_limit_page.status_code == 200
        assert "đạt giới hạn" not in before_limit_page.text.lower()
        assert (
            await client.post(
                "/api/v1/survey/submit-webrtc",
                data={"form_id": "limit_s3", "collaborator_id": "CTV_S3", "q1": "slot 2", "gps_latitude": "10.77", "gps_longitude": "106.70"},
            )
        ).status_code == 200

        # Insert 2nd APPROVED -> limit reached!
        conn.execute(
            "INSERT INTO survey_submissions(submission_id, form_id, ctv_code, status) VALUES ('VN-S3-L5', 'limit_s3', 'CTV_S3', 'APPROVED')"
        )
        reached_page = await client.get("/forms/limit_s3?ref=CTV_S3")
        assert reached_page.status_code == 200
        assert "giới hạn" in reached_page.text.lower() or "đủ số lượng" in reached_page.text.lower()
        assert "/api/v1/survey/submit-webrtc" not in reached_page.text

        blocked_409 = await client.post(
            "/api/v1/survey/submit-webrtc",
            data={"form_id": "limit_s3", "collaborator_id": "CTV_S3", "q1": "too late", "gps_latitude": "10.77", "gps_longitude": "106.70"},
        )
        assert blocked_409.status_code == 409


# ---------------------------------------------------------------------------
# S4: GPS block -> manual_review toggle -> Decree 13 dissent -> repeat driver
# ---------------------------------------------------------------------------


async def test_s4_gps_block_manual_review_toggle_decree13_dissent_and_repeat_driver(e2e_env, monkeypatch):
    pool, conn = e2e_env.pool, e2e_env.conn
    assert await ctv.create_ctv_directory_entry(pool, "CTV_S4", display_name="CTV S4", telegram_user_id=94001, is_active=True)
    conn.execute(
        "INSERT INTO authorized_users(telegram_user_id, username, full_name, role, is_active) "
        "VALUES (94001, 'CTV_S4', 'CTV S4', 'COLLABORATOR', TRUE)"
    )

    forms = {
        "camp_manual_s4": _schema("camp_manual_s4", "Manual Review S4", tracking=True, require_manual_review=True, with_consent=True, with_camera=True),
        "camp_auto_a": _schema("camp_auto_a", "Auto Approve A", limit=1, tracking=True, require_manual_review=False, with_consent=True, with_camera=True),
        "camp_auto_b": _schema("camp_auto_b", "Auto Approve B", tracking=True, require_manual_review=False, with_consent=True, with_camera=True),
    }
    monkeypatch.setattr(survey, "FORM_REGISTRY", forms)
    monkeypatch.setattr(survey, "refresh_form_registry_from_disk", lambda force=False: False)
    await ctv.set_ctv_form_assignments(pool, "CTV_S4", list(forms.keys()), assigned_by="super_e2e")
    conn.execute(
        "INSERT INTO ctv_quotas(ctv_code, scope_type, scope_value, max_submissions, period_days, active) "
        "VALUES ('CTV_S4', 'form', 'camp_auto_a', 5, 30, TRUE)"
    )

    app = FastAPI()
    app.include_router(survey.router)
    monkeypatch.setattr(
        "services.submission_pipeline.compute_dinov2_embedding",
        lambda _img: [0.01] * 384,
    )

    async with embedding_executor.embedding_executor_lifespan(), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://e2e.test"
    ) as client:
        # 1. Hard GPS block (400) on non-test submit when missing/invalid GPS.
        no_gps = await client.post(
            "/api/v1/survey/submit-webrtc",
            data={
                "form_id": "camp_manual_s4",
                "collaborator_id": "CTV_S4",
                "consent_choice": "Tôi đồng ý",
                "driver_phone": "0908887771",
            },
            files={"q_cam_0": ("photo.jpg", _make_sharp_jpeg(1), "image/jpeg")},
        )
        assert no_gps.status_code == 400
        assert no_gps.json() == {"detail": "Bắt buộc bật định vị GPS để gửi khảo sát."}

        # 2. Decree 13 dissent ("Tôi không đồng ý") on auto-approve form with response_limit=1:
        #    MUST be REJECTED and NEVER enter survey_submissions, billing ledger, quota, or limit!
        dissent_resp = await client.post(
            "/api/v1/survey/submit-webrtc",
            data={
                "form_id": "camp_auto_a",
                "collaborator_id": "CTV_S4",
                "consent_choice": "Tôi không đồng ý",
                "driver_phone": "0908887779",
                "driver_name": "Dissent Driver",
                "gps_latitude": "10.7769",
                "gps_longitude": "106.7009",
            },
            files={"q_cam_0": ("dissent.jpg", _make_sharp_jpeg(2), "image/jpeg")},
        )
        assert dissent_resp.status_code == 200
        dissent_body = dissent_resp.json()
        assert dissent_body["status"] == "REJECTED"
        assert "Nghị định 13" in dissent_body["reason"]
        assert dissent_body["driver_phone"] is None
        assert conn.execute("SELECT COUNT(*) AS n FROM survey_submissions WHERE form_id='camp_auto_a'").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM collaborator_billing_ledger").fetchone()["n"] == 0
        assert (await quotas.get_ctv_quota_evaluations(pool, "CTV_S4", strict=True))[0]["used"] == 0

        # Defense-in-depth check: direct _evaluate() + _record() with dissent NEVER returns APPROVED.
        dissent_img = _make_sharp_jpeg(3)
        sample_pil = Image.open(io.BytesIO(dissent_img))
        deps = SubmissionDependencies(
            vision=SimpleNamespace(),
            db_pool=None,
            redis=None,
            legacy_cache=None,
            media_root=str(e2e_env.tmp_path / "photos"),
            dev_storage_file=str(e2e_env.tmp_path / "dev.json"),
            append_dev_record=lambda *a, **kw: None,
            image_max_dimension=1024,
            blur_threshold=40.0,
            normalize_image=lambda b, max_dimension=1024: (b, sample_pil.copy()),
            production=False,
            durable_enabled=False,
            schedule_legacy=lambda w: None,
        )
        for schema_obj in (forms["camp_manual_s4"], forms["camp_auto_a"]):
            inp = SubmissionInput(
                form_id=schema_obj.form_id,
                schema=schema_obj,
                delivery_schema=schema_obj,
                fields={"consent_choice": "Tôi không đồng ý", "q1": "nope", "gps_latitude": "10.77", "gps_longitude": "106.70"},
                uploads=(SubmissionUpload("q_cam", 0, "p.jpg", dissent_img),),
                upload_error=None,
                question_id=None,
                driver_phone="0908887779",
                driver_name="Dissent Driver",
                license_plate=None,
                collaborator_id="CTV_S4",
                vehicle_type="CAR",
                gross_revenue="0",
                trip_count="0",
                signature_png=None,
                is_test_mode=False,
                bypass_img_check=False,
                idempotency_key=None,
                received_at=datetime.now(timezone.utc),
            )
            prep = _prepare(inp)
            ev = await _evaluate(inp, deps, prep)
            assert ev.status == "REJECTED"
            assert ev.is_approved is False
            assert "Nghị định 13" in ev.reason
            rec, _weeks = _record(inp, prep, ev, "VN-DISSENT-CHECK", [])
            assert rec["status"] == "REJECTED"
            assert rec["driver_phone"] is None
            assert rec["driver_name"] is None
            assert rec["form_data"]["_delivery_policy"]["source"] == "none"

        # 3. require_manual_review=True -> persists MANUAL_REVIEW.
        manual_resp = await client.post(
            "/api/v1/survey/submit-webrtc",
            data={
                "form_id": "camp_manual_s4",
                "collaborator_id": "CTV_S4",
                "consent_choice": "Tôi đồng ý",
                "driver_phone": "0908887771",
                "driver_name": "Driver Repeat",
                "license_plate": "51G-12345",
                "q1": "manual review path",
                "gps_latitude": "10.7769",
                "gps_longitude": "106.7009",
            },
            files={"q_cam_0": ("manual.jpg", _make_sharp_jpeg(4), "image/jpeg")},
        )
        assert manual_resp.status_code == 200
        manual_sid = manual_resp.json()["submission_id"]
        assert manual_resp.json()["status"] == "MANUAL_REVIEW"
        assert conn.execute("SELECT status FROM survey_submissions WHERE submission_id=%s", (manual_sid,)).fetchone()["status"] == "MANUAL_REVIEW"

        # 4. Retry after REJECTED on camp_auto_a + repeat driver across 2 campaigns (camp_auto_a & camp_auto_b).
        # Seed a prior REJECTED submission for same driver_phone + receipt_serial_no on camp_auto_a.
        conn.execute(
            "INSERT INTO survey_submissions(submission_id, form_id, ctv_code, driver_phone, receipt_serial_no, store_tax_id, status) "
            "VALUES ('VN-S4-REJ-PRIOR', 'camp_auto_a', 'CTV_S4', '0908887771', 'SN-51G-12345', 'TAX-001', 'REJECTED')"
        )
        retry_resp = await client.post(
            "/api/v1/survey/submit-webrtc",
            data={
                "form_id": "camp_auto_a",
                "collaborator_id": "CTV_S4",
                "consent_choice": "Tôi đồng ý",
                "driver_phone": "0908887771",
                "driver_name": "Driver Repeat",
                "license_plate": "51G-12345",
                "q1": "retry after rejected",
                "gps_latitude": "10.7769",
                "gps_longitude": "106.7009",
            },
            files={"q_cam_0": ("retry.jpg", _make_sharp_jpeg(5), "image/jpeg")},
        )
        assert retry_resp.status_code == 200
        assert retry_resp.json()["status"] == "APPROVED"

        # Same driver submits to second campaign camp_auto_b -> APPROVED too.
        camp_b_resp = await client.post(
            "/api/v1/survey/submit-webrtc",
            data={
                "form_id": "camp_auto_b",
                "collaborator_id": "CTV_S4",
                "consent_choice": "Tôi đồng ý",
                "driver_phone": "0908887771",
                "driver_name": "Driver Repeat",
                "license_plate": "51G-12345",
                "q1": "second campaign",
                "gps_latitude": "10.7769",
                "gps_longitude": "106.7009",
            },
            files={"q_cam_0": ("camp_b.jpg", _make_sharp_jpeg(6), "image/jpeg")},
        )
        assert camp_b_resp.status_code == 200
        assert camp_b_resp.json()["status"] == "APPROVED"


# ---------------------------------------------------------------------------
# S5: Web appeal -> /admin/appeals approve/reject + audit + role boundaries
# ---------------------------------------------------------------------------


async def test_s5_web_appeal_admin_moderation_audit_log_and_role_boundaries(e2e_env):
    pool, conn = e2e_env.pool, e2e_env.conn
    pw_hash = hash_password(STAFF_PASSWORD, iterations=1_000)
    for uname, role in (
        ("s5_super", "superadmin"),
        ("s5_manager", "manager"),
        ("s5_reviewer", "reviewer"),
        ("s5_viewer", "viewer"),
    ):
        assert await admin_accounts.create_admin_account(pool, uname, pw_hash, role)

    conn.execute(
        """
        INSERT INTO survey_submissions
            (submission_id, form_id, ctv_code, driver_phone, driver_name, status, rejection_reason, work_owner, decision_version)
        VALUES
            ('VN-S5-APP-1', 'income_survey', 'CTV_S5', '0901112221', 'Tai Xe A', 'REJECTED', 'Anh mo', 'outbox_v2', 1),
            ('VN-S5-APP-2', 'income_survey', 'CTV_S5', '0901112222', 'Tai Xe B', 'REJECTED', 'Sai khung hinh', 'outbox_v2', 1);
        """
    )

    app = FastAPI()
    app.include_router(survey.router)
    app.include_router(admin_appeals.router)
    app.include_router(admin_audit.router)
    app.include_router(admin_payouts.router)
    app.include_router(admin_accounts_router.router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://e2e.test") as client:
        # 1. Web appeal transitions both REJECTED submissions -> APPEAL_PENDING.
        for sid, phone in (("VN-S5-APP-1", "0901112221"), ("VN-S5-APP-2", "0901112222")):
            app_resp = await client.post(
                "/api/v1/survey/appeal",
                json={"submission_id": sid, "driver_phone": phone, "reason": "Anh goc ro net, nho kiem tra lai"},
            )
            assert app_resp.status_code == 200
            assert app_resp.json()["status"] == "APPEAL_SUBMITTED"
            assert conn.execute("SELECT status FROM survey_submissions WHERE submission_id=%s", (sid,)).fetchone()["status"] == "APPEAL_PENDING"

        # 2. Viewer: read-only on /admin/appeals, 403 on mutations & restricted admin routes.
        viewer_auth = ("s5_viewer", STAFF_PASSWORD)
        assert (await client.get("/admin/appeals", auth=viewer_auth)).status_code == 200
        assert (
            await client.post("/admin/appeals/VN-S5-APP-1/approve", auth=viewer_auth, data={"notes": "no"}, follow_redirects=False)
        ).status_code == 403
        assert (
            await client.post("/admin/appeals/VN-S5-APP-2/reject", auth=viewer_auth, data={"notes": "no"}, follow_redirects=False)
        ).status_code == 403
        assert (await client.get("/admin/audit", auth=viewer_auth)).status_code == 403
        assert (await client.get("/admin/payouts", auth=viewer_auth)).status_code == 403
        assert (await client.get("/admin/accounts", auth=viewer_auth)).status_code == 403

        # 3. Reviewer: allowed on /admin/appeals (approve & reject), blocked on audit/payouts/accounts.
        reviewer_auth = ("s5_reviewer", STAFF_PASSWORD)
        assert (await client.get("/admin/appeals", auth=reviewer_auth)).status_code == 200
        ok_approve = await client.post(
            "/admin/appeals/VN-S5-APP-1/approve",
            auth=reviewer_auth,
            data={"notes": "Duyet phuc khao hop le"},
            follow_redirects=False,
        )
        assert ok_approve.status_code == 303 and ok_approve.headers["location"] == "/admin/appeals?msg=approved"
        ok_reject = await client.post(
            "/admin/appeals/VN-S5-APP-2/reject",
            auth=reviewer_auth,
            data={"notes": "Anh khong dat chuan"},
            follow_redirects=False,
        )
        assert ok_reject.status_code == 303 and ok_reject.headers["location"] == "/admin/appeals?msg=rejected"
        assert (await client.get("/admin/audit", auth=reviewer_auth)).status_code == 403
        assert (await client.get("/admin/payouts", auth=reviewer_auth)).status_code == 403
        assert (await client.get("/admin/accounts", auth=reviewer_auth)).status_code == 403

        # Verify submission statuses and APPEAL_APPROVED / APPEAL_REJECTED audit rows.
        assert conn.execute("SELECT status FROM survey_submissions WHERE submission_id='VN-S5-APP-1'").fetchone()["status"] == "APPROVED"
        assert conn.execute("SELECT status FROM survey_submissions WHERE submission_id='VN-S5-APP-2'").fetchone()["status"] == "REJECTED"
        audits = {
            r["entity_id"]: r
            for r in conn.execute(
                "SELECT entity_id, action, actor_id, details FROM system_audit_log "
                "WHERE component_name='ADMIN_APPEALS' AND entity_id IN ('VN-S5-APP-1', 'VN-S5-APP-2')"
            ).fetchall()
        }
        assert audits["VN-S5-APP-1"]["action"] == "APPEAL_APPROVED"
        assert audits["VN-S5-APP-1"]["actor_id"] == "s5_reviewer"
        assert audits["VN-S5-APP-2"]["action"] == "APPEAL_REJECTED"
        assert audits["VN-S5-APP-2"]["actor_id"] == "s5_reviewer"

        # 4. Manager: allowed on /admin/audit & /api/v1/audit/logs, blocked on /admin/payouts & /admin/accounts.
        manager_auth = ("s5_manager", STAFF_PASSWORD)
        assert (await client.get("/admin/audit", auth=manager_auth)).status_code == 200
        audit_api = await client.get("/api/v1/audit/logs", auth=manager_auth)
        assert audit_api.status_code == 200
        actions_seen = {entry["action"] for entry in audit_api.json()["logs"]}
        assert {"APPEAL_APPROVED", "APPEAL_REJECTED"} <= actions_seen
        assert (await client.get("/admin/payouts", auth=manager_auth)).status_code == 403
        assert (await client.get("/admin/accounts", auth=manager_auth)).status_code == 403


# ---------------------------------------------------------------------------
# S6: Superadmin /admin/payouts status change, revoke, audit log & RBAC
# ---------------------------------------------------------------------------


async def test_s6_superadmin_payouts_status_change_revoke_audit_and_rbac(e2e_env):
    pool, conn = e2e_env.pool, e2e_env.conn
    pw_hash = hash_password(STAFF_PASSWORD, iterations=1_000)
    for uname, role in (
        ("s6_super", "superadmin"),
        ("s6_manager", "manager"),
        ("s6_reviewer", "reviewer"),
        ("s6_viewer", "viewer"),
    ):
        assert await admin_accounts.create_admin_account(pool, uname, pw_hash, role)

    conn.execute(
        "INSERT INTO authorized_users(telegram_user_id, username, full_name, role, is_active) "
        "VALUES (96001, 'CTV_S6', 'CTV S6', 'COLLABORATOR', TRUE)"
    )
    conn.execute(
        """
        INSERT INTO survey_submissions
            (submission_id, form_id, ctv_code, collaborator_id, status, work_owner, decision_version)
        VALUES
            ('VN-S6-PAY-1', 'income_survey', 'CTV_S6', 96001, 'APPROVED', 'outbox_v2', 1),
            ('VN-S6-PAY-2', 'income_survey', 'CTV_S6', 96001, 'APPROVED', 'legacy', 0);
        """
    )
    conn.execute(
        """
        INSERT INTO collaborator_billing_ledger (collaborator_id, submission_id, payout_amount_vnd, payout_status)
        VALUES
            (96001, 'VN-S6-PAY-1', 25000, 'UNPAID'),
            (96001, 'VN-S6-PAY-2', 15000, 'PENDING_PAYOUT');
        """
    )

    app = FastAPI()
    app.include_router(admin_payouts.router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://e2e.test") as client:
        # 1. manager, reviewer, viewer all get 403 on GET & POST /admin/payouts*.
        for non_super in ("s6_manager", "s6_reviewer", "s6_viewer"):
            auth = (non_super, STAFF_PASSWORD)
            assert (await client.get("/admin/payouts", auth=auth)).status_code == 403
            assert (
                await client.post(
                    "/admin/payouts/VN-S6-PAY-1/status",
                    auth=auth,
                    data={"payout_status": "PAID"},
                    follow_redirects=False,
                )
            ).status_code == 403
            assert (
                await client.post(
                    "/admin/payouts/VN-S6-PAY-1/revoke",
                    auth=auth,
                    data={"reason": "blocked"},
                    follow_redirects=False,
                )
            ).status_code == 403

        super_auth = ("s6_super", STAFF_PASSWORD)

        # 2. Superadmin GET /admin/payouts renders ledger rows in Vietnamese default with zero emojis.
        page = await client.get("/admin/payouts", auth=super_auth)
        assert page.status_code == 200
        assert "Quản Lý Chi Trả CTV" in page.text
        assert "VN-S6-PAY-1" in page.text and "VN-S6-PAY-2" in page.text
        assert "CTV_S6" in page.text
        assert not EMOJI_RE.search(page.text)

        # 3. Superadmin changes payout_status UNPAID -> PAID + verifies PAYOUT_STATUS_CHANGED audit log.
        pay_resp = await client.post(
            "/admin/payouts/VN-S6-PAY-1/status",
            auth=super_auth,
            data={"payout_status": "PAID", "notes": "Da thanh toan dot 1"},
            follow_redirects=False,
        )
        assert pay_resp.status_code == 303
        assert pay_resp.headers["location"] == "/admin/payouts?msg=status_updated"
        assert (
            conn.execute("SELECT payout_status FROM collaborator_billing_ledger WHERE submission_id='VN-S6-PAY-1'").fetchone()["payout_status"]
            == "PAID"
        )
        status_audit = conn.execute(
            "SELECT action, actor_id, details FROM system_audit_log "
            "WHERE component_name='ADMIN_PAYOUTS' AND action='PAYOUT_STATUS_CHANGED' AND entity_id='VN-S6-PAY-1'"
        ).fetchone()
        assert status_audit is not None
        assert status_audit["actor_id"] == "s6_super"
        assert status_audit["details"] == {
            "admin_user": "s6_super",
            "submission_id": "VN-S6-PAY-1",
            "previous_payout_status": "UNPAID",
            "new_payout_status": "PAID",
            "notes": "Da thanh toan dot 1",
        }

        # 4. Superadmin revokes APPROVED + PAID outbox_v2 row -> status=REJECTED, payout_status=CANCELLED, PAYOUT_REVOKED audit.
        revoke_resp = await client.post(
            "/admin/payouts/VN-S6-PAY-1/revoke",
            auth=super_auth,
            data={"reason": "Phat hien gian lan sau thanh toan"},
            follow_redirects=False,
        )
        assert revoke_resp.status_code == 303
        assert revoke_resp.headers["location"] == "/admin/payouts?msg=revoked"

        sub1 = conn.execute(
            "SELECT status, rejection_reason, decision_version FROM survey_submissions WHERE submission_id='VN-S6-PAY-1'"
        ).fetchone()
        assert sub1["status"] == "REJECTED"
        assert sub1["rejection_reason"] == "Phat hien gian lan sau thanh toan"
        assert sub1["decision_version"] == 2
        assert (
            conn.execute("SELECT payout_status FROM collaborator_billing_ledger WHERE submission_id='VN-S6-PAY-1'").fetchone()["payout_status"]
            == "CANCELLED"
        )
        revoke_outbox = conn.execute(
            "SELECT event_type FROM outbox_events WHERE aggregate_id='VN-S6-PAY-1' AND event_type='REJECTED'"
        ).fetchall()
        assert len(revoke_outbox) >= 1

        revoke_audit = conn.execute(
            "SELECT action, actor_id, details FROM system_audit_log "
            "WHERE component_name='ADMIN_PAYOUTS' AND action='PAYOUT_REVOKED' AND entity_id='VN-S6-PAY-1'"
        ).fetchone()
        assert revoke_audit is not None
        assert revoke_audit["actor_id"] == "s6_super"
        assert revoke_audit["details"] == {
            "admin_user": "s6_super",
            "submission_id": "VN-S6-PAY-1",
            "previous_submission_status": "APPROVED",
            "previous_payout_status": "PAID",
            "new_submission_status": "REJECTED",
            "new_payout_status": "CANCELLED",
            "reason": "Phat hien gian lan sau thanh toan",
        }

        # 5. Superadmin revokes legacy row with blank reason -> defaults to "Thu hồi bởi quản trị viên".
        revoke_legacy = await client.post(
            "/admin/payouts/VN-S6-PAY-2/revoke",
            auth=super_auth,
            data={"reason": "   "},
            follow_redirects=False,
        )
        assert revoke_legacy.status_code == 303
        assert revoke_legacy.headers["location"] == "/admin/payouts?msg=revoked"
        sub2 = conn.execute("SELECT status, rejection_reason FROM survey_submissions WHERE submission_id='VN-S6-PAY-2'").fetchone()
        assert sub2["status"] == "REJECTED"
        assert sub2["rejection_reason"] == "Thu hồi bởi quản trị viên"
        assert (
            conn.execute("SELECT payout_status FROM collaborator_billing_ledger WHERE submission_id='VN-S6-PAY-2'").fetchone()["payout_status"]
            == "CANCELLED"
        )

    # 6. Verify nav, i18n (VI default + EN switch), and zero emojis in router source.
    nav_js = (ROOT / "static/ui/admin_nav.js").read_text(encoding="utf-8")
    i18n_js = (ROOT / "static/ui/admin_i18n.js").read_text(encoding="utf-8")
    payouts_src = (ROOT / "routers/admin_payouts.py").read_text(encoding="utf-8")
    assert "'/admin/payouts'" in nav_js and "'nav_payouts'" in nav_js
    assert "nav_payouts: 'Chi trả CTV'" in i18n_js and "nav_payouts: 'CTV Payouts'" in i18n_js
    assert "Quản Lý Chi Trả CTV" in i18n_js and "CTV Payouts Management" in i18n_js
    assert "Thu hồi phiếu & hủy chi trả" in i18n_js and "Revoke submission & cancel payout" in i18n_js
    assert not EMOJI_RE.search(payouts_src)
