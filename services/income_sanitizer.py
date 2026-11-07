import math
from typing import Optional
from pydantic import BaseModel


class IncomeAnalysisResult(BaseModel):
    is_valid: bool
    status_code: str  # APPROVED, OUTLANDISH_INCOME, ARITHMETIC_MISMATCH, SUSPICIOUS_RIDE_FARE
    flag_reason: Optional[str] = None
    suggested_action: str  # APPROVE, AUTO_REJECT, ESCALATE_ADMIN


# Realistic operational income benchmarks in Vietnam for VinFast / Xanh SM / GSM fleet
# Empirically calibrated against 648 vehicle inspection records from GSM dataset & 2026 market policies
INCOME_BENCHMARKS = {
    "BIKE": {  # Xanh SM Bike (VinFast Feliz / Evo 200 / Klara)
        # Daily Bounds
        "daily_gross_min": 150_000,          # 150k VND min daily
        "daily_gross_max": 1_200_000,        # 1.2M VND max realistic daily
        "daily_hours_max": 16.0,             # Max 16 hours/day
        # Weekly Bounds (Empirical GSM Data: Mean = 3.0M, Std = 1.59M)
        "weekly_gross_min": 800_000,         # 800k VND min active week
        "weekly_gross_max": 6_200_000,       # 2-sigma bound (~6.18M VND)
        "weekly_hard_reject": 8_000_000,     # > 3-sigma bound (~7.77M VND) -> Auto Reject
        "weekly_hours_min": 20.0,            # Min 20 hours/week
        "weekly_hours_max": 85.0,            # Normal active week cap
        "weekly_hours_hard_reject": 115.0,   # Physical exhaustion limit
        # Monthly Bounds
        "monthly_gross_min": 4_000_000,      # 4M VND
        "monthly_gross_max": 25_000_000,     # 25M VND realistic cap
        "monthly_hard_reject": 45_000_000,   # > 45M VND (~$1,800 USD) for bike driver is impossible -> AUTO REJECT
        # Unit Ratios by Vehicle Type
        "ride_fare_min": 12_000,             # 12k VND min trip (short hops)
        "ride_fare_max": 150_000,            # 150k VND max trip (long cross-city)
        "rev_per_hour_min": 30_000,          # 30k VND / online hour
        "rev_per_hour_max": 110_000,         # 110k VND / online hour (peak surge)
        "platform_fee_ratio": 0.20,          # ~20% discount/app fee (0% if under 2026 incentive, up to 35% with tax/fines)
        "platform_fee_ratio_min": 0.00,
        "platform_fee_ratio_max": 0.38,
        # Statistical Dispersion Metrics
        "stats": {
            "sample_mean_weekly": 2_996_800,
            "sample_std_weekly": 1_589_900,
            "sample_var_weekly": 2_527_772_000_000,
            "fare_per_trip_mean": 53_600,
            "fare_per_trip_std": 12_500,
        }
    },
    "CAR": {  # General Xanh SM Taxi / Fleet Baseline
        # Daily Bounds
        "daily_gross_min": 400_000,          # 400k VND min daily
        "daily_gross_max": 2_800_000,        # 2.8M VND max realistic daily
        "daily_hours_max": 18.0,             # Max 18 hours/day (shared ca or extreme)
        # Weekly Bounds (Empirical GSM Data: Mean = 5.38M, Std = 2.82M)
        "weekly_gross_min": 1_200_000,       # 1.2M VND min active week
        "weekly_gross_max": 11_000_000,      # 2-sigma bound (~11.03M VND)
        "weekly_hard_reject": 14_000_000,    # > 3-sigma bound (~13.85M VND) -> Auto Reject
        "weekly_hours_min": 25.0,            # Min 25 hours/week
        "weekly_hours_max": 105.0,           # Normal active week cap
        "weekly_hours_hard_reject": 135.0,   # Physical driver limit
        # Monthly Bounds
        "monthly_gross_min": 10_000_000,     # 10M VND
        "monthly_gross_max": 55_000_000,     # 55M VND realistic cap
        "monthly_hard_reject": 100_000_000,  # > 100M VND (~$4,000 USD) -> AUTO REJECT
        # Unit Ratios by Vehicle Type
        "ride_fare_min": 30_000,             # 30k VND min trip (short ride)
        "ride_fare_max": 600_000,            # 600k VND max trip (airport / interprovincial)
        "rev_per_hour_min": 50_000,          # 50k VND / online hour
        "rev_per_hour_max": 210_000,         # 210k VND / online hour (airport/peak)
        "platform_fee_ratio": 0.55,          # ~55% average deduction (bimodal: 20-25% partner vs 60-76% RTO rental fleet)
        "platform_fee_ratio_min": 0.00,
        "platform_fee_ratio_max": 0.85,      # Absolute company cap for fees + deductions
        # Statistical Dispersion Metrics
        "stats": {
            "sample_mean_weekly": 5_381_800,
            "sample_std_weekly": 2_824_000,
            "sample_var_weekly": 7_974_748_000_000,
            "fare_per_trip_mean": 125_990,
            "fare_per_trip_std": 21_750,
        }
    },
    "CAR_VF_MINI": {  # Xanh Mini (VinFast VF 3)
        "daily_gross_min": 300_000,
        "daily_gross_max": 1_800_000,
        "daily_hours_max": 16.0,
        "weekly_gross_min": 1_000_000,
        "weekly_gross_max": 6_500_000,
        "weekly_hard_reject": 8_500_000,
        "weekly_hours_min": 25.0,
        "weekly_hours_max": 95.0,
        "weekly_hours_hard_reject": 120.0,
        "monthly_gross_min": 8_000_000,
        "monthly_gross_max": 35_000_000,
        "monthly_hard_reject": 55_000_000,
        "ride_fare_min": 20_000,             # Lower city hop fare
        "ride_fare_max": 200_000,            # Lower battery range limits extreme long trips
        "rev_per_hour_min": 40_000,
        "rev_per_hour_max": 120_000,
        "platform_fee_ratio": 0.45,
        "platform_fee_ratio_min": 0.00,
        "platform_fee_ratio_max": 0.70,
        "stats": {
            "sample_mean_weekly": 4_200_000,
            "sample_std_weekly": 1_250_000,
            "fare_per_trip_mean": 45_000,
            "fare_per_trip_std": 11_000,
        }
    },
    "CAR_VF_STANDARD": {  # GreenCar Standard (VinFast VF 5 Plus, VF e34, VF 6, VF 7)
        "daily_gross_min": 400_000,
        "daily_gross_max": 2_800_000,
        "daily_hours_max": 18.0,
        "weekly_gross_min": 1_500_000,
        "weekly_gross_max": 10_500_000,
        "weekly_hard_reject": 13_500_000,
        "weekly_hours_min": 30.0,
        "weekly_hours_max": 105.0,
        "weekly_hours_hard_reject": 135.0,
        "monthly_gross_min": 12_000_000,
        "monthly_gross_max": 50_000_000,
        "monthly_hard_reject": 90_000_000,
        "ride_fare_min": 30_000,
        "ride_fare_max": 500_000,
        "rev_per_hour_min": 50_000,
        "rev_per_hour_max": 180_000,
        "platform_fee_ratio": 0.65,          # Typical RTO rental deductions
        "platform_fee_ratio_min": 0.00,
        "platform_fee_ratio_max": 0.80,
        "stats": {
            "sample_mean_weekly": 5_400_000,
            "sample_std_weekly": 2_200_000,
            "fare_per_trip_mean": 120_000,
            "fare_per_trip_std": 19_500,
        }
    },
    "CAR_VF_LUXURY": {  # LuxuryCar (VinFast VF 8, VF 9)
        "daily_gross_min": 600_000,
        "daily_gross_max": 3_800_000,
        "daily_hours_max": 18.0,
        "weekly_gross_min": 2_500_000,
        "weekly_gross_max": 14_000_000,
        "weekly_hard_reject": 18_000_000,
        "weekly_hours_min": 25.0,
        "weekly_hours_max": 95.0,
        "weekly_hours_hard_reject": 120.0,
        "monthly_gross_min": 18_000_000,
        "monthly_gross_max": 75_000_000,
        "monthly_hard_reject": 140_000_000,
        "ride_fare_min": 50_000,
        "ride_fare_max": 1_200_000,          # High-end inter-city / VIP airport transfers
        "rev_per_hour_min": 70_000,
        "rev_per_hour_max": 280_000,
        "platform_fee_ratio": 0.70,          # Higher battery lease fee
        "platform_fee_ratio_min": 0.00,
        "platform_fee_ratio_max": 0.85,
        "stats": {
            "sample_mean_weekly": 9_500_000,
            "sample_std_weekly": 3_100_000,
            "fare_per_trip_mean": 310_000,
            "fare_per_trip_std": 65_000,
        }
    }
}




