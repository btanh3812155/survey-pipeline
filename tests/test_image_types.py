# -*- coding: utf-8 -*-
"""Typed image object contracts (services/image_types.py)."""

from services.image_types import ProfileScreenExtraction


def test_profile_screen_object_type_parses_gsm_profile_screen():
    """The GSM driver profile screen maps to its standardized extraction contract."""
    extracted = ProfileScreenExtraction.model_validate({
        "app": "XANH_SM",
        "membership_type": "Vàng",
        "membership_period": "10/2026",
        "driver_rank": "Gold",
        "partner_id": None,
        "wallet_visible": True,
        "rewards_visible": True,
        "trip_history_visible": True,
        "utility_items": [
            "Bản đồ hỗ trợ", "Kiểm tra an toàn vận doanh", "Công việc của tôi",
            "Thống kê thu nhập", "Hiệu suất hoạt động", "Ca làm việc", "Số công nợ",
        ],
        "app_version": "3.17.1",
        "is_authentic": True,
    })
    assert extracted.membership_type == "Vàng"
    assert extracted.membership_period == "10/2026"
    assert extracted.driver_rank == "Gold"
    assert "Thống kê thu nhập" in extracted.utility_items
    assert extracted.app_version == "3.17.1"
    assert extracted.is_authentic


def test_profile_screen_object_type_defaults_are_unknown_not_invented():
    """Absent labels must stay None/[] - never a fabricated tier, rank, or version."""
    extracted = ProfileScreenExtraction()
    assert extracted.membership_type is None
    assert extracted.membership_period is None
    assert extracted.driver_rank is None
    assert extracted.app_version is None
    assert extracted.utility_items == []
    assert extracted.is_authentic is None


def test_profile_screen_object_type_accepts_mostly_empty_screen():
    """A collapsed/older profile screen with only a membership card still parses."""
    extracted = ProfileScreenExtraction.model_validate({"membership_type": "Bạc"})
    assert extracted.membership_type == "Bạc"
    assert extracted.app_version is None
    assert extracted.utility_items == []


def test_income_statement_guard_ignores_profile_menu_screens():
    """A profile menu that lists 'Thống kê thu nhập' must not be treated as an income statement."""
    from services.submission_pipeline import _looks_like_income_statement

    menu_ocr = {
        "layout_matched": False,
        "period_tabs": [],
        "field_labels": ["Thống kê thu nhập", "Hiệu suất hoạt động", "Công việc của tôi", "Sổ công nợ"],
        "gross_revenue": 0, "net_income": 0, "total_trips": 0,
    }
    assert _looks_like_income_statement(menu_ocr) is False

    income_ocr = {
        "layout_matched": True,
        "period_tabs": ["31 - 06", "07 - 13", "14 - 20"],
        "field_labels": ["Tổng thu nhập", "Doanh số", "Thu nhập ròng"],
        "gross_revenue": 2_838_000, "net_income": 2_329_512, "total_trips": 51,
    }
    assert _looks_like_income_statement(income_ocr) is True

    # Layout detector may miss the teal pill on a photographed screen; numeric evidence still counts.
    assert _looks_like_income_statement({"layout_matched": False, "gross_revenue": 2_838_000}) is True
    assert _looks_like_income_statement(None) is False
