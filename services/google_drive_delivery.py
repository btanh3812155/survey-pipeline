import os
import io
import csv
import mimetypes
import glob
import json
import logging
import asyncio
import shutil
import tempfile
from typing import Optional, Union, Dict, Any, List, Tuple
from datetime import datetime, timezone
import httpx
from config import settings
from services.storage_r2 import r2_client

logger = logging.getLogger("google_drive_delivery")

def _guess_mime(filename: str) -> str:
    """Correct MIME for Drive uploads; .jpg/.webp must not go up as octet-stream."""
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _open_image(raw_bytes: bytes):
    """Opens image bytes with Pillow, registering HEIF support when available."""
    from PIL import Image

    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:
        pass
    return Image.open(io.BytesIO(raw_bytes))


def detect_image_mime(raw_bytes: bytes) -> Optional[str]:
    """MIME from the actual image bytes, independent of the filename extension."""
    if not raw_bytes:
        return None
    try:
        with _open_image(raw_bytes) as image:
            fmt = (image.format or "").upper()
    except Exception:
        return None
    return {
        "JPEG": "image/jpeg",
        "JPG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
        "HEIF": "image/heic",
        "HEIC": "image/heic",
    }.get(fmt)


def normalize_image_bytes(raw_bytes: bytes, quality: int = 90) -> Optional[bytes]:
    """
    Returns WebP bytes for any decodable image, or None when the bytes are not an image.

    Already-WebP bytes are returned unchanged; JPEG/PNG/HEIC are re-encoded to WebP
    so the mirror filename, MIME and payload always agree.
    """
    if not raw_bytes:
        return None
    try:
        with _open_image(raw_bytes) as image:
            if (image.format or "").upper() == "WEBP":
                return raw_bytes
            has_alpha = "A" in image.getbands() or "transparency" in image.info
            converted = image.convert("RGBA" if has_alpha else "RGB")
            out = io.BytesIO()
            converted.save(out, format="WEBP", quality=quality, method=4)
            return out.getvalue()
    except Exception as e:
        logger.warning(f"Could not normalize image bytes ({len(raw_bytes)} bytes): {e}")
        return None


def normalized_image_name(filename: str) -> str:
    """Stem of the supplied filename with a .webp extension."""
    stem = os.path.splitext(os.path.basename(filename or "image"))[0] or "image"
    return f"{stem}.webp"


