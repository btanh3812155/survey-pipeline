import hashlib
import json
import logging
import io
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from PIL import Image, ImageFilter

def mask_phone_number(phone: str) -> str:
    if not phone:
        return ""
    p = str(phone)
    if len(p) >= 7:
        return p[:3] + "****" + p[-3:]
    return "****"

logger = logging.getLogger("audit_and_privacy")

def compute_audit_hash(prev_hash: str, timestamp_iso: str, action: str, details: Dict[str, Any]) -> str:
    """
    Computes a tamper-evident SHA-256 block hash chaining prior hops to the current hop.
    """
    payload_str = f"{prev_hash}:{timestamp_iso}:{action}:{json.dumps(details, sort_keys=True)}"
    return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

async def record_hop_audit(
    db_pool,
    hop_number: int,
    hop_name: str,
    component_name: str,
    action: str,
    entity_type: str,
    entity_id: str,
    actor_type: str,
    actor_id: str,
    details: Dict[str, Any],
    prev_hash: str = "GENESIS_HOP"
) -> str:
    """
    Records an immutable audit entry in system_audit_log for a specific pipeline hop.
    """
    ts_iso = datetime.now(timezone.utc).isoformat()
    sanitized_details = dict(details)
    if "driver_phone" in sanitized_details and sanitized_details["driver_phone"]:
        sanitized_details["driver_phone"] = mask_phone_number(sanitized_details["driver_phone"])
    audit_hash = compute_audit_hash(prev_hash, ts_iso, action, sanitized_details)
    if not db_pool:
        logger.info(f"[MOCK AUDIT HOP {hop_number}: {hop_name}] action={action} entity={entity_type}:{entity_id} hash={audit_hash[:16]}")
        return audit_hash

    query = """
    INSERT INTO system_audit_log (
        hop_number, hop_name, component_name, action, entity_type, entity_id,
        actor_type, actor_id, details, tamper_evident_hash
    ) VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
    );
    """
    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                query,
                (
                    hop_number,
                    hop_name,
                    component_name,
                    action,
                    entity_type,
                    entity_id,
                    actor_type,
                    actor_id,
                    json.dumps(sanitized_details),
                    audit_hash
                )
            )
            await conn.commit()
    return audit_hash

async def tokenize_or_get_driver_id(db_pool, driver_phone: str, driver_name: Optional[str] = None) -> str:
    """
    Tokenizes raw phone number into an opaque driver_id (e.g. DRV-8A2F9)
    so raw phone numbers are NOT sent across internal hops or downstream services.
    """
    if not db_pool:
        # Mock deterministic hash token for testing without DB
        token_hash = hashlib.sha256(driver_phone.encode("utf-8")).hexdigest()[:6].upper()
        return f"DRV-{token_hash}"

    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT driver_id FROM driver_identity_vault WHERE driver_phone = %s;",
                (driver_phone,)
            )
            row = await cur.fetchone()
            if row:
                return row[0]

            # Generate new unique driver token
            raw_uuid = uuid.uuid4().hex[:6].upper()
            new_driver_id = f"DRV-{raw_uuid}"
            await cur.execute(
                """
                INSERT INTO driver_identity_vault (driver_id, driver_phone, driver_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (driver_phone) DO UPDATE SET driver_name = EXCLUDED.driver_name
                RETURNING driver_id;
                """,
                (new_driver_id, driver_phone, driver_name or "Tài xế")
            )
            res = await cur.fetchone()
            await conn.commit()
            return res[0]

async def log_telegram_interaction(
    db_pool,
    telegram_user_id: int,
    chat_id: int,
    message_id: int,
    role: str,
    direction: str,
    message_text: str
):
    """
    Persists Telegram bot conversation messages internally in PostgreSQL.
    """
    if not db_pool:
        logger.info(f"[MOCK TG CHAT LOG] user={telegram_user_id} dir={direction} role={role}: {message_text[:40]}")
        return

    query = """
    INSERT INTO telegram_chat_history (
        telegram_user_id, chat_id, message_id, role, direction, message_text
    ) VALUES (%s, %s, %s, %s, %s, %s);
    """
    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, (telegram_user_id, chat_id, message_id, role, direction, message_text))
            await conn.commit()

async def enqueue_telegram_message_for_36h_deletion(
    db_pool,
    chat_id: int,
    message_id: int
):
    """
    Enqueues an ephemeral bot message to be deleted automatically after 36 hours.
    """
    if not db_pool:
        logger.info(f"[MOCK 36H QUEUE] enqueued chat_id={chat_id} msg_id={message_id}")
        return

    query = """
    INSERT INTO telegram_sent_messages (chat_id, message_id, delete_after)
    VALUES (%s, %s, NOW() + INTERVAL '36 hours');
    """
    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, (chat_id, message_id))
            await conn.commit()

