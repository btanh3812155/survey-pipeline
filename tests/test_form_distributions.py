"""Distribution form modules aggregate choice answers across submissions."""
import json
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from routers import admin_forms
from services.form_distributions import run_form_distributions
from services.form_engine import (
    FormModule,
    FormModuleInput,
    FormQuestion,
    FormSchema,
    FormSettings,
    QuestionType,
)
from services.form_manager_html import render_form_editor_html
from services.form_modules import run_form_modules
from tests.postgres_harness import apply_schema, resolve_test_database_url

FORM_ID = "DIST_TEST"


@pytest_asyncio.fixture
async def distribution_store(monkeypatch):
    import os
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("disposable PostgreSQL is required")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL")
    dsn = resolve_test_database_url()
    schema_name = "dist_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema_name)))
        apply_schema(conn)
        scoped_dsn = make_conninfo(dsn, options=f"-c search_path={schema_name},public")
        async with AsyncConnectionPool(scoped_dsn, open=False, min_size=1, max_size=3) as pool:
            with psycopg.connect(scoped_dsn, autocommit=True, row_factory=dict_row) as cur:
                rows = [
                    ("VN-DIST-1", {"vehicle": "car", "policies": "A, B"}),
                    ("VN-DIST-2", {"vehicle": "bike", "policies": ["B", "C"]}),
                    ("VN-DIST-3", {"vehicle": "car", "policies": ["B", "B"]}),
                    ("VN-DIST-4", {}),
                ]
                for sid, answers in rows:
                    cur.execute(
                        "INSERT INTO survey_submissions(submission_id,form_id,status,form_data) "
                        "VALUES (%s,%s,'APPROVED',%s::jsonb)",
                        (sid, FORM_ID, json.dumps(answers)),
                    )
            try:
                yield pool, dsn, schema_name
            finally:
                pass
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name)))


def _schema(module: FormModule) -> FormSchema:
    return FormSchema(
        form_id=FORM_ID,
        title="Distribution test",
        settings=FormSettings(modules=[module]),
        questions=[
            FormQuestion(
                id="vehicle", title="Vehicle", type=QuestionType.SINGLE_CHOICE,
                options=["car", "bike", "bus"],
            ),
            FormQuestion(
                id="policies", title="Policies", type=QuestionType.MULTIPLE_CHOICE,
                options=["A", "B", "C"],
            ),
        ],
    )


def _distribution_module() -> FormModule:
    return FormModule(
        uid="distribution",
        name="Answer distribution",
        kind="distribution",
        inputs=[
            FormModuleInput(name="vehicle", source="question", question_id="vehicle"),
            FormModuleInput(name="policies", source="question", question_id="policies"),
        ],
    )


@pytest.mark.asyncio
async def test_distribution_counts_single_multi_and_percentages(distribution_store):
    pool, _dsn, _schema_name = distribution_store
    schema = _schema(_distribution_module())
    reports = await run_form_distributions(schema, pool)
    assert len(reports) == 1
    report = reports[0]
    assert report["uid"] == "distribution"
    by_id = {item["question_id"]: item for item in report["distribution"]}
    vehicle = by_id["vehicle"]
    assert vehicle["answered"] == 3
    assert vehicle["values"] == [
        {"value": "car", "count": 2, "percent": 66.67},
        {"value": "bike", "count": 1, "percent": 33.33},
        {"value": "bus", "count": 0, "percent": 0.0},
    ]
    policies = by_id["policies"]
    assert policies["answered"] == 3
    counts = {item["value"]: (item["count"], item["percent"]) for item in policies["values"]}
    assert counts["A"] == (1, 33.33)
    assert counts["B"] == (3, 100.0)
    assert counts["C"] == (1, 33.33)


@pytest.mark.asyncio
async def test_distribution_module_does_not_run_per_submission():
    schema = _schema(_distribution_module())
    result = await run_form_modules(schema, {"vehicle": "car", "policies": ["A"]})
    assert result["failure"] is None
    assert result["modules"]["distribution"]["aggregate_only"] is True
    assert result["outputs"] == {}


def test_distribution_rejects_non_choice_questions():
    schema = _schema(_distribution_module())
    text_question = FormQuestion(id="note", title="Note", type=QuestionType.TEXT)
    schema.questions.append(text_question)
    module = _distribution_module().model_copy(update={
        "inputs": [FormModuleInput(name="note", source="question", question_id="note")]
    })
    with pytest.raises(ValidationError):
        FormSchema(
            form_id=FORM_ID, title="Bad", settings=FormSettings(modules=[module]),
            questions=[text_question],
        )


@pytest.mark.asyncio
async def test_distribution_endpoint_returns_saved_module_report(distribution_store, monkeypatch):
    pool, _dsn, _schema_name = distribution_store
    schema = _schema(_distribution_module())
    admin_forms.FORM_REGISTRY[FORM_ID] = schema
    monkeypatch.setattr(admin_forms, "refresh_form_registry_from_disk", lambda: None)
    monkeypatch.setattr(admin_forms, "get_db_pool", lambda: pool)
    app = FastAPI()
    app.include_router(admin_forms.router)
    app.dependency_overrides[admin_forms.verify_admin_auth] = lambda: "dist-admin"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(f"/api/v1/forms/{FORM_ID}/distributions")
        assert response.status_code == 200
        body = response.json()
        assert body["form_id"] == FORM_ID
        assert body["modules"][0]["distribution"][0]["question_id"] == "vehicle"
    finally:
        admin_forms.FORM_REGISTRY.pop(FORM_ID, None)


def test_editor_exposes_distribution_builder():
    html = render_form_editor_html(_schema(_distribution_module()))
    assert "Phân bố (thống kê đáp án)" in html
    assert "loadDistributionPreview" in html
    assert "distributionPreview_" in html
    assert "Tải thống kê đã lưu" in html


def test_distribution_module_persists_with_the_form(tmp_path, monkeypatch):
    form_id = f"DIST_SAVE_{uuid4().hex[:8]}"
    schema = _schema(_distribution_module()).model_copy(update={"form_id": form_id})
    monkeypatch.setattr(admin_forms, "FORMS_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(admin_forms, "refresh_form_registry_from_disk", lambda **_: None)
    app = FastAPI()
    app.include_router(admin_forms.router)
    app.dependency_overrides[admin_forms.verify_admin_auth] = lambda: "dist-admin"
    try:
        response = TestClient(app).post(
            "/api/v1/forms/upload-schema",
            json=schema.model_dump(mode="json"),
            headers={"Origin": "http://testserver"},
        )
        assert response.status_code == 200, response.text
        saved = json.loads((tmp_path / f"{form_id}.json").read_text(encoding="utf-8"))
        assert saved["settings"]["modules"][0]["kind"] == "distribution"
        assert saved["settings"]["modules"][0]["inputs"][0]["question_id"] == "vehicle"
        assert admin_forms.FORM_REGISTRY[form_id].settings.modules[0].kind == "distribution"
    finally:
        admin_forms.FORM_REGISTRY.pop(form_id, None)
        (tmp_path / f"{form_id}.json").unlink(missing_ok=True)
