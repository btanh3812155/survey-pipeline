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
from typing import Any, Iterable

import httpx
from psycopg.rows import dict_row

from services.google_drive_delivery import (
    GoogleDriveClient,
    detect_image_mime,
    is_test_submission,
    normalize_image_bytes,
)
from services.drive_cleanup import (
    prune_stale_files,
    prune_stale_submission_folders,
    purge_form_folders,
)
from services.submission_media import manifest_paths

logger = logging.getLogger("core.drive_exports")

EXPORT_FIELDS = (
    ("submission_id", "Submission ID"),
    ("form_id", "Form"),
    ("status", "Status"),
    ("created_at", "Submitted at"),
    ("vehicle_type", "Vehicle type"),
    ("car_model", "Car model"),
    ("membership_type", "Membership"),
    ("ctv_code", "CTV code"),
    ("driver_name", "Driver name"),
    ("driver_phone", "Driver phone"),
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
    "client_folder_name": "Client Exports",
    "internal_folder_name": "Internal Exports",
    "images_folder_name": "Images Master",
}
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


def export_matrix(rows: Iterable[dict[str, Any]], fields: Iterable[str]) -> list[list[Any]]:
    selected = _selected_fields(fields)
    labels = dict(EXPORT_FIELDS)
    matrix = [[labels[key] for key in selected]]
    matrix.extend([[_csv_value(row.get(key)) for key in selected] for row in rows])
    return matrix


def _clean_list(values: Any, allowed: Iterable[str]) -> list[str]:
    allowed_set = set(allowed)
    return [value for value in values if value in allowed_set]


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
    client = normalize_client(payload)
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """INSERT INTO drive_export_clients
                   (slug,display_name,kind,category,folder_name,enabled,date_from,date_to,
                    form_ids,fields,statuses,vehicle_types,include_images,updated_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(slug) DO UPDATE SET
                     display_name=EXCLUDED.display_name,kind=EXCLUDED.kind,category=EXCLUDED.category,
                     folder_name=EXCLUDED.folder_name,enabled=EXCLUDED.enabled,
                     date_from=EXCLUDED.date_from,date_to=EXCLUDED.date_to,form_ids=EXCLUDED.form_ids,
                     fields=EXCLUDED.fields,statuses=EXCLUDED.statuses,vehicle_types=EXCLUDED.vehicle_types,
                     include_images=EXCLUDED.include_images,updated_by=EXCLUDED.updated_by,
                     updated_at=clock_timestamp()
                   RETURNING *""",
                (client["slug"], client["display_name"], client["kind"], client["category"],
                 client["folder_name"], client["enabled"], client["date_from"], client["date_to"],
                 client["form_ids"], client["fields"], client["statuses"], client["vehicle_types"],
                 client["include_images"], actor[:64]),
            )
            row = await cur.fetchone()
            await _audit(cur, actor, "DRIVE_EXPORT_CLIENT_SAVED", client["slug"], {
                "kind": client["kind"], "enabled": client["enabled"],
            })
            return row


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
) -> None:
    sheet_id = await client.find_file_or_folder(
        http_client, token, name, folder_id, "application/vnd.google-apps.spreadsheet"
    )
    if sheet_id:
        if await client.update_sheet_values(http_client, token, sheet_id, export_matrix(rows, fields)):
            report["sheets_updated"] += 1
        else:
            report["errors"].append(f"sheet update failed: {name}")
        return
    created = await client.import_csv_as_sheet(
        http_client, token, name, build_export_csv(rows, fields), folder_id
    )
    if created:
        report["sheets_created"] += 1
    else:
        report["errors"].append(f"sheet create failed: {name}")


async def run_drive_exports(
    pool,
    *,
    client: Any = None,
    http_client: Any = None,
    token: str | None = None,
    root_folder_id: str | None = None,
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
                master_id = await drive.get_or_create_folder(http, access_token, settings["master_folder_name"], root)
                client_root = await drive.get_or_create_folder(http, access_token, settings["client_folder_name"], master_id)
                internal_root = await drive.get_or_create_folder(http, access_token, settings["internal_folder_name"], master_id)
                images_root = await drive.get_or_create_folder(http, access_token, settings["images_folder_name"], master_id)
                if not all((master_id, client_root, internal_root, images_root)):
                    report["status"] = "folder_error"
                    report["errors"].append("could not create the master export folder tree")
                    return report
                archived_forms: set[str] = set()
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
                        report["drive_items_cleaned"] += await purge_form_folders(
                            drive, http, access_token, root, settings, archived_id
                        )
                    except Exception as exc:
                        report["errors"].append(
                            f"archived form cleanup failed for {archived_id} ({type(exc).__name__})"
                        )
                master_images_cache: dict[tuple[str, str, str], str] = {}
                for config in clients:
                    report["clients"] += 1
                    try:
                        rows = await select_rows(pool, config)
                    except Exception as exc:
                        report["errors"].append(f"{config['slug']}: query failed ({type(exc).__name__})")
                        continue
                    if not rows:
                        continue
                    by_form: dict[str, list[dict[str, Any]]] = {}
                    for row in rows:
                        by_form.setdefault(str(row.get("form_id") or "unknown_form"), []).append(row)
                    if archived_forms:
                        skipped = [form_id for form_id in by_form if form_id in archived_forms]
                        for form_id in skipped:
                            by_form.pop(form_id, None)
                        report["archived_forms_skipped"] += len(skipped)
                    tier3_root = internal_root if config["kind"] == "internal" else client_root
                    tier3_id = await drive.get_or_create_folder(http, access_token, config["folder_name"], tier3_root)
                    if not tier3_id:
                        report["errors"].append(f"{config['slug']}: could not create export folder")
                        continue
                    for form_id, form_rows in sorted(by_form.items()):
                        report["forms"] += 1
                        report["rows"] += len(form_rows)
                        form_folder = await drive.get_or_create_folder(http, access_token, form_id, tier3_id)
                        images_folder = await drive.get_or_create_folder(http, access_token, "Images", form_folder) if form_folder else None
                        if not form_folder:
                            report["errors"].append(f"{config['slug']}/{form_id}: could not create form folder")
                            continue
                        await _sync_sheet(
                            drive, http, access_token, form_id, form_folder,
                            form_rows, list(config["fields"]), report,
                        )
                        if not config["include_images"] or not images_folder:
                            continue
                        for row in form_rows:
                            sid = str(row["submission_id"])
                            master_form = await drive.get_or_create_folder(http, access_token, form_id, images_root)
                            master_sub = await drive.get_or_create_folder(http, access_token, sid, master_form) if master_form else None
                            client_sub = await drive.get_or_create_folder(http, access_token, sid, images_folder)
                            if not master_sub or not client_sub:
                                report["errors"].append(f"{config['slug']}/{form_id}/{sid}: could not create image folders")
                                continue
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
                                existing = await drive.find_file_or_folder(
                                    http, access_token, image["name"], client_sub
                                )
                                if existing:
                                    report["shortcuts_present"] += 1
                                    continue
                                shortcut = await drive.create_shortcut(
                                    http, access_token, image["name"], master_file_id, client_sub
                                )
                                if shortcut:
                                    report["shortcuts_created"] += 1
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
                        if config["include_images"] and images_folder:
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
