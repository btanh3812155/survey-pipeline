# -*- coding: utf-8 -*-
"""
Authoritative Schema & Format Synchronization Engine.
Safeguards against format desynchronization between disk JSONs, in-memory registries,
Pydantic model definitions, and downstream consumer contracts (WebRTC ingress,
frontend rendering, admin moderation, and Google Drive deliverables).
"""

import os
import json
from copy import deepcopy
import logging
from typing import Dict, List, Optional, Any, Tuple

from services.form_engine import FormSchema, FormQuestion, DEFAULT_TRUST_BRIDGE_SCHEMA

logger = logging.getLogger("core.schema_sync")


REQUIRED_SLOT_FIELDS = {
    "slot": int,
    "title": str,
    "role": str,
    "required": bool,
    "require_unique": bool,
    "ai_check": bool,
    "ai_detection_prompt": str,
    "allow_upload": bool,
    "tooltip": str,
    "object_type": str,
    "object_config": dict,
}

SLOT_DEFAULTS = {
    "slot": 0,
    "title": "Ảnh xác thực",
    "role": "general_photo",
    "required": True,
    "require_unique": False,
    "ai_check": False,
    "ai_detection_prompt": "",
    "allow_upload": True,
    "tooltip": "",
    "object_type": "",
    "object_config": {},
}


def normalize_slot_definition(slot_raw: dict, fallback_index: int = 0) -> dict:
    """Ensures an image slot has all required fields with standardized types and defaults."""
    normalized = deepcopy(SLOT_DEFAULTS)
    normalized["slot"] = fallback_index

    for k, v in slot_raw.items():
        if k in REQUIRED_SLOT_FIELDS:
            if k == "slot":
                try:
                    normalized[k] = int(v)
                except (ValueError, TypeError):
                    normalized[k] = fallback_index
            elif k in ("required", "require_unique", "ai_check", "allow_upload"):
                normalized[k] = bool(v)
            elif k == "object_config":
                normalized[k] = dict(v) if isinstance(v, dict) else {}
            else:
                normalized[k] = str(v) if v is not None else ""
        else:
            normalized[k] = v

    return normalized


def reconcile_form_questions(
    base_questions: List[FormQuestion], disk_questions_raw: Optional[List[dict]] = None
) -> List[FormQuestion]:
    """
    Reconciles question list between in-code baseline and disk-persisted JSON definitions.
    Propagates new fields (such as ai_detection_prompt or allow_upload) to all components.
    """
    if not disk_questions_raw:
        # Normalize baseline slots
        reconciled = []
        for q in base_questions:
            q_copy = q.model_copy(deep=True)
            if q_copy.image_slots:
                q_copy.image_slots = [
                    normalize_slot_definition(s, idx)
                    for idx, s in enumerate(q_copy.image_slots)
                ]
            reconciled.append(q_copy)
        return reconciled

    disk_map = {q.get("id"): q for q in disk_questions_raw if isinstance(q, dict) and q.get("id")}
    reconciled_questions: List[FormQuestion] = []

    for base_q in base_questions:
        disk_raw = disk_map.get(base_q.id)
        if not disk_raw:
            # Baseline question not in disk: retain normalized baseline
            q_copy = base_q.model_copy(deep=True)
            if q_copy.image_slots:
                q_copy.image_slots = [
                    normalize_slot_definition(s, idx)
                    for idx, s in enumerate(q_copy.image_slots)
                ]
            reconciled_questions.append(q_copy)
            continue

        # Merge question fields: preserve disk overrides while ensuring new schema fields are present
        merged_q_dict = base_q.model_dump()
        for k, v in disk_raw.items():
            if v is not None:
                merged_q_dict[k] = v

        # Deep reconciliation for image_slots
        base_slots = base_q.image_slots or []
        disk_slots = disk_raw.get("image_slots") or []

        merged_slots: List[dict] = []
        max_len = max(len(base_slots), len(disk_slots))

        for idx in range(max_len):
            b_slot = base_slots[idx] if idx < len(base_slots) else {}
            d_slot = disk_slots[idx] if idx < len(disk_slots) else {}

            merged_slot = deepcopy(b_slot) if b_slot else deepcopy(d_slot)
            for sk, sv in d_slot.items():
                if sv is not None and sv != "":
                    merged_slot[sk] = sv
            for sk, sv in b_slot.items():
                # Baseline non-empty values (e.g. newly added ai_detection_prompt) populate if disk was blank
                if sv is not None and sv != "" and not merged_slot.get(sk):
                    merged_slot[sk] = sv
            base_config = b_slot.get("object_config")
            disk_config = d_slot.get("object_config")
            if isinstance(base_config, dict) and base_config:
                # Keep operator overrides but add newly introduced object-config keys
                merged_slot["object_config"] = {
                    **base_config,
                    **(disk_config if isinstance(disk_config, dict) else {}),
                }

            merged_slots.append(normalize_slot_definition(merged_slot, idx))

        if merged_slots:
            merged_q_dict["image_slots"] = merged_slots
            merged_q_dict["min_images"] = len(merged_slots)
            merged_q_dict["max_images"] = len(merged_slots)
            merged_q_dict["exact_image_count"] = len(merged_slots)

        reconciled_questions.append(FormQuestion.model_validate(merged_q_dict))

    # Also include any newly created questions from disk that are not in baseline
    base_ids = {q.id for q in base_questions}
    for q_id, disk_q in disk_map.items():
        if q_id not in base_ids:
            try:
                if disk_q.get("image_slots"):
                    disk_q["image_slots"] = [
                        normalize_slot_definition(s, idx)
                        for idx, s in enumerate(disk_q["image_slots"])
                    ]
                reconciled_questions.append(FormQuestion.model_validate(disk_q))
            except Exception as e:
                logger.warning(f"Failed to validate extra disk question {q_id}: {e}")

    return reconciled_questions


