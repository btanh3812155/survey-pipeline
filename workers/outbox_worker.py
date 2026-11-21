import asyncio
import logging
import os
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbox_worker")

try:
    DRIVE_SYNC_INTERVAL_SEC = float(os.environ.get("DRIVE_SYNC_INTERVAL_SEC", "900"))
except ValueError:
    DRIVE_SYNC_INTERVAL_SEC = 900.0


async def process_outbox_batch(
    conn_str: str, batch_size: int = 2, flow_target: Optional[str] = None
) -> int:
    """Process keyed durable work only; leave legacy ALL for reconciliation."""
    from core.outbox_delivery import process

    return await process(conn_str, batch_size, flow_target)


async def run_outbox_daemon(conn_str: str, poll_interval_sec: float = 10.0):
    from config import settings
    from core.outbox_delivery import worker_health, record_worker_poll

    if not settings.OUTBOX_V2_WORKER_ENABLED:
        logger.info("outbox v2 claims disabled (Wave 4A; producers remain legacy)")
        await asyncio.Event().wait()
        return
    health = await worker_health(conn_str)
    if not health["ready"]:
        raise RuntimeError("outbox migration 002 is required before worker activation")
    logger.info("outbox v2 ready; unreconciled legacy work=%s", health["legacy_unreconciled"])
    while True:
        try:
            count = await process_outbox_batch(conn_str)
            await record_worker_poll(conn_str)
        except Exception as exc:
            logger.warning("outbox poll failed: %s", type(exc).__name__)
            count = 0
        if count == 0:
            await asyncio.sleep(poll_interval_sec)


async def run_drive_sync_daemon(conn_str: str, interval_sec: float = DRIVE_SYNC_INTERVAL_SEC):
    """Schedule one durable Drive job per UTC interval, or run the legacy sync."""
    if interval_sec <= 0:
        logger.info("[DRIVE SYNC] periodic sync disabled (DRIVE_SYNC_INTERVAL_SEC=0)")
        return

    from psycopg_pool import AsyncConnectionPool
    from config import settings
    from core.durable_work import queue_drive_sync
    from services.google_drive_delivery import (
        DRIVE_SYNC_LOCK_KEY,
        has_google_credentials,
        sync_all_approved_to_drive,
    )

    pool = AsyncConnectionPool(conn_str, min_size=1, max_size=3, open=False)
    await pool.open()
    logger.info(f"[DRIVE SYNC] periodic sync every {interval_sec:g}s started")
    try:
        while True:
            try:
                if not has_google_credentials():
                    logger.debug("[DRIVE SYNC] skipped: no Google credentials configured")
                elif settings.DURABLE_DRIVE_ENABLED:
                    async with pool.connection() as conn:
                        cur = await conn.execute(
                            "SELECT pg_try_advisory_xact_lock(%s), "
                            "floor(EXTRACT(EPOCH FROM clock_timestamp()) / %s::numeric)::bigint",
                            (DRIVE_SYNC_LOCK_KEY, interval_sec),
                        )
                        acquired, period = await cur.fetchone()
                        if acquired:
                            await queue_drive_sync(
                                conn, action_key=f"drive-periodic:{float(interval_sec)!r}:{period}"
                            )
                            await conn.commit()
                else:
                    ok = await sync_all_approved_to_drive(pool)
                    logger.info(f"[DRIVE SYNC] periodic run finished ok={ok}")
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
    import argparse
    from config import settings
    from core.outbox_delivery import worker_health

    parser = argparse.ArgumentParser(description="Compatible durable outbox worker (disabled by default)")
    parser.add_argument("--health", action="store_true", help="Check schema without claiming work")
    args = parser.parse_args()
    if args.health:
        health = asyncio.run(worker_health(settings.DATABASE_URL))
        import json
        print(json.dumps(health))
        raise SystemExit(0 if health["ready"] else 1)
    asyncio.run(run_all_service_loops(settings.DATABASE_URL))
