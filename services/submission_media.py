# -*- coding: utf-8 -*-
"""Private submission media adapter.

Submission bytes live only under ``data/submission_photos/<id>/`` and are never
served by the public /media mount. The database manifest is authoritative for
new rows; historical empty manifests retain the legacy flat-file fallback.
"""

import hashlib
import json
import mimetypes
import os
import re
from typing import Any, Dict, Iterable, List, Optional

from psycopg.rows import tuple_row

from config import settings
from core.durable_work import audit, enqueue_submission_work, transition_submission
from core.phone_links import VERIFIED_PHONE_LINK_SOURCES, phone_normalization_sql

PRIVATE_MEDIA_ROOT = os.path.join(os.getcwd(), "data", "submission_photos")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def private_submission_dir(submission_id: str) -> str:
    if not _SAFE_ID.fullmatch(str(submission_id or "")):
        raise ValueError("invalid submission id")
    root = os.path.realpath(PRIVATE_MEDIA_ROOT)
    path = os.path.realpath(os.path.join(root, submission_id))
    if os.path.commonpath([root, path]) != root:
        raise ValueError("submission path escaped private root")
    return path


def safe_media_path(submission_id: str, relative_path: str) -> str:
    base = private_submission_dir(submission_id)
    name = os.path.basename(str(relative_path or ""))
    if not _SAFE_NAME.fullmatch(name):
        raise ValueError("invalid media filename")
    target = os.path.realpath(os.path.join(base, name))
    if os.path.commonpath([base, target]) != base or not os.path.isfile(target):
        raise FileNotFoundError(name)
    return target


def _legacy_paths(submission_id: str) -> List[str]:
    base = private_submission_dir(submission_id)
    if not os.path.isdir(base):
        return []
    exts = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".bin")
    found = []
    for name in os.listdir(base):
        candidate = os.path.join(base, name)
        if name.lower().endswith(exts) and os.path.isfile(candidate):
            resolved = os.path.realpath(candidate)
            if os.path.commonpath([base, resolved]) != base:
                continue
            found.append(resolved)
    return sorted(found, key=lambda p: (int(re.search(r"(\d+)", os.path.basename(p)).group(1)) if re.search(r"(\d+)", os.path.basename(p)) else 10**9, p))


