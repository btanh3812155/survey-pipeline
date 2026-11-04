import math
from typing import Optional
from pydantic import BaseModel


class IncomeAnalysisResult(BaseModel):
    is_valid: bool
    status_code: str  # APPROVED, OUTLANDISH_INCOME, ARITHMETIC_MISMATCH, SUSPICIOUS_RIDE_FARE
    flag_reason: Optional[str] = None
    suggested_action: str  # APPROVE, AUTO_REJECT, ESCALATE_ADMIN


# Realistic operational income benchmarks in Vietnam for VinFast / Xanh SM / GSM fleet
INCOME_BENCHMARKS = {
    "BIKE": {  # Xanh SM Bike (VinFast Feliz / Evo / Klara)
        "daily_gross_min": 150_000,       # 150k VND min daily
        "daily_gross_max": 1_200_000,     # 1.2M VND max realistic daily
        "monthly_gross_min": 4_000_000,   # 4M VND
        "monthly_gross_max": 25_000_000,  # 25M VND realistic cap
        "monthly_hard_reject": 45_000_000, # > 45M VND (~$1,800 USD) for a bike driver is impossible -> AUTO REJECT
        "ride_fare_min": 12_000,          # 12k VND min trip
        "ride_fare_max": 150_000,         # 150k VND max trip
        "platform_fee_ratio": 0.20,       # ~20% discount/app fee
    },
    "CAR": {  # Xanh SM Taxi / Luxury (VinFast VF 5, VF e34, VF 8)
        "daily_gross_min": 400_000,       # 400k VND min daily
        "daily_gross_max": 2_800_000,     # 2.8M VND max realistic daily
        "monthly_gross_min": 10_000_000,  # 10M VND
        "monthly_gross_max": 55_000_000,  # 55M VND realistic cap
        "monthly_hard_reject": 100_000_000, # > 100M VND (~$4,000 USD) -> AUTO REJECT (e.g. 10k USD auto drop)
        "ride_fare_min": 30_000,          # 30k VND min trip
        "ride_fare_max": 600_000,         # 600k VND max trip
        "platform_fee_ratio": 0.22,       # ~22% discount/app fee
    }
}


def analyze_driver_income(
    vehicle_type: str,  # 'BIKE' or 'CAR'
    gross_income: int,
    net_income: Optional[int] = None,
    trips_count: Optional[int] = None,
    platform_discount: Optional[int] = None,
    period: str = "monthly",  # 'daily' or 'monthly'
) -> IncomeAnalysisResult:
    """
    Analyzes driver reported income against empirical GSM fleet operating bounds in Vietnam.
    Prevents absurd numbers (e.g. bike driver making $10,000/mo) and flags discrepancies.
    """
    vtype = "BIKE" if "BIKE" in vehicle_type.upper() else "CAR"
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
    hard_limit = cfg["monthly_hard_reject"] if period == "monthly" else (cfg["daily_gross_max"] * 3)
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
    soft_max = cfg["monthly_gross_max"] if period == "monthly" else cfg["daily_gross_max"]
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
