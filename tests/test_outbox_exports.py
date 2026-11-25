import json
from email import policy
from email.parser import BytesParser

import httpx
import hashlib
import sys
import types

from contextlib import asynccontextmanager


import pytest
from services import outbox_exports
from services import submission_media


@pytest.fixture(autouse=True)
def isolated_drive_database(monkeypatch):
    @asynccontextmanager
    async def locked(_dsn):
        yield object()
    monkeypatch.setattr(outbox_exports, "_drive_lock", locked)


@pytest.fixture
def manifest_data(monkeypatch, tmp_path):
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(tmp_path))
    folder = tmp_path / "SUB-1"
    folder.mkdir()
    blobs = [b"first immutable image", b"replacement revision"]
    entries = []
    for slot, blob in enumerate(blobs):
        name = f"slot_{slot}_v1.webp"
        (folder / name).write_bytes(blob)
        entries.append({"question_id": f"q{slot}", "slot_index": 0, "flat_index": slot, "relative_path": name, "sha256": hashlib.sha256(blob).hexdigest(), "mime": "image/webp", "media_version": slot + 1})
    return entries, blobs


def event_for(flow="GDRIVE_EXPORT", **payload):
    aggregate_kind = payload.pop("aggregate_kind", "submission")
    return {
        "aggregate_id": "SUB-1",
        "aggregate_kind": aggregate_kind,
        "flow_target": flow,
        "payload": {"policy": {"enabled": True, "version": "p1"}, "media_version": 2, **payload},
    }


async def yes_guard():
    return True


@pytest.mark.asyncio
async def test_empty_drive_snapshot_is_an_explicit_skip_without_provider_calls(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(outbox_exports, "_drive_token", AsyncMock(side_effect=AssertionError("empty sync contacted provider")))
    event = event_for(aggregate_kind="drive_sync", destination={"private": True, "folder_id": "private-root"}, submissions=[])
    result = await outbox_exports.execute(event, None, "postgresql://disposable", yes_guard)
    assert (result.outcome, result.reason) == ("POLICY_SKIPPED", "DRIVE_SYNC_EMPTY")


@pytest.mark.asyncio
async def test_drive_exports_all_manifest_slots_and_sanitized_record(monkeypatch, manifest_data):
    entries, _ = manifest_data
    uploaded = []

    async def upload(client, token, folder, sid, item, guard, known_id=None):
        uploaded.append((sid, item["slot"], item["sha256"]))
        return f"remote-{item['slot']}", None

    async def no_guard(*_):
        return "token"

    monkeypatch.setattr(outbox_exports, "_drive_token", no_guard)
    monkeypatch.setattr(outbox_exports, "_drive_file", upload)
    submission = {"submission_id": "SUB-1", "status": "APPROVED", "vehicle_type": "car", "driver_phone": "must-not-export"}
    event = event_for(destination={"private": True, "folder_id": "private-folder"}, media_manifest=entries)
    result = await outbox_exports.execute(event, submission, "postgresql://disposable", yes_guard)
    assert result.outcome == "SUCCEEDED"
    assert [x[1] for x in uploaded] == [0, 1, "record"]
    assert "driver_phone" not in outbox_exports._sanitize(submission)
    assert all(item["file_id"] for item in result.receipt["files"])


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [None, "set", "revision"])
async def test_drive_sync_exports_only_approved_records_with_remote_refs(monkeypatch, manifest_data, stale):
    entries, _ = manifest_data
    async def no_guard(*_):
        return "token"
    class Cursor:
        async def fetchone(self):
            return ("APPROVED", 99 if stale == "revision" else 2, 0, entries)
        async def fetchall(self): return [("SUB-1",), ("VN-NEW",)] if stale == "set" else [("SUB-1",)]
    class Tx:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
    class Conn:
        def transaction(self): return Tx()
        async def execute(self, *_args): return Cursor()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
    async def _empty_tables(*_args): return []
    @asynccontextmanager
    async def locked(_dsn):
        yield Conn()
    folders = []
    async def folder(client, token, parent, name, guard):
        folders.append((parent, name))
        return f"{parent}/{name}", {"kind": "folder", "file_id": f"{parent}/{name}", "name": name, "parent_id": parent}
    monkeypatch.setattr(outbox_exports, "_drive_lock", locked)
    monkeypatch.setattr(outbox_exports, "_drive_folder", folder)
    monkeypatch.setattr(outbox_exports, "_sync_drive_tables", _empty_tables)

    seen = []

    async def upload(client, token, folder, sid, item, guard, known_id=None):
        seen.append(item["slot"])
        return "remote-id", None

    monkeypatch.setattr(outbox_exports, "_drive_token", no_guard)
    monkeypatch.setattr(outbox_exports, "_drive_file", upload)
    event = event_for(aggregate_kind="drive_sync", destination={"private": True, "folder_id": "private-folder"},
                      submissions=[{"submission_id": "SUB-1", "form_id": "form", "status": "APPROVED", "decision_version": 0, "media_manifest": entries, "media_version": 2}])
    result = await outbox_exports.execute(event, None, "postgresql://test", yes_guard)
    if stale:
        assert result.outcome == "POLICY_SKIPPED"
        assert not seen and not folders
        return
    assert result.outcome == "SUCCEEDED"
    assert folders == [("private-folder", "form"), ("private-folder/form", "submissions"), ("private-folder/form/submissions", "SUB-1")]
    assert result.receipt["aggregate_kind"] == "drive_sync"
    assert seen == [0, 1, "record"]
    media_receipts = [row for row in result.receipt["files"] if "slot" in row]
    assert all(row["file_id"] == "remote-id" for row in media_receipts)