async def _list_remote_files(client, token: str, folder_id: str) -> Dict[str, str]:
    """Map of non-trashed file name -> id inside a Drive folder."""
    try:
        resp = await client.get(
            "https://www.googleapis.com/drive/v3/files",
            params={"q": f"'{folder_id}' in parents and trashed = false", "fields": "files(id,name)", "pageSize": 1000},
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code != 200:
            return {}
        return {
            f["name"]: f["id"]
            for f in (resp.json().get("files") or [])
            if f.get("name") and f.get("id")
        }
    except Exception as e:
        logger.warning(f"Could not list Drive folder {folder_id}: {e}")
        return {}




async def _list_remote_file_names(client, token: str, folder_id: str) -> set:
    """Names of non-trashed files already inside a Drive folder."""
    try:
        resp = await client.get(
            "https://www.googleapis.com/drive/v3/files",
            params={"q": f"'{folder_id}' in parents and trashed = false", "fields": "files(name)", "pageSize": 1000},
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code != 200:
            return set()
        return {f.get("name") for f in (resp.json().get("files") or []) if f.get("name")}
    except Exception as e:
        logger.warning(f"Could not list Drive folder {folder_id}: {e}")
        return set()
# Base directory for local deliverables mirror
LOCAL_DELIVERABLES_DIR = os.path.join(os.getcwd(), "data", "google_drive_deliverables")
LOCAL_IMAGES_DIR = os.path.join(LOCAL_DELIVERABLES_DIR, "Images")
LOCAL_SHEETS_DIR = os.path.join(LOCAL_DELIVERABLES_DIR, "Sheets")
LOCAL_FORMS_SHEETS_DIR = os.path.join(LOCAL_SHEETS_DIR, "forms")

# Raw per-submission photo store written at submission time (source for mirror backfill).
SUBMISSION_PHOTOS_DIR = os.environ.get("SUBMISSION_PHOTOS_DIR") or os.path.join(
    os.getcwd(), "data", "submission_photos"
)
DEV_SUBMISSIONS_FILE = os.path.join(os.getcwd(), "data", "dev_test_submissions.json")

os.makedirs(LOCAL_IMAGES_DIR, exist_ok=True)
os.makedirs(LOCAL_SHEETS_DIR, exist_ok=True)
os.makedirs(LOCAL_FORMS_SHEETS_DIR, exist_ok=True)

MASTER_CSV_FILE = os.path.join(LOCAL_SHEETS_DIR, "Master_Survey_Deliverables.csv")

IMAGE_FILE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}


def local_form_csv_path(form_id: str) -> str:
    """Per-form CSV mirror path: Sheets/forms/<form_id>.csv."""
    return os.path.join(LOCAL_FORMS_SHEETS_DIR, f"{form_id}.csv")

CSV_HEADERS = [
    # 1. Metadata
    "Thời Gian Nộp",
    # 2. Câu hỏi biểu mẫu (Form Questions)
    "Loại Phương Tiện",
    "Dòng Xe",
    "Thời Gian Chạy Cho GSM",
    "Hình Thức Hợp Tác",
    "Số Chuyến Hoàn Thành Mỗi Ngày",
    "Thu Nhập Bình Quân Tháng",
    "Chính Sách Thưởng Hiệu Suất",
    # 3. Hồ sơ ứng dụng (Slot 0 Profile)
    "Hạng Hội Viên",
    # 4. Xác thực thu nhập 5 tuần Tháng 10 (Slots 2-6)
    "Doanh Thu Tuần 1 (28/09 - 04/10)",
    "Thực Nhận Tuần 1",
    "Số Chuyến Tuần 1",
    "Doanh Thu Tuần 2 (05/10 - 11/10)",
    "Thực Nhận Tuần 2",
    "Số Chuyến Tuần 2",
    "Doanh Thu Tuần 3 (12/10 - 18/10)",
    "Thực Nhận Tuần 3",
    "Số Chuyến Tuần 3",
    "Doanh Thu Tuần 4 (19/10 - 25/10)",
    "Thực Nhận Tuần 4",
    "Số Chuyến Tuần 4",
    "Doanh Thu Tuần 5 (26/10 - 01/11)",
    "Thực Nhận Tuần 5",
    "Số Chuyến Tuần 5",
    # 5. Tổng hợp thu nhập 5 tuần
    "Tổng Doanh Thu 5 Tuần",
    "Tổng Thực Nhận 5 Tuần",
    "Tổng Số Chuyến 5 Tuần",
    # 6. Tọa độ định vị
    "Tọa Độ Vĩ Độ",
    "Tọa Độ Kinh Độ",
    # 7. Mã phiếu (stable dedupe key, always last)
    "Mã Phiếu",
]
def is_test_submission(submission_id: Any) -> bool:
    """
    Checks if submission ID indicates a test/dev/mock/sample/demo submission.
    Filters out IDs containing TEST, DEV, MOCK, SAMPLE, or DEMO (case-insensitive).
    """
    if not submission_id:
        return True
    sub_upper = str(submission_id).upper()
    return any(tag in sub_upper for tag in ("TEST", "DEV", "MOCK", "SAMPLE", "DEMO"))


_TEST_FOLDER_PREFIXES = ("DEV-", "TEST-", "TEST_", "MOCK-", "SAMPLE-", "DEMO-")
_TEST_FOLDER_SEGMENTS = (
    "_TEST_", "_DEV_", "_MOCK_", "_SAMPLE_", "_DEMO_",
    "-TEST-", "-DEV-", "-MOCK-", "-SAMPLE-", "-DEMO-",
)


def is_explicit_test_folder(name: Any) -> bool:
    """
    True only for unambiguous test folders (e.g. DEV-foo, TEST-old, MOCK-x,
    VN-DRIVE-TEST-01). Real form ids such as TB_GSM_2026_Nov_Test are not matched.
    """
    if not name:
        return False
    upper = str(name).upper()
    if upper.startswith(_TEST_FOLDER_PREFIXES):
        return True
    return any(segment in upper for segment in _TEST_FOLDER_SEGMENTS)

def extract_survey_row_data(record: Dict[str, Any]) -> List[Any]:
    """
    Extracts deliverable survey columns following the form fields:
    - Form question answers (Vehicle, Duration, Model, Contract, Trips, Policy, etc.)
    - Profile membership type (Slot 0)
    - 5-week income statistics (Slots 2-6) & 5-week totals
    - Excludes PII and internal identifiers (phone, name, signature, CTV ID, submission ID)
    """
    form_data = record.get("form_data") or {}
    if isinstance(form_data, str):
        try:
            form_data = json.loads(form_data)
        except Exception:
            form_data = {}

    ocr_data = record.get("ocr_data") or {}
    if not ocr_data and isinstance(record.get("ai_result"), dict):
        ocr_data = record.get("ai_result").get("ocr_data") or {}
    if isinstance(ocr_data, str):
        try:
            ocr_data = json.loads(ocr_data)
        except Exception:
            ocr_data = {}

    ai_res = record.get("ai_result") or {}
    if isinstance(ai_res, str):
        try:
            ai_res = json.loads(ai_res)
        except Exception:
            ai_res = {}

    weekly_incomes = record.get("weekly_incomes") or ai_res.get("weekly_incomes") or {}

    # Older DB rows have zeroed income_week_* columns; their real values live in
    # raw_extracted_facts (written even when the extended INSERT was incomplete).
    raw_facts = record.get("raw_extracted_facts") or {}
    if isinstance(raw_facts, str):
        try:
            raw_facts = json.loads(raw_facts)
        except Exception:
            raw_facts = {}
    if not isinstance(raw_facts, dict):
        raw_facts = {}
    if not weekly_incomes:
        weekly_incomes = raw_facts.get("weekly_incomes") or {}

    # 1. Thời Gian Nộp
    thoi_gian_nop = record.get("submitted_at") or record.get("created_at") or datetime.now(timezone.utc).isoformat()

    # 2. Loại Phương Tiện
    loai_pt = form_data.get("vehicle_type") or record.get("vehicle_type") or "Xe điện GSM"

    # 3. Dòng Xe
    dong_xe = form_data.get("car_model") or record.get("car_model") or record.get("vehicle_model") or form_data.get("vehicle_model") or ""

    # 4. Thời Gian Chạy Cho GSM
    thoi_gian_gsm = form_data.get("gsm_duration") or record.get("gsm_duration") or ""

    # 5. Hình Thức Hợp Tác
    hinh_thuc = form_data.get("gsm_contract_type") or record.get("gsm_contract_type") or ""

    # 6. Số Chuyến Hoàn Thành Mỗi Ngày
    chuyen_ngay = form_data.get("daily_trips") or record.get("daily_trips") or ""

    # 7. Thu Nhập Bình Quân Tháng
    thu_nhap_thang = form_data.get("monthly_income_range") or record.get("monthly_income_range") or ""

    # 8. Chính Sách Thưởng Hiệu Suất
    thuong_hs = form_data.get("gsm_incentive_policy") or record.get("gsm_incentive_policy") or ""
    if isinstance(thuong_hs, list):
        thuong_hs = "; ".join(str(x) for x in thuong_hs)

    # 9. Hạng Hội Viên (Slot 0 Profile)
    hang_hoi_vien = (
        record.get("membership_type")
        or ai_res.get("membership_type")
        or ocr_data.get("membership_type")
        or form_data.get("membership_type")
        or "Tiêu chuẩn"
    )

    # 10. 5 Tuần Thu Nhập Tháng 10
    w1 = weekly_incomes.get("week_1") or {}
    w2 = weekly_incomes.get("week_2") or {}
    w3 = weekly_incomes.get("week_3") or {}
    w4 = weekly_incomes.get("week_4") or {}
    w5 = weekly_incomes.get("week_5") or {}

    legacy_gross = record.get("gross_revenue") or ocr_data.get("gross_revenue") or 0
    legacy_net = record.get("net_income") or ocr_data.get("net_income") or 0
    legacy_trips = record.get("trip_count") or ocr_data.get("total_trips") or 0

    w1_gross = record.get("income_week_1_gross") or w1.get("gross_revenue") or legacy_gross
    w1_net = record.get("income_week_1_net") or w1.get("net_income") or legacy_net
    w1_trips = record.get("income_week_1_trips") or w1.get("total_trips") or legacy_trips

    w2_gross = record.get("income_week_2_gross") or w2.get("gross_revenue") or 0
    w2_net = record.get("income_week_2_net") or w2.get("net_income") or 0
    w2_trips = record.get("income_week_2_trips") or w2.get("total_trips") or 0

    w3_gross = record.get("income_week_3_gross") or w3.get("gross_revenue") or 0
    w3_net = record.get("income_week_3_net") or w3.get("net_income") or 0
    w3_trips = record.get("income_week_3_trips") or w3.get("total_trips") or 0

    w4_gross = record.get("income_week_4_gross") or w4.get("gross_revenue") or 0
    w4_net = record.get("income_week_4_net") or w4.get("net_income") or 0
    w4_trips = record.get("income_week_4_trips") or w4.get("total_trips") or 0

    w5_gross = record.get("income_week_5_gross") or w5.get("gross_revenue") or 0
    w5_net = record.get("income_week_5_net") or w5.get("net_income") or 0
    w5_trips = record.get("income_week_5_trips") or w5.get("total_trips") or 0

    # 11. Tổng Hợp 5 Tuần
    computed_tot_gross = w1_gross + w2_gross + w3_gross + w4_gross + w5_gross
    computed_tot_net = w1_net + w2_net + w3_net + w4_net + w5_net
    computed_tot_trips = w1_trips + w2_trips + w3_trips + w4_trips + w5_trips

    raw_tot_gross = raw_facts.get("total_gross_revenue") or 0
    raw_tot_net = raw_facts.get("total_net_income") or 0
    raw_tot_trips = raw_facts.get("total_trips") or 0

    tot_gross = record.get("total_gross_revenue") or (computed_tot_gross if computed_tot_gross > 0 else (raw_tot_gross or legacy_gross))
    tot_net = record.get("total_net_income") or (computed_tot_net if computed_tot_net > 0 else (raw_tot_net or legacy_net))
    tot_trips = record.get("total_trips") or (computed_tot_trips if computed_tot_trips > 0 else (raw_tot_trips or legacy_trips))

    # 12. Tọa độ định vị & Trạng thái
    vi_do = record.get("gps_latitude")
    if vi_do is None:
        vi_do = form_data.get("gps_latitude", "")

    kinh_do = record.get("gps_longitude")
    if kinh_do is None:
        kinh_do = form_data.get("gps_longitude", "")

    return [
        thoi_gian_nop,
        loai_pt,
        dong_xe,
        thoi_gian_gsm,
        hinh_thuc,
        chuyen_ngay,
        thu_nhap_thang,
        thuong_hs,
        hang_hoi_vien,
        w1_gross,
        w1_net,
        w1_trips,
        w2_gross,
        w2_net,
        w2_trips,
        w3_gross,
        w3_net,
        w3_trips,
        w4_gross,
        w4_net,
        w4_trips,
        w5_gross,
        w5_net,
        w5_trips,
        tot_gross,
        tot_net,
        tot_trips,
        vi_do,
        kinh_do,
        record.get("submission_id") or "",
    ]
# Google Auth configurations
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or getattr(settings, "GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_DRIVE_ROOT_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_ROOT_FOLDER_ID") or getattr(settings, "GOOGLE_DRIVE_ROOT_FOLDER_ID", "")
GOOGLE_ADMIN_EMAIL = os.environ.get("GOOGLE_ADMIN_EMAIL") or getattr(settings, "GOOGLE_ADMIN_EMAIL", "admin@trustbridgevn.com")
GOOGLE_DRIVE_REFRESH_TOKEN = os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN") or getattr(settings, "GOOGLE_DRIVE_REFRESH_TOKEN", "")
GOOGLE_DRIVE_CLIENT_ID = os.environ.get("GOOGLE_DRIVE_CLIENT_ID") or getattr(settings, "GOOGLE_DRIVE_CLIENT_ID", "")
GOOGLE_DRIVE_CLIENT_SECRET = os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET") or getattr(settings, "GOOGLE_DRIVE_CLIENT_SECRET", "")
GOOGLE_DRIVE_ACCESS_TOKEN = os.environ.get("GOOGLE_DRIVE_ACCESS_TOKEN") or getattr(settings, "GOOGLE_DRIVE_ACCESS_TOKEN", "")

# Postgres advisory lock key so periodic and manual syncs never overlap.
DRIVE_SYNC_LOCK_KEY = 764539267


def get_google_service_account_info() -> Optional[Dict[str, Any]]:
    """
    Retrieves parsed service account JSON dictionary from environment or local filesystem.
    Supports:
    1. File path in GOOGLE_SERVICE_ACCOUNT_JSON
    2. Raw JSON string in GOOGLE_SERVICE_ACCOUNT_JSON
    3. service_account.json in current directory or /survey-pipeline/
    """
    raw = GOOGLE_SERVICE_ACCOUNT_JSON.strip() if GOOGLE_SERVICE_ACCOUNT_JSON else ""
    if raw:
        if raw.startswith("{") and raw.endswith("}"):
            try:
                return json.loads(raw)
            except Exception as e:
                logger.error(f"Failed to parse GOOGLE_SERVICE_ACCOUNT_JSON string: {e}")
        elif os.path.isfile(raw):
            try:
                with open(raw, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to read service account JSON at {raw}: {e}")

    candidate_files = [
        os.path.join(os.getcwd(), "service_account.json"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service_account.json"),
        "/survey-pipeline/service_account.json",
    ]
    for c in candidate_files:
        if os.path.isfile(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to read service account JSON at {c}: {e}")
    return None

def get_client_secrets_from_file() -> Tuple[Optional[str], Optional[str]]:
    """Inspects candidate client_secret.json or credentials.json files."""
    candidate_files = [
        os.path.join(os.getcwd(), "client_secret.json"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client_secret.json"),
        os.path.join(os.getcwd(), "credentials.json"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "credentials.json"),
        "/survey-pipeline/client_secret.json",
    ]
    for c in candidate_files:
        if os.path.isfile(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    info = data.get("installed") or data.get("web") or data
                    cid = info.get("client_id")
                    csec = info.get("client_secret")
                    if cid and csec:
                        return cid, csec
            except Exception as e:
                logger.debug(f"Failed to read client secrets from {c}: {e}")
    return None, None


def get_gcloud_oauth_credentials_from_db(account: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Reads OAuth2 tokens from local gcloud credentials.db for admin account if available."""
    db_path = os.path.expanduser("~/.config/gcloud/credentials.db")
    if not os.path.isfile(db_path):
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        target = account or GOOGLE_ADMIN_EMAIL or "admin@trustbridgevn.com"
        cur.execute("SELECT account_id, value FROM credentials WHERE account_id = ?", (target,))
        row = cur.fetchone()
        if row:
            val = row[1]
            if isinstance(val, bytes):
                val = val.decode("utf-8", errors="ignore")
            data = json.loads(val)
            if data.get("refresh_token"):
                return {
                    "account": row[0],
                    "refresh_token": data.get("refresh_token"),
                    "client_id": data.get("client_id"),
                    "client_secret": data.get("client_secret"),
                }
    except Exception as e:
        logger.debug(f"Failed reading credentials from gcloud db: {e}")
    return None


def get_google_oauth_credentials() -> Optional[Dict[str, Any]]:
    """
    Retrieves OAuth2 client credentials and refresh token from:
    1. Environment variables (GOOGLE_DRIVE_REFRESH_TOKEN, GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET)
    2. token.json in project root or current directory
    3. Local gcloud credentials.db (if authenticated as admin account)
    """
    refresh_token = (os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN") or getattr(settings, "GOOGLE_DRIVE_REFRESH_TOKEN", "") or "").strip()
    client_id = (os.environ.get("GOOGLE_DRIVE_CLIENT_ID") or getattr(settings, "GOOGLE_DRIVE_CLIENT_ID", "") or "").strip()
    client_secret = (os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET") or getattr(settings, "GOOGLE_DRIVE_CLIENT_SECRET", "") or "").strip()
    access_token = (os.environ.get("GOOGLE_DRIVE_ACCESS_TOKEN") or getattr(settings, "GOOGLE_DRIVE_ACCESS_TOKEN", "") or "").strip()

    file_cid, file_csec = get_client_secrets_from_file()
    client_id = client_id or file_cid
    client_secret = client_secret or file_csec

    if refresh_token and client_id and client_secret:
        return {
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "access_token": access_token or None,
        }

    # Candidate token.json files (prioritize current directory, then project root)
    seen_paths = set()
    candidate_tokens = []
    for candidate in [
        os.path.join(os.getcwd(), "token.json"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "token.json"),
        "/survey-pipeline/token.json",
    ]:
        real = os.path.realpath(candidate)
        if real not in seen_paths:
            seen_paths.add(real)
            candidate_tokens.append(candidate)
    for p in candidate_tokens:
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    tok_refresh = data.get("refresh_token") or refresh_token
                    tok_cid = data.get("client_id") or client_id
                    tok_csec = data.get("client_secret") or client_secret
                    tok_acc = data.get("access_token") or data.get("token") or access_token
                    if tok_refresh and tok_cid and tok_csec:
                        return {
                            "refresh_token": tok_refresh,
                            "client_id": tok_cid,
                            "client_secret": tok_csec,
                            "access_token": tok_acc or None,
                        }
                    elif tok_refresh:
                        return {
                            "refresh_token": tok_refresh,
                            "client_id": tok_cid,
                            "client_secret": tok_csec,
                            "access_token": tok_acc or None,
                        }
                    elif tok_acc:
                        return {
                            "access_token": tok_acc,
                            "client_id": tok_cid,
                            "client_secret": tok_csec,
                        }
            except Exception as e:
                logger.error(f"Failed to read token.json at {p}: {e}")

    # Check gcloud credentials.db for admin email
    gcloud_db_creds = get_gcloud_oauth_credentials_from_db(GOOGLE_ADMIN_EMAIL)
    if gcloud_db_creds:
        return gcloud_db_creds

    if access_token:
        return {"access_token": access_token}

    return None


async def refresh_google_oauth_token(
    refresh_token: str,
    client_id: str,
    client_secret: str,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[str]:
    """Exchanges OAuth2 refresh token for a fresh Google Drive access token using httpx."""
    token_uri = "https://oauth2.googleapis.com/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=15.0)
        close_client = True
    try:
        resp = await client.post(token_uri, data=data)
        resp.raise_for_status()
        res_json = resp.json()
        token = res_json.get("access_token")
        if token:
            logger.info("Successfully refreshed Google Drive OAuth2 access token.")
        return token
    except Exception as e:
        logger.error(f"Failed to refresh Google OAuth2 access token: {e}")
        return None
    finally:
        if close_client:
            await client.aclose()


async def get_gcloud_access_token() -> Optional[str]:
    """Retrieves access token from local gcloud CLI if authenticated."""
    candidates = [
        shutil.which("gcloud"),
        os.path.expanduser("~/Downloads/Misc/google-cloud-sdk/bin/gcloud"),
        "/usr/local/bin/gcloud",
        "/usr/bin/gcloud",
    ]
    gcloud_bin = next((c for c in candidates if c and os.path.isfile(c) and os.access(c, os.X_OK)), None)
    if not gcloud_bin:
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            gcloud_bin,
            "auth",
            "print-access-token",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        if proc.returncode == 0:
            token = stdout.decode().strip()
            if token and not token.startswith("ERROR") and not token.startswith("WARNING"):
                logger.info("Retrieved Google Drive access token from gcloud CLI.")
                return token
        else:
            logger.debug(f"gcloud print-access-token failed: {stderr.decode()[:120]}")
    except Exception as e:
        logger.debug(f"gcloud print-access-token check timed out or failed: {e}")
    return None


def create_signed_jwt(sa_info: Dict[str, Any], scopes: List[str]) -> str:
    """Creates a self-signed JWT using cryptography library for Google OAuth2."""
    import time
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    now = int(time.time())
    payload = {
        "iss": sa_info["client_email"],
        "scope": " ".join(scopes),
        "aud": sa_info.get("token_uri", "https://oauth2.googleapis.com/token"),
        "exp": now + 3600,
        "iat": now,
    }
    header = {"alg": "RS256", "typ": "JWT"}

    header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=")
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    signing_input = header_b64 + b"." + payload_b64

    pk_bytes = sa_info["private_key"].encode("utf-8")
    private_key = load_pem_private_key(pk_bytes, password=None)
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=")
    return (signing_input + b"." + sig_b64).decode("utf-8")


async def get_google_access_token(sa_info: Dict[str, Any], client: Optional[httpx.AsyncClient] = None) -> Optional[str]:
    """Exchanges signed JWT for Google OAuth2 access token."""
    token_uri = sa_info.get("token_uri", "https://oauth2.googleapis.com/token")
    jwt_token = create_signed_jwt(
        sa_info,
        [
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    )
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt_token,
    }
    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=15.0)
        close_client = True

    try:
        resp = await client.post(token_uri, data=data)
        resp.raise_for_status()
        token_json = resp.json()
        return token_json.get("access_token")
    except Exception as e:
        logger.error(f"Failed to exchange service account JWT for access token: {e}")
        return None
    finally:
        if close_client:
            await client.aclose()


class GoogleDriveClient:
    """Google Drive v3 REST API client supporting OAuth2, Service Account, and gcloud CLI."""

    def __init__(
        self,
        sa_info: Optional[Dict[str, Any]] = None,
        root_folder_id: Optional[str] = None,
        oauth_creds: Optional[Dict[str, Any]] = None,
    ):
        self.sa_info = sa_info
        self.root_folder_id = root_folder_id or GOOGLE_DRIVE_ROOT_FOLDER_ID or ""
        self.oauth_creds = oauth_creds
        self._cached_access_token: Optional[str] = None

    async def get_token(self, client: Optional[httpx.AsyncClient] = None) -> Optional[str]:
        """
        Retrieves valid Google Drive access token checking:
        1. OAuth2 user credentials (refresh_token or token.json)
        2. Service Account JSON JWT
        3. Local gcloud CLI print-access-token fallback
        """
        close_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=15.0)
            close_client = True

        try:
            # 1. OAuth2 tokens first
            oauth = self.oauth_creds if self.oauth_creds is not None else get_google_oauth_credentials()
            if oauth:
                r_token = oauth.get("refresh_token")
                c_id = oauth.get("client_id")
                c_sec = oauth.get("client_secret")
                if r_token and c_id and c_sec:
                    token = await refresh_google_oauth_token(r_token, c_id, c_sec, client)
                    if token:
                        self._cached_access_token = token
                        return token

                direct_token = oauth.get("access_token") or oauth.get("token")
                if direct_token:
                    self._cached_access_token = direct_token
                    return direct_token

            # 2. Service account JSON
            sa = self.sa_info if self.sa_info is not None else get_google_service_account_info()
            if sa:
                token = await get_google_access_token(sa, client)
                if token:
                    self._cached_access_token = token
                    return token

            # 3. gcloud CLI fallback
            gcloud_tok = await get_gcloud_access_token()
            if gcloud_tok:
                self._cached_access_token = gcloud_tok
                return gcloud_tok

            logger.warning("No valid Google Drive credentials available (OAuth2, Service Account, or gcloud CLI).")
            return None
        finally:
            if close_client:
                await client.aclose()
    async def create_folder(
        self, client: httpx.AsyncClient, token: str, folder_name: str, parent_id: Optional[str] = None
    ) -> Optional[str]:
        """Creates a folder in Google Drive and returns its ID."""
        url = "https://www.googleapis.com/drive/v3/files"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        meta: Dict[str, Any] = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            meta["parents"] = [parent_id]

        try:
            resp = await client.post(url, json=meta, headers=headers)
            resp.raise_for_status()
            folder_data = resp.json()
            folder_id = folder_data.get("id")
            logger.info(f"📁 [GOOGLE DRIVE] Created remote folder '{folder_name}' (ID: {folder_id})")
            return folder_id
        except Exception as e:
            logger.error(f"Failed to create Google Drive folder '{folder_name}': {e}")
            return None

    async def find_file_or_folder(
        self,
        client: httpx.AsyncClient,
        token: str,
        name: str,
        parent_id: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> Optional[str]:
        """Finds an existing file or folder in Google Drive by name and parent ID."""
        url = "https://www.googleapis.com/drive/v3/files"
        headers = {"Authorization": f"Bearer {token}"}
        query_parts = [f"name = '{name}'", "trashed = false"]
        if parent_id:
            query_parts.append(f"'{parent_id}' in parents")
        if mime_type:
            query_parts.append(f"mimeType = '{mime_type}'")
        params = {
            "q": " and ".join(query_parts),
            "fields": "files(id, name, mimeType)",
            "spaces": "drive",
        }
        try:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            files = resp.json().get("files", [])
            if files:
                return files[0]["id"]
            return None
        except Exception as e:
            logger.debug(f"Failed to find '{name}' in Google Drive: {e}")
            return None

    async def get_or_create_folder(
        self, client: httpx.AsyncClient, token: str, folder_name: str, parent_id: Optional[str] = None
    ) -> Optional[str]:
        """Gets existing folder ID or creates a new one."""
        existing_id = await self.find_file_or_folder(
            client, token, folder_name, parent_id, "application/vnd.google-apps.folder"
        )
        if existing_id:
            return existing_id
        return await self.create_folder(client, token, folder_name, parent_id)

    async def upload_file(
        self,
        client: httpx.AsyncClient,
        token: str,
        filename: str,
        content_bytes: bytes,
        mime_type: str = "application/octet-stream",
        parent_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Uploads or updates a file in Google Drive.
        If a file with `filename` already exists in `parent_id`, performs a PATCH request
        to update its content in place without creating duplicate files.
        If it does not exist, performs a POST multipart upload to create it.
        """
        headers = {
            "Authorization": f"Bearer {token}",
        }

        # Check if file already exists in parent_id
        existing_id = await self.find_file_or_folder(client, token, filename, parent_id)
        if existing_id:
            patch_url = f"https://www.googleapis.com/upload/drive/v3/files/{existing_id}?uploadType=media"
            patch_headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": mime_type,
            }
            try:
                resp = await client.patch(patch_url, content=content_bytes, headers=patch_headers)
                resp.raise_for_status()
                logger.info(f"🔄 [GOOGLE DRIVE] Updated '{filename}' in place (ID: {existing_id}, size: {len(content_bytes)} bytes)")
                return existing_id
            except Exception as e:
                logger.error(f"Failed to update '{filename}' (ID: {existing_id}) on Google Drive: {e}")
                return None

        # File does not exist -> create via multipart POST
        url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
        metadata: Dict[str, Any] = {"name": filename}
        if parent_id:
            metadata["parents"] = [parent_id]

        files = {
            "data": ("metadata", json.dumps(metadata), "application/json; charset=UTF-8"),
            "file": (filename, content_bytes, mime_type),
        }

        try:
            resp = await client.post(url, files=files, headers=headers)
            resp.raise_for_status()
            file_data = resp.json()
            file_id = file_data.get("id")
            logger.info(f"✅ [GOOGLE DRIVE] Uploaded '{filename}' (ID: {file_id}, size: {len(content_bytes)} bytes)")
            return file_id
        except Exception as e:
            logger.error(f"Failed to upload '{filename}' to Google Drive: {e}")
            return None

    async def import_csv_as_sheet(
        self,
        client: httpx.AsyncClient,
        token: str,
        sheet_name: str,
        csv_bytes: bytes,
        parent_id: Optional[str] = None,
    ) -> Optional[str]:
        """Creates a native Google Sheet from CSV bytes via Drive import conversion."""
        url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
        metadata: Dict[str, Any] = {
            "name": sheet_name,
            "mimeType": "application/vnd.google-apps.spreadsheet",
        }
        if parent_id:
            metadata["parents"] = [parent_id]
        files = {
            "data": ("metadata", json.dumps(metadata), "application/json; charset=UTF-8"),
            "file": (f"{sheet_name}.csv", csv_bytes, "text/csv; charset=UTF-8"),
        }
        try:
            resp = await client.post(url, files=files, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            sheet_id = resp.json().get("id")
            logger.info(f"📊 [GOOGLE DRIVE] Created Google Sheet '{sheet_name}' (ID: {sheet_id})")
            return sheet_id
        except Exception as e:
            logger.error(f"Failed to create Google Sheet '{sheet_name}': {e}")
            return None

    async def create_sheet(
        self,
        client: httpx.AsyncClient,
        token: str,
        sheet_name: str,
        parent_id: Optional[str] = None,
    ) -> Optional[str]:
        """Creates an empty native Google Sheet (Drive files.create with spreadsheet mime)."""
        metadata: Dict[str, Any] = {
            "name": sheet_name,
            "mimeType": "application/vnd.google-apps.spreadsheet",
        }
        if parent_id:
            metadata["parents"] = [parent_id]
        try:
            resp = await client.post(
                "https://www.googleapis.com/drive/v3/files",
                headers={"Authorization": f"Bearer {token}"},
                json=metadata,
            )
            resp.raise_for_status()
            sheet_id = resp.json().get("id")
            logger.info(f"📊 [GOOGLE DRIVE] Created empty Google Sheet '{sheet_name}' (ID: {sheet_id})")
            return sheet_id
        except Exception as e:
            logger.error(f"Failed to create empty Google Sheet '{sheet_name}': {e}")
            return None

    async def get_spreadsheet_sheet_id(
        self, client: httpx.AsyncClient, token: str, spreadsheet_id: str
    ) -> Optional[int]:
        """Numeric sheetId of the first tab, required for batchUpdate merges."""
        try:
            resp = await client.get(
                f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"fields": "sheets(properties(sheetId,title))"},
            )
            resp.raise_for_status()
            sheets = resp.json().get("sheets") or []
            if not sheets:
                return None
            return sheets[0].get("properties", {}).get("sheetId")
        except Exception as e:
            logger.error(f"Failed to read spreadsheet {spreadsheet_id} metadata: {e}")
            return None

    async def clear_sheet_values(
        self, client: httpx.AsyncClient, token: str, spreadsheet_id: str, range_a1: str = "A1:ZZ1000"
    ) -> bool:
        """Clears stale rows/columns before writing a fresh matrix."""
        try:
            resp = await client.post(
                f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{range_a1}:clear",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={},
            )
            if resp.status_code == 200:
                return True
            logger.warning(
                f"Failed to clear sheet {spreadsheet_id}: {resp.status_code} {resp.text[:200]}"
            )
        except Exception as e:
            logger.error(f"Failed to clear sheet {spreadsheet_id}: {e}")
        return False

    async def batch_update_spreadsheet(
        self, client: httpx.AsyncClient, token: str, spreadsheet_id: str, requests: List[Dict[str, Any]]
    ) -> bool:
        """Runs a Sheets API batchUpdate (e.g. mergeCells) against a spreadsheet."""
        try:
            resp = await client.post(
                f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"requests": requests},
            )
            if resp.status_code == 200:
                return True
            logger.warning(
                f"Failed batchUpdate for {spreadsheet_id}: {resp.status_code} {resp.text[:200]}"
            )
        except Exception as e:
            logger.error(f"Failed batchUpdate for {spreadsheet_id}: {e}")
        return False

    async def update_sheet_values(
        self,
        client: httpx.AsyncClient,
        token: str,
        sheet_id: str,
        rows: List[List[Any]],
        value_input_option: str = "RAW",
    ) -> bool:
        """Overwrites a Google Sheet from A1 via Sheets API v4 values.update."""
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/A1"
        try:
            resp = await client.put(
                url,
                params={"valueInputOption": value_input_option},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"values": rows},
            )
            if resp.status_code == 200:
                logger.info(f"📊 [GOOGLE DRIVE] Updated Google Sheet {sheet_id} with {len(rows)} rows")
                return True
            logger.warning(f"Failed to update Google Sheet {sheet_id}: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Failed to update Google Sheet {sheet_id}: {e}")
        return False

    async def cleanup_drive_clutter(
        self, client: httpx.AsyncClient, token: str, protected_names: Optional[set] = None
    ) -> int:
        """
        Remote Google Drive cleanup routine:
        1. Delete only explicit test folders (DEV-*, TEST-*, MOCK-*, SAMPLE-*, DEMO-*,
           or names containing _TEST_/_DEV_/-TEST-/-DEV- segments) in root and Images.
           Names in protected_names (active form ids) are never deleted.
        2. Search for duplicate Master_Survey_Deliverables.csv files in the root folder.
           If multiple exist, delete all except the latest one!
        3. Search and delete any submission_*_summary.csv files on Google Drive.
        """
        root_id = self.root_folder_id or None
        protected = {str(name) for name in (protected_names or set())}
        folder_ids_to_check = [root_id] if root_id else [None]
        if root_id:
            try:
                images_folder_id = await self.find_file_or_folder(
                    client, token, "Images", root_id, "application/vnd.google-apps.folder"
                )
                if images_folder_id and images_folder_id not in folder_ids_to_check:
                    folder_ids_to_check.append(images_folder_id)
            except Exception as e:
                logger.debug(f"Could not find Images folder for cleanup: {e}")

        deleted_count = 0
        headers = {"Authorization": f"Bearer {token}"}
        url = "https://www.googleapis.com/drive/v3/files"

        # 1. Clean up explicit dev/test folders in root and Images
        for p_id in folder_ids_to_check:
            query_parts = [
                "mimeType = 'application/vnd.google-apps.folder'",
                "trashed = false",
            ]
            if p_id:
                query_parts.append(f"'{p_id}' in parents")

            params = {
                "q": " and ".join(query_parts),
                "fields": "nextPageToken, files(id, name)",
                "pageSize": 100,
            }
            page_token = None
            while True:
                if page_token:
                    params["pageToken"] = page_token
                try:
                    resp = await client.get(url, params=params, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    files = data.get("files", [])
                    for f in files:
                        fname = f.get("name", "")
                        fid = f.get("id")
                        if fid and fname not in protected and is_explicit_test_folder(fname):
                            del_resp = await client.delete(f"{url}/{fid}", headers=headers)
                            if del_resp.status_code in (200, 204):
                                logger.info(f"🗑️ [GOOGLE DRIVE] Deleted dev/test folder '{fname}' (ID: {fid})")
                                deleted_count += 1
                            else:
                                logger.warning(f"Failed to delete dev folder '{fname}' (ID: {fid}): {del_resp.status_code}")
                    page_token = data.get("nextPageToken")
                    if not page_token:
                        break
                except Exception as e:
                    logger.error(f"Error querying/deleting dev folders from Google Drive: {e}")
                    break

        # 2. Duplicate Master_Survey_Deliverables.csv cleanup in root
        try:
            q_parts = [
                "name = 'Master_Survey_Deliverables.csv'",
                "trashed = false",
            ]
            if root_id:
                q_parts.append(f"'{root_id}' in parents")
            params = {
                "q": " and ".join(q_parts),
                "fields": "files(id, name, createdTime, modifiedTime)",
                "orderBy": "createdTime desc",
                "pageSize": 100,
            }
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code == 200:
                master_files = resp.json().get("files", [])
                if len(master_files) > 1:
                    logger.info(f"🧹 Found {len(master_files)} Master_Survey_Deliverables.csv files. Keeping latest, deleting duplicates...")
                    for dup in master_files[1:]:
                        dup_id = dup.get("id")
                        if dup_id:
                            del_resp = await client.delete(f"{url}/{dup_id}", headers=headers)
                            if del_resp.status_code in (200, 204):
                                logger.info(f"🗑️ [GOOGLE DRIVE] Deleted duplicate master CSV (ID: {dup_id})")
                                deleted_count += 1
        except Exception as e:
            logger.error(f"Error cleaning duplicate Master CSVs on Google Drive: {e}")

        # 3. Clean up submission_*_summary.csv files
        try:
            q_parts = [
                "name contains 'submission_' and name contains '_summary.csv'",
                "trashed = false",
            ]
            params = {
                "q": " and ".join(q_parts),
                "fields": "nextPageToken, files(id, name)",
                "pageSize": 100,
            }
            page_token = None
            while True:
                if page_token:
                    params["pageToken"] = page_token
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code != 200:
                    break
                data = resp.json()
                summary_files = data.get("files", [])
                for sf in summary_files:
                    sf_id = sf.get("id")
                    sf_name = sf.get("name", "")
                    if sf_id:
                        del_resp = await client.delete(f"{url}/{sf_id}", headers=headers)
                        if del_resp.status_code in (200, 204):
                            logger.info(f"🗑️ [GOOGLE DRIVE] Deleted summary CSV '{sf_name}' (ID: {sf_id})")
                            deleted_count += 1
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
        except Exception as e:
            logger.error(f"Error cleaning submission summary CSVs on Google Drive: {e}")

        return deleted_count

    # Backwards compatibility alias
    async def cleanup_dev_folders_from_drive(
        self, client: httpx.AsyncClient, token: str, protected_names: Optional[set] = None
    ) -> int:
        return await self.cleanup_drive_clutter(client, token, protected_names)


    async def execute_delivery(
        self,
        submission_id: str,
        record: Dict[str, Any],
        images: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """Executes upload of a submission's images under <root>/<form_id>/submissions/<submission_id>/."""
        if record.get("status") != "APPROVED" or is_test_submission(submission_id):
            logger.info(
                f"🚫 [GOOGLE DRIVE] Skipping non-approved/test submission #{submission_id} "
                f"(status: {record.get('status')})"
            )
            return False
        async with httpx.AsyncClient(timeout=30.0) as client:
            token = await self.get_token(client)
            if not token:
                logger.error("Could not obtain Google Drive access token.")
                return False

            # Structure: <root>/<form_id>/submissions/<submission_id>/...
            parent_id = self.root_folder_id if self.root_folder_id else None
            form_folder_name = str(record.get("form_id") or "unknown_form")
            form_folder_id = await self.get_or_create_folder(client, token, form_folder_name, parent_id)
            submissions_folder_id = await self.get_or_create_folder(client, token, "submissions", form_folder_id)
            sub_folder_id = await self.get_or_create_folder(client, token, submission_id, submissions_folder_id)
            target_folder = sub_folder_id or submissions_folder_id

            # Upload images into <form_id>/submissions/{submission_id}/
            if images:
                for idx, img in enumerate(images):
                    fname = img.get("filename") or f"slot_{idx}.webp"
                    raw_bytes = img.get("bytes")
                    if not raw_bytes:
                        continue
                    normalized = normalize_image_bytes(raw_bytes)
                    payload = normalized if normalized is not None else raw_bytes
                    out_name = normalized_image_name(fname) if normalized is not None else os.path.basename(fname)
                    mime_type = detect_image_mime(payload) or _guess_mime(out_name)
                    await self.upload_file(client, token, out_name, payload, mime_type, target_folder)

            # Refresh the per-form CSV and native Google Sheet for approved submissions
            if (
                form_folder_id
                and record.get("status") == "APPROVED"
                and not is_test_submission(submission_id)
            ):
                append_to_local_sheets_mirror(dict(record, form_id=form_folder_name))
                csv_path = local_form_csv_path(form_folder_name)
                if os.path.isfile(csv_path):
                    with open(csv_path, "rb") as csv_f:
                        csv_bytes = csv_f.read()
                    await self.upload_file(
                        client, token, f"{form_folder_name}.csv", csv_bytes,
                        "text/csv; charset=UTF-8", form_folder_id,
                    )
                    await _sync_form_sheet(
                        self, client, token, form_folder_name, form_folder_id, csv_path
                    )

            logger.info(f"🚀 [GOOGLE DRIVE] Complete remote upload for #{submission_id} into {form_folder_name}/submissions/{submission_id} finished successfully.")
            return True

def _read_csv_rows(path: str) -> List[List[str]]:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


def _write_csv_atomic(path: str, rows: List[List[Any]]) -> None:
    """Writes a CSV via a temp file in the same directory then atomically replaces it."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".csv-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerows(rows)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _ensure_csv_schema(path: str) -> None:
    """Creates the CSV with CSV_HEADERS or upgrades a legacy header row in place."""
    rows = _read_csv_rows(path)
    if rows and rows[0] == CSV_HEADERS:
        return
    id_index = len(CSV_HEADERS) - 1
    migrated_rows = [CSV_HEADERS]
    for old_row in (rows[1:] if rows else []):
        old_row = list(old_row)[:id_index]
        if len(old_row) < id_index:
            old_row += [""] * (id_index - len(old_row))
        old_row.append("")  # legacy rows predate the submission id column
        migrated_rows.append(old_row)
    _write_csv_atomic(path, migrated_rows)


def append_row_to_csv(path: str, row: List[Any]) -> bool:
    """
    Appends one row, skipping a row with the same submission id.

    The submission id is the last CSV column. Legacy rows with an empty id fall back
    to the previous content match (exact, or every column except the volatile
    timestamp) so they are not duplicated before the next full rebuild.
    """
    _ensure_csv_schema(path)
    normalized = [str(cell) if cell is not None else "" for cell in row]
    id_index = len(CSV_HEADERS) - 1
    target_id = normalized[id_index].strip() if len(normalized) > id_index else ""
    existing_rows = _read_csv_rows(path)[1:]
    for existing in existing_rows:
        existing_id = existing[id_index].strip() if len(existing) > id_index else ""
        if target_id:
            if existing_id == target_id:
                return False
        elif not existing_id and (existing == normalized or existing[1:] == normalized[1:]):
            return False
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerow(row)
    return True


def rebuild_local_csvs(records: Dict[str, Dict[str, Any]]) -> int:
    """
    Atomically rebuilds the Master CSV and every per-form CSV from the complete
    approved record set, guaranteeing exactly one row per submission_id.

    Returns the number of records written.
    """
    ordered = [
        record
        for _, record in sorted(
            records.items(),
            key=lambda item: (
                str(item[1].get("submitted_at") or item[1].get("created_at") or ""),
                str(item[0]),
            ),
        )
    ]
    rows = [extract_survey_row_data(record) for record in ordered]
    _write_csv_atomic(MASTER_CSV_FILE, [CSV_HEADERS] + rows)

    rows_by_form: Dict[str, List[List[Any]]] = {}
    for record, row in zip(ordered, rows):
        form_id = str(record.get("form_id") or "unknown_form")
        rows_by_form.setdefault(form_id, []).append(row)
    for form_id, form_rows in rows_by_form.items():
        _write_csv_atomic(local_form_csv_path(form_id), [CSV_HEADERS] + form_rows)

    logger.info(
        f"📊 [DRIVE SHEETS MIRROR] Rebuilt {len(ordered)} rows across "
        f"{len(rows_by_form)} per-form CSVs and the Master CSV"
    )
    return len(ordered)


def append_to_local_sheets_mirror(record: Dict[str, Any]) -> None:
    """Appends an approved record to the Master CSV and its per-form CSV mirror."""
    if record.get("status") != "APPROVED":
        logger.debug(f"Skipping local sheets mirror for non-approved submission #{record.get('submission_id')} (status: {record.get('status')})")
        return

    row = extract_survey_row_data(record)
    try:
        appended = append_row_to_csv(MASTER_CSV_FILE, row)
        form_id = str(record.get("form_id") or "unknown_form")
        form_appended = append_row_to_csv(local_form_csv_path(form_id), row)
        if appended or form_appended:
            logger.info(f"📊 [DRIVE SHEETS MIRROR] Appended #{record.get('submission_id')} to {MASTER_CSV_FILE} and {form_id}.csv")
    except Exception as e:
        logger.error(f"Failed to append to local sheets mirror: {e}")

def save_submission_images_locally(
    submission_id: str,
    images: Optional[List[Dict[str, Any]]],
    form_id: Optional[str] = None,
) -> Optional[str]:
    """
    Saves all photos for a submission into Images/<form_id>/submissions/<submission_id>/.
    Only saves to deliverables mirror if not a dev/test/mock submission and images is not empty!
    """
    if is_test_submission(submission_id) or not images:
        logger.debug(f"Skipping deliverables mirror save for dev/test or empty images submission #{submission_id}")
        return None
    form_folder = str(form_id or "unknown_form")
    subfolder_path = os.path.join(LOCAL_IMAGES_DIR, form_folder, "submissions", submission_id)
    os.makedirs(subfolder_path, exist_ok=True)

    saved_paths = []
    for idx, img in enumerate(images):
        fname = img.get("filename") or f"slot_{idx}.webp"
        raw_bytes = img.get("bytes")
        if not raw_bytes:
            continue
        normalized = normalize_image_bytes(raw_bytes)
        if normalized is None:
            # Undecodable payload: keep original bytes/name rather than corrupting the slot.
            payload, out_name = raw_bytes, os.path.basename(fname)
        else:
            payload, out_name = normalized, normalized_image_name(fname)
        out_file = os.path.join(subfolder_path, out_name)
        with open(out_file, "wb") as f:
            f.write(payload)
        saved_paths.append(out_file)

    if not saved_paths and os.path.exists(subfolder_path):
        try:
            os.rmdir(subfolder_path)
        except OSError:
            pass
        return None

    logger.info(f"📁 [DRIVE IMAGES MIRROR] Saved {len(saved_paths)} images to {subfolder_path}")
    return subfolder_path


def backfill_mirror_images_from_submission_photos(records: Dict[str, Dict[str, Any]]) -> int:
    """
    Normalizes the mirror's images and fills missing slots from the raw photo store.

    Every mirror image is decoded and stored as WebP under Images/<form_id>/submissions/
    <submission_id>/, so filename, payload and MIME always agree. Missing slots are copied
    from data/submission_photos/<submission_id>/ and normalized too. Idempotent; returns
    the number of files created or rewritten.
    """
    changed = 0
    for submission_id, record in records.items():
        form_id = str(record.get("form_id") or "unknown_form")
        target_dir = os.path.join(LOCAL_IMAGES_DIR, form_id, "submissions", submission_id)
        source_dir = os.path.join(SUBMISSION_PHOTOS_DIR, submission_id)

        existing_files = [
            path for path in glob.glob(os.path.join(target_dir, "*"))
            if os.path.isfile(path) and os.path.splitext(path)[1].lower() in IMAGE_FILE_EXTENSIONS
        ]
        source_files = [
            path for path in glob.glob(os.path.join(source_dir, "*"))
            if os.path.isfile(path)
            and os.path.splitext(path)[1].lower() in IMAGE_FILE_EXTENSIONS
            and os.path.getsize(path) > 0
        ]
        if not existing_files and not source_files:
            continue

        os.makedirs(target_dir, exist_ok=True)
        present_stems = set()

        # Normalize what is already mirrored (fixes legacy .jpg-with-WebP-bytes and vice versa).
        for path in existing_files:
            stem = os.path.splitext(os.path.basename(path))[0]
            present_stems.add(stem)
            try:
                with open(path, "rb") as f:
                    raw_bytes = f.read()
            except OSError:
                continue
            normalized = normalize_image_bytes(raw_bytes)
            if normalized is None:
                continue
            target_path = os.path.join(target_dir, normalized_image_name(path))
            if os.path.abspath(target_path) != os.path.abspath(path):
                with open(target_path, "wb") as f:
                    f.write(normalized)
                try:
                    os.remove(path)
                except OSError:
                    pass
                changed += 1
            elif normalized != raw_bytes:
                with open(target_path, "wb") as f:
                    f.write(normalized)
                changed += 1

        # Copy missing slots from the raw store.
        for src in source_files:
            stem = os.path.splitext(os.path.basename(src))[0]
            if stem in present_stems:
                continue
            try:
                with open(src, "rb") as f:
                    raw_bytes = f.read()
            except OSError:
                continue
            normalized = normalize_image_bytes(raw_bytes)
            if normalized is None:
                logger.warning(f"Skipping undecodable raw photo {src}")
                continue
            dest = os.path.join(target_dir, normalized_image_name(src))
            with open(dest, "wb") as f:
                f.write(normalized)
            present_stems.add(stem)
            changed += 1

    if changed:
        logger.info(f"📁 [DRIVE IMAGES MIRROR] Normalized/backfilled {changed} images from {SUBMISSION_PHOTOS_DIR}")
    return changed


def migrate_unknown_form_mirror(submission_forms: Dict[str, str]) -> int:
    """
    Moves known submissions out of Images/unknown_form/submissions into their real form
    folder so the fallback bucket is not uploaded twice. Leaves unknown submissions in
    place and never deletes data that has no known form mapping.
    """
    unknown_submissions_dir = os.path.join(LOCAL_IMAGES_DIR, "unknown_form", "submissions")
    if not os.path.isdir(unknown_submissions_dir):
        return 0

    moved = 0
    for sub_id in os.listdir(unknown_submissions_dir):
        form_id = submission_forms.get(sub_id)
        if not form_id or form_id == "unknown_form":
            continue
        src_dir = os.path.join(unknown_submissions_dir, sub_id)
        if not os.path.isdir(src_dir):
            continue
        dest_dir = os.path.join(LOCAL_IMAGES_DIR, form_id, "submissions", sub_id)
        os.makedirs(dest_dir, exist_ok=True)
        for src in glob.glob(os.path.join(src_dir, "*")):
            if not os.path.isfile(src):
                continue
            dest = os.path.join(dest_dir, os.path.basename(src))
            if not os.path.exists(dest):
                shutil.copy2(src, dest)
                moved += 1
                os.remove(src)
            else:
                try:
                    same = open(src, "rb").read() == open(dest, "rb").read()
                except OSError:
                    same = False
                if same:
                    os.remove(src)
                else:
                    # Keep the distinct legacy file in unknown_form rather than losing data.
                    logger.warning(f"Keeping distinct legacy file {src} (target {dest} differs)")
        try:
            os.rmdir(src_dir)
        except OSError:
            pass

    for empty_dir in (unknown_submissions_dir, os.path.join(LOCAL_IMAGES_DIR, "unknown_form")):
        try:
            os.rmdir(empty_dir)
        except OSError:
            pass

    if moved:
        logger.info(f"📁 [DRIVE IMAGES MIRROR] Moved {moved} files out of unknown_form to their real form folders")
    return moved


def migrate_local_deliverables_layout(submission_forms: Optional[Dict[str, str]] = None) -> None:
    """
    Migrates legacy local layouts to Images/<form_id>/submissions/<submission_id>/.

    Legacy layouts:
    - Images/<submission_id>/... (flat, resolved to a form via submission_forms)
    - Images/<form_id>/<submission_id>/... (pre-submissions per-form level)
    """
    if not os.path.isdir(LOCAL_IMAGES_DIR):
        return
    submission_forms = submission_forms or {}
    for entry in os.listdir(LOCAL_IMAGES_DIR):
        entry_path = os.path.join(LOCAL_IMAGES_DIR, entry)
        if not os.path.isdir(entry_path):
            continue
        child_dirs = [d for d in os.listdir(entry_path) if os.path.isdir(os.path.join(entry_path, d))]
        if not child_dirs:
            # Flat submission folder, only migratable when its form is known.
            if not any(os.path.isfile(os.path.join(entry_path, f)) for f in os.listdir(entry_path)):
                continue
            form_id = submission_forms.get(entry)
            dest = os.path.join(LOCAL_IMAGES_DIR, form_id or "", "submissions", entry)
            if not form_id or os.path.exists(dest):
                continue
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(entry_path, dest)
                logger.info(f"📁 [DRIVE IMAGES MIRROR] Migrated flat folder {entry} -> {form_id}/submissions/{entry}")
            except Exception as e:
                logger.warning(f"Failed to migrate flat folder {entry}: {e}")
            continue
        if "submissions" in child_dirs:
            continue  # already migrated
        dest_parent = os.path.join(entry_path, "submissions")
        for child in child_dirs:
            src = os.path.join(entry_path, child)
            dest = os.path.join(dest_parent, child)
            if os.path.exists(dest):
                continue
            try:
                os.makedirs(dest_parent, exist_ok=True)
                shutil.move(src, dest)
            except Exception as e:
                logger.warning(f"Failed to migrate submission folder {entry}/{child}: {e}")


def rename_local_form_folder(old_form_id: str, new_form_id: str) -> None:
    """Mirror a form-id rename in the local Drive deliverable mirror (images + CSV)."""
    old_dir = os.path.join(LOCAL_IMAGES_DIR, old_form_id)
    new_dir = os.path.join(LOCAL_IMAGES_DIR, new_form_id)
    if os.path.isdir(old_dir) and not os.path.exists(new_dir):
        os.makedirs(os.path.dirname(new_dir), exist_ok=True)
        shutil.move(old_dir, new_dir)
        logger.info(f"📁 [DRIVE IMAGES MIRROR] Renamed local folder {old_form_id} -> {new_form_id}")

    old_csv = local_form_csv_path(old_form_id)
    new_csv = local_form_csv_path(new_form_id)
    if os.path.isfile(old_csv) and not os.path.exists(new_csv):
        os.makedirs(os.path.dirname(new_csv), exist_ok=True)
        shutil.move(old_csv, new_csv)
        logger.info(f"📊 [DRIVE SHEETS MIRROR] Renamed local CSV {old_form_id}.csv -> {new_form_id}.csv")


async def _rename_drive_file(
    http_client: httpx.AsyncClient, token: str, parent_id: str, old_name: str, new_name: str
) -> bool:
    """Renames one direct child file of a Drive folder, if present."""
    client = GoogleDriveClient()
    file_id = await client.find_file_or_folder(http_client, token, old_name, parent_id)
    if not file_id:
        return False
    resp = await http_client.patch(
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": new_name},
    )
    if resp.status_code == 200:
        logger.info(f"📄 [GOOGLE DRIVE] Renamed file {old_name} -> {new_name}")
        return True
    logger.warning(f"Failed to rename Drive file {old_name}: {resp.status_code} {resp.text[:200]}")
    return False


async def rename_form_folder(old_form_id: str, new_form_id: str) -> bool:
    """Renames the Drive form folder plus its per-form CSV and Google Sheet."""
    if not has_google_credentials():
        return False
    client = GoogleDriveClient()
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        token = await client.get_token(http_client)
        if not token:
            return False
        root_id = client.root_folder_id or None
        folder_id = await client.find_file_or_folder(
            http_client, token, old_form_id, root_id, "application/vnd.google-apps.folder"
        )
        if not folder_id:
            return False
        resp = await http_client.patch(
            f"https://www.googleapis.com/drive/v3/files/{folder_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": new_form_id},
        )
        if resp.status_code != 200:
            logger.error(f"Failed to rename Drive folder {old_form_id}: {resp.status_code} {resp.text[:200]}")
            return False
        logger.info(f"📁 [GOOGLE DRIVE] Renamed form folder {old_form_id} -> {new_form_id}")
        await _rename_drive_file(http_client, token, folder_id, f"{old_form_id}.csv", f"{new_form_id}.csv")
        await _rename_drive_file(http_client, token, folder_id, old_form_id, new_form_id)
        return True


def has_google_credentials() -> bool:
    """Checks whether any Google Drive credentials (OAuth2 or Service Account) are configured."""
    return get_google_oauth_credentials() is not None or get_google_service_account_info() is not None


async def upload_to_google_drive(
    submission_id: str,
    record: Dict[str, Any],
    images: Optional[List[Dict[str, Any]]] = None,
    form_id: Optional[str] = None,
) -> bool:
    """
    Orchestrates delivery to Google Drive & Google Sheets:
    1. ONLY status == 'APPROVED' submissions may be included in the Google Drive delivery!
       If record.get("status") != "APPROVED", immediately skip.
    2. Writes to the local structured deliverables mirror:
       - Images/<form_id>/submissions/<submission_id>/ (all images for the submission)
       - Sheets/Master_Survey_Deliverables.csv (global master)
       - Sheets/forms/<form_id>.csv (per-form spreadsheet mirror)
    3. Individual surveys are NOT uploaded directly to Google Drive upon submission.
       Only the master/per-form data sheets and image folders are synced in batch
       (via sync_all_approved_to_drive / scripts/sync_to_drive.py).
    """
    if form_id and not record.get("form_id"):
        record = dict(record, form_id=form_id)

    if record.get("status") != "APPROVED":
        logger.info(f"🚫 [GOOGLE DRIVE DELIVERY] Skipping #{submission_id} because status is '{record.get('status')}' (only 'APPROVED' accepted).")
        return False

    if is_test_submission(submission_id):
        logger.info(f"🚫 [GOOGLE DRIVE DELIVERY] Skipping test/dev submission #{submission_id} from Google Drive delivery.")
        return False
    # 1. Update Sheets Deliverable Mirror
    append_to_local_sheets_mirror(record)

    # 1b. Archive Master Sheet to R2
    if os.path.exists(MASTER_CSV_FILE) and os.path.getsize(MASTER_CSV_FILE) > 0:
        try:
            with open(MASTER_CSV_FILE, "rb") as f:
                csv_bytes = f.read()
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            archive_name = f"Master_Survey_Deliverables_{timestamp}.csv"
            await r2_client.archive_sheet(archive_name, csv_bytes)
            logger.info(f"📦 [R2 SHEET ARCHIVE] Archived last sheet to R2 as archives/sheets/{archive_name}")
        except Exception as e:
            logger.error(f"Failed to archive master sheet to R2: {e}")

    # 2. Save Images to Subfolder (1 subfolder per submission)
    if images:
        save_submission_images_locally(submission_id, images, form_id=record.get("form_id"))

    # 3. Individual surveys are NOT uploaded directly to Google Drive.
    # Deliverables are synchronized via sync_all_approved_to_drive and scripts/sync_to_drive.py,
    # which ensures only the master data sheet and image folder are synced to Google Drive.
    logger.info(f"📁 [DRIVE DELIVERABLES MIRROR] #{submission_id} recorded in Sheets/Master_Survey_Deliverables.csv, Sheets/forms/<form_id>.csv and Images/<form_id>/submissions/{submission_id} (individual surveys not uploaded directly; only data sheets and image folders are synced).")
    return True

async def _sync_form_sheet(
    client: GoogleDriveClient,
    http_client: httpx.AsyncClient,
    token: str,
    form_id: str,
    form_folder_id: str,
    csv_path: str,
) -> bool:
    """Creates or updates the native Google Sheet named <form_id> inside its form folder."""
    rows = _read_csv_rows(csv_path) or [CSV_HEADERS]
    sheet_id = await client.find_file_or_folder(http_client, token, form_id, form_folder_id)
    if sheet_id:
        return await client.update_sheet_values(http_client, token, sheet_id, rows)
    with open(csv_path, "rb") as f:
        csv_bytes = f.read()
    return await client.import_csv_as_sheet(
        http_client, token, form_id, csv_bytes, form_folder_id
    ) is not None


def _cursor_rows_to_records(cur: Any, rows: List[Any]) -> List[Dict[str, Any]]:
    """Maps psycopg tuple rows to dict records using the cursor description."""
    columns = [getattr(desc, "name", None) for desc in (getattr(cur, "description", None) or [])]
    records: List[Dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            records.append(row)
        elif columns and all(columns):
            records.append(dict(zip(columns, row)))
    return records


async def sync_all_approved_to_drive(db_pool: Optional[Any] = None) -> bool:
    """
    Pushes the Master CSV, per-form CSVs/Sheets and submission images to Google Drive.

    Remote layout: <root>/<form_id>/submissions/<submission_id>/*, <root>/<form_id>/<form_id>.csv
    and a native Google Sheet named <form_id> inside <root>/<form_id>/.

    When a database pool is provided, a session advisory lock serializes periodic
    and manual runs; a second concurrent caller returns False without doing work.
    """
    if db_pool is None:
        return await _sync_all_approved_to_drive_unlocked(None)

    try:
        async with db_pool.connection() as lock_conn:
            lock_cur = await lock_conn.execute(
                "SELECT pg_try_advisory_lock(%s);", (DRIVE_SYNC_LOCK_KEY,)
            )
            row = await lock_cur.fetchone()
            if not row or not row[0]:
                logger.info("⏭️ [AUTO DRIVE SYNC] Skipped: another Drive sync is already running.")
                return False
            try:
                return await _sync_all_approved_to_drive_unlocked(db_pool)
            finally:
                try:
                    await lock_conn.execute(
                        "SELECT pg_advisory_unlock(%s);", (DRIVE_SYNC_LOCK_KEY,)
                    )
                except Exception as unlock_err:
                    logger.warning(f"Failed to release Drive sync lock: {unlock_err}")
    except Exception as e:
        logger.warning(f"Drive sync advisory lock unavailable ({e}); running without it.")
        return await _sync_all_approved_to_drive_unlocked(db_pool)


async def _sync_all_approved_to_drive_unlocked(db_pool: Optional[Any] = None) -> bool:
    # Archive master sheet to R2 on every sync execution
    if os.path.exists(MASTER_CSV_FILE) and os.path.getsize(MASTER_CSV_FILE) > 0:
        try:
            with open(MASTER_CSV_FILE, "rb") as f:
                csv_bytes = f.read()
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            archive_name = f"Master_Survey_Deliverables_{timestamp}.csv"
            await r2_client.archive_sheet(archive_name, csv_bytes)
            logger.info(f"📦 [R2 SHEET ARCHIVE] Archived last sheet to R2 as archives/sheets/{archive_name}")
        except Exception as e:
            logger.error(f"Failed to archive master sheet to R2 during sync: {e}")

    if not has_google_credentials():
        logger.debug("No Google Drive credentials configured; skipping drive sync.")
        return False

    # Collect approved, non-test records from dev storage and the database
    approved_records: Dict[str, Dict[str, Any]] = {}

    def _collect(record: Dict[str, Any]) -> None:
        submission_id = record.get("submission_id")
        if not submission_id or record.get("status") != "APPROVED" or is_test_submission(submission_id):
            return
        approved_records[str(submission_id)] = record

    # 1. Dev storage
    if os.path.exists(DEV_SUBMISSIONS_FILE):
        try:
            with open(DEV_SUBMISSIONS_FILE, "r", encoding="utf-8") as f:
                for r in json.load(f):
                    if isinstance(r, dict):
                        _collect(r)
        except Exception as e:
            logger.debug(f"Could not read dev storage for approved records: {e}")

    # 2. Database
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT * FROM survey_submissions WHERE status = 'APPROVED';")
                    rows = await cur.fetchall()
                    for record in _cursor_rows_to_records(cur, rows):
                        _collect(record)
        except Exception as e:
            logger.debug(f"Could not query DB for approved records: {e}")

    submission_forms = {
        sid: str(rec.get("form_id") or "unknown_form") for sid, rec in approved_records.items()
    }
    approved_ids = set(approved_records)

    # Active form ids must survive test-folder cleanup even if their names look testy.
    protected_form_names = set(submission_forms.values())
    try:
        for entry in os.listdir(LOCAL_IMAGES_DIR):
            if os.path.isdir(os.path.join(LOCAL_IMAGES_DIR, entry)):
                protected_form_names.add(entry)
    except OSError:
        pass
    protected_form_names.update(
        os.path.splitext(os.path.basename(path))[0]
        for path in glob.glob(os.path.join(LOCAL_FORMS_SHEETS_DIR, "*.csv"))
    )
    protected_form_names.add("_legacy_unknown")
    try:
        from core.state import FORM_REGISTRY

        protected_form_names.update(str(form_id) for form_id in FORM_REGISTRY.keys())
    except Exception as e:
        logger.debug(f"Could not read form registry for cleanup protection: {e}")

    # Rebuild the Master + per-form CSVs from the full approved record set.
    # Atomic writes guarantee one row per submission_id and no half-written files.
    try:
        rebuild_local_csvs(approved_records)
    except Exception as e:
        logger.error(f"[AUTO DRIVE SYNC] CSV rebuild failed ({e}); falling back to per-record append.")
        for record in approved_records.values():
            append_to_local_sheets_mirror(record)

    client = GoogleDriveClient()
    async with httpx.AsyncClient(timeout=45.0) as http_client:
        token = await client.get_token(http_client)
        if not token:
            logger.error("Failed to obtain Google Drive access token for periodic sync.")
            return False

        root_id = client.root_folder_id or None

        # Clean up test folders, duplicate CSVs, and summary files from Google Drive
        try:
            await client.cleanup_drive_clutter(http_client, token, protected_form_names)
        except Exception as e:
            logger.warning(f"Failed to cleanup clutter from Google Drive: {e}")

        # Quarantine the legacy unknown_form bucket; data is preserved under _legacy_unknown.
        try:
            unknown_folder_id = await client.find_file_or_folder(
                http_client, token, "unknown_form", root_id, "application/vnd.google-apps.folder"
            )
            if unknown_folder_id:
                legacy_folder_id = await client.find_file_or_folder(
                    http_client, token, "_legacy_unknown", root_id, "application/vnd.google-apps.folder"
                )
                if legacy_folder_id:
                    logger.info("🗂️ [AUTO DRIVE SYNC] _legacy_unknown already exists; leaving unknown_form untouched.")
                else:
                    rename_resp = await http_client.patch(
                        f"https://www.googleapis.com/drive/v3/files/{unknown_folder_id}",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"name": "_legacy_unknown"},
                    )
                    if rename_resp.status_code == 200:
                        logger.info("🗂️ [AUTO DRIVE SYNC] Renamed unknown_form -> _legacy_unknown (quarantined)")
                    else:
                        logger.warning(
                            f"[AUTO DRIVE SYNC] Could not quarantine unknown_form: "
                            f"HTTP {rename_resp.status_code} {rename_resp.text[:200]}"
                        )
        except Exception as e:
            logger.warning(f"[AUTO DRIVE SYNC] unknown_form quarantine failed: {e}")

        # 1. Upload Master Sheet (global, kept at root for backwards compatibility)
        if os.path.exists(MASTER_CSV_FILE):
            try:
                with open(MASTER_CSV_FILE, "rb") as f:
                    content = f.read()
                file_id = await client.upload_file(
                    http_client, token, "Master_Survey_Deliverables.csv", content, "text/csv; charset=UTF-8", root_id
                )
                logger.info(f"📊 [AUTO DRIVE SYNC] Uploaded Master CSV to Drive. File ID: {file_id}")
            except Exception as e:
                logger.error(f"[AUTO DRIVE SYNC] Failed to upload Master CSV: {e}")

        # 1b. Migrate legacy local dirs into Images/<form_id>/submissions/<submission_id>/
        try:
            migrate_local_deliverables_layout(submission_forms)
        except Exception as e:
            logger.warning(f"[AUTO DRIVE SYNC] Legacy deliverables migration failed: {e}")

        # 1c. Move known submissions out of the unknown_form fallback bucket
        try:
            migrate_unknown_form_mirror(submission_forms)
        except Exception as e:
            logger.warning(f"[AUTO DRIVE SYNC] unknown_form migration failed: {e}")

        # 1d. Normalize mirrored images and backfill raw photos for older approved submissions
        try:
            backfill_mirror_images_from_submission_photos(approved_records)
        except Exception as e:
            logger.warning(f"[AUTO DRIVE SYNC] Photo mirror backfill failed: {e}")

        # 2. Upload approved submissions under <root>/<form_id>/submissions/<submission_id>/
        uploaded_count = 0
        skipped_count = 0
        try:
            headers = {"Authorization": f"Bearer {token}"}
            url_files = "https://www.googleapis.com/drive/v3/files"

            async def list_remote_folders(parent_id: Optional[str]) -> Dict[str, str]:
                if parent_id:
                    q = f"'{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
                else:
                    q = "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
                params = {"q": q, "fields": "nextPageToken, files(id, name)", "pageSize": 1000}
                found: Dict[str, str] = {}
                page_tok = None
                while True:
                    if page_tok:
                        params["pageToken"] = page_tok
                    resp = await http_client.get(url_files, params=params, headers=headers)
                    if resp.status_code != 200:
                        break
                    body = resp.json()
                    for f in body.get("files", []):
                        found[f["name"]] = f["id"]
                    page_tok = body.get("nextPageToken")
                    if not page_tok:
                        break
                return found

            form_ids = set(submission_forms.values())
            for entry in os.listdir(LOCAL_IMAGES_DIR):
                entry_path = os.path.join(LOCAL_IMAGES_DIR, entry)
                if os.path.isdir(os.path.join(entry_path, "submissions")):
                    form_ids.add(entry)
            # CSV/Sheet-only forms still need their Drive folder and submissions/ container.
            for csv_file in glob.glob(os.path.join(LOCAL_FORMS_SHEETS_DIR, "*.csv")):
                form_ids.add(os.path.splitext(os.path.basename(csv_file))[0])
            # unknown_form is a legacy fallback bucket: known submissions are migrated to
            # their real form, and the remainder must not be uploaded as a duplicate form.
            form_ids.discard("unknown_form")

            for form_id in sorted(form_ids):
                form_folder_id = await client.get_or_create_folder(
                    http_client, token, form_id, root_id
                )
                if not form_folder_id:
                    continue

                # Every form folder gets a submissions/ container, even CSV-only forms.
                submissions_folder_id = await client.get_or_create_folder(
                    http_client, token, "submissions", form_folder_id
                )

                # Per-form CSV + native Google Sheet inside the form folder
                csv_path = local_form_csv_path(form_id)
                if os.path.isfile(csv_path):
                    try:
                        with open(csv_path, "rb") as f:
                            csv_bytes = f.read()
                        await client.upload_file(
                            http_client, token, f"{form_id}.csv", csv_bytes,
                            "text/csv; charset=UTF-8", form_folder_id,
                        )
                    except Exception as e:
                        logger.error(f"[AUTO DRIVE SYNC] Failed to upload CSV for {form_id}: {e}")
                    try:
                        await _sync_form_sheet(client, http_client, token, form_id, form_folder_id, csv_path)
                    except Exception as e:
                        logger.error(f"[AUTO DRIVE SYNC] Failed to sync Google Sheet for {form_id}: {e}")

                submissions_dir = os.path.join(LOCAL_IMAGES_DIR, form_id, "submissions")
                if not os.path.isdir(submissions_dir) or not submissions_folder_id:
                    continue
                remote_subfolders = await list_remote_folders(submissions_folder_id)

                for sub_dir in glob.glob(os.path.join(submissions_dir, "*")):
                    if not os.path.isdir(sub_dir):
                        continue
                    sub_id = os.path.basename(sub_dir)
                    if is_test_submission(sub_id):
                        continue
                    if sub_id not in approved_ids:
                        continue
                    files = [
                        f for f in glob.glob(os.path.join(sub_dir, "*.*"))
                        if os.path.isfile(f)
                        and os.path.getsize(f) > 0
                        and os.path.splitext(f)[1].lower() in IMAGE_FILE_EXTENSIONS
                    ]
                    if not files:
                        continue

                    if sub_id in remote_subfolders:
                        sub_folder_id = remote_subfolders[sub_id]
                        remote_files = await _list_remote_files(http_client, token, sub_folder_id)
                        local_names = {os.path.basename(f) for f in files}
                        local_stems = {os.path.splitext(name)[0] for name in local_names}
                        stale_remote = [
                            (name, file_id)
                            for name, file_id in remote_files.items()
                            if os.path.splitext(name)[1].lower() in IMAGE_FILE_EXTENSIONS
                            and os.path.splitext(name)[0] in local_stems
                            and name not in local_names
                        ]
                        for stale_name, stale_id in stale_remote:
                            del_resp = await http_client.delete(f"{url_files}/{stale_id}", headers=headers)
                            if del_resp.status_code in (200, 204):
                                logger.info(
                                    f"🗑️ [AUTO DRIVE SYNC] Removed stale format variant "
                                    f"{form_id}/submissions/{sub_id}/{stale_name}"
                                )
                                remote_files.pop(stale_name, None)
                            else:
                                logger.warning(
                                    f"[AUTO DRIVE SYNC] Could not remove stale {stale_name}: "
                                    f"HTTP {del_resp.status_code}"
                                )
                        remote_names = set(remote_files)
                        skipped_count += 1
                    else:
                        sub_folder_id = await client.get_or_create_folder(
                            http_client, token, sub_id, submissions_folder_id
                        )
                        remote_names = set()
                        if sub_folder_id:
                            remote_subfolders[sub_id] = sub_folder_id
                    if not sub_folder_id:
                        continue

                    for fpath in files:
                        fname = os.path.basename(fpath)
                        if fname in remote_names:
                            continue
                        with open(fpath, "rb") as img_f:
                            img_bytes = img_f.read()
                        mime_type = detect_image_mime(img_bytes) or _guess_mime(fname)
                        file_id = await client.upload_file(
                            http_client, token, fname, img_bytes, mime_type, sub_folder_id
                        )
                        if file_id:
                            uploaded_count += 1
                        else:
                            logger.warning(f"[AUTO DRIVE SYNC] Upload failed for {form_id}/submissions/{sub_id}/{fname}; will retry next sync.")

            logger.info(
                f"🖼️ [AUTO DRIVE SYNC] Synced {uploaded_count} new image files across "
                f"form folders under root {root_id}; {skipped_count} submission folders already existed."
            )
        except Exception as e:
            logger.error(f"[AUTO DRIVE SYNC] Failed to sync form folders: {e}")

        # 3. Internal CTV matrix (isolated: failures must not affect customer deliverables)
        if db_pool:
            try:
                from services.ctv_matrix import sync_internal_ctv_matrix

                await sync_internal_ctv_matrix(
                    client, http_client, token, root_id, db_pool
                )
            except Exception as e:
                logger.error(f"[AUTO DRIVE SYNC] Internal CTV matrix failed: {e}")

        return True
