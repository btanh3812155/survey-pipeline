"""Database-backed catalog for declarative object and allowlisted logic modules."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from services.image_objects import ImageObjectDefinition, load_registry, set_database_object_registry
from services.logic_modules import IMPLEMENTATIONS, default_get_percentage_definition

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
            return LogicModule.model_validate(payload).model_dump(mode="json")
    except (ValidationError, ValueError) as exc:
        raise ModuleDefinitionError(str(exc)) from exc
    raise ModuleDefinitionError("module type must be object or logic")

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
        _MODULE_REGISTRY_AVAILABLE = True
        await refresh_image_object_registry(pool)
    except Exception as exc:
        _MODULE_REGISTRY_AVAILABLE = False
        logger.warning("admin module registry unavailable; apply migration 008 (%s)", type(exc).__name__)
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
            return await cur.fetchall()


async def save_module(
    pool, module_id: str, module_type: str, display_name: str, module_version: str,
    definition: Any, enabled: bool, actor: str,
) -> dict[str, Any]:
    if not display_name.strip() or len(display_name) > 100 or not module_version.strip() or len(module_version) > 32:
        raise ModuleDefinitionError("module name/version is required and must fit its limit")
    payload = dict(definition) if isinstance(definition, dict) else definition
    if isinstance(payload, dict) and module_type == "logic":
        payload.update(id=module_id, name=display_name.strip(), version=module_version.strip())
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
    if module_type == "object":
        await _refresh_objects_best_effort(pool)
    return row


async def set_module_enabled(pool, module_id: str, enabled: bool, actor: str) -> bool:
    if not pool:
        raise RuntimeError("module registry database unavailable")
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                "UPDATE admin_module_registry SET enabled=%s,updated_by=%s,updated_at=clock_timestamp() "
                "WHERE module_id=%s RETURNING module_type",
                (bool(enabled), actor[:64], module_id),
            )
            row = await cur.fetchone()
            if not row:
                return False
            await cur.execute(
                "INSERT INTO system_audit_log(component_name,action,entity_type,entity_id,actor_type,actor_id,details) "
                "VALUES ('admin_modules','ADMIN_MODULE_TOGGLED','ADMIN',%s,'ADMIN',%s,%s::jsonb)",
                (module_id, actor[:64], json.dumps({"module_type": row[0], "enabled": bool(enabled)})),
            )
    if row[0] == "object":
        await _refresh_objects_best_effort(pool)
    return True


async def delete_module(pool, module_id: str, actor: str) -> bool:
    if not pool:
        raise RuntimeError("module registry database unavailable")
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM admin_module_registry WHERE module_id=%s RETURNING module_type",
                (module_id,),
            )
            row = await cur.fetchone()
            if not row:
                return False
            await cur.execute(
                "INSERT INTO system_audit_log(component_name,action,entity_type,entity_id,actor_type,actor_id,details) "
                "VALUES ('admin_modules','ADMIN_MODULE_DELETED','ADMIN',%s,'ADMIN',%s,%s::jsonb)",
                (module_id, actor[:64], json.dumps({"module_type": row[0]})),
            )
    if row[0] == "object":
        await _refresh_objects_best_effort(pool)
    return True


async def refresh_image_object_registry(pool) -> None:
    if not pool or not _MODULE_REGISTRY_AVAILABLE:
        return
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT module_id,definition,enabled FROM admin_module_registry WHERE module_type='object'"
            )
            rows = await cur.fetchall()
    objects: dict[str, ImageObjectDefinition] = {}
    for row in rows:
        module_id = str(row["module_id"])
        definition = row["definition"]
        if isinstance(definition, str):
            definition = json.loads(definition)
        if not isinstance(definition, dict) or definition.get("id") != module_id:
            raise ModuleDefinitionError(f"invalid stored image object definition: {module_id}")
        try:
            obj = ImageObjectDefinition.model_validate(definition)
        except ValidationError as exc:
            raise ModuleDefinitionError(f"invalid stored image object definition: {module_id}") from exc
        objects[module_id] = obj.model_copy(update={"enabled": bool(row["enabled"])})
    set_database_object_registry(objects)


async def _refresh_objects_best_effort(pool) -> None:
    try:
        await refresh_image_object_registry(pool)
    except Exception as exc:
        logger.error("[ADMIN MODULES] object registry refresh rejected: %s", type(exc).__name__)


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
