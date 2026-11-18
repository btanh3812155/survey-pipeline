# -*- coding: utf-8 -*-
"""File-defined image object types, check engines, and admin API."""

import io
import json
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from main import FORM_REGISTRY, app
from services.form_engine import FormQuestion, FormSchema, FormSettings, ImageCheckConfig, QuestionType
from services.image_check_engines import (
    EngineDefinitionError,
    conditions_pass,
    evaluate_condition,
    resolve_placeholders,
    run_engine,
    safe_arithmetic,
)
from services.image_objects import (
    ImageObjectDefinitionError,
    build_detection_prompt,
    delete_object,
    get_object,
    list_engines,
    list_objects,
    load_registry,
    merged_config,
    object_type_for_classified_role,
    resolve_slot_object,
    run_definition_checks,
    run_object_checks,
    save_object,
)
from services.image_object_projection import project_income_totals, project_income_weeks


def _weekly_config(**overrides):
    config = {"granularity": "WEEKLY", "target_month": "2026-10", "max_days": 24, "require_range": True}
    config.update(overrides)
    return config


# ==============================================================================
# Registry & schema
# ==============================================================================

def test_registry_loads_object_and_engine_files():
    registry = load_registry(force=True)
    assert registry["errors"] == []
    ids = {obj.id for obj in registry["objects"].values()}
    assert {"weekly_income", "app_profile", "vehicle_exterior_or_interior"} <= ids
    implementations = {engine.implementation for engine in registry["engines"].values()}
    assert {"required_fields", "date_range", "number_range", "arithmetic", "regex", "enum", "period_granularity"} <= implementations
    assert all(engine.implementation for engine in list_engines())


def test_missing_object_fails_loudly():
    with pytest.raises(ImageObjectDefinitionError):
        get_object("does_not_exist")


def test_customizable_defaults_and_slot_overrides():
    obj = get_object("weekly_income")
    defaults = merged_config(obj, {})
    assert defaults["granularity"] == "WEEKLY"
    assert defaults["target_month"] == "2026-10"
    assert defaults["max_days"] == 24
    assert defaults["require_range"] is True

    overridden = merged_config(obj, {"granularity": "DAILY", "max_days": 1})
    assert overridden["granularity"] == "DAILY"
    assert overridden["max_days"] == 1
    assert overridden["target_month"] == "2026-10"


def test_placeholder_substitution_keeps_type_and_renders_text():
    config = {"granularity": "WEEKLY", "max_days": 24}
    resolved = resolve_placeholders(
        {"expected": "{{granularity}}", "max_days": "{{max_days}}", "label": "Kỳ {{granularity}}/{{max_days}}"},
        config,
    )
    assert resolved["expected"] == "WEEKLY"
    assert resolved["max_days"] == 24
    assert resolved["label"] == "Kỳ WEEKLY/24"


def test_detection_prompt_is_built_from_object_and_slot_config():
    obj = get_object("weekly_income")
    prompt = build_detection_prompt(obj, {"granularity": "MONTHLY", "max_days": 31, "require_range": False})
    assert "driver income screen" in prompt
    assert "Doanh thu gộp" in prompt
    assert "MONTHLY" in prompt
    assert "31" in prompt


def test_slot_object_resolution_and_legacy_role_compatibility():
    obj = resolve_slot_object({"object_type": "weekly_income"})
    assert obj.id == "weekly_income"
    assert object_type_for_classified_role("app_income") == "weekly_income"
    assert object_type_for_classified_role("app_income_week_3") == "weekly_income"
    assert object_type_for_classified_role("app_profile") == "app_profile"
    assert object_type_for_classified_role("vehicle_exterior") == "vehicle_exterior_or_interior"
    legacy = resolve_slot_object({"role": "app_income", "title": "Thu nhập tuần"})
    assert legacy.id == "weekly_income"
    unknown = resolve_slot_object({"role": "custom_object", "title": "Ảnh bất kỳ"})
    assert unknown.id.startswith("legacy_")


