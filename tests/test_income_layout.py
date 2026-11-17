# -*- coding: utf-8 -*-
"""Tests for deterministic income-screen layout and period recognition."""

from datetime import date

from PIL import Image, ImageDraw

from services.image_types import TimeGranularity
from services.income_layout import (
    INCOME_FIELD_LABELS,
    analyse_income_layout,
    apply_layout_to_ocr,
    detect_tab_row,
    parse_tab_period,
)


def _screen(selected_index: int = 2, tabs: int = 5, width: int = 500, height: int = 400) -> Image.Image:
    """White screen with a period tab row; the selected tab is a teal pill."""
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    cell_w, gap, x0, y0, h = 70, 20, 20, 60, 28
    for index in range(tabs):
        x = x0 + index * (cell_w + gap)
        if index == selected_index:
            draw.rounded_rectangle([x, y0, x + cell_w, y0 + h], radius=6, fill=(0, 176, 176))
            draw.rectangle([x + 20, y0 + 10, x + cell_w - 20, y0 + h - 10], fill="white")
        else:
            draw.rectangle([x + 20, y0 + 10, x + cell_w - 20, y0 + h - 10], fill=(30, 30, 30))
    return image


def _png(image: Image.Image) -> bytes:
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_parse_tab_period_classifies_day_week_month():
    week = parse_tab_period("31 - 06", "09/2026")
    assert (week.start, week.end, week.days) == (date(2026, 8, 31), date(2026, 9, 6), 7)
    assert week.granularity == TimeGranularity.WEEKLY

    wrap = parse_tab_period("26 - 01", "10/2026")
    assert (wrap.start, wrap.end, wrap.days) == (date(2026, 10, 26), date(2026, 11, 1), 7)
    assert wrap.granularity == TimeGranularity.WEEKLY

    # First week tab of a month starts in the PREVIOUS month ("26 - 02" under 10/2026
    # is 26/09-02/10, as its "Chi tiết" list shows; "26 - 01" above still rolls forward).
    first_week = parse_tab_period("26 - 02", "10/2026")
    assert (first_week.start, first_week.end, first_week.days) == (date(2026, 9, 26), date(2026, 10, 2), 7)
    assert first_week.normalized == "26/09 - 02/10/2026"

    month = parse_tab_period("01 - 31", "10/2026")
    assert month.days == 31 and month.granularity == TimeGranularity.MONTHLY

    day = parse_tab_period("15/10/2026", "10/2026")
    assert day.days == 1 and day.granularity == TimeGranularity.DAILY

    slash = parse_tab_period("28/09 - 04/10", "10/2026")
    assert slash.days == 7 and slash.granularity == TimeGranularity.WEEKLY

    assert parse_tab_period("Tổng", "09/2026").granularity == TimeGranularity.UNKNOWN


def test_detect_tab_row_finds_teal_pill():
    detection = detect_tab_row(_png(_screen(selected_index=2)))
    assert detection is not None
    assert detection.index == 2
    assert detection.cells == 5
    assert detect_tab_row(_png(Image.new("RGB", (500, 400), "white"))) is None


def test_analyse_income_layout_uses_model_tab_index():
    data = {
        "period_tabs": ["31 - 06", "07 - 13", "14 - 20", "21 - 27", "28 - 03"],
        "selected_period_index": 0,
        "period_month": "09/2026",
        "field_labels": list(INCOME_FIELD_LABELS),
        "detected_app": "XANH_SM",
    }
    layout = analyse_income_layout(_png(_screen(selected_index=2)), data)
    assert layout.selected_index == 0
    assert layout.matched is True
    assert layout.period.days == 7
    assert layout.period.granularity == TimeGranularity.WEEKLY
    assert layout.missing_labels == []

    applied = apply_layout_to_ocr(data, _png(_screen(selected_index=2)))
    assert applied["period"] == "31/08 - 06/09/2026"
    assert applied["period_days"] == 7
    assert applied["active_tab"] == "WEEKLY"
    assert applied["detected_app"] == "XANH_SM"
    assert applied["is_authentic_gsm_ui"] is True

    partial = parse_tab_period("23 - 25", "09/2026")
    assert partial.days == 3 and partial.granularity == TimeGranularity.WEEKLY