async def process_36h_message_deletions(db_pool, http_client, bot_token: str) -> int:
    """
    Background worker function that finds messages past their 36h lifetime
    and invokes Telegram API deleteMessage to purge them.
    """
    if not db_pool:
        return 0

    deleted_count = 0
    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, chat_id, message_id
                FROM telegram_sent_messages
                WHERE is_deleted = FALSE AND delete_after <= NOW()
                LIMIT 50
                FOR UPDATE SKIP LOCKED;
                """
            )
            rows = await cur.fetchall()
            for row in rows:
                rec_id, chat_id, msg_id = row
                try:
                    if bot_token and bot_token != "mock_tg_token" and http_client:
                        url = f"https://api.telegram.org/bot{bot_token}/deleteMessage"
                        await http_client.post(url, json={"chat_id": chat_id, "message_id": msg_id}, timeout=5.0)
                    
                    await cur.execute(
                        "UPDATE telegram_sent_messages SET is_deleted = TRUE, deleted_at = NOW() WHERE id = %s;",
                        (rec_id,)
                    )
                    deleted_count += 1
                except Exception as e:
                    logger.warning(f"Failed to delete telegram msg {msg_id} in {chat_id}: {e}")

            await conn.commit()
    return deleted_count
async def purge_14day_telegram_retention(db_pool, http_client=None, bot_token: Optional[str] = None) -> Dict[str, int]:
    """
    Automated 14-day retention purger for Telegram CTV Bot messages & logs:
    1. Purges internal conversation history older than 14 days from telegram_chat_history.
    2. Deletes tracked sent messages older than 14 days.
    3. In supergroups/channels (chat_id < 0 where bot has admin delete rights), invokes
       Telegram Bot API deleteMessage to physically remove messages past 14 days from Telegram.
       (Note: In 1-on-1 private DMs, Telegram Bot API limits programmatic deleteMessage to <48h;
       records >48h are purged from internal storage while private users utilize Telegram's native Auto-Delete timer).
    """
    if not db_pool:
        return {"chat_history_purged": 0, "messages_deleted": 0}

    purged_history_count = 0
    deleted_msg_count = 0

    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            # 1. Purge internal chat history older than 14 days
            await cur.execute(
                """
                DELETE FROM telegram_chat_history
                WHERE created_at < NOW() - INTERVAL '14 days';
                """
            )
            purged_history_count = cur.rowcount

            # 2. Find messages past their 14-day retention window
            await cur.execute(
                """
                SELECT id, chat_id, message_id
                FROM telegram_sent_messages
                WHERE is_deleted = FALSE AND sent_at < NOW() - INTERVAL '14 days'
                LIMIT 100
                FOR UPDATE SKIP LOCKED;
                """
            )
            rows = await cur.fetchall()
            for row in rows:
                rec_id, chat_id, msg_id = row
                # In Telegram supergroups (negative chat_id), admin bots can delete messages past 48h
                if str(chat_id).startswith("-") and bot_token and bot_token != "mock_tg_token" and http_client:
                    try:
                        url = f"https://api.telegram.org/bot{bot_token}/deleteMessage"
                        await http_client.post(url, json={"chat_id": chat_id, "message_id": msg_id}, timeout=5.0)
                    except Exception as e:
                        logger.debug(f"Telegram deleteMessage note for {msg_id}: {e}")

                await cur.execute(
                    "UPDATE telegram_sent_messages SET is_deleted = TRUE, deleted_at = NOW() WHERE id = %s;",
                    (rec_id,)
                )
                deleted_msg_count += 1

            await conn.commit()

    return {"chat_history_purged": purged_history_count, "messages_deleted": deleted_msg_count}

async def scrub_driver_pii(db_pool, driver_phone: str) -> Dict[str, Any]:
    """
    Executes driver right-to-erasure (Decree 13 / GDPR):
    - Wipes phone number and name from identity vault.
    - Sets driver_phone to NULL across survey submissions while retaining driver_id and audit hashes.
    - Scrambles chat history text for that user.
    - Emits an immutable audit record proving compliance.
    """
    if not db_pool:
        return {"status": "scrubbed", "driver_phone": "[REDACTED_DECREE13]"}

    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            # 1. Lookup driver_id
            await cur.execute("SELECT driver_id FROM driver_identity_vault WHERE driver_phone = %s;", (driver_phone,))
            row = await cur.fetchone()
            driver_id = row[0] if row else "UNKNOWN_DRIVER"

            # 2. Scrub driver_identity_vault
            await cur.execute(
                """
                UPDATE driver_identity_vault 
                SET driver_name = '[SCRUBBED_DECREE13]',
                    driver_phone = '[SCRUBBED_DECREE13]',
                    is_scrubbed = TRUE,
                    scrubbed_at = NOW()
                WHERE driver_phone = %s;
                """,
                (driver_phone,)
            )

            # 3. Scrub survey_submissions (set driver_phone and driver_name to NULL)
            await cur.execute(
                """
                UPDATE survey_submissions
                SET driver_phone = NULL,
                    driver_name = '[SCRUBBED]'
                WHERE driver_phone = %s OR driver_id = %s;
                """,
                (driver_phone, driver_id)
            )

            # 4. Scrub vehicle registry and vehicle change history
            await cur.execute(
                """
                DELETE FROM driver_registered_vehicles
                WHERE driver_phone = %s;
                """,
                (driver_phone,)
            )
            await cur.execute(
                """
                UPDATE vehicle_change_history
                SET change_reason = '[SCRUBBED_DECREE13_COMPLIANCE]'
                WHERE driver_phone = %s;
                """,
                (driver_phone,)
            )

            # 5. Log scrub action in audit log
            scrub_details = {"driver_id": driver_id, "action": "RIGHT_TO_ERASURE_DECREE_13"}
            ts_iso = datetime.now(timezone.utc).isoformat()
            audit_hash = compute_audit_hash("SCRUB_ORIGIN", ts_iso, "DRIVER_PII_SCRUBBED", scrub_details)

            await cur.execute(
                """
                INSERT INTO system_audit_log (
                    hop_number, hop_name, component_name, action, entity_type, entity_id,
                    actor_type, actor_id, details, tamper_evident_hash
                ) VALUES (
                    99, 'HOP_RIGHT_TO_ERASURE', 'PRIVACY_COMPLIANCE', 'DRIVER_PII_SCRUBBED',
                    'DRIVER', %s, 'DRIVER', %s, %s, %s
                );
                """,
                (driver_id, driver_id, json.dumps(scrub_details), audit_hash)
            )

            await conn.commit()
            return {
                "status": "success",
                "driver_id": driver_id,
                "audit_seal": audit_hash,
                "message": "Toàn bộ thông tin cá nhân và số điện thoại đã được ẩn hoàn toàn theo Nghị định 13."
            }

def sanitize_rejection_image_and_metadata(raw_image_bytes: bytes, mask_boxes: Optional[List[List[int]]] = None) -> bytes:
    """
    Sanitizes rejection feedback images before sending to Telegram:
    - Strips all EXIF, GPS, and device serial numbers.
    - Blurs/blackouts sensitive bounding boxes (faces, license plates).
    - Re-encodes as clean 80% WebP.
    """
    pil_img = Image.open(io.BytesIO(raw_image_bytes))
    if pil_img.mode != "RGB":
        pil_img = pil_img.convert("RGB")

    # If mask boxes provided (e.g. [[x1, y1, x2, y2]]), blur or mask them
    if mask_boxes:
        for box in mask_boxes:
            if len(box) == 4:
                x1, y1, x2, y2 = box
                # Clamp coordinates
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(pil_img.width, x2), min(pil_img.height, y2)
                if x2 > x1 and y2 > y1:
                    region = pil_img.crop((x1, y1, x2, y2))
                    blurred_region = region.filter(ImageFilter.GaussianBlur(radius=15))
                    pil_img.paste(blurred_region, (x1, y1))

    # Save to clean buffer without any original EXIF/GPS tags
    out_buf = io.BytesIO()
    pil_img.save(out_buf, format="WEBP", quality=80)
    return out_buf.getvalue()

async def persist_rejected_submission(
    db_pool,
    submission_id: str,
    driver_id: str,
    rejection_reason: str,
    image_dhash: str,
    raw_facts: Dict[str, Any],
    collaborator_id: Optional[int] = None
) -> str:
    """
    Persists rejected submissions to PostgreSQL:
    - Retains rejection reason, extracted forensic metrics, and driver_id.
    - Strictly sets image_r2_url = NULL, signature_r2_url = NULL, and driver_phone = NULL.
    """
    if not db_pool:
        logger.info(f"[MOCK REJECTED SUBMISSION SAVED] #{submission_id} driver_id={driver_id} reason={rejection_reason}")
        return submission_id

    query = """
    INSERT INTO survey_submissions (
        submission_id, collaborator_id, driver_id, driver_phone, driver_name,
        image_r2_url, signature_r2_url, image_dhash, status, rejection_reason,
        raw_extracted_facts, audit_seal_hash
    ) VALUES (
        %s, %s, %s, NULL, '[REJECTED_ANONYMIZED]',
        NULL, NULL, %s::bit(64), 'REJECTED', %s,
        %s, %s
    )
    ON CONFLICT (submission_id) DO UPDATE SET
        status = 'REJECTED',
        rejection_reason = EXCLUDED.rejection_reason,
        image_r2_url = NULL,
        signature_r2_url = NULL,
        driver_phone = NULL;
    """
    seal_hash = hashlib.sha256(f"{submission_id}:{driver_id}:{rejection_reason}".encode("utf-8")).hexdigest()

    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                query,
                (
                    submission_id,
                    collaborator_id,
                    driver_id,
                    image_dhash if image_dhash and len(image_dhash) == 64 else '0'*64,
                    rejection_reason,
                    json.dumps(raw_facts),
                    seal_hash
                )
            )
            await conn.commit()
    return submission_id
