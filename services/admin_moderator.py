import html
import logging
import httpx
from typing import Optional
from config import settings
logger = logging.getLogger("admin_moderator")


async def forward_flagged_survey_to_admin_group(
    submission_id: str,
    driver_phone: str,
    driver_name: str,
    flag_reason_code: str,
    detail_reason: str,
    image_r2_url: Optional[str] = None,
    http_client: Optional[httpx.AsyncClient] = None,
):
    """
    Escalates suspicious/flagged surveys to the Telegram Admin Moderation Group.
    Admins can review with 1 click: [Force Approve], [Reject], [Warn Driver].
    """
    admin_group_id = getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-1001234567890")
    bot_token = settings.TELEGRAM_BOT_TOKEN

    if not bot_token or bot_token == "mock_tg_token":
        logger.info(
            f"[MOCK ADMIN GROUP ESCALATION] Submission #{submission_id} Flagged: {flag_reason_code} - {detail_reason}"
        )
        return

    caption = (
        f"🚨 <b>CẢNH BÁO KHẢO SÁT CẦN XÉT DUYỆT (FLAGGED)</b>\n\n"
        f"• Mã phiếu: <code>#{submission_id}</code>\n"
        f"• Tài xế: <b>{driver_name}</b> (<code>{driver_phone}</code>)\n"
        f"• Loại cảnh báo: <b>{flag_reason_code}</b>\n"
        f"• Chi tiết vi phạm: <i>{detail_reason}</i>\n\n"
        f"👉 <i>Ban quản trị vui lòng bấm nút bên dưới để xử lý:</i>"
    )

    inline_keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Duyệt",
                    "callback_data": f"admin_approve:{submission_id}",
                },
                {
                    "text": "❌ Từ chối (Confirm Reject)",
                    "callback_data": f"admin_reject:{submission_id}",
                },
            ],
            [
                {
                    "text": "🔄 Cho phép đổi xe",
                    "callback_data": f"admin_change_veh:{submission_id}:{driver_phone}",
                },
                {
                    "text": "🔍 Tra cứu chi tiết",
                    "callback_data": f"admin_query:{submission_id}",
                },
            ],
            [
                {
                    "text": "⚠️ Cảnh cáo",
                    "callback_data": f"admin_warn:{submission_id}:{driver_phone}",
                }
            ],
        ]
    }

    client = http_client or httpx.AsyncClient(timeout=10.0)
    try:
        # if photo available, send as photo with caption; otherwise send as text message
        if image_r2_url and image_r2_url.startswith("http"):
            url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
            payload = {
                "chat_id": admin_group_id,
                "photo": image_r2_url,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": inline_keyboard,
            }
        else:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                "chat_id": admin_group_id,
                "text": caption,
                "parse_mode": "HTML",
                "reply_markup": inline_keyboard,
            }

        await client.post(url, json=payload)
    except Exception as e:
        logger.error(f"failed to escalate to admin group: {e}")