def test_analyse_income_layout_flags_monthly_and_missing_fields():
    data = {
        "period_tabs": ["01 - 31", "01 - 07", "08 - 14"],
        "period_month": "10/2026",
        "field_labels": ["Tổng thu nhập", "Doanh số"],
        "detected_app": "XANH_SM",
    }
    layout = analyse_income_layout(_png(_screen(selected_index=0, tabs=3)), data)
    assert layout.period.days == 31
    assert layout.period.granularity == TimeGranularity.MONTHLY
    assert layout.matched is False
    assert len(layout.missing_labels) >= 4

    applied = apply_layout_to_ocr(data, _png(_screen(selected_index=0, tabs=3)))
    assert applied["active_tab"] == "MONTHLY"
    assert applied["detected_app"] == "OTHER_MOCKUP"
    assert applied["is_authentic_gsm_ui"] is False


def test_unknown_layout_rejects_keyword_claimed_app():
    data = {"detected_app": "XANH_SM", "period": "09/2026", "field_labels": []}
    applied = apply_layout_to_ocr(data, _png(Image.new("RGB", (500, 400), "white")))
    assert applied["layout_matched"] is False
    assert applied["detected_app"] == "OTHER_MOCKUP"
    assert applied["is_authentic_gsm_ui"] is False


def test_apply_layout_coerces_currency_strings():
    from services.income_layout import _coerce_amount

    assert _coerce_amount("13.461.500đ") == 13461500
    assert _coerce_amount("1,234,567") == 1234567
    assert _coerce_amount("-10.332.423") == -10332423
    assert _coerce_amount(3226077) == 3226077
    assert _coerce_amount(None) == 0
    assert _coerce_amount("") == 0
    assert _coerce_amount("unreadable") == 0


def test_selected_period_label_wins_over_tab_list():
    data = {
        "period_tabs": ["Tuần 1", "Tuần 2", "Tuần 3"],
        "selected_period_label": "16 - 22",
        "period_month": "9/2026",
        "field_labels": list(INCOME_FIELD_LABELS),
    }
    layout = analyse_income_layout(_png(_screen(selected_index=2)), data)
    assert layout.period.days == 7
    assert layout.period.granularity == TimeGranularity.WEEKLY
    assert layout.period.start.isoformat() == "2026-09-16"


def test_validator_rejects_monthly_and_daily_but_accepts_weekly():
    from services.image_types import PeriodHeaderMetadata, TimeGranularity, validate_extracted_weekly_period

    def header_for(image, data):
        applied = apply_layout_to_ocr(data, image)
        return applied, PeriodHeaderMetadata(
            detected_app=applied.get("detected_app", "XANH_SM"),
            active_tab=TimeGranularity(applied["active_tab"]),
            period_raw_text=applied["period"],
        )

    monthly_image = _png(_screen(selected_index=0, tabs=3))
    monthly, monthly_hdr = header_for(monthly_image, {
        "period_tabs": ["01 - 31", "01 - 07", "08 - 14"],
        "selected_period_label": "01 - 31",
        "period_month": "10/2026",
        "field_labels": list(INCOME_FIELD_LABELS),
    })
    assert monthly["period_days"] == 31
    ok, err = validate_extracted_weekly_period(monthly_hdr, expected_week_num=1)
    assert ok is False and "THÁNG" in err

    daily, daily_hdr = header_for(monthly_image, {
        "period_tabs": ["15/10/2026", "01 - 07"],
        "selected_period_label": "15/10/2026",
        "period_month": "10/2026",
        "field_labels": list(INCOME_FIELD_LABELS),
    })
    assert daily["period_days"] == 1
    ok, err = validate_extracted_weekly_period(daily_hdr, expected_week_num=1)
    assert ok is False and "NGÀY" in err

    weekly, weekly_hdr = header_for(monthly_image, {
        "period_tabs": ["28 - 03", "05 - 11"],
        "selected_period_label": "05 - 11",
        "period_month": "10/2026",
        "field_labels": list(INCOME_FIELD_LABELS),
    })
    assert weekly["period_days"] == 7 and weekly["active_tab"] == "WEEKLY"
    ok, err = validate_extracted_weekly_period(weekly_hdr, expected_week_num=2)
    assert ok is True, err


