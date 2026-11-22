"""Ordered form module schema and execution contracts."""
import pytest
from pydantic import ValidationError

from services.form_engine import (
    FormModule,
    FormModuleInput,
    FormModuleOutput,
    FormQuestion,
    FormSchema,
    FormSettings,
    QuestionType,
)
from services.form_modules import run_form_modules
from services.logic_modules import default_get_percentage_definition


def _schema(modules, questions=None):
    return FormSchema(
        form_id="MODULE_TEST",
        title="Module test",
        settings=FormSettings(modules=modules),
        questions=questions or [
            FormQuestion(id="target", title="Target", type=QuestionType.NUMBER),
            FormQuestion(id="total", title="Total", type=QuestionType.NUMBER),
        ],
    )


def _logic_module():
    return FormModule(
        uid="m1",
        name="Completion",
        kind="logic",
        module_id="get-percentage",
        inputs=[
            FormModuleInput(name="target", source="question", question_id="target"),
            FormModuleInput(name="total", source="question", question_id="total"),
        ],
        outputs=[FormModuleOutput(key="percentage", name="completion")],
    )


@pytest.mark.asyncio
async def test_logic_module_maps_questions_to_named_output():
    schema = _schema([_logic_module()])
    result = await run_form_modules(
        schema, {"target": 7, "total": 10},
        logic_modules={"get-percentage": default_get_percentage_definition()},
    )
    assert result["failure"] is None
    assert result["outputs"] == {"completion": 70.0}
    assert result["modules"]["m1"]["passed"] is True


@pytest.mark.asyncio
async def test_engine_module_supports_boolean_rejection():
    question = FormQuestion(id="name", title="Name", type=QuestionType.TEXT)
    module = FormModule(
        uid="check",
        name="Name required",
        kind="engine",
        module_id="required_fields",
        inputs=[FormModuleInput(name="fields", source="literal", literal=["name"])],
        outputs=[
            FormModuleOutput(key="passed", name="name_ok"),
            FormModuleOutput(key="reason", name="name_reason"),
        ],
        reject_on_false=True,
        reject_message="Name is required",
    )
    schema = _schema([module], [question])
    ok = await run_form_modules(schema, {"name": "Alice"})
    assert ok["outputs"]["name_ok"] is True
    assert ok["failure"] is None
    failed = await run_form_modules(schema, {})
    assert failed["outputs"]["name_ok"] is False
    assert failed["failure"]["message"] == "Name is required"


@pytest.mark.asyncio
async def test_composite_module_references_earlier_module():
    question = FormQuestion(id="name", title="Name", type=QuestionType.TEXT)
    first = FormModule(
        uid="check1",
        name="Name check",
        kind="engine",
        module_id="required_fields",
        inputs=[FormModuleInput(name="fields", source="literal", literal=["name"])],
        outputs=[FormModuleOutput(key="passed", name="name_ok")],
    )
    composite = FormModule(
        uid="all1",
        name="All checks",
        kind="all",
        inputs=[FormModuleInput(name="check1", source="module", module_uid="check1")],
        outputs=[FormModuleOutput(key="passed", name="all_ok")],
        reject_on_false=True,
    )
    schema = _schema([first, composite], [question])
    ok = await run_form_modules(schema, {"name": "Alice"})
    assert ok["outputs"]["all_ok"] is True
    failed = await run_form_modules(schema, {})
    assert failed["outputs"]["all_ok"] is False
    assert failed["failure"]["module_uid"] == "all1"


def test_module_references_must_point_backwards():
    module = FormModule(
        uid="later",
        name="Later",
        kind="logic",
        module_id="get-percentage",
        inputs=[FormModuleInput(name="target", source="output", module_uid="missing", output_key="x")],
        outputs=[FormModuleOutput(key="x", name="x")],
    )
    with pytest.raises(ValidationError):
        _schema([module])


def test_output_names_must_be_unique_across_modules():
    first = _logic_module()
    second = _logic_module().model_copy(update={"uid": "m2", "outputs": [FormModuleOutput(key="percentage", name="completion")]})
    with pytest.raises(ValidationError):
        _schema([first, second])


def test_output_source_requires_output_key():
    with pytest.raises(ValidationError):
        FormModuleInput(name="x", source="output", module_uid="m1")
