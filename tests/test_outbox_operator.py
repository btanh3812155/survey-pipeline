"""Operator contracts against the disposable PostgreSQL harness; no provider calls."""

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from config import settings
from core import durable_work
from core.schema_gate import MIGRATION_001_CHECKSUM
from routers import actions
from services import action_checklist
from tests.postgres_harness import apply_schema, resolve_test_database_url


@pytest.fixture
def operator_app(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USERNAME", "operator")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "operator-test-password")
    app = FastAPI()
    app.include_router(actions.router)
    return app


@asynccontextmanager
async def client_for(app, *, authenticated=True):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test",
        auth=(settings.ADMIN_USERNAME, settings.ADMIN_PASSWORD) if authenticated else None,
    ) as client:
        yield client


@pytest.fixture
def database():
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("disposable PostgreSQL is required")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL")
    dsn = resolve_test_database_url()
    migrations = Path(__file__).resolve().parents[1] / "scripts" / "migrations"
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as db:
        apply_schema(db)
        for name in ("001_submission_attempts", "002_outbox_delivery"):
            source = (migrations / f"{name}.sql").read_bytes()
            db.execute(source.decode().replace(":'migration_sha256'", f"'{hashlib.sha256(source).hexdigest()}'")
                       .replace(":'migration_001_sha256'", f"'{MIGRATION_001_CHECKSUM}'"))
        ids = []

        def submission(owner="outbox_v2", checklist=None):
            sid = "VN-OP-" + uuid4().hex
            ids.append(sid)
            db.execute("INSERT INTO survey_submissions(submission_id,status,work_owner,action_status) "
                       "VALUES (%s,'APPROVED',%s,%s)", (sid, owner, Jsonb(checklist or {})))
            return sid

        try:
            yield dsn, db, submission
        finally:
            db.execute("DELETE FROM outbox_delivery_attempts WHERE event_id IN "
                       "(SELECT id FROM outbox_events WHERE aggregate_id=ANY(%s))", (ids,))
            for table, column in (("outbox_events", "aggregate_id"), ("system_audit_log", "entity_id"),
                                  ("collaborator_billing_ledger", "submission_id"), ("survey_submissions", "submission_id")):
                db.execute(f"DELETE FROM {table} WHERE {column}=ANY(%s)", (ids,))


def event(db, sid, target, state, *, outcome=None, terminal=None, attempts=0, receipt=None, version=0):
    payload = {"decision_version": version, "media_version": 1,
               "policy": {"enabled": True, "version": "test"}}
    result = {"outcome": outcome, "receipt": receipt, "reason": "TEST_REASON"} if outcome else {}
    return db.execute(
        "INSERT INTO outbox_events(aggregate_id,event_type,flow_target,status,action_key,payload,result,terminal_reason,attempts) "
        "VALUES (%s,'survey.validated',%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (sid, target, state, None if target == "ALL" else uuid4().hex, Jsonb(payload), Jsonb(result), terminal, attempts),
    ).fetchone()


def current(db, event_row):
    return db.execute("SELECT * FROM outbox_events WHERE id=%s", (event_row["id"],)).fetchone()


def base(sid):
    return f"/api/v1/submissions/{sid}"