def test_clipped_week_tab_is_marked_unreliable_and_never_corrected():
    """Regression: VN-F1821C38AB8611BC slot_6 (17/10 Chi tiết, tab OCR'd as 10 - 16).

    The pill is clipped at the screen edge. The layout must keep the OCR period,
    flag the week as unreliable, and never rewrite it from the Chi tiết date.
    """
    from routers.survey import _unreliable_week_reason

    data = {
        "period_tabs": ["26 - 02", "03 - 09", "10 - 16", "17 -"],
        "selected_period_label": "10 - 16",
        "selected_period_index": 2,
        "period_month": "10/2026",
        "first_detail_date": "Thứ bảy, 17/10/2026",
        "field_labels": list(INCOME_FIELD_LABELS),
    }
    applied = apply_layout_to_ocr(data, _png(_screen(selected_index=2)))
    # No guessing: the OCR period is preserved, only the reliability flag changes.
    assert applied["period"] == "10/10 - 16/10/2026"
    assert applied["period_reliable"] is False
    assert "17/10/2026" in applied["period_uncertain_reason"]
    assert not any("corrected" in note for note in applied["layout_notes"])

    reason = _unreliable_week_reason(applied, week_num=4, image_num=7)
    assert reason.startswith("Ảnh Tuần 4 (Ảnh 7): Không đọc được chính xác tuần đang chọn")
    assert "Vui lòng chụp lại màn hình hiển thị đủ các tab tuần." in reason


def test_model_flagged_unreliable_week_is_propagated():
    from routers.survey import _unreliable_week_reason

    data = {
        "period_tabs": ["26 - 02", "03 - 09", "10 - 16", "17 -"],
        "selected_period_label": "10 - 16",
        "selected_period_index": 2,
        "period_month": "10/2026",
        "period_reliable": False,
        "period_uncertain_reason": "Selected week tab is clipped at the right edge of the screen",
        "field_labels": list(INCOME_FIELD_LABELS),
    }
    applied = apply_layout_to_ocr(data, _png(_screen(selected_index=2)))
    assert applied["period_reliable"] is False
    assert "clipped" in applied["period_uncertain_reason"]
    reason = _unreliable_week_reason(applied, week_num=4)
    assert reason.startswith("Ảnh Tuần 4: Không đọc được chính xác tuần đang chọn")


def test_agreeing_detail_date_keeps_week_reliable():
    data = {
        "period_tabs": ["26 - 02", "03 - 09", "10 - 16", "17 - 23"],
        "selected_period_label": "10 - 16",
        "selected_period_index": 2,
        "period_month": "10/2026",
        "first_detail_date": "10/10/2026",
        "field_labels": list(INCOME_FIELD_LABELS),
    }
    applied = apply_layout_to_ocr(data, _png(_screen(selected_index=2)))
    assert applied["period"] == "10/10 - 16/10/2026"
    assert applied["period_reliable"] is True
    assert applied["period_uncertain_reason"] == ""


def test_conflicting_detail_date_is_unreliable_even_if_far_off():
    data = {
        "period_tabs": ["26 - 02", "03 - 09", "10 - 16", "17 - 23"],
        "selected_period_label": "10 - 16",
        "selected_period_index": 2,
        "period_month": "10/2026",
        "first_detail_date": "17/12/2026",
        "field_labels": list(INCOME_FIELD_LABELS),
    }
    applied = apply_layout_to_ocr(data, _png(_screen(selected_index=2)))
    assert applied["period"] == "10/10 - 16/10/2026"
    assert applied["period_reliable"] is False


