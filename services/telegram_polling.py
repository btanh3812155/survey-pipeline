import os
import re
import json
import random
import asyncio
import logging
from typing import Optional, Union, Dict, Any
import httpx

from config import settings
try:
    from main import FORM_REGISTRY
except Exception:
    from services.form_engine import FORM_REGISTRY
from services.admin_moderator import (
    query_submission_dossier,
    render_dossier_message,
)

logger = logging.getLogger("telegram_polling")
DEV_STORAGE_FILE = os.path.join(os.getcwd(), "data", "dev_submissions.json")


class TelegramPollingRunner:
    """
    Standalone asynchronous Telegram Bot long-polling daemon.
    Polls getUpdates from Telegram Bot API and handles commands and callback queries.
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        polling_timeout: int = 30,
        db_pool=None,
    ):
        self.bot_token = bot_token or settings.TELEGRAM_BOT_TOKEN
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
            logger.info(f"[MOCK TG POLLING DISPATCH] to {chat_id}:\n{text}")
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

    async def get_stats_message(self) -> str:
        """Calculates survey submission counts across database or dev test storage."""
        total = 0
        approved = 0
        rejected = 0

        # Try database first if db_pool is available
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

        # Fallback to dev submissions storage file if total is 0
        if total == 0 and os.path.exists(DEV_STORAGE_FILE):
            try:
                with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                    records = json.load(f)
                    total = len(records)
                    for r in records:
                        st = str(r.get("status", "")).upper()
                        if st == "APPROVED":
                            approved += 1
                        elif st == "REJECTED":
                            rejected += 1
            except Exception as e:
                logger.warning(f"Could not read dev submissions file: {e}")

        pending = total - approved - rejected
        if pending < 0:
            pending = 0

        return (
            f"📊 <b>Thống Kê Khảo Sát Hệ Thống (Stats):</b>\n\n"
            f"• <b>Tổng số phiếu đã tiếp nhận:</b> {total}\n"
            f"• <b>Đã duyệt hợp lệ (Approved):</b> {approved}\n"
            f"• <b>Bị từ chối (Rejected):</b> {rejected}\n"
            f"• <b>Chờ kiểm tra thủ công (Review):</b> {pending}\n\n"
            f"<i>Cập nhật thời gian thực từ cơ sở dữ liệu.</i>"
        )

    async def handle_command(self, chat_id: Union[int, str], text: str) -> None:
        """Routes and executes slash commands."""
        clean_text = text.strip()
        cmd = clean_text.split()[0].lower() if clean_text else ""

        if cmd == "/start":
            welcome_msg = (
                "👋 <b>Chào mừng bạn đến với hệ thống kiểm duyệt khảo sát Trust Bridge!</b>\n\n"
                "Mọi phiếu khảo sát nộp qua form sẽ được tự động kiểm tra tuân thủ và phản hồi kết quả trực tiếp tại đây trong vòng 3 giây.\n\n"
                "🔗 <b>Link nộp biểu mẫu khảo sát thu nhập:</b>\n"
                "https://146.190.201.32.sslip.io/forms/income_survey\n\n"
                "👉 <i>Gõ /forms để xem danh sách biểu mẫu hoặc /stats để xem thống kê.</i>"
            )
            await self.send_message(chat_id, welcome_msg)

        elif cmd in ("/check", "/query"):
            parts = clean_text.split(" ", 1)
            if len(parts) < 2 or not parts[1].strip():
                await self.send_message(
                    chat_id,
                    "⚠️ <b>Vui lòng cung cấp mã phiếu cần tra cứu.</b>\n"
                    "Ví dụ: <code>/check VN-123456</code> hoặc <code>/query DEV-ABCD</code>",
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
            await self.send_message(chat_id, report_text)

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
                    lines.append(f"• <b>{fid}</b>: {title}\n  🔗 https://146.190.201.32.sslip.io/forms/{fid}")
            else:
                lines.append("• <b>income_survey</b>: Khảo Sát Thu Nhập & Bàn Giao Ca Xe\n  🔗 https://146.190.201.32.sslip.io/forms/income_survey")
            await self.send_message(chat_id, "\n".join(lines))

        else:
            help_msg = (
                "❓ <b>Lệnh không được nhận diện.</b>\n\n"
                "Các lệnh hỗ trợ:\n"
                "• <code>/start</code> - Thông tin bắt đầu & link nộp form\n"
                "• <code>/check &lt;id&gt;</code> hoặc <code>/query &lt;id&gt;</code> - Tra cứu hồ sơ phiếu\n"
                "• <code>/stats</code> - Xem thống kê tổng số, đã duyệt, từ chối\n"
                "• <code>/forms</code> - Xem danh sách các biểu mẫu đang mở"
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
            await self.send_message(
                cq_chat_id, f"✅ <b>Quản trị viên đã DUYỆT thủ công phiếu #{sub_id}.</b>"
            )
            await self.answer_callback_query(cq_id, f"Đã duyệt #{sub_id}")

        elif data.startswith("admin_reject:"):
            sub_id = data.split(":", 1)[1]
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
        else:
            await self.answer_callback_query(cq_id)

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
        if not self.bot_token or self.bot_token == "mock_tg_token":
            await asyncio.sleep(1.0)
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
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                sleep_sec = float(retry_after) if retry_after and retry_after.isdigit() else self.backoff_delay
                logger.warning(f"Telegram 429 Too Many Requests hit! Backing off for {sleep_sec:.1f}s")
                await asyncio.sleep(sleep_sec)
                self.backoff_delay = min(self.backoff_delay * 2, self.max_backoff)
                return 0
            elif resp.status_code >= 500:
                logger.warning(f"Telegram server error {resp.status_code}. Backing off for {self.backoff_delay:.1f}s")
                await asyncio.sleep(self.backoff_delay)
                self.backoff_delay = min(self.backoff_delay * 2, self.max_backoff)
                return 0
            elif resp.status_code != 200:
                logger.warning(f"Telegram getUpdates returned status {resp.status_code}")
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
                    logger.error(f"Error handling update {upd_id}: {ex}")

            return len(updates)
        except httpx.TimeoutException:
            return 0
        except Exception as e:
            logger.error(f"Telegram long-polling connection error: {e}")
            await asyncio.sleep(3.0)
            return 0

    async def start_polling(self) -> None:
        """Main long-polling loop."""
        self.is_running = True
        logger.info("Telegram long-polling runner loop started...")
        try:
            while self.is_running:
                await self.poll_once()
        finally:
            await self.aclose()

    def stop(self) -> None:
        """Signals the long-polling loop to terminate."""
        self.is_running = False


async def run_polling_loop(runner: Optional[TelegramPollingRunner] = None) -> None:
    active_runner = runner or TelegramPollingRunner()
    try:
        await active_runner.start_polling()
    except asyncio.CancelledError:
        logger.info("Telegram polling loop cancelled.")
    finally:
        active_runner.stop()
        await active_runner.aclose()


if __name__ == "__main__":
    asyncio.run(run_polling_loop())
