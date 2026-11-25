"""Durable, private-media exports for the Wave 4A outbox."""

from contextlib import asynccontextmanager
import asyncio
import csv
import hashlib
import io
import json
import re
from typing import Any

import httpx

from config import settings
from services.outbox_types import Outcome
from services.submission_media import safe_media_path
from services.google_drive_delivery import DRIVE_SYNC_LOCK_KEY
from services.google_drive_delivery import CSV_HEADERS, extract_survey_row_data

_HASH = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_FIELDS = (
    "submission_id", "decision_version", "media_version", "form_id", "status", "submitted_at", "created_at", "vehicle_type", "vehicle_model",
    "car_model", "gsm_duration", "gsm_contract_type", "daily_trips", "monthly_income_range",
    "gsm_incentive_policy", "membership_type", "income_week_1_gross", "income_week_1_net",
    "income_week_1_trips", "income_week_2_gross", "income_week_2_net", "income_week_2_trips",
    "income_week_3_gross", "income_week_3_net", "income_week_3_trips", "income_week_4_gross",
    "income_week_4_net", "income_week_4_trips", "income_week_5_gross", "income_week_5_net",
    "income_week_5_trips", "total_gross_revenue", "total_net_income", "total_trips",
)


def _outcome(kind: str, reason: str = "", receipt: dict | None = None) -> Outcome:
    return Outcome(kind, reason, receipt or {})


