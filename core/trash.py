"""Database-side deletion trash, retention tiers, transaction audit, and Drive name/ID ledger."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from psycopg.rows import dict_row

logger = logging.getLogger("core.trash")

LAST_SNAPSHOTS_KEPT = 10
TRASH_COMPACT_LOCK_KEY = 764539269
RETENTION_FREQUENCIES = {
    "hourly": "0-24 giờ: một bản chụp mỗi thực thể mỗi giờ",
    "four_hourly": "1-7 ngày: một bản chụp mỗi thực thể mỗi 4 giờ",
    "daily": "7-30 ngày: một bản chụp mỗi thực thể mỗi ngày",
    "weekly": "30-90 ngày: một bản chụp mỗi thực thể mỗi tuần",
    "monthly": "90-365 ngày: một bản chụp mỗi thực thể mỗi tháng",
    "semiannual": "trên 365 ngày: một bản chụp mỗi thực thể mỗi 6 tháng, vĩnh viễn",
}


def retention_tier(deleted_at: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    age = now - deleted_at
    if age < timedelta(days=1):
        return "hourly"
    if age < timedelta(days=7):
        return "four_hourly"
    if age < timedelta(days=30):
        return "daily"
    if age < timedelta(days=90):
        return "weekly"
    if age < timedelta(days=365):
        return "monthly"
    return "semiannual"


def _bucket(tier: str, deleted_at: datetime) -> tuple[int, ...]:
    if tier == "hourly":
        return (deleted_at.year, deleted_at.month, deleted_at.day, deleted_at.hour)
    if tier == "four_hourly":
        return (deleted_at.year, deleted_at.month, deleted_at.day, deleted_at.hour // 4)
    if tier == "daily":
        return (deleted_at.year, deleted_at.month, deleted_at.day)
    if tier == "weekly":
        iso = deleted_at.isocalendar()
        return (iso.year, iso.week)
    if tier == "monthly":
        return (deleted_at.year, deleted_at.month)
    return (deleted_at.year, 0 if deleted_at.month <= 6 else 1)


async def add_trash_item(
    cur,
    entity_type: str,
    entity_id: str,
    label: str,
    snapshot: dict[str, Any],
    actor: str,
    *,
    deleted_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Write one trash snapshot and its audit transaction inside the caller's transaction."""
    deleted_at = deleted_at or datetime.now(timezone.utc)
    trash_id = str(uuid.uuid4())
    expires_at = None
    tier = retention_tier(deleted_at)
    await cur.execute(
        """INSERT INTO trash_items
           (id,entity_type,entity_id,label,snapshot,deleted_by,deleted_at,expires_at,retention_tier,metadata)
           VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb)""",
        (
            trash_id, entity_type, entity_id, label,
            json.dumps(snapshot, ensure_ascii=False, default=str),
            str(actor)[:64], deleted_at, expires_at, tier,
            json.dumps(metadata or {}, ensure_ascii=False, default=str),
        ),
    )
    await record_transaction(
        cur, trash_id, "TRASHED", actor,
        {"entity_type": entity_type, "entity_id": entity_id, "retention_tier": tier},
    )
    await cur.execute(
        """INSERT INTO system_audit_log
           (component_name,action,entity_type,entity_id,actor_type,actor_id,details)
           VALUES ('trash',%s,%s,%s,'ADMIN',%s,%s::jsonb)""",
        (
            f"{entity_type.upper()}_TRASHED", entity_type, entity_id,
            str(actor)[:64], json.dumps({"trash_id": trash_id}),
        ),
    )
    return trash_id


async def record_transaction(
    cur, trash_id: str | None, action: str, actor: str, details: dict[str, Any]
) -> None:
    await cur.execute(
        "INSERT INTO trash_transactions(trash_id,action,actor,details) VALUES (%s,%s,%s,%s::jsonb)",
        (trash_id, action[:32], str(actor)[:64], json.dumps(details, ensure_ascii=False, default=str)),
    )


async def update_trash_metadata(
    pool, trash_id: str, patch: dict[str, Any], actor: str, action: str = "METADATA_UPDATED"
) -> None:
    if not pool or not trash_id:
        return
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                "UPDATE trash_items SET metadata = metadata || %s::jsonb WHERE id=%s",
                (json.dumps(patch, ensure_ascii=False, default=str), trash_id),
            )
            await record_transaction(cur, trash_id, action, actor, patch)


