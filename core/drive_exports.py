"""Database-backed Google Drive export configuration and execution.

Remote layout under the configured Drive root:
  <master folder>/<images master>/<form>/<submission>/<image>
  <master folder>/<client exports>/<client>/<form>/<form> (Google Sheet)
  <master folder>/<client exports>/<client>/<form>/Images/<submission>/<shortcut>
  <master folder>/<internal exports>/<category>/<form>/...

Images are uploaded once to the master images tree. Client and internal folders
contain Drive shortcuts to those master files, never duplicate copies.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable
from urllib.parse import quote

import httpx
from psycopg.rows import dict_row

from services.google_drive_delivery import (
    GoogleDriveClient,
    detect_image_mime,
    is_test_submission,
    normalize_image_bytes,
)
from services.drive_cleanup import (
    clear_submission_folders,
    prune_stale_files,
    prune_stale_submission_folders,
    trash_form_folders,
)
from core.trash import ensure_form_trash, record_drive_ledger
from services.submission_media import manifest_paths

logger = logging.getLogger("core.drive_exports")

EXPORT_FIELDS = (
    ("ctv_code", "CTV code"),
    ("driver_phone", "Phone"),
    ("submission_id", "Submission ID"),
    ("form_id", "Form"),
    ("status", "Status"),
    ("created_at", "Submitted at"),
    ("vehicle_type", "Vehicle type"),
    ("car_model", "Car model"),
    ("membership_type", "Membership"),
    ("driver_name", "Driver name"),
    ("total_gross_revenue", "Gross revenue"),
    ("total_net_income", "Net income"),
    ("total_trips", "Trips"),
    ("rejection_reason", "Rejection reason"),
    ("form_data", "Form data (JSON)"),
)
FIELD_KEYS = tuple(key for key, _label in EXPORT_FIELDS)
DEFAULT_FIELDS = (
    "submission_id", "form_id", "status", "created_at", "vehicle_type",
    "total_gross_revenue", "total_net_income", "total_trips",
)
EXPORT_STATUSES = ("APPROVED", "REJECTED", "MANUAL_REVIEW", "APPEAL_PENDING")
VEHICLE_TYPES = ("CAR", "BIKE")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
DEFAULT_SETTINGS = {
    "master_folder_name": "Master Export",
    "client_folder_name": "clients",
    "internal_folder_name": "internal",
    "images_folder_name": "master images",
}
MASTER_DELIVERABLES_NAME = "Master_Survey_Deliverables"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
DRIVE_EXPORT_LOCK_KEY = 764539268
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}$")
_ROLE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class DriveExportError(ValueError):
    pass


def _folder_name(value: Any) -> str:
    text = str(value or "").strip().replace("/", "-")
    if not _FOLDER_RE.fullmatch(text):
        raise DriveExportError("folder names may use letters, numbers, spaces, dot, underscore and dash")
    return text


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return value


def _selected_fields(fields: Iterable[str]) -> list[str]:
    selected: list[str] = []
    for key in fields:
        if key in FIELD_KEYS and key not in selected:
            selected.append(key)
    return selected or list(DEFAULT_FIELDS)


def build_export_csv(rows: Iterable[dict[str, Any]], fields: Iterable[str]) -> bytes:
    selected = _selected_fields(fields)
    labels = dict(EXPORT_FIELDS)
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([labels[key] for key in selected])
    for row in rows:
        writer.writerow([_csv_value(row.get(key)) for key in selected])
    return out.getvalue().encode("utf-8-sig")


def _sheet_value(value: Any) -> Any:
    text = _csv_value(value)
    # Google Sheets rejects a single cell above 50,000 characters.
    if isinstance(text, str) and len(text) > 49000:
        return text[:48980] + "... [truncated]"
    return text


def export_matrix(rows: Iterable[dict[str, Any]], fields: Iterable[str]) -> list[list[Any]]:
    selected = _selected_fields(fields)
    labels = dict(EXPORT_FIELDS)
    matrix = [[labels[key] for key in selected]]
    matrix.extend([[_sheet_value(row.get(key)) for key in selected] for row in rows])
    return matrix


def _clean_list(values: Any, allowed: Iterable[str]) -> list[str]:
    allowed_set = set(allowed)
    return [value for value in values if value in allowed_set]


def _parse_optional_date(value: Any, label: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise DriveExportError(f"{label} must use YYYY-MM-DD") from exc


def _form_override_defaults(client: dict[str, Any]) -> dict[str, Any]:
    return {
        "fields": list(client.get("fields") or DEFAULT_FIELDS),
        "statuses": list(client.get("statuses") or ["APPROVED"]),
        "vehicle_types": list(client.get("vehicle_types") or []),
        "include_images": bool(client.get("include_images", True)),
        "date_from": client.get("date_from"),
        "date_to": client.get("date_to"),
        "date_from_clear": False,
        "date_to_clear": False,
    }


def effective_form_config(client: dict[str, Any], form_id: str) -> dict[str, Any]:
    """Merge one form's override over the client-wide export defaults."""
    effective = _form_override_defaults(client)
    override = (client.get("form_settings") or {}).get(str(form_id)) or {}
    for key in ("fields", "statuses", "vehicle_types", "include_images"):
        if key in override and override[key] is not None:
            effective[key] = override[key]
    for key in ("date_from", "date_to"):
        if override.get(f"{key}_clear") is True:
            effective[key] = None
            effective[f"{key}_clear"] = True
        elif override.get(key) is not None:
            effective[key] = override[key]
            effective[f"{key}_clear"] = False
    return effective