async def query_submission_dossier(
    submission_id: str, db_pool=None, redis_client=None
) -> dict:
    """
    Queries complete forensic, vehicle, financial, and legal audit data for a submission from PostgreSQL & Redis.
    """
    if not db_pool:
        # mock dossier for testing
        return {
            "submission_id": submission_id,
            "status": "REJECTED",
            "rejection_reason": "Ảnh đồng hồ công-tơ-mét bị mờ chói đèn (Laplacian: 42.1 / 100)",
            "driver_name": "Nguyễn Văn A",
            "driver_phone": "0988123456",
            "created_at": "01/11/2026 14:20:10",
            "receipt_serial_no": "VN-9921",
            "store_tax_id": "0100109106",
            "registered_plate": "29E-123.45",
            "detected_plate": "29E-123.45",
            "vehicle_type": "CAR",
            "plate_matched": True,
            "recent_vehicle_changes": 0,
            "gross_income": 880000,
            "platform_discount": 176000,
            "net_income": 704000,
            "trips_count": 12,
            "avg_fare": 73333,
            "math_consistent": True,
            "blur_variance": 42.1,
            "is_sharp": False,
            "is_tampered": False,
            "is_screen_photo": False,
            "gps_latitude": 10.7769,
            "gps_longitude": 106.7009,
            "gps_accuracy": 8.0,
            "has_signature": True,
            "signature_url": "https://cdn.example.com/sig_demo.png",
            "audit_seal_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "remaining_appeals": 2,
        }

    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            # 1. query submission record
            await cur.execute(
                """
                SELECT submission_id, driver_phone, driver_name, receipt_serial_no, store_tax_id,
                       status, rejection_reason, raw_extracted_facts, audit_seal_hash,
                       gps_latitude, gps_longitude, gps_accuracy_meters, signature_r2_url, image_r2_url,
                       created_at, moderated_by_admin_id, moderated_at
                FROM survey_submissions
                WHERE submission_id = %s;
            """,
                (submission_id,),
            )
            sub = await cur.fetchone()
            if not sub:
                import os, json
                dev_storage = os.path.join(os.getcwd(), "data", "dev_test_submissions.json")
                if os.path.exists(dev_storage):
                    try:
                        with open(dev_storage, "r", encoding="utf-8") as df:
                            dev_recs = json.load(df)
                        for r in dev_recs:
                            if r.get("submission_id") == submission_id:
                                return {
                                    "submission_id": submission_id,
                                    "status": r.get("status", "MANUAL_REVIEW"),
                                    "rejection_reason": r.get("reason"),
                                    "driver_name": r.get("driver_name", "Tài xế"),
                                    "driver_phone": r.get("driver_phone", "N/A"),
                                    "created_at": r.get("submitted_at", "Vừa xong"),
                                    "registered_plate": r.get("license_plate", "N/A"),
                                    "detected_plate": r.get("license_plate", "N/A"),
                                    "vehicle_type": r.get("vehicle_type", "CAR"),
                                    "plate_matched": True,
                                    "recent_vehicle_changes": 0,
                                    "gross_income": r.get("total_gross_revenue", 0),
                                    "net_income": r.get("total_net_income", 0),
                                    "trips_count": r.get("total_trips", 0),
                                    "math_consistent": True,
                                    "is_sharp": True,
                                    "is_tampered": False,
                                    "is_screen_photo": False,
                                    "has_signature": True,
                                    "remaining_appeals": 3,
                                }
                    except Exception:
                        pass
                return {"error": f"Không tìm thấy phiếu #{submission_id} trong cơ sở dữ liệu."}
            phone = sub[1] or "Chưa có SĐT"
            raw_facts = sub[7] or {}

            # 2. query registered vehicle
            await cur.execute(
                "SELECT license_plate, vehicle_type FROM driver_registered_vehicles WHERE driver_phone = %s;",
                (phone,),
            )
            veh = await cur.fetchone()
            reg_plate = veh[0] if veh else "Chưa đăng ký"
            reg_type = veh[1] if veh else "CAR"

            # 3. query vehicle change frequency (last 30 days)
            await cur.execute(
                "SELECT COUNT(*) FROM vehicle_change_history WHERE driver_phone = %s AND created_at > NOW() - INTERVAL '30 DAYS';",
                (phone,),
            )
            changes_count = (await cur.fetchone())[0]

            # 4. query remaining appeals in Redis
            remaining_appeals = 3
            if redis_client and phone:
                used_count = await redis_client.get(f"rate_limit:appeal:{phone}")
                if used_count:
                    remaining_appeals = max(0, 3 - int(used_count.decode()))

            return {
                "submission_id": sub[0],
                "driver_phone": phone,
                "driver_name": sub[2] or "Tài xế",
                "receipt_serial_no": sub[3] or "N/A",
                "store_tax_id": sub[4] or "N/A",
                "status": sub[5],
                "rejection_reason": sub[6] or "Không có",
                "audit_seal_hash": sub[8] or "N/A",
                "gps_latitude": sub[9],
                "gps_longitude": sub[10],
                "gps_accuracy": sub[11],
                "signature_url": sub[12],
                "image_url": sub[13],
                "created_at": sub[14].strftime("%d/%m/%Y %H:%M:%S") if sub[14] else "N/A",
                "registered_plate": reg_plate,
                "detected_plate": raw_facts.get("detected_plate", reg_plate),
                "vehicle_type": reg_type,
                "recent_vehicle_changes": changes_count,
                "gross_income": raw_facts.get("raw_total", 0),
                "platform_discount": raw_facts.get("vat_amount", 0),
                "net_income": raw_facts.get("raw_subtotal", 0),
                "has_signature": raw_facts.get("has_signature", False),
                "is_screen_photo": raw_facts.get("is_screen_photo", False),
                "is_tampered": raw_facts.get("is_manipulated_or_photoshopped", False),
                "remaining_appeals": remaining_appeals,
            }


