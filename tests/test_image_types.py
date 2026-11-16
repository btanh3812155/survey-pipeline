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
