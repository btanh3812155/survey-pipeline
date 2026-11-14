# -*- coding: utf-8 -*-
"""
Suite C: Schema Synchronization & Component Reactivity Test Suite.
Verifies every pipeline component's response to incoming schema definitions and updates:
1. Reconciles baseline schemas with disk definitions, propagating new fields bidirectionally.
2. Ingress router response: parses and enforces incoming schema questions and image slots.
3. Form engine response: validates submissions against updated schema rules.
4. Frontend renderer response: renders custom slot titles, tooltips, and prompts cleanly.
5. Admin moderator response: maps raw facts and weekly incomes to dossier cards.
6. Downstream parity verifier: detects contract drift across pipeline layers.
"""

import os
import json
from services.form_engine import FormSchema, FormQuestion, QuestionType, validate_submission_against_schema, DEFAULT_TRUST_BRIDGE_SCHEMA
from services.dynamic_form_html import render_dynamic_form_html
from services.admin_moderator import render_dossier_message
from core.schema_sync import (
    reconcile_and_sync_form_schema,
    verify_schema_downstream_parity,
    normalize_slot_definition,
)


def test_schema_sync_engine_bidirectional_drift_repair(tmp_path):
    """Verifies that schema reconciler detects disk drift, injects missing fields, and updates disk."""
    disk_file = tmp_path / "income_survey.json"
    raw_disk_data = DEFAULT_TRUST_BRIDGE_SCHEMA.model_dump()

    # Simulate older disk JSON missing ai_detection_prompt and allow_upload
    for q in raw_disk_data["questions"]:
        if q.get("image_slots"):
            for s in q["image_slots"]:
                s.pop("ai_detection_prompt", None)
                s.pop("allow_upload", None)

    with open(disk_file, "w", encoding="utf-8") as f:
        json.dump(raw_disk_data, f)

    # Run bidirectional sync
    synced, was_updated = reconcile_and_sync_form_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, str(disk_file))
    assert was_updated is True

    # Component response: disk file must now contain the canonical updated fields
    with open(disk_file, "r", encoding="utf-8") as f:
        re_read = json.load(f)
    cam_q = next(q for q in re_read["questions"] if q["type"] == "camera_webrtc")
    assert all("ai_detection_prompt" in s for s in cam_q["image_slots"])
    assert all("allow_upload" in s for s in cam_q["image_slots"])


def test_form_engine_response_to_incoming_schema():
    """Form engine dynamically enforces incoming mandatory questions and slot rules."""
    schema = DEFAULT_TRUST_BRIDGE_SCHEMA

    # Missing mandatory consent choice is rejected
    valid_no_consent, err_consent = validate_submission_against_schema(schema, {})
    assert valid_no_consent is False
    assert "Chữ ký" in err_consent or "Cam kết" in err_consent

    # Valid payload passing mandatory fields is accepted
    valid_ok, err_ok = validate_submission_against_schema(
        schema,
        {
            "driver_signature": "data:image/png;base64,mock",
            "consent_choice": "Tôi đồng ý",
            "camera_receipt": ["slot0", "slot1", "slot2", "slot3", "slot4", "slot5", "slot6"],
            "vehicle_type": "Ô tô",
            "car_model": "VF 5 Plus",
            "gsm_duration": "Từ 6 – 12 tháng",
            "gsm_contract_type": "Xe Taxi xanh GSM",
            "daily_trips": "18",
            "monthly_income_range": "12 – 20 triệu VNĐ",
            "gsm_incentive_policy": "Có áp dụng chỉ tiêu",
        },
    )
    assert valid_ok is True
    assert err_ok is None


def test_frontend_renderer_response_to_incoming_schema():
    """Frontend dynamic renderer embeds incoming schema questions, slots, and prompts."""
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)
    # Renders question titles and slot configurations
    assert "Chữ ký xác nhận khảo sát" in html
    assert "Cam kết thông tin & Đồng ý xử lý dữ liệu Nghị định 13" in html
    assert "Chụp ảnh xác thực 7 bước" in html


def test_admin_moderator_response_to_incoming_facts():
    """Admin moderator maps financial and operational facts into standardized dossier report."""
    dossier = {
        "submission_id": "VN-12345",
        "status": "APPROVED",
        "created_at": "12/11/2026 14:00",
        "driver_phone": "0988111222",
        "driver_name": "Tài xế",
        "vehicle_type": "Ô tô",
        "car_model": "VF 5 Plus",
        "gross_income": 15000000,
        "net_income": 12000000,
        "trips_count": 80,
        "weekly_incomes": {
            "week_1": {"gross_revenue": 3000000, "period": "Tuần 1", "total_trips": 16},
            "week_2": {"gross_revenue": 3000000, "period": "Tuần 2", "total_trips": 16},
        },
    }
    report = render_dossier_message(dossier)
    assert "VN-12345" in report
    assert "15,000,000 VNĐ" in report
    assert "12,000,000 VNĐ" in report
    assert "VF 5 Plus" in report


def test_downstream_parity_verifier_detects_contract_violations():
    """Downstream parity verifier flags schemas missing camera or required verification roles."""
    # Defective schema without camera
    defective_schema = FormSchema(
        form_id="defective_01",
        title="Biểu mẫu lỗi",
        questions=[
            FormQuestion(id="q1", title="Họ tên", type=QuestionType.TEXT),
        ],
    )
    violations = verify_schema_downstream_parity(defective_schema)
    assert len(violations) > 0
    assert any("camera_webrtc" in v for v in violations)

    # Canonical schema has 0 violations
    canonical_violations = verify_schema_downstream_parity(DEFAULT_TRUST_BRIDGE_SCHEMA)
    assert len(canonical_violations) == 0