def _date_override(payload: dict[str, Any], key: str, label: str) -> tuple[str | None, bool]:
    raw_mode = payload.get(f"{key}_mode")
    if raw_mode is None:
        mode = "custom" if payload.get(key) else "inherit"
    else:
        mode = str(raw_mode).strip().lower()
    if mode == "inherit":
        return None, False
    if mode == "clear":
        return None, True
    if mode != "custom":
        raise DriveExportError(f"{label} mode must be inherit, clear, or custom")
    parsed = _parse_optional_date(payload.get(key), label)
    if not parsed:
        raise DriveExportError(f"{label} is required for a custom date")
    return parsed.isoformat(), False


def normalize_form_override(payload: dict[str, Any], client: dict[str, Any]) -> dict[str, Any]:
    defaults = _form_override_defaults(client)
    fields = _clean_list([str(v) for v in (payload.get("fields") or [])], FIELD_KEYS)
    statuses = _clean_list([str(v).upper() for v in (payload.get("statuses") or [])], EXPORT_STATUSES)
    vehicle_types = _clean_list(
        [str(v).upper() for v in (payload.get("vehicle_types") or [])], VEHICLE_TYPES
    )
    date_from, date_from_clear = _date_override(payload, "date_from", "date-from")
    date_to, date_to_clear = _date_override(payload, "date_to", "date-to")
    if date_from and date_to and date_to < date_from:
        raise DriveExportError("date-to cannot be before date-from")
    return {
        "fields": fields or defaults["fields"],
        "statuses": statuses or defaults["statuses"],
        "vehicle_types": vehicle_types,
        "include_images": bool(payload.get("include_images", defaults["include_images"])),
        "date_from": date_from,
        "date_to": date_to,
        "date_from_clear": date_from_clear,
        "date_to_clear": date_to_clear,
    }


async def ensure_drive_exports(pool) -> bool:
    if not pool:
        return False
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT singleton FROM drive_export_settings LIMIT 0")
                await cur.execute("SELECT slug FROM drive_export_clients LIMIT 0")
    except Exception as exc:
        logger.warning("drive export schema unavailable; apply migration 009 (%s)", type(exc).__name__)
        return False
    return True


async def get_settings(pool) -> dict[str, Any]:
    if not pool:
        raise RuntimeError("drive export database unavailable")
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM drive_export_settings WHERE singleton=TRUE")
            row = await cur.fetchone()
    return dict(row) if row else dict(DEFAULT_SETTINGS)


async def save_settings(
    pool,
    actor: str,
    *,
    master_folder_name: str,
    client_folder_name: str,
    internal_folder_name: str,
    images_folder_name: str,
) -> dict[str, Any]:
    if not pool:
        raise RuntimeError("drive export database unavailable")
    values = {
        "master_folder_name": _folder_name(master_folder_name),
        "client_folder_name": _folder_name(client_folder_name),
        "internal_folder_name": _folder_name(internal_folder_name),
        "images_folder_name": _folder_name(images_folder_name),
    }
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """INSERT INTO drive_export_settings
                   (singleton,master_folder_name,client_folder_name,internal_folder_name,images_folder_name,updated_by)
                   VALUES (TRUE,%s,%s,%s,%s,%s)
                   ON CONFLICT(singleton) DO UPDATE SET
                     master_folder_name=EXCLUDED.master_folder_name,
                     client_folder_name=EXCLUDED.client_folder_name,
                     internal_folder_name=EXCLUDED.internal_folder_name,
                     images_folder_name=EXCLUDED.images_folder_name,
                     updated_by=EXCLUDED.updated_by,updated_at=clock_timestamp()
                   RETURNING *""",
                (values["master_folder_name"], values["client_folder_name"],
                 values["internal_folder_name"], values["images_folder_name"], actor[:64]),
            )
            row = await cur.fetchone()
            await _audit(cur, actor, "DRIVE_EXPORT_SETTINGS_SAVED", "settings", values)
            return row


def normalize_client(payload: dict[str, Any]) -> dict[str, Any]:
    slug = str(payload.get("slug") or "").strip().lower()
    if not _SLUG_RE.fullmatch(slug):
        raise DriveExportError("slug must be 1-64 lowercase letters, numbers, underscore or dash")
    display_name = str(payload.get("display_name") or "").strip()
    if not display_name or len(display_name) > 100:
        raise DriveExportError("display name is required and must fit 100 characters")
    kind = str(payload.get("kind") or "client").strip().lower()
    if kind not in {"client", "internal"}:
        raise DriveExportError("kind must be client or internal")
    category = str(payload.get("category") or "").strip() if kind == "internal" else ""
    if kind == "internal" and not category:
        raise DriveExportError("internal exports require a category")
    folder_name = category if kind == "internal" else str(payload.get("folder_name") or display_name)
    folder_name = _folder_name(folder_name)
    date_from = payload.get("date_from") or None
    date_to = payload.get("date_to") or None
    try:
        date_from = date.fromisoformat(str(date_from)) if date_from else None
        date_to = date.fromisoformat(str(date_to)) if date_to else None
    except ValueError as exc:
        raise DriveExportError("date range must use YYYY-MM-DD") from exc
    if date_from and date_to and date_to < date_from:
        raise DriveExportError("date-to cannot be before date-from")
    form_ids = []
    for value in payload.get("form_ids") or []:
        value = str(value).strip()
        if value:
            if not _ROLE_RE.fullmatch(value):
                raise DriveExportError("invalid form id")
            form_ids.append(value)
    fields = _clean_list([str(v) for v in (payload.get("fields") or [])], FIELD_KEYS)
    if not fields:
        fields = list(DEFAULT_FIELDS)
    statuses = _clean_list([str(v).upper() for v in (payload.get("statuses") or [])], EXPORT_STATUSES)
    if not statuses:
        statuses = ["APPROVED"]
    vehicle_types = _clean_list([str(v).upper() for v in (payload.get("vehicle_types") or [])], VEHICLE_TYPES)
    base_client = {
        "fields": fields,
        "statuses": statuses,
        "vehicle_types": sorted(set(vehicle_types)),
        "include_images": bool(payload.get("include_images", True)),
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
    }
    raw_form_settings = payload.get("form_settings")
    if raw_form_settings in (None, ""):
        raw_form_settings = {}
    if not isinstance(raw_form_settings, dict):
        raise DriveExportError("form_settings must be an object keyed by form id")
    form_settings: dict[str, Any] = {}
    for form_id, override in raw_form_settings.items():
        form_id = str(form_id).strip()
        if not _ROLE_RE.fullmatch(form_id):
            raise DriveExportError("invalid form id in form_settings")
        if not isinstance(override, dict):
            raise DriveExportError(f"form settings for '{form_id}' must be an object")
        form_settings[form_id] = normalize_form_override(override, base_client)
    return {
        "slug": slug,
        "display_name": display_name,
        "kind": kind,
        "category": category or None,
        "folder_name": folder_name,
        "enabled": bool(payload.get("enabled", True)),
        "date_from": date_from,
        "date_to": date_to,
        "form_ids": sorted(set(form_ids)),
        "fields": fields,
        "statuses": statuses,
        "vehicle_types": sorted(set(vehicle_types)),
        "include_images": bool(payload.get("include_images", True)),
        "form_settings": form_settings,
    }


