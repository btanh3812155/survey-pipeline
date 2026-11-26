"""Focused regression tests for the four scoped features:
1. Auto-assign active non-test CTVs by default (new CTV & new form without clobbering edits).
2. Per-form `require_manual_review` toggle (default True vs False auto-approve).
3. Admin Appeals page (`/admin/appeals`) with durable transition, audit log, and viewer restriction.
4. Hard-block non-test submissions when GPS coordinates are missing or invalid.
"""
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
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from core import ctv, state
from core.admin_accounts import create_admin_account, ensure_admin_accounts
from core.passwords import hash_password
from routers import admin_appeals, admin_forms, survey
from services.form_engine import FormQuestion, FormSchema, FormSettings, ImageCheckConfig, QuestionType
from services.submission_pipeline import (
    SubmissionCommit,
    SubmissionDependencies,
    SubmissionInput,
    SubmissionUpload,
    _evaluate,
    _prepare,
)
from tests.test_outbox_delivery import database


TEST_PASSWORD = "appeal-test-password"


def _schema(
    form_id: str,
    title: str,
    *,
    access: str = "public",
    limit=None,
    count: int = 0,
    tracking: bool = False,
    require_manual_review: bool = True,
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
            require_manual_review=require_manual_review,
        ),
        questions=[FormQuestion(id="q1", title="Cau 1", type=QuestionType.TEXT)],
    )


@pytest_asyncio.fixture
async def full_pg_env(database, monkeypatch):
    dsn, conn, *_ = database
    monkeypatch.setattr(ctv, "_CTV_DIRECTORY", {})
    monkeypatch.setattr(ctv, "_CTV_FORM_LINKS", {})
    monkeypatch.setattr(state.settings, "ENVIRONMENT", "test")
    test_ctvs = ("CTV_REAL_01", "CTV_TEST_99", "CTV_DEV_02", "CTV_OFF_03")
    test_admins = ("appeal_reviewer", "appeal_viewer")
    test_subs = ("VN-APP-001", "VN-APP-002", "VN-OTH-003")

    def _cleanup():
        for stmt, params in (
            ("DELETE FROM ctv_form_assignments WHERE ctv_code = ANY(%s) OR form_id = 'brand_new_campaign'", (list(test_ctvs),)),
            ("DELETE FROM ctv_directory WHERE code = ANY(%s)", (list(test_ctvs),)),
            ("DELETE FROM admin_accounts WHERE username = ANY(%s)", (list(test_admins),)),
            ("DELETE FROM outbox_events WHERE aggregate_id = ANY(%s)", (list(test_subs),)),
            ("DELETE FROM survey_submissions WHERE submission_id = ANY(%s)", (list(test_subs),)),
            ("DELETE FROM system_audit_log WHERE entity_id = ANY(%s)", (list(test_subs),)),
        ):
            try:
                conn.execute(stmt, params)
            except Exception:
                pass

    async with AsyncConnectionPool(dsn, open=False, min_size=2, max_size=4) as pool:
        await ctv.ensure_ctv_platform_schema(pool)
        await ensure_admin_accounts(pool)
        _cleanup()
        monkeypatch.setattr(state, "get_db_pool", lambda: pool)
        monkeypatch.setattr(state, "get_redis_client", lambda: SimpleNamespace(eval=AsyncMock(return_value=0)))
        monkeypatch.setattr(admin_forms, "get_db_pool", lambda: pool)
        monkeypatch.setattr(admin_appeals, "get_db_pool", lambda: pool)
        monkeypatch.setattr(survey, "get_db_pool", lambda: pool)
        try:
            yield pool, conn
        finally:
            _cleanup()


