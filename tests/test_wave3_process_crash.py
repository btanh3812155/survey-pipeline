"""A killed submit worker leaves its media, but no committed Wave 3 rows."""

import asyncio
import io
import json
import multiprocessing
import os
import signal
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image
from psycopg_pool import AsyncConnectionPool

from tests.postgres_harness import apply_schema, connect, resolve_test_database_url


def _run_submit_worker(dsn, media_dir, work_dir, key, photo, parent_end, child_end, crash):
    parent_end.close()
    os.chdir(work_dir)
    from routers import survey

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(survey.settings, "ENVIRONMENT", "test")
        patch.setattr(survey, "get_redis_client", lambda: None)
        patch.setattr(survey, "refresh_form_registry_from_disk", lambda: None)
        patch.setattr(survey, "SUBMISSION_PHOTOS_DIR", media_dir)
        patch.setattr(survey.BackgroundTasks, "add_task", lambda *args, **kwargs: None)
        if crash:
            original = survey.commit_response

            async def pause_before_commit(conn, scope, attempt_key, fingerprint, submission_id, response):
                await original(conn, scope, attempt_key, fingerprint, submission_id, response)
                child_end.send(("before commit", submission_id))
                child_end.poll(60)
                os._exit(1)

            patch.setattr(survey, "commit_response", pause_before_commit)

        async def submit():
            async with AsyncConnectionPool(conninfo=dsn, min_size=1, max_size=1, open=False) as pool:
                patch.setattr(survey, "get_db_pool", lambda: pool)
                app = FastAPI()
                app.include_router(survey.router)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    return await client.post(
                        "/api/v1/survey/submit-webrtc",
                        data={"idempotency_key": key},
                        files={"survey_photo": ("test.jpg", photo, "image/jpeg")},
                    )

        response = asyncio.run(submit())
        child_end.send(("response", response.status_code, response.text))


@pytest.mark.postgres
def test_same_key_retry_after_submit_worker_sigkill(monkeypatch, tmp_path):
    if not os.environ.get("UPGRADE_TEST_DATABASE_URL"):
        if os.environ.get("UPGRADE_TEST_REQUIRE") == "1":
            pytest.fail("UPGRADE_TEST_DATABASE_URL missing while UPGRADE_TEST_REQUIRE=1")
        pytest.skip("set UPGRADE_TEST_DATABASE_URL for the process crash test")
    if "spawn" not in multiprocessing.get_all_start_methods():
        pytest.skip("requires multiprocessing spawn and SIGKILL")
    dsn = resolve_test_database_url()
    with connect(dsn) as conn:
        apply_schema(conn)

    media = tmp_path / "photos"
    media.mkdir()
    key = f"ROUTE-SIGKILL-{uuid4().hex}"
    known_ids = set()
    image = io.BytesIO()
    Image.new("RGB", (64, 64)).save(image, format="JPEG")
    photo = image.getvalue()
    ctx = multiprocessing.get_context("spawn")
    monkeypatch.setenv("MOCK_AI", "1")
    monkeypatch.setenv("ENVIRONMENT", "test")

    @contextmanager
    def worker(crash=False):
        parent_end, child_end = ctx.Pipe()
        with parent_end, child_end:
            process = ctx.Process(
                target=_run_submit_worker,
                args=(dsn, str(media), str(tmp_path), key, photo, parent_end, child_end, crash),
            )
            process.start()
            try:
                child_end.close()
                assert parent_end.poll(30), "submit worker did not reach the barrier or respond"
                try:
                    message = parent_end.recv()
                except EOFError:
                    pytest.fail("submit worker exited before reaching the barrier or responding")
                yield process, message
            finally:
                if process.is_alive():
                    process.kill()
                process.join(5)
                assert not process.is_alive(), "submit worker did not exit after SIGKILL"
                process.close()

    def media_ids():
        return known_ids | {path.name for path in media.iterdir()}

    def submission_rows(conn):
        return conn.execute(
            "SELECT submission_id, status FROM survey_submissions "
            "WHERE submission_id = ANY(%s) OR form_data->>'idempotency_key' = %s",
            (list(media_ids()), key),
        ).fetchall()

    def committed_rows():
        with connect(dsn) as conn:
            submissions = submission_rows(conn)
            attempts = conn.execute(
                "SELECT state, submission_id, response_json FROM submission_idempotency "
                "WHERE idempotency_key = %s",
                (key,),
            ).fetchall()
            events = conn.execute(
                "SELECT aggregate_id, flow_target FROM outbox_events WHERE aggregate_id = ANY(%s)",
                (list(media_ids() | {sid for sid, _ in submissions}),),
            ).fetchall()
        return submissions, attempts, events

    try:
        with worker(crash=True) as (process, message):
            assert message[0] == "before commit", message
            crashed_id = message[1]
            known_ids.add(crashed_id)
            (orphan,) = (media / crashed_id).iterdir()
            orphan_bytes = orphan.read_bytes()
            assert orphan_bytes
            assert committed_rows() == ([], [], [])
            process.kill()
            process.join(5)
            assert process.exitcode == -signal.SIGKILL

        assert committed_rows() == ([], [], [])
        with worker() as (process, (kind, status, body)):
            assert kind == "response"
            process.join(5)
            assert process.exitcode == 0
        assert status == 200, body
        payload = json.loads(body)
        sid = payload["submission_id"]
        known_ids.add(sid)
        assert sid != crashed_id
        assert committed_rows() == ([(sid, payload["status"])], [("COMMITTED", sid, payload)], [(sid, "ALL")])
        assert orphan.read_bytes() == orphan_bytes
    finally:
        with connect(dsn) as conn:
            ids = list(media_ids() | {sid for sid, _ in submission_rows(conn)})
            conn.execute("DELETE FROM outbox_events WHERE aggregate_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM survey_submissions WHERE submission_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM submission_idempotency WHERE idempotency_key = %s", (key,))
