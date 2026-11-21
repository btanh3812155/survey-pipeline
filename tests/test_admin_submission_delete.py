"""Admin submission deletion: outbox cascade, audit, and billing guard."""
import pytest
from psycopg_pool import AsyncConnectionPool

from core.repositories import delete_submission_record
from tests.test_outbox_delivery import database

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_delete_submission_cascades_outbox_and_audits(database):
    dsn, conn, submission, event, _read, _users = database
    sid = submission(status="APPROVED")
    event_id = event("R2_UPLOAD", sid)
    try:
        async with AsyncConnectionPool(dsn, open=False, min_size=1, max_size=2) as pool:
            details = await delete_submission_record(
                sid, "admin", ip_address="127.0.0.1", db_pool=pool
            )
        assert details["submission_id"] == sid
        assert details["outbox_events"] == 1
        assert conn.execute(
            "SELECT 1 FROM survey_submissions WHERE submission_id=%s", (sid,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM outbox_events WHERE id=%s", (event_id,)
        ).fetchone() is None
        audit = conn.execute(
            "SELECT actor_type, details FROM system_audit_log "
            "WHERE entity_id=%s AND action='SUBMISSION_DELETED'",
            (sid,),
        ).fetchone()
        assert audit["actor_type"] == "ADMIN"
        assert audit["details"]["submission_id"] == sid
    finally:
        conn.execute("DELETE FROM system_audit_log WHERE entity_id=%s", (sid,))


async def test_delete_submission_refuses_financial_ledger(database):
    dsn, conn, submission, _event, _read, _users = database
    sid = submission(status="APPROVED")
    conn.execute(
        "INSERT INTO collaborator_billing_ledger(submission_id,payout_amount_vnd,payout_status) "
        "VALUES (%s,0,'UNPAID')",
        (sid,),
    )
    try:
        async with AsyncConnectionPool(dsn, open=False, min_size=1, max_size=2) as pool:
            with pytest.raises(ValueError) as exc:
                await delete_submission_record(sid, "admin", db_pool=pool)
        assert str(exc.value) == "BILLING_RECORD"
        assert conn.execute(
            "SELECT 1 FROM survey_submissions WHERE submission_id=%s", (sid,)
        ).fetchone() is not None
    finally:
        conn.execute("DELETE FROM collaborator_billing_ledger WHERE submission_id=%s", (sid,))