async def _audit(cur, actor: str, action: str, entity_id: str, details: dict[str, Any]) -> None:
    await cur.execute(
        "INSERT INTO system_audit_log(component_name,action,entity_type,entity_id,actor_type,actor_id,details) "
        "VALUES ('drive_exports',%s,'DRIVE_EXPORT',%s,'ADMIN',%s,%s::jsonb)",
        (action, entity_id[:64], actor[:64], json.dumps(details, default=str)),
    )


async def list_clients(pool) -> list[dict[str, Any]]:
    if not pool:
        raise RuntimeError("drive export database unavailable")
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM drive_export_clients ORDER BY kind, display_name, slug"
            )
            return await cur.fetchall()


async def get_client(pool, slug: str) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM drive_export_clients WHERE slug=%s", (slug,))
            return await cur.fetchone()


async def save_client(pool, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    if not pool:
        raise RuntimeError("drive export database unavailable")
    existing = await get_client(pool, str(payload.get("slug") or ""))
    effective_payload = dict(payload)
    if existing is not None:
        for key in ("fields", "statuses", "vehicle_types", "include_images"):
            effective_payload.setdefault(key, existing.get(key))
    client = normalize_client(effective_payload)
    if existing is not None and "form_settings" not in payload:
        client["form_settings"] = existing.get("form_settings") or {}
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """INSERT INTO drive_export_clients
                   (slug,display_name,kind,category,folder_name,enabled,date_from,date_to,
                    form_ids,fields,statuses,vehicle_types,include_images,form_settings,updated_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(slug) DO UPDATE SET
                     display_name=EXCLUDED.display_name,kind=EXCLUDED.kind,category=EXCLUDED.category,
                     folder_name=EXCLUDED.folder_name,enabled=EXCLUDED.enabled,
                     date_from=EXCLUDED.date_from,date_to=EXCLUDED.date_to,form_ids=EXCLUDED.form_ids,
                     fields=EXCLUDED.fields,statuses=EXCLUDED.statuses,vehicle_types=EXCLUDED.vehicle_types,
                     include_images=EXCLUDED.include_images,
                     form_settings=CASE WHEN %s THEN drive_export_clients.form_settings ELSE EXCLUDED.form_settings END,
                     updated_by=EXCLUDED.updated_by,updated_at=clock_timestamp()
                   RETURNING *""",
                (client["slug"], client["display_name"], client["kind"], client["category"],
                 client["folder_name"], client["enabled"], client["date_from"], client["date_to"],
                 client["form_ids"], client["fields"], client["statuses"], client["vehicle_types"],
                 client["include_images"], json.dumps(client["form_settings"]), actor[:64],
                 "form_settings" not in payload),
            )
            row = await cur.fetchone()
            await _audit(cur, actor, "DRIVE_EXPORT_CLIENT_SAVED", client["slug"], {
                "kind": client["kind"], "enabled": client["enabled"],
            })
            return row


async def save_client_form(
    pool, slug: str, form_id: str, payload: dict[str, Any], actor: str
) -> dict[str, Any]:
    """Atomically save one form override and keep the client's form selection."""
    if not pool:
        raise RuntimeError("drive export database unavailable")
    form_id = str(form_id or "").strip()
    if not _ROLE_RE.fullmatch(form_id):
        raise DriveExportError("invalid form id")
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM drive_export_clients WHERE slug=%s FOR UPDATE", (slug,)
            )
            client = await cur.fetchone()
            if not client:
                raise DriveExportError("client configuration not found")
            override = normalize_form_override(payload, client)
            form_ids = sorted(set(client.get("form_ids") or []))
            if form_ids and form_id not in form_ids:
                form_ids = sorted(set(form_ids) | {form_id})
            await cur.execute(
                """UPDATE drive_export_clients
                   SET form_ids=%s,
                       form_settings=COALESCE(form_settings,'{}'::jsonb)
                           || jsonb_build_object(%s::text,%s::jsonb),
                       updated_by=%s,updated_at=clock_timestamp()
                   WHERE slug=%s RETURNING *""",
                (form_ids, form_id, json.dumps(override), actor[:64], slug),
            )
            row = await cur.fetchone()
            await _audit(cur, actor, "DRIVE_EXPORT_CLIENT_FORM_SAVED", f"{slug}/{form_id}", {
                "fields": override["fields"],
                "statuses": override["statuses"],
                "include_images": override["include_images"],
            })
            return row

