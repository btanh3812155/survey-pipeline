import re
import inspect
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
            if not norm_detected or (norm_detected and norm_detected.startswith("FAKE")):
                return VehicleMatchResult(
                    is_match=False,
                    is_first_registration=False,
                    status_code="VEHICLE_UNREGISTERED",
                    flag_reason=f"Phương tiện chưa đăng ký: Xe đăng ký là [29E12345] nhưng ảnh chụp là [{norm_detected}].",
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
                reg_plate = existing[0]
                reg_type = existing[1] if len(existing) > 1 else vtype
                reg_color = existing[2] if len(existing) > 2 else None
                norm_reg = normalize_license_plate(reg_plate)

                # check empty or unreadable plate when driver has registered vehicle (VULN-FRAUD-01)
                if norm_reg and not norm_detected:
                    return VehicleMatchResult(
                        is_match=False,
                        is_first_registration=False,
                        status_code="VEHICLE_UNREGISTERED",
                        flag_reason="Không tìm thấy hoặc không đọc được biển số xe trên ảnh.",
                        registered_plate=reg_plate,
                        detected_plate=detected_plate,
                    )
                # check license plate mismatch
                if norm_detected and norm_reg and norm_detected != norm_reg:
                    return VehicleMatchResult(
                        is_match=False,
                        is_first_registration=False,
                        status_code="VEHICLE_UNREGISTERED",
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
                        status_code="VEHICLE_TYPE_UNREGISTERED",
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

    async def approve_vehicle_change(
        self,
        driver_phone: str,
        new_plate: str,
        new_vehicle_type: str,
        admin_id: int,
        reason: str = "Tài xế đổi xe công ty",
    ) -> Tuple[bool, int, str]:
        """
        Pathway for admins to allow legitimate vehicle changes while monitoring frequency & recency.
        Returns (success: bool, changes_in_last_30d: int, audit_note: str).
        """
        norm_new = normalize_license_plate(new_plate)
        vtype = "BIKE" if "BIKE" in new_vehicle_type.upper() else "CAR"

        if not self.db_pool:
            return True, 1, "Mock: Đã cập nhật xe mới thành công."

        async with self.db_pool.connection() as conn:
            async with conn.cursor() as cur:
                # 1. get current vehicle
                await cur.execute(
                    "SELECT license_plate, vehicle_type FROM driver_registered_vehicles WHERE driver_phone = %s;",
                    (driver_phone,),
                )
                curr = await cur.fetchone()
                old_plate = curr[0] if curr else "UNKNOWN"
                old_type = curr[1] if curr else vtype

                # 2. check change frequency in last 30 days
                await cur.execute(
                    """
                    SELECT COUNT(*) FROM vehicle_change_history 
                    WHERE driver_phone = %s AND created_at > NOW() - INTERVAL '30 DAYS';
                """,
                    (driver_phone,),
                )
                recent_changes = (await cur.fetchone())[0] + 1

                # 3. log to audit ledger
                await cur.execute(
                    """
                    INSERT INTO vehicle_change_history (
                        driver_phone, previous_plate, new_plate, previous_vehicle_type, new_vehicle_type, changed_by_admin_id, change_reason
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s);
                """,
                    (driver_phone, old_plate, norm_new, old_type, vtype, admin_id, reason),
                )

                # 4. update active registered vehicle
                await cur.execute(
                    """
                    INSERT INTO driver_registered_vehicles (driver_phone, license_plate, vehicle_type)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (driver_phone) DO UPDATE 
                    SET license_plate = EXCLUDED.license_plate, vehicle_type = EXCLUDED.vehicle_type, updated_at = NOW();
                """,
                    (driver_phone, norm_new, vtype),
                )

                commit_fn = getattr(conn, "commit", None)
                if callable(commit_fn):
                    res = commit_fn()
                    if inspect.isawaitable(res):
                        await res

                warning = ""
                if recent_changes >= 3:
                    warning = f" ⚠️ Cảnh báo: Tài xế này đã đổi xe {recent_changes} lần trong 30 ngày qua!"

                return True, recent_changes, f"Đã duyệt đổi sang xe {norm_new} ({vtype}).{warning}"