def render_dossier_message(dossier: dict) -> str:
    """
    Formats the queried database dossier into an authoritative, readable Telegram HTML report for admins.
    """
    if "error" in dossier:
        safe_err = html.escape(str(dossier['error']))
        return f"❌ <b>Lỗi tra cứu:</b> {safe_err}"

    sub_id = html.escape(str(dossier.get("submission_id", "N/A")))
    status = dossier["status"]
    status_badge = (
        "✅ HỢP LỆ (APPROVED)"
        if status == "APPROVED"
        else ("❌ ĐÃ TỪ CHỐI (REJECTED)" if status == "REJECTED" else f"⏳ {status}")
    )

    # vehicle match badge
    reg_p = html.escape(str(dossier.get("registered_plate", "N/A")))
    det_p = html.escape(str(dossier.get("detected_plate", "N/A")))
    veh_type = html.escape(str(dossier.get("vehicle_type", "N/A")))
    driver_name = html.escape(str(dossier.get("driver_name", "Tài xế")))
    driver_phone = html.escape(str(dossier.get("driver_phone", "N/A")))
    rej_reason = html.escape(str(dossier.get("rejection_reason", "Không có")))
    veh_badge = "Khớp chuẩn ✅" if reg_p == det_p else f"Lệch biển! (ĐK: {reg_p} != Ảnh: {det_p}) ❌"
    changes = dossier.get("recent_vehicle_changes", 0)
    change_badge = f"{changes} lần trong 30 ngày qua" + (" ⚠️ (Bất thường)" if changes >= 3 else " (Bình thường ✅)")

    # financial math
    gross = dossier.get("gross_income", 0)
    net = dossier.get("net_income", 0)
    discount = dossier.get("platform_discount", 0)

    # gps and signature links
    lat, lng = dossier.get("gps_latitude"), dossier.get("gps_longitude")
    gps_link = f"<a href='https://maps.google.com/?q={lat},{lng}'>{lat:.4f}, {lng:.4f} (±{dossier.get('gps_accuracy', 0)}m)</a>" if lat and lng else "Chưa có GPS"
    sig_link = f"<a href='{dossier['signature_url']}'>Xem chữ ký ngón tay</a>" if dossier.get("signature_url") else "Không có"

    return (
        f"📋 <b>HỒ SƠ TRA CỨU DỮ LIỆU PHIẾU #{sub_id}</b>\n\n"
        f"• Trạng thái: <b>{status_badge}</b>\n"
        f"• Thời gian nộp: <code>{dossier.get('created_at', 'N/A')}</code>\n"
        f"• Lý do từ chối: <i>{rej_reason}</i>\n\n"
        f"👤 <b>Tài Xế & Phương Tiện:</b>\n"
        f"• Họ tên: <b>{driver_name}</b>\n"
        f"• Số điện thoại: <code>{driver_phone}</code>\n"
        f"• Loại xe: <b>{veh_type}</b> | Biển số: <code>{reg_p}</code>\n"
        f"• Đối soát biển số: {veh_badge}\n"
        f"• Lịch sử đổi xe: {change_badge}\n\n"
        f"📊 <b>Số Liệu Tài Chính:</b>\n"
        f"• Tổng doanh số: <b>{gross:,} VNĐ</b>\n"
        f"• Chiết khấu/Phí: {discount:,} VNĐ\n"
        f"• Thu nhập thực nhận: {net:,} VNĐ\n\n"
        f"🛡️ <b>Kiểm Định Gian Lận & Pháp Lý (DPIA):</b>\n"
        f"• Chụp lại màn hình: {'Có ❌' if dossier.get('is_screen_photo') else 'Không ✅'}\n"
        f"• Dấu hiệu Photoshop: {'Có ❌' if dossier.get('is_tampered') else 'Không ✅'}\n"
        f"• Chữ ký xác thực: {sig_link}\n"
        f"• Tọa độ thực tế: {gps_link}\n"
        f"• Lượt khiếu nại còn lại: <b>{dossier.get('remaining_appeals', 3)}/3 lượt</b> (trong phiên 8h)\n"
        f"• Mã niêm phong SHA-256: <code>{dossier.get('audit_seal_hash', 'N/A')[:16]}...</code>\n"
    )