def test_custom_prompt_wins_over_legacy_role_and_title():
    slot = {
        "role": "app_income",
        "title": "Tuần thu nhập tháng 10",
        "ai_detection_prompt": "CUSTOM PROMPT ONLY",
    }
    obj = resolve_slot_object(slot)
    assert obj.id == "custom_app_income"
    assert obj.prompt == "CUSTOM PROMPT ONLY"
    assert obj.expected_fields == []
    assert obj.engines == []
    assert build_detection_prompt(obj) == "CUSTOM PROMPT ONLY"
    result = run_definition_checks(obj, {"anything": 1}, {})
    assert result["passed"] is True
    assert result["engines"] == []


def test_legacy_role_with_empty_prompt_still_resolves_known_object():
    obj = resolve_slot_object({"role": "app_income", "title": "Thu nhập tuần", "ai_detection_prompt": ""})
    assert obj.id == "weekly_income"
    assert obj.engines


def test_custom_prompt_object_save_roundtrip():
    object_id = "pytest_custom_prompt_object"
    try:
        save_object(object_id, {
            "label": "Custom prompt",
            "description": "tmp",
            "prompt": "CUSTOM ONLY {{objects}}",
            "objects": ["thing"],
            "expected_fields": [],
            "customizable": [],
            "engines": [],
            "extractor": "generic",
        })
        loaded = get_object(object_id)
        assert loaded.prompt == "CUSTOM ONLY {{objects}}"
        assert build_detection_prompt(loaded) == "CUSTOM ONLY thing"
    finally:
        delete_object(object_id)



def test_default_form_and_migrated_form_have_object_types():
    migrated = json.load(open("data/forms/TB_GSM_2026_Nov_Test.json", encoding="utf-8"))
    schema = FormSchema.model_validate(migrated)
    types = [
        slot.get("object_type")
        for question in schema.questions
        for slot in (question.image_slots or [])
    ]
    assert "app_profile" in types
    assert "vehicle_exterior_or_interior" in types
    assert types.count("weekly_income") == 5
    income_slots = [
        slot for question in schema.questions for slot in (question.image_slots or [])
        if slot.get("object_type") == "weekly_income"
    ]
    assert all(slot["object_config"]["target_month"] == "2026-10" for slot in income_slots)


# ==============================================================================
# Engines
# ==============================================================================

def test_conditions_gate_engine_execution():
    extraction = {"period_raw_text": "28/09 - 04/10"}
    assert conditions_pass([{"field": "period_raw_text", "op": "not_empty"}], extraction)
    assert not conditions_pass([{"field": "period_raw_text", "op": "empty"}], extraction)
    assert evaluate_condition({"field": "gross_revenue", "op": "gte", "value": 100}, {"gross_revenue": 200})
    assert not evaluate_condition({"field": "gross_revenue", "op": "lte", "value": 100}, {"gross_revenue": 200})

    engine = {"id": "req", "implementation": "required_fields"}
    binding = {"id": "req", "params": {"fields": ["gross_revenue"]}, "conditions": [{"field": "period_raw_text", "op": "empty"}]}
    result = run_engine(engine, binding, extraction, {})
    assert result["skipped"] is True and result["passed"] is True


def test_required_fields_engine():
    result = run_object_checks("weekly_income", {"period": "Tuần 1"}, _weekly_config())
    assert result["passed"] is False
    assert "Thiếu trường dữ liệu bắt buộc" in result["reasons"][0]

    result = run_object_checks("weekly_income", {"gross_revenue": 1000, "period": "Tuần 1"}, _weekly_config())
    assert result["passed"] is True


@pytest.mark.parametrize(
    "period,passed",
    [
        ("Tháng 10/2026", False),
        ("15/10/2026", False),
        ("15/10 - 21/10", True),
        ("28/09/2026 - 04/10/2026", True),
        ("Tuần 1", True),
    ],
)
def test_period_granularity_engine(period, passed):
    result = run_object_checks(
        "weekly_income",
        {"gross_revenue": 5_000_000, "net_income": 4_000_000, "discount_and_tax": 1_000_000, "period": period, "detected_app": "XANH_SM", "is_legible": True, "confidence_score": 0.95},
        _weekly_config(),
    )
    assert result["passed"] is passed, result["reasons"]


