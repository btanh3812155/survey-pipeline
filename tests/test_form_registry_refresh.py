"""File-backed FORM_REGISTRY refresh for multi-worker deployments.

uvicorn with multiple workers imports core.state once per process. Without a
disk-backed refresh, a schema saved by worker A stays invisible to worker B
until restart. These tests cover the refresh unit, endpoint-level convergence
after another "worker" writes a JSON directly, and a separate Python process
that must pick the same file up through refresh_form_registry_from_disk().
"""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

import core.state as state
from config import settings
from main import app
from services.form_engine import FormQuestion, FormSchema, FormSettings, QuestionType


REPO_ROOT = Path(__file__).resolve().parents[1]
FORMS_DIR = REPO_ROOT / "data" / "forms"


def _schema_dict(form_id: str, title: str = "Refresh test") -> dict:
    schema = FormSchema(
        form_id=form_id,
        title=title,
        settings=FormSettings(decree13_required=False),
        questions=[
            FormQuestion(
                id="rq1",
                title="Câu 1",
                type=QuestionType.SINGLE_CHOICE,
                options=["A", "B"],
            ),
            FormQuestion(
                id="rq2",
                title="Câu 2",
                type=QuestionType.TEXT,
                condition_groups=[[
                    {"depends_on_question_id": "rq1", "operator": "equals", "expected_value": ["A"]},
                ]],
            ),
        ],
    )
    return json.loads(schema.model_dump_json())


def _auth_headers() -> dict:
    token = base64.b64encode(
        f"{settings.ADMIN_USERNAME}:{settings.ADMIN_PASSWORD}".encode("utf-8")
    ).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def test_refresh_detects_new_changed_and_deleted_forms(tmp_path, monkeypatch):
    original_dir = state.FORMS_STORAGE_DIR
    original_registry = dict(state.FORM_REGISTRY)
    original_signature = state._FORMS_SIGNATURE
    original_disk_ids = set(state._DISK_FORM_IDS)
    try:
        monkeypatch.setattr(state, "FORMS_STORAGE_DIR", str(tmp_path))
        state.FORM_REGISTRY.clear()
        state._DISK_FORM_IDS.clear()
        state._FORMS_SIGNATURE = state._forms_disk_signature()

        form_path = tmp_path / "qa_refresh_unit.json"
        form_path.write_text(json.dumps(_schema_dict("qa_refresh_unit")), encoding="utf-8")

        assert state.refresh_form_registry_from_disk() is True
        assert "qa_refresh_unit" in state.FORM_REGISTRY
        # Unchanged directory: stat-only no-op.
        assert state.refresh_form_registry_from_disk() is False

        form_path.write_text(
            json.dumps(_schema_dict("qa_refresh_unit", title="Đã đổi tên")), encoding="utf-8"
        )
        assert state.refresh_form_registry_from_disk() is True
        assert state.FORM_REGISTRY["qa_refresh_unit"].title == "Đã đổi tên"

        form_path.unlink()
        assert state.refresh_form_registry_from_disk() is True
        assert "qa_refresh_unit" not in state.FORM_REGISTRY
    finally:
        state.FORMS_STORAGE_DIR = original_dir
        state.FORM_REGISTRY.clear()
        state.FORM_REGISTRY.update(original_registry)
        state._DISK_FORM_IDS.clear()
        state._DISK_FORM_IDS.update(original_disk_ids)
        state._FORMS_SIGNATURE = original_signature


def test_endpoints_see_form_written_by_another_worker():
    """Simulate worker B's stale dict by writing worker A's file directly."""
    form_id = f"QA_REFRESH_DISK_{os.getpid()}"
    form_path = FORMS_DIR / f"{form_id}.json"
    state.FORM_REGISTRY.pop(form_id, None)
    form_path.write_text(
        json.dumps(_schema_dict(form_id, title="Sibling worker form")), encoding="utf-8"
    )
    try:
        client = TestClient(app)
        headers = _auth_headers()

        edit_page = client.get(f"/admin/forms/edit/{form_id}", headers=headers)
        assert edit_page.status_code == 200, edit_page.text[:300]
        assert "Sibling worker form" in edit_page.text

        schema_api = client.get(f"/api/v1/forms/{form_id}")
        assert schema_api.status_code == 200
        assert schema_api.json()["title"] == "Sibling worker form"

        listing = client.get("/api/v1/forms")
        assert listing.status_code == 200
        assert form_id in {item["form_id"] for item in listing.json()["forms"]}

        public_page = client.get(f"/forms/{form_id}")
        assert public_page.status_code == 200
        assert "Sibling worker form" in public_page.text
    finally:
        form_path.unlink(missing_ok=True)
        state.FORM_REGISTRY.pop(form_id, None)
        state.refresh_form_registry_from_disk(force=True)


def test_separate_process_refreshes_from_shared_directory(tmp_path):
    """A second OS process (own FORM_REGISTRY) must see the same forms JSON."""
    form_id = "qa_refresh_xproc"
    (tmp_path / f"{form_id}.json").write_text(
        json.dumps(_schema_dict(form_id, title="Xproc form")), encoding="utf-8"
    )
    code = (
        "import core.state as st\n"
        f"st.FORMS_STORAGE_DIR = {str(tmp_path)!r}\n"
        "st.FORM_REGISTRY.clear()\n"
        "st._DISK_FORM_IDS.clear()\n"
        "st._FORMS_SIGNATURE = ()\n"
        "changed = st.refresh_form_registry_from_disk()\n"
        f"print(changed, {form_id!r} in st.FORM_REGISTRY, st.FORM_REGISTRY[{form_id!r}].title)\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert proc.stdout.strip() == "True True Xproc form"
