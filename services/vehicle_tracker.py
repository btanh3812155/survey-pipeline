import re
from typing import Optional, Tuple
from pydantic import BaseModel


class VehicleMatchResult(BaseModel):
    is_match: bool
    is_first_registration: bool
    status_code: str  # APPROVED, VEHICLE_MISMATCH, FIRST_REGISTRATION
    flag_reason: Optional[str] = None
    registered_plate: Optional[str] = None
    detected_plate: Optional[str] = None


def normalize_license_plate(plate: Optional[str]) -> str:
    """Normalizes Vietnamese license plate: '29E-123.45' -> '29E12345'"""
    if not plate:
        return ""
    return re.sub(r"[^A-Z0-9]", "", plate.upper().strip())


class VehiclePersistenceTracker:
    def __init__(self, db_pool=None):
        self.db_pool = db_pool

    async def verify_and_persist_vehicle(
        self,
        driver_phone: str,
        detected_plate: Optional[str],
        detected_vehicle_type: str,  # 'BIKE' or 'CAR'
        detected_color: Optional[str] = None,
    ) -> VehicleMatchResult:
        """
        Ensures vehicle consistency per driver.
        - First survey: Registers the vehicle plate & type to the driver.
        - Subsequent surveys: Flags if the driver submits a different vehicle.
        """
        norm_detected = normalize_license_plate(detected_plate)
        vtype = "BIKE" if "BIKE" in detected_vehicle_type.upper() else "CAR"

        # mock database check if db_pool is not connected
        if not self.db_pool:
            if norm_detected and norm_detected.startswith("FAKE"):
                return VehicleMatchResult(
                    is_match=False,
                    is_first_registration=False,
                    status_code="VEHICLE_MISMATCH",
                    flag_reason=f"Phát hiện đổi biển số xe: Đăng ký [29E12345] nhưng nộp [{norm_detected}].",
                    registered_plate="29E12345",
                    detected_plate=norm_detected,
                )
            return VehicleMatchResult(
                is_match=True,
                is_first_registration=False,
                status_code="APPROVED",
                registered_plate=norm_detected or "29E12345",
                detected_plate=norm_detected,
            )

        async with self.db_pool.connection() as conn:
            async with conn.cursor() as cur:
                # 1. lookup existing registered vehicle for this driver
                await cur.execute(
                    """
                    SELECT license_plate, vehicle_type, vehicle_color
                    FROM driver_registered_vehicles
                    WHERE driver_phone = %s;
                """,
                    (driver_phone,),
                )
                existing = await cur.fetchone()

                # 2. first survey -> register this vehicle
                if not existing:
                    if norm_detected:
                        await cur.execute(
                            """
                            INSERT INTO driver_registered_vehicles (
                                driver_phone, license_plate, vehicle_type, vehicle_color
                            ) VALUES (%s, %s, %s, %s)
                            ON CONFLICT (driver_phone) DO NOTHING;
                        """,
                            (driver_phone, norm_detected, vtype, detected_color),
                        )
                        await conn.commit()
                    return VehicleMatchResult(
                        is_match=True,
                        is_first_registration=True,
                        status_code="FIRST_REGISTRATION",
                        registered_plate=norm_detected,
                        detected_plate=norm_detected,
                    )

                # 3. subsequent survey -> verify plate & vehicle type match
                reg_plate, reg_type, reg_color = existing
                norm_reg = normalize_license_plate(reg_plate)

                # check license plate mismatch
                if norm_detected and norm_reg and norm_detected != norm_reg:
                    return VehicleMatchResult(
                        is_match=False,
                        is_first_registration=False,
                        status_code="VEHICLE_MISMATCH",
                        flag_reason=(
                            f"Phát hiện phương tiện không khớp: Tài xế đăng ký xe biển số [{reg_plate}] "
                            f"nhưng ảnh nộp là biển số [{detected_plate}]."
                        ),
                        registered_plate=reg_plate,
                        detected_plate=detected_plate,
                    )

                # check vehicle type mismatch (e.g. registered car submitting bike)
                if reg_type and reg_type != vtype:
                    return VehicleMatchResult(
                        is_match=False,
                        is_first_registration=False,
                        status_code="VEHICLE_TYPE_MISMATCH",
                        flag_reason=f"Sai loại xe: Tài xế đăng ký [{reg_type}] nhưng gửi ảnh kiểm tra xe [{vtype}].",
                        registered_plate=reg_plate,
                        detected_plate=detected_plate,
                    )

                return VehicleMatchResult(
                    is_match=True,
                    is_first_registration=False,
                    status_code="APPROVED",
                    registered_plate=reg_plate,
                    detected_plate=detected_plate,
                )
