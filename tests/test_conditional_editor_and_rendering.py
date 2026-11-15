# -*- coding: utf-8 -*-
"""
Tests for Custom Conditional Logic, Form Editor UI Hydration, and Dynamic Unlock.
"""

import json
import re

import pytest
from services.form_engine import (
    FormSchema,
    FormSettings,
    FormQuestion,
    QuestionType,
    ConditionalRule,
    validate_submission_against_schema,
)
from services.dynamic_form_html import render_dynamic_form_html
from services.form_manager_html import render_form_editor_html


def test_custom_question_ids_unlock_conditionals():
    """Verifies that non-default question IDs for signature & consent properly unlock and validate."""
    schema = FormSchema(
        form_id="TB_GSM_2026_Nov_Test",
        title="Khảo Sát Thử Nghiệm Tháng 11",
        settings=FormSettings(decree13_required=True),
        questions=[
            FormQuestion(
                id="custom_sig_id",
                title="1. Chữ ký số tài xế",
                type=QuestionType.SIGNATURE,
                required=True,
            ),
            FormQuestion(
                id="custom_consent_id",
                title="2. Cam kết & Đồng ý",
                type=QuestionType.SINGLE_CHOICE,
                options=["Tôi đồng ý", "Tôi không đồng ý"],
                required=True,
            ),
            FormQuestion(
                id="q_vehicle",
                title="3. Dòng xe",
                type=QuestionType.SINGLE_CHOICE,
                options=["Xe máy", "Ô tô"],
                required=True,
                conditional=ConditionalRule(
                    depends_on_question_id="custom_consent_id",
                    operator="equals",
                    expected_value="Tôi đồng ý",
                ),
            ),
            FormQuestion(
                id="q_car_model",
                title="4. Mẫu xe ô tô",
                type=QuestionType.SINGLE_CHOICE,
                options=["VF5", "VF8"],
                required=True,
                conditional=ConditionalRule(
                    depends_on_question_id="q_vehicle",
                    operator="equals",
                    expected_value="Ô tô",
                ),
            ),
        ],
    )

    # 1. When consented as "Tôi đồng ý" and signed, q_vehicle becomes visible and mandatory
    answers_incomplete = {
        "custom_sig_id": "data:image/png;base64,mock",
        "custom_consent_id": "Tôi đồng ý",
    }
    is_valid, err = validate_submission_against_schema(schema, answers_incomplete)
    assert is_valid is False
    assert "3. Dòng xe" in err

    # 2. When vehicle is "Xe máy", q_car_model is conditionally hidden (not required)
    answers_bike = {
        "custom_sig_id": "data:image/png;base64,mock",
        "custom_consent_id": "Tôi đồng ý",
        "q_vehicle": "Xe máy",
    }
    is_valid, err = validate_submission_against_schema(schema, answers_bike)
    assert is_valid is True
    assert err is None

    # 3. When vehicle is "Ô tô", q_car_model is required
    answers_car_empty = {
        "custom_sig_id": "data:image/png;base64,mock",
        "custom_consent_id": "Tôi đồng ý",
        "q_vehicle": "Ô tô",
    }
    is_valid, err = validate_submission_against_schema(schema, answers_car_empty)
    assert is_valid is False
    assert "4. Mẫu xe ô tô" in err

    # 4. When full valid answers provided
    answers_car_full = {
        "custom_sig_id": "data:image/png;base64,mock",
        "custom_consent_id": "Tôi đồng ý",
        "q_vehicle": "Ô tô",
        "q_car_model": "VF5",
    }
    is_valid, err = validate_submission_against_schema(schema, answers_car_full)
    assert is_valid is True


def test_conditional_operators_in_engine():
    """Tests 'not_equals', 'contains', and 'in' operators in validate_submission_against_schema."""
    schema = FormSchema(
        form_id="test_operators",
        title="Test Operators",
        settings=FormSettings(decree13_required=False),
        questions=[
            FormQuestion(
                id="q_fruit",
                title="Chọn hoa quả",
                type=QuestionType.SINGLE_CHOICE,
                options=["Cam", "Táo", "Chuối"],
                required=True,
            ),
            FormQuestion(
                id="q_citrus_detail",
                title="Chi tiết trái cây có múi",
                type=QuestionType.TEXT,
                required=True,
                conditional=ConditionalRule(
                    depends_on_question_id="q_fruit",
                    operator="not_equals",
                    expected_value="Chuối",
                ),
            ),
        ],
    )

    # If fruit is Chuối -> condition not met, child hidden -> passes without detail
    res_chuoi, _ = validate_submission_against_schema(schema, {"q_fruit": "Chuối"})
    assert res_chuoi is True

    # If fruit is Cam -> condition met (!= Chuối) -> child required -> fails if empty
    res_cam, err_cam = validate_submission_against_schema(schema, {"q_fruit": "Cam"})
    assert res_cam is False
    assert "Chi tiết trái cây có múi" in err_cam