def _media(event: dict, submission: dict | None) -> tuple[str, list[dict]]:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("MALFORMED_PAYLOAD")
    sid = str(event.get("aggregate_id") or (submission or {}).get("submission_id") or "")
    if not sid or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid):
        raise ValueError("MISSING_SUBMISSION")
    manifest = payload.get("media_manifest")
    if not isinstance(manifest, list) or (not manifest and event.get("flow_target") == "R2_UPLOAD"):
        raise ValueError("MISSING_MANIFEST")
    version = payload.get("media_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("MISSING_MEDIA_VERSION")
    files = []
    seen = set()
    for entry in manifest:
        if not isinstance(entry, dict):
            raise ValueError("MALFORMED_MANIFEST")
        digest = entry.get("sha256")
        slot = entry.get("flat_index", entry.get("slot_index"))
        local_slot = entry.get("slot_index", slot)
        if type(local_slot) is not int or local_slot < 0:
            raise ValueError("MALFORMED_MANIFEST")
        if not isinstance(digest, str) or not _HASH.fullmatch(digest.lower()) or not isinstance(slot, int) or isinstance(slot, bool) or slot < 0 or slot in seen:
            raise ValueError("MALFORMED_MANIFEST")
        seen.add(slot)
        relative = entry.get("relative_path")
        if not isinstance(relative, str) or not relative or relative != relative.split("/")[-1] or relative != relative.split("\\")[-1] or relative in {".", ".."}:
            raise ValueError("MALFORMED_MANIFEST")
        try:
            path = safe_media_path(sid, relative)
            with open(path, "rb") as f:
                content = f.read()
        except (OSError, ValueError):
            raise ValueError("MEDIA_UNAVAILABLE") from None
        if hashlib.sha256(content).hexdigest() != digest.lower():
            raise ValueError("MEDIA_HASH_MISMATCH")
        entry_revision = entry.get("media_version", version)
        if not isinstance(entry_revision, int) or isinstance(entry_revision, bool) or entry_revision < 1 or entry_revision > version:
            raise ValueError("MALFORMED_MANIFEST")
        files.append({"slot": slot, "slot_index": entry.get("slot_index"), "flat_index": entry.get("flat_index", slot), "question_id": entry.get("question_id"), "revision": entry_revision, "sha256": digest.lower(), "mime": entry.get("mime") or "application/octet-stream", "content": content})
    return sid, sorted(files, key=lambda item: item["slot"])


async def _guard(guard) -> bool:
    return bool(await guard())


async def _execute_r2(event: dict, sid: str, files: list[dict], guard) -> Outcome:
    dest = event["payload"].get("destination")
    bucket = dest.get("bucket") if isinstance(dest, dict) else None
    private_bucket = getattr(settings, "R2_PRIVATE_BUCKET_NAME", None)
    if not private_bucket:
        return _outcome("POLICY_SKIPPED", "R2_PRIVATE_DESTINATION_UNCONFIGURED")
    if private_bucket == settings.R2_BUCKET_NAME:
        return _outcome("TERMINAL", "R2_PUBLIC_BUCKET_FORBIDDEN")
    if not isinstance(bucket, str) or not bucket.strip() or dest.get("private") is not True:
        return _outcome("TERMINAL", "R2_DESTINATION_REQUIRED")
    if bucket != private_bucket:
        return _outcome("TERMINAL", "R2_DESTINATION_MISMATCH")
    if not (settings.R2_ACCOUNT_ID and settings.R2_ACCESS_KEY_ID and settings.R2_SECRET_ACCESS_KEY):
        return _outcome("POLICY_SKIPPED", "R2_NOT_CONFIGURED")
    try:
        import aioboto3
        from botocore.config import Config
    except ImportError:
        return _outcome("RETRYABLE", "R2_CLIENT_UNAVAILABLE")
    endpoint = f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    session = aioboto3.Session()
    receipts = []
    try:
        async with session.client("s3", endpoint_url=endpoint, aws_access_key_id=settings.R2_ACCESS_KEY_ID,
                                  aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
                                  config=Config(connect_timeout=20, read_timeout=20, retries={"total_max_attempts": 1})) as s3:
            for item in files:
                revision = item["revision"]
                if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                    return _outcome("TERMINAL", "MALFORMED_MANIFEST")
                key = f"private/submissions/{sid}/{revision}/{item['sha256']}/slot-{item['slot']}"
                metadata = {"sha256": item["sha256"], "submission-id": sid, "media-version": str(revision), "slot": str(item["slot"])}
                if not await _guard(guard):
                    return _outcome("UNCERTAIN", "LEASE_LOST")
                try:
                    async with asyncio.timeout(25):
                        head = await s3.head_object(Bucket=bucket, Key=key)
                except Exception as exc:
                    if _not_found(exc):
                        head = None
                    else:
                        return _outcome("UNCERTAIN", "R2_LOOKUP_UNKNOWN")
                if head is None:
                    if not await _guard(guard):
                        return _outcome("UNCERTAIN", "LEASE_LOST")
                    try:
                        async with asyncio.timeout(25):
                            await s3.put_object(Bucket=bucket, Key=key, Body=item["content"], ContentType=item["mime"], Metadata=metadata)
                    except Exception:
                        return _outcome("UNCERTAIN", "R2_WRITE_UNKNOWN")
                    if not await _guard(guard):
                        return _outcome("UNCERTAIN", "LEASE_LOST")
                    try:
                        async with asyncio.timeout(25):
                            head = await s3.head_object(Bucket=bucket, Key=key)
                    except Exception:
                        return _outcome("UNCERTAIN", "R2_VERIFY_UNKNOWN")
                got = head.get("Metadata") or {}
                if head.get("ContentLength") != len(item["content"]) or any(got.get(k) != v for k, v in metadata.items()):
                    return _outcome("UNCERTAIN", "R2_OBJECT_MISMATCH")
                if not await _guard(guard):
                    return _outcome("UNCERTAIN", "LEASE_LOST")
                try:
                    async with asyncio.timeout(25):
                        remote = await s3.get_object(Bucket=bucket, Key=key)
                    if not await _guard(guard):
                        return _outcome("UNCERTAIN", "LEASE_LOST")
                    async with asyncio.timeout(25):
                        remote_bytes = await remote["Body"].read()
                except Exception:
                    return _outcome("UNCERTAIN", "R2_VERIFY_UNKNOWN")
                request_id = (head.get("ResponseMetadata") or {}).get("RequestId")
                if not head.get("ETag") or not request_id:
                    return _outcome("UNCERTAIN", "R2_RECEIPT_MISSING")
                if hashlib.sha256(remote_bytes).hexdigest() != item["sha256"]:
                    return _outcome("UNCERTAIN", "R2_OBJECT_MISMATCH")
                receipts.append({"slot": item["slot"], "key": key, "bucket": bucket, "sha256": item["sha256"], "revision": revision, "etag": head.get("ETag"), "request_id": (head.get("ResponseMetadata") or {}).get("RequestId")})
                await _progress(guard, {"objects": list(receipts)})
    except PermissionError:
        return _outcome("UNCERTAIN", "LEASE_LOST")
    except Exception:
        return _outcome("UNCERTAIN", "R2_OPERATION_UNKNOWN")
    return _outcome("SUCCEEDED", receipt={"provider": "r2", "objects": receipts})


def _not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    return error.get("Code") in {"404", "NoSuchKey", "NotFound"}



class _DriveRateLimited(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


async def _drive_request(client: httpx.AsyncClient, guard, method: str, url: str, **kwargs):
    if not await _guard(guard):
        raise PermissionError("LEASE_LOST")
    async with asyncio.timeout(25):
        response = await client.request(method, url, timeout=25.0, **kwargs)
    if response.status_code == 429:
        raw = response.headers.get("Retry-After")
        try:
            retry_after = max(0, int(raw)) if raw is not None else None
        except ValueError:
            retry_after = None
        raise _DriveRateLimited(retry_after)
    return response

async def _drive_folder(client, token, parent_id, name, guard):
    headers = {"Authorization": f"Bearer {token}"}
    mime = "application/vnd.google-apps.folder"
    query = f"'{parent_id}' in parents and name = '{name}' and mimeType = '{mime}' and trashed = false"
    response = await _drive_request(client, guard, "GET", "https://www.googleapis.com/drive/v3/files",
                                    params={"q": query, "fields": "nextPageToken,files(id,name,mimeType,parents)", "pageSize": 10}, headers=headers)
    if response.status_code != 200:
        raise RuntimeError("DRIVE_FOLDER_LOOKUP_UNKNOWN")
    result = _json(response)
    files = result.get("files")
    if result.get("nextPageToken") or not isinstance(files, list) or len(files) > 1:
        raise RuntimeError("DRIVE_FOLDER_AMBIGUOUS")
    if files:
        file_id = files[0].get("id")
        if not file_id or files[0].get("name") != name or files[0].get("mimeType") != mime or parent_id not in files[0].get("parents", []):
            raise RuntimeError("DRIVE_FOLDER_MISMATCH")
        return file_id, {"kind": "folder", "file_id": file_id, "parent_id": parent_id, "name": name}
    response = await _drive_request(client, guard, "POST", "https://www.googleapis.com/drive/v3/files",
                                    params={"fields": "id,name,mimeType,parents"},
                                    headers={**headers, "Content-Type": "application/json"},
                                    json={"name": name, "mimeType": mime, "parents": [parent_id]})
    if response.status_code not in {200, 201}:
        raise RuntimeError("DRIVE_FOLDER_CREATE_UNKNOWN")
    created = _json(response)
    if not created.get("id") or created.get("name") != name or created.get("mimeType") != mime or parent_id not in created.get("parents", []):
        raise RuntimeError("DRIVE_FOLDER_CREATE_UNKNOWN")
    return created["id"], {"kind": "folder", "file_id": created["id"], "parent_id": parent_id, "name": name}


async def _drive_token(client: httpx.AsyncClient, guard) -> str | None:
    oauth = (settings.GOOGLE_DRIVE_REFRESH_TOKEN, settings.GOOGLE_DRIVE_CLIENT_ID, settings.GOOGLE_DRIVE_CLIENT_SECRET)
    if not all(oauth):
        return settings.GOOGLE_DRIVE_ACCESS_TOKEN or None
    # A static access token is short-lived. Prefer the refresh token whenever
    # OAuth credentials are configured, otherwise an expired static token makes
    # every Drive export fail as an unknown operation.
    try:
        response = await _drive_request(client, guard, "POST", "https://oauth2.googleapis.com/token",
                                        data={"grant_type": "refresh_token", "refresh_token": oauth[0], "client_id": oauth[1], "client_secret": oauth[2]})
        response.raise_for_status()
        return response.json().get("access_token")
    except (PermissionError, _DriveRateLimited):
        raise
    except Exception:
        raise RuntimeError("DRIVE_AUTH_UNKNOWN") from None


def _json(response) -> dict:
    try:
        value = response.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


async def _drive_file(client, token, folder, sid, item, guard, known_id=None):
    revision, slot, digest = item["revision"], item["slot"], item["sha256"]
    kind = str(item.get("kind") or "media")
    name = item.get("filename") or f"{sid}-slot-{slot}"
    props = {"submission_id": sid, "media_version": str(revision), "slot": str(slot), "sha256": digest, "kind": kind}
    headers = {"Authorization": f"Bearer {token}"}
    found = None
    if known_id:
        response = await _drive_request(client, guard, "GET", f"https://www.googleapis.com/drive/v3/files/{known_id}",
                                        params={"fields": "id,name,appProperties,size,parents"}, headers=headers)
        if response.status_code != 200:
            raise RuntimeError("DRIVE_REFERENCE_UNKNOWN")
        found = _json(response)
        if found.get("id") != known_id or folder not in found.get("parents", []):
            raise RuntimeError("DRIVE_REFERENCE_MISMATCH")
    else:
        query = f"'{folder}' in parents and name = '{name}' and trashed = false"
        response = await _drive_request(client, guard, "GET", "https://www.googleapis.com/drive/v3/files",
                                        params={"q": query, "fields": "nextPageToken,files(id,name,appProperties,size,parents)", "pageSize": 10}, headers=headers)
        if response.status_code >= 500:
            raise RuntimeError("DRIVE_LOOKUP_UNKNOWN")
        if response.status_code != 200:
            return None, "DRIVE_LOOKUP_REJECTED"
        result = _json(response)
        matches = result.get("files")
        if result.get("nextPageToken") or not isinstance(matches, list):
            raise RuntimeError("DRIVE_LOOKUP_UNKNOWN")
        if len(matches) > 1:
            raise RuntimeError("DRIVE_IDENTITY_AMBIGUOUS")
        found = matches[0] if matches else None
    if found:
        old_props = found.get("appProperties") or {}
        if not found.get("id") or old_props.get("submission_id") != sid or str(old_props.get("slot")) != str(slot) or old_props.get("kind", "media") != kind:
            raise RuntimeError("DRIVE_REFERENCE_MISMATCH")
        file_id = found["id"]
        response = await _drive_request(client, guard, "PATCH", f"https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=media",
                                        headers={**headers, "Content-Type": item["mime"]}, content=item["content"])
        if response.status_code not in {200, 201}:
            raise RuntimeError("DRIVE_UPDATE_UNKNOWN")
        response = await _drive_request(client, guard, "PATCH", f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=id,appProperties",
                                        headers={**headers, "Content-Type": "application/json"}, json={"name": name, "appProperties": props})
        updated = _json(response)
        if response.status_code not in {200, 201} or updated.get("id") != file_id or updated.get("appProperties") != props:
            raise RuntimeError("DRIVE_UPDATE_UNKNOWN")
    else:
        metadata = {"name": name, "parents": [folder], "appProperties": props}
        boundary = "outbox4a" + digest[:24]
        crlf = bytes((13, 10))
        body = (b"--" + boundary.encode() + crlf + b"Content-Type: application/json; charset=UTF-8" + crlf + crlf +
                json.dumps(metadata).encode() + crlf + b"--" + boundary.encode() + crlf +
                f"Content-Type: {item['mime']}".encode() + crlf + crlf + item["content"] + crlf +
                b"--" + boundary.encode() + b"--" + crlf)
        response = await _drive_request(client, guard, "POST", "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,appProperties,size",
                                        headers={**headers, "Content-Type": f"multipart/related; boundary={boundary}"}, content=body)
        if response.status_code >= 500 or response.status_code in {408, 429}:
            raise RuntimeError("DRIVE_CREATE_UNKNOWN")
        if response.status_code not in {200, 201}:
            return None, "DRIVE_CREATE_REJECTED"
        created = _json(response)
        if not created.get("id") or created.get("appProperties") != props:
            raise RuntimeError("DRIVE_CREATE_UNKNOWN")
        file_id = created["id"]
    await _verify_drive_bytes(client, headers, file_id, item, guard)
    return file_id, None

async def _verify_drive_bytes(client, headers, file_id, item, guard):
    response = await _drive_request(client, guard, "GET", f"https://www.googleapis.com/drive/v3/files/{file_id}",
                                    params={"alt": "media"}, headers=headers)
    if response.status_code != 200:
        raise RuntimeError("DRIVE_VERIFY_UNKNOWN")
    if hashlib.sha256(response.content).hexdigest() != item["sha256"]:
        raise RuntimeError("DRIVE_REFERENCE_MISMATCH")


def _sanitize(record: dict) -> dict:
    return {key: record[key] for key in _DRIVE_FIELDS if key in record and isinstance(record[key], (str, int, float, bool, type(None)))}

def _record_item(record: dict, sid: str, revision: int) -> dict:
    content = json.dumps(_sanitize(record), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"slot": "record", "revision": revision, "sha256": hashlib.sha256(content).hexdigest(), "mime": "application/json", "content": content}

def _table_item(name: str, rows: list[list[Any]], revision: int = 1) -> dict:
    out = io.StringIO(newline="")
    csv.writer(out).writerows(rows)
    content = out.getvalue().encode("utf-8-sig")
    return {"slot": "table:" + name, "revision": revision, "filename": name, "kind": "csv", "sha256": hashlib.sha256(content).hexdigest(), "mime": "text/csv; charset=utf-8", "content": content}


async def _drive_sheet(client, token, folder, name, rows, digest, guard):
    headers = {"Authorization": f"Bearer {token}"}
    props = {"kind": "sheet", "sha256": digest}
    query = f"'{folder}' in parents and name = '{name}' and trashed = false"
    response = await _drive_request(client, guard, "GET", "https://www.googleapis.com/drive/v3/files",
                                    params={"q": query, "fields": "nextPageToken,files(id,name,appProperties,parents,mimeType)", "pageSize": 10}, headers=headers)
    if response.status_code != 200:
        raise RuntimeError("DRIVE_SHEET_LOOKUP_UNKNOWN")
    result = _json(response)
    files = result.get("files")
    if result.get("nextPageToken") or not isinstance(files, list) or len(files) > 1:
        raise RuntimeError("DRIVE_SHEET_IDENTITY_AMBIGUOUS")
    if files:
        file = files[0]
        old = file.get("appProperties") or {}
        if file.get("mimeType") != "application/vnd.google-apps.spreadsheet" or old.get("kind") != "sheet" or folder not in file.get("parents", []):
            raise RuntimeError("DRIVE_SHEET_REFERENCE_MISMATCH")
        sheet_id = file.get("id")
        response = await _drive_request(client, guard, "PATCH", f"https://www.googleapis.com/drive/v3/files/{sheet_id}?fields=id,appProperties",
                                        headers={**headers, "Content-Type": "application/json"}, json={"appProperties": props})
        if response.status_code not in {200, 201} or _json(response).get("id") != sheet_id or _json(response).get("appProperties") != props:
            raise RuntimeError("DRIVE_SHEET_UPDATE_UNKNOWN")
    else:
        response = await _drive_request(client, guard, "POST", "https://sheets.googleapis.com/v4/spreadsheets",
                                        headers={**headers, "Content-Type": "application/json"},
                                        json={"properties": {"title": name}, "sheets": [{"properties": {"title": "Deliverables"}}]})
        if response.status_code not in {200, 201}:
            raise RuntimeError("DRIVE_SHEET_CREATE_UNKNOWN")
        sheet_id = _json(response).get("spreadsheetId")
        if not sheet_id:
            raise RuntimeError("DRIVE_SHEET_CREATE_UNKNOWN")
        response = await _drive_request(client, guard, "PATCH", f"https://www.googleapis.com/drive/v3/files/{sheet_id}?addParents={folder}&fields=id,parents,appProperties",
                                        headers={**headers, "Content-Type": "application/json"}, json={"appProperties": props})
        moved = _json(response)
        if response.status_code not in {200, 201} or moved.get("id") != sheet_id or folder not in moved.get("parents", []) or moved.get("appProperties") != props:
            raise RuntimeError("DRIVE_SHEET_CREATE_UNKNOWN")
    response = await _drive_request(client, guard, "POST", f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values:batchClear",
                                    headers={**headers, "Content-Type": "application/json"}, json={"ranges": ["A:ZZZ"]})
    if response.status_code != 200:
        raise RuntimeError("DRIVE_SHEET_UPDATE_UNKNOWN")
    range_name = "'Deliverables'!A1"
    response = await _drive_request(client, guard, "PUT", f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_name}",
                                    params={"valueInputOption": "RAW"}, headers={**headers, "Content-Type": "application/json"}, json={"range": range_name, "majorDimension": "ROWS", "values": rows})
    updated = _json(response)
    if (response.status_code not in {200, 201} or not updated.get("updatedRange")
            or updated.get("spreadsheetId") != sheet_id
            or updated.get("updatedCells") != sum(len(row) for row in rows)):
        raise RuntimeError("DRIVE_SHEET_UPDATE_UNKNOWN")
    return {"kind": "sheet", "name": name, "file_id": sheet_id, "sha256": digest}


async def _sync_drive_tables(client, token, root_folder, form_folders, records, guard):
    grouped = {}
    for record in records:
        grouped.setdefault(_safe_drive_segment(record.get("form_id"), "unknown_form"), []).append(record)
    artifacts = [(root_folder, "Master_Survey_Deliverables.csv", [CSV_HEADERS, *(extract_survey_row_data(record) for record in records)])]
    artifacts.extend((form_folders[form_id], f"{form_id}.csv", [CSV_HEADERS, *(extract_survey_row_data(record) for record in rows)]) for form_id, rows in sorted(grouped.items()))
    receipts = []
    for parent, name, rows in artifacts:
        item = _table_item(name, rows)
        file_id, error = await _drive_file(client, token, parent, "drive-sync", item, guard)
        if error or not file_id:
            return _outcome("RETRYABLE", error or "DRIVE_REFERENCE_MISSING")
        receipts.append({"kind": "csv", "name": name, "file_id": file_id, "sha256": item["sha256"]})
        await _progress(guard, {"tables": list(receipts)})
        sheet = await _drive_sheet(client, token, parent, name.removesuffix(".csv"), rows, item["sha256"], guard)
        receipts.append(sheet)
        await _progress(guard, {"tables": list(receipts)})
    return receipts


@asynccontextmanager
async def _drive_lock(conn_str):
    import psycopg

    async with await psycopg.AsyncConnection.connect(conn_str, connect_timeout=10, autocommit=True) as conn:
        cur = await conn.execute("SELECT pg_try_advisory_lock(%s)", (DRIVE_SYNC_LOCK_KEY,))
        row = await cur.fetchone()
        if not row or not row[0]:
            yield None
            return
        try:
            yield conn
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (DRIVE_SYNC_LOCK_KEY,))