@pytest.mark.asyncio
async def test_drive_missing_remote_id_and_unknown_write_are_uncertain(monkeypatch, manifest_data):
    entries, _ = manifest_data

    async def no_guard(*_):
        return "token"

    async def no_id(*_args, **_kwargs):
        return None, None

    monkeypatch.setattr(outbox_exports, "_drive_token", no_guard)
    monkeypatch.setattr(outbox_exports, "_drive_file", no_id)
    event = event_for(destination={"private": True, "folder_id": "private-folder"}, media_manifest=entries)
    submission = {"submission_id": "SUB-1", "status": "APPROVED"}
    result = await outbox_exports.execute(event, submission, "postgresql://disposable", yes_guard)
    assert (result.outcome, result.reason) == ("UNCERTAIN", "DRIVE_REFERENCE_MISSING")

    async def ambiguous(*_args, **_kwargs):
        raise RuntimeError("DRIVE_CREATE_UNKNOWN")

    monkeypatch.setattr(outbox_exports, "_drive_file", ambiguous)
    result = await outbox_exports.execute(event, submission, "postgresql://disposable", yes_guard)
    assert (result.outcome, result.reason) == ("UNCERTAIN", "DRIVE_CREATE_UNKNOWN")


@pytest.mark.asyncio
async def test_manifest_hash_malformed_and_missing_destination_fail_closed(manifest_data):
    entries, _ = manifest_data
    event = event_for(destination={"private": True, "folder_id": "private-folder"}, media_manifest=entries)
    bad_hash = [dict(entries[0], sha256="0" * 64), entries[1]]
    result = await outbox_exports.execute(event_for(destination={"private": True, "folder_id": "private-folder"}, media_manifest=bad_hash), None, "", yes_guard)
    assert (result.outcome, result.reason) == ("TERMINAL", "MEDIA_HASH_MISMATCH")
    malformed = [dict(entries[0], slot_index="zero"), entries[1]]
    result = await outbox_exports.execute(event_for(destination={"private": True, "folder_id": "private-folder"}, media_manifest=malformed), None, "", yes_guard)
    assert (result.outcome, result.reason) == ("TERMINAL", "MALFORMED_MANIFEST")
    missing = await outbox_exports.execute(event_for(media_manifest=entries), {"submission_id": "SUB-1", "status": "APPROVED"}, "postgresql://disposable", yes_guard)
    assert (missing.outcome, missing.reason) == ("TERMINAL", "DRIVE_DESTINATION_REQUIRED")


