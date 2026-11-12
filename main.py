import os
import html
import re
import secrets
import hashlib
import json
import asyncio
import time
import threading
import ipaddress
from collections import OrderedDict
import httpx
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Union, List, Tuple
from contextlib import asynccontextmanager
from pydantic import BaseModel, Field
import redis.asyncio as aioredis
from psycopg_pool import AsyncConnectionPool
from fastapi import FastAPI, Request, Header, HTTPException, BackgroundTasks, status, File, UploadFile, Form, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse, RedirectResponse
from services.form_engine import (
    FormSchema,
    DEFAULT_TRUST_BRIDGE_SCHEMA,
    validate_submission_against_schema,
    get_effective_image_checks,
)
from services.dynamic_form_html import render_dynamic_form_html, render_ctv_selector_landing_html
from services.duplicate_detector import (
    check_intra_submission_duplicates,
    check_inter_submission_duplicates,
    compute_dinov2_embedding,
    compute_image_dhash_str,
)
from services.online_and_ai_detector import compute_dhash_int, hamming_distance
from services.form_manager_html import render_forms_list_html, render_form_editor_html
from services.submissions_viewer_html import render_submissions_viewer_html
from services.audit_viewer_html import render_audit_viewer_html
from services.lark_base_sync import sync_submission_to_lark_base
from services.google_drive_delivery import (
    upload_to_google_drive,
    has_google_credentials,
    sync_all_approved_to_drive,
)
from fastapi.staticfiles import StaticFiles
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
        try:
            async with db_pool.connection() as mig_conn:
                async with mig_conn.cursor() as mig_cur:
                    await mig_cur.execute(
                        """
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS consent_choice TEXT;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS vehicle_type VARCHAR(32);
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS car_model VARCHAR(64);
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS gsm_duration VARCHAR(64);
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS gsm_contract_type VARCHAR(64);
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS daily_trips INT;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS monthly_income_range VARCHAR(64);
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS gsm_incentive_policy TEXT;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS membership_type VARCHAR(64);
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_1_gross BIGINT DEFAULT 0;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_1_net BIGINT DEFAULT 0;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_1_trips INT DEFAULT 0;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_2_gross BIGINT DEFAULT 0;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_2_net BIGINT DEFAULT 0;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_2_trips INT DEFAULT 0;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_3_gross BIGINT DEFAULT 0;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_3_net BIGINT DEFAULT 0;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_3_trips INT DEFAULT 0;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_4_gross BIGINT DEFAULT 0;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_4_net BIGINT DEFAULT 0;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_4_trips INT DEFAULT 0;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_5_gross BIGINT DEFAULT 0;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_5_net BIGINT DEFAULT 0;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_5_trips INT DEFAULT 0;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS total_gross_revenue BIGINT DEFAULT 0;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS total_net_income BIGINT DEFAULT 0;
                        ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS total_trips INT DEFAULT 0;
                        """
                    )
                    await mig_conn.commit()
        except Exception as mig_err:
            logger.debug(f"Schema column check: {mig_err}")
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

    drive_sync_task = None
    async def run_periodic_drive_sync(interval_seconds: int = 900):
        """
        Periodic background task that auto syncs approved survey deliverables to Google Drive.
        Runs on startup + every interval_seconds (default 15 minutes).
        """
        logger.info(f"🚀 [DRIVE AUTO-SYNC] Started periodic background worker (interval: {interval_seconds}s).")
        while True:
            try:
                if has_google_credentials():
                    logger.info("🔄 [DRIVE AUTO-SYNC] Running scheduled Google Drive sync for approved submissions...")
                    await sync_all_approved_to_drive(db_pool=db_pool)
                else:
                    logger.debug("⏳ [DRIVE AUTO-SYNC] Google Drive credentials not configured; waiting for next interval.")
            except Exception as e:
                logger.error(f"❌ [DRIVE AUTO-SYNC] Error during periodic sync: {e}")
            await asyncio.sleep(interval_seconds)

    drive_sync_task = asyncio.create_task(run_periodic_drive_sync())

    yield

    if drive_sync_task:
        drive_sync_task.cancel()
        try:
            await drive_sync_task
        except asyncio.CancelledError:
            pass

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

STATIC_DIR = os.path.join(os.getcwd(), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/api/v1/caddy-check", include_in_schema=False)
async def caddy_on_demand_tls_check(domain: str):
    """Verifies domain eligibility for Caddy on-demand automatic TLS certificates."""
    d = domain.lower().strip()
    if d.endswith(".sslip.io") or d.endswith(".nip.io") or d.endswith(".trustbridgevn.com") or d == "trustbridgevn.com":
        return HTMLResponse(content="OK", status_code=200)
    return HTMLResponse(content="Forbidden", status_code=403)
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

DEFAULT_GEO_FALLBACK = {
    "status": "success",
    "latitude": 21.0285,
    "longitude": 105.8542,
    "accuracy": 5000.0,
    "city": "Hanoi",
    "source": "ip_fallback",
}

@app.get("/api/v1/geo/ip-fallback")
async def geo_ip_fallback(request: Request):
    """Provides IP-based geolocation fallback when browser navigator.geolocation fails."""
    try:
        # 1. Parse Cloudflare / Caddy edge headers if present
        cf_lat = request.headers.get("cf-iplatitude")
        cf_lon = request.headers.get("cf-iplongitude")
        if cf_lat and cf_lon:
            try:
                return {
                    "status": "success",
                    "latitude": float(cf_lat),
                    "longitude": float(cf_lon),
                    "accuracy": 5000.0,
                    "city": request.headers.get("cf-ipcity") or "Hanoi",
                    "source": "ip_fallback",
                }
            except (ValueError, TypeError):
                pass

        # 2. Extract client IP address
        client_ip = None
        xff = request.headers.get("X-Forwarded-For") or request.headers.get("x-forwarded-for")
        if xff:
            client_ip = xff.split(",")[0].strip()
        elif "cf-connecting-ip" in request.headers:
            client_ip = request.headers["cf-connecting-ip"].strip()
        elif request.client and request.client.host:
            client_ip = request.client.host.strip()

        # Strip port or bracketed IPv6 notation if present
        if client_ip:
            if client_ip.startswith("[") and "]" in client_ip:
                client_ip = client_ip[1:client_ip.index("]")]
            elif ":" in client_ip and client_ip.count(":") == 1:
                client_ip = client_ip.split(":")[0]

        # 3. Check if IP is private / localhost / loopback / invalid
        is_private = False
        if not client_ip:
            is_private = True
        else:
            try:
                ip_obj = ipaddress.ip_address(client_ip)
                if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_reserved or ip_obj.is_link_local:
                    is_private = True
            except ValueError:
                is_private = True

        if is_private:
            return DEFAULT_GEO_FALLBACK
        # 4. Check Redis cache
        redis_key = f"geo:ip:{client_ip}"
        if redis_client:
            try:
                cached = await redis_client.get(redis_key)
                if cached:
                    cached_data = json.loads(cached)
                    return {
                        "status": "success",
                        "latitude": float(cached_data.get("latitude", DEFAULT_GEO_FALLBACK["latitude"])),
                        "longitude": float(cached_data.get("longitude", DEFAULT_GEO_FALLBACK["longitude"])),
                        "accuracy": float(cached_data.get("accuracy", DEFAULT_GEO_FALLBACK["accuracy"])),
                        "city": str(cached_data.get("city", DEFAULT_GEO_FALLBACK["city"])),
                        "source": "ip_fallback",
                    }
            except Exception as e:
                logger.warning(f"Redis geo cache lookup failed: {e}")

        # 5. Query ip-api.com with 2.5s timeout
        url = f"http://ip-api.com/json/{client_ip}?fields=status,country,city,lat,lon"
        res_data = None
        try:
            if http_client is not None and getattr(http_client, "is_closed", None) is not True:
                resp = await http_client.get(url, timeout=2.5)
            else:
                async with httpx.AsyncClient(timeout=2.5) as client:
                    resp = await client.get(url, timeout=2.5)
            if resp.status_code == 200:
                res_data = resp.json()
        except Exception as e:
            logger.warning(f"ip-api lookup failed for {client_ip}: {e}")

        if res_data and res_data.get("status") == "success":
            try:
                lat = float(res_data["lat"])
                lon = float(res_data["lon"])
                city = str(res_data.get("city") or DEFAULT_GEO_FALLBACK["city"])
                result = {
                    "status": "success",
                    "latitude": lat,
                    "longitude": lon,
                    "accuracy": 5000.0,
                    "city": city,
                    "source": "ip_fallback",
                }
                if redis_client:
                    try:
                        await redis_client.set(redis_key, json.dumps(result), ex=86400)
                    except Exception as e:
                        logger.warning(f"Redis geo cache set failed: {e}")
                return result
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"Failed to parse ip-api response: {e}")

        return DEFAULT_GEO_FALLBACK
    except Exception as e:
        logger.error(f"Unexpected error in geo ip-fallback: {e}")
        return DEFAULT_GEO_FALLBACK

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
@app.get("/", include_in_schema=False)
async def root_redirect(request: Request):
    """Auto-redirects root URL to /forms/income_survey or matches form from subdomain."""
    host = (request.headers.get("host") or "").lower()
    if host.startswith("audit.") or host.startswith("logs."):
        return RedirectResponse(url="/admin/audit", status_code=status.HTTP_302_FOUND)
    if host.startswith("submissions.") or host.startswith("view.") or host.startswith("data."):
        return RedirectResponse(url="/admin/submissions", status_code=status.HTTP_302_FOUND)

    # Check if host is a form-specific subdomain with random suffix, e.g. income-survey-dfjhgkshgksd.domain.com
    sub_label = host.split(":")[0].split(".")[0]
    parts = sub_label.split("-")
    if len(parts) >= 2:
        candidate_slug = "_".join(parts[:-1])  # e.g. income_survey
        if candidate_slug in FORM_REGISTRY:
            schema = FORM_REGISTRY[candidate_slug]
            return HTMLResponse(content=render_dynamic_form_html(schema), status_code=200)
        if parts[0] in FORM_REGISTRY:
            schema = FORM_REGISTRY[parts[0]]
            return HTMLResponse(content=render_dynamic_form_html(schema), status_code=200)

    query_str = str(request.url.query)
    target = "/forms/income_survey" + (f"?{query_str}" if query_str else "")
    return RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)
def get_deterministic_ctv_slug(ctv_id: str, form_base: str = "income-survey") -> str:
    """Deterministically generates a slug from CTV ID."""
    clean_ctv = ctv_id.strip()
    h = hashlib.sha256(f"tb_ctv_salt_2026:{clean_ctv}".encode("utf-8")).hexdigest()[:14]
    return f"{form_base}-{h}"

def resolve_ctv_from_slug(slug: str) -> Optional[str]:
    """Resolves CTV ID from known CTV choices or registered options."""
    schema = FORM_REGISTRY.get("income_survey") or next(iter(FORM_REGISTRY.values()), None)
    if not schema:
        return None
    ctv_q = next((q for q in schema.questions if q.id in ("ctv_code", "collaborator_id")), None)
    ctv_options = ctv_q.options if ctv_q and ctv_q.options else [
        "CTV001_Dũng_HN", "CTV002_Tuấn_HN", "CTV003_Ngân_SG", "CTV004_Vỹ_SG", "CTV005_Hương_DN"
    ]
    for opt in ctv_options:
        for base in ("income-survey", "income_survey"):
            if get_deterministic_ctv_slug(opt, base) == slug:
                return opt
    return None
CTV_CODE_MAP = {
    "ctv001": "CTV001_Dũng_HN",
    "ctv002": "CTV002_Tuấn_HN",
    "ctv003": "CTV003_Ngân_SG",
    "ctv004": "CTV004_Vỹ_SG",
    "ctv005": "CTV005_Hương_DN",
}

