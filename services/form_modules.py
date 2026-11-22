"""Execute ordered form modules over submitted answers.

Modules run after question validation and before the final submission verdict.
Each module reads questions or earlier module outputs, then publishes named
outputs. A false module can reject the submission when configured.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

from core.admin_modules import LogicModule, list_modules
from services.form_engine import FormSchema
from services.image_check_engines import run_engine
from services.image_objects import get_engine
from services.logic_modules import IMPLEMENTATIONS, OUTPUTS

logger = logging.getLogger(__name__)


def _coerce(value: Any, input_type: str) -> Any:
    if value is None:
        return None
    if input_type == "integer":
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return value
    if input_type == "number":
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if input_type == "boolean":
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "on", "có", "co"}
        return bool(value)
    return value


def _resolve(inp, answers: dict[str, Any], outputs: dict[str, dict[str, Any]], modules: dict[str, dict[str, Any]]):
    if inp.source == "question":
        return answers.get(inp.question_id)
    if inp.source == "output":
        return (outputs.get(inp.module_uid) or {}).get(inp.output_key)
    if inp.source == "module":
        return modules.get(inp.module_uid) or {}
    return inp.literal


async def _load_logic_modules(pool) -> dict[str, dict[str, Any]]:
    if not pool:
        return {}
    try:
        rows = await list_modules(pool)
    except Exception as exc:
        logger.warning("form module catalog unavailable: %s", type(exc).__name__)
        return {}
    return {
        row["module_id"]: row["definition"]
        for row in rows
        if row["module_type"] == "logic" and row["enabled"]
    }


async def run_form_modules(
    schema: FormSchema,
    answers: dict[str, Any],
    pool=None,
    *,
    logic_modules: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    configured = [
        module for module in (schema.settings.modules or []) if module.enabled
    ]
    if not configured:
        return {"outputs": {}, "modules": {}, "failure": None}

    logic = logic_modules if logic_modules is not None else await _load_logic_modules(pool)
    named_outputs: dict[str, Any] = {}
    module_outputs: dict[str, dict[str, Any]] = {}
    module_results: dict[str, dict[str, Any]] = {}
    failure = None

    for module in configured:
        resolved: dict[str, Any] = {}
        for inp in module.inputs:
            resolved[inp.name] = _resolve(inp, answers, module_outputs, module_results)

        passed = True
        reason = ""
        result: dict[str, Any] = {}
        try:
            if module.kind == "logic":
                definition = logic.get(module.module_id or "")
                if not definition:
                    raise ValueError(f"logic module '{module.module_id}' is unavailable")
                model = LogicModule.model_validate(definition)
                values = {}
                for field in model.inputs:
                    if field.name not in resolved:
                        raise ValueError(f"missing input '{field.name}'")
                    values[field.name] = _coerce(resolved[field.name], field.type)
                result = dict(IMPLEMENTATIONS[model.implementation](values) or {})
                passed = bool(result.get("passed", True))
                reason = str(result.get("reason") or "")
                keys = model.outputs or list(OUTPUTS.get(model.implementation, ["result"]))
            elif module.kind == "engine":
                engine = get_engine(module.module_id or "")
                binding = {"id": engine.id, "params": resolved, "conditions": []}
                extraction = {**answers, **resolved}
                result = dict(
                    run_engine(
                        engine.model_dump(), binding, extraction, extraction,
                        context={"form_id": schema.form_id, "module": module.uid},
                    )
                    or {}
                )
                passed = bool(result.get("passed"))
                reason = str(result.get("reason") or "")
                keys = ["passed", "reason", "details"]
            else:
                refs = []
                for inp in module.inputs:
                    if inp.source == "module":
                        refs.append(module_results.get(inp.module_uid or "", {}).get("passed") is True)
                    else:
                        refs.append(bool(resolved[inp.name]))
                passed = bool(refs) and all(refs)
                reason = "" if passed else "Một kiểm tra thành phần không đạt."
                keys = ["passed"]
        except Exception as exc:
            passed = False
            reason = f"Không thể chạy module '{module.name}': {exc}"
            failure = failure or {
                "module_uid": module.uid,
                "module_name": module.name,
                "scope": module.scope,
                "question_id": module.question_id,
                "slot_index": module.slot_index,
                "message": reason,
            }

        named: dict[str, Any] = {}
        output_defs = module.outputs or []
        if not output_defs:
            output_defs = [SimpleNamespace(key=key, name=key) for key in keys]
        for output in output_defs:
            if output.key == "passed":
                value = passed
            elif output.key == "reason":
                value = reason
            else:
                value = result.get(output.key)
            named[output.name] = value
            named_outputs[output.name] = value
        module_outputs[module.uid] = named
        module_results[module.uid] = {
            "passed": passed,
            "reason": reason,
            "scope": module.scope,
            "question_id": module.question_id,
            "slot_index": module.slot_index,
            "outputs": named,
        }

        if module.reject_on_false and not passed and failure is None:
            failure = {
                "module_uid": module.uid,
                "module_name": module.name,
                "scope": module.scope,
                "question_id": module.question_id,
                "slot_index": module.slot_index,
                "message": module.reject_message or f"Kiểm tra '{module.name}' không đạt.",
            }

    return {
        "outputs": named_outputs,
        "modules": module_results,
        "failure": failure,
    }