async def delete_client_form(pool, slug: str, form_id: str, actor: str) -> bool:
    """Atomically remove one form override without deleting the client selection."""
    if not pool:
        raise RuntimeError("drive export database unavailable")
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                """UPDATE drive_export_clients
                   SET form_settings=COALESCE(form_settings,'{}'::jsonb) - %s,
                       updated_by=%s,updated_at=clock_timestamp()
                   WHERE slug=%s AND COALESCE(form_settings,'{}'::jsonb) ? %s
                   RETURNING slug""",
                (form_id, actor[:64], slug, form_id),
            )
            row = await cur.fetchone()
            if not row:
                return False
            await _audit(cur, actor, "DRIVE_EXPORT_CLIENT_FORM_RESET", f"{slug}/{form_id}", {})
            return True

async def delete_client(pool, slug: str, actor: str) -> bool:
    if not pool:
        raise RuntimeError("drive export database unavailable")
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute("DELETE FROM drive_export_clients WHERE slug=%s RETURNING slug", (slug,))
            row = await cur.fetchone()
            if not row:
                return False
            await _audit(cur, actor, "DRIVE_EXPORT_CLIENT_DELETED", slug, {})
            return True


async def select_rows(pool, client: dict[str, Any]) -> list[dict[str, Any]]:
    if not pool:
        raise RuntimeError("drive export database unavailable")
    query = (
        "SELECT submission_id,form_id,status,created_at,vehicle_type,car_model,membership_type,"
        "ctv_code,driver_name,driver_phone,total_gross_revenue,total_net_income,total_trips,"
        "rejection_reason,form_data,media_manifest FROM survey_submissions WHERE status = ANY(%s)"
    )
    params: list[Any] = [list(client["statuses"])]
    if client.get("form_ids"):
        query += " AND form_id = ANY(%s)"
        params.append(list(client["form_ids"]))
    if client.get("vehicle_types"):
        query += " AND vehicle_type = ANY(%s)"
        params.append(list(client["vehicle_types"]))
    if client.get("date_from"):
        query += " AND created_at >= %s"
        params.append(client["date_from"])
    if client.get("date_to"):
        query += " AND created_at < %s::date + interval '1 day'"
        params.append(client["date_to"])
    query += " ORDER BY created_at, submission_id"
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()
    return [dict(row) for row in rows if not is_test_submission(row.get("submission_id"))]


def _image_payloads(submission_id: str, manifest: Any) -> list[dict[str, Any]]:
    entries = []
    for path in manifest_paths(submission_id, manifest):
        if os.path.splitext(path)[1].lower() not in IMAGE_EXTENSIONS:
            continue
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            logger.warning("Could not read export image %s: %s", path, exc)
            continue
        normalized = normalize_image_bytes(raw)
        payload = normalized if normalized is not None else raw
        extension = ".webp" if normalized is not None else os.path.splitext(path)[1].lower()
        stem = os.path.splitext(os.path.basename(path))[0] or "image"
        digest = hashlib.sha256(payload).hexdigest()[:10]
        name = f"{stem}_{digest}{extension}"
        entries.append({
            "name": name,
            "payload": payload,
            "mime": detect_image_mime(payload) or "application/octet-stream",
        })
    return entries


async def _ensure_master_image(
    client: Any, http_client: Any, token: str, folder_id: str, image: dict[str, Any], report: dict[str, Any],
) -> str | None:
    existing = await client.find_file_or_folder(http_client, token, image["name"], folder_id)
    if existing:
        report["images_present"] += 1
        return existing
    file_id = await client.upload_file(
        http_client, token, image["name"], image["payload"], image["mime"], folder_id
    )
    if file_id:
        report["images_uploaded"] += 1
    else:
        report["errors"].append(f"image upload failed: {image['name']}")
    return file_id


async def _sync_sheet(
    client: Any, http_client: Any, token: str, name: str, folder_id: str,
    rows: list[dict[str, Any]], fields: list[str], report: dict[str, Any],
) -> str | None:
    existing = await client.find_file_or_folder(
        http_client, token, name, folder_id, SHEET_MIME
    )
    matrix = export_matrix(rows, fields)
    sheet_id = await ensure_workbook(
        client, http_client, token, name, folder_id, [(name, matrix)]
    )
    if sheet_id:
        report["sheets_updated" if existing else "sheets_created"] += 1
        return sheet_id
    report["errors"].append(f"sheet create failed: {name}")
    return None


async def _sheets_request(http, token: str, method: str, path: str, **kwargs):
    return await http.request(
        method,
        f"https://sheets.googleapis.com/v4/spreadsheets{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        **kwargs,
    )


def _json(response) -> dict[str, Any]:
    try:
        value = response.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


async def _spreadsheet_meta(http, token: str, spreadsheet_id: str) -> dict[str, Any]:
    response = await _sheets_request(
        http, token, "GET", f"/{spreadsheet_id}",
        params={"fields": "sheets(properties(sheetId,title,gridProperties),bandedRanges)"},
    )
    return response.json() if response.status_code == 200 else {}


async def _ensure_spreadsheet(drive: Any, http, token: str, title: str, parent_id: str) -> str | None:
    existing = await drive.find_file_or_folder(http, token, title, parent_id, SHEET_MIME)
    if existing:
        return existing
    response = await http.post(
        "https://www.googleapis.com/drive/v3/files",
        headers={"Authorization": f"Bearer {token}"},
        params={"fields": "id,name,mimeType,parents"},
        json={"name": title, "mimeType": SHEET_MIME, "parents": [parent_id]},
    )
    if response.status_code not in {200, 201}:
        logger.warning("Could not create spreadsheet %s: %s", title, response.status_code)
        return None
    return _json(response).get("id")


