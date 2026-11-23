"""Helpers for tests that exercise checks as form engine modules."""
import asyncio
from typing import Any

from services.form_engine import FormModule, FormModuleInput, FormSchema
from services.form_modules import run_form_modules


def check_module(uid: str, module_id: str, params: dict[str, Any], message: str) -> FormModule:
    return FormModule(
        uid=uid,
        name=uid,
        kind="engine",
        module_id=module_id,
        inputs=[
            FormModuleInput(name=name, source="literal", literal=value)
            for name, value in params.items()
        ],
        reject_on_false=True,
        reject_message=message,
    )


def with_checks(schema: FormSchema, *modules: FormModule) -> FormSchema:
    schema.settings.modules = list(modules)
    return schema


async def run_checks(schema: FormSchema, answers: dict[str, Any]) -> tuple[bool, str | None]:
    result = await run_form_modules(schema, answers)
    failure = result.get("failure")
    return (failure is None, None if failure is None else failure["message"])


def run_checks_sync(schema: FormSchema, answers: dict[str, Any]) -> tuple[bool, str | None]:
    return asyncio.run(run_checks(schema, answers))
