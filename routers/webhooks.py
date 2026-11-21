# -*- coding: utf-8 -*-
"""
Webhooks Ingress Router.
Handles external webhook integrations:
1. Bytedance Lark Base webhook verification and handshake.
2. Telegram Bot API push webhooks (Admin moderation bot & Driver support bot).
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
from tempfile import NamedTemporaryFile
from typing import Optional
import httpx
from fastapi import APIRouter, Request, Header, HTTPException, BackgroundTasks, Depends, status
from config import settings, BUILTIN_TELEGRAM_WEBHOOK_SECRET
from core.durable_work import enqueue_action, require_delivery_schema
from core.state import verify_admin_auth, get_redis_client, get_db_pool, vision_client
from services.auth_lark import decrypt_lark_payload, verify_lark_signature, handle_lark_challenge_handshake
from services.normalizer import sanitize_and_normalize_image, compute_laplacian_blur_score
from services.storage_r2 import r2_client
from services.submission_media import build_manifest, private_submission_dir
from services.evaluator import evaluate_survey_compliance
from services.templates_vn import render_telegram_verdict
from routers.survey import send_telegram_notification
from services.telegram_polling import TelegramPollingRunner, DriverBotRunner

logger = logging.getLogger("routers.webhooks")
router = APIRouter(tags=["Webhooks"])

admin_bot_runner: Optional[TelegramPollingRunner] = None
driver_bot_runner: Optional[DriverBotRunner] = None


def get_admin_bot_runner() -> Optional[TelegramPollingRunner]:
    global admin_bot_runner
    if admin_bot_runner is None:
        token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
        if token and token != "mock_tg_token":
            admin_bot_runner = TelegramPollingRunner(bot_token=token, db_pool=get_db_pool(), role="admin")
    return admin_bot_runner


def get_driver_bot_runner() -> Optional[DriverBotRunner]:
    global driver_bot_runner
    if driver_bot_runner is None:
        token = getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None)
        if token and token != "mock_tg_token":
            driver_bot_runner = DriverBotRunner(bot_token=token, db_pool=get_db_pool())
    return driver_bot_runner


def verify_telegram_webhook_secret(secret_token: Optional[str] = None):
    """Authenticate webhook provenance before any body parsing or dispatch."""
    configured = str(getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "") or "").strip()
    if settings.ENVIRONMENT == "production":
        if not configured or configured == BUILTIN_TELEGRAM_WEBHOOK_SECRET:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="telegram webhook authentication is unavailable",
            )
        if not secrets.compare_digest(secret_token or "", configured):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
        return
    if settings.ENVIRONMENT != "development" and configured:
        if not secrets.compare_digest(secret_token or "", configured):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


_LARK_VERDICT = "lark.party_verdict"
_LARK_EVIDENCE = "lark.evidence"


def _lark_replay():
    return HTTPException(status_code=409, detail="Replay attack detected: nonce already used")


async def _lark_receipt(conn, event_id, digest, nonce_hash, *, durable):
    # The flag-off lookup must also work before migration 002 has been applied.
    indexed = " AND aggregate_kind='lark_event' AND action_key='party-verdict-v1'" if durable else ""
    cur = await conn.execute(
        "SELECT payload FROM outbox_events WHERE aggregate_id=%s AND flow_target='TELEGRAM_NOTIFY' "
        "AND event_type=%s" + indexed,
        (event_id, _LARK_VERDICT),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    payload = row["payload"] if isinstance(row, dict) else row[0]
    if payload["event_digest"] != digest:
        raise HTTPException(status_code=409, detail="Lark event identity has a different payload")
    if nonce_hash and nonce_hash == payload.get("nonce_hash"):
        raise _lark_replay()
    return payload["response"]


async def _lark_nonce_used(conn, nonce_hash):
    if nonce_hash is None:
        return False
    cur = await conn.execute(
        "SELECT 1 FROM outbox_events WHERE aggregate_kind='lark_event' AND event_type=%s "
        "AND action_key='party-verdict-v1' AND payload->>'nonce_hash'=%s "
        "AND created_at > NOW() - interval '5 minutes' LIMIT 1",
        (_LARK_VERDICT, nonce_hash),
    )
    return bool(await cur.fetchone())


def _stage_lark_evidence(event_id, content, extension):
    folder = private_submission_dir(event_id)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    name = f"{hashlib.sha256(content).hexdigest()}.{extension}"
    # A cancelled ingress can leave an orphan but cannot expose a partial file.
    with NamedTemporaryFile(dir=folder, delete=False) as evidence:
        try:
            evidence.write(content)
            evidence.flush()
            os.fsync(evidence.fileno())
            os.replace(evidence.name, os.path.join(folder, name))
        finally:
            if os.path.exists(evidence.name):
                os.unlink(evidence.name)
    return build_manifest(event_id, [{"name": name}])


async def _commit_lark_verdict(
    pool, event_id, digest, nonce_hash, chat_id, text, response, evidence, extension, export,
):
    async with pool.connection() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (event_id,))
        existing = await _lark_receipt(conn, event_id, digest, nonce_hash, durable=True)
        if existing is not None:
            return existing
        if nonce_hash:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (nonce_hash,))
            if await _lark_nonce_used(conn, nonce_hash):
                raise _lark_replay()

        manifest = await asyncio.to_thread(_stage_lark_evidence, event_id, evidence, extension) if evidence else []
        role = "admin" if settings.TELEGRAM_ADMIN_BOT_TOKEN else "primary"
        # Lark verdicts are plain text for the outbox Telegram adapter.
        body = re.sub(r"</?(?:b|i|code)>", "", text)[:4096]
        await enqueue_action(conn, event_id, "TELEGRAM_NOTIFY", _LARK_VERDICT, {
            "event_digest": digest,
            "nonce_hash": nonce_hash,
            "response": response,
            "media_version": 1,
            "media_manifest": manifest,
            "policy": {"enabled": chat_id is not None, "version": "lark-v1"},
            "bot_role": role,
            "message_class": "PARTY_VERDICT",
            "chat_id": chat_id,
            "text": body,
        }, aggregate_kind="lark_event", action_key="party-verdict-v1")
        if export:
            bucket = settings.R2_PRIVATE_BUCKET_NAME
            private = bool(bucket and bucket != settings.R2_BUCKET_NAME)
            await enqueue_action(conn, event_id, "R2_UPLOAD", _LARK_EVIDENCE, {
                "event_digest": digest,
                "policy": {"enabled": private, "version": "lark-v1"},
                "destination": {"bucket": bucket if private else None, "private": True},
                "media_version": 1,
                "media_manifest": manifest,
            }, aggregate_kind="lark_event", action_key="evidence-v1")
        await conn.commit()
    return response


@router.post("/webhook/lark-survey")
async def lark_webhook_ingress(
    request: Request,
    background_tasks: BackgroundTasks,
    x_lark_request_timestamp: str = Header(None),
    x_lark_request_nonce: str = Header(None),
    x_lark_signature: str = Header(None),
):
    raw_body = await request.body()

    # 1. handle url_verification challenge handshake immediately
    try:
        is_challenge, challenge_token = handle_lark_challenge_handshake(raw_body, settings.LARK_ENCRYPT_KEY)
    except (AttributeError, TypeError):
        is_challenge, challenge_token = False, None
    if is_challenge:
        return {"challenge": challenge_token}

    durable = settings.DURABLE_LARK_ENABLED
    # 2. cryptographic signature verification
    if settings.ENVIRONMENT != "development":
        if durable and settings.LARK_ENCRYPT_KEY.strip() in ("", "mock_encrypt_key"):
            raise HTTPException(status_code=503, detail="Lark webhook authentication is unavailable")
        is_valid = verify_lark_signature(
            timestamp=x_lark_request_timestamp,
            nonce=x_lark_request_nonce,
            encrypt_key=settings.LARK_ENCRYPT_KEY,
            raw_body=raw_body,
            expected_signature=x_lark_signature,
        )
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid lark signature",
            )

    # Legacy reserves the nonce before processing. Durable ingress caches only
    # after commit so an outage cannot poison a valid retry of an unaccepted event.
    redis_client = get_redis_client()
    nonce_key = None
    if x_lark_request_nonce and redis_client:
        nonce_key = f"lock:lark_nonce:{x_lark_request_nonce}"
        try:
            replay = (
                await redis_client.get(nonce_key) if durable
                else not await redis_client.set(nonce_key, "1", nx=True, ex=300)
            )
            if replay:
                raise _lark_replay()
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"redis nonce caching error: {e}")

    # 3. parse survey event payload
    try:
        data = json.loads(raw_body)
        if isinstance(data, dict) and isinstance(data.get("encrypt"), str):
            data = json.loads(decrypt_lark_payload(settings.LARK_ENCRYPT_KEY, data["encrypt"]))
        if not isinstance(data, dict) or not isinstance(data.get("event", {}), dict):
            raise ValueError("invalid event envelope")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json") from None

    event = data.get("event", {})
    submission_id = event.get("submission_id", "sub_demo_001")
    collaborator_id = event.get("collaborator_tg_id") if durable else event.get("collaborator_tg_id", 123456789)
    header = data.get("header")
    provider_id = header.get("event_id") if isinstance(header, dict) else data.get("uuid")
    stable_id = isinstance(provider_id, str) and bool(provider_id.strip()) and len(provider_id) <= 256
    if durable and not stable_id:
        raise HTTPException(status_code=400, detail="missing Lark event identity")

    async def cache_nonce():
        if durable and nonce_key:
            try:
                await redis_client.set(nonce_key, "1", nx=True, ex=300)
            except Exception:
                logger.warning("Lark nonce cache unavailable after commit")

    if stable_id:
        event_id = "lark_" + hashlib.sha256(provider_id.encode()).hexdigest()[:59]
        try:
            canonical = json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False)
            digest = hashlib.sha256(canonical.encode()).hexdigest()
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid Lark event contents") from None
        nonce_hash = hashlib.sha256(x_lark_request_nonce.encode()).hexdigest() if x_lark_request_nonce else None
        try:
            pool = get_db_pool()
            if pool is None:
                raise RuntimeError("database pool unavailable")
            async with pool.connection() as conn:
                if durable:
                    await require_delivery_schema(conn)
                prior = await _lark_receipt(conn, event_id, digest, nonce_hash, durable=durable)
                if durable and prior is None and await _lark_nonce_used(conn, nonce_hash):
                    raise _lark_replay()
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=503, detail="Lark event storage is unavailable") from None
        if prior is not None:
            await cache_nonce()
            return prior

    async def finish(text, response, evidence=None, extension=None, export=False):
        if not durable:
            await send_telegram_notification(collaborator_id, text)
            if export:
                background_tasks.add_task(r2_client.upload_image, f"{submission_id}.webp", evidence)
            return response
        chat_id = collaborator_id
        if isinstance(chat_id, str) and re.fullmatch(r"[1-9][0-9]{0,15}", chat_id):
            chat_id = int(chat_id)
        if type(chat_id) is not int or not 0 < chat_id <= 2**52:
            chat_id = None
        try:
            response = await _commit_lark_verdict(pool, event_id, digest, nonce_hash, chat_id,
                                                  text, response, evidence, extension, export)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=503, detail="Lark event storage is unavailable") from None
        await cache_nonce()
        return response
    raw_image_bytes = None
    # 4. media sanitization & blur check
    try:
        if "image_base64" in event:
            raw_image_bytes = base64.b64decode(event["image_base64"], validate=durable)
        elif not durable:
            raw_image_bytes = b"MOCK_IMAGE_BYTES"
        webp_bytes, pil_img = sanitize_and_normalize_image(
            raw_image_bytes, max_dimension=settings.IMAGE_MAX_DIMENSION
        )
        is_sharp, blur_variance = compute_laplacian_blur_score(
            pil_img, threshold=settings.BLUR_THRESHOLD
        )
    except Exception:
        msg = render_telegram_verdict(submission_id, "BLURRY", ["ảnh bị hỏng hoặc lỗi định dạng"])
        return await finish(msg, {"status": "rejected", "reason": "UNREADABLE_IMAGE"}, raw_image_bytes, "bin")

    if not is_sharp:
        msg = render_telegram_verdict(submission_id, "BLURRY")
        return await finish(msg, {"status": "rejected", "reason": "BLURRY_IMAGE", "variance": blur_variance},
                            webp_bytes, "webp")

    # 5. dual-model AI vision extraction
    facts = await vision_client.extract_facts(webp_bytes)

    # 6. deterministic python compliance evaluation
    eval_result = evaluate_survey_compliance(facts)

    # 7. Preserve the synchronous verdict; durable provider effects are intents.
    verdict_text = render_telegram_verdict(
        submission_id=submission_id,
        reason_code=eval_result.reason_code,
        missing_fields=eval_result.missing_fields,
        collaborator_note=eval_result.collaborator_note,
    )
    if durable and eval_result.reason_code == "APPROVED":
        verdict_text = verdict_text.split("\n• Thù lao:", 1)[0]

    return await finish(verdict_text, {
        "status": "processed",
        "submission_id": submission_id,
        "verdict": eval_result.reason_code,
    }, webp_bytes, "webp", export=True)


@router.post("/webhook/telegram")
@router.post("/webhook/telegram/admin")
async def telegram_admin_webhook_ingress(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None),
):
    verify_telegram_webhook_secret(x_telegram_bot_api_secret_token)
    body = await request.json()
    runner = get_admin_bot_runner()
    if runner:
        await runner.handle_update(body)
    return {"status": "ok"}


@router.post("/webhook/telegram/driver")
async def telegram_driver_webhook_ingress(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None),
):
    verify_telegram_webhook_secret(x_telegram_bot_api_secret_token)
    body = await request.json()
    runner = get_driver_bot_runner()
    if runner:
        await runner.handle_update(body)
    return {"status": "ok"}


@router.post("/api/v1/telegram/set-webhooks")
async def set_telegram_webhooks(
    domain: Optional[str] = None,
    admin_user: str = Depends(verify_admin_auth),
):
    """Registers production webhooks with Telegram Bot API for both Admin and Driver bots."""
    base = (domain or "https://survey.trustbridgevn.com").rstrip("/")
    sec = str(getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "") or "").strip()
    if settings.ENVIRONMENT == "production" and (not sec or sec == BUILTIN_TELEGRAM_WEBHOOK_SECRET):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="telegram webhook authentication is unavailable",
        )
    results = {}

    admin_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
    driver_token = getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None)

    async with httpx.AsyncClient(timeout=15.0) as client:
        if admin_token and admin_token != "mock_tg_token":
            url = f"https://api.telegram.org/bot{admin_token}/setWebhook"
            payload = {"url": f"{base}/webhook/telegram/admin"}
            if sec:
                payload["secret_token"] = sec
            r = await client.post(url, json=payload)
            results["admin_bot"] = r.json()

        if driver_token and driver_token != "mock_tg_token":
            url = f"https://api.telegram.org/bot{driver_token}/setWebhook"
            payload = {"url": f"{base}/webhook/telegram/driver"}
            if sec:
                payload["secret_token"] = sec
            r = await client.post(url, json=payload)
            results["driver_bot"] = r.json()

    return {"status": "success", "results": results}