@pytest.mark.asyncio
async def test_legacy_json_is_read_only_and_operator_paths_require_auth(operator_app, monkeypatch, tmp_path):
    path = tmp_path / "dev_submissions.json"
    payload = action_checklist.init_action_checklist(is_approved=False, has_collaborator=True)
    assert payload["billing_ledger"]["status"] == payload["gdrive_sync"]["status"] == "SKIPPED"
    approved = action_checklist.init_action_checklist(is_approved=True, has_collaborator=True)
    assert approved["billing_ledger"]["status"] == approved["gdrive_sync"]["status"] == "PENDING"
    content = json.dumps([{"submission_id": "local", "action_status": payload}])
    path.write_text(content)
    monkeypatch.setattr(action_checklist, "DEV_STORAGE_FILE", str(path))
    monkeypatch.setattr(actions, "get_db_pool", lambda: None)
    prefix = base("local")
    eid = uuid4()
    async with client_for(operator_app, authenticated=False) as anon:
        for verb, path_suffix in (("GET", "/actions"), ("POST", "/retry-actions"),
                                  ("GET", f"/actions/{eid}/evidence"), ("POST", f"/actions/{eid}/reconcile"),
                                  ("POST", "/actions/reconcile-legacy")):
            assert (await anon.request(verb, prefix + path_suffix)).status_code == 401
    async with client_for(operator_app) as client:
        assert (await client.get(prefix + "/actions")).json() == {"submission_id": "local", "actions": payload}
        assert (await client.post(prefix + "/retry-actions")).status_code == 409
    assert path.read_text() == content
    assert not path.with_name("dev_test_submissions.json").exists()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_projection_and_retry_use_committed_rows_not_cached_json(database, operator_app, monkeypatch):
    dsn, db, submission = database
    sid = submission(checklist={"telegram_notify": {"status": "COMPLETED"}, "outbox_v2": {"stale": {"status": "PROCESSED"}}})
    failed = event(db, sid, "LARK_SYNC", "FAILED", outcome="RETRYABLE", attempts=5, terminal="ATTEMPTS_EXHAUSTED")
    uncertain = event(db, sid, "TELEGRAM_NOTIFY", "FAILED", outcome="UNCERTAIN", terminal="DELIVERY_UNCERTAIN")
    sent = event(db, sid, "TELEGRAM_NOTIFY", "PROCESSED", outcome="SUCCEEDED", receipt={"message_id": 22})
    processing = event(db, sid, "R2_UPLOAD", "PROCESSING")
    skipped = event(db, sid, "GDRIVE_EXPORT", "PROCESSED", outcome="SKIPPED")
    stale = event(db, sid, "LARK_SYNC", "FAILED", outcome="RETRYABLE", version=99)
    pending = event(db, sid, "TELEGRAM_NOTIFY", "PENDING")
    db.execute("UPDATE outbox_events SET available_at=NOW()+interval '1 hour' WHERE id=%s", (pending["id"],))
    paid = event(db, sid, "BILLING", "FAILED", outcome="RETRYABLE")
    ledger = db.execute("INSERT INTO collaborator_billing_ledger(submission_id,payout_amount_vnd,payout_status) "
                        "VALUES (%s,91000,'PAID') RETURNING *", (sid,)).fetchone()
    async with AsyncConnectionPool(dsn, min_size=1, max_size=3, open=False) as pool:
        monkeypatch.setattr(actions, "get_db_pool", lambda: pool)
        async with client_for(operator_app) as client:
            projection = (await client.get(base(sid) + "/actions")).json()["actions"]
            entries = projection["outbox_v2"]
            assert "stale" not in entries
            assert [entries[row["action_key"]]["status"] for row in (processing, skipped, sent, uncertain, failed, pending)] == [
                "RUNNING", "SKIPPED", "COMPLETED", "UNCERTAIN", "FAILED", "PENDING",
            ]
            assert projection["telegram_notify"]["status"] == "UNCERTAIN"
            response = await client.post(base(sid) + "/retry-actions")
            assert response.json() == {"status": "retry_dispatched", "submission_id": sid}
            assert current(db, failed)["status"] == "PENDING" and current(db, failed)["attempts"] == 0
            await client.post(base(sid) + "/retry-actions")
    assert [current(db, row)["status"] for row in (uncertain, stale, paid, processing)] == [
        "FAILED", "FAILED", "FAILED", "PROCESSING",
    ]
    assert current(db, pending)["available_at"] > current(db, pending)["created_at"]
    assert db.execute("SELECT * FROM collaborator_billing_ledger WHERE submission_id=%s", (sid,)).fetchone() == ledger
    assert db.execute("SELECT details FROM system_audit_log WHERE entity_id=%s AND action='OUTBOX_RETRY'", (sid,)).fetchall() == [
        {"details": {"event_ids": [str(failed["id"])]}},
    ]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_operator_receipts_and_resends_are_fenced_by_evidence(database, operator_app, monkeypatch):
    dsn, db, submission = database
    sid, other = submission(), submission()
    uncertain, delivered, late, absent = [event(db, sid, "TELEGRAM_NOTIFY", "FAILED", outcome="UNCERTAIN",
                                                terminal="DELIVERY_UNCERTAIN", attempts=1) for _ in range(4)]
    attempt = db.execute("INSERT INTO outbox_delivery_attempts(event_id,claim_token,attempt_number,request_started,outcome,receipt) "
                         "VALUES (%s,'retained',1,true,'UNCERTAIN',%s) RETURNING *",
                         (uncertain["id"], Jsonb({"operation": "send"}))).fetchone()
    db.execute("INSERT INTO outbox_delivery_attempts(event_id,claim_token,attempt_number,request_started,outcome,receipt) "
               "VALUES (%s,'late',1,true,'LATE_SUCCEEDED',%s)", (late["id"], Jsonb({"message_id": 17})))
    async with AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False) as pool:
        monkeypatch.setattr(actions, "get_db_pool", lambda: pool)
        async with client_for(operator_app) as client:
            url = base(sid) + f"/actions/{uncertain['id']}"
            snap = (await client.get(url + "/evidence")).json()
            assert snap["attempts"][0]["receipt"] == {"operation": "send"}
            assert "claim_token" not in snap["attempts"][0]
            assert (await client.get(base(other) + f"/actions/{uncertain['id']}/evidence")).status_code == 404
            body = {"command": "resend", "version": snap["version"], "evidence_sha256": "a" * 64}
            assert (await client.post(url + "/reconcile", json=body)).status_code == 409
            body["accept_duplicate_risk"] = True
            responses = await asyncio.gather(*(client.post(url + "/reconcile", json=body) for _ in range(2)))
            assert sorted(response.status_code for response in responses) == [200, 409]
            child_id = next(response.json()["resend_event_id"] for response in responses if response.status_code == 200)
            child = db.execute("SELECT * FROM outbox_events WHERE id=%s", (child_id,)).fetchone()
            assert child["status"] == "PENDING" and child["action_key"] != uncertain["action_key"]
            assert child["payload"] == {**uncertain["payload"], "operator_resend_of": str(uncertain["id"])}
            assert current(db, uncertain) == uncertain
            assert (await client.get(url + "/evidence")).json()["resend_event_id"] == child_id
            for row, command, expected in ((delivered, "confirm_delivered", 200), (late, "resend", 409),
                                           (absent, "confirm_not_delivered", 200)):
                path = base(sid) + f"/actions/{row['id']}"
                version = (await client.get(path + "/evidence")).json()["version"]
                assert (await client.post(path + "/reconcile", json={**body, "command": command, "version": version})).status_code == expected
    assert current(db, delivered)["status"] == "PROCESSED"
    assert current(db, delivered)["result"]["prior_result"] == delivered["result"]
    assert current(db, late)["status"] == "FAILED"
    assert current(db, absent)["status"] == "FAILED"
    assert current(db, absent)["terminal_reason"] == "OPERATOR_CONFIRMED_NOT_DELIVERED"
    assert db.execute("SELECT * FROM outbox_delivery_attempts WHERE event_id=%s", (uncertain["id"],)).fetchone() == attempt
    audit = db.execute("SELECT actor_id,details FROM system_audit_log WHERE entity_id=%s AND action='OUTBOX_OPERATOR_RESOLUTION'", (sid,)).fetchall()
    assert sum(item["details"]["command"] == "resend" and item["details"]["accept_duplicate_risk"] for item in audit) == 1
    assert {item["actor_id"] for item in audit} == {settings.ADMIN_USERNAME}


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_legacy_quarantine_and_selective_transfer_are_atomic(database, operator_app, monkeypatch):
    dsn, db, submission = database
    sid = submission("legacy", {"r2_upload": {"status": "COMPLETED", "synced": True}})
    parents = [event(db, sid, "ALL", state) for state in ("PENDING", "PROCESSED", "PROCESSING")]
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-100123")
    monkeypatch.setattr(settings, "GOOGLE_DRIVE_ROOT_FOLDER_ID", "")
    async with AsyncConnectionPool(dsn, min_size=1, max_size=3, open=False) as pool:
        monkeypatch.setattr(actions, "get_db_pool", lambda: pool)
        async with client_for(operator_app) as client:
            prefix = base(sid)
            actions_url = prefix + "/actions"
            projected = (await client.get(actions_url)).json()["actions"]
            assert projected["r2_upload"]["status"] == "UNCERTAIN"
            snapshot = projected["legacy_reconciliation"]
            assert {parent["classification"] for parent in snapshot["parents"]} == {"AMBIGUOUS"}
            assert (await client.post(prefix + "/retry-actions")).status_code == 409
            url = actions_url + "/reconcile-legacy"
            evidence = {"ADMIN_REVIEW": {"finding": "missing", "evidence_sha256": "b" * 64}}
            assert (await client.post(url, json={"version": snapshot["version"], "legacy_drained": True,
                                                 "evidence": evidence})).status_code == 409
            assert (await client.post(url, json={"version": snapshot["version"]})).json()["child_ids"] == []
            assert all(current(db, row)["terminal_reason"] == "LEGACY_QUARANTINED" for row in parents)
            assert db.execute("SELECT count(*) AS n FROM outbox_events WHERE aggregate_id=%s", (sid,)).fetchone()["n"] == 3
            assert await action_checklist._claim_legacy_action(pool, sid, "lark_sync") is None
            version = (await client.get(actions_url)).json()["actions"]["legacy_reconciliation"]["version"]
            blocked = await client.post(url, json={"version": version, "legacy_drained": True, "evidence": {
                "GDRIVE_EXPORT": {"finding": "missing", "evidence_sha256": "c" * 64},
            }})
            assert blocked.status_code == 200 and blocked.json()["child_ids"] == []
            assert current(db, parents[0])["result"]["legacy_reconciliation"]["blocked"] == ["GDRIVE_EXPORT"]
            version = (await client.get(actions_url)).json()["actions"]["legacy_reconciliation"]["version"]
            body = {"version": version, "legacy_drained": True, "evidence": evidence}

            async def audit_failure(*args, **kwargs):
                raise RuntimeError("injected audit write failure")

            with monkeypatch.context() as patch:
                patch.setattr(durable_work, "audit", audit_failure)
                assert (await client.post(url, json=body)).status_code == 500
            assert db.execute("SELECT work_owner FROM survey_submissions WHERE submission_id=%s", (sid,)).fetchone()["work_owner"] == "legacy"
            assert db.execute("SELECT count(*) AS n FROM outbox_events WHERE aggregate_id=%s", (sid,)).fetchone()["n"] == 3
            response = await client.post(url, json=body)
            assert response.status_code == 200
            (child_id,) = response.json()["child_ids"]
            assert (await client.post(url, json=body)).status_code == 409
    child = db.execute("SELECT * FROM outbox_events WHERE id=%s", (child_id,)).fetchone()
    assert child["flow_target"] == "TELEGRAM_NOTIFY" and child["payload"]["message_class"] == "ADMIN_REVIEW"
    assert db.execute("SELECT work_owner FROM survey_submissions WHERE submission_id=%s", (sid,)).fetchone()["work_owner"] == "outbox_v2"
    for row in parents:
        marker = current(db, row)["result"]["legacy_reconciliation"]
        assert marker["source_status"] == row["status"]
        assert marker["child_ids"] == [child_id]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_legacy_database_claim_has_one_owner_and_no_expired_replay(database):
    dsn, db, submission = database
    sid = submission("legacy", {"r2_upload": {"status": "PENDING"}})
    async with AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False) as pool:
        tokens = await asyncio.gather(*(action_checklist._claim_legacy_action(pool, sid, "r2_upload") for _ in range(2)))
        (token,) = [token for token in tokens if token]
        db.execute("UPDATE survey_submissions SET action_status=jsonb_set(action_status,'{r2_upload,lease_until}',"
                   "'\"2000-01-01T00:00:00+00:00\"') WHERE submission_id=%s", (sid,))
        assert await action_checklist._claim_legacy_action(pool, sid, "r2_upload") is None
        await action_checklist._finish_legacy_action(pool, sid, "r2_upload", "wrong-token", "COMPLETED")
        assert (await action_checklist.fetch_action_status(pool, sid))["r2_upload"]["status"] == "RUNNING"
        await action_checklist._finish_legacy_action(pool, sid, "r2_upload", token, "COMPLETED")
        assert (await action_checklist.fetch_action_status(pool, sid))["r2_upload"]["status"] == "COMPLETED"
        assert await action_checklist._claim_legacy_action(pool, submission(), "r2_upload") is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_legacy_transfer_pins_committed_private_media_for_exports(database, operator_app, monkeypatch, tmp_path):
    from services import submission_media

    dsn, db, submission = database
    sid = submission("legacy")
    event(db, sid, "ALL", "PENDING")
    monkeypatch.setattr(submission_media, "PRIVATE_MEDIA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "R2_PRIVATE_BUCKET_NAME", "private-legacy")
    monkeypatch.setattr(settings, "GOOGLE_DRIVE_ROOT_FOLDER_ID", "private-drive-root")
    directory = tmp_path / sid
    directory.mkdir()
    (directory / "slot_0.webp").write_bytes(b"committed evidence")
    (directory / "slot_0_v99.webp").write_bytes(b"uncommitted evidence")
    async with AsyncConnectionPool(dsn, min_size=1, max_size=1, open=False) as pool:
        monkeypatch.setattr(actions, "get_db_pool", lambda: pool)
        async with client_for(operator_app) as client:
            snapshot = (await client.get(base(sid) + "/actions")).json()["actions"]["legacy_reconciliation"]
            body = {"version": snapshot["version"], "legacy_drained": True, "evidence": {
                target: {"finding": "missing", "evidence_sha256": "a" * 64}
                for target in ("R2_UPLOAD", "GDRIVE_EXPORT")}}
            response = await client.post(base(sid) + "/actions/reconcile-legacy", json=body)
            assert response.status_code == 200
        async with pool.connection() as conn:
            await durable_work.transition_submission(conn, sid, "REJECTED", "test", actor="operator")
            await durable_work.transition_submission(conn, sid, "APPROVED", "test", actor="operator")
            await conn.commit()
    manifest = db.execute("SELECT media_manifest FROM survey_submissions WHERE submission_id=%s", (sid,)).fetchone()["media_manifest"]
    assert [(item["relative_path"], item["sha256"]) for item in manifest] == [
        ("slot_0.webp", hashlib.sha256(b"committed evidence").hexdigest())]
    children = db.execute("SELECT payload FROM outbox_events WHERE aggregate_id=%s AND flow_target IN ('R2_UPLOAD','GDRIVE_EXPORT')", (sid,)).fetchall()
    assert len(response.json()["child_ids"]) == 2
    assert children and all(row["payload"]["media_manifest"] == manifest for row in children)
    assert (directory / "slot_0_v99.webp").read_bytes() == b"uncommitted evidence"
