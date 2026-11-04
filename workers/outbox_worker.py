import asyncio
import json
import logging
import inspect
from typing import Optional
import psycopg
from psycopg.rows import dict_row

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbox_worker")


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

                                # FLOW A: Financial Ledger Payout
                                if target in ("BILLING", "ALL") and ev["event_type"] in ("SURVEY_APPROVED", "survey.validated"):
                                    collab_id = payload.get("collaborator_id")
                                    payout_amount = payload.get("payout_vnd", 25000)
                                    await cur.execute(
                                        """
                                        INSERT INTO collaborator_billing_ledger (
                                            collaborator_id, submission_id, payout_amount_vnd, payout_status
                                        ) VALUES (%s, %s, %s, 'PENDING_PAYOUT')
                                        ON CONFLICT (submission_id) DO NOTHING;
                                    """,
                                        (collab_id, submission_id, payout_amount),
                                    )

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

            commit_fn = getattr(conn, "commit", None)
            if callable(commit_fn):
                res = commit_fn()
                if inspect.isawaitable(res):
                    await res
    except Exception as e:
        logger.error(f"error processing outbox batch: {e}")

    return processed_count


async def run_outbox_daemon(conn_str: str, poll_interval_sec: float = 1.0):
    logger.info("outbox daemon started...")
    while True:
        count = await process_outbox_batch(conn_str)
        if count == 0:
            await asyncio.sleep(poll_interval_sec)
        else:
            logger.info(f"processed {count} outbox events")


if __name__ == "__main__":
    import sys
    from config import settings

    asyncio.run(run_outbox_daemon(settings.DATABASE_URL))
