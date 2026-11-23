import re
import pytest
from services.form_engine import (
    FormSchema,
    FormQuestion,
    QuestionType,
    FormSettings,
    DEFAULT_TRUST_BRIDGE_SCHEMA,
    validate_submission_against_schema,
)
from services.dynamic_form_html import render_dynamic_form_html
from services.form_modules import run_form_modules
from services.form_manager_html import render_form_editor_html


def test_input_mode_is_keyboard_only():
    """input_mode changes the keyboard, not validation; checks live in modules."""
    schema = FormSchema(
        form_id="test_numbers_form",
        title="Test Numbers Form",
        settings=FormSettings(decree13_required=False),
        questions=[FormQuestion(
            id="num_field", title="Số điện thoại / Mã số",
            type=QuestionType.TEXT, required=True, input_mode="numeric",
        )],
    )
    is_valid, err = validate_submission_against_schema(schema, {"num_field": "0988abc123"})
    assert is_valid is True
    assert err is None


@pytest.mark.asyncio
async def test_default_schema_driver_phone_uses_regex_module():
    """The default phone check is an engine module, not a question ValidationRule."""
    phone_q = next((q for q in DEFAULT_TRUST_BRIDGE_SCHEMA.questions if q.id == "driver_phone"), None)
    assert phone_q is not None
    assert not hasattr(phone_q, "validation")
    assert phone_q.input_mode == "tel"
    assert all(not hasattr(q, "validation") for q in DEFAULT_TRUST_BRIDGE_SCHEMA.questions)
    module = next(m for m in DEFAULT_TRUST_BRIDGE_SCHEMA.settings.modules if m.uid == "phone_format")
    assert module.kind == "engine"
    assert module.module_id == "regex"

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
    failed = await run_form_modules(DEFAULT_TRUST_BRIDGE_SCHEMA, sub)
    assert failed["failure"]["module_uid"] == "phone_format"
    assert "0 + 9 chữ số" in failed["failure"]["message"]
    valid = await run_form_modules(DEFAULT_TRUST_BRIDGE_SCHEMA, {**sub, "driver_phone": "0988123456"})
    assert valid["failure"] is None
    plus84 = await run_form_modules(DEFAULT_TRUST_BRIDGE_SCHEMA, {**sub, "driver_phone": "+84988123456"})
    assert plus84["failure"] is None


def test_dynamic_form_html_renders_numeric_keypad_attributes():
    """Test dynamic form HTML rendering: verify inputmode='numeric', type='tel', and pattern='[0-9]*' logic."""
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)

    assert "isNumbersOnly" in html
    assert "inp.type = 'tel';" in html
    assert "inp.inputMode = 'numeric';" in html
    assert "inp.pattern = '[0-9]*';" in html
    assert "inp.autocomplete = (inputMode === 'tel' ? 'tel' : 'off');" in html
    assert "e.target.value = e.target.value.replace(/[^0-9]/g, '');" in html


def test_form_manager_html_includes_numbers_only_toggle():
    """Test form manager HTML includes numbers-only toggle checkbox and helper functions."""
    html = render_form_editor_html(DEFAULT_TRUST_BRIDGE_SCHEMA)

    assert 'class="chk-numbers-only"' in html
    assert "toggleQuestionNumbersOnly" in html
    assert "Bàn phím số (numeric keypad, không phải kiểm tra)" in html
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


@pytest.mark.asyncio
async def test_default_schema_daily_trips_uses_range_module():
    result = await run_form_modules(DEFAULT_TRUST_BRIDGE_SCHEMA, {"daily_trips": "0"})
    assert result["failure"]["module_uid"] == "daily_trips_range"
    assert "1 đến 200" in result["failure"]["message"]
