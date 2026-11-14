import os
import glob
import json
import html
import logging
import httpx
from typing import Optional, List, Dict, Any, Union
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
        for storage_path in (
            os.path.join(os.getcwd(), "data", "dev_test_submissions.json"),
            os.path.join(os.getcwd(), "data", "dev_submissions.json"),
        ):
            if os.path.exists(storage_path):
                try:
                    with open(storage_path, "r", encoding="utf-8") as df:
                        dev_recs = json.load(df)
                    for r in dev_recs:
                        if r.get("submission_id") == submission_id:
                            return {
                                "submission_id": submission_id,
                                "status": r.get("status", "MANUAL_REVIEW"),
                                "rejection_reason": r.get("reason"),
                                "driver_name": r.get("driver_name", "Tài xế"),
                                "driver_phone": r.get("driver_phone", "N/A"),
                                "collaborator_id": r.get("collaborator_id") or r.get("ctv_code") or r.get("form_data", {}).get("ctv_code"),
                                "ctv_code": r.get("ctv_code") or r.get("collaborator_id") or r.get("form_data", {}).get("ctv_code"),
                                "created_at": r.get("submitted_at", "Vừa xong"),
                                "vehicle_type": r.get("vehicle_type", "Chưa xác định"),
                                "gross_income": r.get("total_gross_revenue", 0),
                                "net_income": r.get("total_net_income", 0),
                                "trips_count": r.get("total_trips", 0),
                                "photo_paths": [],
                            }
                except Exception:
                    pass
        return {
            "submission_id": submission_id,
            "status": "MANUAL_REVIEW",
            "rejection_reason": "Chờ ban quản trị kiểm tra",
            "driver_name": "Nguyễn Văn A",
            "driver_phone": "0988123456",
            "created_at": "01/11/2026 14:20:10",
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
            # 1. Query submission record with all real survey columns
            await cur.execute(
                """
                SELECT submission_id, driver_phone, driver_name, receipt_serial_no, store_tax_id,
                       status, rejection_reason, raw_extracted_facts, audit_seal_hash,
                       gps_latitude, gps_longitude, gps_accuracy_meters, signature_r2_url, image_r2_url,
                       created_at, moderated_by_admin_id, moderated_at,
                       total_gross_revenue, total_net_income, total_trips,
                       vehicle_type, car_model, form_data,
                       income_week_1_gross, income_week_2_gross, income_week_3_gross, income_week_4_gross, income_week_5_gross,
                       collaborator_id, ctv_code
                FROM survey_submissions
                WHERE submission_id = %s;
            """,
                (submission_id,),
            )
            sub = await cur.fetchone()
            if not sub:
                for storage_path in (
                    os.path.join(os.getcwd(), "data", "dev_test_submissions.json"),
                    os.path.join(os.getcwd(), "data", "dev_submissions.json"),
                ):
                    if os.path.exists(storage_path):
                        try:
                            with open(storage_path, "r", encoding="utf-8") as df:
                                dev_recs = json.load(df)
                            for r in dev_recs:
                                if r.get("submission_id") == submission_id:
                                    return {
                                        "submission_id": submission_id,
                                        "status": r.get("status", "MANUAL_REVIEW"),
                                        "rejection_reason": r.get("reason"),
                                        "driver_name": r.get("driver_name", "Tài xế"),
                                        "driver_phone": r.get("driver_phone", "N/A"),
                                        "collaborator_id": r.get("collaborator_id") or r.get("ctv_code") or r.get("form_data", {}).get("ctv_code"),
                                        "ctv_code": r.get("ctv_code") or r.get("collaborator_id") or r.get("form_data", {}).get("ctv_code"),
                                        "created_at": r.get("submitted_at", "Vừa xong"),
                                        "vehicle_type": r.get("vehicle_type", "Chưa xác định"),
                                        "car_model": r.get("car_model", ""),
                                        "gross_income": r.get("total_gross_revenue", 0),
                                        "net_income": r.get("total_net_income", 0),
                                        "trips_count": r.get("total_trips", 0),
                                        "weekly_incomes": r.get("weekly_incomes", {}),
                                        "photo_paths": [],
                                    }
                        except Exception:
                            pass
                return {"error": f"Không tìm thấy phiếu #{submission_id} trong cơ sở dữ liệu."}

            phone = sub[1] or "Chưa có SĐT"
            driver_name = sub[2] or "Tài xế"
            raw_facts = sub[7] or {}
            if isinstance(raw_facts, str):
                try: raw_facts = json.loads(raw_facts)
                except: raw_facts = {}

            tot_gross = int(sub[17] or 0)
            tot_net = int(sub[18] or 0)
            tot_trips = int(sub[19] or 0)
            v_type = sub[20] or "Chưa xác định"
            c_model = sub[21] or ""
            form_data = sub[22] or {}
            if isinstance(form_data, str):
                try: form_data = json.loads(form_data)
                except: form_data = {}

            if tot_gross == 0 and isinstance(raw_facts, dict):
                tot_gross = int(raw_facts.get("total_gross_revenue") or raw_facts.get("gross_revenue") or raw_facts.get("raw_total") or 0)
                tot_net = int(raw_facts.get("total_net_income") or raw_facts.get("net_income") or raw_facts.get("raw_subtotal") or 0)
                tot_trips = int(raw_facts.get("total_trips") or raw_facts.get("total_trips_count") or 0)

            if (not v_type or v_type in ("CAR", "N/A")) and isinstance(form_data, dict):
                v_type = form_data.get("vehicle_type") or v_type

            # Locate photos on disk
            photo_paths = []
            candidates = [
                os.path.join(os.getcwd(), "data", "submission_photos", submission_id),
                os.path.join(os.getcwd(), "data", "google_drive_deliverables", "Images", submission_id),
            ]
            for c_dir in candidates:
                if os.path.exists(c_dir):
                    p_files = sorted(
                        glob.glob(os.path.join(c_dir, "*.jpg")) +
                        glob.glob(os.path.join(c_dir, "*.jpeg")) +
                        glob.glob(os.path.join(c_dir, "*.png")) +
                        glob.glob(os.path.join(c_dir, "*.webp"))
                    )
                    if p_files:
                        photo_paths = p_files
                        break

            weekly_incomes = raw_facts.get("weekly_incomes") or {
                "week_1": {"gross_revenue": int(sub[23] or 0), "period": "Tuần 1"},
                "week_2": {"gross_revenue": int(sub[24] or 0), "period": "Tuần 2"},
                "week_3": {"gross_revenue": int(sub[25] or 0), "period": "Tuần 3"},
                "week_4": {"gross_revenue": int(sub[26] or 0), "period": "Tuần 4"},
                "week_5": {"gross_revenue": int(sub[27] or 0), "period": "Tuần 5"},
            }

            return {
                "submission_id": sub[0],
                "driver_phone": phone,
                "driver_name": driver_name,
                "status": sub[5],
                "rejection_reason": sub[6] or "Không có",
                "audit_seal_hash": sub[8] or "N/A",
                "gps_latitude": sub[9],
                "gps_longitude": sub[10],
                "gps_accuracy": sub[11],
                "signature_url": sub[12],
                "image_url": sub[13],
                "created_at": sub[14].strftime("%d/%m/%Y %H:%M:%S") if sub[14] else "N/A",
                "vehicle_type": v_type,
                "car_model": c_model,
                "gross_income": tot_gross,
                "net_income": tot_net,
                "trips_count": tot_trips,
                "weekly_incomes": weekly_incomes,
                "photo_paths": photo_paths,
                "collaborator_id": sub[28] if len(sub) > 28 and sub[28] else (form_data.get("collaborator_id") if isinstance(form_data, dict) else None),
                "ctv_code": sub[29] if len(sub) > 29 and sub[29] else (form_data.get("ctv_code") if isinstance(form_data, dict) else None),
            }


def render_dossier_message(dossier: dict) -> str:
    """
    Formats the queried database dossier into an authoritative, readable Telegram HTML report for admins.
    """
    if "error" in dossier:
        safe_err = html.escape(str(dossier['error']))
        return f"❌ <b>Lỗi tra cứu:</b> {safe_err}"

    sub_id = html.escape(str(dossier.get("submission_id", "N/A")))
    status = dossier.get("status", "PENDING")
    status_icon = "✅" if status == "APPROVED" else ("⏳" if status == "MANUAL_REVIEW" else "❌")

    gross = dossier.get("gross_income", 0)
    net = dossier.get("net_income", 0)
    trips = dossier.get("trips_count", 0)
    v_type = dossier.get("vehicle_type", "Chưa xác định")
    c_model = dossier.get("car_model", "")
    veh_str = f"{v_type} ({c_model})" if c_model else v_type

    lines = [
        f"📋 <b>HỒ SƠ KHẢO SÁT #{sub_id}</b>\n",
        f"• <b>Trạng thái:</b> {status_icon} <code>{status}</code>",
        f"• <b>Thời gian:</b> <code>{dossier.get('created_at', 'Vừa xong')}</code>",
        (
            f"• <b>Tài xế:</b> <b>{html.escape(dossier['driver_name'])}</b> (<code>{dossier.get('driver_phone', 'N/A')}</code>)"
            if dossier.get("driver_name") and dossier.get("driver_name") not in ("Tài xế", "Tài xế Bí Mật", "N/A", "[SCRUBBED]")
            else f"• <b>Số điện thoại:</b> <code>{dossier.get('driver_phone', 'N/A')}</code>"
        ),
        f"• <b>Phương tiện:</b> <code>{veh_str}</code>\n",
        "📊 <b>Số Liệu Thu Nhập:</b>",
        f"• Tổng doanh số: <b>{gross:,} VNĐ</b>",
        f"• Thu nhập thực nhận: <b>{net:,} VNĐ</b>",
        f"• Số chuyến: <b>{trips} chuyến</b>\n",
    ]

    w_inc = dossier.get("weekly_incomes") or {}
    if w_inc and isinstance(w_inc, dict):
        w_lines = []
        for w_i in range(1, 6):
            w_data = w_inc.get(f"week_{w_i}", {})
            g_w = int(w_data.get("gross_revenue") or 0)
            t_w = int(w_data.get("total_trips") or 0)
            p_w = w_data.get("period", f"Tuần {w_i}")
            if g_w > 0:
                t_str = f" ({t_w} chuyến)" if t_w > 0 else ""
                w_lines.append(f"  - {p_w}: <b>{g_w:,} đ</b>{t_str}")
        if w_lines:
            lines.append("📅 <b>Chi Tiết Từng Tuần:</b>")
            lines.extend(w_lines)
            lines.append("")

    # Only show GPS if coordinates actually exist!
    lat, lng = dossier.get("gps_latitude"), dossier.get("gps_longitude")
    if lat and lng:
        acc = dossier.get("gps_accuracy")
        acc_str = f" (±{acc}m)" if acc else ""
        lines.append(f"📍 <b>Tọa độ GPS:</b> <a href='https://maps.google.com/?q={lat},{lng}'>{lat:.4f}, {lng:.4f}</a>{acc_str}\n")

    rejection = dossier.get("rejection_reason")
    if rejection and rejection != "Không có":
        lines.append(f"⚠️ <b>Ghi chú:</b> <i>{html.escape(rejection)}</i>\n")

    return "\n".join(lines)