@pytest.mark.asyncio
async def test_disabled_policy_and_unconfigured_private_r2_do_no_io(monkeypatch, manifest_data):
    entries, _ = manifest_data
    event = event_for("R2_UPLOAD", media_manifest=entries, destination={"bucket": "private", "private": True})
    event["payload"]["policy"]["enabled"] = False
    result = await outbox_exports.execute(event, None, "", yes_guard)
    assert result.outcome == "POLICY_SKIPPED"
    event["payload"]["policy"]["enabled"] = True
    monkeypatch.setattr(outbox_exports.settings, "R2_PRIVATE_BUCKET_NAME", None, raising=False)
    result = await outbox_exports.execute(event, None, "", yes_guard)
    assert (result.outcome, result.reason) == ("POLICY_SKIPPED", "R2_PRIVATE_DESTINATION_UNCONFIGURED")

@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "bytes", "receipt", "lease"])
async def test_r2_upload_verifies_all_slots_hashes_and_receipt(monkeypatch, manifest_data, fault):
    entries, blobs = manifest_data
    state = {}
    class Missing(Exception):
        response = {"Error": {"Code": "404"}}
    class Body:
        def __init__(self, value): self.value = value
        async def read(self): return b"corrupt remote bytes" if fault == "bytes" else self.value
    class S3:
        async def head_object(self, **kwargs):
            if kwargs["Key"] not in state: raise Missing()
            value = state[kwargs["Key"]]
            return {"ContentLength": len(value["body"]), "Metadata": value["metadata"], "ETag": None if fault == "receipt" else '"etag"', "ResponseMetadata": {"RequestId": "req-1"}}
        async def put_object(self, **kwargs):
            state[kwargs["Key"]] = {"body": kwargs["Body"], "metadata": kwargs["Metadata"]}
        async def get_object(self, **kwargs): return {"Body": Body(state[kwargs["Key"]]["body"])}
    class Context:
        async def __aenter__(self): return S3()
        async def __aexit__(self, *args): return False
    class Session:
        def client(self, *args, **kwargs):
            assert kwargs["config"].retries["total_max_attempts"] == 1
            return Context()
    monkeypatch.setitem(sys.modules, "aioboto3", types.SimpleNamespace(Session=Session))
    monkeypatch.setattr(outbox_exports.settings, "R2_PRIVATE_BUCKET_NAME", "private", raising=False)
    monkeypatch.setattr(outbox_exports.settings, "R2_ACCOUNT_ID", "account")
    monkeypatch.setattr(outbox_exports.settings, "R2_ACCESS_KEY_ID", "key")
    monkeypatch.setattr(outbox_exports.settings, "R2_SECRET_ACCESS_KEY", "secret")
    calls = 0
    async def guard():
        nonlocal calls
        calls += 1
        return fault != "lease" or calls < 2
    event = event_for("R2_UPLOAD", destination={"bucket": "private", "private": True}, media_manifest=entries)
    result = await outbox_exports.execute(event, None, "", guard)
    if fault:
        expected = {"bytes": "R2_OBJECT_MISMATCH", "receipt": "R2_RECEIPT_MISSING", "lease": "LEASE_LOST"}[fault]
        assert (result.outcome, result.reason) == ("UNCERTAIN", expected)
        assert len(state) == (0 if fault == "lease" else 1)
        return
    assert result.outcome == "SUCCEEDED"
    assert len(result.receipt["objects"]) == len(blobs)
    assert calls == len(blobs) * 5
    assert all(item["etag"] and item["request_id"] for item in result.receipt["objects"])


async def _token():
    return "token"