async def record_drive_ledger(
    pool,
    entity_type: str,
    entity_id: str,
    entries: Iterable[dict[str, Any]],
    actor: str,
    *,
    trash_id: str | None = None,
    action: str = "DRIVE_TRASHED",
    state: str = "TRASHED",
) -> int:
    """Upsert Drive name/ID links and audit the Drive ledger transaction."""
    state = state if state in {"ACTIVE", "TRASHED", "PURGED"} else "TRASHED"
    entries = [entry for entry in entries if entry.get("id") and entry.get("name")]
    if not pool or not entries:
        return 0
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor() as cur:
            for entry in entries:
                await cur.execute(
                    """INSERT INTO drive_name_ledger
                       (entity_type,entity_id,object_name,drive_id,parent_drive_id,parent_name,
                        mime_type,target_id,state,last_action,metadata)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                       ON CONFLICT(drive_id) DO UPDATE SET
                         entity_type=EXCLUDED.entity_type, entity_id=EXCLUDED.entity_id,
                         object_name=EXCLUDED.object_name, parent_drive_id=EXCLUDED.parent_drive_id,
                         parent_name=EXCLUDED.parent_name, mime_type=EXCLUDED.mime_type,
                         target_id=EXCLUDED.target_id, state=EXCLUDED.state,
                         last_seen_at=clock_timestamp(), last_action=EXCLUDED.last_action,
                         metadata=drive_name_ledger.metadata || EXCLUDED.metadata""",
                    (
                        entity_type, entity_id, str(entry["name"]), str(entry["id"]),
                        entry.get("parent_id"), entry.get("parent_name"),
                        entry.get("mime_type"), entry.get("target_id"), state,
                        action[:32],
                        json.dumps({"trash_id": trash_id} if trash_id else {}),
                    ),
                )
            await record_transaction(
                cur, trash_id, action, actor,
                {"entity_type": entity_type, "entity_id": entity_id, "items": len(entries)},
            )
    return len(entries)


