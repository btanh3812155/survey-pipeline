import os
import secrets
import json
import httpx
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager
import redis.asyncio as aioredis
from psycopg_pool import AsyncConnectionPool
from fastapi import FastAPI, Request, Header, HTTPException, BackgroundTasks, status, File, UploadFile, Form, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from services.camera_form_html import CAMERA_FORM_HTML
from services.form_engine import FormSchema, DEFAULT_TRUST_BRIDGE_SCHEMA, validate_submission_against_schema
from services.dynamic_form_html import render_dynamic_form_html
from services.form_manager_html import render_forms_list_html, render_form_editor_html

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
db_pool: Optional[AsyncConnectionPool] = None
LOCAL_IDEMP_CACHE: Dict[str, str] = {}
try:
    redis_client: Optional[aioredis.Redis] = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
except Exception:
    redis_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, redis_client, db_pool
    http_client = httpx.AsyncClient(timeout=10.0)
    try:
        db_pool = AsyncConnectionPool(conninfo=settings.DATABASE_URL, min_size=1, max_size=10, open=False)
        await db_pool.open()
    except Exception as e:
        logger.warning(f"Could not open DB pool: {e}")
        db_pool = None
    if redis_client is None:
        try:
            redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        except Exception as e:
            logger.warning(f"could not initialize redis: {e}")
            redis_client = None
    yield
    if db_pool:
        try:
            await db_pool.close()
        except Exception:
            pass
    if redis_client:
        try:
            await redis_client.aclose()
        except Exception:
            pass
    await http_client.aclose()

app = FastAPI(title="Survey Verification Pipeline", lifespan=lifespan)
app.mount("/data", StaticFiles(directory="data"), name="data")

admin_security = HTTPBasic()