def reconcile_and_sync_form_schema(
    base_schema: FormSchema, disk_json_path: Optional[str] = None
) -> Tuple[FormSchema, bool]:
    """
    Reconciles in-memory baseline schema with disk JSON.
    If divergence is detected, merges both bidirectionally and updates the disk file.
    Returns (synchronized_schema, was_updated_on_disk).
    """
    disk_data: Optional[dict] = None
    if disk_json_path and os.path.exists(disk_json_path):
        try:
            with open(disk_json_path, "r", encoding="utf-8") as f:
                disk_data = json.load(f)
        except Exception as e:
            logger.warning(f"Error reading disk schema from {disk_json_path}: {e}")

    reconciled_questions = reconcile_form_questions(
        base_schema.questions, disk_data.get("questions") if disk_data else None
    )

    merged_schema_dict = base_schema.model_dump()
    if disk_data:
        # Preserve disk settings and descriptions if customized
        for k, v in disk_data.items():
            if k != "questions" and v is not None:
                merged_schema_dict[k] = v

    merged_schema_dict["questions"] = [q.model_dump() for q in reconciled_questions]
    canonical_schema = FormSchema.model_validate(merged_schema_dict)

    # Check if disk file needs rewrite to maintain zero drift
    was_updated = False
    if disk_json_path:
        canonical_json = canonical_schema.model_dump_json(indent=2)
        needs_write = True
        if disk_data:
            try:
                current_disk_json = json.dumps(disk_data, indent=2, ensure_ascii=False)
                if current_disk_json == canonical_json:
                    needs_write = False
            except Exception:
                needs_write = True

        if needs_write:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(disk_json_path)), exist_ok=True)
                # Atomic replace: sibling workers refresh this file from disk at any time.
                tmp_path = f"{disk_json_path}.tmp.{os.getpid()}"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(canonical_json)
                os.replace(tmp_path, disk_json_path)
                was_updated = True
                logger.info(f"✅ [SCHEMA SYNC] Automatically synchronized canonical schema to {disk_json_path}")
            except Exception as e:
                logger.error(f"Failed to write synchronized schema to {disk_json_path}: {e}")

    return canonical_schema, was_updated


def sync_all_persisted_forms(
    forms_dir: str, default_schema: Optional[FormSchema] = None
) -> Dict[str, FormSchema]:
    """
    Scans forms directory, applies automated reconciliation to every schema,
    guarantees bidirectional synchronization, and returns the authoritative registry.
    """
    registry: Dict[str, FormSchema] = {}
    base_schema = default_schema or DEFAULT_TRUST_BRIDGE_SCHEMA

    os.makedirs(forms_dir, exist_ok=True)
    default_disk_path = os.path.join(forms_dir, f"{base_schema.form_id}.json")

    # 1. Seed the built-in default schema only for an empty deployment.
    #    A forms directory that already has JSONs is authoritative; never resurrect
    #    the default form (e.g. income_survey) once operators removed it.
    persisted_json_names = [
        fname for fname in os.listdir(forms_dir) if fname.endswith(".json")
    ]
    if not persisted_json_names:
        synced_default, _ = reconcile_and_sync_form_schema(base_schema, default_disk_path)
        registry[synced_default.form_id] = synced_default

    # 2. Sync all other JSON schemas in directory
    try:
        for fname in os.listdir(forms_dir):
            if fname.endswith(".json"):
                fpath = os.path.join(forms_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        disk_obj = json.load(f)
                    parsed = FormSchema.model_validate(disk_obj)
                    # The default form's in-code baseline is authoritative for
                    # newly added slot contract fields (object_type/object_config).
                    baseline = base_schema if parsed.form_id == base_schema.form_id else parsed
                    synced_schema, _ = reconcile_and_sync_form_schema(baseline, fpath)
                    registry[synced_schema.form_id] = synced_schema
                except Exception as err:
                    logger.warning(f"Error validating and syncing {fname}: {err}")
    except Exception as e:
        logger.error(f"Error scanning forms directory for schema sync: {e}")

    return registry


def verify_schema_downstream_parity(schema: FormSchema) -> List[str]:
    """
    Validates that a FormSchema satisfies all architectural invariants required
    by downstream pipeline components (WebRTC ingress, AI vision, Admin Moderator, Drive).
    Returns list of violations (empty list = 100% compliant).
    """
    violations: List[str] = []

    # 1. Verify camera question exists
    cam_q = next((q for q in schema.questions if q.type == "camera_webrtc"), None)
    if not cam_q:
        violations.append("Schema missing 'camera_webrtc' question required for visual verification.")
    else:
        # 2. Verify image slots cover required roles for fleet verification
        slots = cam_q.image_slots or []
        if len(slots) < 2:
            violations.append(f"Camera question has only {len(slots)} slots; minimum 2 required for profile & income.")
        else:
            roles = [s.get("role", "") for s in slots]
            if not any("profile" in r or "odometer" in r for r in roles):
                violations.append("Image slots missing 'app_profile' role for driver identity verification.")
            if not any("income" in r for r in roles):
                violations.append("Image slots missing 'app_income' role for revenue auditing.")

            for s_idx, slot in enumerate(slots):
                for req_key in REQUIRED_SLOT_FIELDS:
                    if req_key not in slot:
                        violations.append(f"Slot {s_idx} missing required contract field '{req_key}'.")

    # 3. Verify mandatory questions have titles
    for q in schema.questions:
        if not q.id:
            violations.append("Found question with empty or missing 'id'.")
        if not q.title:
            violations.append(f"Question '{q.id}' is missing a title.")

    return violations