async def _progress(guard, receipt):
    record = getattr(guard, "__dict__", {}).get("record")
    if record is not None and not await record(receipt):
        raise PermissionError("LEASE_LOST")


async def _execute_drive(event: dict, submission: dict | None, sid: str, files: list[dict], guard) -> Outcome:
    payload = event["payload"]
    dest = payload.get("destination")
    root = dest.get("root_folder_id") if isinstance(dest, dict) else None
    folder = payload.get("folder_id") or (dest.get("folder_id") if isinstance(dest, dict) else None) or root
    if not isinstance(dest, dict) or dest.get("private") is not True or not isinstance(folder, str) or not folder.strip():
        return _outcome("TERMINAL", "DRIVE_DESTINATION_REQUIRED")
    if not isinstance(submission, dict) or str(submission.get("status", "")).upper() != "APPROVED":
        return _outcome("TERMINAL", "DRIVE_SUBMISSION_NOT_APPROVED")
    async with httpx.AsyncClient(timeout=25.0) as client:
        try:
            token = await _drive_token(client, guard)
            if not token:
                return _outcome("RETRYABLE", "DRIVE_NOT_CONFIGURED")
            receipts = []
            folders = []
            if root:
                form = _safe_drive_segment(payload.get("form_id") or submission.get("form_id"), "unknown_form")
                for segment in (form, "submissions", sid):
                    folder, receipt = await _drive_folder(client, token, folder, segment, guard)
                    folders.append(receipt)
                    await _progress(guard, {"folders": list(folders)})
            revision = event["payload"]["media_version"]
            items = [*files, _record_item(submission, sid, revision)]
            for item in items:
                known_id = (payload.get("file_ids") or {}).get(str(item["slot"])) or (payload.get("file_ids") or {}).get(item["slot"])
                file_id, error = await _drive_file(client, token, folder, sid, item, guard, known_id=known_id)
                if error:
                    return _outcome("RETRYABLE", error)
                if not file_id:
                    return _outcome("UNCERTAIN", "DRIVE_REFERENCE_MISSING")
                receipts.append({"slot": item["slot"], "file_id": file_id, "sha256": item["sha256"], "revision": item["revision"]})
                await _progress(guard, {"files": list(receipts)})
            return _outcome("SUCCEEDED", receipt={"provider": "drive", "files": receipts, "folders": folders})
        except PermissionError:
            return _outcome("UNCERTAIN", "LEASE_LOST")
        except _DriveRateLimited as exc:
            return Outcome("RETRYABLE", "DRIVE_RATE_LIMITED", {}, retry_after=exc.retry_after)
        except Exception as exc:
            reason = str(exc) if str(exc) in {"DRIVE_AUTH_UNKNOWN", "DRIVE_LOOKUP_UNKNOWN", "DRIVE_CREATE_UNKNOWN", "DRIVE_IDENTITY_AMBIGUOUS", "DRIVE_REFERENCE_MISMATCH"} else "DRIVE_OPERATION_UNKNOWN"
            return _outcome("UNCERTAIN", reason)



