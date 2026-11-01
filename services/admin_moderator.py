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