def _embedded_schema_json(html_text: str, marker: str) -> dict:
    match = re.search(re.escape(marker), html_text)
    assert match, f"marker not found: {marker}"
    data, _ = json.JSONDecoder().raw_decode(html_text[match.end():])
    return data


def _schema_with_multi_conditions() -> FormSchema:
    return FormSchema(
        form_id="test_editor_render",
        title="Test Editor Form",
        questions=[
            FormQuestion(
                id="q1",
                title="Câu 1",
                type=QuestionType.SINGLE_CHOICE,
                options=["A", "B"],
            ),
            FormQuestion(
                id="q2",
                title="Câu 2",
                type=QuestionType.TEXT,
                conditionals=[
                    ConditionalRule(depends_on_question_id="q1", operator="equals", expected_value=["A", "B"]),
                    ConditionalRule(depends_on_question_id="q1", operator="not_empty", expected_value=None),
                ],
            ),
            FormQuestion(
                id="gate",
                title="Đồng ý xử lý dữ liệu",
                type=QuestionType.SINGLE_CHOICE,
                options=["Đồng ý", "Không đồng ý"],
                is_consent_gate=True,
                consent_accept_values=["Đồng ý"],
            ),
        ],
    )


def test_form_editor_embeds_conditionals_and_consent_gate():
    schema = _schema_with_multi_conditions()
    html_content = render_form_editor_html(schema, is_new=False)
    assert "{schema_json_str}" not in html_content

    data = _embedded_schema_json(html_content, "const INITIAL_SCHEMA = ")
    q2 = data["questions"][1]
    assert q2["conditionals"][0]["expected_value"] == ["A", "B"]
    assert q2["conditionals"][1]["operator"] == "not_empty"
    assert "conditional" not in q2
    gate = data["questions"][2]
    assert gate["is_consent_gate"] is True
    assert gate["consent_accept_values"] == ["Đồng ý"]


def test_dynamic_form_embeds_conditionals_and_consent_gate():
    schema = _schema_with_multi_conditions()
    data = _embedded_schema_json(render_dynamic_form_html(schema), "const SCHEMA = ")
    q2 = data["questions"][1]
    assert [rule["operator"] for rule in q2["conditionals"]] == ["equals", "not_empty"]
    assert data["questions"][2]["is_consent_gate"] is True


def test_empty_and_not_empty_operators():
    schema = FormSchema(
        form_id="test_blank_operators",
        title="Blank operators",
        settings=FormSettings(decree13_required=False),
        questions=[
            FormQuestion(id="q_flag", title="Có ghi chú", type=QuestionType.TEXT, required=False),
            FormQuestion(
                id="q_note",
                title="Nội dung ghi chú",
                type=QuestionType.TEXT,
                required=True,
                conditionals=[ConditionalRule(depends_on_question_id="q_flag", operator="not_empty")],
            ),
            FormQuestion(
                id="q_fallback",
                title="Không có ghi chú",
                type=QuestionType.TEXT,
                required=True,
                conditionals=[ConditionalRule(depends_on_question_id="q_flag", operator="empty")],
            ),
        ],
    )

    ok, err = validate_submission_against_schema(schema, {"q_flag": "", "q_fallback": "x"})
    assert ok is True, err

    ok, err = validate_submission_against_schema(schema, {"q_flag": "có", "q_fallback": "x"})
    assert ok is False
    assert "Nội dung ghi chú" in err

    ok, err = validate_submission_against_schema(schema, {"q_flag": "có", "q_note": "abc"})
    assert ok is True, err


def test_multiple_conditions_all_must_match():
    schema = FormSchema(
        form_id="test_multi_conditions",
        title="Multi conditions",
        settings=FormSettings(decree13_required=False),
        questions=[
            FormQuestion(id="q_a", title="A", type=QuestionType.SINGLE_CHOICE, options=["x", "y"]),
            FormQuestion(id="q_b", title="B", type=QuestionType.SINGLE_CHOICE, options=["x", "y"]),
            FormQuestion(
                id="q_target",
                title="Bắt buộc khi cả hai khớp",
                type=QuestionType.TEXT,
                required=True,
                conditionals=[
                    ConditionalRule(depends_on_question_id="q_a", operator="equals", expected_value=["x"]),
                    ConditionalRule(depends_on_question_id="q_b", operator="equals", expected_value=["x"]),
                ],
            ),
        ],
    )

    assert validate_submission_against_schema(schema, {"q_a": "x", "q_b": "y"})[0] is True
    ok, err = validate_submission_against_schema(schema, {"q_a": "x", "q_b": "x"})
    assert ok is False
    assert "Bắt buộc" in err


