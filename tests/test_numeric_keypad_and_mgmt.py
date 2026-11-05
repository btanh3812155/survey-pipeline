import re
import pytest
from services.form_engine import (
    FormSchema,
    FormQuestion,
    QuestionType,
    ValidationRule,
    FormSettings,
    DEFAULT_TRUST_BRIDGE_SCHEMA,
    validate_submission_against_schema,
)
from services.dynamic_form_html import render_dynamic_form_html
from services.form_manager_html import render_form_editor_html


def test_validation_numbers_only_rejects_letters():
    """Test server-side validation: numbers_only=True rejects letters with localized error."""
    schema = FormSchema(
        form_id="test_numbers_form",
        title="Test Numbers Form",
        settings=FormSettings(decree13_required=False),
        questions=[
            FormQuestion(
                id="num_field",
                title="Số điện thoại / Mã số",
                type=QuestionType.TEXT,
                required=True,
                validation=ValidationRule(numbers_only=True),
            )
        ],
    )

    invalid_submission = {"num_field": "0988abc123"}
    is_valid, err = validate_submission_against_schema(schema, invalid_submission)
    assert is_valid is False
    assert err == "Câu hỏi 'Số điện thoại / Mã số' chỉ được chứa các chữ số!"


def test_validation_numbers_only_accepts_pure_numeric_string():
    """Test server-side validation: numbers_only=True accepts pure numeric string."""
    schema = FormSchema(
        form_id="test_numbers_form",
        title="Test Numbers Form",
        settings=FormSettings(decree13_required=False),
        questions=[
            FormQuestion(
                id="num_field",
                title="Số điện thoại / Mã số",
                type=QuestionType.TEXT,
                required=True,
                validation=ValidationRule(numbers_only=True),
            )
        ],
    )

    valid_submission = {"num_field": "0988123456"}
    is_valid, err = validate_submission_against_schema(schema, valid_submission)
    assert is_valid is True
    assert err is None


def test_default_schema_driver_phone_has_numbers_only():
    """Verify DEFAULT_TRUST_BRIDGE_SCHEMA driver_phone has numbers_only=True."""
    phone_q = next((q for q in DEFAULT_TRUST_BRIDGE_SCHEMA.questions if q.id == "driver_phone"), None)
    assert phone_q is not None
    assert phone_q.validation is not None
    assert phone_q.validation.numbers_only is True

    # Submission with non-numeric phone should be rejected by numbers_only
    sub = {
        "consent_choice": "Tôi đồng ý",
        "driver_signature": "mock_sig",
        "driver_phone": "0988A23456",
        "driver_name": "Nguyen Van A",
        "license_plate": "29E-12345",
        "vehicle_type": "Ô tô điện VinFast (GSM)",
        "car_model": "VF 5 Plus",
        "gross_revenue": "1000000",
        "odometer_km": "5000",
        "camera_receipt": ["mock.jpg", "mock2.jpg"],
    }
    is_valid, err = validate_submission_against_schema(DEFAULT_TRUST_BRIDGE_SCHEMA, sub)
    assert is_valid is False
    assert "chỉ được chứa các chữ số" in err


def test_dynamic_form_html_renders_numeric_keypad_attributes():
    """Test dynamic form HTML rendering: verify inputmode='numeric', type='tel', and pattern='[0-9]*' logic."""
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)

    assert "isNumbersOnly" in html
    assert "inp.type = 'tel';" in html
    assert "inp.inputMode = 'numeric';" in html
    assert "inp.pattern = '[0-9]*';" in html
    assert "inp.autocomplete = (q.id === 'driver_phone' ? 'tel' : 'off');" in html
    assert "e.target.value = e.target.value.replace(/[^0-9]/g, '');" in html


def test_form_manager_html_includes_numbers_only_toggle():
    """Test form manager HTML includes numbers-only toggle checkbox and helper functions."""
    html = render_form_editor_html(DEFAULT_TRUST_BRIDGE_SCHEMA)

    assert 'class="chk-numbers-only"' in html
    assert "toggleQuestionNumbersOnly" in html
    assert "Chỉ cho phép chữ số (Bàn phím số numeric keypad)" in html
    # Verify DOM integrity: tabQuestions is not nested inside tabOptions
    options_idx = html.find('id="tabOptions"')
    questions_idx = html.find('id="tabQuestions"')
    assert options_idx != -1 and questions_idx != -1
    # Make sure tabOptions is closed before tabQuestions starts
    options_block = html[options_idx:questions_idx]
    # Check that closing div for tabOptions precedes tabQuestions
    assert "</div>" in options_block
    # Verify open vs close divs balance in options_block
    open_divs = options_block.count("<div")
    close_divs = options_block.count("</div>")
    assert open_divs == close_divs