def test_date_range_engine_cross_month_target_window():
    engine = {"id": "dr", "implementation": "date_range"}
    binding = {"id": "dr", "params": {"field": "period_raw_text", "target_month": "2026-10", "max_days": 24, "granularity": "WEEKLY"}}

    cross_start = run_engine(engine, binding, {"period_raw_text": "28/09 - 04/10"}, {})
    assert cross_start["passed"] is True, cross_start["reason"]

    cross_end = run_engine(engine, binding, {"period_raw_text": "26/10 - 01/11"}, {})
    assert cross_end["passed"] is True, cross_end["reason"]

    wrong_month = run_engine(engine, binding, {"period_raw_text": "05/09 - 11/09"}, {})
    assert wrong_month["passed"] is False
    assert "không khớp tháng mục tiêu" in wrong_month["reason"]


def test_period_granularity_engine_cross_month_target_window():
    cross_start = run_object_checks(
        "weekly_income",
        {"gross_revenue": 5_000_000, "net_income": 4_000_000, "discount_and_tax": 1_000_000, "period": "28/09 - 04/10", "detected_app": "XANH_SM", "is_legible": True, "confidence_score": 0.95},
        _weekly_config(),
    )
    assert cross_start["passed"] is True, cross_start["reasons"]

    cross_end = run_object_checks(
        "weekly_income",
        {"gross_revenue": 5_000_000, "net_income": 4_000_000, "discount_and_tax": 1_000_000, "period": "26/10 - 01/11", "detected_app": "XANH_SM", "is_legible": True, "confidence_score": 0.95},
        _weekly_config(),
    )
    assert cross_end["passed"] is True, cross_end["reasons"]

    wrong_month = run_object_checks(
        "weekly_income",
        {"gross_revenue": 5_000_000, "net_income": 4_000_000, "discount_and_tax": 1_000_000, "period": "05/09 - 11/09", "detected_app": "XANH_SM", "is_legible": True, "confidence_score": 0.95},
        _weekly_config(),
    )
    assert wrong_month["passed"] is False
    assert any("tháng mục tiêu" in reason for reason in wrong_month["reasons"])


def test_number_range_engine():
    engine = {"id": "nr", "implementation": "number_range"}
    binding = {"id": "nr", "params": {"field": "confidence_score", "min": 0.6}}
    assert run_engine(engine, binding, {"confidence_score": 0.9}, {})["passed"] is True
    failed = run_engine(engine, binding, {"confidence_score": 0.2}, {})
    assert failed["passed"] is False and "tối thiểu" in failed["reason"]


def test_arithmetic_engine_is_safe_ast_not_eval():
    value, error = safe_arithmetic("(gross - abs(discount) - net) / max(gross, 1)", {"gross": 10, "discount": -2, "net": 8})
    assert error is None and value == 0.0

    value, error = safe_arithmetic("__import__('os').system('echo pwn')", {})
    assert value is None and "không được phép" in error

    engine = {"id": "arith", "implementation": "arithmetic"}
    binding = {"id": "arith", "params": {"expression": "net - gross", "tolerance": 0, "direction": "lte", "message": "Số học không khớp"}}
    assert run_engine(engine, binding, {"net": 8, "gross": 10}, {})["passed"] is True
    assert run_engine(engine, binding, {"net": 12, "gross": 10}, {})["passed"] is False


def test_regex_and_enum_engines():
    regex_engine = {"id": "rx", "implementation": "regex"}
    assert run_engine(regex_engine, {"id": "rx", "params": {"field": "plate", "pattern": r"^\d{2}[A-Z]"}}, {"plate": "29E-123"}, {})["passed"] is True
    assert run_engine(regex_engine, {"id": "rx", "params": {"field": "plate", "pattern": r"^\d{2}[A-Z]"}}, {"plate": "bad"}, {})["passed"] is False

    enum_engine = {"id": "en", "implementation": "enum"}
    assert run_engine(enum_engine, {"id": "en", "params": {"field": "app", "allowed": ["XANH_SM"]}}, {"app": "XANH_SM"}, {})["passed"] is True
    assert run_engine(enum_engine, {"id": "en", "params": {"field": "app", "allowed": ["XANH_SM"]}}, {"app": "GRAB"}, {})["passed"] is False


def test_unknown_engine_implementation_fails_loudly():
    with pytest.raises(EngineDefinitionError):
        run_engine({"id": "bad", "implementation": "arbitrary_python"}, {"id": "bad"}, {}, {})


# ==============================================================================
# Weekly-income preserved behavior
# ==============================================================================