def test_multiple_expected_values_match_any_selection():
    schema = FormSchema(
        form_id="test_multi_values",
        title="Multi values",
        settings=FormSettings(decree13_required=False),
        questions=[
            FormQuestion(id="q_vehicle", title="Xe", type=QuestionType.SINGLE_CHOICE, options=["Xe máy", "Xe đạp", "Ô tô"]),
            FormQuestion(
                id="q_two_wheel_doc",
                title="Giấy tờ xe hai bánh",
                type=QuestionType.TEXT,
                required=True,
                conditionals=[
                    ConditionalRule(
                        depends_on_question_id="q_vehicle",
                        operator="equals",
                        expected_value=["Xe máy", "Xe đạp"],
                    )
                ],
            ),
        ],
    )

    ok, err = validate_submission_against_schema(schema, {"q_vehicle": "Xe đạp"})
    assert ok is False and "Giấy tờ" in err
    assert validate_submission_against_schema(schema, {"q_vehicle": "Ô tô"})[0] is True


def test_consent_gate_gates_following_questions_and_rejects_refusal():
    schema = FormSchema(
        form_id="test_gate",
        title="Gate",
        settings=FormSettings(decree13_required=True),
        questions=[
            FormQuestion(id="q_before", title="Trước cổng", type=QuestionType.TEXT, required=False),
            FormQuestion(
                id="q_consent",
                title="Đồng ý xử lý dữ liệu",
                type=QuestionType.SINGLE_CHOICE,
                options=["Đồng ý", "Không đồng ý"],
                is_consent_gate=True,
                consent_accept_values=["Đồng ý"],
            ),
            FormQuestion(id="q_after", title="Sau cổng", type=QuestionType.TEXT, required=True),
            FormQuestion(
                id="q_second_gate",
                title="Xác nhận lần hai",
                type=QuestionType.SIGNATURE,
                required=True,
                is_consent_gate=True,
            ),
            FormQuestion(id="q_last", title="Cuối cùng", type=QuestionType.TEXT, required=True),
        ],
    )

    # Dissent is rejected outright
    ok, err = validate_submission_against_schema(schema, {"q_consent": "Không đồng ý"})
    assert ok is False and "Nghị định 13" in err

    # Consent without the rest: gate passes validation visibility, q_after is now required
    ok, err = validate_submission_against_schema(schema, {"q_consent": "Đồng ý"})
    assert ok is False and "Sau cổng" in err

    # First gate satisfied, second gate blocks q_last
    ok, err = validate_submission_against_schema(
        schema, {"q_consent": "Đồng ý", "q_after": "a", "q_second_gate": "sig"}
    )
    assert ok is False and "Cuối cùng" in err

    ok, err = validate_submission_against_schema(
        schema,
        {"q_consent": "Đồng ý", "q_after": "a", "q_second_gate": "sig", "q_last": "z"},
    )
    assert ok is True, err

    # Legacy schemas without the flag still gate on consent_choice == "Tôi đồng ý"
    legacy = FormSchema(
        form_id="test_legacy_gate",
        title="Legacy",
        questions=[
            FormQuestion(
                id="consent_choice",
                title="Đồng ý",
                type=QuestionType.SINGLE_CHOICE,
                options=["Tôi đồng ý", "Tôi không đồng ý"],
            ),
            FormQuestion(id="q_child", title="Con", type=QuestionType.TEXT, required=True),
        ],
    )
    ok, err = validate_submission_against_schema(legacy, {"consent_choice": "Không đồng ý"})
    assert ok is False and "Nghị định 13" in err
    ok, err = validate_submission_against_schema(legacy, {"consent_choice": "Tôi đồng ý"})
    assert ok is False and "Con" in err


def test_signature_questions_are_always_gates_without_values():
    schema = FormSchema(
        form_id="test_signature_gate",
        title="Signature gate",
        settings=FormSettings(decree13_required=False),
        questions=[
            FormQuestion(
                id="sig",
                title="Ký xác nhận",
                type=QuestionType.SIGNATURE,
                is_consent_gate=False,
                consent_accept_values=["anything"],
            ),
            FormQuestion(id="q_after", title="Sau chữ ký", type=QuestionType.TEXT, required=True),
        ],
    )
    sig = schema.questions[0]
    assert sig.is_consent_gate is True
    assert sig.consent_accept_values == []

    ok, err = validate_submission_against_schema(schema, {})
    assert ok is False and "Ký xác nhận" in err
    ok, err = validate_submission_against_schema(schema, {"sig": "data:image/png;base64,x"})
    assert ok is False and "Sau chữ ký" in err
    ok, err = validate_submission_against_schema(schema, {"sig": "data:image/png;base64,x", "q_after": "x"})
    assert ok is True, err


def test_signature_parent_condition_is_forced_to_not_empty():
    schema = FormSchema(
        form_id="test_signature_condition",
        title="Signature condition",
        settings=FormSettings(decree13_required=False),
        questions=[
            FormQuestion(id="sig", title="Ký", type=QuestionType.SIGNATURE),
            FormQuestion(
                id="q_child",
                title="Con",
                type=QuestionType.TEXT,
                required=True,
                conditionals=[
                    ConditionalRule(depends_on_question_id="sig", operator="equals", expected_value="signed")
                ],
            ),
        ],
    )
    rule = schema.questions[1].conditionals[0]
    assert rule.operator == "not_empty"
    assert rule.expected_value is None
    ok, err = validate_submission_against_schema(schema, {"sig": "data:x"})
    assert ok is False and "Con" in err
