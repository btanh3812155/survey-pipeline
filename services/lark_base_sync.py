import os
import re
import json
import logging
import asyncio
from typing import Optional, Union, Dict, Any
from datetime import datetime, timezone
import httpx

from config import settings

logger = logging.getLogger("lark_base_sync")

# Default Base & Table owned by the custom app
LARK_BASE_APP_TOKEN = getattr(settings, "LARK_BASE_APP_TOKEN", "UwVabU53laZe5OsKgEGjy7QRpOh")
LARK_BASE_TABLE_ID = getattr(settings, "LARK_BASE_TABLE_ID", "tblTpiVhUtY98Zc8")

_cached_token: Optional[str] = None
_token_expires_at: float = 0.0


async def get_lark_tenant_access_token() -> Optional[str]:
    """Retrieves and caches Lark tenant access token."""
    global _cached_token, _token_expires_at
    now = asyncio.get_event_loop().time()
    if _cached_token and now < _token_expires_at - 300:
        return _cached_token

    app_id = settings.LARK_APP_ID
    app_secret = settings.LARK_APP_SECRET
    if not app_id or not app_secret or app_id == "cli_mock_id":
        return None

    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={"app_id": app_id, "app_secret": app_secret},
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            data = resp.json()
            if data.get("code") == 0:
                _cached_token = data.get("tenant_access_token")
                expires_in = data.get("expire", 7200)
                _token_expires_at = now + expires_in
                return _cached_token
            else:
                logger.error(f"Failed to fetch Lark tenant access token: {data}")
                return None
    except Exception as e:
        logger.error(f"Lark token network error: {e}")
        return None


async def sync_submission_to_lark_base(
    submission_id: str,
    status_str: str,
    driver_phone: Optional[str] = None,
    driver_name: Optional[str] = None,
    license_plate: Optional[str] = None,
    gross_revenue: Optional[Union[str, int]] = None,
    reason_str: Optional[str] = None,
    submitted_at_iso: Optional[str] = None,
    app_token: Optional[str] = None,
    table_id: Optional[str] = None,
) -> bool:
    """
    Writes a survey submission record directly into the Lark Base table.
    """
    token = await get_lark_tenant_access_token()
    if not token:
        logger.warning(f"[LARK BASE SYNC] Skipped for #{submission_id}: Lark credentials not active.")
        return False

    target_app = app_token or LARK_BASE_APP_TOKEN
    target_table = table_id or LARK_BASE_TABLE_ID

    num_revenue = 0
    if gross_revenue and str(gross_revenue).strip() not in ("0", ""):
        try:
            num_revenue = int(re.sub(r"[^\d]", "", str(gross_revenue)))
        except Exception:
            num_revenue = 0

    time_str = submitted_at_iso or datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S UTC")

    record_fields: Dict[str, Any] = {
        "Mã Phiếu (Submission ID)": submission_id,
        "Trạng Thái (Status)": status_str,
        "Số Điện Thoại (Driver Phone)": driver_phone or "N/A",
        "Họ Tên (Driver Name)": driver_name or "Tài xế",
        "Biển Số Xe (License Plate)": license_plate or "N/A",
        "Doanh Thu (Gross Revenue)": num_revenue,
        "Lý Do / Ghi Chú (Reason / Note)": reason_str or "Hợp lệ",
        "Thời Gian Nộp (Submitted At)": time_str,
    }

    url = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{target_app}/tables/{target_table}/records"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                json={"fields": record_fields},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
            data = resp.json()
            if data.get("code") == 0:
                rec_id = data.get("data", {}).get("record", {}).get("record_id")
                logger.info(f"✅ [LARK BASE SYNC] Synced #{submission_id} -> Record ID: {rec_id}")
                return True
            else:
                logger.error(f"❌ [LARK BASE SYNC] Error for #{submission_id}: {data}")
                return False
    except Exception as e:
        logger.error(f"❌ [LARK BASE SYNC] Connection failed for #{submission_id}: {e}")
        return False