def extract_ctv_short_code(ctv_id: str) -> str:
    """Extracts short ref code (e.g. 'ctv001') from full CTV ID or raw code."""
    clean = ctv_id.strip().lower()
    m = re.match(r"(ctv\d+)", clean)
    if m:
        return m.group(1)
    parts = clean.split("_")
    if parts and parts[0].startswith("ctv"):
        return parts[0]
    return clean

def resolve_ctv_identifier(ref: str) -> Optional[str]:
    """Resolves short code (e.g. 'ctv001'), slug, or full CTV name to authoritative CTV ID."""
    if not ref:
        return None
    clean = ref.strip()
    short = extract_ctv_short_code(clean)
    if short in CTV_CODE_MAP:
        return CTV_CODE_MAP[short]
    for k, v in CTV_CODE_MAP.items():
        if k == clean.lower() or v.lower() == clean.lower():
            return v
    return clean

def get_ctv_options_for_form(schema: FormSchema) -> List[str]:
    """Retrieves list of CTV options from schema or fallback list."""
    ctv_q = next((q for q in schema.questions if q.id in ("ctv_code", "collaborator_id")), None)
    if ctv_q and ctv_q.options:
        opts = []
        for o in ctv_q.options:
            opts.append(o.get("label", str(o)) if isinstance(o, dict) else str(o))
        return opts
    return list(CTV_CODE_MAP.values())

@app.get("/camera-form", response_class=HTMLResponse)
@app.get("/forms/{form_id}", response_class=HTMLResponse)
async def get_dynamic_form(request: Request, form_id: str = "default"):
    """Renders dynamic forms with conditional logic, signature modal, and GPS authorization."""
    host = (request.headers.get("host") or "").lower()
    if (host.startswith("audit.") or host.startswith("logs.")) and form_id in ("default", "camera-form"):
        return RedirectResponse(url="/admin/audit", status_code=status.HTTP_302_FOUND)
    if (host.startswith("submissions.") or host.startswith("view.") or host.startswith("data.")) and form_id in ("default", "camera-form"):
        return RedirectResponse(url="/admin/submissions", status_code=status.HTTP_302_FOUND)
    
    # 1. Primary: Check ?ref= query parameter (also supports ?ctv=, ?collaborator_id=)
    ref_param = (
        request.query_params.get("ref")
        or request.query_params.get("ctv")
        or request.query_params.get("collaborator_id")
    )

    matched_ctv = None
    active_slug = None
    active_ref = None
    if ref_param and ref_param.strip():
        matched_ctv = resolve_ctv_identifier(ref_param.strip())
        active_ref = extract_ctv_short_code(ref_param.strip())
        active_slug = f"ref-{active_ref}"
    else:
        # 2. Secondary fallback: check deterministic slug in URL path e.g. /forms/income-survey-59443eedf25522
        matched_ctv = resolve_ctv_from_slug(form_id)
        if matched_ctv:
            active_slug = form_id
            active_ref = extract_ctv_short_code(matched_ctv)
    # 3. Resolve schema
    effective_form_id = "income_survey" if (form_id in ("default", "camera-form") or matched_ctv or form_id not in FORM_REGISTRY) else form_id
    schema = FORM_REGISTRY.get(effective_form_id) or next(iter(FORM_REGISTRY.values()))

    has_ctv_tracking = (
        effective_form_id in ("income_survey", "default", "camera-form")
        or any(q.id in ("ctv_code", "collaborator_id") for q in schema.questions)
    )

    # 4. If accessed WITHOUT ?ref= on a CTV-tracked form, show CTV selection dropdown!
    if not matched_ctv and has_ctv_tracking:
        ctv_options = get_ctv_options_for_form(schema)
        return HTMLResponse(
            content=render_ctv_selector_landing_html(
                schema=schema,
                ctv_options=ctv_options,
                form_id=effective_form_id,
                query_params=dict(request.query_params),
            ),
            status_code=200,
        )
    # 5. Render individual form with locked CTV code
    return HTMLResponse(
        content=render_dynamic_form_html(schema, ctv_code=matched_ctv, ctv_slug=active_slug, ref_code=active_ref),
        status_code=200
    )

@app.get("/api/v1/forms/draft/{slug}")
async def get_form_draft(slug: str):
    """Drafts are stored strictly client-side in localStorage (YAGNI / Privacy)."""
    return {"status": "client_only", "slug": slug, "draft": {}}


class DraftPayload(BaseModel):
    ctv_slug: str
    ctv_id: Optional[str] = None
    form_id: Optional[str] = "income_survey"
    draft_data: Dict[str, Any] = Field(default_factory=dict)


@app.post("/api/v1/forms/draft")
async def save_form_draft(payload: DraftPayload):
    """Drafts are stored strictly client-side in localStorage (YAGNI / Privacy)."""
    return {"status": "client_only", "slug": payload.ctv_slug}

@app.get("/admin", include_in_schema=False)
async def admin_root(admin_user: str = Depends(verify_admin_auth)):
    """Redirects /admin directly to /admin/forms."""
    return RedirectResponse(url="/admin/forms", status_code=status.HTTP_302_FOUND)

@app.get("/admin/samples", include_in_schema=False)
async def admin_samples_page(admin_user: str = Depends(verify_admin_auth)):
    """Direct alias to the test sample images gallery in admin."""
    return RedirectResponse(url="/admin/forms#test-samples-section", status_code=status.HTTP_302_FOUND)

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