async def _compact_trash_unlocked(pool, now: datetime | None = None) -> dict[str, int]:
    """Keep the last 10 snapshots plus one representative per retention bucket."""
    now = now or datetime.now(timezone.utc)
    if not pool:
        return {"kept": 0, "removed": 0, "protected_recent": 0}
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT id,entity_type,entity_id,deleted_at
                   FROM trash_items
                   WHERE restored_at IS NULL AND purged_at IS NULL
                   ORDER BY entity_type,entity_id,deleted_at DESC,id DESC"""
            )
            rows = await cur.fetchall()
            groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for row in rows:
                groups.setdefault((row["entity_type"], row["entity_id"]), []).append(row)
            kept = removed = protected_recent = 0
            for (entity_type, entity_id), items in groups.items():
                protected = {str(item["id"]) for item in items[:LAST_SNAPSHOTS_KEPT]}
                protected_recent += len(protected)
                seen_buckets: set[tuple[int, ...]] = set()
                for item in items:
                    item_id = str(item["id"])
                    tier = retention_tier(item["deleted_at"], now)
                    if item_id in protected:
                        await cur.execute(
                            "UPDATE trash_items SET retention_tier=%s,expires_at=NULL WHERE id=%s",
                            (tier, item_id),
                        )
                        kept += 1
                        continue
                    bucket = _bucket(tier, item["deleted_at"])
                    if bucket in seen_buckets:
                        await cur.execute("DELETE FROM trash_items WHERE id=%s", (item_id,))
                        removed += 1
                        continue
                    seen_buckets.add(bucket)
                    await cur.execute(
                        "UPDATE trash_items SET retention_tier=%s,expires_at=NULL WHERE id=%s",
                        (tier, item_id),
                    )
                    kept += 1
            if removed:
                await record_transaction(
                    cur, None, "TRASH_COMPACTED", "system",
                    {"kept": kept, "removed": removed, "protected_recent": protected_recent},
                )
                await cur.execute(
                    """INSERT INTO system_audit_log
                       (component_name,action,entity_type,entity_id,actor_type,actor_id,details)
                       VALUES ('trash','TRASH_COMPACTED','TRASH','retention','SYSTEM','system',%s::jsonb)""",
                    (json.dumps({"kept": kept, "removed": removed, "protected_recent": protected_recent}),),
                )
            return {"kept": kept, "removed": removed, "protected_recent": protected_recent}



async def compact_trash(pool, now: datetime | None = None) -> dict[str, Any]:
    """Run one compaction under a session advisory lock."""
    if not pool:
        return {"status": "unavailable", "kept": 0, "removed": 0, "protected_recent": 0}
    async with pool.connection() as lock_conn:
        lock_cur = await lock_conn.execute(
            "SELECT pg_try_advisory_lock(%s)", (TRASH_COMPACT_LOCK_KEY,)
        )
        acquired = (await lock_cur.fetchone())[0]
        if not acquired:
            return {"status": "busy", "kept": 0, "removed": 0, "protected_recent": 0}
        try:
            report = await _compact_trash_unlocked(pool, now)
            return {"status": "ok", **report}
        finally:
            try:
                await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (TRASH_COMPACT_LOCK_KEY,))
            except Exception:
                pass


async def run_compaction(pool, actor: str = "system") -> dict[str, Any]:
    """Run compaction and persist the last-run report."""
    if not pool:
        return {"status": "unavailable", "run_id": None}
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO trash_compaction_runs(status) VALUES ('running') RETURNING id"
            )
            run_id = (await cur.fetchone())[0]
    report: dict[str, Any] = {"status": "failed", "kept": 0, "removed": 0, "protected_recent": 0}
    try:
        report = await compact_trash(pool)
    except Exception as exc:
        report = {"status": "failed", "kept": 0, "removed": 0, "protected_recent": 0,
                  "error": type(exc).__name__}
        logger.warning("trash compaction failed: %s", type(exc).__name__)
    status = str(report.get("status") or "failed")
    if status not in {"ok", "busy", "failed"}:
        status = "failed"
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                """UPDATE trash_compaction_runs
                   SET finished_at=clock_timestamp(),status=%s,kept=%s,removed=%s,
                       protected_recent=%s,error=%s,details=%s::jsonb
                   WHERE id=%s""",
                (
                    status, int(report.get("kept") or 0), int(report.get("removed") or 0),
                    int(report.get("protected_recent") or 0), report.get("error"),
                    json.dumps({"actor": actor[:64]}), run_id,
                ),
            )
            await record_transaction(
                cur, None, "TRASH_COMPACTION_RUN", actor,
                {"run_id": run_id, **{k: report.get(k) for k in ("status", "kept", "removed", "protected_recent", "error")}},
            )
    return {"run_id": run_id, **report}


async def last_compaction(pool) -> dict[str, Any] | None:
    if not pool:
        return None
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT id,started_at,finished_at,status,kept,removed,protected_recent,error,details
                   FROM trash_compaction_runs ORDER BY id DESC LIMIT 1"""
            )
            return await cur.fetchone()


async def trash_compaction_loop(pool, interval_sec: int, initial_delay_sec: int) -> None:
    if not pool or interval_sec <= 0:
        logger.info("[TRASH] periodic compaction disabled")
        return
    await asyncio.sleep(max(0, initial_delay_sec))
    while True:
        try:
            await run_compaction(pool, actor="scheduler")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[TRASH] compaction loop failed: %s", type(exc).__name__)
        await asyncio.sleep(max(1, interval_sec))


def start_trash_compaction(pool, interval_sec: int, initial_delay_sec: int):
    if not pool or interval_sec <= 0:
        return None
    return asyncio.create_task(trash_compaction_loop(pool, interval_sec, initial_delay_sec))


async def list_trash(pool, limit: int = 200) -> list[dict[str, Any]]:
    if not pool:
        return []
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT id,entity_type,entity_id,label,deleted_by,deleted_at,expires_at,
                          retention_tier,restored_at,purged_at,metadata
                   FROM trash_items
                   WHERE restored_at IS NULL AND purged_at IS NULL
                   ORDER BY deleted_at DESC LIMIT %s""",
                (max(1, min(int(limit), 1000)),),
            )
            return await cur.fetchall()


async def get_trash_item(pool, trash_id: str) -> dict[str, Any] | None:
    if not pool:
        return None
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT id,entity_type,entity_id,label,snapshot,deleted_by,deleted_at,
                          expires_at,retention_tier,restored_at,purged_at,metadata
                   FROM trash_items WHERE id=%s""",
                (trash_id,),
            )
            return await cur.fetchone()


