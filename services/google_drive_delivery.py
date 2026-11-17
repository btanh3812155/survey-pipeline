import os
import io
import csv
import mimetypes
import glob
import json
import logging
import asyncio
import shutil
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

os.makedirs(LOCAL_IMAGES_DIR, exist_ok=True)
os.makedirs(LOCAL_SHEETS_DIR, exist_ok=True)

MASTER_CSV_FILE = os.path.join(LOCAL_SHEETS_DIR, "Master_Survey_Deliverables.csv")

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

    tot_gross = record.get("total_gross_revenue") or (computed_tot_gross if computed_tot_gross > 0 else legacy_gross)
    tot_net = record.get("total_net_income") or (computed_tot_net if computed_tot_net > 0 else legacy_net)
    tot_trips = record.get("total_trips") or (computed_tot_trips if computed_tot_trips > 0 else legacy_trips)

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
    ]
# Google Auth configurations
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or getattr(settings, "GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_DRIVE_ROOT_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_ROOT_FOLDER_ID") or getattr(settings, "GOOGLE_DRIVE_ROOT_FOLDER_ID", "")
GOOGLE_ADMIN_EMAIL = os.environ.get("GOOGLE_ADMIN_EMAIL") or getattr(settings, "GOOGLE_ADMIN_EMAIL", "admin@trustbridgevn.com")
GOOGLE_DRIVE_REFRESH_TOKEN = os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN") or getattr(settings, "GOOGLE_DRIVE_REFRESH_TOKEN", "")
GOOGLE_DRIVE_CLIENT_ID = os.environ.get("GOOGLE_DRIVE_CLIENT_ID") or getattr(settings, "GOOGLE_DRIVE_CLIENT_ID", "")
GOOGLE_DRIVE_CLIENT_SECRET = os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET") or getattr(settings, "GOOGLE_DRIVE_CLIENT_SECRET", "")
GOOGLE_DRIVE_ACCESS_TOKEN = os.environ.get("GOOGLE_DRIVE_ACCESS_TOKEN") or getattr(settings, "GOOGLE_DRIVE_ACCESS_TOKEN", "")


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
    jwt_token = create_signed_jwt(sa_info, ["https://www.googleapis.com/auth/drive"])
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

    async def cleanup_drive_clutter(
        self, client: httpx.AsyncClient, token: str
    ) -> int:
        """
        Remote Google Drive cleanup routine:
        1. Search and delete any folders whose name contains TEST, DEV, MOCK, SAMPLE (including VN-DRIVE-TEST*)
           in root and Images parent folder.
        2. Search for duplicate Master_Survey_Deliverables.csv files in the root folder.
           If multiple exist, delete all except the latest one!
        3. Search and delete any submission_*_summary.csv files on Google Drive.
        """
        root_id = self.root_folder_id or None
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
        test_tags = ("TEST", "DEV", "MOCK", "SAMPLE", "DEMO")

        # 1. Clean up dev/test folders in root and Images
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
                        fname_upper = fname.upper()
                        if fid and any(tag in fname_upper for tag in test_tags):
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
        self, client: httpx.AsyncClient, token: str
    ) -> int:
        return await self.cleanup_drive_clutter(client, token)


    async def execute_delivery(
        self,
        submission_id: str,
        record: Dict[str, Any],
        images: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """Executes upload of images subfolder under Images/ to Google Drive."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            token = await self.get_token(client)
            if not token:
                logger.error("Could not obtain Google Drive access token.")
                return False

            # Structure: <root>/<form_id>/<submission_id>/...
            parent_id = self.root_folder_id if self.root_folder_id else None
            form_folder_name = str(record.get("form_id") or "unknown_form")
            form_folder_id = await self.get_or_create_folder(client, token, form_folder_name, parent_id)

            # 2. Create or resolve submission folder under the form folder
            sub_folder_id = await self.get_or_create_folder(client, token, submission_id, form_folder_id)
            target_folder = sub_folder_id or form_folder_id

            # 3. Upload images into <form_id>/{submission_id}/
            if images:
                for idx, img in enumerate(images):
                    fname = img.get("filename") or f"slot_{idx}.webp"
                    raw_bytes = img.get("bytes")
                    if raw_bytes:
                        await self.upload_file(client, token, fname, raw_bytes, _guess_mime(fname), target_folder)

            logger.info(f"🚀 [GOOGLE DRIVE] Complete remote upload for #{submission_id} into {form_folder_name}/{submission_id} finished successfully.")
            return True

def append_to_local_sheets_mirror(record: Dict[str, Any]) -> None:
    """Appends submission record to the Master_Survey_Deliverables spreadsheet mirror."""
    if record.get("status") != "APPROVED":
        logger.debug(f"Skipping local sheets mirror for non-approved submission #{record.get('submission_id')} (status: {record.get('status')})")
        return

    file_exists = os.path.exists(MASTER_CSV_FILE) and os.path.getsize(MASTER_CSV_FILE) > 0
    if file_exists:
        try:
            with open(MASTER_CSV_FILE, "r", encoding="utf-8-sig") as f:
                reader = list(csv.reader(f))
            if reader and reader[0] != CSV_HEADERS:
                # Upgrade header and existing rows to match new CSV_HEADERS schema
                migrated_rows = [CSV_HEADERS]
                for old_row in reader[1:]:
                    if len(old_row) < len(CSV_HEADERS):
                        old_row = old_row + [""] * (len(CSV_HEADERS) - len(old_row))
                    elif len(old_row) > len(CSV_HEADERS):
                        old_row = old_row[:len(CSV_HEADERS)]
                    migrated_rows.append(old_row)
                with open(MASTER_CSV_FILE, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerows(migrated_rows)
        except Exception as mig_e:
            logger.debug(f"Header migration notice: {mig_e}")

    try:
        with open(MASTER_CSV_FILE, "a", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(CSV_HEADERS)
            writer.writerow(extract_survey_row_data(record))
        logger.info(f"📊 [DRIVE SHEETS MIRROR] Appended #{record.get('submission_id')} to {MASTER_CSV_FILE}")
    except Exception as e:
        logger.error(f"Failed to append to local sheets mirror: {e}")

def save_submission_images_locally(
    submission_id: str,
    images: Optional[List[Dict[str, Any]]],
    form_id: Optional[str] = None,
) -> Optional[str]:
    """
    Saves all photos for a submission into Images/<form_id>/<submission_id>/.
    Only saves to deliverables mirror if not a dev/test/mock submission and images is not empty!
    """
    if is_test_submission(submission_id) or not images:
        logger.debug(f"Skipping deliverables mirror save for dev/test or empty images submission #{submission_id}")
        return None
    form_folder = str(form_id or "unknown_form")
    subfolder_path = os.path.join(LOCAL_IMAGES_DIR, form_folder, submission_id)
    os.makedirs(subfolder_path, exist_ok=True)

    saved_paths = []
    for idx, img in enumerate(images):
        fname = img.get("filename") or f"slot_{idx}.webp"
        raw_bytes = img.get("bytes")
        if raw_bytes:
            out_file = os.path.join(subfolder_path, fname)
            with open(out_file, "wb") as f:
                f.write(raw_bytes)
            saved_paths.append(out_file)

    if not saved_paths and os.path.exists(subfolder_path):
        try:
            os.rmdir(subfolder_path)
        except OSError:
            pass
        return None

    logger.info(f"📁 [DRIVE IMAGES MIRROR] Saved {len(saved_paths)} images to {subfolder_path}")
    return subfolder_path


def rename_local_form_folder(old_form_id: str, new_form_id: str) -> None:
    """Mirror a form-id rename in the local Drive deliverable mirror."""
    old_dir = os.path.join(LOCAL_IMAGES_DIR, old_form_id)
    new_dir = os.path.join(LOCAL_IMAGES_DIR, new_form_id)
    if os.path.isdir(old_dir) and not os.path.exists(new_dir):
        os.makedirs(os.path.dirname(new_dir), exist_ok=True)
        shutil.move(old_dir, new_dir)
        logger.info(f"📁 [DRIVE IMAGES MIRROR] Renamed local folder {old_form_id} -> {new_form_id}")


async def rename_form_folder(old_form_id: str, new_form_id: str) -> bool:
    """Renames the Drive form folder when a form's id changes."""
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
        if resp.status_code == 200:
            logger.info(f"📁 [GOOGLE DRIVE] Renamed form folder {old_form_id} -> {new_form_id}")
            return True
        logger.error(f"Failed to rename Drive folder {old_form_id}: {resp.status_code} {resp.text[:200]}")
        return False


def has_google_credentials() -> bool:
    """Checks whether any Google Drive credentials (OAuth2 or Service Account) are configured."""
    return get_google_oauth_credentials() is not None or get_google_service_account_info() is not None


async def upload_to_google_drive(
    submission_id: str,
    record: Dict[str, Any],
    images: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """
    Orchestrates delivery to Google Drive & Google Sheets:
    1. ONLY status == 'APPROVED' submissions may be included in the Google Drive delivery!
       If record.get("status") != "APPROVED", immediately skip.
    2. Writes to the local structured deliverables mirror:
       - Images/{submission_id}/ (1 subfolder per submission containing all images)
       - Sheets/Master_Survey_Deliverables.csv (Spreadsheet containing all info fields)
    3. Individual surveys are NOT uploaded directly to Google Drive upon submission.
       Only the master data sheet and image folder are synced in batch (via sync_all_approved_to_drive / scripts/sync_to_drive.py).
    """
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
    logger.info(f"📁 [DRIVE DELIVERABLES MIRROR] #{submission_id} recorded in Sheets/Master_Survey_Deliverables.csv and Images/{submission_id} (individual surveys not uploaded directly; only data sheet and image folder are synced).")
    return True

async def sync_all_approved_to_drive(db_pool: Optional[Any] = None) -> bool:
    """
    Pushes the updated Master CSV and image folders for approved submissions to Google Drive.
    """
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

    # Find approved submission IDs
    approved_ids = set()
    submission_forms: Dict[str, str] = {}
    # 1. Dev storage
    dev_storage_file = os.path.join(os.getcwd(), "data", "dev_test_submissions.json")
    if os.path.exists(dev_storage_file):
        try:
            with open(dev_storage_file, "r", encoding="utf-8") as f:
                dev_records = json.load(f)
            for r in dev_records:
                if r.get("status") == "APPROVED" and r.get("submission_id"):
                    approved_ids.add(r.get("submission_id"))
                    submission_forms[r.get("submission_id")] = r.get("form_id") or "unknown_form"
        except Exception as e:
            logger.debug(f"Could not read dev storage for approved IDs: {e}")

    # 2. Database
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT submission_id, form_id FROM survey_submissions WHERE status = 'APPROVED';")
                    rows = await cur.fetchall()
                    for row in rows:
                        if row[0]:
                            approved_ids.add(row[0])
                            submission_forms[row[0]] = row[1] or "unknown_form"
        except Exception as e:
            logger.debug(f"Could not query DB for approved IDs: {e}")

    client = GoogleDriveClient()
    async with httpx.AsyncClient(timeout=45.0) as http_client:
        token = await client.get_token(http_client)
        if not token:
            logger.error("Failed to obtain Google Drive access token for periodic sync.")
            return False

        root_id = client.root_folder_id or None

        # Clean up any DEV-* or TEST-* folders from Google Drive
        # Clean up test folders, duplicate CSVs, and summary files from Google Drive
        try:
            await client.cleanup_drive_clutter(http_client, token)
        except Exception as e:
            logger.warning(f"Failed to cleanup clutter from Google Drive: {e}")
        # 1. Upload Master Sheet
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

        # 1b. Migrate legacy Images/<submission_id>/ dirs into Images/<form_id>/<submission_id>/
        try:
            for entry in os.listdir(LOCAL_IMAGES_DIR):
                entry_path = os.path.join(LOCAL_IMAGES_DIR, entry)
                if not os.path.isdir(entry_path):
                    continue
                child_dirs = [d for d in os.listdir(entry_path) if os.path.isdir(os.path.join(entry_path, d))]
                if child_dirs:
                    continue  # already a form folder
                form_id = submission_forms.get(entry)
                if not form_id:
                    continue
                dest_dir = os.path.join(LOCAL_IMAGES_DIR, form_id)
                os.makedirs(dest_dir, exist_ok=True)
                dest = os.path.join(dest_dir, entry)
                if not os.path.exists(dest):
                    shutil.move(entry_path, dest)
        except Exception as e:
            logger.warning(f"[AUTO DRIVE SYNC] Legacy image dir migration failed: {e}")

        # 2. Upload approved submissions under <root>/<form_id>/<submission_id>/
        uploaded_count = 0
        skipped_count = 0
        try:
            root_id = client.root_folder_id or None
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

            form_dirs = [p for p in glob.glob(os.path.join(LOCAL_IMAGES_DIR, "*")) if os.path.isdir(p)]
            for form_dir in form_dirs:
                form_folder_name = os.path.basename(form_dir)
                form_folder_id = await client.get_or_create_folder(
                    http_client, token, form_folder_name, root_id
                )
                if not form_folder_id:
                    continue
                remote_subfolders = await list_remote_folders(form_folder_id)

                for sub_dir in glob.glob(os.path.join(form_dir, "*")):
                    if not os.path.isdir(sub_dir):
                        continue
                    sub_id = os.path.basename(sub_dir)
                    if is_test_submission(sub_id):
                        continue
                    if approved_ids and sub_id not in approved_ids:
                        continue
                    files = [
                        f for f in glob.glob(os.path.join(sub_dir, "*.*"))
                        if os.path.isfile(f) and os.path.getsize(f) > 0
                    ]
                    if not files:
                        continue

                    if sub_id in remote_subfolders:
                        sub_folder_id = remote_subfolders[sub_id]
                        remote_names = await _list_remote_file_names(http_client, token, sub_folder_id)
                        skipped_count += 1
                    else:
                        sub_folder_id = await client.get_or_create_folder(
                            http_client, token, sub_id, form_folder_id
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
                        file_id = await client.upload_file(
                            http_client, token, fname, img_bytes, _guess_mime(fname), sub_folder_id
                        )
                        if file_id:
                            uploaded_count += 1
                        else:
                            logger.warning(f"[AUTO DRIVE SYNC] Upload failed for {form_folder_name}/{sub_id}/{fname}; will retry next sync.")

            logger.info(
                f"🖼️ [AUTO DRIVE SYNC] Synced {uploaded_count} new image files across "
                f"form folders under root {root_id}; {skipped_count} submission folders already existed."
            )
        except Exception as e:
            logger.error(f"[AUTO DRIVE SYNC] Failed to sync image subfolders: {e}")

        return True