async def _ensure_tab(http, token: str, spreadsheet_id: str, title: str, meta: dict[str, Any]) -> int | None:
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties") or {}
        if props.get("title") == title:
            return props.get("sheetId")
    response = await _sheets_request(
        http, token, "POST", f"/{spreadsheet_id}:batchUpdate",
        json={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    )
    if response.status_code == 200:
        replies = _json(response).get("replies") or []
        return ((replies[0] if replies else {}).get("addSheet") or {}).get("properties", {}).get("sheetId")
    logger.warning("Could not add sheet tab %s: %s", title, response.status_code)
    return None


async def _write_tab(http, token: str, spreadsheet_id: str, title: str, rows: list[list[Any]]) -> bool:
    encoded = quote(f"{title}!A1:ZZZ", safe="")
    await _sheets_request(http, token, "POST", f"/{spreadsheet_id}/values/{encoded}:clear")
    encoded_start = quote(f"{title}!A1", safe="")
    response = await _sheets_request(
        http, token, "PUT", f"/{spreadsheet_id}/values/{encoded_start}",
        params={"valueInputOption": "RAW"}, json={"values": rows},
    )
    return response.status_code == 200


async def _format_tab(http, token: str, spreadsheet_id: str, title: str, row_count: int, column_count: int) -> None:
    meta = await _spreadsheet_meta(http, token, spreadsheet_id)
    sheet = next(
        (item for item in meta.get("sheets", []) if (item.get("properties") or {}).get("title") == title),
        None,
    )
    if not sheet:
        return
    sheet_id = (sheet.get("properties") or {}).get("sheetId")
    if sheet_id is None:
        return
    grid = (sheet.get("properties") or {}).get("gridProperties") or {}
    requests: list[dict[str, Any]] = []
    for band in sheet.get("bandedRanges") or []:
        if band.get("bandedRangeId") is not None:
            requests.append({"deleteBanding": {"bandedRangeId": band["bandedRangeId"]}})
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {
                    "rowCount": max(int(grid.get("rowCount") or 0), row_count + 20),
                    "columnCount": max(int(grid.get("columnCount") or 0), column_count),
                    "frozenRowCount": 1,
                },
            },
            "fields": "gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount",
        }
    })
    if row_count > 1:
        requests.append({
            "addBanding": {
                "bandedRange": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": row_count,
                        "startColumnIndex": 0,
                        "endColumnIndex": max(column_count, 1),
                    },
                    "rowProperties": {
                        "firstBandColor": {"red": 0.94, "green": 0.96, "blue": 0.99},
                        "secondBandColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                    },
                }
            }
        })
    await _sheets_request(http, token, "POST", f"/{spreadsheet_id}:batchUpdate", json={"requests": requests})


async def ensure_workbook(
    drive: Any, http, token: str, title: str, parent_id: str,
    tabs: list[tuple[str, list[list[Any]]]],
) -> str | None:
    spreadsheet_id = await _ensure_spreadsheet(drive, http, token, title, parent_id)
    if not spreadsheet_id or not tabs:
        return spreadsheet_id
    meta = await _spreadsheet_meta(http, token, spreadsheet_id)
    for tab_title, rows in tabs:
        if not await _ensure_tab(http, token, spreadsheet_id, tab_title, meta):
            continue
        await _write_tab(http, token, spreadsheet_id, tab_title, rows)
        await _format_tab(http, token, spreadsheet_id, tab_title, len(rows), len(rows[0]) if rows else 1)
        meta = await _spreadsheet_meta(http, token, spreadsheet_id)
    desired = {tab_title for tab_title, _rows in tabs}
    requests = []
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties") or {}
        if props.get("title") not in desired and props.get("sheetId") is not None:
            requests.append({"deleteSheet": {"sheetId": props["sheetId"]}})
    if requests:
        await _sheets_request(http, token, "POST", f"/{spreadsheet_id}:batchUpdate", json={"requests": requests})
    return spreadsheet_id


async def _internal_form_ids(pool, drive: Any, http, token: str, internal_id: str) -> list[str]:
    forms: set[str] = set()
    if pool:
        try:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT DISTINCT form_id FROM survey_submissions")
                    forms.update(str(row[0]) for row in await cur.fetchall() if row[0])
        except Exception as exc:
            logger.warning("Could not list internal forms: %s", type(exc).__name__)
    for child in await drive.list_children(http, token, internal_id):
        if child.get("mimeType") == "application/vnd.google-apps.folder":
            name = str(child.get("name") or "")
            if name and name not in {"submissions", "_legacy", "_legacy_unknown"}:
                forms.add(name)
    return sorted(forms)


