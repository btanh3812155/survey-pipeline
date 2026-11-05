import os
import re
import secrets
import json
import asyncio
import time
import threading
from collections import OrderedDict
import httpx
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Union
from contextlib import asynccontextmanager
import redis.asyncio as aioredis
from psycopg_pool import AsyncConnectionPool
from fastapi import FastAPI, Request, Header, HTTPException, BackgroundTasks, status, File, UploadFile, Form, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from services.form_engine import (
    FormSchema,
    DEFAULT_TRUST_BRIDGE_SCHEMA,
    validate_submission_against_schema,
    get_effective_image_checks,
)
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
from services.vehicle_tracker import VehiclePersistenceTracker
from services.privacy_crypto import (
    hash_phone,
    hash_plate,
    generate_driver_tag,
    mask_phone,
    mask_license_plate,
)
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
class BoundedTTLCache:
    """Thread-safe bounded in-memory cache with OrderedDict LRU eviction and TTL expiration."""
    def __init__(self, max_size: int = 2048, ttl_seconds: float = 86400.0):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    @property
    def maxsize(self) -> int:
        return self.max_size

    @property
    def ttl(self) -> float:
        return self.ttl_seconds

    def _purge_expired(self, now: float):
        while self._cache:
            k, (_, exp) = next(iter(self._cache.items()))
            if exp <= now:
                del self._cache[k]
            else:
                break

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            now = time.time()
            if key not in self._cache:
                return default
            val, exp = self._cache[key]
            if exp <= now:
                del self._cache[key]
                return default
            self._cache.move_to_end(key)
            return val

    def __getitem__(self, key: str) -> Any:
        val = self.get(key)
        if val is None:
            raise KeyError(key)
        return val

    def __setitem__(self, key: str, value: Any):
        with self._lock:
            now = time.time()
            self._purge_expired(now)
            if key in self._cache:
                del self._cache[key]
            elif len(self._cache) >= self.max_size:
                self._cache.popitem(last=False)
            self._cache[key] = (value, now + self.ttl_seconds)

    def __contains__(self, key: str) -> bool:
        with self._lock:
            now = time.time()
            if key not in self._cache:
                return False
            val, exp = self._cache[key]
            if exp <= now:
                del self._cache[key]
                return False
            return True

    def __delitem__(self, key: str):
        with self._lock:
            del self._cache[key]

    def clear(self):
        with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        with self._lock:
            now = time.time()
            self._purge_expired(now)
            return len(self._cache)

LOCAL_IDEMP_CACHE = BoundedTTLCache(max_size=2048, ttl_seconds=86400.0)
HISTORY_RATE_LIMITER = BoundedTTLCache(max_size=2048, ttl_seconds=60.0)
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

    polling_task = None
    polling_runner = None
    if getattr(settings, "TELEGRAM_POLLING_ENABLED", False):
        try:
            from services.telegram_polling import TelegramPollingRunner
            polling_runner = TelegramPollingRunner(db_pool=db_pool)
            polling_task = asyncio.create_task(polling_runner.start_polling())
            logger.info("Telegram long-polling runner started in background.")
        except Exception as e:
            logger.error(f"Failed to start telegram polling runner: {e}")

    yield

    if polling_runner:
        polling_runner.stop()
    if polling_task:
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass

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

admin_security = HTTPBasic()

