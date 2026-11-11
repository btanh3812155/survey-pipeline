# -*- coding: utf-8 -*-
"""
Webhooks Ingress Router.
Handles external webhook integrations:
1. Bytedance Lark Base webhook verification and handshake.
2. Telegram Bot API push webhooks (Admin moderation bot & Driver support bot).
"""

import json
import logging
import secrets
from typing import Optional
import httpx
from fastapi import APIRouter, Request, Header, HTTPException, BackgroundTasks, Depends, status
from config import settings
from core.state import verify_admin_auth, get_redis_client, get_db_pool
from services.auth_lark import verify_lark_signature, handle_lark_challenge_handshake
from services.normalizer import sanitize_and_normalize_image, compute_laplacian_blur_score
from services.storage_r2 import r2_client
from core.state import verify_admin_auth, get_redis_client, get_db_pool, vision_client
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
    if settings.ENVIRONMENT != "development" and getattr(settings, "TELEGRAM_WEBHOOK_SECRET", None):
        if not secrets.compare_digest(secret_token or "", settings.TELEGRAM_WEBHOOK_SECRET):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


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
    is_challenge, challenge_token = handle_lark_challenge_handshake(
        raw_body, settings.LARK_ENCRYPT_KEY
    )
    if is_challenge:
        return {"challenge": challenge_token}

    # 2. cryptographic signature verification
    if settings.ENVIRONMENT != "development":
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

    # 2b. replay attack prevention via redis nonce cache
    redis_client = get_redis_client()
    if x_lark_request_nonce and redis_client:
        nonce_key = f"lock:lark_nonce:{x_lark_request_nonce}"
        try:
            is_new = await redis_client.set(nonce_key, "1", nx=True, ex=300)
            if not is_new:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Replay attack detected: nonce already used",
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"redis nonce caching error: {e}")

    # 3. parse survey event payload
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid json"
        )

    event = data.get("event", {})
    submission_id = event.get("submission_id", "sub_demo_001")
    collaborator_id = event.get("collaborator_tg_id", 123456789)
    raw_image_bytes = None

    if "image_base64" in event:
        import base64
        raw_image_bytes = base64.b64decode(event["image_base64"])
    else:
        raw_image_bytes = b"MOCK_IMAGE_BYTES"

    # 4. media sanitization & blur check
    try:
        webp_bytes, pil_img = sanitize_and_normalize_image(
            raw_image_bytes, max_dimension=settings.IMAGE_MAX_DIMENSION
        )
        is_sharp, blur_variance = compute_laplacian_blur_score(
            pil_img, threshold=settings.BLUR_THRESHOLD
        )
    except Exception:
        msg = render_telegram_verdict(submission_id, "BLURRY", ["ảnh bị hỏng hoặc lỗi định dạng"])
        await send_telegram_notification(collaborator_id, msg)
        return {"status": "rejected", "reason": "UNREADABLE_IMAGE"}

    if not is_sharp:
        msg = render_telegram_verdict(submission_id, "BLURRY")
        await send_telegram_notification(collaborator_id, msg)
        return {"status": "rejected", "reason": "BLURRY_IMAGE", "variance": blur_variance}

    # 5. dual-model AI vision extraction
    facts = await vision_client.extract_facts(webp_bytes)

    # 6. deterministic python compliance evaluation
    eval_result = evaluate_survey_compliance(facts)

    # 7. instant telegram verdict dispatch (< 3.5s SLA)
    verdict_text = render_telegram_verdict(
        submission_id=submission_id,
        reason_code=eval_result.reason_code,
        missing_fields=eval_result.missing_fields,
        collaborator_note=eval_result.collaborator_note,
    )
    await send_telegram_notification(collaborator_id, verdict_text)

    # 8. background async offload: R2 upload
    background_tasks.add_task(
        r2_client.upload_image, f"{submission_id}.webp", webp_bytes
    )

    return {
        "status": "processed",
        "submission_id": submission_id,
        "verdict": eval_result.reason_code,
    }


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
    sec = getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "") or ""
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