def test_weekly_income_daily_monthly_single_day_and_valid_week():
    base = {"gross_revenue": 5_000_000, "net_income": 4_000_000, "discount_and_tax": 1_000_000, "detected_app": "XANH_SM", "is_legible": True, "confidence_score": 0.95}

    monthly = run_object_checks("weekly_income", {**base, "period": "Tháng 10/2026"}, _weekly_config())
    assert monthly["passed"] is False and "THÁNG" in monthly["reasons"][0] and "TUẦN" in monthly["reasons"][0]

    daily_tab = run_object_checks("weekly_income", {**base, "period": "Hôm nay (15/10/2026)", "active_tab": "DAILY"}, _weekly_config())
    assert daily_tab["passed"] is False and "NGÀY" in daily_tab["reasons"][0]

    single_day = run_object_checks("weekly_income", {**base, "period": "15/10/2026 - 15/10/2026"}, _weekly_config())
    assert single_day["passed"] is False and "1 ngày" in single_day["reasons"][0]

    valid = run_object_checks("weekly_income", {**base, "period": "28/09/2026 - 04/10/2026"}, _weekly_config())
    assert valid["passed"] is True, valid["reasons"]


def test_weekly_income_config_changes_target_month_and_max_days():
    base = {"gross_revenue": 5_000_000, "net_income": 4_000_000, "discount_and_tax": 1_000_000, "detected_app": "XANH_SM", "is_legible": True, "confidence_score": 0.95, "period": "05/10 - 11/10"}
    assert run_object_checks("weekly_income", base, _weekly_config())["passed"] is True
    wrong_month = run_object_checks("weekly_income", base, _weekly_config(target_month="2026-09"))
    assert wrong_month["passed"] is False
    too_long = run_object_checks("weekly_income", {**base, "period": "01/10 - 10/10"}, _weekly_config(max_days=7))
    assert too_long["passed"] is False


def test_projection_adapter_is_not_fixed_to_five_weeks():
    results = [
        {"object_type": "weekly_income", "extraction": {"gross_revenue": 1_000_000 * n, "net_income": 800_000 * n, "total_trips": 10 * n, "period": f"0{n}/10 - 0{n}/10"}}
        for n in range(1, 8)
    ]
    weeks = project_income_weeks(results)
    assert len(weeks) == 7
    assert weeks["week_7"]["gross_revenue"] == 7_000_000
    totals = project_income_totals(weeks)
    assert totals["total_gross_revenue"] == sum(1_000_000 * n for n in range(1, 8))
    assert totals["total_trips"] == sum(10 * n for n in range(1, 8))


def test_projection_adapter_accepts_any_income_bearing_object():
    weeks = project_income_weeks([
        {"object_type": "custom_profit_screen", "extraction": {"gross_revenue": 500_000, "net_income": 400_000}},
        {"object_type": "app_profile", "extraction": {"membership_type": "Vàng"}},
    ])
    assert list(weeks) == ["week_1"]
    assert weeks["week_1"]["gross_revenue"] == 500_000


# ==============================================================================
# Admin page + API
# ==============================================================================

def test_admin_api_returns_on_disk_definitions():
    client = TestClient(app)
    response = client.get("/api/v1/image-objects", auth=("admin", "12345678"))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"objects", "engines"}
    object_ids = {item["id"] for item in body["objects"]}
    assert {"weekly_income", "app_profile", "vehicle_exterior_or_interior"} <= object_ids
    assert any(engine["implementation"] == "period_granularity" for engine in body["engines"])


def test_admin_page_lists_objects_and_engines():
    client = TestClient(app)
    response = client.get("/admin/image-objects", auth=("admin", "12345678"))
    assert response.status_code == 200
    assert "weekly_income" in response.text
    assert "period_granularity" in response.text

    forms_page = client.get("/admin/forms", auth=("admin", "12345678"))
    assert forms_page.status_code == 200
    assert 'href="/admin/image-objects"' in forms_page.text


def test_admin_save_and_delete_object_roundtrip():
    object_id = "pytest_image_object"
    try:
        saved = save_object(object_id, {"label": "Pytest object", "description": "tmp"})
        assert saved.id == object_id
        assert os.path.exists(os.path.join("data", "image_objects", f"{object_id}.json"))
        assert get_object(object_id).label == "Pytest object"
    finally:
        delete_object(object_id)
    assert object_id not in {obj.id for obj in list_objects()}


