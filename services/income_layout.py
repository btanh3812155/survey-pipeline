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

import calendar
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
        days_in_month = calendar.monthrange(year, month)[1]
        try:
            if d1 > days_in_month:
                # the week starts in the previous month, e.g. "31 - 06" under 09/2026
                prev_month, prev_year = (12, year - 1) if month == 1 else (month - 1, year)
                start = date(prev_year, prev_month, d1)
                end = date(year, month, d2)
            else:
                start = date(year, month, d1)
                if d2 >= d1:
                    end = date(year, month, d2)
                else:
                    next_month, next_year = (1, year + 1) if month == 12 else (month + 1, year)
                    end = date(next_year, next_month, d2)
        except ValueError:
            return PeriodInfo(label=text)
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


def _correct_period_from_detail_date(
    detail_date: date,
    tabs: Sequence[str],
    month_label: str,
    reference_year: int,
) -> Tuple[Optional[PeriodInfo], Optional[int]]:
    """Restate the selected period using the selected week's first daily breakdown date.

    The vision model occasionally anchors on the dark date text of a neighbouring
    (unselected) tab, reporting the previous week. The 'Chi tiết' list always starts
    on the selected week's first day, so it is the reliable anchor.
    """
    for tab_index, tab in enumerate(tabs):
        candidate = parse_tab_period(tab, month_label, reference_year)
        if candidate.start and candidate.end and candidate.start <= detail_date <= candidate.end:
            return candidate, tab_index
    week_end = detail_date + timedelta(days=6)
    fallback = parse_tab_period(
        f"{detail_date.strftime('%d/%m')} - {week_end.strftime('%d/%m')}",
        month_label,
        reference_year,
    )
    # A tab clipped at the screen edge ("17 -") cannot be parsed as a range, but its
    # leading day still identifies which tab is active.
    for tab_index, tab in enumerate(tabs):
        day_match = re.match(r"\s*(\d{1,2})", tab)
        if day_match and int(day_match.group(1)) == detail_date.day:
            return fallback, tab_index
    return fallback, None


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

    # Cross-check the tab read against the selected week's first 'Chi tiết' date.
    # Only correct an adjacent-week misread (<= 8 days off) so a hallucinated or
    # unrelated date can never rewrite the period.
    detail_date = _parse_detail_date(str(data.get("first_detail_date") or ""), reference_year)
    if (
        detail_date
        and period.start
        and period.end
        and not (period.start <= detail_date <= period.end)
        and abs((detail_date - period.start).days) <= 8
    ):
        corrected, corrected_index = _correct_period_from_detail_date(
            detail_date, tabs, month_label, reference_year
        )
        if corrected and corrected.start and corrected.end and corrected.start <= detail_date <= corrected.end:
            result.notes.append(
                f"period corrected via Chi tiết date {detail_date.strftime('%d/%m/%Y')}: "
                f"{period.label or period.normalized} -> {corrected.normalized}"
            )
            period = corrected
            if corrected_index is not None:
                index = corrected_index

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