async def execute(event: dict, submission: dict | None, conn_str: str, guard) -> Outcome:
    """Execute one private R2 or approved Drive export; never falls back locally/publicly."""
    conn_str = str(conn_str or "")
    if not isinstance(event.get("payload"), dict):
        return _outcome("TERMINAL", "MALFORMED_PAYLOAD")
    policy = event["payload"].get("policy")
    if not isinstance(policy, dict) or policy.get("enabled") is not True:
        reason = policy.get("skip_reason") if isinstance(policy, dict) else None
        return _outcome("POLICY_SKIPPED", reason if isinstance(reason, str) and re.fullmatch(r"[A-Z0-9_]{1,96}", reason) else "POLICY_DISABLED")
    if not isinstance(policy.get("version"), str) or not policy["version"].strip():
        return _outcome("TERMINAL", "MISSING_POLICY_VERSION")
    target = event.get("flow_target")
    if target not in {"R2_UPLOAD", "GDRIVE_EXPORT"}:
        return _outcome("TERMINAL", "UNSUPPORTED_EXPORT_TARGET")
    if target == "GDRIVE_EXPORT" and event.get("aggregate_kind") == "drive_sync":
        return await _execute_drive_sync(event, conn_str, guard)
    if target == "R2_UPLOAD" and not getattr(settings, "R2_PRIVATE_BUCKET_NAME", None):
        return _outcome("POLICY_SKIPPED", "R2_PRIVATE_DESTINATION_UNCONFIGURED")
    try:
        sid, files = _media(event, submission)
    except ValueError as exc:
        return _outcome("TERMINAL", str(exc))
    if target == "R2_UPLOAD":
        return await _execute_r2(event, sid, files, guard)
    if not conn_str:
        return _outcome("TERMINAL", "DRIVE_DATABASE_REQUIRED")
    try:
        async with _drive_lock(conn_str) as locked:
            if locked is None:
                return _outcome("RETRYABLE", "DRIVE_SYNC_BUSY")
            return await _execute_drive(event, submission, sid, files, guard)
    except Exception:
        return _outcome("UNCERTAIN", "DRIVE_DATABASE_UNAVAILABLE")


