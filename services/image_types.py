# -*- coding: utf-8 -*-
"""
Standardized Typed Image Schemas & Deterministic Temporal Math Engine.
Decouples visual transcription from semantic validation:
- AI acts as an isolated, focused optical transcriber (headers/tabs vs financial numbers).
- Python enforces exact calendar math, cross-month date intervals, and slot-to-week alignment.
"""

import re
import logging
from enum import Enum
from datetime import date
from typing import Optional, Dict, Any, Tuple, List
from pydantic import BaseModel, Field

logger = logging.getLogger("services.image_types")


class TimeGranularity(str, Enum):
    DAILY = "DAILY"        # 1-day statement (e.g. "Hôm nay", "15/10/2026")
    WEEKLY = "WEEKLY"      # 7-day statement (e.g. "28/09 - 04/10")
    MONTHLY = "MONTHLY"    # Full month statement (e.g. "Tháng 10/2026", "01/10 - 31/10")
    UNKNOWN = "UNKNOWN"


class PeriodHeaderMetadata(BaseModel):
    """Temporal and header metadata extracted from app top navigation chrome."""
    detected_app: str = Field(default="XANH_SM", description="'XANH_SM', 'GRAB', 'BE', 'OTHER'")
    active_tab: TimeGranularity = Field(default=TimeGranularity.WEEKLY, description="Tab active at top: DAILY, WEEKLY, or MONTHLY")
    period_raw_text: str = Field(default="", description="Verbatim period text e.g. '28/09 - 04/10'")
    has_range_separator: bool = Field(default=True, description="True if text contains date range separator like '-' or '–'")


class WeeklyIncomeNumbers(BaseModel):
    """Raw financial and operational digits extracted from income statement."""
    gross_revenue: int = 0
    net_income: int = 0
    discount_and_tax: int = 0
    total_trips: int = 0
    online_hours: str = ""
    confidence_score: float = 1.0
    is_legible: bool = True
    is_manipulated_or_photoshopped: bool = False
    is_authentic_gsm_ui: bool = True


class WeeklyIncomeScreenExtraction(BaseModel):
    """Combined standardized contract for Weekly Income Screen images."""
    header: PeriodHeaderMetadata
    numbers: WeeklyIncomeNumbers


class ProfileScreenExtraction(BaseModel):
    """
    Standardized contract for the GSM / Xanh SM driver app profile screen.

    Visual object: the driver app home/profile screen showing the wallet and
    rewards shortcuts, the membership card (e.g. "Tài xế hạng Vàng", period
    "10/2026"), the utility menu (support map, safety check, my jobs, income
    stats, performance, shifts, debt), and the app version footer.
    """
    # Every descriptive field is optional: labels/cards may be absent from the
    # captured screen (collapsed menus, older app builds, scrolled views).
    # None means "not visible", never a fabricated default.
    app: Optional[str] = None
    membership_type: Optional[str] = None
    membership_period: Optional[str] = None
    driver_rank: Optional[str] = None
    partner_id: Optional[str] = None
    wallet_visible: Optional[bool] = None
    rewards_visible: Optional[bool] = None
    trip_history_visible: Optional[bool] = None
    utility_items: List[str] = Field(default_factory=list)
    app_version: Optional[str] = None
    is_authentic: Optional[bool] = None


class VehicleExteriorExtraction(BaseModel):
    """Standardized contract for Physical Vehicle & Cockpit verification photos."""
    vehicle_type: str = "CAR"
    view: str = "EXTERIOR"  # EXTERIOR, COCKPIT, STEERING_WHEEL, ODOMETER
    license_plate: Optional[str] = None
    is_authentic: bool = True


# ==============================================================================
# Calendar Target Windows (October 2026 Survey Benchmark)
# ==============================================================================

OCTOBER_2026_WEEKS: Dict[int, Dict[str, Any]] = {
    1: {"name": "Tuần 1", "start": date(2026, 9, 28), "end": date(2026, 10, 4), "label": "28/09 - 04/10"},
    2: {"name": "Tuần 2", "start": date(2026, 10, 5), "end": date(2026, 10, 11), "label": "05/10 - 11/10"},
    3: {"name": "Tuần 3", "start": date(2026, 10, 12), "end": date(2026, 10, 18), "label": "12/10 - 18/10"},
    4: {"name": "Tuần 4", "start": date(2026, 10, 19), "end": date(2026, 10, 25), "label": "19/10 - 25/10"},
    5: {"name": "Tuần 5", "start": date(2026, 10, 26), "end": date(2026, 11, 1), "label": "26/10 - 01/11"},
}


# ==============================================================================
# Deterministic Python Temporal Validator
# ==============================================================================