def analyze_driver_income(
    vehicle_type: str,  # 'BIKE' or 'CAR'
    gross_income: int,
    net_income: Optional[int] = None,
    trips_count: Optional[int] = None,
    platform_discount: Optional[int] = None,
    period: str = "monthly",  # 'daily', 'weekly', or 'monthly'
    online_hours: Optional[float] = None,
    car_model: Optional[str] = None,
) -> IncomeAnalysisResult:
    """
    Analyzes driver reported income against empirical GSM fleet operating bounds in Vietnam.
    Prevents absurd numbers (e.g. bike driver making $10,000/mo) and flags discrepancies.
    """
    v_upper = vehicle_type.upper()
    m_upper = (car_model or "").upper()
    if "BIKE" in v_upper or "XE MÁY" in v_upper:
        vtype = "BIKE"
    elif "VF 8" in m_upper or "VF 9" in m_upper or "LUXURY" in m_upper or "VF8" in m_upper or "VF9" in m_upper:
        vtype = "CAR_VF_LUXURY"
    elif "VF 3" in m_upper or "MINI" in m_upper or "VF3" in m_upper:
        vtype = "CAR_VF_MINI"
    elif "VF 5" in m_upper or "E34" in m_upper or "VF 6" in m_upper or "VF 7" in m_upper or "VF5" in m_upper:
        vtype = "CAR_VF_STANDARD"
    else:
        vtype = "CAR"
    cfg = INCOME_BENCHMARKS[vtype]
    # 0. Check for valid positive finite gross income (VULN-FRAUD-06)
    try:
        val = float(gross_income)
        if math.isnan(val) or math.isinf(val) or val <= 0:
            return IncomeAnalysisResult(
                is_valid=False,
                status_code="INCOME_EXCEEDS_THRESHOLD",
                flag_reason="Thu nhập khai báo không hợp lệ (phải là số dương lớn hơn 0).",
                suggested_action="AUTO_REJECT",
            )
    except (TypeError, ValueError):
        return IncomeAnalysisResult(
            is_valid=False,
            status_code="INCOME_EXCEEDS_THRESHOLD",
            flag_reason="Thu nhập khai báo không hợp lệ (phải là số dương lớn hơn 0).",
            suggested_action="AUTO_REJECT",
        )

    # 1. Check Outlandish Hard Ceiling (Auto-Reject)
    if period == "monthly":
        hard_limit = cfg["monthly_hard_reject"]
        soft_max = cfg["monthly_gross_max"]
    elif period == "weekly":
        hard_limit = cfg["weekly_hard_reject"]
        soft_max = cfg["weekly_gross_max"]
    else:  # daily
        hard_limit = cfg["daily_gross_max"] * 3
        soft_max = cfg["daily_gross_max"]

    if gross_income > hard_limit:
        return IncomeAnalysisResult(
            is_valid=False,
            status_code="OUTLANDISH_INCOME",
            flag_reason=(
                f"Thu nhập khai báo ({gross_income:,} VNĐ) vượt ngưỡng trần thực tế của tài xế {vtype} "
                f"(tối đa {hard_limit:,} VNĐ). Tự động từ chối số liệu phi thực tế."
            ),
            suggested_action="AUTO_REJECT",
        )

    # 2. Check Suspicious Upper Range (Escalate to Admin for Moderation)
    if gross_income > soft_max:
        return IncomeAnalysisResult(
            is_valid=False,
            status_code="SUSPICIOUS_HIGH_INCOME",
            flag_reason=(
                f"Thu nhập ({gross_income:,} VNĐ) cao bất thường so với mức trung bình ({soft_max:,} VNĐ). "
                f"Cần giám sát/admin xác minh lại phiếu lương hoặc ứng dụng tài xế."
            ),
            suggested_action="REJECT",
        )

    # 2.1 Check Online Hours (if provided)
    if online_hours is not None:
        try:
            h_val = float(online_hours)
            if period == "weekly" and (h_val > cfg["weekly_hours_hard_reject"] or h_val < 0):
                return IncomeAnalysisResult(
                    is_valid=False,
                    status_code="OUTLANDISH_ONLINE_HOURS",
                    flag_reason=(
                        f"Số giờ online ({h_val:.1f}h/tuần) vượt quá giới hạn thể chất của tài xế "
                        f"(tối đa {cfg['weekly_hours_hard_reject']}h/tuần)."
                    ),
                    suggested_action="AUTO_REJECT",
                )
            elif period == "daily" and (h_val > cfg["daily_hours_max"] or h_val < 0):
                return IncomeAnalysisResult(
                    is_valid=False,
                    status_code="OUTLANDISH_ONLINE_HOURS",
                    flag_reason=(
                        f"Số giờ online ({h_val:.1f}h/ngày) vượt quá giới hạn ca xe {vtype}."
                    ),
                    suggested_action="AUTO_REJECT",
                )
        except (TypeError, ValueError):
            pass


    # 3. Micro Check: Money Made Per Ride & Trip Counts
    if trips_count and trips_count > 0:
        avg_fare = gross_income / trips_count
        fare_min = cfg["ride_fare_min"]
        fare_max = cfg["ride_fare_max"]

        if avg_fare < fare_min * 0.7 or avg_fare > fare_max * 1.5:
            return IncomeAnalysisResult(
                is_valid=False,
                status_code="SUSPICIOUS_RIDE_FARE",
                flag_reason=(
                    f"Giá cước bình quân ({int(avg_fare):,} VNĐ/cuốc qua {trips_count} chuyến) "
                    f"nằm ngoài dải thực tế ({fare_min:,} - {fare_max:,} VNĐ)."
                ),
                suggested_action="REJECT",
            )

    # 4. Arithmetic Reconciliation: Gross - Discount - Tax == Net
    if net_income and platform_discount:
        expected_net = gross_income - platform_discount
        # allow 5% rounding / tip / incentive variance
        if abs(expected_net - net_income) > (gross_income * 0.05 + 50_000):
            return IncomeAnalysisResult(
                is_valid=False,
                status_code="ARITHMETIC_MISMATCH",
                flag_reason=(
                    f"Số học không khớp: Doanh số ({gross_income:,}đ) - Chiết khấu ({platform_discount:,}đ) "
                    f"không khớp Thu nhập thực nhận ({net_income:,}đ)."
                ),
                suggested_action="REJECT",
            )

    return IncomeAnalysisResult(
        is_valid=True,
        status_code="APPROVED",
        flag_reason=None,
        suggested_action="APPROVE",
    )
