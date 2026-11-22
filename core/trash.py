"""Database-side deletion trash, retention tiers, transaction audit, and Drive name/ID ledger."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from psycopg.rows import dict_row

logger = logging.getLogger("core.trash")

LAST_SNAPSHOTS_KEPT = 10
RETENTION_FREQUENCIES = {
    "hourly": "0-24 hours: one snapshot per entity per hour",
    "four_hourly": "1-7 days: one snapshot per entity per 4 hours",
    "daily": "7-30 days: one snapshot per entity per day",
    "weekly": "30-90 days: one snapshot per entity per week",
    "monthly": "90-365 days: one snapshot per entity per month",
    "semiannual": "over 365 days: one snapshot per entity per 6 months, forever",
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
) -> int:
    """Upsert Drive name/ID links and audit the Drive trash transaction."""
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
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'TRASHED',%s,%s::jsonb)
                       ON CONFLICT(drive_id) DO UPDATE SET
                         entity_type=EXCLUDED.entity_type, entity_id=EXCLUDED.entity_id,
                         object_name=EXCLUDED.object_name, parent_drive_id=EXCLUDED.parent_drive_id,
                         parent_name=EXCLUDED.parent_name, mime_type=EXCLUDED.mime_type,
                         target_id=EXCLUDED.target_id, state='TRASHED',
                         last_seen_at=clock_timestamp(), last_action=EXCLUDED.last_action,
                         metadata=drive_name_ledger.metadata || EXCLUDED.metadata""",
                    (
                        entity_type, entity_id, str(entry["name"]), str(entry["id"]),
                        entry.get("parent_id"), entry.get("parent_name"),
                        entry.get("mime_type"), entry.get("target_id"), action[:32],
                        json.dumps({"trash_id": trash_id} if trash_id else {}),
                    ),
                )
            await record_transaction(
                cur, trash_id, action, actor,
                {"entity_type": entity_type, "entity_id": entity_id, "items": len(entries)},
            )
    return len(entries)


async def compact_trash(pool, now: datetime | None = None) -> dict[str, int]:
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