def parse_date_range_from_text(raw_text: str, default_year: int = 2026) -> Tuple[Optional[date], Optional[date], int]:
    """
    Parses start and end dates from Vietnamese ride-hailing date headers.
    Handles cross-month boundary transitions (e.g. '28/09 - 04/10', '26/10 - 01/11').
    Returns (start_date, end_date, duration_days).
    """
    text = (raw_text or "").strip().lower()

    # Pattern: DD/MM - DD/MM or DD/MM/YYYY - DD/MM/YYYY
    m = re.search(
        r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\s*[-–~đến]+\s*(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?",
        text,
    )
    if not m:
        # Check single date without separator
        m_single = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", text)
        if m_single and not any(sep in text for sep in ["-", "–", "đến", "to"]):
            d = int(m_single.group(1))
            mo = int(m_single.group(2))
            yr = int(m_single.group(3) or default_year)
            if yr < 100:
                yr += 2000
            try:
                dt = date(yr, mo, d)
                return dt, dt, 1
            except ValueError:
                pass
        return None, None, 0

    d1, mo1 = int(m.group(1)), int(m.group(2))
    y1 = int(m.group(3) or default_year)
    d2, mo2 = int(m.group(4)), int(m.group(5))
    y2 = int(m.group(6) or (y1 if mo2 >= mo1 else y1 + 1))

    if y1 < 100:
        y1 += 2000
    if y2 < 100:
        y2 += 2000

    try:
        dt1 = date(y1, mo1, d1)
        dt2 = date(y2, mo2, d2)
        delta_days = (dt2 - dt1).days + 1
        return dt1, dt2, delta_days
    except ValueError:
        return None, None, 0


def validate_extracted_weekly_period(
    header: PeriodHeaderMetadata,
    expected_week_num: int = 1,
) -> Tuple[bool, str]:
    """
    Deterministic rule engine validating that the extracted period strictly matches
    the expected weekly slot, rejecting daily statements, monthly summaries, and swapped weeks.
    """
    # 1. Reject explicit monthly tab or text
    raw_str = (header.period_raw_text or "").strip().lower()
    active_tab = header.active_tab
    if active_tab == TimeGranularity.MONTHLY or "tháng" in raw_str or "toàn bộ tháng" in raw_str:
        return False, (
            f"Ảnh Tuần {expected_week_num}: Phát hiện ảnh chụp báo cáo doanh thu theo THÁNG. "
            f"Biểu mẫu yêu cầu nộp báo cáo thu nhập chi tiết theo TUẦN (7 ngày)!"
        )

    # 2. Reject explicit daily tab
    if active_tab == TimeGranularity.DAILY:
        return False, (
            f"Ảnh Tuần {expected_week_num}: Phát hiện tab 'Ngày' (báo cáo thu nhập theo NGÀY). "
            f"Vui lòng chuyển sang tab 'Tuần' trên ứng dụng để chụp báo cáo 7 ngày!"
        )

    # 3. Parse date range
    dt_start, dt_end, duration_days = parse_date_range_from_text(header.period_raw_text)

    if dt_start and dt_end:
        # Check single-day duration
        if duration_days == 1:
            return False, (
                f"Ảnh Tuần {expected_week_num}: Đây là báo cáo thu nhập của 1 ngày ({dt_start.strftime('%d/%m')}). "
                f"Biểu mẫu yêu cầu báo cáo thu nhập tổng hợp theo TUẦN (7 ngày)!"
            )

        # Check full-month duration (>= 25 days)
        if duration_days >= 25:
            return False, (
                f"Ảnh Tuần {expected_week_num}: Khoảng thời gian ghi nhận ({duration_days} ngày) là báo cáo cả THÁNG. "
                f"Biểu mẫu yêu cầu chụp riêng từng tuần!"
            )

        # Check expected calendar week window
        target = OCTOBER_2026_WEEKS.get(expected_week_num)
        if target:
            target_start = target["start"]
            target_end = target["end"]

            # If start date does NOT match target window, check if it matches a different week (swapped week!)
            if abs((dt_start - target_start).days) > 2:
                for other_w, other_cfg in OCTOBER_2026_WEEKS.items():
                    if other_w != expected_week_num:
                        if abs((dt_start - other_cfg["start"]).days) <= 2:
                            return False, (
                                f"Ảnh Tuần {expected_week_num}: Bạn đã nộp nhầm ảnh của {other_cfg['name']} ({other_cfg['label']}) "
                                f"vào ô {target['name']} ({target['label']})! "
                                f"Vui lòng chọn đúng ảnh của {target['name']}."
                            )

                # Out of survey scope
                if dt_start < date(2026, 9, 20) or dt_start > date(2026, 11, 10):
                    return False, (
                        f"Ảnh Tuần {expected_week_num}: Kỳ báo cáo ({dt_start.strftime('%d/%m')} - {dt_end.strftime('%d/%m/%Y')}) "
                        f"nằm ngoài khung thời gian Tháng 10/2026 của đợt khảo sát!"
                    )

    # If text is present but no dash and contains 'hôm nay'
    if ("hôm nay" in raw_str or "ngày" in raw_str) and not any(s in raw_str for s in ["-", "–", "đến"]):
        return False, (
            f"Ảnh Tuần {expected_week_num}: Phát hiện báo cáo theo NGÀY. "
            f"Biểu mẫu yêu cầu báo cáo thu nhập theo TUẦN!"
        )

    return True, ""