def verify_admin_auth(credentials: HTTPBasicCredentials = Depends(admin_security)):
    """Protects administrative endpoints with HTTP Basic Authentication."""
    is_user_ok = secrets.compare_digest(credentials.username, settings.ADMIN_USERNAME)
    is_pass_ok = secrets.compare_digest(credentials.password, settings.ADMIN_PASSWORD)
    if not (is_user_ok and is_pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Thông tin đăng nhập quản trị viên không chính xác",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username



@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    return {"status": "healthy", "environment": settings.ENVIRONMENT}

SAMPLE_IMAGES_DIR = os.path.join(os.getcwd(), "data", "test_samples")

@app.get("/samples/{filename}")
async def get_sample_image(filename: str):
    """Serves public sample testing images with path traversal protection."""
    if not filename or "\x00" in filename or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=404, detail="Sample image not found")
    try:
        base_dir = os.path.realpath(SAMPLE_IMAGES_DIR)
        safe_path = os.path.realpath(os.path.join(base_dir, filename))
        if not (safe_path.startswith(base_dir + os.sep) and os.path.isfile(safe_path)):
            raise HTTPException(status_code=404, detail="Sample image not found")
    except (ValueError, TypeError, OSError):
        raise HTTPException(status_code=404, detail="Sample image not found")
    ext = os.path.splitext(filename)[1].lower()
    media_type = "image/jpeg" if ext in (".jpg", ".jpeg") else ("image/png" if ext == ".png" else "image/webp")
    return FileResponse(safe_path, media_type=media_type)

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
    schema_path = os.path.join(FORMS_STORAGE_DIR, f"{schema.form_id}.json")
    target_path = os.path.realpath(schema_path)
    base_path = os.path.realpath(FORMS_STORAGE_DIR)
    if not (target_path == base_path or target_path.startswith(base_path + os.sep)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Mã biểu mẫu không hợp lệ")

    FORM_REGISTRY[schema.form_id] = schema
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
async def get_dev_submissions(admin_user: str = Depends(verify_admin_auth)):
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
async def clear_dev_submissions(admin_user: str = Depends(verify_admin_auth)):
    """Clears all records in temporary dev test storage."""
    if os.path.exists(DEV_STORAGE_FILE):
        os.remove(DEV_STORAGE_FILE)
    return {"status": "cleared", "message": "Bộ nhớ tạm thử nghiệm đã được dọn sạch!"}

@app.get("/api/v1/surveys/history")
async def get_survey_history(
    phone: str,
    admin_user: str = Depends(verify_admin_auth),
):
    """Retrieves previous survey attempts and rejection reasons for a given driver phone number."""
    if not phone:
        raise HTTPException(status_code=400, detail="Vui lòng cung cấp số điện thoại hợp lệ.")

    clean_phone = re.sub(r"[\s\-\.\(\)]", "", phone.strip())
    if not clean_phone or not re.match(r"^\+?[0-9]{8,15}$", clean_phone):
        raise HTTPException(status_code=400, detail="Số điện thoại không hợp lệ.")

    rate_limited = False
    if redis_client:
        try:
            rkey = f"rate_limit:history:{clean_phone}"
            count = await redis_client.incr(rkey)
            if count == 1:
                await redis_client.expire(rkey, 60)
            if count > 30:
                rate_limited = True
        except Exception as e:
            logger.warning(f"Redis rate limiting error: {e}")
            req_count = HISTORY_RATE_LIMITER.get(clean_phone, 0)
            if req_count >= 30:
                rate_limited = True
            else:
                HISTORY_RATE_LIMITER[clean_phone] = req_count + 1
    else:
        req_count = HISTORY_RATE_LIMITER.get(clean_phone, 0)
        if req_count >= 30:
            rate_limited = True
        else:
            HISTORY_RATE_LIMITER[clean_phone] = req_count + 1

    if rate_limited:
        raise HTTPException(status_code=429, detail="Quá nhiều yêu cầu tra cứu lịch sử. Vui lòng thử lại sau!")

    phone = clean_phone
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
    request: Request,
    background_tasks: BackgroundTasks,
    survey_photo: Optional[UploadFile] = File(None),
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
    form_id: Optional[str] = Form(None),
    question_id: Optional[str] = Form(None),
):
    """Ingests live WebRTC canvas captures and performs real-time blur and vision audit."""
    # Privacy hashing & PII pseudonymization
    driver_phone_hash = hash_phone(driver_phone)
    license_plate_hash = hash_plate(license_plate) if license_plate else None
    driver_tag = generate_driver_tag(driver_phone)

    submission_id = f"{'DEV' if is_test_mode else 'VN'}-{secrets.token_hex(8).upper()}"
    # 0. Idempotency Check: return cached response if key was already submitted
    if idempotency_key:
        idemp_cache_key = f"idemp:{driver_phone}:{idempotency_key}"
        cached_payload = None
        if redis_client:
            try:
                cached_payload = await redis_client.get(idemp_cache_key)
            except Exception as e:
                logger.warning(f"Redis idempotency lookup error: {e}")
        if not cached_payload and idemp_cache_key in LOCAL_IDEMP_CACHE:
            cached_payload = LOCAL_IDEMP_CACHE[idemp_cache_key]

        if cached_payload:
            logger.info(f"[IDEMPOTENCY HIT] Returning cached survey for key: {idemp_cache_key}")
            return JSONResponse(status_code=200, content=json.loads(cached_payload))

    # Extract all submitted form parameters and support multiple image uploads
    form_data = await request.form()
    active_form_id = form_id or "income_survey"
    schema = FORM_REGISTRY.get(active_form_id)
    target_question_id = question_id or "camera_receipt"
    target_q = next((qu for qu in schema.questions if qu.id == target_question_id), None) if schema else None

    # Inspect survey_photo and request form for survey_photo_0, survey_photo_1, etc.
    images_list = []
    has_indexed_photos = False
    idx = 0
    while True:
        k_sp = f"survey_photo_{idx}"
        k_tq = f"{target_question_id}_{idx}"
        item = None
        if k_sp in form_data and (hasattr(form_data[k_sp], "read") or isinstance(form_data[k_sp], UploadFile)):
            item = form_data[k_sp]
        elif k_tq in form_data and (hasattr(form_data[k_tq], "read") or isinstance(form_data[k_tq], UploadFile)):
            item = form_data[k_tq]
        if item is not None:
            images_list.append(item)
            has_indexed_photos = True
            idx += 1
        else:
            break

    if not images_list and survey_photo:
        images_list.append(survey_photo)
        idx = 1
        while True:
            k_sp = f"survey_photo_{idx}"
            k_tq = f"{target_question_id}_{idx}"
            item = None
            if k_sp in form_data and (hasattr(form_data[k_sp], "read") or isinstance(form_data[k_sp], UploadFile)):
                item = form_data[k_sp]
            elif k_tq in form_data and (hasattr(form_data[k_tq], "read") or isinstance(form_data[k_tq], UploadFile)):
                item = form_data[k_tq]
            if item is not None:
                images_list.append(item)
                has_indexed_photos = True
                idx += 1
            else:
                break

    answers: Dict[str, Any] = dict(form_data)
    if "driver_phone" not in answers:
        answers["driver_phone"] = driver_phone
    if "driver_name" not in answers and driver_name:
        answers["driver_name"] = driver_name
    if "driver_signature" not in answers and signature_png:
        answers["driver_signature"] = signature_png
    if images_list:
        if has_indexed_photos or len(images_list) > 1:
            answers[target_question_id] = [f"uploaded_{i}" for i in range(len(images_list))]
        else:
            # Legacy single photo upload without indexed multi-image fields
            if target_q and target_q.min_images and target_q.min_images > 1:
                answers[target_question_id] = [f"uploaded_{i}" for i in range(target_q.min_images)]
            else:
                answers[target_question_id] = "uploaded"
    elif target_question_id not in answers or not answers[target_question_id]:
        answers[target_question_id] = None
    if "camera_receipt" not in answers and target_question_id != "camera_receipt":
        answers["camera_receipt"] = answers.get(target_question_id, "uploaded")
    if "daily_net_income" not in answers and gross_revenue is not None:
        answers["daily_net_income"] = gross_revenue
    if "monthly_gross_revenue" not in answers and gross_revenue is not None:
        answers["monthly_gross_revenue"] = gross_revenue

    # Server-Side Schema Enforcement (VULN-API-01)
    checks = get_effective_image_checks(schema, target_question_id)
    is_schema_valid = True
    validation_err = None
    if schema:
        is_schema_valid, validation_err = validate_submission_against_schema(schema, answers)

    blur_var = None
    if not is_schema_valid:
        status_str = "REJECTED"
        is_approved = False
        reason_str = validation_err or "Không hợp lệ theo biểu mẫu."
        ai_res = {}
    else:
        # 1. Fleet Anti-Fraud: Route through VehiclePersistenceTracker when license plate & phone are provided
        v_match = None
        if checks.enable_vehicle_match and license_plate and driver_phone:
            tracker = VehiclePersistenceTracker(db_pool=None)
            v_match = await tracker.verify_and_persist_vehicle(
                driver_phone=driver_phone,
                detected_plate=license_plate,
                detected_vehicle_type=vehicle_type or "CAR",
                driver_phone_hash=driver_phone_hash,
            )

        # Collect raw bytes for all uploaded images in images_list
        raw_bytes_list = []
        for img in images_list:
            await img.seek(0)
            b = await img.read()
            raw_bytes_list.append(b)
            await img.seek(0)

        raw_bytes = raw_bytes_list[0] if raw_bytes_list else b""

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
            if v_match and not v_match.is_match:
                is_approved = False
                status_str = "REJECTED"
                reason_str = v_match.flag_reason or f"Lỗi phương tiện: {v_match.status_code}"
            else:
                is_approved = True
                status_str = "APPROVED"
                reason_str = "Chế độ thử nghiệm: Đã bỏ qua kiểm tra ảnh để đẩy nhanh luồng dữ liệu mẫu."
        else:
            # For facts extraction, select the app revenue screen if multi-image
            target_raw_bytes = raw_bytes
            if len(raw_bytes_list) >= 2:
                target_raw_bytes = raw_bytes_list[1]

            try:
                norm_bytes, pil_im = sanitize_and_normalize_image(
                    target_raw_bytes, max_dimension=settings.IMAGE_MAX_DIMENSION
                )
            except Exception as e:
                norm_bytes, pil_im = None, None
                is_approved = False
                status_str = "REJECTED"
                reason_str = f"Không thể giải mã ảnh: {str(e)}"
                ai_res = {}

            if pil_im is not None:
                is_sharp = True
                blur_var = None
                if checks.enable_blur_check:
                    blur_thresh = checks.blur_threshold if checks.blur_threshold is not None else settings.BLUR_THRESHOLD
                    for img_b in raw_bytes_list:
                        try:
                            _, p_im = sanitize_and_normalize_image(img_b, max_dimension=settings.IMAGE_MAX_DIMENSION)
                            sharp, b_var = compute_laplacian_blur_score(p_im, threshold=blur_thresh)
                            if not sharp:
                                is_sharp = False
                                blur_var = b_var
                                break
                            blur_var = b_var
                        except Exception:
                            is_sharp = False
                            break
                else:
                    is_sharp, blur_var = True, None

                if not is_sharp:
                    is_approved = False
                    status_str = "REJECTED"
                    reason_str = "Ảnh chụp bị mờ hoặc rung tay, vui lòng chụp lại rõ nét hơn!"
                    ai_res = {}
                elif v_match and not v_match.is_match:
                    is_approved = False
                    status_str = "REJECTED"
                    reason_str = v_match.flag_reason or f"Lỗi phương tiện: {v_match.status_code}"
                    ai_res = {}
                elif not checks.enable_ai_vision:
                    ai_res = {"skipped": True, "reason": "Bỏ qua kiểm tra AI theo cấu hình"}
                    is_approved = True
                    status_str = "APPROVED"
                    reason_str = "Chấp thuận (bỏ qua kiểm tra AI theo cấu hình biểu mẫu)."
                else:
                    # AI vision audit with parallel execution via asyncio.gather
                    if target_q and target_q.image_slots and len(images_list) >= 2:
                        fn_0 = getattr(images_list[0], "filename", "") or ""
                        fn_1 = getattr(images_list[1], "filename", "") or ""
                        task_role_0 = vision_client.classify_image_role(raw_bytes_list[0], filename=fn_0)
                        task_role_1 = vision_client.classify_image_role(raw_bytes_list[1], filename=fn_1)
                        task_facts = vision_client.extract_facts(norm_bytes)

                        results = await asyncio.gather(task_role_0, task_role_1, task_facts, return_exceptions=True)
                        role_0_res, role_1_res, facts_res = results

                        role_0 = "unknown" if isinstance(role_0_res, Exception) else role_0_res
                        role_1 = "unknown" if isinstance(role_1_res, Exception) else role_1_res

                        if (role_0 == "unknown" or role_1 == "unknown") and not isinstance(facts_res, Exception):
                            st_1 = (getattr(facts_res, "screen_type", None) or getattr(facts_res, "role", None) or "").upper()
                            if "REV" in st_1:
                                role_1 = "revenue_screen"
                            elif "ODO" in st_1:
                                role_1 = "odometer"

                        if role_0 == "unknown":
                            try:
                                facts_0 = await vision_client.extract_facts(raw_bytes_list[0])
                                st_0 = (getattr(facts_0, "screen_type", None) or getattr(facts_0, "role", None) or "").upper()
                                if "REV" in st_0:
                                    role_0 = "revenue_screen"
                                elif "ODO" in st_0:
                                    role_0 = "odometer"
                            except Exception:
                                pass

                        slot_0_role = target_q.image_slots[0].get("role", "odometer") if len(target_q.image_slots) > 0 else "odometer"
                        slot_1_role = target_q.image_slots[1].get("role", "revenue_screen") if len(target_q.image_slots) > 1 else "revenue_screen"

                        # If an inverted order is detected (e.g. image 0 is an app screen and image 1 is an odometer)
                        if (role_0 == slot_1_role and role_1 == slot_0_role) or (role_0 == "revenue_screen" and role_1 == "odometer"):
                            is_approved = False
                            status_str = "REJECTED"
                            reason_str = "Thứ tự ảnh không chính xác: Ảnh 1 phải là đồng hồ công-tơ-mét, Ảnh 2 phải là màn hình doanh thu!"
                            ai_res = {
                                "status": "REJECTED",
                                "reason": reason_str,
                                "order_check_failed": True,
                                "detected_roles": [role_0, role_1],
                            }
                        else:
                            if isinstance(facts_res, Exception):
                                logger.error(f"Vision AI call failed: {facts_res}")
                                ai_res = {"status": "manual_review", "error": str(facts_res)}
                                is_approved = False
                                status_str = "MANUAL_REVIEW"
                                reason_str = "Hệ thống đã tiếp nhận ảnh và chuyển sang chờ kiểm tra thủ công."
                            else:
                                facts = facts_res
                                compliance = evaluate_survey_compliance(facts)
                                ai_res = facts.model_dump() if hasattr(facts, "model_dump") else {}
                                is_approved = bool(compliance.passed or compliance.status_tier == "APPROVED")
                                status_str = "APPROVED" if is_approved else "REJECTED"
                                reason_str = compliance.collaborator_note or compliance.reason_code
                    else:
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
                            is_approved = False
                            status_str = "MANUAL_REVIEW"
                            reason_str = "Hệ thống đã tiếp nhận ảnh và chuyển sang chờ kiểm tra thủ công."
    # 3. Persistence: Test mode -> Dev storage; Production mode -> DB + Outbox Events in same transaction
    is_dissent = answers.get("consent_choice") == "Tôi không đồng ý"
    persisted_phone = None if is_dissent else driver_phone
    persisted_name = None if is_dissent else (driver_name or "Tài xế")

    if is_test_mode:
        dev_record = {
            "submission_id": submission_id,
            "mode": "TEST_DEV_MODE",
            "driver_tag": driver_tag,
            "driver_phone_hash": driver_phone_hash,
            "license_plate_hash": license_plate_hash,
            "driver_phone": persisted_phone,
            "driver_name": persisted_name,
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
                cur_res = conn.cursor()
                if hasattr(cur_res, "__await__"):
                    cur_res = await cur_res
                cur = await cur_res.__aenter__() if hasattr(cur_res, "__aenter__") else cur_res
                try:
                    if not is_dissent and persisted_phone:
                        await cur.execute(
                            """
                            INSERT INTO driver_identity_vault (
                                driver_id, driver_phone, driver_name
                            ) VALUES (%s, %s, %s)
                            ON CONFLICT (driver_phone) DO UPDATE
                            SET driver_id = EXCLUDED.driver_id, driver_name = EXCLUDED.driver_name;
                            """,
                            (driver_tag, persisted_phone, persisted_name),
                        )
                    await cur.execute(
                        """
                        INSERT INTO survey_submissions (
                            submission_id, driver_phone, driver_name, receipt_serial_no,
                            status, rejection_reason, raw_extracted_facts, driver_id
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (submission_id) DO NOTHING;
                        """,
                        (
                            submission_id,
                            persisted_phone,
                            persisted_name,
                            f"SN-{license_plate or 'AUTO'}",
                            status_str,
                            reason_str if not is_approved else None,
                            json.dumps(ai_res),
                            driver_tag if not is_dissent else None,
                        ),
                    )
                    if license_plate and not is_dissent:
                        await cur.execute(
                            """
                            INSERT INTO driver_registered_vehicles (
                                driver_phone, license_plate, vehicle_type
                            ) VALUES (%s, %s, %s)
                            ON CONFLICT (driver_phone) DO NOTHING;
                            """,
                            (driver_phone, license_plate.upper(), vehicle_type or "CAR"),
                        )
                    outbox_payload = {
                        "submission_id": submission_id,
                        "driver_tag": driver_tag,
                        "driver_phone_hash": driver_phone_hash,
                        "driver_name": persisted_name,
                        "license_plate": license_plate or ai_res.get("detected_plate"),
                        "license_plate_hash": license_plate_hash or (hash_plate(ai_res.get("detected_plate")) if ai_res.get("detected_plate") else None),
                        "vehicle_type": vehicle_type or "CAR",
                        "gross_revenue": gross_revenue,
                        "trip_count": trip_count,
                        "status": status_str,
                        "reason": reason_str,
                        "ai_result": ai_res,
                    }
                    event_type = "survey.validated" if is_approved else "survey.rejected"
                    await cur.execute(
                        """
                        INSERT INTO outbox_events (
                            event_type, flow_target, aggregate_id, payload, status
                        ) VALUES (%s, %s, %s, %s, 'PENDING');
                        """,
                        (
                            event_type,
                            "ALL",
                            submission_id,
                            json.dumps(outbox_payload),
                        ),
                    )
                    await conn.commit()
                finally:
                    if hasattr(cur_res, "__aexit__"):
                        await cur_res.__aexit__(None, None, None)
        except Exception as e:
            logger.error(f"failed to persist survey to DB: {e}")

    # 4. Telegram dispatch to admin group
    status_emoji = "✅" if status_str == "APPROVED" else ("⚠️" if status_str == "MANUAL_REVIEW" else "❌")
    verdict_text = (
        f"{status_emoji} <b>KẾT QUẢ KHẢO SÁT #{submission_id}</b>\n\n"
        f"• <b>Trạng thái:</b> <code>{status_str}</code>\n"
        f"• <b>Số điện thoại:</b> <code>{driver_phone or 'N/A'}</code>\n"
        f"• <b>Biển số xe:</b> <code>{license_plate or ai_res.get('detected_plate') or 'N/A'}</code>\n"
        f"• <b>Lý do / Ghi chú:</b> {reason_str or 'Hợp lệ'}"
    )
    if getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", None):
        try:
            background_tasks.add_task(send_telegram_notification, settings.TELEGRAM_ADMIN_GROUP_ID, verdict_text)
        except Exception as tg_err:
            logger.error(f"Failed to dispatch Telegram notification to admin group: {tg_err}")
    resp_data = {
        "status": status_str,
        "submission_id": submission_id,
        "mode": "TEST_DEV_MODE" if is_test_mode else "PROD_MODE",
        "reason": reason_str,
        "driver_tag": driver_tag,
        "driver_phone": driver_phone,
        "detected_plate": ai_res.get("detected_plate") or license_plate,
        "gross_revenue": gross_revenue,
        "trip_count": trip_count,
    }
    if blur_var is not None:
        resp_data["blur_score"] = blur_var

    # Cache response under idempotency key for 24 hours
    if idempotency_key:
        idemp_cache_key = f"idemp:{driver_phone}:{idempotency_key}"
        LOCAL_IDEMP_CACHE[idemp_cache_key] = json.dumps(resp_data)
        if redis_client:
            try:
                await redis_client.set(idemp_cache_key, json.dumps(resp_data), ex=86400)
            except Exception as e:
                logger.warning(f"Redis idempotency caching error: {e}")
    return JSONResponse(status_code=200, content=resp_data)


async def send_telegram_notification(chat_id: Union[int, str], text: str):
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
        webp_bytes, pil_img = sanitize_and_normalize_image(
            raw_image_bytes, max_dimension=settings.IMAGE_MAX_DIMENSION
        )
        is_sharp, blur_variance = compute_laplacian_blur_score(
            pil_img, threshold=settings.BLUR_THRESHOLD
        )
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
    message = body.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = message.get("text") or ""
    if len(text) > 4096:
        raise HTTPException(status_code=400, detail="Tin nhắn vượt quá độ dài cho phép")

    # 1. Handle Admin 1-Click Database Query Callback
    cq = body.get("callback_query") or {}
    if cq:
        data = cq.get("data", "")
        cq_msg = cq.get("message") or {}
        cq_chat = cq_msg.get("chat") or {}
        cq_chat_id = cq_chat.get("id")
        if data.startswith("admin_query:"):
            if str(cq_chat_id) != str(settings.TELEGRAM_ADMIN_GROUP_ID):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="unauthorized chat")
            sub_id = data.split(":", 1)[1]
            if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", sub_id):
                return {"status": "invalid_id"}
            dossier = await query_submission_dossier(sub_id)
            report_text = render_dossier_message(dossier)
            await send_telegram_notification(cq_chat_id, report_text)
            return {"status": "query_handled"}

    # 2. Handle /query or /check slash command in Admin Group
    if text.startswith("/query ") or text.startswith("/check "):
        if str(chat_id) != str(settings.TELEGRAM_ADMIN_GROUP_ID):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="unauthorized chat")
        parts = text.split(" ", 1)
        sub_id = parts[1].strip().lstrip("#")
        if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", sub_id):
            await send_telegram_notification(
                chat_id,
                "⚠️ <b>Mã phiếu không đúng định dạng.</b> Chỉ chấp nhận a-z, 0-9, gạch ngang (tối đa 64 ký tự).",
            )
            return {"status": "invalid_id"}
        dossier = await query_submission_dossier(sub_id)
        report_text = render_dossier_message(dossier)
        await send_telegram_notification(chat_id, report_text)
        return {"status": "query_command_handled"}

    if text == "/start" and chat_id:
        welcome_msg = (
            "👋 <b>Chào mừng bạn đến với hệ thống kiểm duyệt khảo sát!</b>\n\n"
            "Mọi phiếu khảo sát nộp qua form sẽ được tự động kiểm tra và phản hồi kết quả trực tiếp tại đây trong vòng 3 giây."
        )
        await send_telegram_notification(chat_id, welcome_msg)
    return {"status": "ok"}