async def _execute_drive_sync(event: dict, conn_str: str, guard) -> Outcome:
    payload = event["payload"]
    destination = payload.get("destination")
    root = payload.get("folder_id") or (destination.get("folder_id") if isinstance(destination, dict) else None)
    if not isinstance(destination, dict) or destination.get("private") is not True or not isinstance(root, str) or not root.strip():
        return _outcome("TERMINAL", "DRIVE_DESTINATION_REQUIRED")
    records = payload.get("submissions")
    if not isinstance(records, list):
        return _outcome("TERMINAL", "DRIVE_SYNC_RECORDS_REQUIRED")
    if not records:
        return _outcome("POLICY_SKIPPED", "DRIVE_SYNC_EMPTY")
    if not conn_str:
        return _outcome("TERMINAL", "DRIVE_SYNC_DATABASE_REQUIRED")
    if any(not isinstance(record, dict) for record in records):
        return _outcome("TERMINAL", "DRIVE_SYNC_RECORD_INVALID")
    identities = [record.get("submission_id") for record in records]
    if any(not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid) for sid in identities) or len(set(identities)) != len(records):
        return _outcome("TERMINAL", "DRIVE_SYNC_RECORD_INVALID")
    async with httpx.AsyncClient(timeout=25.0) as client:
        try:
            token = await _drive_token(client, guard)
            if not token:
                return _outcome("POLICY_SKIPPED", "DRIVE_NOT_CONFIGURED")
            files_receipt, verified, form_folders = [], [], {}
            async with _drive_lock(conn_str) as conn:
                if conn is None:
                    return _outcome("RETRYABLE", "DRIVE_SYNC_BUSY")
                # ponytail: hold approved-row locks across bulk publication; a
                # larger deployment can use versioned remote staging instead.
                async with conn.transaction():
                    submissions_folders, submission_folders = {}, {}
                    cur = await conn.execute("SELECT submission_id FROM survey_submissions WHERE status='APPROVED' FOR UPDATE")
                    from services.google_drive_delivery import is_test_submission
                    approved = {row[0] for row in await cur.fetchall() if not is_test_submission(row[0])}
                    if approved != set(identities):
                        return _outcome("POLICY_SKIPPED", "DRIVE_SYNC_STALE_SET")
                    for record in records:
                        if str(record.get("status", "")).upper() != "APPROVED":
                            return _outcome("TERMINAL", "DRIVE_SYNC_RECORD_INVALID")
                        sid = record["submission_id"]
                        cur = await conn.execute("SELECT status, media_version, decision_version, media_manifest FROM survey_submissions WHERE submission_id=%s FOR UPDATE", (sid,))
                        current = await cur.fetchone()
                        if not current or str(current[0]).upper() != "APPROVED" or current[1] != record.get("media_version") or current[2] != record.get("decision_version"):
                            return _outcome("POLICY_SKIPPED", "DRIVE_SYNC_STALE_RECORD")
                        manifest = record.get("media_manifest")
                        db_manifest = current[3]
                        if isinstance(db_manifest, str):
                            db_manifest = json.loads(db_manifest)
                        if db_manifest != manifest:
                            return _outcome("POLICY_SKIPPED", "DRIVE_SYNC_STALE_RECORD")
                        try:
                            sid, media = _media({"aggregate_id": sid, "payload": {"media_manifest": manifest, "media_version": record["media_version"]}}, record)
                        except (KeyError, ValueError):
                            return _outcome("TERMINAL", "DRIVE_SYNC_RECORD_INVALID")
                        form = _safe_drive_segment(record.get("form_id"), "unknown_form")
                        if form not in form_folders:
                            form_id, receipt = await _drive_folder(client, token, root, form, guard)
                            form_folders[form] = form_id
                            files_receipt.append(receipt)
                            await _progress(guard, {"files": list(files_receipt)})
                            submissions_id, receipt = await _drive_folder(client, token, form_id, "submissions", guard)
                            submissions_folders[form] = submissions_id
                            files_receipt.append(receipt)
                            await _progress(guard, {"files": list(files_receipt)})
                        if sid not in submission_folders:
                            submission_id, receipt = await _drive_folder(client, token, submissions_folders[form], sid, guard)
                            submission_folders[sid] = submission_id
                            files_receipt.append(receipt)
                            await _progress(guard, {"files": list(files_receipt)})
                        for item in [*media, _record_item(record, sid, record["media_version"])]:
                            known_id = (record.get("file_ids") or {}).get(str(item["slot"])) or (record.get("file_ids") or {}).get(item["slot"])
                            file_id, error = await _drive_file(client, token, submission_folders[sid], sid, item, guard, known_id=known_id)
                            if error:
                                return _outcome("RETRYABLE", error)
                            if not file_id:
                                return _outcome("UNCERTAIN", "DRIVE_REFERENCE_MISSING")
                            files_receipt.append({"submission_id": sid, "slot": item["slot"], "file_id": file_id, "sha256": item["sha256"], "revision": item["revision"]})
                            await _progress(guard, {"files": list(files_receipt)})
                        verified.append(record)
                    tables = await _sync_drive_tables(client, token, root, form_folders, verified, guard)
                    if isinstance(tables, Outcome):
                        return tables
                    files_receipt.extend(tables)
            return _outcome("SUCCEEDED", receipt={"provider": "drive", "aggregate_kind": "drive_sync", "files": files_receipt})
        except PermissionError:
            return _outcome("UNCERTAIN", "LEASE_LOST")
        except _DriveRateLimited as exc:
            return Outcome("RETRYABLE", "DRIVE_RATE_LIMITED", {}, retry_after=exc.retry_after)
        except Exception as exc:
            reason = str(exc) if str(exc).startswith("DRIVE_") else "DRIVE_OPERATION_UNKNOWN"
            return _outcome("UNCERTAIN", reason)


def _safe_drive_segment(value: Any, fallback: str) -> str:
    raw = str(value or "")
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw):
        return raw
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._") or fallback
