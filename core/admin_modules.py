"""Database-backed catalog for declarative object, logic and check-engine modules."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from services.image_objects import (
    EngineDefinition,
    ImageObjectDefinition,
    load_registry,
    set_database_engine_registry,
    set_database_object_registry,
)
from services.logic_modules import IMPLEMENTATIONS, OUTPUTS, default_get_percentage_definition

logger = logging.getLogger(__name__)
MODULE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class LogicInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    type: str = "integer"
    required: bool = True

    @field_validator("type")
    @classmethod
    def _integer_only(cls, value: str) -> str:
        if value != "integer":
            raise ValueError("only integer inputs are supported by the current safe implementations")
        return value


class LogicModule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(..., min_length=1, max_length=100)
    version: str = Field(..., min_length=1, max_length=32)
    description: str = Field(default="", max_length=2000)
    implementation: str
    inputs: list[LogicInput]
    outputs: list[str] = Field(default_factory=list)

    @field_validator("implementation")
    @classmethod
    def _allowlisted_implementation(cls, value: str) -> str:
        if value not in IMPLEMENTATIONS:
            raise ValueError("implementation is not registered in the application")
        return value

    @field_validator("inputs")
    @classmethod
    def _known_inputs(cls, value: list[LogicInput], info):
        implementation = info.data.get("implementation")
        if implementation == "get_percentage" and [item.name for item in value] != ["target", "total"]:
            raise ValueError("get_percentage requires target and total integer inputs in that order")
        return value

def default_gsm_profile_definition() -> dict[str, Any]:
    return {
        "version": 1,
        "id": "gsm-profile-screen",
        "label": "GSM profile screen",
        "description": "GSM driver profile and membership view.",
        "prompt": "Inspect the GSM driver profile image and extract visible membership, achievements, history, and name.",
        "objects": ["GSM driver profile screen"],
        "expected_fields": [
            {"key": "membership", "label": "Membership", "type": "string", "required": False},
            {"key": "achievements", "label": "Achievements", "type": "array", "required": True},
            {"key": "history", "label": "History", "type": "array", "required": True},
            {"key": "name", "label": "Name", "type": "string", "required": False},
            {"key": "version", "label": "Version", "type": "string", "required": False},
            {"key": "app_name", "label": "App name", "type": "string", "required": False},
        ],
        "customizable": [
            {"key": "version", "label": "App version", "type": "text", "default": "2026.10"},
            {"key": "app_name", "label": "App name", "type": "text", "default": "gsm"},
        ],
        "engines": [{"id": "required_fields", "label": "Required fields", "params": {"fields": ["achievements", "history"]}}],
        "extractor": "generic",
        "classify_role": "gsm_profile_screen",
        "json_output_instruction": True,
    }


class ModuleDefinitionError(ValueError):
    pass


def _normalise_definition(module_id: str, module_type: str, definition: Any) -> dict[str, Any]:
    if not MODULE_ID_RE.fullmatch(module_id):
        raise ModuleDefinitionError("invalid module id")
    if not isinstance(definition, dict):
        raise ModuleDefinitionError("definition must be a JSON object")
    payload = dict(definition)
    payload["id"] = module_id
    try:
        if module_type == "object":
            model = ImageObjectDefinition.model_validate(payload)
            missing = sorted({binding.id for binding in model.engines if binding.id not in load_registry()["engines"]})
            if missing:
                raise ModuleDefinitionError("references missing engine(s): " + ", ".join(missing))
            return model.model_dump(mode="json")
        if module_type == "logic":
            model = LogicModule.model_validate(payload)
            data = model.model_dump(mode="json")
            if not data["outputs"]:
                data["outputs"] = list(OUTPUTS.get(data["implementation"], ["result"]))
            return data
        if module_type == "engine":
            return EngineDefinition.model_validate(payload).model_dump(mode="json")
    except (ValidationError, ValueError) as exc:
        raise ModuleDefinitionError(str(exc)) from exc
    raise ModuleDefinitionError("module type must be object, logic or engine")

_MODULE_REGISTRY_AVAILABLE = False


async def ensure_admin_module_registry(pool) -> bool:
    global _MODULE_REGISTRY_AVAILABLE
    if not pool:
        return False
    try:
        registry = load_registry(force=True)
        async with pool.connection() as conn:
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute("SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_admin_module_seed'))")
                await cur.execute("SELECT 1 FROM admin_module_registry_seed WHERE singleton=TRUE")
                if not await cur.fetchone():
                    for module_id, obj in registry["objects"].items():
                        definition = obj.model_dump(mode="json")
                        await cur.execute(
                            "INSERT INTO admin_module_registry(module_id,module_type,display_name,module_version,definition,enabled) "
                            "VALUES (%s,'object',%s,%s,%s::jsonb,%s) ON CONFLICT(module_id) DO NOTHING",
                            (module_id, obj.label, str(obj.version), json.dumps(definition, ensure_ascii=False), obj.enabled),
                        )
                    for module_id, definition in (
                        ("gsm-profile-screen", default_gsm_profile_definition()),
                        ("get-percentage", default_get_percentage_definition()),
                    ):
                        module_type = "object" if module_id == "gsm-profile-screen" else "logic"
                        module_version = "2026.10" if module_id == "gsm-profile-screen" else str(definition.get("version", 1))
                        await cur.execute(
                            "INSERT INTO admin_module_registry(module_id,module_type,display_name,module_version,definition) "
                            "VALUES (%s,%s,%s,%s,%s::jsonb) ON CONFLICT(module_id) DO NOTHING",
                            (module_id, module_type, str(definition.get("label") or definition["name"]),
                             module_version, json.dumps(definition, ensure_ascii=False)),
                        )
                    await cur.execute("INSERT INTO admin_module_registry_seed(singleton) VALUES (TRUE)")
                await cur.execute("SELECT 1 FROM admin_module_engine_seed WHERE singleton=TRUE")
                if not await cur.fetchone():
                    for module_id, engine in registry["engines"].items():
                        await cur.execute(
                            "INSERT INTO admin_module_registry(module_id,module_type,display_name,module_version,definition,enabled) "
                            "VALUES (%s,'engine',%s,%s,%s::jsonb,TRUE) ON CONFLICT(module_id) DO NOTHING",
                            (module_id, engine.label, str(engine.version),
                             json.dumps(engine.model_dump(mode="json"), ensure_ascii=False)),
                        )
                    await cur.execute("INSERT INTO admin_module_engine_seed(singleton) VALUES (TRUE)")
        _MODULE_REGISTRY_AVAILABLE = True
        await refresh_module_registries(pool)
    except Exception as exc:
        _MODULE_REGISTRY_AVAILABLE = False
        logger.warning("admin module registry unavailable; apply migration 011 (%s)", type(exc).__name__)
        return False
    return True


async def list_modules(pool) -> list[dict[str, Any]]:
    if not pool:
        raise RuntimeError("module registry database unavailable")
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT module_id,module_type,display_name,module_version,definition,enabled,created_at,updated_at "
                "FROM admin_module_registry ORDER BY module_type,module_id"
            )
            rows = await cur.fetchall()
    for row in rows:
        if row["module_type"] != "logic":
            continue
        definition = row["definition"]
        if isinstance(definition, str):
            definition = json.loads(definition)
        if isinstance(definition, dict) and not definition.get("outputs"):
            definition["outputs"] = list(OUTPUTS.get(definition.get("implementation"), ["result"]))
        row["definition"] = definition
    return rows


async def save_module(
    pool, module_id: str, module_type: str, display_name: str, module_version: str,
    definition: Any, enabled: bool, actor: str,
) -> dict[str, Any]:
    if not display_name.strip() or len(display_name) > 100 or not module_version.strip() or len(module_version) > 32:
        raise ModuleDefinitionError("module name/version is required and must fit its limit")
    payload = dict(definition) if isinstance(definition, dict) else definition
    if isinstance(payload, dict) and module_type == "logic":
        payload.update(id=module_id, name=display_name.strip(), version=module_version.strip())
    if isinstance(payload, dict) and module_type == "engine":
        payload.update(id=module_id, label=display_name.strip(), version=int(module_version.strip()))
    payload = _normalise_definition(module_id, module_type, payload)
    if not pool:
        raise RuntimeError("module registry database unavailable")
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO admin_module_registry AS current "
                "(module_id,module_type,display_name,module_version,definition,enabled,updated_by) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s) "
                "ON CONFLICT(module_id) DO UPDATE SET display_name=EXCLUDED.display_name,"
                "module_version=EXCLUDED.module_version,definition=EXCLUDED.definition,enabled=EXCLUDED.enabled,"
                "updated_by=EXCLUDED.updated_by,updated_at=clock_timestamp() "
                "WHERE current.module_type=EXCLUDED.module_type "
                "RETURNING module_id,module_type,display_name,module_version,definition,enabled,created_at,updated_at",
                (module_id, module_type, display_name.strip(), module_version.strip(),
                 json.dumps(payload, ensure_ascii=False), bool(enabled), actor[:64]),
            )
            row = await cur.fetchone()
            if not row:
                raise ModuleDefinitionError("module id is already registered with another type")
            await cur.execute(
                "INSERT INTO system_audit_log(component_name,action,entity_type,entity_id,actor_type,actor_id,details) "
                "VALUES ('admin_modules','ADMIN_MODULE_SAVED','ADMIN',%s,'ADMIN',%s,%s::jsonb)",
                (module_id, actor[:64], json.dumps({"module_type": module_type, "enabled": bool(enabled)})),
            )
    if module_type in {"object", "engine"}:
        await _refresh_objects_best_effort(pool)
    return row


async def _engine_object_dependents(cur, engine_id: str) -> list[str]:
    await cur.execute(
        "SELECT module_id, definition, enabled FROM admin_module_registry WHERE module_type='object'"
    )
    dependents: list[str] = []
    for module_id, definition, enabled in await cur.fetchall():
        if not enabled:
            continue
        if isinstance(definition, str):
            definition = json.loads(definition)
        if not isinstance(definition, dict):
            continue
        if any(str(binding.get("id")) == engine_id for binding in definition.get("engines") or [] if isinstance(binding, dict)):
            dependents.append(str(module_id))
    return sorted(dependents)


async def _object_missing_engines(cur, definition: Any) -> list[str]:
    if isinstance(definition, str):
        definition = json.loads(definition)
    if not isinstance(definition, dict):
        return []
    bindings = {
        str(binding.get("id"))
        for binding in definition.get("engines") or []
        if isinstance(binding, dict) and binding.get("id")
    }
    if not bindings:
        return []
    await cur.execute(
        "SELECT module_id FROM admin_module_registry WHERE module_type='engine' AND enabled"
    )
    enabled = {str(row[0]) for row in await cur.fetchall()}
    return sorted(bindings - enabled)


async def set_module_enabled(pool, module_id: str, enabled: bool, actor: str) -> bool:
    if not pool:
        raise RuntimeError("module registry database unavailable")
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                "SELECT module_type, definition FROM admin_module_registry WHERE module_id=%s",
                (module_id,),
            )
            current = await cur.fetchone()
            if not current:
                return False
            module_type, definition = current
            if module_type == "engine" and not enabled:
                dependents = await _engine_object_dependents(cur, module_id)
                if dependents:
                    raise ModuleDefinitionError(
                        "engine is used by enabled image object(s): " + ", ".join(dependents)
                    )
            if module_type == "object" and enabled:
                missing = await _object_missing_engines(cur, definition)
                if missing:
                    raise ModuleDefinitionError(
                        "object references missing/disabled engine(s): " + ", ".join(missing)
                    )
            await cur.execute(
                "UPDATE admin_module_registry SET enabled=%s,updated_by=%s,updated_at=clock_timestamp() "
                "WHERE module_id=%s RETURNING module_type",
                (bool(enabled), actor[:64], module_id),
            )
            row = await cur.fetchone()
            await cur.execute(
                "INSERT INTO system_audit_log(component_name,action,entity_type,entity_id,actor_type,actor_id,details) "
                "VALUES ('admin_modules','ADMIN_MODULE_TOGGLED','ADMIN',%s,'ADMIN',%s,%s::jsonb)",
                (module_id, actor[:64], json.dumps({"module_type": row[0], "enabled": bool(enabled)})),
            )
    if row[0] in {"object", "engine"}:
        await _refresh_objects_best_effort(pool)
    return True

async def delete_module(pool, module_id: str, actor: str) -> bool:
    if not pool:
        raise RuntimeError("module registry database unavailable")
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                "SELECT module_type FROM admin_module_registry WHERE module_id=%s",
                (module_id,),
            )
            current = await cur.fetchone()
            if not current:
                return False
            module_type = current[0]
            if module_type == "engine":
                dependents = await _engine_object_dependents(cur, module_id)
                if dependents:
                    raise ModuleDefinitionError(
                        "engine is used by enabled image object(s): " + ", ".join(dependents)
                    )
            await cur.execute(
                "DELETE FROM admin_module_registry WHERE module_id=%s RETURNING module_type",
                (module_id,),
            )
            row = await cur.fetchone()
            await cur.execute(
                "INSERT INTO system_audit_log(component_name,action,entity_type,entity_id,actor_type,actor_id,details) "
                "VALUES ('admin_modules','ADMIN_MODULE_DELETED','ADMIN',%s,'ADMIN',%s,%s::jsonb)",
                (module_id, actor[:64], json.dumps({"module_type": module_type})),
            )
    if module_type in {"object", "engine"}:
        await _refresh_objects_best_effort(pool)
    return True

async def refresh_module_registries(pool) -> None:
    if not pool or not _MODULE_REGISTRY_AVAILABLE:
        return
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT module_id,module_type,definition,enabled FROM admin_module_registry "
                "WHERE module_type IN ('object','engine')"
            )
            rows = await cur.fetchall()
    engines: dict[str, EngineDefinition] = {}
    objects: dict[str, ImageObjectDefinition] = {}
    for row in rows:
        module_id = str(row["module_id"])
        module_type = str(row["module_type"])
        definition = row["definition"]
        if isinstance(definition, str):
            definition = json.loads(definition)
        if not isinstance(definition, dict):
            raise ModuleDefinitionError(f"invalid stored {module_type} definition: {module_id}")
        if module_type == "engine":
            if not bool(row["enabled"]):
                continue
            if definition.get("id") != module_id:
                raise ModuleDefinitionError(f"invalid stored engine definition: {module_id}")
            try:
                engines[module_id] = EngineDefinition.model_validate(definition)
            except ValidationError as exc:
                raise ModuleDefinitionError(f"invalid stored engine definition: {module_id}") from exc
            continue
        if definition.get("id") != module_id:
            raise ModuleDefinitionError(f"invalid stored image object definition: {module_id}")
        try:
            obj = ImageObjectDefinition.model_validate(definition)
        except ValidationError as exc:
            raise ModuleDefinitionError(f"invalid stored image object definition: {module_id}") from exc
        objects[module_id] = obj.model_copy(update={"enabled": bool(row["enabled"])})
    for obj in objects.values():
        if not obj.enabled:
            continue
        missing = sorted({binding.id for binding in obj.engines if binding.id not in engines})
        if missing:
            raise ModuleDefinitionError(
                f"image object '{obj.id}' references missing/disabled engine(s): {', '.join(missing)}"
            )
    set_database_engine_registry(engines)
    set_database_object_registry(objects)


async def refresh_image_object_registry(pool) -> None:
    """Compatibility alias for callers that refresh the shared catalog."""
    await refresh_module_registries(pool)


async def _refresh_objects_best_effort(pool) -> None:
    try:
        await refresh_module_registries(pool)
    except Exception as exc:
        logger.error("[ADMIN MODULES] module registry refresh rejected: %s", type(exc).__name__)


async def execute_logic_module(pool, module_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
    if not pool:
        raise RuntimeError("module registry database unavailable")
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT definition FROM admin_module_registry WHERE module_id=%s AND module_type='logic' AND enabled",
                (module_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise ModuleDefinitionError("logic module is missing or disabled")
    definition = row["definition"]
    if isinstance(definition, str):
        definition = json.loads(definition)
    module = LogicModule.model_validate(definition)
    values = {}
    allowed = {field.name for field in module.inputs}
    if not isinstance(inputs, dict) or set(inputs) != allowed:
        raise ModuleDefinitionError("input keys do not match the module schema")
    for field in module.inputs:
        value = inputs[field.name]
        if type(value) is not int:
            raise ModuleDefinitionError(f"{field.name} must be an integer")
        values[field.name] = value
    return IMPLEMENTATIONS[module.implementation](values)
