import re
from typing import Optional, Tuple

from psycopg.rows import tuple_row
from pydantic import BaseModel

from core.phone_links import normalize_vn_phone, phone_normalization_sql
from services.privacy_crypto import hash_phone


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
    if not isinstance(plate, str):
        return ""
    return re.sub(r"[^A-Z0-9]", "", plate.upper().strip())


def stored_vehicle_candidate(driver_phone, facts, vehicle_type):
    """Accept only unambiguous plate and type facts on the reviewed submission."""
    phone = normalize_vn_phone(driver_phone)
    if not phone or not isinstance(facts, dict):
        return None
    raw_plate = facts.get("detected_plate")
    if not isinstance(raw_plate, str) or not re.fullmatch(r"[A-Za-z0-9. -]+", raw_plate):
        return None
    plate = normalize_license_plate(raw_plate)
    types = {str(value).strip().upper() for value in (vehicle_type, facts.get("detected_vehicle_type"), facts.get("vehicle_type")) if value}
    if len(types) != 1 or not types <= {"CAR", "BIKE"} or not re.fullmatch(r"[0-9]{2}[A-Z][A-Z0-9]{3,7}", plate):
        return None
    return phone, plate, types.pop()


class VehiclePersistenceTracker:
    def __init__(self, db_pool=None):
        self.db_pool = db_pool

    async def verify_and_persist_vehicle(
        self,
        driver_phone: str,
        detected_plate: Optional[str],
        detected_vehicle_type: str,  # 'BIKE' or 'CAR'
        detected_color: Optional[str] = None,
        driver_phone_hash: Optional[str] = None,
        *,
        persist: bool = True,
    ) -> VehicleMatchResult:
        """
        Ensures vehicle consistency per driver.
        - First survey: Registers the vehicle plate & type unless persist=False.
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

        conn_ctx = self.db_pool.connection()
        conn = await conn_ctx.__aenter__() if hasattr(conn_ctx, "__aenter__") else (await conn_ctx if hasattr(conn_ctx, "__await__") else conn_ctx)
        try:
            cur_ctx = conn.cursor(row_factory=tuple_row)
            if hasattr(cur_ctx, "__await__"):
                cur_ctx = await cur_ctx
            cur = await cur_ctx.__aenter__() if hasattr(cur_ctx, "__aenter__") else cur_ctx
            try:
                # 1. lookup existing registered vehicle for this driver by raw phone or phone hash
                canonical_phone = normalize_vn_phone(driver_phone)
                phone_hash = driver_phone_hash or hash_phone(driver_phone)

                await cur.execute(
                    f"""
                    SELECT license_plate, vehicle_type, vehicle_color
                    FROM driver_registered_vehicles
                    WHERE driver_phone IN (%s, %s, %s) OR {phone_normalization_sql('driver_phone')} = %s
                    ORDER BY (driver_phone = %s) DESC, driver_phone;
                    """,
                    (driver_phone, canonical_phone or driver_phone, phone_hash or driver_phone,
                     canonical_phone, canonical_phone or driver_phone),
                )
                existing = await cur.fetchone()

                # 2. first survey -> register this vehicle
                if existing is None:
                    if persist and norm_detected:
                        await cur.execute(
                            """
                            INSERT INTO driver_registered_vehicles (
                                driver_phone, license_plate, vehicle_type, vehicle_color
                            ) VALUES (%s, %s, %s, %s)
                            ON CONFLICT (driver_phone) DO NOTHING;
                        """,
                            (canonical_phone or driver_phone, norm_detected, vtype, detected_color),
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
                        flag_reason="Biển số trên ảnh không khớp với phương tiện đã đăng ký.",
                        registered_plate=reg_plate,
                        detected_plate=detected_plate,
                    )

                # check vehicle type mismatch (e.g. registered car submitting bike)
                if reg_type and reg_type != vtype:
                    return VehicleMatchResult(
                        is_match=False,
                        is_first_registration=False,
                        status_code="VEHICLE_TYPE_UNREGISTERED",
                        flag_reason="Loại xe trên ảnh không khớp với phương tiện đã đăng ký.",
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
            finally:
                if hasattr(cur_ctx, "__aexit__"):
                    await cur_ctx.__aexit__(None, None, None)
        finally:
            if hasattr(conn_ctx, "__aexit__"):
                await conn_ctx.__aexit__(None, None, None)

    async def approve_vehicle_change(
        self,
        driver_phone: str,
        new_plate: str,
        new_vehicle_type: str,
        admin_id: int,
        reason: str = "Tài xế đổi xe công ty",
        driver_phone_hash: Optional[str] = None,
        *,
        conn=None,
    ) -> Tuple[bool, int, str]:
        """Change the binding and write its history in the same transaction."""
        phone = normalize_vn_phone(driver_phone)
        plate = normalize_license_plate(new_plate)
        vtype = str(new_vehicle_type or "").upper()
        if not phone or not plate or vtype not in ("CAR", "BIKE"):
            return False, 0, "Cần bổ sung số điện thoại, biển số và loại xe đã xác minh."
        if conn is None:
            if not self.db_pool:
                return False, 0, "Chưa cập nhật được xe: thiếu kết nối cơ sở dữ liệu."
            async with self.db_pool.connection() as own_conn:
                return await self.approve_vehicle_change(
                    driver_phone, plate, vtype, admin_id, reason, driver_phone_hash, conn=own_conn,
                )

        async with conn.cursor(row_factory=tuple_row) as cur:
            await cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"vehicle:{phone}",))
            await cur.execute(
                f"SELECT driver_phone, license_plate, vehicle_type FROM driver_registered_vehicles "
                f"WHERE {phone_normalization_sql('driver_phone')}=%s OR driver_phone=%s FOR UPDATE",
                (phone, driver_phone_hash or hash_phone(phone)),
            )
            registrations = await cur.fetchall()
            if len(registrations) > 1:
                return False, 0, "Có nhiều đăng ký xe cho tài xế; cần đối soát trước khi đổi xe."
            if registrations:
                key, old_plate, old_type = registrations[0]
                if normalize_license_plate(old_plate) == plate and old_type == vtype:
                    return False, 0, "Xe này đã được đăng ký; không có thay đổi mới."
                await cur.execute(
                    "UPDATE driver_registered_vehicles SET license_plate=%s, vehicle_type=%s, "
                    "vehicle_color=NULL, vehicle_model=NULL, updated_at=NOW() WHERE driver_phone=%s",
                    (plate, vtype, key),
                )
            else:
                key, old_plate, old_type = phone, "UNKNOWN", "UNKNOWN"
                await cur.execute(
                    "INSERT INTO driver_registered_vehicles(driver_phone, license_plate, vehicle_type) VALUES (%s,%s,%s)",
                    (key, plate, vtype),
                )
            await cur.execute(
                "INSERT INTO vehicle_change_history "
                "(driver_phone, previous_plate, new_plate, previous_vehicle_type, new_vehicle_type, changed_by_admin_id, change_reason) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (key, old_plate, plate, old_type, vtype, admin_id, reason),
            )
            await cur.execute(
                "SELECT COUNT(*) FROM vehicle_change_history WHERE driver_phone=%s AND created_at>NOW()-INTERVAL '30 days'",
                (key,),
            )
            recent_changes = (await cur.fetchone())[0]
        warning = f" ⚠️ Cảnh báo: Tài xế đã đổi xe {recent_changes} lần trong 30 ngày." if recent_changes >= 3 else ""
        return True, recent_changes, f"Đã duyệt đổi sang xe {plate} ({vtype}).{warning}"
