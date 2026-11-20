# -*- coding: utf-8 -*-
"""Wave 3 durable submission-attempt helpers."""

import hashlib
import hmac
import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from config import settings
from core.phone_links import normalize_vn_phone


def canonical_phone_identity(phone: Optional[str]) -> str:
    value = str(phone or "").strip()
    if not value:
        return "anonymous"
    normalized = normalize_vn_phone(value) or value
    return hmac.new(str(settings.APP_SECRET).encode(), normalized.encode(), hashlib.sha256).hexdigest()


def attempt_scope(form_id: str, mode: str, phone: Optional[str]) -> str:
    return f"{settings.ENVIRONMENT}:{form_id}:{mode}:{canonical_phone_identity(phone)}"


def request_fingerprint(
    form_id: str,
    mode: str,
    answers: Dict[str, Any],
    uploads: Iterable[bytes],
    upload_slots: Optional[Iterable[Any]] = None,
    *,
    signature: Optional[Any] = None,
    gps: Optional[Dict[str, Any]] = None,
    ctv: Optional[str] = None,
) -> str:
    """Versioned stable fingerprint; excludes key/timestamps/model output."""
    upload_items = list(uploads)
    slots = list(upload_slots or [])
    payload = {
        "version": 1,
        "form_id": form_id,
        "mode": mode,
        "answers": answers,
        "uploads": [
            {
                "question_id": (slots[index][0] if index < len(slots) and isinstance(slots[index], (tuple, list)) else None),
                "slot_index": (slots[index][1] if index < len(slots) and isinstance(slots[index], (tuple, list)) and len(slots[index]) > 1 else None),
                "sha256": hashlib.sha256(bytes(item)).hexdigest(),
            }
            for index, item in enumerate(upload_items)
        ],
        "signature": hashlib.sha256(str(signature or "").encode()).hexdigest(),
        "gps": gps or {},
        "ctv": ctv or None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


async def reserve(
    conn, scope: str, key: str, fingerprint: str
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Reserve in the caller's transaction; returns (new|replay|conflict|reserved, response)."""
    # Never hold an HTTP request behind another writer indefinitely. The caller
    # may return a controlled retryable failure; it must not mint a fresh key.
    await conn.execute("SET LOCAL lock_timeout = '5s'")
    cur = await conn.execute(
        """
        INSERT INTO submission_idempotency
            (scope, idempotency_key, request_fingerprint, state)
        VALUES (%s, %s, %s, 'RESERVED')
        ON CONFLICT (scope, idempotency_key) DO NOTHING
        RETURNING scope;
        """,
        (scope, key, fingerprint),
    )
    inserted = await cur.fetchone()
    if inserted:
        return "new", None
    # The conflicting transaction holds the row; this waits until it commits.
    cur = await conn.execute(
        "SELECT state, request_fingerprint, response_json FROM submission_idempotency "
        "WHERE scope=%s AND idempotency_key=%s FOR UPDATE;",
        (scope, key),
    )
    row = await cur.fetchone()
    if not row:
        return "reserved", None
    state, stored_fingerprint, response = row[0], row[1], row[2]
    if state == "COMMITTED":
        if stored_fingerprint != fingerprint:
            return "conflict", None
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except Exception:
                response = None
        return "replay", response if isinstance(response, dict) else None
    return "reserved", None


async def commit_response(conn, scope: str, key: str, fingerprint: str, submission_id: str, response: Dict[str, Any]) -> None:
    await conn.execute(
        "UPDATE submission_idempotency SET state='COMMITTED', submission_id=%s, "
        "response_json=%s::jsonb WHERE scope=%s AND idempotency_key=%s "
        "AND request_fingerprint=%s AND state='RESERVED';",
        (submission_id, json.dumps(response), scope, key, fingerprint),
    )
