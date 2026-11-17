"""Deterministic layout and period recognition for driver income screens.

The AI models transcribe text and numbers; this module decides *what object type*
the image is and *which period* it covers, using pixels rather than words:

- the period tab row is located structurally, and the active tab is the teal
  pill (white text on teal) among the row's text cells;
- the selected tab's date range is parsed and its span drives DAILY / WEEKLY /
  MONTHLY classification;
- the hardcoded income field labels of the known layout are checked, so an
  unknown mockup is rejected by layout instead of by keyword mismatch.

Everything here is pure and synchronous so it can be unit tested without the
network. `analyse_income_layout` folds the model's transcription and this
deterministic pass together.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from services.image_types import TimeGranularity, parse_date_range_from_text

logger = logging.getLogger("services.income_layout")

# Field rows of the known Xanh SM / GSM weekly income layout, on-screen order.
INCOME_FIELD_LABELS: Tuple[str, ...] = (
    "Tổng thu nhập",
    "Doanh số",
    "Chiết khấu + thuế",
    "Thu nhập ròng",
    "Số giờ online",
    "Số giờ vận doanh",
    "Tổng số chuyến",
)

LAYOUT_XANH_SM_INCOME = "xanh_sm_income_v1"

# Teal selected-tab pill (Xanh SM cyan) in HSV.
_TEAL_LOW = np.array([78, 80, 60], dtype=np.uint8)
_TEAL_HIGH = np.array([105, 255, 255], dtype=np.uint8)


@dataclass
class TabRowDetection:
    index: Optional[int]
    cells: int
    box: Optional[Tuple[int, int, int, int]]
    coverage: float


@dataclass
class PeriodInfo:
    label: str = ""
    normalized: str = ""
    start: Optional[date] = None
    end: Optional[date] = None
    days: int = 0
    granularity: TimeGranularity = TimeGranularity.UNKNOWN


@dataclass
class IncomeLayoutResult:
    layout_id: str = LAYOUT_XANH_SM_INCOME
    matched: bool = False
    tabs: List[str] = field(default_factory=list)
    selected_index: Optional[int] = None
    period: PeriodInfo = field(default_factory=PeriodInfo)
    period_reliable: bool = True
    period_uncertain_reason: str = ""
    missing_labels: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", stripped.lower())


def _decode(image_bytes: bytes) -> Optional[np.ndarray]:
    if not image_bytes:
        return None
    frame = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    return frame if frame is not None and frame.size else None


def _band_segments(content: np.ndarray, gap: int) -> List[Tuple[int, int]]:
    columns = np.where(content.any(axis=0))[0]
    if len(columns) == 0:
        return []
    segments: List[Tuple[int, int]] = []
    start = prev = int(columns[0])
    for column in columns[1:]:
        column = int(column)
        if column - prev > gap:
            segments.append((start, prev))
            start = column
        prev = column
    segments.append((start, prev))
    return segments


def detect_tab_row(image_bytes: bytes) -> Optional[TabRowDetection]:
    """Find the period tab row and the index of its active (teal) tab."""
    frame = _decode(image_bytes)
    if frame is None:
        return None
    height, width = frame.shape[:2]
    band = frame[: max(1, int(height * 0.35))]
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    teal = cv2.inRange(hsv, _TEAL_LOW, _TEAL_HIGH)
    teal = cv2.morphologyEx(teal, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))

    count, _, stats, _ = cv2.connectedComponentsWithStats(teal, connectivity=8)
    candidates = []
    for label in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[label])
        if width * 0.05 <= w <= width * 0.6 and height * 0.012 <= h <= height * 0.08 and area >= band.size * 0.0004:
            candidates.append((x, y, w, h, area))
    if not candidates:
        return None
    # The screen has two teal pills: the selected month (upper) and the selected
    # week (lower). The week row is always below the month row, so take the lowest.
    best = max(candidates, key=lambda item: (item[1], item[4]))
    x, y, w, h, area = best
    top = max(0, y - int(h * 0.25))
    bottom = min(band.shape[0], y + h + int(h * 0.25))
    row = band[top:bottom]
    row_hsv = hsv[top:bottom]
    dark = cv2.inRange(row_hsv, np.array([0, 0, 0], np.uint8), np.array([179, 255, 120], np.uint8))
    content = cv2.bitwise_or(dark, cv2.inRange(row_hsv, _TEAL_LOW, _TEAL_HIGH))
    gap = max(8, int(width * 0.02))
    segments = [s for s in _band_segments(content, gap) if s[1] - s[0] >= max(6, int(width * 0.015))]
    center = x + w / 2.0
    index = None
    for position, (left, right) in enumerate(segments):
        if left <= center <= right:
            index = position
            break
    if index is None and segments:
        index = min(range(len(segments)), key=lambda i: abs((segments[i][0] + segments[i][1]) / 2.0 - center))

    return TabRowDetection(
        index=index,
        cells=len(segments),
        box=(x, y, w, h),
        coverage=round(area / float(band.shape[0] * band.shape[1]), 6),
    )


_TAB_DAY_RANGE = re.compile(r"(\d{1,2})\s*[-–~]\s*(\d{1,2})")
_MONTH_YEAR = re.compile(r"(\d{1,2})\s*/\s*(\d{4})")


def _month_context(month_label: str, reference_year: int) -> Tuple[int, int]:
    match = _MONTH_YEAR.search(month_label or "")
    if match:
        month, year = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return month, year
    return 0, reference_year


def parse_tab_period(label: str, month_label: str = "", reference_year: int = 2026) -> PeriodInfo:
    """Parse one tab label ("31 - 06", "28/09 - 04/10", "Tuần 4 (16 - 22)") into a range."""
    text = (label or "").strip()
    if not text:
        return PeriodInfo()
    start = end = None
    slash_start, slash_end, _ = parse_date_range_from_text(text, default_year=reference_year)
    if slash_start and slash_end:
        start, end = slash_start, slash_end
    else:
        day_only = _TAB_DAY_RANGE.search(text)
        month, year = _month_context(month_label, reference_year)
        if not day_only or not month:
            return PeriodInfo(label=text)
        d1, d2 = int(day_only.group(1)), int(day_only.group(2))
        prev_month, prev_year = (12, year - 1) if month == 1 else (month - 1, year)
        next_month, next_year = (1, year + 1) if month == 12 else (month + 1, year)
        candidates = []
        # The week ends in the selected month but started in the previous one
        # ("26 - 02" under 10/2026 is 26/09 - 02/10, not 26/10 - 02/11).
        try:
            candidates.append((date(prev_year, prev_month, d1), date(year, month, d2)))
        except ValueError:
            pass
        # The week starts in the selected month and ends there or in the next one
        # ("03 - 09" or "26 - 01" under 10/2026).
        try:
            end_in_month = date(year, month, d2) if d2 >= d1 else date(next_year, next_month, d2)
            candidates.append((date(year, month, d1), end_in_month))
        except ValueError:
            pass
        if not candidates:
            return PeriodInfo(label=text)
        # Weekly tabs span 7 days (only the trailing partial week is shorter); pick the
        # candidate closest to a week so a month anchor never silently adds a month.
        start, end = min(candidates, key=lambda span: abs((span[1] - span[0]).days + 1 - 7))
    if not start or not end:
        return PeriodInfo(label=text)
    days = (end - start).days + 1
    granularity = TimeGranularity.UNKNOWN
    if days == 1:
        granularity = TimeGranularity.DAILY
    elif 2 <= days <= 24:
        # partial survey weeks (e.g. the 3-day fifth week) are still weekly reports
        granularity = TimeGranularity.WEEKLY
    elif days >= 25:
        granularity = TimeGranularity.MONTHLY
    return PeriodInfo(
        label=text,
        normalized=f"{start.strftime('%d/%m')} - {end.strftime('%d/%m/%Y')}",
        start=start,
        end=end,
        days=days,
        granularity=granularity,
    )


_DETAIL_DATE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{2,4}))?")


def _parse_detail_date(value: str, reference_year: int) -> Optional[date]:
    """Parse the first 'Chi tiết' date (e.g. 'Thứ bảy, 17/10/2026') into a date."""
    match = _DETAIL_DATE.search(value or "")
    if not match:
        return None
    day, month = int(match.group(1)), int(match.group(2))
    year = int(match.group(3)) if match.group(3) else reference_year
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def analyse_income_layout(
    image_bytes: bytes,
    model_data: Optional[Dict[str, Any]],
    reference_year: int = 2026,
) -> IncomeLayoutResult:
    """Combine the model's transcription with deterministic layout/period checks."""
    data = model_data or {}
    result = IncomeLayoutResult()

    tabs = [str(item).strip() for item in (data.get("period_tabs") or []) if str(item).strip()]
    detection = detect_tab_row(image_bytes)
    model_index = data.get("selected_period_index")
    pill_index = detection.index if detection is not None else None
    index: Optional[int] = None
    if isinstance(model_index, (int, float)) and tabs and 0 <= int(model_index) < len(tabs):
        index = int(model_index)
        result.notes.append(f"active tab from model list: {index + 1}/{len(tabs)}")
        if pill_index is not None and pill_index != index:
            result.notes.append(f"pill position {pill_index + 1}/{detection.cells} differs from model index")
    elif pill_index is not None and tabs and 0 <= pill_index < len(tabs):
        index = pill_index
        result.notes.append(f"active tab from teal pill: {index + 1}/{len(tabs)}")

    selected = str(data.get("selected_period_label") or "").strip()
    if not selected and index is not None and index < len(tabs):
        selected = tabs[index]
    if not selected:
        selected = str(data.get("period") or "")
    month_label = str(data.get("period_month") or "")
    if not month_label:
        month_match = _MONTH_YEAR.search(str(data.get("period") or ""))
        if month_match:
            month_label = month_match.group(0)
    period = parse_tab_period(selected, month_label, reference_year)

    # Never rewrite the period from a guess. Instead, surface every disagreement so
    # the caller can ask the driver to re-shoot the screen with all week tabs visible.
    period_reliable = data.get("period_reliable") is not False
    uncertain_reason = str(data.get("period_uncertain_reason") or "").strip()
    if not period_reliable and not uncertain_reason:
        uncertain_reason = "model reported the active week tab as unreadable or ambiguous"
    detail_date = _parse_detail_date(str(data.get("first_detail_date") or ""), reference_year)
    if (
        period_reliable
        and detail_date
        and period.start
        and period.end
        and not (period.start <= detail_date <= period.end)
    ):
        period_reliable = False
        conflict = (
            f"ngày Chi tiết {detail_date.strftime('%d/%m/%Y')} không nằm trong kỳ đã đọc "
            f"({period.normalized or period.label or 'không rõ'})"
        )
        uncertain_reason = f"{uncertain_reason}; {conflict}" if uncertain_reason else conflict
    if not period_reliable:
        result.notes.append(f"period unreliable: {uncertain_reason}")

    labels = [_normalize(str(item)) for item in (data.get("field_labels") or [])]
    joined = "".join(labels)
    missing = [label for label in INCOME_FIELD_LABELS if _normalize(label) not in joined]

    layout_matched = detection is not None and len(tabs) >= 3 and len(missing) <= 1
    if detection is None:
        result.notes.append("no teal period pill found")
    if len(tabs) < 3:
        result.notes.append(f"only {len(tabs)} period tabs transcribed")
    if missing:
        result.notes.append(f"missing layout labels: {', '.join(missing)}")

    result.matched = layout_matched
    result.tabs = tabs
    result.selected_index = index
    result.period = period
    result.period_reliable = period_reliable
    result.period_uncertain_reason = uncertain_reason
    result.missing_labels = missing
    return result