# ==============================================================================
# Runtime: object_type form and legacy form
# ==============================================================================

def _jpeg_bytes(color=(30, 160, 180)):
    buffer = io.BytesIO()
    Image.new("RGB", (200, 300), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _probe_form(form_id: str, slots):
    FORM_REGISTRY[form_id] = FormSchema(
        form_id=form_id,
        title="Image Object Probe",
        settings=FormSettings(
            decree13_required=False,
            is_test_mode=True,
            image_checks=ImageCheckConfig(enable_blur_check=False, enable_ai_vision=True, enable_anti_fraud=False),
        ),
        questions=[FormQuestion(
            id="q_cam",
            title="Anh",
            type=QuestionType.CAMERA_WEBRTC,
            required=True,
            image_slots=slots,
            min_images=len(slots),
            max_images=len(slots),
            exact_image_count=len(slots),
        )],
    )


def test_runtime_accepts_legacy_form_without_object_type(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    form_id = "SIM-LEGACY-IMAGE-OBJECTS"
    _probe_form(form_id, [
        {"slot": 0, "title": "Ho so", "role": "app_profile", "required": True},
        {"slot": 1, "title": "Thu nhap", "role": "app_income", "required": True},
    ])
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/survey/submit-webrtc",
            data={"form_id": form_id, "is_test_mode": "true", "q_cam_0": "uploaded_0", "q_cam_1": "uploaded_1"},
            files=[
                ("survey_photo_0", ("profile.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")),
                ("survey_photo_1", ("income.jpg", io.BytesIO(_jpeg_bytes((220, 40, 40))), "image/jpeg")),
            ],
        )
        body = response.json()
        assert "Thiếu ảnh" not in (body.get("reason") or "")
        assert "Cấu hình kiểm tra ảnh không hợp lệ" not in (body.get("reason") or "")
    finally:
        FORM_REGISTRY.pop(form_id, None)


def test_runtime_custom_prompt_mode_skips_hardcoded_income_checks(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    form_id = "SIM-CUSTOM-PROMPT-RUNTIME"
    _probe_form(form_id, [
        {"slot": 0, "title": "Ho so tuy chinh", "role": "app_profile", "ai_detection_prompt": "CUSTOM PROFILE PROMPT", "required": True},
        {"slot": 1, "title": "Thu nhap tuy chinh", "role": "app_income", "ai_detection_prompt": "CUSTOM INCOME PROMPT", "required": True},
    ])
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/survey/submit-webrtc",
            data={"form_id": form_id, "is_test_mode": "true", "q_cam_0": "uploaded_0", "q_cam_1": "uploaded_1"},
            files=[
                ("survey_photo_0", ("profile.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")),
                ("survey_photo_1", ("income.jpg", io.BytesIO(_jpeg_bytes((220, 40, 40))), "image/jpeg")),
            ],
        )
        body = response.json()
        assert body.get("status") == "APPROVED", body
        assert "Số học" not in (body.get("reason") or "")
        assert "báo cáo thu nhập" not in (body.get("reason") or "")
    finally:
        FORM_REGISTRY.pop(form_id, None)


def test_runtime_accepts_object_type_form(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    form_id = "SIM-OBJECT-TYPE-RUNTIME"
    _probe_form(form_id, [
        {"slot": 0, "title": "Ho so", "role": "app_profile", "object_type": "app_profile", "object_config": {}, "required": True},
        {"slot": 1, "title": "Thu nhap", "role": "app_income", "object_type": "weekly_income", "object_config": _weekly_config(), "required": True},
    ])
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/survey/submit-webrtc",
            data={"form_id": form_id, "is_test_mode": "true", "q_cam_0": "uploaded_0", "q_cam_1": "uploaded_1"},
            files=[
                ("survey_photo_0", ("profile.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")),
                ("survey_photo_1", ("income.jpg", io.BytesIO(_jpeg_bytes((220, 40, 40))), "image/jpeg")),
            ],
        )
        body = response.json()
        assert "Thiếu ảnh" not in (body.get("reason") or "")
        assert "Cấu hình kiểm tra ảnh không hợp lệ" not in (body.get("reason") or "")
    finally:
        FORM_REGISTRY.pop(form_id, None)