# ---------------------------------------------------------------------------
# Feature 1: Auto-assign non-test CTVs by default
# ---------------------------------------------------------------------------


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_new_ctv_and_new_form_auto_assign_non_test_only_without_clobbering_edits(
    full_pg_env, monkeypatch, tmp_path
):
    pool, conn = full_pg_env
    forms = {
        "open_pub_1": _schema("open_pub_1", "Open Public 1"),
        "open_pub_2": _schema("open_pub_2", "Open Public 2", limit=0),
        "closed_pub": _schema("closed_pub", "Closed Public", limit=1, count=1),
        "priv_form": _schema("priv_form", "Private Form", access="private"),
    }
    monkeypatch.setattr(ctv, "FORM_REGISTRY", forms)
    monkeypatch.setattr(admin_forms, "FORM_REGISTRY", forms)
    monkeypatch.setattr(admin_forms, "FORMS_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(admin_forms, "refresh_form_registry_from_disk", lambda force=False: False)

    # Active non-test CTV gets all open public forms (not private/closed).
    assert await ctv.create_ctv_directory_entry(pool, "CTV_REAL_01", display_name="Nguyen Van Real", is_active=True)
    assigned_real = await ctv.get_ctv_assigned_form_ids(pool, "CTV_REAL_01", strict=True)
    assert assigned_real == {"open_pub_1", "open_pub_2"}
    rows = conn.execute(
        "SELECT form_id, assigned_by FROM ctv_form_assignments WHERE ctv_code='CTV_REAL_01' ORDER BY form_id"
    ).fetchall()
    assert [(r["form_id"], r["assigned_by"]) for r in rows] == [
        ("open_pub_1", "default_new_ctv"),
        ("open_pub_2", "default_new_ctv"),
    ]

    # Test CTV (by code or display_name) and inactive CTV are NOT auto-assigned.
    assert await ctv.create_ctv_directory_entry(pool, "CTV_TEST_99", display_name="Normal Name", is_active=True)
    assert await ctv.create_ctv_directory_entry(pool, "CTV_DEV_02", display_name="Tai khoan test", is_active=True)
    assert await ctv.create_ctv_directory_entry(pool, "CTV_OFF_03", display_name="Inactive Real", is_active=False)
    assert await ctv.get_ctv_assigned_form_ids(pool, "CTV_TEST_99", strict=True) == set()
    assert await ctv.get_ctv_assigned_form_ids(pool, "CTV_DEV_02", strict=True) == set()
    assert await ctv.get_ctv_assigned_form_ids(pool, "CTV_OFF_03", strict=True) == set()

    # Brand-new form created via upload-schema assigns all active non-test CTVs only.
    app = FastAPI()
    app.include_router(admin_forms.router)
    app.dependency_overrides[admin_forms.verify_admin_auth] = lambda: "admin_tester"
    client = TestClient(app)

    new_payload = {
        "form_id": "brand_new_campaign",
        "title": "Brand New Campaign",
        "settings": {"access_control": "public", "ctv_ref_enabled": True, "require_manual_review": False},
        "questions": [{"id": "q1", "title": "Cau 1", "type": "text"}],
    }
    resp = client.post("/api/v1/forms/upload-schema", json=new_payload)
    assert resp.status_code == 200

    new_form_rows = conn.execute(
        "SELECT ctv_code, assigned_by FROM ctv_form_assignments WHERE form_id='brand_new_campaign'"
    ).fetchall()
    assigned_codes = {r["ctv_code"]: r["assigned_by"] for r in new_form_rows}
    assert assigned_codes.get("CTV_REAL_01") == "default_new_form"
    assert "CTV_TEST_99" not in assigned_codes
    assert "CTV_DEV_02" not in assigned_codes
    assert "CTV_OFF_03" not in assigned_codes

    # Manually unassign CTV_REAL_01 from brand_new_campaign, then EDIT the form.
    await ctv.set_ctv_form_assignments(pool, "CTV_REAL_01", ["open_pub_1"], assigned_by="admin_tester")
    assert "brand_new_campaign" not in await ctv.get_ctv_assigned_form_ids(pool, "CTV_REAL_01", strict=True)

    # Edit both with and without previous_form_id: must NEVER re-add CTV_REAL_01.
    for prev in ("brand_new_campaign", ""):
        edit_payload = {**new_payload, "title": f"Updated Title {prev}", "previous_form_id": prev}
        edit_resp = client.post("/api/v1/forms/upload-schema", json=edit_payload)
        assert edit_resp.status_code == 200
        assert "brand_new_campaign" not in await ctv.get_ctv_assigned_form_ids(pool, "CTV_REAL_01", strict=True)


# ---------------------------------------------------------------------------
# Feature 2: Per-form require_manual_review toggle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_manual_review_setting_controls_pipeline_verdict_and_editor_ui():
    assert FormSettings().require_manual_review is True

    buf = io.BytesIO()
    sample_img = Image.new("RGB", (120, 120), (30, 140, 210))
    sample_img.save(buf, format="JPEG")
    img_bytes = buf.getvalue()

    deps = SubmissionDependencies(
        vision=SimpleNamespace(),
        db_pool=None,
        redis=None,
        legacy_cache=None,
        media_root="/tmp",
        dev_storage_file="/tmp/dev.json",
        append_dev_record=lambda *a, **kw: None,
        image_max_dimension=1024,
        blur_threshold=40.0,
        normalize_image=lambda b, max_dimension=1024: (b, sample_img.copy()),
        production=False,
        durable_enabled=False,
        schedule_legacy=lambda w: None,
    )

    for require_manual, expected_status in ((True, "MANUAL_REVIEW"), (False, "APPROVED")):
        schema = FormSchema(
            form_id=f"manual_flag_{require_manual}",
            title="Manual Flag Form",
            settings=FormSettings(
                access_control="public",
                ctv_ref_enabled=False,
                decree13_required=False,
                require_manual_review=require_manual,
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
            questions=[
                FormQuestion(id="q1", title="Cau 1", type=QuestionType.TEXT),
                FormQuestion(id="q_cam", title="Anh", type=QuestionType.CAMERA_WEBRTC, required=False),
            ],
        )
        inp = SubmissionInput(
            form_id=schema.form_id,
            schema=schema,
            delivery_schema=schema,
            fields={"q1": "ok", "gps_latitude": "10.7769", "gps_longitude": "106.7009"},
            uploads=(SubmissionUpload("q_cam", 0, "photo.jpg", img_bytes),),
            upload_error=None,
            question_id=None,
            driver_phone="0901234567",
            driver_name="Tai xe",
            license_plate=None,
            collaborator_id=None,
            vehicle_type="CAR",
            gross_revenue="0",
            trip_count="0",
            signature_png=None,
            is_test_mode=False,
            bypass_img_check=False,
            idempotency_key=None,
            received_at=datetime.now(timezone.utc),
        )
        prepared = _prepare(inp)
        evaluation = await _evaluate(inp, deps, prepared)
        assert evaluation.status == expected_status

    root = Path(__file__).resolve().parents[1]
    editor_html = (root / "static/ui/form_editor.html").read_text(encoding="utf-8")
    i18n_js = (root / "static/ui/admin_i18n.js").read_text(encoding="utf-8")
    assert "globalRequireManualReview" in editor_html
    assert "require_manual_review" in editor_html
    assert "Đối soát thủ công sau khi đạt kiểm tra tự động" in editor_html
    assert "Đối soát thủ công sau khi đạt kiểm tra tự động" in i18n_js
    assert "Manual review after automated checks pass" in i18n_js


# ---------------------------------------------------------------------------
# Feature 3: Admin Appeals page (`/admin/appeals`) with full audit logging
# ---------------------------------------------------------------------------


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admin_appeals_list_approve_reject_audit_and_viewer_restriction(full_pg_env, monkeypatch):
    pool, conn = full_pg_env
    pw_hash = hash_password(TEST_PASSWORD, iterations=1_000)
    assert await create_admin_account(pool, "appeal_reviewer", pw_hash, "reviewer")
    assert await create_admin_account(pool, "appeal_viewer", pw_hash, "viewer")

    # Seed two APPEAL_PENDING rows (outbox_v2) + one non-appeal row.
    conn.execute(
        """
        INSERT INTO survey_submissions
            (submission_id, form_id, ctv_code, driver_phone, driver_name, status, rejection_reason, work_owner, decision_version)
        VALUES
            ('VN-APP-001', 'income_survey', 'CTV_A', '0901111222', 'Nguyen Appeal 1', 'APPEAL_PENDING', 'Anh ro net nhung bi nham', 'outbox_v2', 1),
            ('VN-APP-002', 'income_survey', 'CTV_B', '0903333444', 'Tran Appeal 2', 'APPEAL_PENDING', 'Khieu nai tu tai xe', 'outbox_v2', 1),
            ('VN-OTH-003', 'income_survey', 'CTV_A', '0905555666', 'Le Other', 'MANUAL_REVIEW', 'Not an appeal', 'outbox_v2', 1);
        """
    )

    app = FastAPI()
    app.include_router(admin_appeals.router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://admin.test",
    ) as client:
        # Unauthenticated is rejected (303 redirect to /login for browser or 401).
        unauth = await client.get("/admin/appeals", follow_redirects=False)
        assert unauth.status_code in (303, 401)

        # Viewer can GET /admin/appeals (read-only), but CANNOT mutate via approve/reject (403).
        viewer_page = await client.get("/admin/appeals", auth=("appeal_viewer", TEST_PASSWORD))
        assert viewer_page.status_code == 200
        assert "VN-APP-001" in viewer_page.text
        assert "VN-APP-002" in viewer_page.text
        assert "VN-OTH-003" not in viewer_page.text
        assert "Chỉ xem" in viewer_page.text
        assert "/approve" not in viewer_page.text
        # No emojis or dingbats in rendered UI.
        assert not re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", viewer_page.text)

        for action in ("approve", "reject"):
            forbidden = await client.post(
                f"/admin/appeals/VN-APP-001/{action}",
                auth=("appeal_viewer", TEST_PASSWORD),
                data={"notes": "viewer attempt"},
                follow_redirects=False,
            )
            assert forbidden.status_code == 403

        # Reviewer sees action controls and can approve VN-APP-001.
        rev_page = await client.get("/admin/appeals", auth=("appeal_reviewer", TEST_PASSWORD))
        assert rev_page.status_code == 200
        assert "/admin/appeals/VN-APP-001/approve" in rev_page.text
        assert "/admin/appeals/VN-APP-001/reject" in rev_page.text

        approve_resp = await client.post(
            "/admin/appeals/VN-APP-001/approve",
            auth=("appeal_reviewer", TEST_PASSWORD),
            data={"notes": "Da doi soat anh hop le"},
            follow_redirects=False,
        )
        assert approve_resp.status_code == 303
        assert approve_resp.headers["location"] == "/admin/appeals?msg=approved"

        row1 = conn.execute(
            "SELECT status, rejection_reason, decision_version FROM survey_submissions WHERE submission_id='VN-APP-001'"
        ).fetchone()
        assert row1["status"] == "APPROVED"
        assert row1["rejection_reason"] == "Da doi soat anh hop le"
        assert row1["decision_version"] == 2

        outbox_targets = {
            r["flow_target"]
            for r in conn.execute(
                "SELECT flow_target FROM outbox_events WHERE aggregate_id='VN-APP-001'"
            ).fetchall()
        }
        assert {"TELEGRAM_NOTIFY", "BILLING"} <= outbox_targets

        audit1 = conn.execute(
            "SELECT action, actor_id, details FROM system_audit_log "
            "WHERE component_name='ADMIN_APPEALS' AND entity_id='VN-APP-001'"
        ).fetchone()
        assert audit1 is not None
        assert audit1["action"] == "APPEAL_APPROVED"
        assert audit1["actor_id"] == "appeal_reviewer"
        assert audit1["details"]["previous_status"] == "APPEAL_PENDING"
        assert audit1["details"]["notes"] == "Da doi soat anh hop le"
        assert audit1["details"]["admin_user"] == "appeal_reviewer"
        assert audit1["details"]["submission_id"] == "VN-APP-001"

        # Reviewer rejects VN-APP-002.
        reject_resp = await client.post(
            "/admin/appeals/VN-APP-002/reject",
            auth=("appeal_reviewer", TEST_PASSWORD),
            data={"notes": "Anh chup lai man hinh"},
            follow_redirects=False,
        )
        assert reject_resp.status_code == 303
        assert reject_resp.headers["location"] == "/admin/appeals?msg=rejected"

        row2 = conn.execute(
            "SELECT status, rejection_reason FROM survey_submissions WHERE submission_id='VN-APP-002'"
        ).fetchone()
        assert row2["status"] == "REJECTED"
        assert row2["rejection_reason"] == "Anh chup lai man hinh"

        audit2 = conn.execute(
            "SELECT action, actor_id, details FROM system_audit_log "
            "WHERE component_name='ADMIN_APPEALS' AND entity_id='VN-APP-002'"
        ).fetchone()
        assert audit2 is not None
        assert audit2["action"] == "APPEAL_REJECTED"
        assert audit2["actor_id"] == "appeal_reviewer"
        assert audit2["details"]["previous_status"] == "APPEAL_PENDING"
        assert audit2["details"]["notes"] == "Anh chup lai man hinh"

    # Nav & i18n wiring check
    root = Path(__file__).resolve().parents[1]
    nav_js = (root / "static/ui/admin_nav.js").read_text(encoding="utf-8")
    i18n_js = (root / "static/ui/admin_i18n.js").read_text(encoding="utf-8")
    assert "'/admin/appeals'" in nav_js and "'nav_appeals'" in nav_js
    assert "nav_appeals: 'Phúc khảo'" in i18n_js and "nav_appeals: 'Appeals'" in i18n_js
    assert "Xét Duyệt Phúc Khảo" in i18n_js and "Appeals Review" in i18n_js


# ---------------------------------------------------------------------------
# Feature 4: Hard-block submission if no GPS
# ---------------------------------------------------------------------------


def test_submit_hard_blocks_missing_or_invalid_gps_and_allows_valid_or_test_mode(monkeypatch):
    forms = {"gps_form": _schema("gps_form", "GPS Required Form", tracking=False)}
    monkeypatch.setattr(survey, "FORM_REGISTRY", forms)
    monkeypatch.setattr(survey, "refresh_form_registry_from_disk", lambda: False)
    monkeypatch.setattr(survey, "get_db_pool", lambda: None)
    monkeypatch.setattr(survey.settings, "ENVIRONMENT", "test")

    submit_spy = AsyncMock(
        return_value=SubmissionCommit(
            submission_id="VN-GPS-OK",
            status="MANUAL_REVIEW",
            response={"status": "MANUAL_REVIEW", "submission_id": "VN-GPS-OK"},
        )
    )
    monkeypatch.setattr(survey, "process_submission", submit_spy)

    app = FastAPI()
    app.include_router(survey.router)
    client = TestClient(app)

    bad_payloads = [
        {"form_id": "gps_form", "q1": "no gps"},
        {"form_id": "gps_form", "q1": "empty", "gps_latitude": "", "gps_longitude": ""},
        {"form_id": "gps_form", "q1": "zero", "gps_latitude": "0", "gps_longitude": "0"},
        {"form_id": "gps_form", "q1": "nan", "gps_latitude": "nan", "gps_longitude": "106.7"},
        {"form_id": "gps_form", "q1": "inf", "gps_latitude": "inf", "gps_longitude": "106.7"},
        {"form_id": "gps_form", "q1": "oor lat", "gps_latitude": "91.0", "gps_longitude": "106.7"},
        {"form_id": "gps_form", "q1": "oor lon", "gps_latitude": "10.7", "gps_longitude": "-181.0"},
        {"form_id": "gps_form", "q1": "bad text", "gps_latitude": "abc", "gps_longitude": "xyz"},
    ]
    for payload in bad_payloads:
        resp = client.post("/api/v1/survey/submit-webrtc", data=payload)
        assert resp.status_code == 400, (payload, resp.status_code, resp.text)
        assert resp.json() == {"detail": "Bắt buộc bật định vị GPS để gửi khảo sát."}
    assert submit_spy.await_count == 0

    # Valid GPS passes to process_submission.
    ok_resp = client.post(
        "/api/v1/survey/submit-webrtc",
        data={"form_id": "gps_form", "q1": "with gps", "gps_latitude": "10.7769", "gps_longitude": "106.7009"},
    )
    assert ok_resp.status_code == 200
    assert submit_spy.await_count == 1

    # Test mode bypasses GPS requirement.
    test_resp = client.post(
        "/api/v1/survey/submit-webrtc",
        data={"form_id": "gps_form", "q1": "test mode", "is_test_mode": "true"},
    )
    assert test_resp.status_code == 200
    assert submit_spy.await_count == 2

    # Client form UI enforces GPS before submit in Vietnamese and English.
    root = Path(__file__).resolve().parents[1]
    dyn_html = (root / "static/ui/dynamic_form.html").read_text(encoding="utf-8")
    assert "gps_required_submit" in dyn_html
    assert "Bắt buộc bật định vị GPS để gửi khảo sát" in dyn_html
    assert "GPS location is required to submit this survey" in dyn_html
    assert "if (!isTestMode && !hasValidGps)" in dyn_html