def verify_admin_auth(credentials: HTTPBasicCredentials = Depends(admin_security)):
    """Protects administrative endpoints with HTTP Basic Authentication."""
    is_user_ok = secrets.compare_digest(credentials.username, "admin")
    is_pass_ok = secrets.compare_digest(credentials.password, "Legendofzeus2@")
    if not (is_user_ok and is_pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Thông tin đăng nhập quản trị viên không chính xác",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username



@app.get("/health")
async def health_check():
    return {"status": "healthy", "environment": settings.ENVIRONMENT}

FORMS_STORAGE_DIR = os.path.join(os.getcwd(), "data", "forms")
os.makedirs(FORMS_STORAGE_DIR, exist_ok=True)

def load_persisted_forms() -> Dict[str, FormSchema]:
    registry: Dict[str, FormSchema] = {
        DEFAULT_TRUST_BRIDGE_SCHEMA.form_id: DEFAULT_TRUST_BRIDGE_SCHEMA
    }
    default_path = os.path.join(FORMS_STORAGE_DIR, f"{DEFAULT_TRUST_BRIDGE_SCHEMA.form_id}.json")
    if not os.path.exists(default_path):
        try:
            with open(default_path, "w", encoding="utf-8") as f:
                f.write(DEFAULT_TRUST_BRIDGE_SCHEMA.model_dump_json(indent=2))
        except Exception as e:
            logger.warning(f"Failed to persist default schema: {e}")

    try:
        for fname in os.listdir(FORMS_STORAGE_DIR):
            if fname.endswith(".json"):
                fpath = os.path.join(FORMS_STORAGE_DIR, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    schema = FormSchema.model_validate(data)
                    registry[schema.form_id] = schema
                except Exception as err:
                    logger.warning(f"Error loading schema from {fname}: {err}")
    except Exception as e:
        logger.warning(f"Error scanning forms directory: {e}")

    return registry

FORM_REGISTRY: Dict[str, FormSchema] = load_persisted_forms()
@app.get("/", response_class=HTMLResponse)
@app.get("/camera-form", response_class=HTMLResponse)
@app.get("/forms/{form_id}", response_class=HTMLResponse)
async def get_dynamic_form(form_id: str = "default"):
    """Renders dynamic forms with conditional logic, signature modal, and GPS authorization."""
    if form_id in ("default", "camera-form") or form_id not in FORM_REGISTRY:
        schema = FORM_REGISTRY.get("income_survey") or next(iter(FORM_REGISTRY.values()))
    else:
        schema = FORM_REGISTRY[form_id]
    return HTMLResponse(content=render_dynamic_form_html(schema), status_code=200)

@app.get("/admin/forms", response_class=HTMLResponse)
async def get_forms_list(page: int = 1, admin_user: str = Depends(verify_admin_auth)):
    """Admin Web UI with pagination to view and manage forms."""
    return HTMLResponse(content=render_forms_list_html(FORM_REGISTRY, page=page), status_code=200)

@app.get("/admin/forms/create", response_class=HTMLResponse)
async def create_new_form_page(admin_user: str = Depends(verify_admin_auth)):
    """Dedicated 3-tab creation page."""
    return HTMLResponse(content=render_form_editor_html(None, is_new=True), status_code=200)

@app.get("/admin/forms/edit/{form_id}", response_class=HTMLResponse)
async def edit_form_page(form_id: str, admin_user: str = Depends(verify_admin_auth)):
    """Dedicated 3-tab edit page for a specific form."""
    schema = FORM_REGISTRY.get(form_id)
    if not schema:
        raise HTTPException(status_code=404, detail="Không tìm thấy biểu mẫu")
    return HTMLResponse(content=render_form_editor_html(schema, is_new=False), status_code=200)



@app.post("/api/v1/forms/upload-schema")
async def upload_form_schema(schema: FormSchema, admin_user: str = Depends(verify_admin_auth)):
    """Allows form owners to upload arbitrary survey schemas with custom rules and constraints."""
    FORM_REGISTRY[schema.form_id] = schema
    schema_path = os.path.join(FORMS_STORAGE_DIR, f"{schema.form_id}.json")
    try:
        with open(schema_path, "w", encoding="utf-8") as f:
            f.write(schema.model_dump_json(indent=2))
    except Exception as e:
        logger.error(f"Failed to persist schema to disk: {e}")

    return {
        "status": "success",
        "form_id": schema.form_id,
        "title": schema.title,
        "form_url": f"/forms/{schema.form_id}",
        "question_count": len(schema.questions),
        "message": f"Biểu mẫu '{schema.title}' đã sẵn sàng hoạt động!",
    }

DEV_STORAGE_FILE = os.path.join(os.getcwd(), "data", "dev_test_submissions.json")
os.makedirs(os.path.dirname(DEV_STORAGE_FILE), exist_ok=True)

def append_dev_submission(record: dict):
    records = []
    if os.path.exists(DEV_STORAGE_FILE):
        try:
            with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                records = json.load(f)
        except Exception:
            records = []
    records.append(record)
    with open(DEV_STORAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

@app.get("/api/v1/dev/submissions")
async def get_dev_submissions():
    """Inspects all surveys currently saved in temporary dev test storage."""
    if not os.path.exists(DEV_STORAGE_FILE):
        return {"count": 0, "submissions": []}
    try:
        with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"count": len(data), "submissions": data}
    except Exception as e:
        return {"count": 0, "submissions": [], "error": str(e)}

@app.delete("/api/v1/dev/submissions")
async def clear_dev_submissions():
    """Clears all records in temporary dev test storage."""
    if os.path.exists(DEV_STORAGE_FILE):
        os.remove(DEV_STORAGE_FILE)
    return {"status": "cleared", "message": "Bộ nhớ tạm thử nghiệm đã được dọn sạch!"}

@app.get("/api/v1/surveys/history")
async def get_survey_history(phone: str, admin_user: str = Depends(verify_admin_auth)):
    """Retrieves previous survey attempts and rejection reasons for a given driver phone number."""
    results = []
    if os.path.exists(DEV_STORAGE_FILE):
        try:
            with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                dev_records = json.load(f)
            for r in dev_records:
                if r.get("driver_phone") == phone:
                    results.append({
                        "submission_id": r.get("submission_id"),
                        "mode": "DEV_TEST",
                        "status": r.get("status"),
                        "reason": r.get("reason"),
                        "submitted_at": r.get("submitted_at")
                    })
        except Exception as e:
            logger.warning(f"Error reading dev history: {e}")

    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT submission_id, status, rejection_reason, created_at
                        FROM survey_submissions
                        WHERE driver_phone = %s
                        ORDER BY created_at DESC
                        LIMIT 20;
                        """,
                        (phone,)
                    )
                    rows = await cur.fetchall()
                    for row in rows:
                        results.append({
                            "submission_id": row[0],
                            "mode": "PRODUCTION",
                            "status": row[1],
                            "reason": row[2] or "Hợp lệ",
                            "submitted_at": row[3].isoformat() if row[3] else None
                        })
        except Exception as e:
            logger.warning(f"Error reading DB history: {e}")

    return {"phone": phone, "count": len(results), "attempts": results}



@app.post("/api/v1/survey/submit-webrtc")
async def submit_webrtc_survey(
    survey_photo: UploadFile = File(...),
    driver_phone: str = Form(...),
    license_plate: Optional[str] = Form(None),
    driver_name: Optional[str] = Form(None),
    vehicle_type: Optional[str] = Form("CAR"),
    gross_revenue: Optional[str] = Form("0"),
    trip_count: Optional[str] = Form("0"),
    odometer_km: Optional[str] = Form("0"),
    signature_png: Optional[str] = Form(None),
    is_test_mode: Optional[bool] = Form(False),
    idempotency_key: Optional[str] = Form(None),
    bypass_img_check: Optional[bool] = Form(False),
):
    """Ingests live WebRTC canvas captures and performs real-time blur and vision audit."""
    submission_id = f"{'DEV' if is_test_mode else 'VN'}-{secrets.token_hex(3).upper()}"
    # 0. Idempotency Check: return cached response if key was already submitted
    if idempotency_key:
        cached_payload = None
        if redis_client:
            try:
                cached_payload = await redis_client.get(f"idemp:{idempotency_key}")
            except Exception as e:
                logger.warning(f"Redis idempotency lookup error: {e}")
        if not cached_payload and idempotency_key in LOCAL_IDEMP_CACHE:
            cached_payload = LOCAL_IDEMP_CACHE[idempotency_key]

        if cached_payload:
            logger.info(f"[IDEMPOTENCY HIT] Returning cached survey for key: {idempotency_key}")
            return JSONResponse(status_code=200, content=json.loads(cached_payload))

    raw_bytes = await survey_photo.read()

    is_bypassed = bool(is_test_mode and str(bypass_img_check).lower() in ("true", "1", "t", "yes"))
    if is_bypassed:
        ai_res = {
            "status": "APPROVED",
            "gross_revenue": gross_revenue or "650000",
            "driver_phone": driver_phone,
            "detected_plate": license_plate or "29E-123.45",
            "is_screen_photo": False,
            "bypass_img_check": True,
        }
        is_approved = True
        status_str = "APPROVED"
        reason_str = "Chế độ thử nghiệm: Đã bỏ qua kiểm tra ảnh để đẩy nhanh luồng dữ liệu mẫu."
    else:
        # 1. normalize image and compute blur score on PIL image
        try:
            norm_bytes, pil_im = sanitize_and_normalize_image(raw_bytes)
        except Exception as e:
            return JSONResponse(
                status_code=200,
                content={
                    "status": "REJECTED",
                    "submission_id": submission_id,
                    "mode": "TEST_DEV_MODE" if is_test_mode else "PROD_MODE",
                    "reason": f"Không thể giải mã ảnh: {str(e)}",
                },
            )

        is_sharp, blur_var = compute_laplacian_blur_score(pil_im)
        if not is_sharp:
            return JSONResponse(
                status_code=200,
                content={
                    "status": "REJECTED",
                    "submission_id": submission_id,
                    "mode": "TEST_DEV_MODE" if is_test_mode else "PROD_MODE",
                    "reason": "Ảnh chụp bị mờ hoặc rung tay, vui lòng chụp lại rõ nét hơn!",
                    "blur_score": blur_var,
                },
            )

        # 2. AI vision audit with graceful failover
        try:
            facts = await vision_client.extract_facts(norm_bytes)
            compliance = evaluate_survey_compliance(facts)
            ai_res = facts.model_dump() if hasattr(facts, "model_dump") else {}
            is_approved = bool(compliance.passed or compliance.status_tier == "APPROVED")
            status_str = "APPROVED" if is_approved else "REJECTED"
            reason_str = compliance.collaborator_note or compliance.reason_code
        except Exception as ai_err:
            logger.error(f"Vision AI call failed: {ai_err}")
            ai_res = {"status": "manual_review", "error": str(ai_err)}
            is_approved = True
            status_str = "MANUAL_REVIEW"
            reason_str = "Hệ thống đã tiếp nhận ảnh và chuyển sang chờ kiểm tra thủ công."
    # 3. If in test mode, route strictly to temporary dev storage (Bypasses prod DB & Outbox)
    if is_test_mode:
        dev_record = {
            "submission_id": submission_id,
            "mode": "TEST_DEV_MODE",
            "driver_phone": driver_phone,
            "driver_name": driver_name,
            "license_plate": license_plate,
            "vehicle_type": vehicle_type,
            "status": status_str,
            "reason": reason_str,
            "ai_result": ai_res,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
        append_dev_submission(dev_record)
        logger.info(f"[DEV TEST MODE] Saved #{submission_id} to temporary dev storage: {DEV_STORAGE_FILE}")
    elif db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO survey_submissions (
                            submission_id, driver_phone, driver_name, receipt_serial_no,
                            status, rejection_reason, raw_extracted_facts
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (submission_id) DO NOTHING;
                        """,
                        (
                            submission_id,
                            driver_phone,
                            driver_name or "Tài xế",
                            f"SN-{license_plate or 'AUTO'}",
                            status_str,
                            reason_str if not is_approved else None,
                            json.dumps(ai_res),
                        ),
                    )
                    if license_plate:
                        await cur.execute(
                            """
                            INSERT INTO driver_registered_vehicles (
                                driver_phone, license_plate, vehicle_type
                            ) VALUES (%s, %s, %s)
                            ON CONFLICT (driver_phone) DO UPDATE 
                            SET license_plate = EXCLUDED.license_plate,
                                vehicle_type = EXCLUDED.vehicle_type;
                            """,
                            (driver_phone, license_plate.upper(), vehicle_type or "CAR"),
                        )
                    await conn.commit()
        except Exception as e:
            logger.error(f"failed to persist survey to DB: {e}")

    resp_data = {
        "status": status_str,
        "submission_id": submission_id,
        "mode": "TEST_DEV_MODE" if is_test_mode else "PROD_MODE",
        "reason": reason_str,
        "driver_phone": driver_phone,
        "detected_plate": ai_res.get("detected_plate"),
        "gross_revenue": gross_revenue,
        "trip_count": trip_count,
    }

    # Cache response under idempotency key for 24 hours
    if idempotency_key:
        LOCAL_IDEMP_CACHE[idempotency_key] = json.dumps(resp_data)
        if redis_client:
            try:
                await redis_client.set(f"idemp:{idempotency_key}", json.dumps(resp_data), ex=86400)
            except Exception as e:
                logger.warning(f"Redis idempotency caching error: {e}")
    return JSONResponse(status_code=200, content=resp_data)


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
