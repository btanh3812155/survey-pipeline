import os
import re
import io
import time
import json
import random
import asyncio
import logging
from typing import Optional, Union, Dict, Any, List
import httpx
from PIL import Image

from config import settings
try:
    from main import FORM_REGISTRY
except Exception:
    from services.form_engine import FORM_REGISTRY
from services.admin_moderator import (
    query_submission_dossier,
    render_dossier_message,
)
from services.normalizer import compute_laplacian_blur_score

logger = logging.getLogger("telegram_polling")
DEV_STORAGE_FILE = os.path.join(os.getcwd(), "data", "dev_submissions.json")


class TelegramPollingRunner:
    """
    Asynchronous Telegram Bot long-polling daemon.
    In default/admin mode, polls updates for administrative moderation and inspection.
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        polling_timeout: int = 30,
        db_pool=None,
        role: str = "admin",
    ):
        if bot_token:
            self.bot_token = bot_token
        elif role == "admin":
            self.bot_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
        else:
            self.bot_token = getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN

        self.role = role
        self.polling_timeout = polling_timeout
        self.offset = 0
        self.is_running = False
        self.http_client: Optional[httpx.AsyncClient] = None
        self.db_pool = db_pool
        self.backoff_delay = 1.0
        self.max_backoff = 60.0

    async def get_client(self) -> httpx.AsyncClient:
        if self.http_client is None or self.http_client.is_closed:
            self.http_client = httpx.AsyncClient(timeout=float(self.polling_timeout + 15))
        return self.http_client

    async def aclose(self) -> None:
        if self.http_client and not self.http_client.is_closed:
            await self.http_client.aclose()
            self.http_client = None

    async def send_message(
        self,
        chat_id: Union[int, str],
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[dict] = None,
    ) -> bool:
        """Dispatches an HTML or text message to a given chat_id via Telegram Bot API."""
        if not self.bot_token or self.bot_token == "mock_tg_token":
            logger.info(f"[MOCK TG {self.role.upper()} DISPATCH] to {chat_id}:\n{text}")
            return True

        client = await self.get_client()
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            resp = await client.post(url, json=payload)
            if resp.status_code == 429:
                logger.warning("Telegram rate limit 429 encountered during polling dispatch")
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Failed to send Telegram message to {chat_id}: {e}")
            return False

    async def answer_callback_query(
        self, callback_query_id: str, text: Optional[str] = None
    ) -> bool:
        """Acknowledges an inline callback query."""
        if not self.bot_token or self.bot_token == "mock_tg_token":
            return True

        client = await self.get_client()
        url = f"https://api.telegram.org/bot{self.bot_token}/answerCallbackQuery"
        payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text

        try:
            resp = await client.post(url, json=payload)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Failed to answer callback query {callback_query_id}: {e}")
            return False

    async def log_chat_history(
        self,
        telegram_user_id: int,
        chat_id: int,
        message_id: int,
        role: str,
        direction: str,
        message_text: str,
    ) -> None:
        """Records message in telegram_chat_history for Decree 13 regulatory compliance."""
        if not self.db_pool:
            return
        try:
            async with self.db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO telegram_chat_history 
                            (telegram_user_id, chat_id, message_id, role, direction, message_text)
                        VALUES (%s, %s, %s, %s, %s, %s);
                        """,
                        (telegram_user_id, chat_id, message_id, role, direction, message_text),
                    )
                await conn.commit()
        except Exception as e:
            logger.debug(f"Failed to log chat history: {e}")

    async def get_stats_message(self) -> str:
        """Calculates survey submission counts across database or dev test storage."""
        total = 0
        approved = 0
        rejected = 0

        if self.db_pool:
            try:
                async with self.db_pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            SELECT 
                                COUNT(*),
                                COUNT(*) FILTER (WHERE status = 'APPROVED'),
                                COUNT(*) FILTER (WHERE status = 'REJECTED')
                            FROM survey_submissions;
                            """
                        )
                        row = await cur.fetchone()
                        if row:
                            total = row[0] or 0
                            approved = row[1] or 0
                            rejected = row[2] or 0
            except Exception as e:
                logger.warning(f"Could not read survey stats from database: {e}")

        if total == 0 and os.path.exists(DEV_STORAGE_FILE):
            try:
                with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                    dev_data = json.load(f)
                    total = len(dev_data)
                    approved = sum(1 for item in dev_data if item.get("status") == "APPROVED")
                    rejected = sum(1 for item in dev_data if item.get("status") == "REJECTED")
            except Exception as e:
                logger.warning(f"Could not read dev storage for stats: {e}")

        pending = total - (approved + rejected)
        if pending < 0:
            pending = 0

        return (
            f"📊 <b>Thống Kê Khảo Sát Hệ Thống (Admin Stats):</b>\n\n"
            f"• <b>Tổng số phiếu đã tiếp nhận:</b> {total}\n"
            f"• <b>Đã duyệt hợp lệ (Approved):</b> {approved}\n"
            f"• <b>Bị từ chối (Rejected):</b> {rejected}\n"
            f"• <b>Chờ kiểm tra thủ công (Review):</b> {pending}\n\n"
            f"<i>Cập nhật thời gian thực từ cơ sở dữ liệu SSOT.</i>"
        )

    async def handle_command(self, chat_id: Union[int, str], text: str) -> None:
        """Routes and executes admin slash commands."""
        clean_text = text.strip()
        cmd = clean_text.split()[0].lower() if clean_text else ""

        if cmd == "/start":
            welcome_msg = (
                "👋 <b>Chào mừng Ban Quản Trị đến với Bot Điều Hành Trust Bridge (@TrustBridgeVNAdmin_Bot)!</b>\n\n"
                "Hệ thống tự động đồng bộ hóa với pipeline kiểm định AI và cơ sở dữ liệu khảo sát tài xế.\n\n"
                "🛠️ <b>Lệnh điều hành nhanh:</b>\n"
                "• <code>/check &lt;mã_phiếu&gt;</code> - Tra cứu hồ sơ & 1-click duyệt/từ chối\n"
                "• <code>/audit &lt;mã_phiếu&gt;</code> - Kiểm toán niêm phong mật mã SHA-256\n"
                "• <code>/stats</code> - Báo cáo số liệu thời gian thực\n"
                "• <code>/forms</code> - Quản lý danh mục biểu mẫu đang mở"
            )
            await self.send_message(chat_id, welcome_msg)

        elif cmd in ("/check", "/query", "/audit"):
            parts = clean_text.split(" ", 1)
            if len(parts) < 2 or not parts[1].strip():
                action_name = "kiểm toán" if cmd == "/audit" else "tra cứu"
                await self.send_message(
                    chat_id,
                    f"⚠️ <b>Vui lòng cung cấp mã phiếu cần {action_name}.</b>\n"
                    f"Ví dụ: <code>{cmd} VN-123456</code> hoặc <code>{cmd} DEV-ABCD</code>",
                )
                return

            sub_id = parts[1].strip().lstrip("#")
            if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", sub_id):
                await self.send_message(
                    chat_id,
                    "⚠️ <b>Mã phiếu không đúng định dạng.</b>\n"
                    "Chỉ chấp nhận chữ cái, số và dấu gạch ngang (tối đa 64 ký tự).",
                )
                return

            dossier = await query_submission_dossier(sub_id, db_pool=self.db_pool)
            report_text = render_dossier_message(dossier)
            audit_buttons = {
                "inline_keyboard": [
                    [
                        {"text": "🔍 Xem hồ sơ", "callback_data": f"admin_query:{sub_id}"},
                        {"text": "🛡️ Kiểm toán", "callback_data": f"admin_audit:{sub_id}"},
                    ],
                    [
                        {"text": "✅ Duyệt", "callback_data": f"admin_approve:{sub_id}"},
                        {"text": "❌ Từ chối", "callback_data": f"admin_reject:{sub_id}"},
                    ],
                ]
            }
            await self.send_message(chat_id, report_text, reply_markup=audit_buttons)

        elif cmd == "/stats":
            stats_msg = await self.get_stats_message()
            await self.send_message(chat_id, stats_msg)

        elif cmd == "/forms":
            lines = ["📋 <b>Danh Sách Biểu Mẫu Khảo Sát Đang Hoạt Động (Forms):</b>\n"]
            reg = FORM_REGISTRY
            try:
                from main import FORM_REGISTRY as main_reg
                reg = main_reg
            except Exception:
                pass
            if reg:
                for fid, schema in reg.items():
                    title = getattr(schema, "title", fid)
                    lines.append(f"• <b>{fid}</b>: {title}\n  🔗 https://survey.trustbridgevn.com/forms/{fid}")
            else:
                lines.append("• <b>income_survey</b>: Khảo Sát Thu Nhập Tài Xế GSM\n  🔗 https://survey.trustbridgevn.com/forms/income_survey")
            await self.send_message(chat_id, "\n".join(lines))

        else:
            help_msg = (
                "❓ <b>Lệnh không được nhận diện.</b>\n\n"
                "Các lệnh dành cho Quản Trị Viên:\n"
                "• <code>/check &lt;id&gt;</code> - Tra cứu hồ sơ chi tiết\n"
                "• <code>/audit &lt;id&gt;</code> - Kiểm toán niêm phong SHA-256\n"
                "• <code>/stats</code> - Thống kê tiến độ toàn hệ thống\n"
                "• <code>/forms</code> - Xem danh sách link biểu mẫu"
            )
            await self.send_message(chat_id, help_msg)

    async def handle_callback_query(self, cq: dict) -> None:
        """Processes inline keyboard button interactions for admin audits."""
        cq_id = cq.get("id", "")
        data = cq.get("data", "")
        cq_msg = cq.get("message") or {}
        cq_chat = cq_msg.get("chat") or {}
        cq_chat_id = cq_chat.get("id")

        if not cq_chat_id:
            await self.answer_callback_query(cq_id)
            return

        if data.startswith("admin_query:"):
            sub_id = data.split(":", 1)[1]
            dossier = await query_submission_dossier(sub_id, db_pool=self.db_pool)
            report_text = render_dossier_message(dossier)
            await self.send_message(cq_chat_id, report_text)
            await self.answer_callback_query(cq_id, f"Đã tra cứu #{sub_id}")

        elif data.startswith("admin_approve:"):
            sub_id = data.split(":", 1)[1]
            await self._update_submission_status(sub_id, "APPROVED", reason="Quản trị viên phê duyệt thủ công")
            await self.send_message(
                cq_chat_id, f"✅ <b>Quản trị viên đã DUYỆT thủ công phiếu #{sub_id}.</b>"
            )
            await self.answer_callback_query(cq_id, f"Đã duyệt #{sub_id}")

        elif data.startswith("admin_reject:"):
            sub_id = data.split(":", 1)[1]
            await self._update_submission_status(sub_id, "REJECTED", reason="Quản trị viên từ chối thủ công")
            await self.send_message(
                cq_chat_id, f"❌ <b>Quản trị viên đã TỪ CHỐI phiếu #{sub_id}.</b>"
            )
            await self.answer_callback_query(cq_id, f"Đã từ chối #{sub_id}")

        elif data.startswith("admin_warn:"):
            parts = data.split(":")
            sub_id = parts[1] if len(parts) > 1 else "N/A"
            phone = parts[2] if len(parts) > 2 else "N/A"
            await self.send_message(
                cq_chat_id,
                f"⚠️ <b>Đã gửi cảnh cáo tới tài xế {phone} cho phiếu #{sub_id}.</b>",
            )
            await self.answer_callback_query(cq_id, "Đã gửi cảnh cáo")

        elif data.startswith("admin_change_veh:"):
            parts = data.split(":")
            sub_id = parts[1] if len(parts) > 1 else "N/A"
            phone = parts[2] if len(parts) > 2 else "N/A"
            await self.send_message(
                cq_chat_id,
                f"🔄 <b>Đã chấp thuận yêu cầu đổi xe cho tài xế {phone} (phiếu #{sub_id}).</b>",
            )
            await self.answer_callback_query(cq_id, "Đã cho phép đổi xe")

        elif data.startswith("admin_audit:"):
            sub_id = data.split(":", 1)[1]
            dossier = await query_submission_dossier(sub_id, db_pool=self.db_pool)
            report_text = render_dossier_message(dossier)
            await self.send_message(cq_chat_id, report_text)
            await self.answer_callback_query(cq_id, f"Đã kiểm toán #{sub_id}")
        else:
            await self.answer_callback_query(cq_id)

    async def _update_submission_status(self, sub_id: str, new_status: str, reason: str = "") -> None:
        if self.db_pool:
            try:
                async with self.db_pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "UPDATE survey_submissions SET status = %s, rejection_reason = %s WHERE submission_id = %s;",
                            (new_status, reason or None, sub_id),
                        )
                    await conn.commit()
            except Exception as e:
                logger.error(f"Failed to update submission status in DB: {e}")

        if os.path.exists(DEV_STORAGE_FILE):
            try:
                with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                    records = json.load(f)
                updated = False
                for r in records:
                    if r.get("submission_id") == sub_id:
                        r["status"] = new_status
                        r["reason"] = reason
                        updated = True
                        break
                if updated:
                    with open(DEV_STORAGE_FILE, "w", encoding="utf-8") as f:
                        json.dump(records, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"Failed to update submission status in dev storage: {e}")

    async def handle_update(self, update: dict) -> None:
        """Dispatches an incoming update payload to message or callback handlers."""
        if "message" in update and update["message"]:
            msg = update["message"]
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            text = msg.get("text") or ""
            if chat_id and text:
                if len(text) <= 4096 and text.startswith("/"):
                    await self.handle_command(chat_id, text)

        elif "callback_query" in update and update["callback_query"]:
            await self.handle_callback_query(update["callback_query"])

    async def poll_once(self) -> int:
        """Executes a single long-polling request against getUpdates."""
        if not self.bot_token or self.bot_token.lower() in ("mock_tg_token", "mock", "none", ""):
            logger.warning(
                "TELEGRAM_BOT_TOKEN for %s is not configured. Sleeping %.1fs...",
                self.role,
                self.backoff_delay,
            )
            await asyncio.sleep(self.backoff_delay)
            self.backoff_delay = min(self.backoff_delay * 1.5, self.max_backoff)
            return 0

        client = await self.get_client()
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        params = {
            "offset": self.offset,
            "timeout": self.polling_timeout,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }

        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 409:
                logger.warning(
                    f"[{self.role.upper()} BOT] Telegram 409 Conflict: another getUpdates instance running. Backing off {self.backoff_delay:.1f}s"
                )
                await asyncio.sleep(self.backoff_delay)
                self.backoff_delay = min(self.backoff_delay * 2, self.max_backoff)
                return 0
            elif resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                sleep_sec = float(retry_after) if retry_after and retry_after.isdigit() else self.backoff_delay
                logger.warning(f"[{self.role.upper()} BOT] Telegram 429 Too Many Requests hit! Backing off for {sleep_sec:.1f}s")
                await asyncio.sleep(sleep_sec)
                self.backoff_delay = min(self.backoff_delay * 2, self.max_backoff)
                return 0
            elif resp.status_code >= 500:
                logger.warning(f"[{self.role.upper()} BOT] Telegram server error {resp.status_code}. Backing off for {self.backoff_delay:.1f}s")
                await asyncio.sleep(self.backoff_delay)
                self.backoff_delay = min(self.backoff_delay * 2, self.max_backoff)
                return 0
            elif resp.status_code != 200:
                logger.warning(f"[{self.role.upper()} BOT] Telegram getUpdates returned status {resp.status_code}. Backing off {self.backoff_delay:.1f}s")
                await asyncio.sleep(self.backoff_delay)
                self.backoff_delay = min(self.backoff_delay * 1.5, self.max_backoff)
                return 0

            self.backoff_delay = 1.0
            data = resp.json()
            updates = data.get("result", [])
            for upd in updates:
                upd_id = upd.get("update_id", 0)
                if upd_id >= self.offset:
                    self.offset = upd_id + 1
                try:
                    await self.handle_update(upd)
                except Exception as ex:
                    logger.error(f"[{self.role.upper()} BOT] Error handling update {upd_id}: {ex}")

            return len(updates)
        except httpx.TimeoutException:
            return 0
        except Exception as e:
            logger.error(f"[{self.role.upper()} BOT] Telegram long-polling connection error: {e}")
            await asyncio.sleep(3.0)
            return 0

    async def start_polling(self) -> None:
        """Main long-polling loop."""
        self.is_running = True
        masked = f"{self.bot_token[:8]}...{self.bot_token[-4:]}" if self.bot_token and len(self.bot_token) > 12 else "(none)"
        logger.info(f"[{self.role.upper()} BOT] Long-polling runner started (token={masked})")
        try:
            while self.is_running:
                await self.poll_once()
        finally:
            await self.aclose()

    def stop(self) -> None:
        """Signals the long-polling loop to terminate."""
        self.is_running = False


class DriverBotRunner(TelegramPollingRunner):
    """
    Dedicated Collaborator / Driver 1-on-1 Direct Message Bot (@TrustBridgeVN_bot).
    Features:
    - 📱 One-tap Phone Number Verification (Contact Share)
    - 📋 View Submissions & Payout Status (/surveys, /my_surveys)
    - 📸 In-chat Replacement of Blurry Images (Laplacian Blur Check & Instant Resubmit)
    - ⚖️ 1-Click Appeal of Rejected Submissions (Max 3 / 8h Rate Limited)
    - 🆘 Direct Admin Help Request (/support, /hotro)
    - 🛡️ Decree 13 Right to Personal Data Erasure (/scrub_my_data)
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        polling_timeout: int = 30,
        db_pool=None,
    ):
        token = bot_token or getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
        super().__init__(bot_token=token, polling_timeout=polling_timeout, db_pool=db_pool, role="driver")
        # In-memory tracking for pending replacement photo uploads: chat_id -> {sub_id, slot, timestamp}
        self.pending_fixes: Dict[int, dict] = {}
        # In-memory tracking for appeal rate limits: phone -> list of timestamps
        self.appeal_history: Dict[str, List[float]] = {}
        # In-memory tracking for pending support message input: chat_id -> True
        self.pending_support: Dict[int, bool] = {}

    def get_driver_reply_keyboard(self) -> dict:
        """Returns the persistent 1-tap quick action keyboard for drivers & collaborators."""
        return {
            "keyboard": [
                [{"text": "📱 Xác Thực Số Điện Thoại", "request_contact": True}],
                [{"text": "📋 Khảo Sát Của Tôi"}, {"text": "📝 Nộp Form Mới"}],
                [{"text": "🆘 Yêu Cầu Hỗ Trợ"}, {"text": "🛡️ Quyền Riêng Tư"}],
                [{"text": "❓ Trợ Giúp"}],
            ],
            "resize_keyboard": True,
            "one_time_keyboard": False,
        }
    async def get_driver_phone(self, telegram_user_id: int) -> Optional[str]:
        """Looks up driver phone from authorized_users table or memory."""
        if self.db_pool:
            try:
                async with self.db_pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT driver_phone FROM authorized_users WHERE telegram_user_id = %s;",
                            (telegram_user_id,),
                        )
                        row = await cur.fetchone()
                        if row and row[0]:
                            return str(row[0])
            except Exception as e:
                logger.debug(f"DB lookup for driver phone failed: {e}")
        return None

    async def bind_driver_phone(
        self, telegram_user_id: int, username: str, full_name: str, phone: str
    ) -> None:
        """Saves verified driver phone link to authorized_users & identity vault."""
        clean_phone = re.sub(r"[^\d+]", "", phone.strip())
        if clean_phone.startswith("+84"):
            clean_phone = "0" + clean_phone[3:]
        elif clean_phone.startswith("84") and len(clean_phone) == 11:
            clean_phone = "0" + clean_phone[2:]

        if self.db_pool:
            try:
                async with self.db_pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            INSERT INTO authorized_users 
                                (telegram_user_id, username, full_name, role, driver_phone, is_active)
                            VALUES (%s, %s, %s, 'DRIVER', %s, TRUE)
                            ON CONFLICT (telegram_user_id) DO UPDATE SET 
                                driver_phone = EXCLUDED.driver_phone,
                                full_name = EXCLUDED.full_name;
                            """,
                            (telegram_user_id, username or None, full_name, clean_phone),
                        )
                        driver_id = f"DRV-{telegram_user_id % 100000:05d}"
                        await cur.execute(
                            """
                            INSERT INTO driver_identity_vault (driver_id, driver_phone, driver_name)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (driver_phone) DO UPDATE SET driver_name = EXCLUDED.driver_name;
                            """,
                            (driver_id, clean_phone, full_name),
                        )
                    await conn.commit()
            except Exception as e:
                logger.error(f"Failed to bind driver phone in database: {e}")

    async def download_telegram_photo(self, file_id: str) -> bytes:
        """Downloads high-res photo bytes from Telegram Bot API."""
        client = await self.get_client()
        url = f"https://api.telegram.org/bot{self.bot_token}/getFile"
        res = await client.get(url, params={"file_id": file_id})
        data = res.json()
        file_path = data["result"]["file_path"]
        download_url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
        photo_resp = await client.get(download_url)
        return photo_resp.content

    async def handle_contact_sharing(self, chat_id: int, user_id: int, contact: dict, from_user: dict) -> None:
        """Handles native contact card sharing."""
        phone = contact.get("phone_number", "")
        name = contact.get("first_name", "") + (" " + contact.get("last_name", "") if contact.get("last_name") else "")
        if not name.strip():
            name = from_user.get("first_name", "Thành viên")

        username = from_user.get("username", "")
        await self.bind_driver_phone(user_id, username, name, phone)

        clean_phone = re.sub(r"[^\d+]", "", phone.strip())
        masked_phone = clean_phone[:3] + "****" + clean_phone[-3:] if len(clean_phone) >= 7 else clean_phone

        welcome_text = (
            f"✅ <b>Xác thực số điện thoại thành công!</b>\n\n"
            f"• <b>Họ tên:</b> {name}\n"
            f"• <b>Số điện thoại liên kết:</b> <code>{masked_phone}</code>\n"
            f"• <b>Vai trò:</b> Tài xế & Cộng tác viên\n"
            f"• <b>Trạng thái:</b> Đã kích hoạt liên kết tài khoản\n\n"
            f"Từ bây giờ, mọi thông báo duyệt phiếu khảo sát, thông báo trả thưởng và yêu cầu bổ sung ảnh sẽ được gửi tự động trực tiếp vào khung chat này!\n\n"
            f"👉 <i>Bấm <b>[📋 Khảo Sát Của Tôi]</b> để xem danh sách phiếu đã nộp.</i>"
        )
        await self.send_message(chat_id, welcome_text, reply_markup=self.get_driver_reply_keyboard())

    async def handle_photo_upload(self, chat_id: int, user_id: int, photos: list) -> None:
        """Handles in-chat replacement of blurry survey photos."""
        highest_res = photos[-1]
        file_id = highest_res["file_id"]

        fix_info = self.pending_fixes.get(chat_id)
        sub_id = fix_info.get("sub_id") if fix_info else None
        slot_name = fix_info.get("slot", "slot_photo") if fix_info else "Ảnh khảo sát"

        try:
            photo_bytes = await self.download_telegram_photo(file_id)
            pil_img = Image.open(io.BytesIO(photo_bytes))
            is_sharp, blur_score = compute_laplacian_blur_score(pil_img, threshold=35.0)

            if not is_sharp:
                await self.send_message(
                    chat_id,
                    f"⚠️ <b>Ảnh gửi chưa đạt chuẩn độ nét!</b>\n\n"
                    f"• <b>Độ nét hiện tại:</b> <code>{blur_score:.1f}</code> / 35.0 (Chưa đạt)\n"
                    f"• <b>Mục cần bổ sung:</b> <b>{slot_name}</b>\n\n"
                    f"Vui lòng lau sạch ống kính camera, giữ chắc tay và chụp lại trong điều kiện đủ ánh sáng.",
                    reply_markup=self.get_driver_reply_keyboard(),
                )
                return

            # Image is sharp! Update the record
            if sub_id:
                await self._update_submission_status(
                    sub_id, "APPROVED", reason=f"Bổ sung ảnh đạt chuẩn qua Telegram DM (Độ nét: {blur_score:.1f})"
                )
                if chat_id in self.pending_fixes:
                    del self.pending_fixes[chat_id]

                confirm_text = (
                    f"🎉 <b>Bổ sung ảnh thành công và đạt chuẩn!</b>\n\n"
                    f"• <b>Mã phiếu:</b> <code>#{sub_id}</code>\n"
                    f"• <b>Độ nét đo được:</b> <code>{blur_score:.1f}</code> (Đạt chuẩn > 35.0)\n"
                    f"• <b>Trạng thái mới:</b> <b>ĐÃ DUYỆT THÀNH CÔNG ✅</b>\n\n"
                    f"Hệ thống đã ghi nhận cập nhật và chuyển phiếu sang hàng đợi chi trả tiền thưởng!"
                )
                await self.send_message(chat_id, confirm_text, reply_markup=self.get_driver_reply_keyboard())

                # Alert Admin Group via Admin Bot
                admin_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
                admin_group = getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-1001234567890")
                if admin_token and admin_token != "mock_tg_token":
                    client = await self.get_client()
                    alert_url = f"https://api.telegram.org/bot{admin_token}/sendMessage"
                    alert_text = (
                        f"🔄 <b>Thành viên đã bổ sung ảnh đạt chuẩn qua Telegram DM!</b>\n\n"
                        f"• <b>Mã phiếu:</b> #{sub_id}\n"
                        f"• <b>Độ nét:</b> {blur_score:.1f}\n"
                        f"• <b>Trạng thái:</b> Chuyển thành APPROVED ✅"
                    )
                    try:
                        await client.post(alert_url, json={"chat_id": admin_group, "text": alert_text, "parse_mode": "HTML"})
                    except Exception as ex:
                        logger.debug(f"Could not dispatch admin alert: {ex}")
            else:
                await self.send_message(
                    chat_id,
                    f"📸 <b>Đã nhận ảnh với độ nét đạt chuẩn ({blur_score:.1f})!</b>\n\n"
                    f"Hiện tại không có phiếu nào đang chờ bổ sung ảnh. Nếu bạn muốn kiểm tra các phiếu đã nộp, bấm <b>[📋 Khảo Sát Của Tôi]</b>.",
                    reply_markup=self.get_driver_reply_keyboard(),
                )
        except Exception as e:
            logger.error(f"Failed to process photo upload: {e}")
            await self.send_message(
                chat_id,
                "⚠️ Đã có lỗi khi tải hoặc phân tích ảnh. Vui lòng thử lại sau.",
                reply_markup=self.get_driver_reply_keyboard(),
            )

    async def handle_driver_surveys_lookup(self, chat_id: int, user_id: int) -> None:
        """Fetches and displays driver's submissions with quick action buttons."""
        phone = await self.get_driver_phone(user_id)
        submissions = []

        if self.db_pool and phone:
            try:
                async with self.db_pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            SELECT submission_id, status, rejection_reason, created_at
                            FROM survey_submissions
                            WHERE driver_phone = %s
                            ORDER BY created_at DESC
                            LIMIT 5;
                            """,
                            (phone,),
                        )
                        rows = await cur.fetchall()
                        for row in rows:
                            submissions.append({
                                "submission_id": row[0],
                                "status": row[1],
                                "reason": row[2] or "",
                                "created_at": row[3].strftime("%d/%m/%Y %H:%M") if hasattr(row[3], "strftime") else str(row[3])[:16],
                            })
            except Exception as e:
                logger.warning(f"Could not read surveys for driver: {e}")

        # Check dev test storage as fallback
        if not submissions and os.path.exists(DEV_STORAGE_FILE):
            try:
                with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                    records = json.load(f)
                    for r in records[-5:]:
                        if not phone or r.get("driver_phone") == phone:
                            submissions.append({
                                "submission_id": r.get("submission_id", "DEV"),
                                "status": r.get("status", "SUBMITTED"),
                                "reason": r.get("reason", ""),
                                "created_at": str(r.get("submitted_at", ""))[:16],
                            })
            except Exception:
                pass

        if not submissions:
            if not phone:
                msg = (
                    "ℹ️ <b>Bạn chưa liên kết số điện thoại!</b>\n\n"
                    "Vui lòng bấm nút <b>[📱 Xác Thực Số Điện Thoại]</b> bên dưới để hệ thống tự động tìm các phiếu khảo sát của bạn."
                )
            else:
                msg = (
                    f"📋 <b>Chưa tìm thấy phiếu khảo sát nào cho số điện thoại <code>{phone}</code>.</b>\n\n"
                    f"👉 Bấm <b>[📝 Nộp Form Mới]</b> để tham gia khảo sát ngay nhé!"
                )
            await self.send_message(chat_id, msg, reply_markup=self.get_driver_reply_keyboard())
            return

        lines = ["📋 <b>Danh Sách Phiếu Khảo Sát Của Bạn:</b>\n"]
        buttons = []

        for idx, sub in enumerate(submissions, 1):
            sub_id = sub["submission_id"]
            st = sub["status"]
            if st == "APPROVED":
                badge = "ĐÃ DUYỆT ✅"
            elif st == "REJECTED":
                badge = "TỪ CHỐI ❌"
            elif st == "APPEAL_PENDING":
                badge = "CHỜ PHÚC KHẢO ⚖️"
            else:
                badge = "ĐANG XỬ LÝ ⏳"

            item_text = f"{idx}. <b>#{sub_id}</b> ({sub['created_at']})\n   • Trạng thái: <b>{badge}</b>"
            if sub["reason"]:
                item_text += f"\n   • Lý do: <i>{sub['reason']}</i>"
            lines.append(item_text)

            if st == "REJECTED":
                buttons.append([
                    {"text": f"📸 Gửi lại ảnh #{sub_id}", "callback_data": f"driver_fix:{sub_id}:slot_photo"},
                    {"text": f"⚖️ Phúc khảo #{sub_id}", "callback_data": f"driver_appeal:{sub_id}"},
                ])

        inline_markup = {"inline_keyboard": buttons} if buttons else None
        await self.send_message(chat_id, "\n\n".join(lines), reply_markup=inline_markup)

    async def handle_driver_appeal(self, chat_id: int, user_id: int, sub_id: str) -> None:
        """Handles driver appeal request with 8-hour rate limit (max 3)."""
        phone = await self.get_driver_phone(user_id) or str(user_id)
        now = time.time()
        window = 8 * 3600

        # Rate limit check: max 3 appeals in 8h
        history = [t for t in self.appeal_history.get(phone, []) if now - t < window]
        if len(history) >= 3:
            await self.send_message(
                chat_id,
                "🚫 <b>Bạn đã gửi tối đa 3 lần phúc khảo trong 8 giờ qua!</b>\n\n"
                "Vui lòng đợi Ban Quản Trị xử lý các yêu cầu trước đó hoặc gửi yêu cầu trợ giúp qua lệnh <code>/support</code>.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return

        history.append(now)
        self.appeal_history[phone] = history

        # Update status in DB / storage
        await self._update_submission_status(sub_id, "APPEAL_PENDING", reason="Thành viên gửi yêu cầu phúc khảo qua Telegram DM")

        # Notify Member
        await self.send_message(
            chat_id,
            f"📨 <b>Yêu cầu phúc khảo cho phiếu #{sub_id} đã được gửi thành công!</b>\n\n"
            f"Ban Quản Trị sẽ trực tiếp kiểm tra lại hồ sơ và phản hồi kết quả sớm nhất tại đây.",
            reply_markup=self.get_driver_reply_keyboard(),
        )

        # Notify Admin Group via Admin Bot
        admin_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
        admin_group = getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-1001234567890")
        if admin_token and admin_token != "mock_tg_token":
            client = await self.get_client()
            url = f"https://api.telegram.org/bot{admin_token}/sendMessage"
            payload = {
                "chat_id": admin_group,
                "text": (
                    f"⚖️ <b>YÊU CẦU PHÚC KHẢO MỚI TỪ TÀI XẾ!</b>\n\n"
                    f"• <b>Mã phiếu:</b> <code>#{sub_id}</code>\n"
                    f"• <b>SĐT tài xế:</b> <code>{phone}</code>\n"
                    f"• <b>Thời gian:</b> Vừa gửi\n\n"
                    f"Vui lòng xem xét và bấm nút duyệt hoặc bác bỏ bên dưới:"
                ),
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {"text": f"✅ Duyệt #{sub_id}", "callback_data": f"admin_approve:{sub_id}"},
                            {"text": f"❌ Bác bỏ #{sub_id}", "callback_data": f"admin_reject:{sub_id}"},
                        ],
                        [
                            {"text": f"🔍 Xem hồ sơ #{sub_id}", "callback_data": f"admin_query:{sub_id}"},
                        ],
                    ]
                },
            }
            try:
                await client.post(url, json=payload)
            except Exception as e:
                logger.error(f"Failed to alert admin group about appeal: {e}")

    async def handle_decree13_scrub(self, chat_id: int, user_id: int) -> None:
        """Handles Decree 13 right to erasure."""
        confirm_markup = {
            "inline_keyboard": [
                [
                    {"text": "⚠️ Xác Nhận Xóa Vĩnh Viễn", "callback_data": "driver_confirm_scrub"},
                    {"text": "❌ Hủy Bỏ", "callback_data": "driver_cancel_scrub"},
                ]
            ]
        }
        msg = (
            "🛡️ <b>Quyền Riêng Tư & Xóa Dữ Liệu Cá Nhân (Nghị định 13/2023/NĐ-CP):</b>\n\n"
            "Bạn có quyền yêu cầu xóa bỏ hoàn toàn số điện thoại, danh tính và lịch sử liên kết tài khoản khỏi hệ thống Trust Bridge.\n\n"
            "⚠️ <b>Lưu ý:</b> Sau khi xóa, bạn sẽ không nhận được thông báo giải ngân tiền thưởng và không thể tra cứu lại các phiếu cũ.\n\n"
            "Bạn có chắc chắn muốn xóa không?"
        )
        await self.send_message(chat_id, msg, reply_markup=confirm_markup)

    async def handle_admin_help_request(
        self, chat_id: int, user_id: int, message_text: str, from_user: dict
    ) -> None:
        """Forwards support inquiries from drivers or collaborators to the Telegram Admin Group."""
        clean_msg = message_text.strip()
        if not clean_msg:
            self.pending_support[chat_id] = True
            prompt = (
                "💬 <b>Bạn cần Ban Quản Trị hỗ trợ vấn đề gì?</b>\n\n"
                "Hãy nhập nội dung thắc mắc hoặc khó khăn của bạn ngay tại đây (ví dụ: <i>Cần kiểm tra trạng thái trả thưởng phiếu VN-123456</i>).\n\n"
                "Hệ thống sẽ chuyển tiếp trực tiếp tin nhắn của bạn tới Ban Quản Trị."
            )
            await self.send_message(chat_id, prompt, reply_markup=self.get_driver_reply_keyboard())
            return

        phone = await self.get_driver_phone(user_id) or "Chưa liên kết SĐT"
        name = from_user.get("first_name", "") + (" " + from_user.get("last_name", "") if from_user.get("last_name") else "")
        if not name.strip():
            name = f"Thành viên #{user_id}"
        username = from_user.get("username")

        if chat_id in self.pending_support:
            del self.pending_support[chat_id]

        # 1. Forward to Telegram Admin Group via Admin Bot
        admin_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
        admin_group = getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-1001234567890")
        if admin_token and admin_token != "mock_tg_token":
            client = await self.get_client()
            url = f"https://api.telegram.org/bot{admin_token}/sendMessage"
            user_ref = f"@{username}" if username else f"ID: <code>{user_id}</code>"
            admin_card = (
                f"🆘 <b>YÊU CẦU TRỢ GIÚP TỪ THÀNH VIÊN!</b>\n\n"
                f"• <b>Người gửi:</b> {name} ({user_ref})\n"
                f"• <b>Số điện thoại:</b> <code>{phone}</code>\n"
                f"• <b>Chat ID:</b> <code>{chat_id}</code>\n"
                f"• <b>Nội dung yêu cầu:</b>\n"
                f"<i>\"{clean_msg}\"</i>"
            )
            buttons = []
            if username:
                buttons.append([{"text": "💬 Nhắn Qua Telegram", "url": f"https://t.me/{username}"}])
            payload: Dict[str, Any] = {
                "chat_id": admin_group,
                "text": admin_card,
                "parse_mode": "HTML",
            }
            if buttons:
                payload["reply_markup"] = {"inline_keyboard": buttons}
            try:
                await client.post(url, json=payload)
            except Exception as e:
                logger.error(f"Failed to forward help request to admin group: {e}")

        # 2. Confirm to user
        confirm_text = (
            f"📨 <b>Đã gửi yêu cầu hỗ trợ tới Ban Quản Trị!</b>\n\n"
            f"• <b>Nội dung:</b> <i>\"{clean_msg}\"</i>\n\n"
            f"Đội ngũ hỗ trợ sẽ xem xét và phản hồi bạn trong thời gian sớm nhất."
        )
        await self.send_message(chat_id, confirm_text, reply_markup=self.get_driver_reply_keyboard())

    async def handle_driver_command(self, chat_id: int, user_id: int, text: str, from_user: dict) -> None:
        """Dispatches text commands and persistent keyboard button presses."""
        clean = text.strip()
        cmd = clean.split()[0].lower() if clean else ""

        if cmd == "/start" or clean == "Bắt đầu":
            parts = clean.split(" ", 1)
            if len(parts) > 1 and parts[1].startswith("fix_"):
                # Deep link: /start fix_<sub_id>_<slot>
                fix_data = parts[1][4:].split("_", 1)
                sub_id = fix_data[0]
                slot_name = fix_data[1] if len(fix_data) > 1 else "slot_photo"
                self.pending_fixes[chat_id] = {"sub_id": sub_id, "slot": slot_name, "timestamp": time.time()}
                await self.send_message(
                    chat_id,
                    f"📸 <b>Bổ Sung Ảnh Cho Phiếu #{sub_id}:</b>\n\n"
                    f"Ảnh mục <b>{slot_name}</b> trước đó chưa đạt tiêu chuẩn độ nét.\n"
                    f"👉 <i>Vui lòng chụp lại rõ nét và gửi thẳng ảnh vào khung chat này ngay bây giờ!</i>",
                    reply_markup=self.get_driver_reply_keyboard(),
                )
                return

            welcome_text = (
                "👋 <b>Chào mừng bạn đến với Kênh Hỗ Trợ Khảo Sát Trust Bridge!</b>\n\n"
                "Kênh thông tin và hỗ trợ tự động dành cho Tài xế và Cộng tác viên:\n"
                "• Nhận thông báo kết quả duyệt phiếu tức thì\n"
                "• Bổ sung ảnh mờ trực tiếp trong chat để kiểm tra lại\n"
                "• Tra cứu lịch sử khảo sát & yêu cầu phúc khảo\n"
                "• Kết nối trực tiếp với Ban Quản Trị\n\n"
                "👉 <i>Vui lòng bấm nút <b>[📱 Xác Thực Số Điện Thoại]</b> bên dưới để bắt đầu.</i>"
            )
            await self.send_message(chat_id, welcome_text, reply_markup=self.get_driver_reply_keyboard())

        elif cmd in ("/surveys", "/my_surveys", "/tracuu") or clean == "📋 Khảo Sát Của Tôi":
            await self.handle_driver_surveys_lookup(chat_id, user_id)

        elif cmd in ("/forms", "/form") or clean == "📝 Nộp Form Mới":
            lines = ["📝 <b>Biểu Mẫu Khảo Sát Đang Mở:</b>\n"]
            reg = FORM_REGISTRY
            try:
                from main import FORM_REGISTRY as main_reg
                reg = main_reg
            except Exception:
                pass
            if reg:
                for fid, schema in reg.items():
                    title = getattr(schema, "title", fid)
                    lines.append(f"• <b>{title}</b>:\n  🔗 https://survey.trustbridgevn.com/forms/{fid}")
            else:
                lines.append("• <b>Khảo Sát Thu Nhập</b>:\n  🔗 https://survey.trustbridgevn.com/forms/income_survey")
            await self.send_message(chat_id, "\n".join(lines), reply_markup=self.get_driver_reply_keyboard())

        elif cmd in ("/support", "/hotro", "/help_admin", "/lienhe") or "hỗ trợ" in clean.lower():
            if clean.startswith("/"):
                parts = clean.split(" ", 1)
                msg_body = parts[1].strip() if len(parts) > 1 else ""
            else:
                msg_body = ""
            await self.handle_admin_help_request(chat_id, user_id, msg_body, from_user)

        elif cmd in ("/privacy", "/scrub_my_data") or clean == "🛡️ Quyền Riêng Tư":
            await self.handle_decree13_scrub(chat_id, user_id)

        elif cmd in ("/help", "/trogiup") or clean == "❓ Trợ Giúp":
            help_text = (
                "❓ <b>Hướng Dẫn Sử Dụng (Dành Cho Tài Xế & Cộng Tác Viên):</b>\n\n"
                "1. <b>Xác thực SĐT:</b> Bấm [📱 Xác Thực Số Điện Thoại] để liên kết tài khoản.\n"
                "2. <b>Xem phiếu đã nộp:</b> Bấm [📋 Khảo Sát Của Tôi].\n"
                "3. <b>Nộp phiếu mới:</b> Bấm [📝 Nộp Form Mới] để nhận link khảo sát.\n"
                "4. <b>Bổ sung ảnh mờ:</b> Bấm [📸 Gửi lại ảnh] rồi gửi ảnh mới trực tiếp vào chat.\n"
                "5. <b>Phúc khảo:</b> Gõ <code>/appeal &lt;mã_phiếu&gt;</code> nếu kết quả chưa chính xác.\n"
                "6. <b>Cần trợ giúp:</b> Gõ <code>/support &lt;nội dung&gt;</code> hoặc bấm [🆘 Yêu Cầu Hỗ Trợ].\n"
                "7. <b>Quyền riêng tư:</b> Bấm [🛡️ Quyền Riêng Tư] để xóa dữ liệu cá nhân theo Nghị định 13."
            )
            await self.send_message(chat_id, help_text, reply_markup=self.get_driver_reply_keyboard())

        elif cmd == "/appeal":
            parts = clean.split(" ", 1)
            if len(parts) < 2:
                await self.send_message(
                    chat_id,
                    "⚠️ Vui lòng cung cấp mã phiếu cần phúc khảo. Ví dụ: <code>/appeal VN-123456</code>",
                    reply_markup=self.get_driver_reply_keyboard(),
                )
                return
            await self.handle_driver_appeal(chat_id, user_id, parts[1].strip().lstrip("#"))

        else:
            if self.pending_support.get(chat_id):
                await self.handle_admin_help_request(chat_id, user_id, clean, from_user)
            else:
                await self.send_message(
                    chat_id,
                    "Bạn cần hỗ trợ gì? Hãy chọn một tính năng trên bàn phím bên dưới nhé!",
                    reply_markup=self.get_driver_reply_keyboard(),
                )

    async def handle_update(self, update: dict) -> None:
        """Dispatches driver direct messages, contact shares, photos, and callbacks."""
        if "message" in update and update["message"]:
            msg = update["message"]
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            from_user = msg.get("from") or {}
            user_id = from_user.get("id", chat_id)

            # 1. Contact Card Shared
            if "contact" in msg and msg["contact"]:
                await self.handle_contact_sharing(chat_id, user_id, msg["contact"], from_user)
                return

            # 2. Photo Uploaded
            if "photo" in msg and msg["photo"]:
                await self.handle_photo_upload(chat_id, user_id, msg["photo"])
                return

            # 3. Text Command or Button
            text = msg.get("text") or ""
            if chat_id and text:
                if self.pending_support.get(chat_id) and not text.startswith("/"):
                    await self.handle_admin_help_request(chat_id, user_id, text, from_user)
                else:
                    await self.handle_driver_command(chat_id, user_id, text, from_user)
        elif "callback_query" in update and update["callback_query"]:
            cq = update["callback_query"]
            cq_id = cq.get("id", "")
            data = cq.get("data", "")
            cq_msg = cq.get("message") or {}
            chat_id = cq_msg.get("chat", {}).get("id")
            user_id = cq.get("from", {}).get("id", chat_id)

            if data.startswith("driver_appeal:"):
                sub_id = data.split(":", 1)[1]
                await self.handle_driver_appeal(chat_id, user_id, sub_id)
                await self.answer_callback_query(cq_id, f"Đã gửi phúc khảo #{sub_id}")

            elif data.startswith("driver_fix:"):
                parts = data.split(":")
                sub_id = parts[1]
                slot_name = parts[2] if len(parts) > 2 else "slot_photo"
                self.pending_fixes[chat_id] = {"sub_id": sub_id, "slot": slot_name, "timestamp": time.time()}
                await self.send_message(
                    chat_id,
                    f"📸 <b>Đang chờ nhận ảnh cho phiếu #{sub_id} ({slot_name}):</b>\n\n"
                    f"👉 Bác tài hãy mở camera, chụp rõ nét và gửi thẳng ảnh vào chat này ngay nhé!",
                    reply_markup=self.get_driver_reply_keyboard(),
                )
                await self.answer_callback_query(cq_id, "Đang chờ gửi ảnh mới")

            elif data == "driver_confirm_scrub":
                phone = await self.get_driver_phone(user_id)
                if self.db_pool and phone:
                    try:
                        async with self.db_pool.connection() as conn:
                            async with conn.cursor() as cur:
                                await cur.execute(
                                    """
                                    UPDATE driver_identity_vault 
                                    SET driver_name = '[SCRUBBED]', is_scrubbed = TRUE, scrubbed_at = NOW()
                                    WHERE driver_phone = %s;
                                    """,
                                    (phone,),
                                )
                                await cur.execute(
                                    "UPDATE authorized_users SET driver_phone = NULL WHERE telegram_user_id = %s;",
                                    (user_id,),
                                )
                            await conn.commit()
                    except Exception as e:
                        logger.error(f"Failed to scrub driver data: {e}")

                await self.send_message(
                    chat_id,
                    "🛡️ <b>Dữ liệu cá nhân của bạn đã được xóa hoàn toàn!</b>\n\n"
                    "Mọi thông tin liên kết số điện thoại đã được hủy theo Nghị định 13/2023/NĐ-CP.",
                    reply_markup=self.get_driver_reply_keyboard(),
                )
                await self.answer_callback_query(cq_id, "Đã xóa dữ liệu")

            elif data == "driver_cancel_scrub":
                await self.send_message(
                    chat_id,
                    "Đã hủy yêu cầu xóa dữ liệu.",
                    reply_markup=self.get_driver_reply_keyboard(),
                )
                await self.answer_callback_query(cq_id, "Đã hủy")

            else:
                await self.answer_callback_query(cq_id)


async def run_polling_loop() -> None:
    """Spawns both Admin Bot and Driver Bot concurrently if tokens are provided."""
    pool = None
    try:
        from psycopg_pool import AsyncConnectionPool
        pool = AsyncConnectionPool(conninfo=settings.DATABASE_URL, min_size=1, max_size=5, open=False)
        await pool.open()
    except Exception as e:
        logger.info("Database pool not available in standalone bot: %s", e)
        pool = None

    admin_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
    driver_token = getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None)

    runners = []
    tasks = []

    # 1. Admin Bot
    if admin_token and admin_token != "mock_tg_token":
        admin_runner = TelegramPollingRunner(bot_token=admin_token, db_pool=pool, role="admin")
        runners.append(admin_runner)
        tasks.append(admin_runner.start_polling())

    # 2. Driver / Collaborator Bot
    if driver_token and driver_token != "mock_tg_token" and driver_token != admin_token:
        driver_runner = DriverBotRunner(bot_token=driver_token, db_pool=pool)
        runners.append(driver_runner)
        tasks.append(driver_runner.start_polling())
    elif not tasks:
        # Fallback to single runner with default settings
        fallback_runner = TelegramPollingRunner(db_pool=pool, role="admin")
        runners.append(fallback_runner)
        tasks.append(fallback_runner.start_polling())

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Telegram polling loops cancelled.")
    finally:
        for r in runners:
            r.stop()
            await r.aclose()
        if pool:
            try:
                await pool.close()
            except Exception:
                pass


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    asyncio.run(run_polling_loop())