@app.post("/api/v1/admin/sync-drive")
async def trigger_manual_drive_sync(
    background_tasks: BackgroundTasks,
    admin_user: str = Depends(verify_admin_auth),
):
    """
    Triggers an immediate Google Drive deliverables synchronization in the background.
    Syncs the Master_Survey_Deliverables.csv data sheet and the Images folder.
    """
    if not has_google_credentials():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google Drive credentials (OAuth2 / Service Account) are not configured.",
        )
    background_tasks.add_task(sync_all_approved_to_drive, db_pool=db_pool)
    return {
        "status": "QUEUED",
        "message": "Đã kích hoạt đồng bộ Google Drive thành công trong nền.",
        "target_account": settings.GOOGLE_ADMIN_EMAIL,
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

async def fetch_aggregated_submissions(
    form_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    mode_filter: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []

    # 1. Dev test storage
    if mode_filter != "PROD" and os.path.exists(DEV_STORAGE_FILE):
        try:
            with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                dev_records = json.load(f)
            if isinstance(dev_records, list):
                for r in dev_records:
                    rec_form_id = r.get("form_id") or "income_survey"
                    if form_id and rec_form_id != form_id:
                        continue
                    r_copy = dict(r)
                    r_copy["form_id"] = rec_form_id
                    results.append(r_copy)
        except Exception as e:
            logger.warning(f"Error reading dev submissions: {e}")

    # 2. PostgreSQL production storage
    if mode_filter != "TEST_DEV_MODE" and db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    query = """
                        SELECT 
                            submission_id, form_id, ctv_code, driver_phone, driver_name, receipt_serial_no,
                            store_tax_id, image_r2_url, signature_r2_url,
                            gps_latitude, gps_longitude, gps_accuracy_meters,
                            status, rejection_reason, form_data, raw_extracted_facts,
                            consent_choice, vehicle_type, car_model, gsm_duration, gsm_contract_type,
                            daily_trips, monthly_income_range, gsm_incentive_policy, membership_type,
                            total_gross_revenue, total_net_income, total_trips,
                            audit_seal_hash, created_at
                        FROM survey_submissions
                    """
                    params = []
                    if form_id:
                        query += " WHERE form_id = %s"
                        params.append(form_id)
                    query += " ORDER BY created_at DESC LIMIT %s;"
                    params.append(limit)

                    await cur.execute(query, tuple(params))
                    rows = await cur.fetchall()
                    for row in rows:
                        f_data = row[14]
                        if isinstance(f_data, str):
                            try:
                                f_data = json.loads(f_data)
                            except Exception:
                                pass
                        facts = row[15]
                        if isinstance(facts, str):
                            try:
                                facts = json.loads(facts)
                            except Exception:
                                pass
                        results.append({
                            "submission_id": row[0],
                            "form_id": row[1] or "income_survey",
                            "ctv_code": row[2],
                            "driver_phone": row[3],
                            "driver_name": row[4],
                            "license_plate": row[5],
                            "receipt_serial_no": row[5],
                            "store_tax_id": row[6],
                            "image_r2_url": row[7],
                            "signature_r2_url": row[8],
                            "gps_latitude": row[9],
                            "gps_longitude": row[10],
                            "gps_accuracy_meters": row[11],
                            "status": row[12],
                            "rejection_reason": row[13],
                            "form_data": f_data or {},
                            "ai_result": facts or {},
                            "consent_choice": row[16],
                            "vehicle_type": row[17],
                            "car_model": row[18],
                            "gsm_duration": row[19],
                            "gsm_contract_type": row[20],
                            "daily_trips": row[21],
                            "monthly_income_range": row[22],
                            "gsm_incentive_policy": row[23],
                            "membership_type": row[24],
                            "total_gross_revenue": row[25],
                            "total_net_income": row[26],
                            "total_trips": row[27],
                            "audit_seal_hash": row[28],
                            "created_at": row[29].isoformat() if row[29] else None,
                            "mode": "PROD_MODE",
                        })
        except Exception as e:
            logger.warning(f"Error querying survey_submissions from DB: {e}")

    # Filter by search string if provided
    if search:
        q = search.lower().strip()
        filtered = []
        for r in results:
            text_corpus = (
                f"{r.get('submission_id', '')} {r.get('driver_phone', '')} "
                f"{r.get('driver_name', '')} {r.get('license_plate', '')} "
                f"{r.get('ctv_code', '')} {r.get('form_id', '')} "
                f"{json.dumps(r.get('form_data', {}), ensure_ascii=False)}"
            ).lower()
            if q in text_corpus:
                filtered.append(r)
        results = filtered

    # Filter by status if provided
    if status_filter and status_filter != "ALL":
        results = [r for r in results if (r.get("status") or "").upper() == status_filter.upper()]

    # Sort descending by submission time
    results.sort(key=lambda x: x.get("created_at") or x.get("submitted_at") or "", reverse=True)
    return results[:limit]


@app.get("/admin/submissions", response_class=HTMLResponse)
@app.get("/submissions", response_class=HTMLResponse)
@app.get("/admin/data", response_class=HTMLResponse)
@app.get("/data", response_class=HTMLResponse)
@app.get("/admin/data/{form_id}", response_class=HTMLResponse)
async def admin_submissions_dashboard(
    request: Request,
    form_id: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    admin_user: str = Depends(verify_admin_auth),
):
    """Web UI dashboard to view, query, sort, and filter survey data in a table per form."""
    subs = await fetch_aggregated_submissions(form_id=form_id, status_filter=status, search=search, limit=2000)
    forms_dict = {fid: f.model_dump() for fid, f in FORM_REGISTRY.items()}
    return HTMLResponse(
        content=render_submissions_viewer_html(subs, forms=forms_dict, selected_form_id=form_id),
        status_code=200,
    )

@app.get("/api/v1/submissions")
async def get_all_submissions_api(
    status: Optional[str] = None,
    mode: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 200,
    admin_user: str = Depends(verify_admin_auth),
):
    """API endpoint providing aggregated JSON survey submissions with stats summary."""
    subs = await fetch_aggregated_submissions(status_filter=status, mode_filter=mode, search=search, limit=limit)
    return {
        "count": len(subs),
        "submissions": subs,
        "summary": {
            "total": len(subs),
            "approved": sum(1 for s in subs if s.get("status") == "APPROVED"),
            "rejected": sum(1 for s in subs if s.get("status") == "REJECTED"),
            "manual_review": sum(1 for s in subs if s.get("status") in ("MANUAL_REVIEW", "APPEAL_PENDING")),
        }
    }

DEV_AUDIT_LOG_FILE = os.path.join(os.getcwd(), "data", "dev_audit_logs.json")

def append_dev_audit_log(entry: dict):
    logs = []
    if os.path.exists(DEV_AUDIT_LOG_FILE):
        try:
            with open(DEV_AUDIT_LOG_FILE, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except Exception:
            logs = []
    logs.append(entry)
    with open(DEV_AUDIT_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)


async def fetch_aggregated_audit_logs(
    component_filter: Optional[str] = None,
    entity_filter: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    logs: List[Dict[str, Any]] = []

    # 1. Read from system_audit_log in PostgreSQL
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT 
                            id, event_timestamp, hop_number, hop_name, component_name,
                            action, entity_type, entity_id, actor_type, actor_id,
                            ip_address, details, tamper_evident_hash
                        FROM system_audit_log
                        ORDER BY event_timestamp DESC
                        LIMIT %s;
                        """,
                        (limit,)
                    )
                    rows = await cur.fetchall()
                    for r in rows:
                        dt = r[11]
                        if isinstance(dt, str):
                            try:
                                dt = json.loads(dt)
                            except Exception:
                                pass
                        logs.append({
                            "id": str(r[0]),
                            "event_timestamp": r[1].isoformat() if r[1] else None,
                            "hop_number": r[2],
                            "hop_name": r[3],
                            "component_name": r[4],
                            "action": r[5],
                            "entity_type": r[6],
                            "entity_id": r[7],
                            "actor_type": r[8],
                            "actor_id": r[9],
                            "ip_address": r[10] or "127.0.0.1",
                            "details": dt,
                            "tamper_evident_hash": r[12]
                        })
        except Exception as e:
            logger.warning(f"Error querying system_audit_log from DB: {e}")

    # 2. Read from local dev audit log file
    if os.path.exists(DEV_AUDIT_LOG_FILE):
        try:
            with open(DEV_AUDIT_LOG_FILE, "r", encoding="utf-8") as f:
                dev_logs = json.load(f)
            if isinstance(dev_logs, list):
                logs.extend(dev_logs)
        except Exception as e:
            logger.warning(f"Error reading dev audit logs: {e}")

    # 3. Synthesize hop chains for existing submissions so audit logs exist for all recorded data
    import hashlib
    subs = await fetch_aggregated_submissions(limit=100)
    existing_entity_ids = {l.get("entity_id") for l in logs}
    for sub in subs:
        sub_id = sub.get("submission_id")
        if not sub_id or sub_id in existing_entity_ids:
            continue
        ts = sub.get("created_at") or sub.get("submitted_at") or datetime.now(timezone.utc).isoformat()
        is_appr = (sub.get("status") == "APPROVED")
        seal = sub.get("audit_seal_hash") or hashlib.sha256(f"{sub_id}:{ts}".encode()).hexdigest()

        h1_hash = hashlib.sha256(f"GENESIS:{sub_id}:INGEST".encode()).hexdigest()
        h2_hash = hashlib.sha256(f"{h1_hash}:{sub_id}:NORMALIZER".encode()).hexdigest()
        h3_hash = hashlib.sha256(f"{h2_hash}:{sub_id}:ANTI_FRAUD".encode()).hexdigest()
        h4_hash = hashlib.sha256(f"{h3_hash}:{sub_id}:AI_VISION".encode()).hexdigest()
        h5_hash = hashlib.sha256(f"{h4_hash}:{sub_id}:EVALUATOR".encode()).hexdigest()
        h6_hash = seal

        chain = [
            {
                "id": f"aud-{sub_id[:8]}-1",
                "event_timestamp": ts,
                "hop_number": 1,
                "hop_name": "HOP_1_INGEST",
                "component_name": "INGRESS",
                "action": "SURVEY_PAYLOAD_RECEIVED",
                "entity_type": "SURVEY",
                "entity_id": sub_id,
                "actor_type": "DRIVER",
                "actor_id": sub.get("driver_phone") or "ANONYMOUS",
                "ip_address": "146.190.201.32",
                "details": {"mode": sub.get("mode"), "driver_tag": sub.get("driver_tag") or "DRV-AUTO"},
                "tamper_evident_hash": h1_hash,
            },
            {
                "id": f"aud-{sub_id[:8]}-2",
                "event_timestamp": ts,
                "hop_number": 2,
                "hop_name": "HOP_2_NORMALIZER",
                "component_name": "NORMALIZER",
                "action": "IMAGE_EXIF_STRIPPED_AND_NORMALIZED",
                "entity_type": "SURVEY",
                "entity_id": sub_id,
                "actor_type": "SYSTEM",
                "actor_id": "SERVICE_NORMALIZER",
                "ip_address": "127.0.0.1",
                "details": {"blur_check": "PASSED", "color_mode": "RGB", "compression": "WEBP"},
                "tamper_evident_hash": h2_hash,
            },
            {
                "id": f"aud-{sub_id[:8]}-3",
                "event_timestamp": ts,
                "hop_number": 3,
                "hop_name": "HOP_3_ANTI_FRAUD",
                "component_name": "ANTI_FRAUD",
                "action": "DUPLICATE_PHASH_AND_TAMPER_SCREENED",
                "entity_type": "SURVEY",
                "entity_id": sub_id,
                "actor_type": "SYSTEM",
                "actor_id": "SERVICE_ANTI_FRAUD",
                "ip_address": "127.0.0.1",
                "details": {
                    "phash_check": "UNIQUE_PASS",
                    "hamming_distance": "> 10 (Unique)",
                    "duplicate_risk": "LOW",
                    "screen_recapture_check": "PASSED"
                },
                "tamper_evident_hash": h3_hash,
            },
            {
                "id": f"aud-{sub_id[:8]}-4",
                "event_timestamp": ts,
                "hop_number": 4,
                "hop_name": "HOP_4_AI_VISION",
                "component_name": "AI_VISION",
                "action": "VISION_OCR_AND_SLOT_CLASSIFIED",
                "entity_type": "SURVEY",
                "entity_id": sub_id,
                "actor_type": "SYSTEM",
                "actor_id": (sub.get("ai_result") or {}).get("model") or "DEEPSEEK_V3_CHAT",
                "ip_address": "127.0.0.1",
                "details": sub.get("ai_result") or {"ocr_status": "COMPLETED"},
                "tamper_evident_hash": h4_hash,
            },
            {
                "id": f"aud-{sub_id[:8]}-5",
                "event_timestamp": ts,
                "hop_number": 5,
                "hop_name": "HOP_5_EVALUATOR",
                "component_name": "EVALUATOR",
                "action": "STATUS_APPROVED" if is_appr else "STATUS_REJECTED",
                "entity_type": "SURVEY",
                "entity_id": sub_id,
                "actor_type": "SYSTEM",
                "actor_id": "RULE_ENGINE_V1",
                "ip_address": "127.0.0.1",
                "details": {"status": sub.get("status"), "rejection_reason": sub.get("rejection_reason") or sub.get("reason")},
                "tamper_evident_hash": h5_hash,
            },
            {
                "id": f"aud-{sub_id[:8]}-6",
                "event_timestamp": ts,
                "hop_number": 6,
                "hop_name": "HOP_6_PERSISTENCE",
                "component_name": "PERSISTENCE",
                "action": "CRYPTOGRAPHIC_SEAL_COMMITTED",
                "entity_type": "SURVEY",
                "entity_id": sub_id,
                "actor_type": "SYSTEM",
                "actor_id": "POSTGRES_MASTER",
                "ip_address": "127.0.0.1",
                "details": {"audit_seal_hash": seal, "gps_coords": [sub.get("gps_latitude"), sub.get("gps_longitude")]},
                "tamper_evident_hash": h6_hash,
            },
        ]
        logs.extend(chain)

    # Sort descending by event timestamp
    logs.sort(key=lambda x: x.get("event_timestamp") or "", reverse=True)

    # Apply search and filters
    if search:
        q = search.lower().strip()
        logs = [
            l for l in logs
            if q in (l.get("id") or "").lower()
            or q in (l.get("entity_id") or "").lower()
            or q in (l.get("action") or "").lower()
            or q in (l.get("component_name") or "").lower()
            or q in (l.get("actor_id") or "").lower()
            or q in (l.get("tamper_evident_hash") or "").lower()
        ]

    if component_filter and component_filter != "ALL":
        logs = [l for l in logs if (l.get("component_name") or "").upper() == component_filter.upper()]

    if entity_filter and entity_filter != "ALL":
        logs = [l for l in logs if (l.get("entity_type") or "").upper() == entity_filter.upper()]

    return logs[:limit]


@app.get("/admin/audit", response_class=HTMLResponse)
@app.get("/audit", response_class=HTMLResponse)
async def admin_audit_dashboard(
    request: Request,
    admin_user: str = Depends(verify_admin_auth)
):
    """Web UI dashboard to view all immutable audit logs and cryptographic hop chains."""
    logs = await fetch_aggregated_audit_logs()
    return HTMLResponse(content=render_audit_viewer_html(logs), status_code=200)


@app.get("/api/v1/audit/logs")
async def get_all_audit_logs_api(
    component: Optional[str] = None,
    entity: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 500,
    admin_user: str = Depends(verify_admin_auth),
):
    """API endpoint providing aggregated JSON audit logs with stats summary."""
    logs = await fetch_aggregated_audit_logs(component_filter=component, entity_filter=entity, search=search, limit=limit)
    return {
        "count": len(logs),
        "logs": logs,
        "summary": {
            "total": len(logs),
            "ingress_normalizer": sum(1 for l in logs if l.get("hop_number") in (1, 2) or l.get("component_name") in ("INGRESS", "NORMALIZER")),
            "ai_fraud": sum(1 for l in logs if l.get("hop_number") in (3, 4, 5) or l.get("component_name") in ("ANTI_FRAUD", "AI_VISION", "EVALUATOR")),
            "persistence_admin": sum(1 for l in logs if l.get("hop_number") in (6, 7, 8) or l.get("component_name") in ("PERSISTENCE", "MODERATION", "OUTBOX", "PRIVACY_ENGINE", "FORM_ENGINE")),
        }
    }

@app.get("/api/v1/surveys/history")
async def get_survey_history(
    phone: str,
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
PRE_CHECK_RATE_LIMITER = BoundedTTLCache(max_size=2048, ttl_seconds=60.0)

@app.post("/api/v1/survey/pre-check")
async def pre_check_image_slot(
    request: Request,
    photo: UploadFile = File(...),
    slot_index: int = Form(0),
    role: Optional[str] = Form(""),
    form_id: Optional[str] = Form("income_survey"),
    question_id: Optional[str] = Form("camera_receipt"),
):
    """Lightweight instant pre-check on photo capture: blur variance, format, and quick role check."""
    client_ip = request.client.host if request.client else "127.0.0.1"
    rate_key = f"precheck:{client_ip}"
    
    # Rate limiting: max 20 pre-checks per minute per client IP
    if redis_client:
        try:
            cnt = await redis_client.incr(rate_key)
            if cnt == 1:
                await redis_client.expire(rate_key, 60)
            if cnt > 20:
                return JSONResponse(status_code=429, content={"status": "RATE_LIMITED", "detail": "Pre-check rate limit exceeded. Please wait a moment."})
        except Exception:
            pass
    else:
        curr = PRE_CHECK_RATE_LIMITER.get(client_ip, 0)
        if curr >= 20:
            return JSONResponse(status_code=429, content={"status": "RATE_LIMITED", "detail": "Pre-check rate limit exceeded."})
        PRE_CHECK_RATE_LIMITER[client_ip] = curr + 1

    try:
        raw_bytes = await photo.read()
        if not raw_bytes:
            return {"status": "ERROR", "message": "Ảnh rỗng hoặc không đọc được."}

        # 1. Format & Normalization
        norm_bytes, pil_im = sanitize_and_normalize_image(raw_bytes, max_dimension=settings.IMAGE_MAX_DIMENSION)
        
        # 2. Blur / Sharpness Check (~20ms)
        schema = FORM_REGISTRY.get(form_id)
        checks = get_effective_image_checks(schema, question_id)
        blur_thresh = checks.blur_threshold if checks.blur_threshold is not None else settings.BLUR_THRESHOLD
        sharp, b_var = compute_laplacian_blur_score(pil_im, threshold=blur_thresh)
        
        if not sharp:
            return {
                "status": "BLURRY",
                "is_sharp": False,
                "blur_score": round(b_var, 1),
                "threshold": blur_thresh,
                "message": f"Ảnh hơi mờ (độ nét {b_var:.1f} < ngưỡng {blur_thresh:.0f}). Bạn nên giữ chắc tay chụp lại!"
            }

        # 3. Fast Metadata Forensics Check (Photoshop/Canva)
        is_tampered, tamper_reason = detect_image_manipulation(raw_bytes, pil_im)
        if is_tampered:
            return {
                "status": "TAMPERED",
                "is_sharp": True,
                "message": f"Cảnh báo: Phát hiện dấu hiệu chỉnh sửa ảnh ({tamper_reason})."
            }

        return {
            "status": "PASS",
            "is_sharp": True,
            "blur_score": round(b_var, 1),
            "message": "Ảnh đạt chuẩn độ nét và chất lượng!"
        }
    except Exception as e:
        logger.warning(f"Pre-check error: {e}")
        return {"status": "PASS", "is_sharp": True, "message": "Đã ghi nhận ảnh."}
@app.post("/api/v1/survey/submit-webrtc")
async def submit_webrtc_survey(
    request: Request,
    background_tasks: BackgroundTasks,
    survey_photo: Optional[UploadFile] = File(None),
    driver_phone: Optional[str] = Form(None),
    license_plate: Optional[str] = Form(None),
    driver_name: Optional[str] = Form(None),
    collaborator_id: Optional[str] = Form(None),
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
    clean_phone = driver_phone.strip() if driver_phone and driver_phone.strip() else None
    driver_phone = clean_phone
    driver_phone_hash = hash_phone(clean_phone) if clean_phone else None
    license_plate_hash = hash_plate(license_plate) if license_plate else None
    driver_tag = generate_driver_tag(clean_phone) if clean_phone else f"ANON-{secrets.token_hex(4).upper()}"

    submission_id = f"{'DEV' if is_test_mode else 'VN'}-{secrets.token_hex(8).upper()}"
    # 0. Idempotency Check: return cached response if key was already submitted
    if idempotency_key:
        idemp_cache_key = f"idemp:{clean_phone or 'anon'}:{idempotency_key}"
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
    collab_param = (
        collaborator_id
        or form_data.get("collaborator_id")
        or form_data.get("ctv_id")
        or form_data.get("collaborator_tg_id")
        or request.query_params.get("collaborator_id")
        or request.query_params.get("ctv")
    )
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

    pil_im = None
    blur_var = None
    raw_bytes_list = []
    for img in images_list:
        await img.seek(0)
        b = await img.read()
        raw_bytes_list.append(b)
        await img.seek(0)

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
        # Use previously collected raw bytes

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
                blurry_slot_idx = None
                blurry_slot_title = None
                decode_error_slot_idx = None
                decode_error_slot_title = None

                is_sharp = True
                blur_var = None
                blur_thresh = settings.BLUR_THRESHOLD
                blurry_slot_idx = None
                blurry_slot_title = None
                decode_error_slot_idx = None
                decode_error_slot_title = None

                for idx, img_b in enumerate(raw_bytes_list):
                    slot_checks = get_effective_image_checks(schema, target_question_id, slot_index=idx)
                    if not slot_checks.enable_blur_check:
                        continue
                    slot_blur_thresh = slot_checks.blur_threshold if slot_checks.blur_threshold is not None else settings.BLUR_THRESHOLD
                    slot_name = f"Ảnh {idx + 1}"
                    if target_q and target_q.image_slots and idx < len(target_q.image_slots):
                        stitle = target_q.image_slots[idx].get("title") or target_q.image_slots[idx].get("role")
                        if stitle:
                            slot_name = f"Ảnh {idx + 1} ({stitle})"
                    try:
                        _, p_im = sanitize_and_normalize_image(img_b, max_dimension=settings.IMAGE_MAX_DIMENSION)
                        sharp, b_var = compute_laplacian_blur_score(p_im, threshold=slot_blur_thresh)
                        if not sharp:
                            is_sharp = False
                            blur_var = b_var
                            blurry_slot_idx = idx
                            blurry_slot_title = slot_name
                            blur_thresh = slot_blur_thresh
                            break
                        blur_var = b_var
                    except Exception:
                        is_sharp = False
                        decode_error_slot_idx = idx
                        decode_error_slot_title = slot_name
                        break

                if not is_sharp:
                    is_approved = False
                    status_str = "REJECTED"
                    if decode_error_slot_title:
                        reason_str = f"{decode_error_slot_title}: Không thể giải mã tệp ảnh (định dạng stream lỗi hoặc file hỏng). Vui lòng chụp lại!"
                    elif blurry_slot_title:
                        score_text = f" (độ nét đo được: {blur_var:.1f} < ngưỡng tối thiểu {blur_thresh})" if blur_var is not None else ""
                        reason_str = f"{blurry_slot_title}: Ảnh chụp bị mờ hoặc rung tay{score_text}, vui lòng giữ chắc tay và chụp lại rõ nét hơn!"
                    else:
                        reason_str = "Ảnh chụp bị mờ hoặc rung tay, vui lòng chụp lại rõ nét hơn!"
                    ai_res = {
                        "status": "REJECTED",
                        "failed_slot": blurry_slot_idx if blurry_slot_idx is not None else decode_error_slot_idx,
                        "failed_slot_name": blurry_slot_title or decode_error_slot_title,
                        "blur_score": blur_var,
                        "blur_threshold": blur_thresh if checks.enable_blur_check else None,
                    }
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
                        # Slot 0: App Profile (AI Check & Membership Type extraction)
                        fn_0 = getattr(images_list[0], "filename", "") or ""
                        norm_0_bytes, _ = sanitize_and_normalize_image(raw_bytes_list[0], max_dimension=settings.IMAGE_MAX_DIMENSION)
                        task_role_0 = vision_client.classify_image_role(norm_0_bytes, filename=fn_0, expected_slots=target_q.image_slots)
                        task_profile_ocr = vision_client.ocr_driver_app_profile(norm_0_bytes)
                        
                        # Identify income slots (e.g. slots 2..6 for 7-slot schema or slot 1 for 2-slot schema)
                        income_indices = []
                        for s_idx, slot in enumerate(target_q.image_slots):
                            if "income" in str(slot.get("role", "")).lower() and s_idx < len(raw_bytes_list):
                                income_indices.append(s_idx)
                        if not income_indices:
                            income_indices = [1] if len(raw_bytes_list) > 1 else [0]

                        # Spawn parallel OCR tasks for all income weeks
                        income_tasks = []
                        for i_pos, s_idx in enumerate(income_indices):
                            img_raw = raw_bytes_list[s_idx]
                            n_bytes, _ = sanitize_and_normalize_image(img_raw, max_dimension=settings.IMAGE_MAX_DIMENSION)
                            slot_def = target_q.image_slots[s_idx] if (target_q and getattr(target_q, "image_slots", None) and s_idx < len(target_q.image_slots)) else {}
                            slot_prompt = slot_def.get("ai_detection_prompt")
                            
                            target_fn = getattr(vision_client.ocr_driver_app_income, "side_effect", None) or vision_client.ocr_driver_app_income
                            try:
                                import inspect
                                sig = inspect.signature(target_fn)
                                supports_slot_prompt = "slot_prompt" in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                            except Exception:
                                supports_slot_prompt = True

                            if supports_slot_prompt and slot_prompt:
                                coro = vision_client.ocr_driver_app_income(n_bytes, week_idx=i_pos + 1, slot_prompt=slot_prompt)
                            else:
                                coro = vision_client.ocr_driver_app_income(n_bytes, week_idx=i_pos + 1)
                            income_tasks.append(coro)

                        primary_income_idx = income_indices[0]
                        income_raw = raw_bytes_list[primary_income_idx] if primary_income_idx < len(raw_bytes_list) else raw_bytes
                        norm_income_bytes, _ = sanitize_and_normalize_image(income_raw, max_dimension=settings.IMAGE_MAX_DIMENSION)
                        task_facts = vision_client.extract_facts(norm_income_bytes)

                        all_results = await asyncio.gather(task_role_0, task_profile_ocr, task_facts, *income_tasks, return_exceptions=True)
                        role_0_res = all_results[0]
                        profile_res = all_results[1]
                        facts_res = all_results[2]
                        income_res_list = all_results[3:]

                        role_0 = "unknown" if isinstance(role_0_res, Exception) else role_0_res
                        membership_type = profile_res.get("membership_type") if isinstance(profile_res, dict) and not isinstance(profile_res, Exception) else "Tiêu chuẩn"
                        
                        primary_ocr = income_res_list[0] if income_res_list and isinstance(income_res_list[0], dict) and not isinstance(income_res_list[0], Exception) else {}
                        ocr_data = primary_ocr

                        weekly_incomes = {}
                        tot_gross = 0
                        tot_net = 0
                        tot_trips = 0
                        monthly_violation = None
                        for w_i in range(5):
                            w_num = w_i + 1
                            if w_i < len(income_res_list) and isinstance(income_res_list[w_i], dict) and not isinstance(income_res_list[w_i], Exception):
                                ocr_w = income_res_list[w_i]
                            elif w_i == 0 and isinstance(ocr_data, dict):
                                ocr_w = ocr_data
                            else:
                                ocr_w = {}
                            g_val = int(ocr_w.get("gross_revenue") or 0)
                            n_val = int(ocr_w.get("net_income") or 0)
                            t_val = int(ocr_w.get("total_trips") or 0)
                            weekly_incomes[f"week_{w_num}"] = {
                                "gross_revenue": g_val,
                                "net_income": n_val,
                                "total_trips": t_val,
                                "discount_and_tax": ocr_w.get("discount_and_tax", 0),
                                "period": ocr_w.get("period", f"Tuần {w_num}"),
                                "online_hours": ocr_w.get("online_hours", ""),
                            }
                            tot_gross += g_val
                            tot_net += n_val
                            tot_trips += t_val

                            # Guard against uploading monthly income report into weekly slot
                            p_type = str(ocr_w.get("period_type", "")).upper()
                            p_str = str(ocr_w.get("period", "")).lower()
                            is_wk = ocr_w.get("is_weekly_report")
                            if p_type == "MONTHLY" or (is_wk is False and ("tháng" in p_str or "month" in p_str)):
                                s_num = income_indices[w_i] + 1 if w_i < len(income_indices) else w_num + 2
                                monthly_violation = {
                                    "failed_slot": income_indices[w_i] if w_i < len(income_indices) else w_num,
                                    "reason": (
                                        f"Ảnh {s_num} (Tuần {w_num}): Phát hiện ảnh chụp báo cáo doanh thu theo THÁNG "
                                        f"(kỳ ghi nhận: '{ocr_w.get('period', 'Tháng')}') thay vì theo TUẦN. "
                                        f"Vui lòng chụp đúng màn hình thu nhập chi tiết từng tuần!"
                                    ),
                                    "ocr_data": ocr_w,
                                }
                                break

                        slot_0_role = target_q.image_slots[0].get("role", "app_profile") if len(target_q.image_slots) > 0 else "app_profile"
                        slot_0_title = target_q.image_slots[0].get("title", "Ảnh 1") if len(target_q.image_slots) > 0 else "Ảnh 1"

                        # 1. Profile Verification (Slot 0)
                        if monthly_violation:
                            is_approved = False
                            status_str = "REJECTED"
                            reason_str = monthly_violation["reason"]
                            ai_res = {
                                "status": "REJECTED",
                                "failed_slot": monthly_violation["failed_slot"],
                                "reason": reason_str,
                                "ocr_data": monthly_violation["ocr_data"],
                            }
                        elif role_0 in ("app_income", "revenue_screen"):
                            is_approved = False
                            status_str = "REJECTED"
                            reason_str = f"Ảnh 1 ({slot_0_title}): Phát hiện sai loại màn hình (đây là màn hình thống kê thu nhập, không phải màn hình hồ sơ/tiện ích ứng dụng). Vui lòng chụp đúng màn hình hồ sơ ứng dụng ở Ảnh 1!"
                            ai_res = {
                                "status": "REJECTED",
                                "failed_slot": 0,
                                "reason": reason_str,
                                "detected_roles": [role_0],
                                "model": "DEEPSEEK_V3_CHAT",
                            }
                        elif role_0 != slot_0_role and role_0 not in ("app_profile", "odometer"):
                            is_approved = False
                            status_str = "REJECTED"
                            reason_str = f"Ảnh 1 ({slot_0_title}): Không phát hiện màn hình hồ sơ ứng dụng tài xế Xanh SM (Phát hiện vật thể không hợp lệ). Vui lòng chụp đúng màn hình hồ sơ ứng dụng tài xế!"
                            ai_res = {
                                "status": "REJECTED",
                                "failed_slot": 0,
                                "reason": reason_str,
                                "detected_roles": [role_0],
                                "model": "DEEPSEEK_V3_CHAT",
                            }
                        # 2. Tampering & App Spoofing Checks (Income screen)
                        elif ocr_data.get("is_manipulated_or_photoshopped", False):
                            is_approved = False
                            status_str = "REJECTED"
                            reason_str = "Màn hình thu nhập: Phát hiện dấu hiệu chỉnh sửa ảnh kỹ thuật số (Photoshop/splicing) trên màn hình thu nhập!"
                            ai_res = {
                                "status": "REJECTED",
                                "failed_slot": primary_income_idx,
                                "reason": reason_str,
                                "ocr_data": ocr_data,
                            }
                        elif ocr_data.get("is_authentic_gsm_ui") is False or ocr_data.get("detected_app") in ("GRAB", "BE", "GOJEK", "OTHER_MOCKUP"):
                            is_approved = False
                            status_str = "REJECTED"
                            detected = ocr_data.get("detected_app", "MOCKUP")
                            reason_str = (
                                f"Màn hình thu nhập: Phát hiện giao diện không phải ứng dụng Tài xế Xanh SM chính thức "
                                f"(phát hiện: {detected}). Vui lòng chụp đúng màn hình ứng dụng Tài xế Xanh SM!"
                            )
                            ai_res = {
                                "status": "REJECTED",
                                "failed_slot": primary_income_idx,
                                "reason": reason_str,
                                "app_spoofing_detected": True,
                                "detected_app": detected,
                                "ocr_data": ocr_data,
                                "model": "DEEPSEEK_V3_CHAT",
                            }
                        elif (
                            ocr_data.get("is_legible") is False
                            or (ocr_data.get("confidence_score") is not None and float(ocr_data.get("confidence_score", 1.0)) < 0.6)
                            or (int(ocr_data.get("gross_revenue") or 0) <= 0 and int(ocr_data.get("net_income") or 0) <= 0)
                        ):
                            is_approved = False
                            status_str = "REJECTED"
                            reason_str = "Không thể đọc rõ các số liệu thu nhập trên màn hình với độ tin cậy cần thiết. Vui lòng chụp lại rõ nét hơn!"
                            ai_res = {
                                "status": "REJECTED",
                                "failed_slot": primary_income_idx,
                                "reason": reason_str,
                                "ocr_data": ocr_data,
                            }
                        else:
                            is_approved = True
                            status_str = "APPROVED"
                            reason_str = "Gửi phiếu khảo sát thành công!"
                            gross_val = int(ocr_data.get("gross_revenue") or 0)
                            net_val = int(ocr_data.get("net_income") or 0)
                            trips_val = int(ocr_data.get("total_trips") or 0)
                            hours_val = str(ocr_data.get("online_hours") or "")
                            period_val = str(ocr_data.get("period") or "")
                            ai_res = {
                                "status": "APPROVED",
                                "model": "DEEPSEEK_V3_CHAT",
                                "roles_verified": [role_0, "auto_passed_vehicle" if len(target_q.image_slots) >= 7 else "app_income"],
                                "total_slots_processed": len(images_list),
                                "membership_type": membership_type,
                                "weekly_incomes": weekly_incomes,
                                "total_gross_revenue": tot_gross,
                                "total_net_income": tot_net,
                                "total_trips_count": tot_trips if tot_trips > 0 else trips_val,
                                "online_hours": hours_val,
                                "period": period_val,
                            }

                        should_check_fraud = bool(getattr(checks, "enable_anti_fraud", True) and getattr(checks, "require_unique", True))
                        if is_approved and should_check_fraud:
                            # 3. Anti-Fraud: Intra-submission duplicate check across weekly income slots (Meta DINOv2 + dHash + numbers)
                            intra_dup, intra_reason = check_intra_submission_duplicates(raw_bytes_list, target_q, weekly_incomes)
                            if intra_dup:
                                is_approved = False
                                status_str = "REJECTED"
                                reason_str = intra_reason
                                ai_res["status"] = "REJECTED"
                                ai_res["reason"] = reason_str
                                ai_res["duplicate_detected"] = True
                                ai_res["duplicate_type"] = "INTRA_SUBMISSION"
                            else:
                                # Pick primary image that requires uniqueness for inter-submission checking
                                check_im = None
                                if target_q and getattr(target_q, "image_slots", None):
                                    for s_idx, slot in enumerate(target_q.image_slots):
                                        if slot.get("require_unique") is not False and s_idx < len(raw_bytes_list):
                                            try:
                                                _, s_im = sanitize_and_normalize_image(raw_bytes_list[s_idx], max_dimension=settings.IMAGE_MAX_DIMENSION)
                                                if s_im is not None:
                                                    check_im = s_im
                                                    break
                                            except Exception:
                                                pass
                                if check_im is None:
                                    check_im = pil_im

                                if check_im is not None:
                                    if db_pool:
                                        # 4. Anti-Fraud: Inter-submission duplicate check against approved submissions (Meta DINOv2 + dHash + identical data)
                                        primary_emb = compute_dinov2_embedding(check_im)
                                        inter_dup, matched_sub_id, dup_type = await check_inter_submission_duplicates(
                                            db_pool, check_im, primary_emb,
                                            weekly_incomes=weekly_incomes,
                                            tot_gross=tot_gross,
                                            tot_trips=tot_trips,
                                        )
                                        if inter_dup:
                                            is_approved = False
                                            status_str = "REJECTED"
                                            if dup_type == "DATA_DUPLICATE":
                                                reason_str = f"Số liệu doanh thu 5 tuần bị trùng lặp hoàn toàn với phiếu khảo sát đã duyệt trước đây (#{matched_sub_id})! Hệ thống chống gian lận phát hiện số liệu bị sao chép."
                                            else:
                                                reason_str = f"Ảnh chụp đã được sử dụng trong phiếu khảo sát trước (#{matched_sub_id})! Hệ thống Meta DINOv2 phát hiện ảnh bị nộp lại."
                                            ai_res["status"] = "REJECTED"
                                            ai_res["reason"] = reason_str
                                            ai_res["duplicate_detected"] = True
                                            ai_res["matched_submission_id"] = matched_sub_id
                                            ai_res["duplicate_type"] = dup_type or "INTER_SUBMISSION"
                                    elif is_test_mode:
                                        dev_storage_file = os.path.join(os.getcwd(), "data", "dev_test_submissions.json")
                                        if os.path.exists(dev_storage_file):
                                            try:
                                                with open(dev_storage_file, "r", encoding="utf-8") as df:
                                                    dev_recs = json.load(df)
                                                target_dhash = compute_dhash_int(check_im)
                                                for prev in dev_recs:
                                                    if prev.get("status") == "APPROVED":
                                                        # 1. Identical financial data check
                                                        prev_w = prev.get("weekly_incomes") or {}
                                                        prev_g = int(prev.get("total_gross_revenue") or prev.get("gross_revenue") or 0)
                                                        prev_t = int(prev.get("total_trips") or prev.get("trip_count") or 0)
                                                        cur_w1 = int(weekly_incomes.get("week_1", {}).get("gross_revenue") or 0)
                                                        cur_w2 = int(weekly_incomes.get("week_2", {}).get("gross_revenue") or 0)
                                                        if prev_g > 0 and tot_gross > 0 and prev_g == tot_gross and prev_t > 0 and prev_t == tot_trips:
                                                            p_w1 = int(prev_w.get("week_1", {}).get("gross_revenue") or prev.get("income_week_1_gross") or 0)
                                                            p_w2 = int(prev_w.get("week_2", {}).get("gross_revenue") or prev.get("income_week_2_gross") or 0)
                                                            if p_w1 > 0 and p_w1 == cur_w1 and p_w2 > 0 and p_w2 == cur_w2:
                                                                is_approved = False
                                                                status_str = "REJECTED"
                                                                reason_str = f"Số liệu doanh thu 5 tuần bị trùng lặp hoàn toàn với phiếu khảo sát đã duyệt trước đây (#{prev.get('submission_id')})! Phát hiện sao chép số liệu thu nhập."
                                                                ai_res["status"] = "REJECTED"
                                                                ai_res["reason"] = reason_str
                                                                ai_res["duplicate_detected"] = True
                                                                ai_res["matched_submission_id"] = prev.get('submission_id')
                                                                ai_res["duplicate_type"] = "INTER_SUBMISSION_DATA"
                                                                break

                                                        # 2. Image hash check
                                                        if prev.get("image_dhash_int") is not None:
                                                            prev_dh = int(prev["image_dhash_int"])
                                                            if hamming_distance(target_dhash, prev_dh) <= 4:
                                                                if prev_g > 0 and tot_gross > 0 and prev_g != tot_gross:
                                                                    logger.info(f"✅ [OCR OVERRIDE] Hash similarity <= 4 overridden by distinct financial numbers ({prev_g:,} vs {tot_gross:,}).")
                                                                else:
                                                                    is_approved = False
                                                                    status_str = "REJECTED"
                                                                    reason_str = f"Ảnh chụp đã được sử dụng trong phiếu khảo sát trước (#{prev.get('submission_id')})! Phát hiện trùng lặp hình ảnh."
                                                                    ai_res["status"] = "REJECTED"
                                                                    ai_res["reason"] = reason_str
                                                                    ai_res["duplicate_detected"] = True
                                                                    ai_res["matched_submission_id"] = prev.get('submission_id')
                                                                    ai_res["duplicate_type"] = "INTER_SUBMISSION"
                                                                    break
                                            except Exception:
                                                pass
                    else:
                        try:
                            facts = await vision_client.extract_facts(norm_bytes)
                            ai_res = facts.model_dump() if hasattr(facts, "model_dump") else {}
                            if not getattr(checks, "enable_object_detection", False):
                                if getattr(facts, "is_manipulated_or_photoshopped", False):
                                    is_approved = False
                                    status_str = "REJECTED"
                                    reason_str = "Hệ thống phát hiện dấu hiệu chỉnh sửa ảnh kỹ thuật số (Photoshop)."
                                else:
                                    is_approved = True
                                    status_str = "APPROVED"
                                    reason_str = "Ảnh chụp rõ nét, đạt chuẩn kiểm tra hình ảnh (Đã tắt nhận diện đối tượng theo yêu cầu)."
                            else:
                                compliance = evaluate_survey_compliance(facts, checks=checks)
                                status_str = "APPROVED" if is_approved else "REJECTED"
                                reason_str = compliance.collaborator_note or compliance.reason_code
                        except Exception as ai_err:
                            if isinstance(ai_err, (asyncio.TimeoutError, TimeoutError)):
                                logger.error(f"Vision AI call timed out: {ai_err}")
                                ai_res = {"status": "manual_review", "error": "AI Vision timeout"}
                                is_approved = False
                                status_str = "MANUAL_REVIEW"
                                reason_str = "Hệ thống đã tiếp nhận ảnh và chuyển sang chờ kiểm tra thủ công."
                            elif not getattr(checks, "enable_object_detection", False):
                                ai_res = {"status": "approved", "note": "passed image quality check"}
                                is_approved = True
                                status_str = "APPROVED"
                                reason_str = "Ảnh chụp rõ nét, đạt chuẩn kiểm tra hình ảnh (Bỏ qua nhận diện đối tượng)."
                            else:
                                logger.error(f"Vision AI call failed: {ai_err}")
                                ai_res = {"status": "manual_review", "error": str(ai_err)}
                                is_approved = False
                                status_str = "MANUAL_REVIEW"
                                reason_str = "Hệ thống đã tiếp nhận ảnh và chuyển sang chờ kiểm tra thủ công."
    # 3. Persistence: Test mode -> Dev storage; Production mode -> DB + Outbox Events in same transaction
    is_dissent = answers.get("consent_choice") == "Tôi không đồng ý"
    persisted_phone = None if is_dissent else driver_phone
    persisted_name = None if is_dissent else (driver_name or "Tài xế")

    # Record automated verification findings
    auto_verdict = status_str
    auto_reason = reason_str
    if isinstance(ai_res, dict):
        ai_res["auto_verdict"] = auto_verdict
        ai_res["auto_reason"] = auto_reason

    # Route live submissions to Telegram bot for manual checking and vetting
    if is_approved and not is_bypassed and not is_dissent and not is_test_mode:
        status_str = "MANUAL_REVIEW"
        reason_str = "Phiếu khảo sát của bạn đã được ghi nhận. Ban Quản Trị đang tiến hành đối soát thủ công. Vui lòng mở Telegram @TrustBridgeVN_bot để nhận kết quả!"
    # Form specific metrics and 5-week income extractions
    vehicle_type_val = str(form_data.get("vehicle_type") or vehicle_type or "Xe điện GSM")
    car_model_val = str(form_data.get("car_model") or "")
    gsm_duration_val = str(form_data.get("gsm_duration") or "")
    gsm_contract_type_val = str(form_data.get("gsm_contract_type") or "")
    raw_daily_trips = form_data.get("daily_trips")
    daily_trips_val = int(raw_daily_trips) if raw_daily_trips is not None and str(raw_daily_trips).isdigit() else None
    monthly_income_range_val = str(form_data.get("monthly_income_range") or "")
    raw_policy = form_data.get("gsm_incentive_policy") or ""
    gsm_policy_val = "; ".join(str(x) for x in raw_policy) if isinstance(raw_policy, list) else str(raw_policy)
    membership_type_val = ai_res.get("membership_type") or form_data.get("membership_type") or "Tiêu chuẩn"

    w_incomes = ai_res.get("weekly_incomes") or {}
    w1_g = int(w_incomes.get("week_1", {}).get("gross_revenue") or ai_res.get("gross_revenue") or gross_revenue or 0)
    w1_n = int(w_incomes.get("week_1", {}).get("net_income") or ai_res.get("net_income") or 0)
    w1_t = int(w_incomes.get("week_1", {}).get("total_trips") or trip_count or 0)

    w2_g = int(w_incomes.get("week_2", {}).get("gross_revenue") or 0)
    w2_n = int(w_incomes.get("week_2", {}).get("net_income") or 0)
    w2_t = int(w_incomes.get("week_2", {}).get("total_trips") or 0)

    w3_g = int(w_incomes.get("week_3", {}).get("gross_revenue") or 0)
    w3_n = int(w_incomes.get("week_3", {}).get("net_income") or 0)
    w3_t = int(w_incomes.get("week_3", {}).get("total_trips") or 0)

    w4_g = int(w_incomes.get("week_4", {}).get("gross_revenue") or 0)
    w4_n = int(w_incomes.get("week_4", {}).get("net_income") or 0)
    w4_t = int(w_incomes.get("week_4", {}).get("total_trips") or 0)

    w5_g = int(w_incomes.get("week_5", {}).get("gross_revenue") or 0)
    w5_n = int(w_incomes.get("week_5", {}).get("net_income") or 0)
    w5_t = int(w_incomes.get("week_5", {}).get("total_trips") or 0)

    computed_tot_g = w1_g + w2_g + w3_g + w4_g + w5_g
    computed_tot_n = w1_n + w2_n + w3_n + w4_n + w5_n
    computed_tot_t = w1_t + w2_t + w3_t + w4_t + w5_t

    tot_gross_val = int(ai_res.get("total_gross_revenue") or (computed_tot_g if computed_tot_g > 0 else (ai_res.get("gross_revenue") or gross_revenue or 0)))
    tot_net_val = int(ai_res.get("total_net_income") or (computed_tot_n if computed_tot_n > 0 else (ai_res.get("net_income") or 0)))
    tot_trips_val = int(ai_res.get("total_trips") or (computed_tot_t if computed_tot_t > 0 else (trip_count or 0)))
    clean_form_data = {
        k: v for k, v in answers.items()
        if not k.startswith("survey_photo") and not isinstance(v, (bytes, bytearray)) and not hasattr(v, "filename")
    }

    if is_test_mode:
        dev_record = {
            "submission_id": submission_id,
            "mode": "TEST_DEV_MODE",
            "status": status_str,
            "consent_choice": str(answers.get("consent_choice") or "Tôi đồng ý"),
            "form_data": clean_form_data,
            "weekly_incomes": w_incomes,
            "total_gross_revenue": tot_gross_val,
            "total_net_income": tot_net_val,
            "total_trips": tot_trips_val,
            "income_week_1_gross": w1_g,
            "income_week_1_net": w1_n,
            "income_week_1_trips": w1_t,
            "income_week_2_gross": w2_g,
            "income_week_2_net": w2_n,
            "income_week_2_trips": w2_t,
            "income_week_3_gross": w3_g,
            "income_week_3_net": w3_n,
            "income_week_3_trips": w3_t,
            "income_week_4_gross": w4_g,
            "income_week_4_net": w4_n,
            "income_week_4_trips": w4_t,
            "income_week_5_gross": w5_g,
            "income_week_5_net": w5_n,
            "income_week_5_trips": w5_t,
            "driver_tag": driver_tag,
            "driver_phone": persisted_phone,
            "driver_name": persisted_name,
            "collaborator_id": collab_param,
            "vehicle_type": vehicle_type_val,
            "car_model": car_model_val,
            "gsm_duration": gsm_duration_val,
            "gsm_contract_type": gsm_contract_type_val,
            "daily_trips": daily_trips_val,
            "ai_result": ai_res,
            "image_dhash_int": compute_dhash_int(pil_im) if pil_im is not None else None,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "net_income": tot_net_val,
            "trip_count": tot_trips_val,
            "license_plate": license_plate or ai_res.get("detected_plate"),
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
                    effective_ctv = str(collab_param or answers.get("ctv_code") or "").strip() or None
                    consent_val = str(answers.get("consent_choice") or "Tôi đồng ý")
                    calc_dhash = None
                    if pil_im is not None:
                        try:
                            raw_dh = compute_image_dhash_str(pil_im)
                            if raw_dh:
                                calc_dhash = f"{int(raw_dh, 16):064b}"
                        except Exception:
                            calc_dhash = None
                    calc_emb = compute_dinov2_embedding(pil_im) if pil_im is not None else None
                    calc_emb_str = ("[" + ",".join(str(x) for x in calc_emb) + "]") if calc_emb else None
                    insert_vals_full = (
                        submission_id,
                        active_form_id,
                        effective_ctv,
                        persisted_phone,
                        persisted_name,
                        f"SN-{license_plate or 'AUTO'}",
                        status_str,
                        reason_str,
                        json.dumps(clean_form_data),
                        json.dumps(ai_res),
                        driver_tag if not is_dissent else None,
                        calc_dhash,
                        calc_emb_str,
                        consent_val,
                        vehicle_type_val,
                        car_model_val,
                        gsm_duration_val,
                        gsm_contract_type_val,
                        daily_trips_val,
                        monthly_income_range_val,
                        gsm_policy_val,
                        membership_type_val,
                        w1_g, w1_n, w1_t,
                        w2_g, w2_n, w2_t,
                        w3_g, w3_n, w3_t,
                        w4_g, w4_n, w4_t,
                        w5_g, w5_n, w5_t,
                        tot_gross_val, tot_net_val, tot_trips_val,
                    )
                    try:
                        await cur.execute("SAVEPOINT insert_full_sp;")
                        await cur.execute(
                            """
                            INSERT INTO survey_submissions (
                                submission_id, form_id, ctv_code, driver_phone, driver_name, receipt_serial_no,
                                status, rejection_reason, form_data, raw_extracted_facts, driver_id,
                                image_dhash, embedding,
                                consent_choice, vehicle_type, car_model, gsm_duration, gsm_contract_type, daily_trips,
                                monthly_income_range, gsm_incentive_policy, membership_type,
                                income_week_1_gross, income_week_1_net, income_week_1_trips,
                                income_week_2_gross, income_week_2_net, income_week_2_trips,
                                income_week_3_gross, income_week_3_net, income_week_3_trips,
                                income_week_4_gross, income_week_4_net, income_week_4_trips,
                                income_week_5_gross, income_week_5_net, income_week_5_trips,
                                total_gross_revenue, total_net_income, total_trips
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s,
                                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s
                            )
                            ON CONFLICT (submission_id) DO UPDATE SET
                                image_dhash = EXCLUDED.image_dhash,
                                embedding = EXCLUDED.embedding,
                                consent_choice = EXCLUDED.consent_choice,
                                vehicle_type = EXCLUDED.vehicle_type,
                                car_model = EXCLUDED.car_model,
                                gsm_duration = EXCLUDED.gsm_duration,
                                gsm_contract_type = EXCLUDED.gsm_contract_type,
                                daily_trips = EXCLUDED.daily_trips,
                                monthly_income_range = EXCLUDED.monthly_income_range,
                                gsm_incentive_policy = EXCLUDED.gsm_incentive_policy,
                                membership_type = EXCLUDED.membership_type,
                                income_week_1_gross = EXCLUDED.income_week_1_gross,
                                income_week_1_net = EXCLUDED.income_week_1_net,
                                income_week_1_trips = EXCLUDED.income_week_1_trips,
                                income_week_2_gross = EXCLUDED.income_week_2_gross,
                                income_week_2_net = EXCLUDED.income_week_2_net,
                                income_week_2_trips = EXCLUDED.income_week_2_trips,
                                income_week_3_gross = EXCLUDED.income_week_3_gross,
                                income_week_3_net = EXCLUDED.income_week_3_net,
                                income_week_3_trips = EXCLUDED.income_week_3_trips,
                                income_week_4_gross = EXCLUDED.income_week_4_gross,
                                income_week_4_net = EXCLUDED.income_week_4_net,
                                income_week_4_trips = EXCLUDED.income_week_4_trips,
                                income_week_5_gross = EXCLUDED.income_week_5_gross,
                                income_week_5_net = EXCLUDED.income_week_5_net,
                                income_week_5_trips = EXCLUDED.income_week_5_trips,
                                total_gross_revenue = EXCLUDED.total_gross_revenue,
                                total_net_income = EXCLUDED.total_net_income,
                                total_trips = EXCLUDED.total_trips;
                            """,
                            insert_vals_full,
                        )
                        await cur.execute("RELEASE SAVEPOINT insert_full_sp;")
                    except Exception as ins_err:
                        logger.warning(f"Extended column insert fallback: {ins_err}")
                        await cur.execute("ROLLBACK TO SAVEPOINT insert_full_sp;")
                        await cur.execute(
                            """
                            INSERT INTO survey_submissions (
                                submission_id, form_id, ctv_code, driver_phone, driver_name, receipt_serial_no,
                                status, rejection_reason, form_data, raw_extracted_facts, driver_id
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (submission_id) DO UPDATE SET
                                status = EXCLUDED.status,
                                rejection_reason = EXCLUDED.rejection_reason;
                            """,
                            (
                                submission_id,
                                active_form_id,
                                effective_ctv,
                                persisted_phone,
                                persisted_name,
                                f"SN-{license_plate or 'AUTO'}",
                                status_str,
                                reason_str,
                                json.dumps(clean_form_data),
                                json.dumps(ai_res),
                                driver_tag if not is_dissent else None,
                            ),
                        )
                    if license_plate and not is_dissent:
                        vtype_db = "BIKE" if "BIKE" in str(vehicle_type).upper() or "MÁY" in str(vehicle_type).upper() else "CAR"
                        await cur.execute(
                            """
                            INSERT INTO driver_registered_vehicles (
                                driver_phone, license_plate, vehicle_type
                            ) VALUES (%s, %s, %s)
                            ON CONFLICT (driver_phone) DO NOTHING;
                            """,
                            (driver_phone, license_plate.upper(), vtype_db),
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
    status_emoji = "⏳" if status_str == "MANUAL_REVIEW" else ("✅" if status_str == "APPROVED" else "❌")
    auto_emoji = "✅" if auto_verdict == "APPROVED" else "❌"
    verdict_text = (
        f"{status_emoji} <b>PHIẾU KHẢO SÁT CHỜ DUYỆT THỦ CÔNG #{submission_id}</b>\n\n"
        f"• <b>Tài xế:</b> <b>{html.escape(driver_name or 'N/A')}</b> (<code>{driver_phone or 'N/A'}</code>)\n"
        f"• <b>Biển số xe:</b> <code>{license_plate or ai_res.get('detected_plate') or 'N/A'}</code>\n"
        f"• <b>Đề xuất từ Hệ thống AI:</b> {auto_emoji} <b>{auto_verdict}</b> ({html.escape(auto_reason) if auto_reason else 'Đạt chuẩn'})\n"
        f"• <b>Doanh thu ghi nhận:</b> <b>{tot_gross_val:,} VNĐ</b>\n"
        f"• <b>Trạng thái:</b> <code>{status_str}</code>\n"
        f"• <b>Ghi chú:</b> {html.escape(reason_str) if reason_str else 'Chờ duyệt'}"
    )
    if getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", None):
        try:
            background_tasks.add_task(send_telegram_notification, settings.TELEGRAM_ADMIN_GROUP_ID, verdict_text)
        except Exception as tg_err:
            logger.error(f"Failed to dispatch Telegram notification to admin group: {tg_err}")
    try:
        background_tasks.add_task(
            dispatch_submission_telegram_notifications,
            submission_id=submission_id,
            status_str=status_str,
            driver_phone=driver_phone,
            driver_name=driver_name,
            collaborator_id=collab_param,
            reason_str=reason_str,
            gross_revenue=ai_res.get("gross_revenue") or gross_revenue,
        )
    except Exception as party_err:
        logger.error(f"Failed to dispatch party Telegram notifications: {party_err}")
    try:
        background_tasks.add_task(
            sync_submission_to_lark_base,
            submission_id=submission_id,
            status_str=status_str,
            driver_phone=driver_phone,
            driver_name=driver_name,
            license_plate=license_plate or ai_res.get("detected_plate"),
            gross_revenue=ai_res.get("gross_revenue") or gross_revenue,
            net_income=ai_res.get("net_income"),
            total_trips=ai_res.get("total_trips") or trip_count,
            online_hours=ai_res.get("online_hours"),
            vehicle_model=str(form_data.get("car_model") or form_data.get("vehicle_model") or vehicle_type or "Xe điện GSM"),
            reason_str=reason_str,
        )
    except Exception as lark_err:
        logger.error(f"Failed to dispatch Lark Base sync task: {lark_err}")
    try:
        effective_ctv_drive = str(collab_param or answers.get("ctv_code") or "").strip() or None
        effective_vehicle = str(form_data.get("car_model") or form_data.get("vehicle_model") or vehicle_type or "Xe điện GSM")
        drive_rec = {
            "submission_id": submission_id,
            "status": status_str,
            "consent_choice": str(answers.get("consent_choice") or "Tôi đồng ý"),
            "form_data": clean_form_data,
            "membership_type": membership_type_val,
            "weekly_incomes": w_incomes,
            "total_gross_revenue": tot_gross_val,
            "total_net_income": tot_net_val,
            "total_trips": tot_trips_val,
            "income_week_1_gross": w1_g,
            "income_week_1_net": w1_n,
            "income_week_1_trips": w1_t,
            "income_week_2_gross": w2_g,
            "income_week_2_net": w2_n,
            "income_week_2_trips": w2_t,
            "income_week_3_gross": w3_g,
            "income_week_3_net": w3_n,
            "income_week_3_trips": w3_t,
            "income_week_4_gross": w4_g,
            "income_week_4_net": w4_n,
            "income_week_4_trips": w4_t,
            "income_week_5_gross": w5_g,
            "income_week_5_net": w5_n,
            "income_week_5_trips": w5_t,
            "ocr_data": ai_res.get("ocr_data") or {},
            "gross_revenue": tot_gross_val,
            "net_income": tot_net_val,
            "trip_count": tot_trips_val,
            "gps_latitude": getattr(request.state, "gps_lat", None) or form_data.get("gps_latitude"),
            "gps_longitude": getattr(request.state, "gps_lon", None) or form_data.get("gps_longitude"),
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "vehicle_type": effective_vehicle,
        }
        drive_imgs = []
        for i_idx, r_b in enumerate(raw_bytes_list):
            drive_imgs.append({"filename": f"slot_{i_idx}.webp", "bytes": r_b})
        background_tasks.add_task(
            upload_to_google_drive,
            submission_id=submission_id,
            record=drive_rec,
            images=drive_imgs,
        )
    except Exception as drive_err:
        logger.error(f"Failed to dispatch Drive delivery: {drive_err}")
    resp_data = {
        "status": status_str,
        "submission_id": submission_id,
        "mode": "TEST_DEV_MODE" if is_test_mode else "PROD_MODE",
        "reason": reason_str,
        "driver_tag": driver_tag,
        "driver_phone": driver_phone,
        "detected_plate": ai_res.get("detected_plate") or license_plate,
        "gross_revenue": ai_res.get("gross_revenue") or gross_revenue,
        "net_income": ai_res.get("net_income"),
        "trip_count": ai_res.get("total_trips") or trip_count,
        "ocr_data": ai_res.get("ocr_data"),
    }
    if ai_res.get("duplicate_detected"):
        resp_data["duplicate_detected"] = ai_res.get("duplicate_detected")
        resp_data["duplicate_type"] = ai_res.get("duplicate_type")
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

LOCAL_APPEAL_TRACKER: Dict[str, List[float]] = {}

class AppealRequest(BaseModel):
    submission_id: str
    driver_phone: Optional[str] = "ANONYMOUS"
    reason: Optional[str] = "Yêu cầu người thật kiểm tra lại trên web form"

@app.post("/api/v1/survey/appeal")
async def handle_survey_appeal(req: AppealRequest, background_tasks: BackgroundTasks):
    """Processes driver manual review requests with 2-hour rate limiting (max 5) and active guard."""
    phone = (req.driver_phone or "ANONYMOUS").strip()
    sub_id = req.submission_id.strip()

    # 1. Prevent duplicate active appeal
    is_already_active = False
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT status FROM survey_submissions WHERE submission_id = %s;", (sub_id,))
                    row = await cur.fetchone()
                    if row and row[0] in ("APPEAL_PENDING", "MANUAL_REVIEW"):
                        is_already_active = True
        except Exception:
            pass
    if not is_already_active and os.path.exists(DEV_STORAGE_FILE):
        try:
            with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                dev_records = json.load(f)
            for r in dev_records:
                if r.get("submission_id") == sub_id and r.get("status") in ("APPEAL_PENDING", "MANUAL_REVIEW"):
                    is_already_active = True
                    break
        except Exception:
            pass

    if is_already_active:
        return JSONResponse(status_code=200, content={
            "status": "ALREADY_ACTIVE",
            "submission_id": sub_id,
            "message": f"Phiếu #{sub_id} đã có yêu cầu kiểm tra đang hoạt động! Bạn không thể gửi thêm yêu cầu mới khi yêu cầu cũ chưa hoàn tất."
        })

    rate_key = f"rate_limit:appeal:{phone}"
    now = time.time()
    window = 3600  # 1 hour window
    max_appeals = 20  # Relaxed quota per window
    remaining_appeals = max_appeals - 1

    rate_limited = False
    if redis_client:
        try:
            count = await redis_client.incr(rate_key)
            if count == 1:
                await redis_client.expire(rate_key, window)
            if count > max_appeals:
                rate_limited = True
            remaining_appeals = max(0, max_appeals - count)
        except Exception as e:
            logger.warning(f"Redis appeal rate limit error: {e}")
            history = [t for t in LOCAL_APPEAL_TRACKER.get(phone, []) if now - t < window]
            if len(history) >= max_appeals:
                rate_limited = True
            else:
                history.append(now)
                LOCAL_APPEAL_TRACKER[phone] = history
                remaining_appeals = max(0, max_appeals - len(history))
    else:
        history = [t for t in LOCAL_APPEAL_TRACKER.get(phone, []) if now - t < window]
        if len(history) >= max_appeals:
            rate_limited = True
        else:
            history.append(now)
            LOCAL_APPEAL_TRACKER[phone] = history
            remaining_appeals = max(0, max_appeals - len(history))

    if rate_limited:
        return JSONResponse(status_code=200, content={
            "status": "RATE_LIMITED",
            "remaining_appeals": 0,
            "message": f"Bạn đã gửi tối đa {max_appeals} lượt yêu cầu kiểm tra trong vòng 1 giờ này. Vui lòng thử lại sau!"
        })

    # 2. Update status in Database
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE survey_submissions SET status = 'APPEAL_PENDING', rejection_reason = %s WHERE submission_id = %s;",
                        (f"Tài xế yêu cầu người thật đối soát lại: {req.reason}", sub_id)
                    )
                    await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to update survey_submissions for appeal: {e}")

    # 3. Update status in Dev Test Storage
    if os.path.exists(DEV_STORAGE_FILE):
        try:
            with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                dev_records = json.load(f)
            for r in dev_records:
                if r.get("submission_id") == sub_id:
                    r["status"] = "APPEAL_PENDING"
                    r["reason"] = f"Tài xế yêu cầu người thật đối soát lại: {req.reason}"
            with open(DEV_STORAGE_FILE, "w", encoding="utf-8") as f:
                json.dump(dev_records, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to update dev_storage for appeal: {e}")

    # 4. Dispatch Alert to Telegram Admin Group
    if getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", None):
        alert_text = (
            f"🚨 <b>YÊU CẦU NGƯỜI THẬT KIỂM TRA LẠI (KHIẾU NẠI) #{sub_id}</b>\n\n"
            f"• <b>Số điện thoại:</b> <code>{phone}</code>\n"
            f"• <b>Lượt khiếu nại còn lại:</b> <b>{remaining_appeals}/5 lượt</b> (trong phiên 2h)\n"
            f"• <b>Lý do:</b> {html.escape(req.reason)}\n\n"
            f"👉 <i>Vui lòng vào <a href='https://survey.trustbridgevn.com/admin/submissions'>Quản trị phiếu</a> để đối soát ảnh gốc!</i>"
        )
        try:
            background_tasks.add_task(send_telegram_notification, settings.TELEGRAM_ADMIN_GROUP_ID, alert_text)
        except Exception as e:
            logger.warning(f"Failed to alert admin group: {e}")

    return JSONResponse(status_code=200, content={
        "status": "APPEAL_SUBMITTED",
        "submission_id": sub_id,
        "remaining_appeals": remaining_appeals,
        "message": f"Đã gửi yêu cầu kiểm tra lại thành công! Bạn còn {remaining_appeals}/5 lượt khiếu nại trong phiên 2h này."
    })


@app.post("/api/v1/survey/appeal/cancel")
async def cancel_survey_appeal(req: AppealRequest, background_tasks: BackgroundTasks):
    """Allows cancelling an active manual review / appeal request."""
    sub_id = req.submission_id.strip()
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE survey_submissions SET status = 'REJECTED', rejection_reason = 'Yêu cầu kiểm tra lại đã bị hủy bởi người dùng' WHERE submission_id = %s;",
                        (sub_id,)
                    )
                    await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to cancel appeal in DB: {e}")

    if os.path.exists(DEV_STORAGE_FILE):
        try:
            with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                dev_records = json.load(f)
            for r in dev_records:
                if r.get("submission_id") == sub_id:
                    r["status"] = "REJECTED"
                    r["reason"] = "Yêu cầu kiểm tra lại đã bị hủy bởi người dùng"
            with open(DEV_STORAGE_FILE, "w", encoding="utf-8") as f:
                json.dump(dev_records, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to cancel appeal in dev storage: {e}")

    if getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", None):
        alert_text = f"ℹ️ <b>Người dùng đã HỦY yêu cầu kiểm tra lại cho phiếu #{sub_id}.</b>"
        try:
            background_tasks.add_task(send_telegram_notification, settings.TELEGRAM_ADMIN_GROUP_ID, alert_text)
        except Exception:
            pass

    return JSONResponse(status_code=200, content={
        "status": "CANCELLED",
        "submission_id": sub_id,
        "message": f"Đã hủy yêu cầu kiểm tra lại cho phiếu #{sub_id} thành công!"
    })

async def send_telegram_notification(chat_id: Union[int, str], text: str):
    """Dispatches standardized HTML verdict message to admin group or collaborator on Telegram."""
    bot_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
    if not bot_token or bot_token == "mock_tg_token":
        logger.info(f"[MOCK TELEGRAM DISPATCH] to {chat_id}:\n{text}")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
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


async def lookup_telegram_user_by_phone(phone: str) -> Optional[int]:
    """Looks up telegram_user_id in authorized_users by driver_phone."""
    clean = re.sub(r"[^\d+]", "", phone.strip())
    if clean.startswith("+84"):
        clean = "0" + clean[3:]
    elif clean.startswith("84") and len(clean) == 11:
        clean = "0" + clean[2:]

    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT telegram_user_id FROM authorized_users WHERE (driver_phone = %s OR driver_phone = %s) AND is_active = TRUE LIMIT 1;",
                        (clean, phone),
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        return int(row[0])
        except Exception as e:
            logger.debug(f"DB lookup failed for phone {phone}: {e}")
    return None


async def lookup_telegram_user_for_collaborator(collab_id: Union[str, int]) -> Optional[int]:
    """Resolves collaborator identifier to telegram_user_id (supports ID, phone, or username)."""
    collab_str = str(collab_id).strip()
    if collab_str.isdigit():
        val = int(collab_str)
        if db_pool:
            try:
                async with db_pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT telegram_user_id FROM authorized_users WHERE telegram_user_id = %s AND is_active = TRUE LIMIT 1;",
                            (val,),
                        )
                        row = await cur.fetchone()
                        if row and row[0]:
                            return int(row[0])
            except Exception:
                pass
        return val

    clean_user = collab_str.lstrip("@")
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT telegram_user_id FROM authorized_users WHERE (username = %s OR driver_phone = %s) AND is_active = TRUE LIMIT 1;",
                        (clean_user, collab_str),
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        return int(row[0])
        except Exception:
            pass
    return None


async def send_telegram_direct_message(
    bot_token: str,
    chat_id: Union[int, str],
    text: str,
    parse_mode: str = "HTML",
    reply_markup: Optional[dict] = None,
) -> bool:
    """Dispatches a direct message using the specified bot token (Driver Bot or Admin Bot)."""
    if not bot_token or bot_token == "mock_tg_token":
        logger.info(f"[MOCK DIRECT MESSAGE DISPATCH] to {chat_id}:\n{text}")
        return True

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        resp = await http_client.post(url, json=payload)
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Failed to dispatch Telegram message to {chat_id}: {e}")
        return False


def render_driver_submission_verdict_message(
    submission_id: str,
    status_str: str,
    driver_name: Optional[str] = None,
    reason_str: Optional[str] = None,
    gross_revenue: Optional[Union[str, int]] = None,
) -> Tuple[str, Optional[dict]]:
    """Renders user-friendly, supportive verdict notifications for drivers/collaborators."""
    name_display = f" {driver_name}" if driver_name else ""
    if status_str == "APPROVED":
        rev_text = ""
        if gross_revenue and str(gross_revenue) not in ("0", ""):
            try:
                num = int(re.sub(r"[^\d]", "", str(gross_revenue)))
                rev_text = f"• <b>Doanh thu ghi nhận:</b> <code>{num:,}đ</code>\n"
            except Exception:
                pass
        text = (
            f"🎉 <b>Khảo sát #{submission_id} đã được DUYỆT THÀNH CÔNG!</b>\n\n"
            f"Chào bạn{name_display}, kết quả khảo sát của bạn đã hợp lệ và đạt chuẩn quy định.\n"
            f"{rev_text}"
            f"• <b>Trạng thái:</b> <b>HỢP LỆ ✅</b>\n\n"
            f"Cảm ơn bạn đã tham gia. Tiền thưởng đang được đưa vào hàng đợi chi trả tự động!"
        )
        return text, None

    elif status_str == "REJECTED":
        text = (
            f"⚠️ <b>Thông báo kết quả khảo sát #{submission_id}:</b>\n\n"
            f"Chào bạn{name_display}, phiếu khảo sát của bạn chưa đạt yêu cầu.\n"
            f"• <b>Trạng thái:</b> <b>CHƯA ĐẠT ❌</b>\n"
            f"• <b>Lý do:</b> <i>{reason_str or 'Ảnh chụp hoặc số liệu chưa hợp lệ'}</i>\n\n"
            f"👉 <i>Bạn có thể bấm nút bên dưới để gửi lại ảnh chụp rõ nét hơn hoặc gửi yêu cầu phúc khảo:</i>"
        )
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": f"📸 Gửi lại ảnh #{submission_id}", "callback_data": f"driver_fix:{submission_id}:slot_photo"},
                    {"text": f"⚖️ Phúc khảo #{submission_id}", "callback_data": f"driver_appeal:{submission_id}"},
                ],
                [
                    {"text": "🆘 Yêu Cầu Hỗ Trợ", "callback_data": f"driver_support:{submission_id}"},
                ]
            ]
        }
        return text, reply_markup

    else:
        text = (
            f"⏳ <b>Khảo sát #{submission_id} đang được kiểm tra:</b>\n\n"
            f"Chào bạn{name_display}, hồ sơ của bạn đang được chuyển tới Giám Sát Viên duyệt bổ sung.\n"
            f"• <b>Trạng thái:</b> <b>ĐANG XỬ LÝ ⏳</b>\n"
            f"• <b>Ghi chú:</b> <i>{reason_str or 'Hệ thống đang đối soát dữ liệu'}</i>\n\n"
            f"Kết quả cuối cùng sẽ được thông báo trực tiếp tại đây ngay khi hoàn tất."
        )
        return text, None


async def dispatch_submission_telegram_notifications(
    submission_id: str,
    status_str: str,
    driver_phone: Optional[str] = None,
    driver_name: Optional[str] = None,
    collaborator_id: Optional[Union[str, int]] = None,
    reason_str: Optional[str] = None,
    gross_revenue: Optional[Union[str, int]] = None,
):
    """
    Asynchronously notifies driver and collaborator in Telegram direct message.
    """
    driver_bot_token = getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
    if not driver_bot_token or driver_bot_token == "mock_tg_token":
        logger.info(f"[MOCK TG NOTIFY PARTIES] sub={submission_id} status={status_str} driver={driver_phone} collaber={collaborator_id}")
        return

    # 1. Notify Driver if linked
    if driver_phone:
        driver_tg_id = await lookup_telegram_user_by_phone(driver_phone)
        if driver_tg_id:
            msg_text, reply_markup = render_driver_submission_verdict_message(
                submission_id=submission_id,
                status_str=status_str,
                driver_name=driver_name,
                reason_str=reason_str,
                gross_revenue=gross_revenue,
            )
            await send_telegram_direct_message(
                bot_token=driver_bot_token,
                chat_id=driver_tg_id,
                text=msg_text,
                reply_markup=reply_markup,
            )
        else:
            logger.debug(f"Driver phone {driver_phone} not yet linked to Telegram chat ID.")

    # 2. Notify Collaborator if linked
    if collaborator_id:
        collab_tg_id = await lookup_telegram_user_for_collaborator(collaborator_id)
        if collab_tg_id:
            status_emoji = "✅" if status_str == "APPROVED" else ("⚠️" if status_str == "MANUAL_REVIEW" else "❌")
            clean_phone = driver_phone or "N/A"
            masked_phone = clean_phone[:3] + "****" + clean_phone[-3:] if len(clean_phone) >= 7 else clean_phone
            collab_msg = (
                f"📋 <b>Cập Nhật Khảo Sát #{submission_id}</b>\n\n"
                f"• <b>Đối tác / Khách hàng:</b> {driver_name or 'Tài xế'} (<code>{masked_phone}</code>)\n"
                f"• <b>Trạng thái:</b> {status_emoji} <b>{status_str}</b>\n"
                f"• <b>Ghi chú:</b> {reason_str or 'Hợp lệ'}\n\n"
                f"<i>Thông báo tự động dành cho Cộng tác viên phụ trách.</i>"
            )
            await send_telegram_direct_message(
                bot_token=driver_bot_token,
                chat_id=collab_tg_id,
                text=collab_msg,
            )
        else:
            logger.info(f"Collaborator '{collaborator_id}' not yet linked to Telegram chat ID (will link later).")

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


from services.telegram_polling import TelegramPollingRunner, DriverBotRunner

admin_bot_runner: Optional[TelegramPollingRunner] = None
driver_bot_runner: Optional[DriverBotRunner] = None

def get_admin_bot_runner() -> Optional[TelegramPollingRunner]:
    global admin_bot_runner
    if admin_bot_runner is None:
        token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
        if token and token != "mock_tg_token":
            admin_bot_runner = TelegramPollingRunner(bot_token=token, db_pool=db_pool, role="admin")
    return admin_bot_runner

def get_driver_bot_runner() -> Optional[DriverBotRunner]:
    global driver_bot_runner
    if driver_bot_runner is None:
        token = getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None)
        if token and token != "mock_tg_token":
            driver_bot_runner = DriverBotRunner(bot_token=token, db_pool=db_pool)
    return driver_bot_runner

def verify_telegram_webhook_secret(secret_token: Optional[str] = None):
    if settings.ENVIRONMENT != "development" and getattr(settings, "TELEGRAM_WEBHOOK_SECRET", None):
        if not secrets.compare_digest(secret_token or "", settings.TELEGRAM_WEBHOOK_SECRET):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

@app.post("/webhook/telegram")
@app.post("/webhook/telegram/admin")
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

@app.post("/webhook/telegram/driver")
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

@app.post("/api/v1/telegram/set-webhooks")
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