async def sync_internal_sheets(
    pool, drive: Any, http, token: str, root_id: str,
    form_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Create/update internal per-form sheets and the master workbook tabs."""
    settings = await get_settings(pool) if pool else dict(DEFAULT_SETTINGS)
    internal_id = await drive.get_or_create_folder(
        http, token, settings["internal_folder_name"], root_id
    )
    if not internal_id:
        return {"status": "folder_error"}
    forms = sorted(set(form_ids or await _internal_form_ids(pool, drive, http, token, internal_id)))
    tabs: list[tuple[str, list[list[Any]]]] = []
    created: list[str] = []
    for form_id in forms:
        rows: list[dict[str, Any]] = []
        if pool:
            try:
                rows = await select_rows(
                    pool, {"statuses": list(EXPORT_STATUSES), "form_ids": [form_id]}
                )
            except Exception as exc:
                logger.warning("Internal sheet query failed for %s: %s", form_id, type(exc).__name__)
        folder = await drive.get_or_create_folder(http, token, form_id, internal_id)
        if not folder:
            continue
        sheet_id = await ensure_workbook(
            drive, http, token, form_id, folder,
            [(form_id, export_matrix(rows, list(FIELD_KEYS)))],
        )
        if sheet_id:
            created.append(sheet_id)
        tabs.append((form_id, export_matrix(rows, list(FIELD_KEYS))))
    master_id = None
    if tabs:
        master_id = await ensure_workbook(
            drive, http, token, MASTER_DELIVERABLES_NAME, internal_id, tabs
        )
    return {"status": "ok", "forms": forms, "form_sheets": created, "master_id": master_id}


async def refresh_ctv_sheet(pool, drive: Any, http, token: str, internal_id: str) -> str | None:
    sheet_id = await drive.find_file_or_folder(http, token, "CTV_Info", internal_id, SHEET_MIME)
    if not sheet_id:
        return None
    response = await _sheets_request(
        http, token, "GET", f"/{sheet_id}/values/A1:E200",
        params={"valueRenderOption": "FORMULA"},
    )
    values = _json(response).get("values") or []
    if not values:
        return sheet_id
    form_title = str((values[0] or [""])[0] or "CTV_Info")
    entries: list[tuple[str, str, str]] = []
    for row in values[2:]:
        row = list(row or [])
        first = str(row[0] or "") if row else ""
        if first.startswith("=IMAGE(") or first.startswith("http"):
            qr = first
            url = str(row[1] or "") if len(row) > 1 else ""
            code = str(row[3] or "") if len(row) > 3 else ""
        else:
            code = first
            url = str(row[3] or "") if len(row) > 3 else ""
            qr = str(row[2] or "") if len(row) > 2 else ""
        if qr.startswith('=IMAGE("') and qr.endswith('")'):
            qr = qr[8:-2]
        if not code and "ref=" in url:
            code = url.split("ref=", 1)[1].split("&", 1)[0]
        if qr and code:
            entries.append((code, url, qr))
    phones: dict[str, str] = {}
    if pool and entries:
        try:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT code,phone FROM ctv_directory WHERE code = ANY(%s)",
                        ([code for code, _url, _qr in entries],),
                    )
                    phones = {str(code): str(phone or "") for code, phone in await cur.fetchall()}
        except Exception as exc:
            logger.warning("Could not read CTV phones: %s", type(exc).__name__)
    rows: list[list[Any]] = [[form_title, "", "", ""], ["CTV Code", "Phone", "QR", "URL"]]
    rows.extend([
        [code, phones.get(code, ""), f'=IMAGE("{qr}")', url]
        for code, url, qr in entries
    ])
    encoded = quote("A1:E200", safe="")
    await _sheets_request(http, token, "POST", f"/{sheet_id}/values/{encoded}:clear")
    await _sheets_request(
        http, token, "PUT", f"/{sheet_id}/values/A1",
        params={"valueInputOption": "USER_ENTERED"}, json={"values": rows},
    )
    meta = await _spreadsheet_meta(http, token, sheet_id)
    sheet_props = (meta.get("sheets") or [{}])[0].get("properties") or {}
    tab_id = sheet_props.get("sheetId")
    requests: list[dict[str, Any]] = []
    if entries and tab_id is not None:
        requests.extend([
            {
                "updateCells": {
                    "range": {
                        "sheetId": tab_id,
                        "startRowIndex": 2,
                        "endRowIndex": 2 + len(entries),
                        "startColumnIndex": 2,
                        "endColumnIndex": 3,
                    },
                    "rows": [
                        {"values": [{"note": qr}]} for _code, _url, qr in entries
                    ],
                    "fields": "note",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": tab_id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
                    "properties": {"pixelSize": 170}, "fields": "pixelSize",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": tab_id, "dimension": "ROWS",
                        "startIndex": 2, "endIndex": 2 + len(entries),
                    },
                    "properties": {"pixelSize": 170}, "fields": "pixelSize",
                }
            },
        ])
    if requests:
        await _sheets_request(http, token, "POST", f"/{sheet_id}:batchUpdate", json={"requests": requests})
    await _format_tab(http, token, sheet_id, sheet_props.get("title") or "CTV_Info", len(rows), 4)
    return sheet_id


async def _record_export_ledger(pool, slug: str, form_id: str, entries: list[dict[str, Any]]) -> None:
    if not entries:
        return
    entity_id = f"{slug}/{form_id}"[:64]
    try:
        await record_drive_ledger(
            pool, "export", entity_id, entries, "export",
            action="DRIVE_ACTIVE", state="ACTIVE",
        )
    except Exception as exc:
        logger.warning("Drive ledger write failed for %s/%s: %s", slug, form_id, type(exc).__name__)


async def run_drive_exports(
    pool,
    *,
    client: Any = None,
    http_client: Any = None,
    token: str | None = None,
    root_folder_id: str | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Manually execute configured exports. Synchronous by design for an admin action."""
    if not pool:
        return {"status": "unavailable", "errors": ["database unavailable"]}
    report: dict[str, Any] = {
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "clients": 0,
        "forms": 0,
        "rows": 0,
        "sheets_created": 0,
        "sheets_updated": 0,
        "images_uploaded": 0,
        "images_present": 0,
        "images_pruned": 0,
        "submission_folders_pruned": 0,
        "drive_items_cleaned": 0,
        "archived_forms_skipped": 0,
        "shortcuts_created": 0,
        "shortcuts_present": 0,
        "errors": [],
    }
    def emit() -> None:
        if progress is None:
            return
        try:
            progress({**report, "errors": list(report["errors"])})
        except Exception:
            pass

    lock_conn = None
    try:
        async with pool.connection() as lock_conn:
            lock_cur = await lock_conn.execute(
                "SELECT pg_try_advisory_lock(%s);", (DRIVE_EXPORT_LOCK_KEY,)
            )
            lock_row = await lock_cur.fetchone()
            if not lock_row or not lock_row[0]:
                report["status"] = "busy"
                return report
            settings = await get_settings(pool)
            clients = [row for row in await list_clients(pool) if row["enabled"]]
            own_http = http_client is None
            drive = client or GoogleDriveClient()
            http = http_client or httpx.AsyncClient(timeout=60.0)
            try:
                access_token = token or await drive.get_token(http)
                if not access_token:
                    report["status"] = "no_credentials"
                    report["errors"].append("Google Drive credentials unavailable")
                    return report
                root = root_folder_id or drive.root_folder_id
                if not root:
                    report["status"] = "no_root"
                    report["errors"].append("GOOGLE_DRIVE_ROOT_FOLDER_ID is not configured")
                    return report
                client_root = await drive.get_or_create_folder(http, access_token, settings["client_folder_name"], root)
                internal_root = await drive.get_or_create_folder(http, access_token, settings["internal_folder_name"], root)
                images_root = await drive.get_or_create_folder(http, access_token, settings["images_folder_name"], root)
                if not all((client_root, internal_root, images_root)):
                    report["status"] = "folder_error"
                    report["errors"].append("could not create the clients/internal/master images folder tree")
                    return report
                archived_forms: set[str] = set()
                active_forms: set[str] = set()
                try:
                    async with pool.connection() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute("SELECT DISTINCT form_id FROM deleted_forms")
                            archived_ids = {str(row[0]) for row in await cur.fetchall()}
                    from core.state import FORM_REGISTRY, refresh_form_registry_from_disk
                    try:
                        refresh_form_registry_from_disk()
                    except Exception:
                        pass
                    active_forms = set(FORM_REGISTRY)
                    archived_forms = archived_ids - active_forms
                except Exception:
                    # Older databases may not have the archive table yet.
                    archived_forms = set()
                for archived_id in sorted(archived_forms):
                    try:
                        trash_id = await ensure_form_trash(pool, archived_id, "export-maintenance")
                        count, ledger = await trash_form_folders(
                            drive, http, access_token, root, settings, archived_id
                        )
                        report["drive_items_cleaned"] += count
                        if ledger:
                            await record_drive_ledger(
                                pool, "form", archived_id, ledger, "export-maintenance",
                                trash_id=trash_id, action="DRIVE_TRASHED",
                            )
                    except Exception as exc:
                        report["errors"].append(
                            f"archived form cleanup failed for {archived_id} ({type(exc).__name__})"
                        )
                master_images_cache: dict[tuple[str, str, str], str] = {}
                for config in clients:
                    report["clients"] += 1
                    emit()
                    overrides = config.get("form_settings") or {}
                    selected_forms = set(config.get("form_ids") or [])
                    target_forms = selected_forms or (set(overrides) | active_forms)
                    by_form: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
                    if target_forms:
                        for form_id in sorted(target_forms):
                            effective = effective_form_config(config, form_id)
                            try:
                                form_rows = await select_rows(
                                    pool,
                                    {**config, **effective, "form_ids": [form_id]},
                                )
                            except Exception as exc:
                                report["errors"].append(
                                    f"{config['slug']}/{form_id}: query failed ({type(exc).__name__})"
                                )
                                continue
                            if form_rows:
                                by_form[form_id] = (form_rows, effective)
                    else:
                        try:
                            rows = await select_rows(pool, config)
                        except Exception as exc:
                            report["errors"].append(f"{config['slug']}: query failed ({type(exc).__name__})")
                            continue
                        for row in rows:
                            form_id = str(row.get("form_id") or "unknown_form")
                            by_form.setdefault(form_id, ([], effective_form_config(config, form_id)))[0].append(row)
                    if archived_forms:
                        skipped = [form_id for form_id in by_form if form_id in archived_forms]
                        for form_id in skipped:
                            by_form.pop(form_id, None)
                        report["archived_forms_skipped"] += len(skipped)
                    if not by_form:
                        continue
                    tier3_root = internal_root if config["kind"] == "internal" else client_root
                    tier3_id = await drive.get_or_create_folder(http, access_token, config["folder_name"], tier3_root)
                    if not tier3_id:
                        report["errors"].append(f"{config['slug']}: could not create export folder")
                        continue
                    for form_id, (form_rows, effective) in sorted(by_form.items()):
                        report["forms"] += 1
                        report["rows"] += len(form_rows)
                        emit()
                        folder_mime = "application/vnd.google-apps.folder"
                        form_folder = await drive.get_or_create_folder(http, access_token, form_id, tier3_id)
                        if not form_folder:
                            report["errors"].append(f"{config['slug']}/{form_id}: could not create form folder")
                            continue
                        images_folder = await drive.get_or_create_folder(http, access_token, "Images", form_folder)
                        ledger: list[dict[str, Any]] = [{
                            "id": form_folder, "name": form_id, "parent_id": tier3_id,
                            "parent_name": config["folder_name"], "mime_type": folder_mime,
                        }]
                        if images_folder:
                            ledger.append({
                                "id": images_folder, "name": "Images", "parent_id": form_folder,
                                "parent_name": form_id, "mime_type": folder_mime,
                            })
                        sheet_id = await _sync_sheet(
                            drive, http, access_token, form_id, form_folder,
                            form_rows, list(effective["fields"]), report,
                        )
                        if sheet_id:
                            ledger.append({
                                "id": sheet_id, "name": form_id, "parent_id": form_folder,
                                "parent_name": form_id, "mime_type": "application/vnd.google-apps.spreadsheet",
                            })
                        if not images_folder:
                            await _record_export_ledger(pool, config["slug"], form_id, ledger)
                            continue
                        if not effective["include_images"]:
                            try:
                                report["submission_folders_pruned"] += await clear_submission_folders(
                                    drive, http, access_token, images_folder
                                )
                            except Exception as exc:
                                report["errors"].append(
                                    f"{config['slug']}/{form_id}: image-disable cleanup failed ({type(exc).__name__})"
                                )
                            await _record_export_ledger(pool, config["slug"], form_id, ledger)
                            continue
                        master_form = await drive.get_or_create_folder(http, access_token, form_id, images_root)
                        if master_form:
                            ledger.append({
                                "id": master_form, "name": form_id, "parent_id": images_root,
                                "parent_name": settings["images_folder_name"], "mime_type": folder_mime,
                            })
                        for row in form_rows:
                            sid = str(row["submission_id"])
                            master_sub = await drive.get_or_create_folder(http, access_token, sid, master_form) if master_form else None
                            client_sub = await drive.get_or_create_folder(http, access_token, sid, images_folder)
                            if not master_sub or not client_sub:
                                report["errors"].append(f"{config['slug']}/{form_id}/{sid}: could not create image folders")
                                continue
                            ledger.extend([
                                {"id": master_sub, "name": sid, "parent_id": master_form,
                                 "parent_name": form_id, "mime_type": folder_mime},
                                {"id": client_sub, "name": sid, "parent_id": images_folder,
                                 "parent_name": "Images", "mime_type": folder_mime},
                            ])
                            images = _image_payloads(sid, row.get("media_manifest"))
                            expected_names = [image["name"] for image in images]
                            for image in images:
                                cache_key = (form_id, sid, image["name"])
                                master_file_id = master_images_cache.get(cache_key)
                                if not master_file_id:
                                    master_file_id = await _ensure_master_image(
                                        drive, http, access_token, master_sub, image, report
                                    )
                                    if master_file_id:
                                        master_images_cache[cache_key] = master_file_id
                                if not master_file_id:
                                    continue
                                ledger.append({
                                    "id": master_file_id, "name": image["name"], "parent_id": master_sub,
                                    "parent_name": sid, "mime_type": image["mime"],
                                })
                                existing = await drive.find_file_or_folder(
                                    http, access_token, image["name"], client_sub
                                )
                                if existing:
                                    report["shortcuts_present"] += 1
                                    ledger.append({
                                        "id": existing, "name": image["name"], "parent_id": client_sub,
                                        "parent_name": sid, "mime_type": "application/vnd.google-apps.shortcut",
                                        "target_id": master_file_id,
                                    })
                                    continue
                                shortcut = await drive.create_shortcut(
                                    http, access_token, image["name"], master_file_id, client_sub
                                )
                                if shortcut:
                                    report["shortcuts_created"] += 1
                                    ledger.append({
                                        "id": shortcut, "name": image["name"], "parent_id": client_sub,
                                        "parent_name": sid, "mime_type": "application/vnd.google-apps.shortcut",
                                        "target_id": master_file_id,
                                    })
                                else:
                                    report["errors"].append(f"{config['slug']}/{sid}: shortcut failed for {image['name']}")
                            if expected_names:
                                try:
                                    report["images_pruned"] += await prune_stale_files(
                                        drive, http, access_token, master_sub, expected_names
                                    )
                                    report["images_pruned"] += await prune_stale_files(
                                        drive, http, access_token, client_sub, expected_names
                                    )
                                except Exception as exc:
                                    report["errors"].append(
                                        f"{config['slug']}/{sid}: stale-image cleanup failed ({type(exc).__name__})"
                                    )
                        if images_folder:
                            try:
                                report["submission_folders_pruned"] += await prune_stale_submission_folders(
                                    drive,
                                    http,
                                    access_token,
                                    images_folder,
                                    {str(row["submission_id"]) for row in form_rows},
                                )
                            except Exception as exc:
                                report["errors"].append(
                                    f"{config['slug']}/{form_id}: stale-submission cleanup failed ({type(exc).__name__})"
                                )
                        await _record_export_ledger(pool, config["slug"], form_id, ledger)
                try:
                    sheet_report = await sync_internal_sheets(
                        pool, drive, http, access_token, root
                    )
                    report["internal_sheets"] = sheet_report
                    from services.ctv_matrix import sync_internal_ctv_matrix

                    await sync_internal_ctv_matrix(
                        drive, http, access_token, root, pool
                    )
                except Exception as exc:
                    report["errors"].append(
                        f"internal sheet sync failed ({type(exc).__name__})"
                    )
            finally:
                if own_http:
                    await http.aclose()
            report["status"] = "ok" if not report["errors"] else "completed_with_errors"
            return report
    except Exception as exc:
        logger.error("Drive export failed: %s", type(exc).__name__)
        report["status"] = "failed"
        report["errors"].append(type(exc).__name__)
        return report
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        emit()
        try:
            if report.get("status") != "busy":
                async with pool.connection() as conn:
                    await conn.execute(
                        "UPDATE drive_export_settings SET last_run_at=clock_timestamp(),last_report=%s::jsonb "
                        "WHERE singleton=TRUE",
                        (json.dumps(report, default=str),),
                    )
                    await conn.commit()
        except Exception as exc:
            logger.warning("Could not persist drive export report: %s", type(exc).__name__)