def _coerce_amount(value: Any) -> int:
    """Turn '13.461.500đ', '1,234,567', '-10.332.423' into ints; 0 when unreadable."""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    digits = re.sub(r"[^0-9]", "", text)
    if not digits:
        return 0
    amount = int(digits)
    return -amount if text.lstrip()[:1] in ("-", "−", "–") else amount


def apply_layout_to_ocr(model_data: Optional[Dict[str, Any]], image_bytes: bytes, reference_year: int = 2026) -> Dict[str, Any]:
    """Return the model's dict with deterministic period + app/layout fields applied."""
    data = dict(model_data or {})
    for key in ("gross_revenue", "net_income", "discount_and_tax", "total_trips"):
        if key in data:
            data[key] = _coerce_amount(data.get(key))
    try:
        layout = analyse_income_layout(image_bytes, data, reference_year)
    except Exception as exc:  # never fail the OCR call because of a layout check
        logger.warning(f"[INCOME LAYOUT] analysis failed: {exc}")
        return data

    data["layout_id"] = layout.layout_id
    data["layout_matched"] = layout.matched
    data["period_tabs"] = layout.tabs
    data["active_tab_index"] = layout.selected_index
    data["period_raw_text"] = layout.period.label
    data["period_days"] = layout.period.days
    if layout.period.granularity != TimeGranularity.UNKNOWN:
        data["period_granularity"] = layout.period.granularity.value
        data["active_tab"] = layout.period.granularity.value
    if layout.period.normalized:
        data["period"] = layout.period.normalized
    elif layout.period.label:
        data["period"] = layout.period.label
    data["period_reliable"] = layout.period_reliable
    data["period_uncertain_reason"] = layout.period_uncertain_reason

    detected_app = str(data.get("detected_app") or "").upper()
    if layout.matched:
        data["detected_app"] = "XANH_SM"
        data["is_authentic_gsm_ui"] = True
    elif detected_app in ("", "XANH_SM"):
        # claimed Xanh SM but the known layout is not present -> reject on layout
        data["detected_app"] = "OTHER_MOCKUP"
        data["is_authentic_gsm_ui"] = False
    data["layout_notes"] = layout.notes
    return data
