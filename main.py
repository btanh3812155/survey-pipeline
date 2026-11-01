import secrets
import json
import httpx
import logging
from typing import Optional
from contextlib import asynccontextmanager
import redis.asyncio as aioredis
from fastapi import FastAPI, Request, Header, HTTPException, BackgroundTasks, status
from fastapi.responses import JSONResponse

from config import settings
from services.normalizer import (
    sanitize_and_normalize_image,
    compute_laplacian_blur_score,
)
from services.auth_lark import (
    verify_lark_signature,
    handle_lark_challenge_handshake,
)
from services.admin_moderator import (
    query_submission_dossier,
    render_dossier_message,
)
from services.vision_ai import DualModelVisionClient
from services.evaluator import evaluate_survey_compliance
from services.templates_vn import render_telegram_verdict
from services.storage_r2 import r2_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main_api")

# singleton AI vision client
vision_client = DualModelVisionClient(
    gemini_api_key=settings.GEMINI_API_KEY,
    gemini_base_url=settings.GEMINI_BASE_URL,
    deepseek_api_key=settings.DEEPSEEK_API_KEY,
    deepseek_base_url=settings.DEEPSEEK_BASE_URL,
    timeout_seconds=settings.AI_TIMEOUT_SECONDS,
)

http_client: httpx.AsyncClient = None
try:
    redis_client: Optional[aioredis.Redis] = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
except Exception:
    redis_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, redis_client
    http_client = httpx.AsyncClient(timeout=10.0)
    if redis_client is None:
        try:
            redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        except Exception as e:
            logger.warning(f"could not initialize redis: {e}")
            redis_client = None
    yield
    if redis_client:
        try:
            await redis_client.aclose()
        except Exception:
            pass
    await http_client.aclose()

app = FastAPI(title="Survey Verification Pipeline", lifespan=lifespan)


@app.get("/health")
async def health_check():
    return {"status": "healthy", "environment": settings.ENVIRONMENT}


async def send_telegram_notification(chat_id: int, text: str):
    """Dispatches standardized HTML verdict message to collaborator on Telegram."""
    if not settings.TELEGRAM_BOT_TOKEN or settings.TELEGRAM_BOT_TOKEN == "mock_tg_token":
        logger.info(f"[MOCK TELEGRAM DISPATCH] to {chat_id}:\n{text}")
        return

    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    try:
        resp = await http_client.post(url, json=payload)
        if resp.status_code == 429:
            logger.warning("telegram 429 hit; backing off")
    except Exception as e:
        logger.error(f"failed to push telegram message: {e}")


@app.post("/webhook/lark-survey")
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

    # for development/testing: if image base64 provided in payload
    if "image_base64" in event:
        import base64
        raw_image_bytes = base64.b64decode(event["image_base64"])
    else:
        # fallback demo dummy image
        raw_image_bytes = b"MOCK_IMAGE_BYTES"

    # 4. media sanitization & blur check
    try:
        webp_bytes, pil_img = sanitize_and_normalize_image(raw_image_bytes)
        is_sharp, blur_variance = compute_laplacian_blur_score(pil_img)
    except Exception as e:
        # handle unreadable images
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

    # 8. background async offload: R2 upload & outbox event emission
    background_tasks.add_task(
        r2_client.upload_image, f"{submission_id}.webp", webp_bytes
    )

    return {
        "status": "processed",
        "submission_id": submission_id,
        "verdict": eval_result.reason_code,
    }


@app.post("/webhook/telegram")
async def telegram_webhook_ingress(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(None),
):
    if settings.ENVIRONMENT != "development":
        if not secrets.compare_digest(
            x_telegram_bot_api_secret_token or "", settings.TELEGRAM_WEBHOOK_SECRET
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    body = await request.json()
    message = body.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")
    # 1. Handle Admin 1-Click Database Query Callback
    cq = body.get("callback_query")
    if cq:
        data = cq.get("data", "")
        cq_chat_id = cq.get("message", {}).get("chat", {}).get("id")
        if data.startswith("admin_query:"):
            sub_id = data.split(":", 1)[1]
            dossier = await query_submission_dossier(sub_id)
            report_text = render_dossier_message(dossier)
            await send_telegram_notification(cq_chat_id, report_text)
            return {"status": "query_handled"}

    # 2. Handle /query or /check slash command in Admin Group
    if text.startswith("/query ") or text.startswith("/check "):
        parts = text.split(" ", 1)
        sub_id = parts[1].strip().lstrip("#")
        dossier = await query_submission_dossier(sub_id)
        report_text = render_dossier_message(dossier)
        await send_telegram_notification(chat_id, report_text)
        return {"status": "query_command_handled"}

    if text == "/start":
        welcome_msg = (
            "👋 <b>Chào mừng bạn đến với hệ thống kiểm duyệt khảo sát!</b>\n\n"
            "Mọi phiếu khảo sát nộp qua form sẽ được tự động kiểm tra và phản hồi kết quả trực tiếp tại đây trong vòng 3 giây."
        )
        await send_telegram_notification(chat_id, welcome_msg)
    return {"status": "ok"}
