import os
import re
import io
import time
import json
import random
import asyncio
import logging
import html
from typing import Optional, Union, Dict, Any, List, Tuple
import httpx
from PIL import Image

from config import settings
from services.form_engine import FORM_REGISTRY
from services.admin_moderator import (
    query_submission_dossier,
    render_dossier_message,
)
from services.normalizer import compute_laplacian_blur_score

logger = logging.getLogger("telegram_polling")
DEV_STORAGE_FILE = os.path.join(os.getcwd(), "data", "dev_submissions.json")

# Shared live support conversation registry between Admin Group and User Private DMs
ACTIVE_SUPPORT_CONVERSATIONS: Dict[int, dict] = {}  # user_id -> conversation metadata
ADMIN_GROUP_ACTIVE_SESSION: Dict[str, int] = {}     # legacy chat/admin identifier -> user_id
ADMIN_ACTIVE_CONVERSATIONS: Dict[int, int] = {}     # admin_user_id -> user_id (isolated per-admin routing)

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
        # Chat-scoped registry of the last prompt message per logical content key,
        # so a new instance can strip the old one's inline buttons.
        self.prompt_registry: Dict[str, List[int]] = {}

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
        return_message: bool = False,
    ):
        """Dispatches an HTML or text message to a given chat_id via Telegram Bot API."""
        if not self.bot_token or self.bot_token == "mock_tg_token":
            logger.info(f"[MOCK TG {self.role.upper()} DISPATCH] to {chat_id}:\n{text}")
            return {"message_id": None} if return_message else True

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
            if resp.status_code != 200:
                return False
            if not return_message:
                return True
            return resp.json().get("result") or {}
        except Exception as e:
            logger.error(f"Failed to send Telegram message to {chat_id}: {e}")
            return False

    async def delete_message(self, chat_id: Union[int, str], message_id: int) -> bool:
        """Deletes a single Telegram message (used to replace repeated /check responses)."""
        if not message_id or not self.bot_token or self.bot_token.lower() in ("mock_tg_token", "mock", "none", ""):
            return True
        try:
            client = await self.get_client()
            resp = await client.post(
                f"https://api.telegram.org/bot{self.bot_token}/deleteMessage",
                json={"chat_id": str(chat_id), "message_id": int(message_id)},
                timeout=10.0,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.debug(f"Failed to delete message {message_id}: {e}")
            return False

    async def clear_previous_prompt(self, key: str, chat_id: Union[int, str]) -> None:
        """Deletes the previous response (message or media group) for this content key."""
        previous = self.prompt_registry.pop(key, None)
        if not previous:
            return
        ids = previous if isinstance(previous, list) else [previous]
        for message_id in ids:
            if message_id:
                await self.delete_message(chat_id, message_id)

    async def send_tracked(self, key: str, chat_id: Union[int, str], text: str, reply_markup: Optional[dict] = None):
        """Sends a message, first stripping buttons from the previous message with the same key."""
        await self.clear_previous_prompt(key, chat_id)
        result = await self.send_message(chat_id, text, reply_markup=reply_markup, return_message=True)
        if isinstance(result, dict) and result.get("message_id"):
            self.prompt_registry[key] = [result["message_id"]]
        return result

    async def send_dossier_bundle(
        self,
        chat_id: Union[int, str],
        key: str,
        dossier: dict,
        caption: str,
        txt_bytes: bytes,
        reply_markup: Optional[dict] = None,
    ) -> bool:
        """
        Sends ONE response: all submission photos + the raw dossier .txt in a media
        group, with the short caption (sub id + CTV) on the first item and the review
        buttons attached to that item. Falls back to a document-only message.
        """
        await self.clear_previous_prompt(key, chat_id)
        if not self.bot_token or self.bot_token.lower() in ("mock_tg_token", "mock", "none", ""):
            logger.info(f"[MOCK TG DISPATCH] dossier bundle for {dossier.get('submission_id')}: {caption}")
            return True

        files: Dict[str, Any] = {}
        media: List[dict] = []
        for idx, path in enumerate((dossier.get("photo_paths") or [])[:9]):
            try:
                with open(path, "rb") as fp:
                    raw_b = fp.read()
                if len(raw_b) > 300_000:
                    try:
                        im = Image.open(io.BytesIO(raw_b))
                        if max(im.size) > 1200:
                            im.thumbnail((1200, 1200), Image.Resampling.BILINEAR)
                        out_io = io.BytesIO()
                        im.convert("RGB").save(out_io, format="JPEG", quality=82, optimize=True)
                        raw_b = out_io.getvalue()
                    except Exception:
                        pass
                files[f"photo_{idx}"] = (os.path.basename(path), raw_b, "image/jpeg")
                media.append({"type": "photo", "media": f"attach://photo_{idx}"})
            except Exception as e:
                logger.warning(f"Failed reading dossier photo {path}: {e}")

        sub_id = str(dossier.get("submission_id") or "submission")
        files["dossier_txt"] = (f"{sub_id}.txt", txt_bytes, "text/plain")
        if media:
            media[0]["caption"] = caption
            media[0]["parse_mode"] = "HTML"

        client = await self.get_client()
        try:
            if not media:
                resp = await client.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendDocument",
                    data={
                        "chat_id": str(chat_id),
                        "caption": caption,
                        "parse_mode": "HTML",
                        **({"reply_markup": json.dumps(reply_markup)} if reply_markup else {}),
                    },
                    files={"document": files["dossier_txt"]},
                    timeout=30.0,
                )
                if resp.status_code != 200:
                    logger.error(f"Dossier document send failed: {resp.status_code} {resp.text[:200]}")
                    return False
                result = resp.json().get("result") or {}
                ids = [result.get("message_id")]
            else:
                # Telegram forbids mixing documents with photos in one media group.
                # Both payloads are fully prepared above, so dispatch the image media
                # group and the raw-data document together, in parallel.
                photo_files = {k: v for k, v in files.items() if k != "dossier_txt"}

                async def send_images():
                    return await client.post(
                        f"https://api.telegram.org/bot{self.bot_token}/sendMediaGroup",
                        data={"chat_id": str(chat_id), "media": json.dumps(media)},
                        files=photo_files,
                        timeout=30.0,
                    )

                async def send_data():
                    return await client.post(
                        f"https://api.telegram.org/bot{self.bot_token}/sendDocument",
                        data={"chat_id": str(chat_id)},
                        files={"document": files["dossier_txt"]},
                        timeout=30.0,
                    )

                images_res, data_res = await asyncio.gather(
                    send_images(), send_data(), return_exceptions=True
                )
                ids = []
                if isinstance(images_res, Exception) or getattr(images_res, "status_code", None) != 200:
                    logger.error(f"Dossier media group send failed: {images_res}")
                else:
                    ids = [m.get("message_id") for m in (images_res.json().get("result") or [])]
                if isinstance(data_res, Exception) or getattr(data_res, "status_code", None) != 200:
                    logger.error(f"Dossier txt send failed: {data_res}")
                else:
                    doc_msg = data_res.json().get("result") or {}
                    if doc_msg.get("message_id"):
                        ids.append(doc_msg["message_id"])
                if not ids:
                    return False
                if reply_markup and ids[0]:
                    await self.edit_message_reply_markup(chat_id, ids[0], reply_markup=reply_markup)
            self.prompt_registry[key] = [mid for mid in ids if mid]
            return True
        except Exception as e:
            logger.error(f"Failed sending dossier bundle: {e}")
            return False

    async def answer_callback_query(
        self, callback_query_id: str, text: Optional[str] = None, show_alert: bool = False
    ) -> bool:
        """Acknowledges an inline callback query with optional pop-up alert."""
        if not self.bot_token or self.bot_token == "mock_tg_token":
            return True
        client = await self.get_client()
        url = f"https://api.telegram.org/bot{self.bot_token}/answerCallbackQuery"
        payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        if show_alert:
            payload["show_alert"] = True
        try:
            resp = await client.post(url, json=payload)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Failed to answer callback query {callback_query_id}: {e}")
            return False

    def prepare_media_group_payload(self, photo_paths: List[str]) -> Optional[dict]:
        """Loads and pre-compresses photos into lightweight memory buffers for instant Telegram transmission."""
        if not photo_paths:
            return None
        selected = [p for p in photo_paths[:10] if os.path.exists(p) and os.path.getsize(p) > 0]
        if not selected:
            return None

        files = {}
        media = []
        for idx, p in enumerate(selected):
            key = f"photo_{idx}"
            try:
                with open(p, "rb") as fp:
                    raw_b = fp.read()
                # Fast downscale if oversized (> 300KB) to ensure sub-second transmission
                if len(raw_b) > 300_000:
                    try:
                        im = Image.open(io.BytesIO(raw_b))
                        if max(im.size) > 1200:
                            im.thumbnail((1200, 1200), Image.Resampling.BILINEAR)
                        out_io = io.BytesIO()
                        im.convert("RGB").save(out_io, format="JPEG", quality=82, optimize=True)
                        raw_b = out_io.getvalue()
                    except Exception:
                        pass
                files[key] = (os.path.basename(p), raw_b, "image/jpeg")
                media.append({"type": "photo", "media": f"attach://{key}"})
            except Exception as e:
                logger.warning(f"Error reading photo for media group: {e}")

        if not files:
            return None
        return {"files": files, "media": media}

    async def send_prepared_media_group(self, chat_id: Union[int, str], payload: dict) -> bool:
        """Dispatches an already prepared in-memory media group to Telegram."""
        if not self.bot_token or self.bot_token.lower() in ("mock_tg_token", "mock", "none", ""):
            return True
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMediaGroup"
        try:
            client = await self.get_client()
            resp = await client.post(
                url,
                data={"chat_id": str(chat_id), "media": json.dumps(payload["media"])},
                files=payload["files"],
                timeout=30.0,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Failed to send prepared media group: {e}")
            return False

    async def send_media_group(self, chat_id: Union[int, str], photo_paths: List[str]) -> bool:
        """Convenience helper to prepare and send media group."""
        payload = self.prepare_media_group_payload(photo_paths)
        if payload:
            return await self.send_prepared_media_group(chat_id, payload)
        return False

    async def edit_message_reply_markup(
        self,
        chat_id: Union[int, str],
        message_id: int,
        reply_markup: Optional[dict] = None,
    ) -> bool:
        """Updates or removes inline keyboard buttons on an existing message."""
        if not message_id or not self.bot_token or self.bot_token.lower() in ("mock_tg_token", "mock", "none", ""):
            return True

        client = await self.get_client()
        url = f"https://api.telegram.org/bot{self.bot_token}/editMessageReplyMarkup"
        payload = {
            "chat_id": str(chat_id),
            "message_id": message_id,
            "reply_markup": reply_markup or {"inline_keyboard": []},
        }
        try:
            resp = await client.post(url, json=payload, timeout=10.0)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Failed to edit message reply markup: {e}")
            return False

    async def execute_submission_review(self, chat_id: Union[int, str], sub_id: str) -> None:
        """
        Unified review handler invoked identically by /check command and [📋 Mở Phiếu] button.
        Loads media group and review text in parallel, waiting for both to be ready before dispatching.
        """
        clean_sub_id = sub_id.strip().lstrip("#")
        dossier = await query_submission_dossier(clean_sub_id, db_pool=self.db_pool)
        if "error" in dossier:
            await self.send_message(chat_id, f"❌ {dossier['error']}")
            return

        cur_status = dossier.get("status", "PENDING")
        ctv = dossier.get("ctv_code") or dossier.get("collaborator_id") or "N/A"
        caption = (
            f"📋 <b>HỒ SƠ KHẢO SÁT #{html.escape(str(clean_sub_id))}</b>\n"
            f"• <b>CTV:</b> <code>{html.escape(str(ctv))}</code>"
        )
        raw_data = {k: v for k, v in dossier.items() if k != "photo_paths"}
        txt_bytes = json.dumps(raw_data, ensure_ascii=False, indent=2, default=str).encode("utf-8")

        if cur_status in ("APPROVED", "REJECTED"):
            status_text = "Đã DUYỆT (APPROVED)" if cur_status == "APPROVED" else "Đã TỪ CHỐI (REJECTED)"
            buttons = {
                "inline_keyboard": [
                    [{"text": f"🔒 {status_text} - Đã hoàn tất", "callback_data": "resolved_notice"}],
                ]
            }
        else:
            buttons = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Duyệt (Approve)", "callback_data": f"admin_req_approve:{clean_sub_id}"},
                        {"text": "❌ Từ chối (Reject)", "callback_data": f"admin_req_reject:{clean_sub_id}"},
                    ],
                ]
            }

        await self.send_dossier_bundle(
            chat_id,
            f"dossier:{chat_id}:{dossier.get('submission_id')}",
            dossier,
            caption,
            txt_bytes,
            reply_markup=buttons,
        )
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

    async def handle_command(
        self,
        chat_id: Union[int, str],
        text: str,
        reply_to_message: Optional[dict] = None,
    ) -> None:
        """Routes and executes admin slash commands."""
        clean_text = text.strip()
        cmd = clean_text.split()[0].lower() if clean_text else ""

        if cmd in ("/check", "/query"):
            parts = clean_text.split(" ", 1)
            sub_id = None
            if len(parts) >= 2 and parts[1].strip():
                sub_id = parts[1].strip().lstrip("#")
            elif reply_to_message:
                reply_text = reply_to_message.get("text") or reply_to_message.get("caption") or ""
                m = re.search(r"#?((?:VN|DEV)-[A-Za-z0-9_-]+)", reply_text)
                if m:
                    sub_id = m.group(1).lstrip("#")

            if not sub_id:
                reply_markup = {
                    "inline_keyboard": [
                        [
                            {"text": "✍️ Điền /check <mã_phiếu>", "switch_inline_query_current_chat": "/check "}
                        ]
                    ]
                }
                await self.send_message(
                    chat_id,
                    "⚠️ <b>Vui lòng cung cấp mã phiếu cần kiểm tra (/check).</b>\n\n"
                    "• Bấm nút bên dưới để tự động điền: <code>/check &lt;mã_phiếu&gt;</code>\n"
                    "• Hoặc <b>reply (trả lời)</b> trực tiếp vào tin nhắn thông báo phiếu bằng lệnh <code>/check</code>!",
                    reply_markup=reply_markup,
                )
                return

            if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", sub_id):
                await self.send_message(
                    chat_id,
                    "⚠️ <b>Mã phiếu không đúng định dạng.</b>\n"
                    "Chỉ chấp nhận chữ cái, số và dấu gạch ngang (tối đa 64 ký tự).",
                )
                return

            await self.execute_submission_review(chat_id, sub_id)
        elif cmd in ("/start", "/help"):
            help_msg = (
                "🤖 <b>Bot Điều Hành Trust Bridge (@TrustBridgeVNAdmin_Bot)</b>\n\n"
                "Chào mừng bạn đến với hệ thống kiểm định khảo sát Trust Bridge!\n\n"
                "Lệnh điều hành:\n"
                "• <code>/check &lt;mã_phiếu&gt;</code> - Mở hồ sơ & ảnh phiếu khảo sát để xét duyệt\n"
                "• <code>/stats</code> - Xem thống kê tiến độ duyệt phiếu\n"
                "• <code>/close</code> - Kết thúc phiên chat hỗ trợ trực tiếp\n\n"
                "👉 <i>Mẹo: Bạn có thể reply (trả lời) trực tiếp tin nhắn thông báo bằng <code>/check</code>!</i>"
            )
            await self.send_message(chat_id, help_msg)
        else:
            help_msg = (
                "🤖 <b>Bot Điều Hành Trust Bridge (@TrustBridgeVNAdmin_Bot)</b>\n\n"
                "Lệnh điều hành:\n"
                "• <code>/check &lt;mã_phiếu&gt;</code> - Mở hồ sơ & ảnh phiếu để xét duyệt\n\n"
                "👉 <i>Mẹo: Bạn có thể reply (trả lời) trực tiếp tin nhắn thông báo bằng <code>/check</code>!</i>"
            )
            await self.send_message(chat_id, help_msg)
    async def handle_callback_query(self, cq: dict) -> None:
        """Processes inline keyboard button interactions for admin audits."""
        cq_id = cq.get("id", "")
        data = cq.get("data", "")
        cq_msg = cq.get("message") or {}
        cq_chat = cq_msg.get("chat") or {}
        cq_chat_id = cq_chat.get("id")
        from_user = cq.get("from") or {}
        msg_id = cq_msg.get("message_id")
        if not cq_chat_id:
            await self.answer_callback_query(cq_id)
            return

        if data.startswith("admin_review:") or data.startswith("admin_query:"):
            sub_id = data.split(":", 1)[1]
            await self.execute_submission_review(cq_chat_id, sub_id)
            await self.answer_callback_query(cq_id, f"Đã mở phiếu #{sub_id}")
        elif data.startswith("admin_req_approve:"):
            sub_id = data.split(":", 1)[1]
            confirm_buttons = {
                "inline_keyboard": [
                    [
                        {"text": f"⚠️ Xác nhận DUYỆT phiếu #{sub_id}?", "callback_data": "none"},
                    ],
                    [
                        {"text": "✅ ĐỒNG Ý DUYỆT", "callback_data": f"admin_approve:{sub_id}"},
                        {"text": "↩️ HỦY BỎ", "callback_data": f"admin_cancel_decision:{sub_id}"},
                    ],
                ]
            }
            if msg_id:
                await self.edit_message_reply_markup(cq_chat_id, msg_id, reply_markup=confirm_buttons)
            await self.answer_callback_query(cq_id, "Vui lòng xác nhận để hoàn tất duyệt phiếu.")

        elif data.startswith("admin_req_reject:"):
            sub_id = data.split(":", 1)[1]
            confirm_buttons = {
                "inline_keyboard": [
                    [
                        {"text": f"⚠️ Xác nhận TỪ CHỐI phiếu #{sub_id}?", "callback_data": "none"},
                    ],
                    [
                        {"text": "❌ ĐỒNG Ý TỪ CHỐI", "callback_data": f"admin_reject:{sub_id}"},
                        {"text": "↩️ HỦY BỎ", "callback_data": f"admin_cancel_decision:{sub_id}"},
                    ],
                ]
            }
            if msg_id:
                await self.edit_message_reply_markup(cq_chat_id, msg_id, reply_markup=confirm_buttons)
            await self.answer_callback_query(cq_id, "Vui lòng xác nhận để hoàn tất từ chối phiếu.")

        elif data.startswith("admin_cancel_decision:"):
            sub_id = data.split(":", 1)[1]
            restore_buttons = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Duyệt (Approve)", "callback_data": f"admin_req_approve:{sub_id}"},
                        {"text": "❌ Từ chối (Reject)", "callback_data": f"admin_req_reject:{sub_id}"},
                    ],
                ]
            }
            if msg_id:
                await self.edit_message_reply_markup(cq_chat_id, msg_id, reply_markup=restore_buttons)
            await self.answer_callback_query(cq_id, "Đã hủy thao tác.")

        elif data.startswith("admin_approve:"):
            sub_id = data.split(":", 1)[1]
            success, final_status = await self._update_submission_status(sub_id, "APPROVED", reason="Quản trị viên phê duyệt thủ công")
            msg_id = cq_msg.get("message_id")
            if not success:
                await self.answer_callback_query(cq_id, f"⚠️ Phiếu #{sub_id} đã được xử lý ({final_status}) trước đó! Không thể thay đổi trạng thái.", show_alert=True)
                if msg_id:
                    await self.edit_message_reply_markup(cq_chat_id, msg_id, reply_markup={"inline_keyboard": [[{"text": f"🔒 Đã xử lý ({final_status})", "callback_data": "resolved_notice"}]]})
                return

            await self.send_message(
                cq_chat_id, f"✅ <b>Quản trị viên đã DUYỆT thủ công phiếu #{sub_id}. (Đã đánh dấu hoàn tất)</b>"
            )
            await self.answer_callback_query(cq_id, f"Đã duyệt #{sub_id}")
            if msg_id:
                await self.edit_message_reply_markup(cq_chat_id, msg_id, reply_markup={"inline_keyboard": [[{"text": "🔒 Đã DUYỆT (APPROVED) - Hoàn tất", "callback_data": "resolved_notice"}]]})
            await self._notify_parties_of_decision(sub_id, "APPROVED", reason="Quản trị viên phê duyệt thủ công")

        elif data.startswith("admin_reject:"):
            sub_id = data.split(":", 1)[1]
            success, final_status = await self._update_submission_status(sub_id, "REJECTED", reason="Quản trị viên từ chối thủ công")
            msg_id = cq_msg.get("message_id")
            if not success:
                await self.answer_callback_query(cq_id, f"⚠️ Phiếu #{sub_id} đã được xử lý ({final_status}) trước đó! Không thể thay đổi trạng thái.", show_alert=True)
                if msg_id:
                    await self.edit_message_reply_markup(cq_chat_id, msg_id, reply_markup={"inline_keyboard": [[{"text": f"🔒 Đã xử lý ({final_status})", "callback_data": "resolved_notice"}]]})
                return

            await self.send_message(
                cq_chat_id, f"❌ <b>Quản trị viên đã TỪ CHỐI phiếu #{sub_id}. (Đã đánh dấu hoàn tất)</b>"
            )
            await self.answer_callback_query(cq_id, f"Đã từ chối #{sub_id}")
            if msg_id:
                await self.edit_message_reply_markup(cq_chat_id, msg_id, reply_markup={"inline_keyboard": [[{"text": "🔒 Đã TỪ CHỐI (REJECTED) - Hoàn tất", "callback_data": "resolved_notice"}]]})
            await self._notify_parties_of_decision(sub_id, "REJECTED", reason="Quản trị viên từ chối thủ công")

        elif data == "resolved_notice":
            await self.answer_callback_query(cq_id, "Phiếu này đã được xử lý hoàn tất và bị khóa, không nhận phản hồi thêm.", show_alert=True)
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

        # /audit button removed as requested
        elif data.startswith("admin_reply_chat:"):
            target_user_id = int(data.split(":", 1)[1])
            admin_id = from_user.get("id")
            admin_name = from_user.get("first_name", "Admin")

            # 1. Check if this support request is already locked by another admin!
            existing = ACTIVE_SUPPORT_CONVERSATIONS.get(target_user_id)
            if existing and existing.get("locked_admin_id"):
                locked_id = existing["locked_admin_id"]
                locked_name = existing.get("locked_admin_name", "quản trị viên khác")
                if locked_id != admin_id:
                    await self.answer_callback_query(
                        cq_id,
                        f"⚠️ Yêu cầu hỗ trợ này đã được khóa và đang xử lý bởi {locked_name}! Chỉ {locked_name} mới có quyền nhắn tin.",
                        show_alert=True
                    )
                    return

            # 2. Enforce 1-admin-1-convo rule: check if this admin is already holding another active conversation!
            if admin_id and admin_id in ADMIN_ACTIVE_CONVERSATIONS:
                held_user_id = ADMIN_ACTIVE_CONVERSATIONS[admin_id]
                if held_user_id != target_user_id and held_user_id in ACTIVE_SUPPORT_CONVERSATIONS:
                    await self.answer_callback_query(
                        cq_id,
                        f"⚠️ Bạn đang trong phiên hỗ trợ với thành viên khác (ID: {held_user_id})!\n\n"
                        f"Mỗi quản trị viên chỉ được giữ 1 phiên hỗ trợ tại một thời điểm.\n"
                        f"Vui lòng gõ /close để hoàn tất phiên hiện tại trước khi nhận phiên mới.",
                        show_alert=True,
                    )
                    return
                elif held_user_id != target_user_id:
                    del ADMIN_ACTIVE_CONVERSATIONS[admin_id]
            # Lock session to this admin only!
            ACTIVE_SUPPORT_CONVERSATIONS[target_user_id] = {
                "user_id": target_user_id,
                "admin_chat_id": cq_chat_id,
                "locked_admin_id": admin_id,
                "locked_admin_name": admin_name,
                "opened_at": time.time(),
            }
            if admin_id:
                ADMIN_ACTIVE_CONVERSATIONS[admin_id] = target_user_id
                ADMIN_GROUP_ACTIVE_SESSION[str(admin_id)] = target_user_id
            ADMIN_GROUP_ACTIVE_SESSION[str(cq_chat_id)] = target_user_id
            ADMIN_GROUP_ACTIVE_SESSION[cq_chat_id] = target_user_id
            # Update ticket message buttons in group to show locked state!
            if msg_id:
                locked_ticket_markup = {
                    "inline_keyboard": [
                        [
                            {"text": f"🔒 Đang hỗ trợ bởi {admin_name}", "callback_data": f"admin_locked_info:{target_user_id}"},
                        ],
                    ]
                }
                await self.edit_message_reply_markup(cq_chat_id, msg_id, reply_markup=locked_ticket_markup)

            await self.send_message(
                cq_chat_id,
                f"🟢 <b>ĐÃ MỞ & KHÓA PHIÊN HỘI THOẠI TRỰC TIẾP (ID: <code>{target_user_id}</code>) CHO {admin_name}!</b>\n\n"
                f"👉 Phiên chat này đã được <b>khóa riêng cho {admin_name}</b>. Tin nhắn từ quản trị viên khác trong nhóm sẽ không chuyển tiếp tới người dùng.\n"
                f"👉 {admin_name} gõ <code>/close</code> để kết thúc phiên bất cứ lúc nào.",
            )
            await self.answer_callback_query(cq_id, f"Đã khóa phiên hỗ trợ cho {admin_name}")
            driver_token = getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
            if driver_token and driver_token != "mock_tg_token":
                client = await self.get_client()
                url = f"https://api.telegram.org/bot{driver_token}/sendMessage"
                user_msg = (
                    "🟢 <b>Quản trị viên đã tham gia cuộc hội thoại hỗ trợ trực tiếp!</b>\n\n"
                    "Bạn có thể nhắn tin trao đổi tại đây. Quản trị viên sẽ đọc và trả lời bạn ngay trong khung chat này.\n"
                    "👉 Gõ <code>/close</code> khi đã giải quyết xong để kết thúc phiên hỗ trợ."
                )
                try:
                    await client.post(url, json={"chat_id": target_user_id, "text": user_msg, "parse_mode": "HTML"})
                except Exception as ex:
                    logger.error(f"Failed to notify user about open chat: {ex}")

        elif data.startswith("admin_close_ticket:"):
            target_user_id = int(data.split(":", 1)[1])
            convo = ACTIVE_SUPPORT_CONVERSATIONS.pop(target_user_id, None)
            locked_admin = convo.get("locked_admin_id") if convo else None
            if locked_admin and locked_admin in ADMIN_ACTIVE_CONVERSATIONS:
                del ADMIN_ACTIVE_CONVERSATIONS[locked_admin]
            if locked_admin and str(locked_admin) in ADMIN_GROUP_ACTIVE_SESSION:
                del ADMIN_GROUP_ACTIVE_SESSION[str(locked_admin)]
            if str(cq_chat_id) in ADMIN_GROUP_ACTIVE_SESSION:
                del ADMIN_GROUP_ACTIVE_SESSION[str(cq_chat_id)]
            await self.send_message(
                cq_chat_id,
                f"🔒 Đã đóng yêu cầu hỗ trợ của thành viên <code>{target_user_id}</code>."
            )
            await self.answer_callback_query(cq_id, "Đã đóng ticket")
        else:
            await self.answer_callback_query(cq_id)
    async def _update_submission_status(self, sub_id: str, new_status: str, reason: str = "") -> Tuple[bool, str]:
        """
        Updates submission status in DB and dev storage.
        If the submission was already resolved (APPROVED or REJECTED), refuses to take further responses!
        Returns (success: bool, current_status: str).
        """
        if self.db_pool:
            try:
                async with self.db_pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT status FROM survey_submissions WHERE submission_id = %s FOR UPDATE;",
                            (sub_id,),
                        )
                        row = await cur.fetchone()
                        if row:
                            current_status = row[0]
                            if current_status in ("APPROVED", "REJECTED"):
                                return False, current_status

                        await cur.execute(
                            "UPDATE survey_submissions SET status = %s, rejection_reason = %s WHERE submission_id = %s;",
                            (new_status, reason or None, sub_id),
                        )
                    await conn.commit()
                    return True, new_status
            except Exception as e:
                logger.error(f"Failed to update submission status in DB: {e}")

        for fpath in (
            os.path.join(os.getcwd(), "data", "dev_test_submissions.json"),
            os.path.join(os.getcwd(), "data", "dev_submissions.json"),
        ):
            if os.path.exists(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        records = json.load(f)
                    for r in records:
                        if r.get("submission_id") == sub_id:
                            current_status = r.get("status")
                            if current_status in ("APPROVED", "REJECTED"):
                                return False, current_status
                            r["status"] = new_status
                            r["reason"] = reason
                            with open(fpath, "w", encoding="utf-8") as f_out:
                                json.dump(records, f_out, ensure_ascii=False, indent=2)
                            return True, new_status
                except Exception as e:
                    logger.error(f"Failed to update submission status in {fpath}: {e}")
        return True, new_status
    async def _notify_parties_of_decision(
        self, sub_id: str, new_status: str, reason: str = ""
    ) -> None:
        """Dispatches decision updates directly to Collaborator (CTV) and Driver."""
        clean_sub_id = sub_id.strip().lstrip("#")
        dossier = await query_submission_dossier(clean_sub_id, db_pool=self.db_pool)
        if not dossier or "error" in dossier:
            logger.warning(f"Could not load dossier for #{clean_sub_id} to dispatch decision notification.")
            return

        driver_phone = dossier.get("driver_phone")
        driver_name = dossier.get("driver_name")
        collab_id = dossier.get("collaborator_id") or dossier.get("ctv_code")
        gross_rev = dossier.get("gross_income") or 0
        effective_reason = reason or dossier.get("rejection_reason") or ""

        try:
            try:
                from routers.survey import dispatch_submission_telegram_notifications
            except ImportError:
                from main import dispatch_submission_telegram_notifications
            await dispatch_submission_telegram_notifications(
                submission_id=clean_sub_id,
                status_str=new_status,
                driver_phone=driver_phone,
                driver_name=driver_name,
                collaborator_id=collab_id,
                reason_str=effective_reason,
                gross_revenue=gross_rev,
                form_id=dossier.get("form_id"),
            )
            logger.info(f"Dispatched decision verdict for #{clean_sub_id} to CTV ({collab_id}) and driver ({driver_phone})")
        except Exception as e:
            logger.error(f"Failed to dispatch decision notification for #{clean_sub_id}: {e}")
    async def close_active_support(
        self,
        admin_chat_id: int,
        user_id: int,
        closed_by: str = "admin",
        admin_user_id: Optional[int] = None,
    ) -> None:
        """Closes live support conversation from admin side for the specific admin session."""
        convo = ACTIVE_SUPPORT_CONVERSATIONS.pop(user_id, None)
        locked_admin = convo.get("locked_admin_id") if convo else admin_user_id
        if locked_admin and locked_admin in ADMIN_ACTIVE_CONVERSATIONS:
            del ADMIN_ACTIVE_CONVERSATIONS[locked_admin]
        if locked_admin and str(locked_admin) in ADMIN_GROUP_ACTIVE_SESSION:
            del ADMIN_GROUP_ACTIVE_SESSION[str(locked_admin)]
        if str(admin_chat_id) in ADMIN_GROUP_ACTIVE_SESSION:
            del ADMIN_GROUP_ACTIVE_SESSION[str(admin_chat_id)]

        await self.send_message(
            admin_chat_id,
            f"🔒 <b>Đã đóng phiên hội thoại hỗ trợ với người dùng <code>{user_id}</code>.</b>"
        )
        driver_token = getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
        if driver_token and driver_token != "mock_tg_token":
            client = await self.get_client()
            url = f"https://api.telegram.org/bot{driver_token}/sendMessage"
            msg = (
                "🔒 <b>Phiên hỗ trợ trực tiếp với Ban Quản Trị đã kết thúc.</b>\n\n"
                "Cảm ơn bạn! Nếu có bất kỳ câu hỏi nào trong tương lai, bạn có thể gửi lại yêu cầu bất cứ lúc nào."
            )
            try:
                await client.post(url, json={"chat_id": user_id, "text": msg, "parse_mode": "HTML"})
            except Exception as e:
                logger.error(f"Failed to notify user about closed session: {e}")


    async def handle_update(self, update: dict) -> None:
        """Dispatches an incoming update payload to message or callback handlers."""
        if "my_chat_member" in update:
            mcm = update["my_chat_member"]
            chat = mcm.get("chat") or {}
            chat_id = chat.get("id")
            title = chat.get("title") or "Nhóm Quản Trị"
            logger.info(f"🌟 [{self.role.upper()} BOT JOINED CHAT] '{title}' (ID: {chat_id})")
            if chat_id and self.role == "admin":
                await self.send_message(
                    chat_id,
                    f"👋 <b>Trust Bridge Admin Bot đã kết nối thành công vào nhóm!</b>\n\n"
                    f"• <b>Tên nhóm:</b> {title}\n"
                    f"• <b>Group Chat ID:</b> <code>{chat_id}</code>\n\n"
                    f"👉 Hãy lưu mã <code>{chat_id}</code> vào cấu hình <code>TELEGRAM_ADMIN_GROUP_ID</code> trên máy chủ để nhận thông báo kiểm định!",
                )

        if "message" in update and update["message"]:
            msg = update["message"]
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            text = msg.get("text") or ""
            from_user = msg.get("from") or {}
            admin_name = from_user.get("first_name", "Quản trị viên")

            if chat_id and text:
                from_user_id = from_user.get("id")

                # Check for /close command in admin group - closes ONLY the active session of this specific admin!
                if text.strip().lower() in ("/close", "/end", "/ketthuc"):
                    active_user = (
                        (ADMIN_ACTIVE_CONVERSATIONS.get(from_user_id) if from_user_id else None)
                        or (ADMIN_GROUP_ACTIVE_SESSION.get(str(from_user_id)) if from_user_id else None)
                        or ADMIN_GROUP_ACTIVE_SESSION.get(str(chat_id))
                    )
                    if active_user:
                        await self.close_active_support(chat_id, active_user, closed_by="admin", admin_user_id=from_user_id)
                        return
                    else:
                        await self.send_message(chat_id, "ℹ️ Hiện bạn không có phiên hội thoại hỗ trợ nào đang mở.")
                        return

                # Check if message is replying to a support ticket, or if session is active
                reply_to = msg.get("reply_to_message") or {}
                reply_text = reply_to.get("text") or reply_to.get("caption") or ""
                target_user = None

                if reply_text:
                    m = re.search(r"(?:Chat ID|User ID|ID):\s*(?:<code>)?(\d+)(?:</code>)?", reply_text, re.IGNORECASE)
                    if m:
                        target_user = int(m.group(1))

                # Guard cross-admin leakage:
                if target_user:
                    convo = ACTIVE_SUPPORT_CONVERSATIONS.get(target_user)
                    if convo and convo.get("locked_admin_id"):
                        if from_user_id and from_user_id != convo["locked_admin_id"]:
                            logger.info(
                                f"Blocked cross-convo reply: admin {admin_name} ({from_user_id}) tried to message user {target_user} "
                                f"locked to {convo.get('locked_admin_name')} ({convo.get('locked_admin_id')})"
                            )
                            return
                    else:
                        # 1-admin-1-convo: If admin attempts to start a new convo via reply, ensure they don't already hold one!
                        if from_user_id and from_user_id in ADMIN_ACTIVE_CONVERSATIONS:
                            held = ADMIN_ACTIVE_CONVERSATIONS[from_user_id]
                            if held != target_user and held in ACTIVE_SUPPORT_CONVERSATIONS:
                                logger.info(
                                    f"Blocked admin {admin_name} ({from_user_id}) from taking user {target_user} "
                                    f"because they already hold user {held}"
                                )
                                await self.send_message(
                                    chat_id,
                                    f"⚠️ {admin_name}, bạn đang trong phiên hỗ trợ với thành viên <code>{held}</code>!\n"
                                    f"Mỗi quản trị viên chỉ được hỗ trợ 1 thành viên tại một thời điểm. "
                                    f"Vui lòng gõ <code>/close</code> phiên hiện tại trước khi nhận phiên mới.",
                                )
                                return
                else:
                    # Message is typed into group without replying: route EXCLUSIVELY by this admin's active session!
                    if from_user_id and from_user_id in ADMIN_ACTIVE_CONVERSATIONS:
                        target_user = ADMIN_ACTIVE_CONVERSATIONS[from_user_id]
                    elif from_user_id and str(from_user_id) in ADMIN_GROUP_ACTIVE_SESSION:
                        target_user = ADMIN_GROUP_ACTIVE_SESSION[str(from_user_id)]
                    elif not ACTIVE_SUPPORT_CONVERSATIONS and (ADMIN_GROUP_ACTIVE_SESSION.get(str(chat_id)) or ADMIN_GROUP_ACTIVE_SESSION.get(chat_id)):
                        # Fall back to group-level only if no conflicting active conversations exist
                        target_user = ADMIN_GROUP_ACTIVE_SESSION.get(str(chat_id)) or ADMIN_GROUP_ACTIVE_SESSION.get(chat_id)

                if target_user and not text.startswith("/"):
                    convo = ACTIVE_SUPPORT_CONVERSATIONS.get(target_user)

                    # If session is locked to a specific admin, only relay messages from that admin!
                    if convo and convo.get("locked_admin_id"):
                        if from_user_id and from_user_id != convo["locked_admin_id"]:
                            logger.info(
                                f"Ignored group message from non-assigned admin {admin_name} ({from_user_id}) "
                                f"for session locked to {convo.get('locked_admin_name')} ({convo.get('locked_admin_id')})"
                            )
                            return

                    ACTIVE_SUPPORT_CONVERSATIONS[target_user] = convo or {
                        "user_id": target_user,
                        "admin_chat_id": chat_id,
                        "locked_admin_id": from_user_id,
                        "locked_admin_name": admin_name,
                        "opened_at": time.time(),
                    }
                    if from_user_id:
                        ADMIN_ACTIVE_CONVERSATIONS[from_user_id] = target_user
                        ADMIN_GROUP_ACTIVE_SESSION[str(from_user_id)] = target_user
                    ADMIN_GROUP_ACTIVE_SESSION[str(chat_id)] = target_user
                    driver_token = getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
                    if driver_token and driver_token != "mock_tg_token":
                        client = await self.get_client()
                        url = f"https://api.telegram.org/bot{driver_token}/sendMessage"
                        relay_text = f"💬 <b>Phản hồi từ Ban Quản Trị ({admin_name}):</b>\n\n{text}"
                        try:
                            resp = await client.post(url, json={"chat_id": target_user, "text": relay_text, "parse_mode": "HTML"})
                            if resp.status_code == 200:
                                logger.info(f"Relayed admin message to user {target_user}")
                                await self.send_message(
                                    chat_id,
                                    f"✅ <i>Đã gửi phản hồi tới người dùng (ID: <code>{target_user}</code>). Phiên hội thoại đang mở, gõ <code>/close</code> để kết thúc.</i>"
                                )
                            else:
                                logger.error(f"Failed to relay admin message: {resp.text}")
                                await self.send_message(
                                    chat_id,
                                    f"⚠️ Không thể chuyển tiếp tin nhắn tới người dùng (Mã lỗi HTTP {resp.status_code})."
                                )
                        except Exception as e:
                            logger.error(f"Failed to relay message to user: {e}")
                    return
                if len(text) <= 4096 and text.startswith("/"):
                    await self.handle_command(chat_id, text, reply_to_message=msg.get("reply_to_message"))
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
            "allowed_updates": json.dumps(["message", "callback_query", "my_chat_member"]),
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
        # In-memory tracking for support rate limits: user_id -> list of timestamps
        self.support_history: Dict[Union[int, str], List[float]] = {}
        # In-memory tracking for pending support message input: chat_id -> True or dict
        self.pending_support: Dict[int, Any] = {}
        # In-memory tracking for active help tickets awaiting admin response
        self.pending_help_tickets: Dict[int, dict] = {}
        # In-memory tracking for active appeals: user_id -> sub_id
        self.active_appeals: Dict[int, str] = {}

    def get_driver_reply_keyboard(self) -> dict:
        """Returns the persistent 1-tap quick action keyboard for drivers & collaborators."""
        return {
            "keyboard": [
                [{"text": "📱 Xác Thực Số Điện Thoại", "request_contact": True}],
                [{"text": "📋 Khảo Sát Của Tôi"}],
                [{"text": "🆘 Yêu Cầu Hỗ Trợ"}, {"text": "❓ Trợ Giúp"}],
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

    async def bind_collaborator(self, telegram_user_id: int, username: str, full_name: str, ctv_code: str) -> None:
        """Links a Telegram account to a collaborator (CTV) code."""
        if not self.db_pool:
            return
        try:
            async with self.db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO authorized_users (telegram_user_id, username, full_name, role, is_active)
                        VALUES (%s, %s, %s, 'COLLABORATOR', TRUE)
                        ON CONFLICT (telegram_user_id) DO UPDATE SET
                            username = EXCLUDED.username,
                            full_name = EXCLUDED.full_name,
                            role = 'COLLABORATOR';
                        """,
                        (telegram_user_id, ctv_code, full_name),
                    )
                await conn.commit()
        except Exception as e:
            logger.error(f"Failed to bind collaborator {ctv_code}: {e}")

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
                ])

        inline_markup = {"inline_keyboard": buttons} if buttons else None
        await self.send_tracked(f"surveys:{user_id}", chat_id, "\n\n".join(lines), reply_markup=inline_markup)

    async def handle_driver_appeal(self, chat_id: int, user_id: int, sub_id: str) -> None:
        """Handles driver appeal request with 2-hour rate limit (max 5) and active guard."""
        # 1. Prevent duplicate active appeal
        is_active = False
        if sub_id in self.active_appeals.values() or user_id in self.active_appeals:
            is_active = True
        if not is_active and self.db_pool:
            try:
                async with self.db_pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("SELECT status FROM survey_submissions WHERE submission_id = %s;", (sub_id,))
                        row = await cur.fetchone()
                        if row and row[0] in ("APPEAL_PENDING", "MANUAL_REVIEW"):
                            is_active = True
            except Exception:
                pass
        if is_active:
            await self.send_message(
                chat_id,
                f"⚠️ <b>Phiếu #{sub_id} đã có một yêu cầu kiểm tra/phúc khảo đang hoạt động!</b>\n\n"
                f"Bạn không thể gửi thêm yêu cầu mới khi yêu cầu cũ chưa hoàn tất.\n"
                f"👉 Gõ <code>/cancel {sub_id}</code> nếu bạn muốn hủy yêu cầu này.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return

        phone = await self.get_driver_phone(user_id) or str(user_id)
        now = time.time()
        window = 2 * 3600  # 2 hours

        # 2. Rate limit check: max 5 appeals in 2h
        history = [t for t in self.appeal_history.get(phone, []) if now - t < window]
        if len(history) >= 5:
            await self.send_message(
                chat_id,
                "🚫 <b>Bạn đã gửi tối đa 5 lần phúc khảo trong vòng 2 giờ qua!</b>\n\n"
                "Vui lòng đợi Ban Quản Trị xử lý các yêu cầu trước đó hoặc gửi yêu cầu trợ giúp qua lệnh <code>/support</code>.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return

        history.append(now)
        self.appeal_history[phone] = history
        self.active_appeals[user_id] = sub_id

        # Update status in DB / storage
        await self._update_submission_status(sub_id, "APPEAL_PENDING", reason="Thành viên gửi yêu cầu phúc khảo qua Telegram DM")

        # Notify Member
        await self.send_message(
            chat_id,
            f"📨 <b>Yêu cầu phúc khảo cho phiếu #{sub_id} đã được gửi thành công!</b>\n\n"
            f"Ban Quản Trị sẽ trực tiếp kiểm tra lại hồ sơ và phản hồi kết quả sớm nhất tại đây.\n"
            f"<i>(Bạn có thể gõ <code>/cancel {sub_id}</code> để hủy yêu cầu này bất cứ lúc nào).</i>",
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

    async def handle_admin_help_request(
        self, chat_id: int, user_id: int, message_text: str, from_user: dict
    ) -> None:
        """Forwards support inquiries from drivers or collaborators to the Telegram Admin Group."""
        # 1. Prevent duplicate active help request / session
        if user_id in ACTIVE_SUPPORT_CONVERSATIONS or user_id in self.pending_help_tickets:
            await self.send_message(
                chat_id,
                "⚠️ <b>Bạn đã có một yêu cầu hỗ trợ đang hoạt động với Ban Quản Trị!</b>\n\n"
                "Bạn có thể nhắn tin trực tiếp để tiếp tục trao đổi, hoặc gõ <code>/cancel</code> để hủy yêu cầu hiện tại trước khi gửi yêu cầu mới.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return

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

        # 2. Rate limit check: max 5 requests per 2 hours
        now = time.time()
        window = 2 * 3600
        s_history = [t for t in self.support_history.get(user_id, []) if now - t < window]
        if len(s_history) >= 5:
            await self.send_message(
                chat_id,
                "🚫 <b>Bạn đã gửi tối đa 5 yêu cầu hỗ trợ trong vòng 2 giờ qua!</b>\n\n"
                "Vui lòng chờ Ban Quản Trị xử lý các yêu cầu trước đó hoặc thử lại sau.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return

        s_history.append(now)
        self.support_history[user_id] = s_history
        self.pending_help_tickets[user_id] = {"chat_id": chat_id, "timestamp": now, "message": clean_msg}

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
            buttons = [
                [
                    {"text": f"💬 Trả lời {name[:16]}", "callback_data": f"admin_reply_chat:{user_id}"},
                    {"text": "❌ Đóng", "callback_data": f"admin_close_ticket:{user_id}"},
                ]
            ]
            if username:
                buttons.append([{"text": "📱 Mở Telegram riêng", "url": f"https://t.me/{username}"}])
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
    async def handle_cancel_request(self, chat_id: int, user_id: int, target_arg: str = "") -> None:
        """Allows cancelling active review/appeal requests, support tickets, or photo fix sessions."""
        cancelled_items = []
        admin_group = getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-5445563473")
        admin_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN

        # 1. Cancel active appeal
        sub_id = target_arg.strip().lstrip("#") if target_arg else self.active_appeals.get(user_id)
        if sub_id:
            await self._update_submission_status(sub_id, "REJECTED", reason="Người dùng đã hủy yêu cầu phúc khảo")
            if user_id in self.active_appeals:
                del self.active_appeals[user_id]
            cancelled_items.append(f"yêu cầu phúc khảo phiếu <code>#{sub_id}</code>")

            if admin_token and admin_token != "mock_tg_token":
                client = await self.get_client()
                url = f"https://api.telegram.org/bot{admin_token}/sendMessage"
                try:
                    await client.post(url, json={
                        "chat_id": admin_group,
                        "text": f"ℹ️ <b>Người dùng (ID: <code>{user_id}</code>) đã HỦY yêu cầu phúc khảo cho phiếu #{sub_id}.</b>",
                        "parse_mode": "HTML",
                    })
                except Exception:
                    pass

        # 2. Cancel active support ticket or live chat
        if user_id in ACTIVE_SUPPORT_CONVERSATIONS or user_id in self.pending_help_tickets:
            if user_id in ACTIVE_SUPPORT_CONVERSATIONS:
                convo = ACTIVE_SUPPORT_CONVERSATIONS.pop(user_id, {})
                locked_admin = convo.get("locked_admin_id")
                if locked_admin and locked_admin in ADMIN_ACTIVE_CONVERSATIONS:
                    del ADMIN_ACTIVE_CONVERSATIONS[locked_admin]
                if locked_admin and str(locked_admin) in ADMIN_GROUP_ACTIVE_SESSION:
                    del ADMIN_GROUP_ACTIVE_SESSION[str(locked_admin)]
                admin_chat = convo.get("admin_chat_id") or admin_group
                if str(admin_chat) in ADMIN_GROUP_ACTIVE_SESSION:
                    del ADMIN_GROUP_ACTIVE_SESSION[str(admin_chat)]
            if user_id in self.pending_help_tickets:
                del self.pending_help_tickets[user_id]
            cancelled_items.append("yêu cầu hỗ trợ Ban Quản Trị")

            if admin_token and admin_token != "mock_tg_token":
                client = await self.get_client()
                url = f"https://api.telegram.org/bot{admin_token}/sendMessage"
                try:
                    await client.post(url, json={
                        "chat_id": admin_group,
                        "text": f"ℹ️ <b>Người dùng (ID: <code>{user_id}</code>) đã HỦY yêu cầu hỗ trợ.</b>",
                        "parse_mode": "HTML",
                    })
                except Exception:
                    pass

        # 3. Cancel pending photo fix
        if chat_id in self.pending_fixes:
            fix = self.pending_fixes.pop(chat_id)
            cancelled_items.append(f"phiên gửi lại ảnh cho phiếu <code>#{fix.get('sub_id')}</code>")

        # 4. Cancel pending support prompt
        if chat_id in self.pending_support:
            del self.pending_support[chat_id]

        if cancelled_items:
            details = ", ".join(cancelled_items)
            await self.send_message(
                chat_id,
                f"✅ <b>Đã hủy thành công:</b> {details}.\n\nBây giờ bạn có thể thao tác bình thường hoặc gửi yêu cầu mới.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
        else:
            await self.send_message(
                chat_id,
                "ℹ️ Hiện tại bạn không có yêu cầu hỗ trợ, phúc khảo hoặc gửi ảnh nào đang chờ để hủy.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
    async def close_user_support(self, chat_id: int, user_id: int, closed_by: str = "user") -> None:
        """Closes live support conversation from user side."""
        convo = ACTIVE_SUPPORT_CONVERSATIONS.pop(user_id, {})
        locked_admin = convo.get("locked_admin_id")
        if locked_admin and locked_admin in ADMIN_ACTIVE_CONVERSATIONS:
            del ADMIN_ACTIVE_CONVERSATIONS[locked_admin]
        if locked_admin and str(locked_admin) in ADMIN_GROUP_ACTIVE_SESSION:
            del ADMIN_GROUP_ACTIVE_SESSION[str(locked_admin)]
        admin_chat_id = convo.get("admin_chat_id") or getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-5445563473")
        if str(admin_chat_id) in ADMIN_GROUP_ACTIVE_SESSION:
            del ADMIN_GROUP_ACTIVE_SESSION[str(admin_chat_id)]
        await self.send_message(
            chat_id,
            "🔒 <b>Cuộc trò chuyện hỗ trợ đã kết thúc theo yêu cầu của bạn.</b>\n\n"
            "Cảm ơn bạn! Chúc bạn một ngày làm việc thuận lợi.",
            reply_markup=self.get_driver_reply_keyboard(),
        )
        admin_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
        if admin_token and admin_token != "mock_tg_token":
            client = await self.get_client()
            url = f"https://api.telegram.org/bot{admin_token}/sendMessage"
            msg = f"🔒 <b>Người dùng <code>{user_id}</code> đã kết thúc cuộc trò chuyện hỗ trợ.</b>"
            try:
                await client.post(url, json={"chat_id": admin_chat_id, "text": msg, "parse_mode": "HTML"})
            except Exception as e:
                logger.error(f"Failed to notify admin: {e}")


    async def handle_driver_command(self, chat_id: int, user_id: int, text: str, from_user: dict) -> None:
        """Dispatches text commands and persistent keyboard button presses."""
        clean = text.strip()
        cmd = clean.split()[0].lower() if clean else ""

        if cmd == "/start" or clean == "Bắt đầu":
            parts = clean.split(" ", 1)
            payload = parts[1].strip() if len(parts) > 1 else ""
            from core.ctv import CTV_CODES, normalize_ctv_code

            ctv_payload = normalize_ctv_code(re.sub(r"^ctv[_-]?", "", payload, flags=re.IGNORECASE))
            if payload and (payload.lower().startswith("ctv") or ctv_payload in CTV_CODES):
                ctv_code = ctv_payload or "UNKNOWN"
                display = ((from_user.get("first_name") or "") + " " + (from_user.get("last_name") or "")).strip()
                await self.bind_collaborator(user_id, from_user.get("username"), display or f"CTV {user_id}", ctv_code)
                await self.send_message(
                    chat_id,
                    f"✅ <b>Đã liên kết Cộng tác viên <code>{ctv_code}</code> với tài khoản Telegram này.</b>\n\n"
                    "Từ giờ mọi kết quả duyệt / từ chối phiếu do bạn giới thiệu sẽ được thông báo tại đây.",
                    reply_markup=self.get_driver_reply_keyboard(),
                )
                return
            if payload.startswith("support"):
                sub_ref = payload[7:].lstrip("_")
                ref_text = f" (cho phiếu <code>#{sub_ref}</code>)" if sub_ref else ""
                self.pending_support[chat_id] = True
                prompt = (
                    f"💬 <b>Bạn cần Ban Quản Trị hỗ trợ vấn đề gì{ref_text}?</b>\n\n"
                    f"Hãy nhập nội dung thắc mắc hoặc khó khăn của bạn ngay tại đây.\n\n"
                    f"Hệ thống sẽ chuyển tiếp trực tiếp tin nhắn của bạn tới Ban Quản Trị trong nhóm điều hành."
                )
                await self.send_message(chat_id, prompt, reply_markup=self.get_driver_reply_keyboard())
                return

            if payload.startswith("fix_"):
                # Deep link: /start fix_<sub_id>_<slot>
                fix_data = payload[4:].split("_", 1)
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
                "👋 <b>Kênh Hỗ Trợ Khảo Sát Trust Bridge (CTV)</b>\n\n"
                "Kênh thông tin và hỗ trợ dành cho <b>Cộng tác viên</b>:\n"
                "• Nhận kết quả duyệt / từ chối phiếu do bạn giới thiệu\n"
                "• Bổ sung ảnh chưa đạt trực tiếp trong chat để kiểm tra lại\n"
                "• Tra cứu phiếu và gửi phúc khảo bằng <code>/appeal &lt;mã_phiếu&gt;</code>\n"
                "• Kết nối trực tiếp với Ban Quản Trị\n\n"
                "👉 <i>Nếu chưa liên kết, hãy mở link mời CTV (dạng <code>...?start=ctv_&lt;mã_ctv&gt;</code>) hoặc bấm nút <b>[📱 Xác Thực Số Điện Thoại]</b> bên dưới.</i>"
            )
            await self.send_message(chat_id, welcome_text, reply_markup=self.get_driver_reply_keyboard())

        elif cmd in ("/surveys", "/my_surveys", "/tracuu") or clean == "📋 Khảo Sát Của Tôi":
            await self.handle_driver_surveys_lookup(chat_id, user_id)

        elif cmd in ("/support", "/hotro", "/help_admin", "/lienhe") or "hỗ trợ" in clean.lower():
            if clean.startswith("/"):
                parts = clean.split(" ", 1)
                msg_body = parts[1].strip() if len(parts) > 1 else ""
            else:
                msg_body = ""
            await self.handle_admin_help_request(chat_id, user_id, msg_body, from_user)

        elif cmd in ("/cancel", "/huy", "/cancel_request") or clean == "❌ Hủy Yêu Cầu":
            parts = clean.split(" ", 1)
            target = parts[1].strip() if len(parts) > 1 else ""
            await self.handle_cancel_request(chat_id, user_id, target)


        elif cmd in ("/help", "/trogiup") or clean == "❓ Trợ Giúp":
            help_text = (
                "❓ <b>Hướng Dẫn Sử Dụng (Dành Cho Cộng Tác Viên):</b>\n\n"
                "1. <b>Liên kết CTV:</b> mở link mời <code>...?start=ctv_&lt;mã_ctv&gt;</code> để nhận thông báo phiếu bạn giới thiệu.\n"
                "2. <b>Xem phiếu đã nộp:</b> Bấm [📋 Khảo Sát Của Tôi] hoặc gõ <code>/surveys</code>.\n"
                "3. <b>Bổ sung ảnh chưa đạt:</b> Bấm [📸 Gửi lại ảnh] rồi gửi ảnh mới trực tiếp vào chat.\n"
                "4. <b>Phúc khảo:</b> Gõ <code>/appeal &lt;mã_phiếu&gt;</code> nếu kết quả chưa chính xác.\n"
                "5. <b>Cần trợ giúp:</b> Gõ <code>/support &lt;nội dung&gt;</code> hoặc bấm [🆘 Yêu Cầu Hỗ Trợ].\n"
                "6. <b>Kết thúc phiên hỗ trợ:</b> Gõ <code>/close</code> khi đã trao đổi xong với Ban Quản Trị."
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
        elif cmd in ("/close", "/end", "/ketthuc"):
            if user_id in ACTIVE_SUPPORT_CONVERSATIONS:
                await self.close_user_support(chat_id, user_id, closed_by="user")
            else:
                await self.send_message(
                    chat_id,
                    "ℹ️ Hiện tại không có phiên hội thoại hỗ trợ trực tiếp nào đang mở.",
                    reply_markup=self.get_driver_reply_keyboard(),
                )


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
                # 1. Check if user wants to close active support conversation
                if text.strip().lower() in ("/close", "/end", "/ketthuc") and user_id in ACTIVE_SUPPORT_CONVERSATIONS:
                    await self.close_user_support(chat_id, user_id, closed_by="user")
                    return

                # 2. Check if user is in an active support conversation open with admin
                if user_id in ACTIVE_SUPPORT_CONVERSATIONS and not text.startswith("/"):
                    convo = ACTIVE_SUPPORT_CONVERSATIONS[user_id]
                    admin_chat_id = convo.get("admin_chat_id") or getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-5445563473")
                    admin_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
                    user_display = from_user.get("first_name", f"User {user_id}")
                    if admin_token and admin_token != "mock_tg_token":
                        client = await self.get_client()
                        url = f"https://api.telegram.org/bot{admin_token}/sendMessage"
                        relay_text = f"👤 <b>Tin nhắn từ {user_display} (ID: <code>{user_id}</code>):</b>\n\n{text}"
                        try:
                            await client.post(url, json={"chat_id": admin_chat_id, "text": relay_text, "parse_mode": "HTML"})
                        except Exception as e:
                            logger.error(f"Failed to relay user message to admin group: {e}")
                    return

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

            if data.startswith("driver_fix:"):
                parts = data.split(":")
                sub_id = parts[1]
                slot_name = parts[2] if len(parts) > 2 else "slot_photo"
                self.pending_fixes[chat_id] = {"sub_id": sub_id, "slot": slot_name, "timestamp": time.time()}
                await self.send_message(
                    chat_id,
                    f"📸 <b>Đang chờ nhận ảnh cho phiếu #{sub_id} ({slot_name}):</b>\n\n"
                    f"👉 Vui lòng mở camera, chụp rõ nét và gửi thẳng ảnh vào khung chat này ngay nhé!",
                    reply_markup=self.get_driver_reply_keyboard(),
                )
                await self.answer_callback_query(cq_id, "Đang chờ gửi ảnh mới")

            elif data.startswith("driver_support:"):
                sub_id = data.split(":", 1)[1]
                await self.handle_admin_help_request(
                    chat_id, user_id, f"Cần hỗ trợ về phiếu khảo sát #{sub_id}", cq.get("from", {})
                )
                await self.answer_callback_query(cq_id, f"Đã gửi yêu cầu hỗ trợ cho #{sub_id}")
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