def manifest_paths(submission_id: str, manifest: Optional[Iterable[Dict[str, Any]]]) -> List[str]:
    """Resolve manifest entries, falling back to the historical private flat files."""
    entries = list(manifest or [])
    if not entries:
        return _legacy_paths(submission_id)
    paths: List[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            path = safe_media_path(submission_id, entry.get("relative_path") or "")
            expected_hash = str(entry.get("sha256") or "").lower()
            if expected_hash:
                with open(path, "rb") as handle:
                    actual_hash = hashlib.sha256(handle.read()).hexdigest()
                if actual_hash != expected_hash:
                    continue
            paths.append(path)
        except (ValueError, FileNotFoundError, OSError):
            # A missing/hash-mismatched revision is not a reason to expose a
            # different file. The caller can surface the missing slot.
            continue
    return paths


def build_manifest(
    submission_id: str,
    files: List[Dict[str, Any]],
    media_version: int = 1,
    base_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    entries = []
    base = os.path.realpath(base_dir) if base_dir else private_submission_dir(submission_id)
    for index, item in enumerate(files):
        name = os.path.basename(str(item.get("name") or ""))
        if not _SAFE_NAME.fullmatch(name):
            raise ValueError("invalid media filename")
        target = os.path.realpath(os.path.join(base, name))
        if os.path.commonpath([base, target]) != base or not os.path.isfile(target):
            raise FileNotFoundError(name)
        path = target
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        entries.append({
            "question_id": item.get("question_id"),
            "slot_index": item.get("slot_index", index),
            "flat_index": item.get("flat_index", index),
            "relative_path": name,
            "sha256": digest,
            "mime": item.get("mime") or ("image/webp" if name.endswith(".webp") else "application/octet-stream"),
            "media_version": int(media_version),
        })
    return entries


def legacy_manifest(submission_id: str, media_version: int = 1) -> List[Dict[str, Any]]:
    """Pin the latest committed-or-earlier flat-file revision of each historical slot."""
    selected = {}
    for path in _legacy_paths(submission_id):
        name = os.path.basename(path)
        match = re.fullmatch(r"slot_(\d+)(?:_v(\d+))?\.[A-Za-z0-9]+", name)
        if not match:
            continue
        slot, revision = int(match.group(1)), int(match.group(2) or 1)
        if revision <= media_version and (slot not in selected or revision > selected[slot][0]):
            selected[slot] = (revision, name)
    return build_manifest(submission_id, [
        {"name": name, "slot_index": slot, "flat_index": slot,
         "mime": mimetypes.guess_type(name)[0] or "application/octet-stream"}
        for slot, (_, name) in sorted(selected.items())
    ], media_version)


async def load_manifest(db_pool, submission_id: str) -> List[Dict[str, Any]]:
    if not db_pool:
        return []
    async with db_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT media_manifest FROM survey_submissions WHERE UPPER(submission_id)=UPPER(%s);",
            (submission_id,),
        )
        row = await cur.fetchone()
    if not row or not row[0]:
        return []
    value = row[0]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    return value if isinstance(value, list) else []


async def write_replacement(
    db_pool,
    submission_id: str,
    slot_index: Optional[int],
    webp_bytes: bytes,
    *,
    question_id: Optional[str] = None,
    flat_index: Optional[int] = None,
    requester_telegram_id: Optional[int] = None,
    approve_reason: Optional[str] = None,
) -> Optional[str]:
    """Write an immutable revision, manifest, and approval in one transaction.

    Database-backed replacements require a trusted phone binding for the
    requesting Telegram account and the submission's own phone. The caller
    cannot bypass this by knowing a submission id or changing a callback slot.
    """
    if not db_pool:
        return None
    async with db_pool.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
        if requester_telegram_id is None:
            raise PermissionError("replacement requires a bound Telegram requester")
        await cur.execute(
            f"""
            SELECT s.media_version, s.media_manifest, s.status, s.driver_phone,
                   s.collaborator_id, s.ctv_code, s.submission_id,
                   COALESCE(to_jsonb(s)->>'work_owner', 'legacy')
            FROM survey_submissions AS s
            JOIN phone_telegram_links AS link
              ON link.phone_normalized = {phone_normalization_sql('s.driver_phone')}
            WHERE UPPER(s.submission_id)=UPPER(%s)
              AND link.telegram_user_id = %s
              AND link.telegram_user_id > 0
              AND link.source IN (%s, %s)
              AND s.created_at >= link.updated_at
            FOR UPDATE OF s
            FOR SHARE OF link;
            """,
            (submission_id, int(requester_telegram_id), *VERIFIED_PHONE_LINK_SOURCES),
        )
        row = await cur.fetchone()
        if not row:
            raise PermissionError("requester is not authorized for this submission")
        canonical_id, work_owner = row[6:8]
        if work_owner != "outbox_v2" and settings.DURABLE_MODERATION_ENABLED:
            raise ValueError("LEGACY_RECONCILIATION_REQUIRED")
        version = int(row[0] or 1) + 1
        manifest = row[1] or []
        current_status = str(row[2] or "").upper()
        if current_status == "APPROVED":
            raise ValueError("submission is already approved")
        base = private_submission_dir(canonical_id)
        if isinstance(manifest, str):
            try:
                manifest = json.loads(manifest)
            except Exception:
                manifest = []
        if not isinstance(manifest, list):
            manifest = []
        if not manifest:
            manifest = legacy_manifest(canonical_id, int(row[0] or 1))
        matches = []
        for item in manifest:
            if not isinstance(item, dict):
                continue
            item_slot = int(item.get("slot_index", item.get("flat_index", -1)))
            item_flat = int(item.get("flat_index", item_slot))
            if flat_index is not None and item_flat != int(flat_index):
                continue
            if question_id is not None and str(item.get("question_id") or "") != str(question_id):
                continue
            if slot_index is not None and item_slot != int(slot_index):
                continue
            matches.append(item)
        if len(matches) != 1:
            raise ValueError("replacement target is missing or ambiguous")
        target = matches[0]
        target_flat = int(target.get("flat_index", target.get("slot_index", 0)))
        target_slot = int(target.get("slot_index", target_flat))
        target_question = target.get("question_id")
        name = f"slot_{target_flat}_v{version}.webp"
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, name)
        with open(path, "wb") as handle:
            handle.write(webp_bytes)
        digest = hashlib.sha256(webp_bytes).hexdigest()
        replacement_entry = {
            "question_id": target_question,
            "slot_index": target_slot,
            "flat_index": target_flat,
            "relative_path": name,
            "sha256": digest,
            "mime": "image/webp",
            "media_version": version,
        }
        entries = []
        replaced = False
        for item in manifest:
            if not isinstance(item, dict):
                continue
            item_slot = int(item.get("slot_index", item.get("flat_index", -1)))
            item_flat = int(item.get("flat_index", item_slot))
            same = (
                item_flat == target_flat
                and str(item.get("question_id") or "") == str(target_question or "")
                and item_slot == target_slot
            )
            if same and not replaced:
                entries.append(replacement_entry)
                replaced = True
            elif not same:
                entries.append(item)
        if not replaced:
            entries.append(replacement_entry)
        await conn.execute(
            "UPDATE survey_submissions SET media_manifest=%s::jsonb, media_version=%s "
            "WHERE submission_id=%s;",
            (json.dumps(entries), version, canonical_id),
        )
        if work_owner == "outbox_v2":
            actor = f"driver:{requester_telegram_id}"
            await audit(conn, "MEDIA_REPLACEMENT", canonical_id, actor, {"media_version": version})
            if approve_reason:
                changed, status = await transition_submission(
                    conn, canonical_id, "APPROVED", None, actor=actor,
                    expected_statuses=("REJECTED", "MANUAL_REVIEW", "APPEAL_PENDING"),
                )
                if not changed:
                    raise ValueError(f"replacement cannot approve a {status} submission")
            else:
                await enqueue_submission_work(conn, canonical_id, "MEDIA_REPLACEMENT")
        elif approve_reason:
            await conn.execute(
                "UPDATE survey_submissions SET status='APPROVED', rejection_reason=NULL, "
                "moderated_at=NOW() WHERE submission_id=%s;",
                (canonical_id,),
            )
            await conn.execute(
                "INSERT INTO outbox_events (event_type, flow_target, aggregate_id, payload, status) "
                "VALUES ('survey.validated', 'ALL', %s, %s::jsonb, 'PENDING');",
                (canonical_id, json.dumps({
                    "submission_id": canonical_id,
                    "status": "APPROVED",
                    "reason": approve_reason,
                    "collaborator_id": row[4],
                    "ctv_id": row[4],
                    "ctv_code": row[5],
                })),
            )
        await conn.commit()
    return name
