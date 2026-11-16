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


def test_profile_screen_object_type_defaults_are_safe():
    extracted = ProfileScreenExtraction()
    assert extracted.membership_type == "Tiêu chuẩn"
    assert extracted.driver_rank == "Standard"
    assert extracted.utility_items == []
    assert extracted.is_authentic is True