@pytest.fixture
def drive_wire(monkeypatch):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(outbox_exports.settings, "GOOGLE_DRIVE_ACCESS_TOKEN", "offline-token")
    monkeypatch.setattr(outbox_exports.settings, "GOOGLE_DRIVE_REFRESH_TOKEN", "")
    monkeypatch.setattr(outbox_exports.settings, "GOOGLE_DRIVE_CLIENT_ID", "")
    monkeypatch.setattr(outbox_exports.settings, "GOOGLE_DRIVE_CLIENT_SECRET", "")

    def install(respond):
        calls = []
        def handler(request):
            calls.append(request)
            return respond(request)
        monkeypatch.setattr(outbox_exports.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
        return calls
    return install


@pytest.mark.asyncio
async def test_drive_wire_uploads_related_parts_and_verifies_every_remote_hash(drive_wire, manifest_data):
    entries, blobs = manifest_data
    remote = {}

    def respond(request):
        if request.method == "GET" and request.url.params.get("alt") == "media":
            return httpx.Response(200, content=remote[request.url.path.rsplit("/", 1)[-1]])
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        assert request.method == "POST" and request.url.params["uploadType"] == "multipart"
        content_type = request.headers["Content-Type"]
        assert content_type.startswith("multipart/related;")
        message = BytesParser(policy=policy.default).parsebytes(("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + request.content)
        metadata_part, file_part = list(message.iter_parts())
        metadata = json.loads(metadata_part.get_payload(decode=True))
        content = file_part.get_payload(decode=True)
        assert hashlib.sha256(content).hexdigest() == metadata["appProperties"]["sha256"]
        remote_id = f"remote-{len(remote)}"
        remote[remote_id] = content
        return httpx.Response(200, json={"id": remote_id, "appProperties": metadata["appProperties"]})

    calls = drive_wire(respond)
    item = event_for(destination={"private": True, "folder_id": "approved-folder"}, media_manifest=entries)
    result = await outbox_exports.execute(item, {"submission_id": "SUB-1", "status": "APPROVED", "driver_phone": "private"}, "postgresql://disposable", yes_guard)
    assert result.outcome == "SUCCEEDED"
    assert [entry["revision"] for entry in result.receipt["files"][:2]] == [1, 2]
    assert list(remote.values())[:2] == blobs
    assert "driver_phone" not in json.loads(list(remote.values())[2])
    assert len(calls) == 9


@pytest.mark.asyncio
async def test_drive_rate_limit_preserves_provider_deferral(drive_wire, manifest_data):
    entries, _ = manifest_data
    calls = drive_wire(lambda request: httpx.Response(429, headers={"Retry-After": "901"}))
    result = await outbox_exports.execute(
        event_for(destination={"private": True, "folder_id": "folder"}, media_manifest=entries),
        {"status": "APPROVED"}, "postgresql://disposable", yes_guard,
    )
    assert (result.outcome, result.reason, result.retry_after) == ("RETRYABLE", "DRIVE_RATE_LIMITED", 901)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_drive_folder_receipt_validates_parent_and_requests_metadata(drive_wire):
    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        assert request.url.params["fields"] == "id,name,mimeType,parents"
        return httpx.Response(200, json={"id": "folder-id", **json.loads(request.content)})
    drive_wire(respond)
    async with httpx.AsyncClient() as client:
        folder, receipt = await outbox_exports._drive_folder(client, "token", "root", "form", yes_guard)
    assert folder == "folder-id"
    assert receipt == {"kind": "folder", "file_id": "folder-id", "parent_id": "root", "name": "form"}


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_receipt", [True, False])
async def test_native_sheet_publication_requires_confirmed_cell_count(drive_wire, valid_receipt):
    rows = [["Column", "Second"], ["Value", "Another"]]
    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        if request.url.host == "sheets.googleapis.com" and request.url.path.endswith("/spreadsheets"):
            return httpx.Response(200, json={"spreadsheetId": "sheet-id"})
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": "sheet-id", "parents": ["form-folder"], "appProperties": {"kind": "sheet", "sha256": "a" * 64}})
        if request.url.path.endswith("batchClear"):
            return httpx.Response(200, json={"clearedRanges": ["Deliverables!A:ZZZ"]})
        assert request.method == "PUT"
        assert json.loads(request.content)["values"] == rows
        return httpx.Response(200, json={"spreadsheetId": "sheet-id", "updatedRange": "Deliverables!A1:B2", "updatedCells": 4 if valid_receipt else 0})
    drive_wire(respond)
    async with httpx.AsyncClient() as client:
        if valid_receipt:
            receipt = await outbox_exports._drive_sheet(client, "token", "form-folder", "form", rows, "a" * 64, yes_guard)
            assert receipt["file_id"] == "sheet-id"
        else:
            with pytest.raises(RuntimeError, match="DRIVE_SHEET_UPDATE_UNKNOWN"):
                await outbox_exports._drive_sheet(client, "token", "form-folder", "form", rows, "a" * 64, yes_guard)


@pytest.mark.asyncio
async def test_bulk_tables_preserve_master_and_per_form_rows(monkeypatch):
    import csv
    import io

    csvs, sheets = {}, {}
    async def upload(client, token, folder, sid, item, guard, known_id=None):
        csvs[(folder, item["filename"])] = list(csv.reader(io.StringIO(item["content"].decode("utf-8-sig"))))
        return "csv-" + item["filename"], None
    async def sheet(client, token, folder, name, rows, digest, guard):
        sheets[(folder, name)] = rows
        return {"kind": "sheet", "file_id": "sheet-" + name, "sha256": digest}
    monkeypatch.setattr(outbox_exports, "_drive_file", upload)
    monkeypatch.setattr(outbox_exports, "_drive_sheet", sheet)
    records = [{"submission_id": "VN-A", "form_id": "_a"}, {"submission_id": "VN-B", "form_id": "b"}]
    async with httpx.AsyncClient() as client:
        result = await outbox_exports._sync_drive_tables(client, "token", "root", {"_a": "folder-a", "b": "folder-b"}, records, yes_guard)
    assert csvs[("root", "Master_Survey_Deliverables.csv")][0] == outbox_exports.CSV_HEADERS
    assert [row[-1] for row in csvs[("root", "Master_Survey_Deliverables.csv")][1:]] == ["VN-A", "VN-B"]
    assert csvs[("folder-a", "_a.csv")][1][-1] == "VN-A"
    assert csvs[("folder-b", "b.csv")][1][-1] == "VN-B"
    assert set(sheets) == {("root", "Master_Survey_Deliverables"), ("folder-a", "_a"), ("folder-b", "b")}
    assert len(result) == 6 and all(row["file_id"] for row in result)


@pytest.mark.asyncio
async def test_drive_token_prefers_refresh_over_stale_static_token(monkeypatch):
    class Client:
        async def request(self, method, url, **kwargs):
            return httpx.Response(200, json={"access_token": "fresh-token"}, request=httpx.Request(method, url))

    monkeypatch.setattr(outbox_exports.settings, "GOOGLE_DRIVE_ACCESS_TOKEN", "stale-token")
    monkeypatch.setattr(outbox_exports.settings, "GOOGLE_DRIVE_REFRESH_TOKEN", "refresh")
    monkeypatch.setattr(outbox_exports.settings, "GOOGLE_DRIVE_CLIENT_ID", "client")
    monkeypatch.setattr(outbox_exports.settings, "GOOGLE_DRIVE_CLIENT_SECRET", "secret")
    assert await outbox_exports._drive_token(Client(), yes_guard) == "fresh-token"


@pytest.mark.asyncio
async def test_private_bucket_cannot_alias_public_destination(monkeypatch, manifest_data):
    entries, _ = manifest_data
    monkeypatch.setattr(outbox_exports.settings, "R2_PRIVATE_BUCKET_NAME", outbox_exports.settings.R2_BUCKET_NAME)
    result = await outbox_exports.execute(event_for("R2_UPLOAD", destination={"private": True, "bucket": outbox_exports.settings.R2_BUCKET_NAME}, media_manifest=entries), None, "", yes_guard)
    assert (result.outcome, result.reason) == ("TERMINAL", "R2_PUBLIC_BUCKET_FORBIDDEN")


@pytest.mark.asyncio
async def test_per_record_root_destination_uses_same_form_submission_layout(monkeypatch, manifest_data):
    entries, _ = manifest_data
    parents, uploads = [], []
    async def token(*args): return "token"
    async def folder(client, token, parent, name, guard):
        parents.append((parent, name))
        return parent + "/" + name, {"kind": "folder", "file_id": parent + "/" + name}
    async def upload(client, token, parent, sid, item, guard, known_id=None):
        uploads.append(parent)
        return "remote-" + str(item["slot"]), None
    monkeypatch.setattr(outbox_exports, "_drive_token", token)
    monkeypatch.setattr(outbox_exports, "_drive_folder", folder)
    monkeypatch.setattr(outbox_exports, "_drive_file", upload)
    result = await outbox_exports.execute(event_for(form_id="_form", media_manifest=entries, destination={"private": True, "root_folder_id": "root"}), {"status": "APPROVED"}, "postgresql://disposable", yes_guard)
    assert result.outcome == "SUCCEEDED"
    assert parents == [("root", "_form"), ("root/_form", "submissions"), ("root/_form/submissions", "SUB-1")]
    assert uploads == ["root/_form/submissions/SUB-1"] * 3
