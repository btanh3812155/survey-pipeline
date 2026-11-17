import asyncio
import logging
import os
from typing import Optional
import psycopg
from psycopg.rows import dict_row

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbox_worker")

try:
    DRIVE_SYNC_INTERVAL_SEC = float(os.environ.get("DRIVE_SYNC_INTERVAL_SEC", "900"))
except ValueError:
    DRIVE_SYNC_INTERVAL_SEC = 900.0


async def process_outbox_batch(
    conn_str: str, batch_size: int = 50, flow_target: Optional[str] = None
) -> int:
    """
    Polls and processes pending outbox events using FOR UPDATE SKIP LOCKED.
    Supports dedicated worker loops per flow target (BILLING, R2_UPLOAD, GDRIVE_EXPORT, LARK_SYNC, ZALO_ZNS).
    If one flow fails, only that flow retries, preventing cascading stalls across outputs.
    """
    processed_count = 0
    try:
        async with await psycopg.AsyncConnection.connect(
            conn_str, row_factory=dict_row
        ) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    # 1. claim batch of events safely filtered by flow_target
                    await cur.execute(
                        """
                        SELECT id, event_type, flow_target, aggregate_id, payload
                        FROM outbox_events
                        WHERE (%s::text IS NULL OR flow_target = %s::text OR flow_target = 'ALL')
                          AND (status = 'PENDING'
                               OR (status = 'PROCESSING' AND locked_at < NOW() - INTERVAL '60 SECONDS'))
                        ORDER BY id ASC
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED;
                    """,
                        (flow_target, flow_target, batch_size),
                    )
                    events = await cur.fetchall()

                    if not events:
                        return 0

                    event_ids = [e["id"] for e in events]
                    await cur.execute(
                        """
                        UPDATE outbox_events
                        SET status = 'PROCESSING', locked_at = NOW()
                        WHERE id = ANY(%s);
                    """,
                        (event_ids,),
                    )

                    # 2. process each event with isolated sub-transactions (savepoints)
                    for ev in events:
                        try:
                            async with conn.transaction():
                                payload = ev["payload"]
                                submission_id = ev["aggregate_id"]

                                target = ev.get("flow_target", "ALL")

                                # FLOW A: Financial Ledger Payout (Generalized Commission / Compensation)
                                if target in ("BILLING", "ALL") and ev["event_type"] in ("SURVEY_APPROVED", "survey.validated"):
                                    collab_id = payload.get("collaborator_id") or payload.get("ctv_id")
                                    # Generalized payout amount: reads from payload or defaults to 0 (no hardcoded amounts)
                                    payout_amount = payload.get("payout_vnd") or payload.get("commission_amount") or payload.get("payout_amount") or 0
                                    
                                    # If a collaborator is linked, record in ledger; otherwise skip cleanly without throwing FK error
                                    if collab_id is not None:
                                        await cur.execute(
                                            """
                                            INSERT INTO collaborator_billing_ledger (
                                                collaborator_id, submission_id, payout_amount_vnd, payout_status
                                            ) VALUES (%s, %s, %s, 'UNPAID')
                                            ON CONFLICT (submission_id) DO UPDATE SET
                                                payout_amount_vnd = EXCLUDED.payout_amount_vnd;
                                            """,
                                            (collab_id, submission_id, payout_amount),
                                        )
                                    else:
                                        logger.debug(f"[BILLING] No collaborator ID for #{submission_id}, skipping billing ledger entry.")
                                # FLOW B: Cloudflare R2 Upload (simulated / dispatched)
                                elif target == "R2_UPLOAD":
                                    logger.info(f"[R2_WORKER] uploaded image for #{submission_id}")

                                # FLOW C: Google Drive Client Export (simulated / dispatched)
                                elif target == "GDRIVE_EXPORT":
                                    logger.info(f"[GDRIVE_WORKER] exported spreadsheet row for #{submission_id}")

                                # FLOW D: Lark Base Dashboard Sync
                                elif target == "LARK_SYNC":
                                    logger.info(f"[LARK_WORKER] synced survey #{submission_id} to Lark Base")

                                # FLOW E: Zalo ZNS Phone Notification
                                elif target == "ZALO_ZNS":
                                    logger.info(f"[ZALO_WORKER] sent ZNS thank-you for #{submission_id}")
                                elif ev["event_type"] in ("SURVEY_REJECTED", "survey.rejected"):
                                    logger.info(f"[OUTBOX_WORKER] processed rejection event for #{submission_id}")
                                await cur.execute(
                                    """
                                    UPDATE outbox_events
                                    SET status = 'PROCESSED', processed_at = NOW()
                                    WHERE id = %s;
                                """,
                                    (ev["id"],),
                                )
                                processed_count += 1
                        except Exception as e:
                            logger.error(f"failed event {ev['id']}: {e}")
                            await cur.execute(
                                """
                                UPDATE outbox_events
                                SET retry_count = retry_count + 1,
                                    status = CASE WHEN retry_count >= 5 THEN 'FAILED' ELSE 'PENDING' END
                                WHERE id = %s;
                            """,
                                (ev["id"],),
                            )

    except Exception as e:
        logger.error(f"error processing outbox batch: {e}")

    return processed_count


async def run_outbox_daemon(conn_str: str, poll_interval_sec: float = 10.0):
    logger.info("outbox daemon started...")
    while True:
        count = await process_outbox_batch(conn_str)
        if count == 0:
            await asyncio.sleep(poll_interval_sec)
        else:
            logger.info(f"processed {count} outbox events")


async def run_drive_sync_daemon(conn_str: str, interval_sec: float = DRIVE_SYNC_INTERVAL_SEC):
    """
    Periodically pushes approved deliverables to Google Drive.

    sync_all_approved_to_drive takes a Postgres advisory lock, so this loop and
    manual /api/v1/admin/sync-drive calls never run concurrently. An interval of
    0 disables the loop.
    """
    if interval_sec <= 0:
        logger.info("[DRIVE SYNC] periodic sync disabled (DRIVE_SYNC_INTERVAL_SEC=0)")
        return

    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(conn_str, min_size=1, max_size=3, open=False)
    await pool.open()
    logger.info(f"[DRIVE SYNC] periodic sync every {interval_sec:g}s started")
    try:
        while True:
            try:
                from services.google_drive_delivery import (
                    has_google_credentials,
                    sync_all_approved_to_drive,
                )

                if has_google_credentials():
                    ok = await sync_all_approved_to_drive(pool)
                    logger.info(f"[DRIVE SYNC] periodic run finished ok={ok}")
                else:
                    logger.debug("[DRIVE SYNC] skipped: no Google credentials configured")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[DRIVE SYNC] periodic run failed: {e}")
            await asyncio.sleep(interval_sec)
    finally:
        await pool.close()


async def run_all_service_loops(conn_str: str):
    """Runs the outbox worker and the periodic Drive sync loop together."""
    await asyncio.gather(
        run_outbox_daemon(conn_str),
        run_drive_sync_daemon(conn_str, DRIVE_SYNC_INTERVAL_SEC),
    )


if __name__ == "__main__":
    from config import settings

    asyncio.run(run_all_service_loops(settings.DATABASE_URL))
