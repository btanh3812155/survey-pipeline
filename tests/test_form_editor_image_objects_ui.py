"""Focused checks for the image-object selector in the form editor.

The editor is static HTML + inline JS (no bundler / JSDOM harness in this repo),
so this test inspects the rendered HTML for the required controls/functions and
runs `node --check` on the inline script when node is available.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

from services.form_engine import FormQuestion, FormSchema, FormSettings, QuestionType
from services.form_manager_html import render_form_editor_html


def _camera_schema(slots):
    return FormSchema(
        form_id="test_image_objects_editor",
        title="Test Image Objects Editor",
        settings=FormSettings(decree13_required=False),
        questions=[
            FormQuestion(
                id="q_cam",
                title="Ảnh chụp",
                type=QuestionType.CAMERA_WEBRTC,
                required=True,
                image_slots=slots,
            )
        ],
    )


def _embedded_schema_json(html_text: str) -> dict:
    marker = "const INITIAL_SCHEMA = "
    match = re.search(re.escape(marker), html_text)
    assert match, f"marker not found: {marker}"
    data, _ = json.JSONDecoder().raw_decode(html_text[match.end():])
    return data


def test_editor_slot_has_object_selector_and_custom_prompt():
    html = render_form_editor_html(_camera_schema([]), is_new=False)

    # Object mode dropdown replaces the free-text "role" input.
    assert '-- Tự nhập prompt --' in html
    assert 'onchange="setSlotObjectType(${qidx}, ${sidx}, this.value)"' in html
    assert "Loại đối tượng / Role (Mã AI nhận diện)" not in html

    # Custom prompt fallback plus the object-config renderer.
    assert "Prompt nhận diện tự nhập" in html
    assert "function renderSlotObjectConfig" in html
    assert "function renderSlotObjectControl" in html
    assert "function setSlotObjectType" in html
    assert "function setSlotObjectConfig" in html

    # Loader wires the API contract and keeps load failures non-blocking.
    assert "fetch('/api/v1/image-objects'" in html
    assert "credentials: 'include'" in html
    assert "window.IMAGE_OBJECTS" in html
    assert "window.CHECK_ENGINES" in html
    assert "loadImageObjects().then(" in html
    assert "renderImageObjectsWarning()" in html


def test_editor_renders_every_customizable_control_type():
    html = render_form_editor_html(_camera_schema([]), is_new=False)
    src = html[html.index("function renderSlotObjectControl"):]
    src = src[:src.index("function renderSlotObjectConfig")]

    for control_type in ("select", "toggle", "number", "month", "date", "textarea"):
        assert f"type === '{control_type}'" in src
    # Anything else falls back to a plain text input.
    assert '<input type="text"' in src
    assert "this.value, 'text')" in src
    # Toggle reuses the editor's switch classes instead of a bare checkbox.
    assert 'class="toggle-switch"' in src
    assert 'class="toggle-slider"' in src
    # Read-only extracted fields are surfaced for admins.
    assert "AI sẽ trích xuất các trường sau" in html


def test_editor_round_trips_existing_slot_fields():
    slot = {
        "slot": 0,
        "title": "Thu nhập tuần",
        "role": "weekly_income",
        "object_type": "weekly_income",
        "object_config": {"granularity": "WEEKLY", "max_days": 24},
        "ai_detection_prompt": "ignored in object mode",
        "required": True,
        "require_unique": True,
        "allow_upload": False,
    }
    html = render_form_editor_html(_camera_schema([slot]), is_new=False)
    embedded = _embedded_schema_json(html)
    restored = embedded["questions"][0]["image_slots"][0]
    assert restored["object_type"] == "weekly_income"
    assert restored["object_config"] == {"granularity": "WEEKLY", "max_days": 24}
    assert restored["title"] == "Thu nhập tuần"
    assert restored["require_unique"] is True
    assert restored["allow_upload"] is False


def test_editor_legacy_slot_without_object_type_stays_custom_prompt():
    slot = {
        "slot": 0,
        "title": "Hồ sơ",
        "role": "app_profile",
        "ai_detection_prompt": "Đọc mã đối tác và hạng thành viên",
        "required": True,
    }
    html = render_form_editor_html(_camera_schema([slot]), is_new=False)
    restored = _embedded_schema_json(html)["questions"][0]["image_slots"][0]
    assert "object_type" not in restored
    assert restored["role"] == "app_profile"
    assert restored["ai_detection_prompt"] == "Đọc mã đối tác và hạng thành viên"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_editor_inline_script_passes_node_syntax_check():
    html = render_form_editor_html(_camera_schema([]), is_new=False)
    scripts = re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", html, re.S)
    assert scripts, "editor should embed an inline script"
    for script in scripts:
        fd, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(script)
            result = subprocess.run(
                ["node", "--check", path], capture_output=True, text=True, timeout=30
            )
            assert result.returncode == 0, result.stderr
        finally:
            os.unlink(path)