async def mark_restored(
    pool, trash_id: str, actor: str, details: dict[str, Any] | None = None
) -> bool:
    if not pool:
        return False
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                """UPDATE trash_items SET restored_at=clock_timestamp()
                   WHERE id=%s AND restored_at IS NULL AND purged_at IS NULL
                   RETURNING entity_type,entity_id""",
                (trash_id,),
            )
            row = await cur.fetchone()
            if not row:
                return False
            await record_transaction(cur, trash_id, "RESTORED", actor, details or {})
            await cur.execute(
                """INSERT INTO system_audit_log
                   (component_name,action,entity_type,entity_id,actor_type,actor_id,details)
                   VALUES ('trash','TRASH_RESTORED',%s,%s,'ADMIN',%s,%s::jsonb)""",
                (row[0], row[1], str(actor)[:64], json.dumps({"trash_id": trash_id, **(details or {})})),
            )
            return True


async def restore_drive_ledger(
    pool, entity_type: str, entity_id: str, actor: str
) -> dict[str, Any]:
    """Untrash top-level ledger folders and mark the entity's links active."""
    if not pool:
        return {"status": "unavailable", "restored": 0}
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT id,drive_id,object_name,parent_drive_id,state
                   FROM drive_name_ledger WHERE entity_type=%s AND entity_id=%s""",
                (entity_type, entity_id),
            )
            rows = await cur.fetchall()
    if not rows:
        return {"status": "empty", "restored": 0}
    from services.google_drive_delivery import GoogleDriveClient
    import httpx

    drive = GoogleDriveClient()
    http = httpx.AsyncClient(timeout=60.0)
    restored = 0
    try:
        token = await drive.get_token(http)
        if not token:
            return {"status": "no_credentials", "restored": 0}
        ledger_ids = {str(row["drive_id"]) for row in rows}
        roots = [
            row for row in rows
            if row["state"] == "TRASHED" and str(row["parent_drive_id"] or "") not in ledger_ids
        ]
        for row in roots:
            if await drive.untrash_file(http, token, str(row["drive_id"])):
                restored += 1
        if len(roots) and restored != len(roots):
            return {"status": "partial", "restored": restored, "roots": len(roots)}
        async with pool.connection() as conn:
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute(
                    """UPDATE drive_name_ledger
                       SET state='ACTIVE',last_seen_at=clock_timestamp(),last_action='DRIVE_RESTORED'
                       WHERE entity_type=%s AND entity_id=%s""",
                    (entity_type, entity_id),
                )
                await record_transaction(
                    cur, None, "DRIVE_RESTORED", actor,
                    {"entity_type": entity_type, "entity_id": entity_id, "roots": restored},
                )
        return {"status": "ok", "restored": restored}
    except Exception as exc:
        logger.warning("Drive restore failed for %s/%s: %s", entity_type, entity_id, type(exc).__name__)
        return {"status": "error", "restored": restored, "error": type(exc).__name__}
    finally:
        await http.aclose()


async def ensure_form_trash(pool, form_id: str, actor: str = "system") -> str | None:
    """Return an active form trash row, creating one from deleted_forms if needed."""
    if not pool:
        return None
    async with pool.connection() as conn:
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT id FROM trash_items
                   WHERE entity_type='form' AND entity_id=%s
                     AND restored_at IS NULL AND purged_at IS NULL
                   ORDER BY deleted_at DESC LIMIT 1""",
                (form_id,),
            )
            row = await cur.fetchone()
            if row:
                return str(row["id"])
            try:
                await cur.execute(
                    """SELECT title,schema_json,deleted_at FROM deleted_forms
                       WHERE form_id=%s ORDER BY deleted_at DESC,id DESC LIMIT 1""",
                    (form_id,),
                )
                archived = await cur.fetchone()
            except Exception:
                archived = None
            if not archived:
                return None
            return await add_trash_item(
                cur, "form", form_id, str(archived.get("title") or form_id),
                {"form_id": form_id, "title": archived.get("title"), "schema": archived.get("schema_json")},
                actor,
                deleted_at=archived.get("deleted_at"),
                metadata={"source": "archived_export_cleanup"},
            )
