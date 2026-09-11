import os
import logging
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from telegram.constants import ParseMode
from monitor import MultiMonitor
from discovery import LiveDiscovery

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8550034707:AAESxNB97Yd-woA3eAm6PhflPid_c4W5RO0")
# CHAT_ID: ID nhóm/kênh nhận thông báo tự động
NOTIFY_CHAT_ID = os.environ.get("NOTIFY_CHAT_ID", "8093889693")
# WATCH_LIST: danh sách streamer tự động quét khi bot khởi động
# Ví dụ: "user1,user2,user3"
WATCH_LIST_RAW = os.environ.get("WATCH_LIST", "")


def _xu_label(diamonds: int) -> str:
    """TikTok dùng đơn vị Coin/Diamond — hiển thị cả hai."""
    return f"{diamonds:,} 💎 kim cương"


def _format_bag(username: str, data: dict) -> str:
    diamonds = data["diamond_count"]
    people   = data["people_count"]
    joining  = data["vote_count"]
    sender   = data["sender"]

    lines = [
        f"🎁 <b>TÚI MAY MẮN xuất hiện!</b>",
        f"",
        f"👤 Streamer:  <b>@{username}</b>",
        f"💎 Giá trị:  <b>{diamonds:,} kim cương</b>",
        f"🏆 Số người nhận:   <b>{people:,} người</b>",
    ]
    if joining > 0:
        lines.append(f"🎯 Đang tham gia: <b>{joining:,} người</b>")
    if sender.lower() != username.lower():
        lines.append(f"🎀 Người gửi: <b>{sender}</b>")

    lines.append(f"")
    lines.append(f"⏰ Sắp hết hạn — vào ngay!")
    lines.append(f'🔗 <a href="{data["link"]}">Vào LIVE @{username}</a>')
    return "\n".join(lines)


def _format_chest(username: str, data: dict) -> str:
    diamonds = data["diamond_count"]
    people   = data["people_count"]

    lines = [
        f"📦 <b>RƯƠNG xuất hiện!</b>",
        f"",
        f"👤 Streamer:  <b>@{username}</b>",
        f"💎 Giá trị:  <b>{diamonds:,} kim cương</b>",
        f"🏆 Số người nhận:   <b>{people:,} người</b>",
        f"",
        f"⏰ Sắp hết hạn — vào ngay!",
        f'🔗 <a href="{data["link"]}">Vào LIVE @{username}</a>',
    ]
    return "\n".join(lines)


class TelegramBot:
    def __init__(self):
        if not BOT_TOKEN:
            raise ValueError("Thiếu BOT_TOKEN trong Replit Secrets!")

        self.app = Application.builder().token(BOT_TOKEN).build()
        self.monitor = MultiMonitor(notify=self._on_event)
        # Tự động tìm streamer đang live — không cần /add thủ công nữa
        self.discovery = LiveDiscovery(
            on_add=self._on_discovery_add,
            on_remove=self._on_discovery_remove,
        )

        # Đăng ký lệnh
        self.app.add_handler(CommandHandler("start",  self._cmd_start))
        self.app.add_handler(CommandHandler("add",    self._cmd_add))
        self.app.add_handler(CommandHandler("remove", self._cmd_remove))
        self.app.add_handler(CommandHandler("list",   self._cmd_list))
        self.app.add_handler(CommandHandler("help",   self._cmd_help))

    # ──────────────────────────────────────────────────────────
    # CALLBACK từ monitor — gửi thông báo lên Telegram
    # ──────────────────────────────────────────────────────────
    async def _on_event(self, event_type: str, username: str, data: dict):
        if event_type == "bag":
            text = _format_bag(username, data)
        elif event_type == "chest":
            text = _format_chest(username, data)
        else:
            return

        # Gửi tới NOTIFY_CHAT_ID nếu có cấu hình, không thì skip
        # (thông báo sẽ gửi khi có người dùng lệnh /add trong group)
        target = NOTIFY_CHAT_ID or getattr(self, "_last_chat_id", None)
        if not target:
            logger.warning("Chưa có NOTIFY_CHAT_ID — bỏ qua thông báo")
            return

        try:
            await self.app.bot.send_message(
                chat_id=target,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error(f"Gửi thông báo thất bại: {e}")

    # ──────────────────────────────────────────────────────────
    # CALLBACK từ discovery — tự thêm/xóa streamer, không nhắn chat
    # ──────────────────────────────────────────────────────────
    async def _on_discovery_add(self, username: str):
        added = await self.monitor.add(username)
        if added:
            logger.info(f"[Auto-discovery] Thêm @{username}")

    async def _on_discovery_remove(self, username: str):
        await self.monitor.remove(username)

    # ──────────────────────────────────────────────────────────
    # LỆNH /start
    # ──────────────────────────────────────────────────────────
    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self._last_chat_id = str(update.effective_chat.id)
        await update.message.reply_text(
            "🤖 <b>TikTok LIVE Scanner</b>\n\n"
            "Bot quét rương và túi may mắn trong TikTok LIVE.\n\n"
            "Dùng /help để xem danh sách lệnh.",
            parse_mode=ParseMode.HTML,
        )

    # ──────────────────────────────────────────────────────────
    # LỆNH /help
    # ──────────────────────────────────────────────────────────
    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        text = (
            "📋 <b>Danh sách lệnh</b>\n\n"
            "/add <code>@username</code>  —  Thêm streamer vào danh sách theo dõi\n"
            "/remove <code>@username</code>  —  Xóa streamer khỏi danh sách\n"
            "/list  —  Xem danh sách đang theo dõi\n\n"
            "<b>Ghi chú:</b>\n"
            "• Bot tự động kết nối lại nếu streamer chưa live\n"
            "• Thông báo gửi về <code>NOTIFY_CHAT_ID</code> (cấu hình trong Secrets)\n"
            "• Nếu chưa set <code>NOTIFY_CHAT_ID</code>, bot gửi về chat hiện tại"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    # ──────────────────────────────────────────────────────────
    # LỆNH /add @username
    # ──────────────────────────────────────────────────────────
    async def _cmd_add(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self._last_chat_id = str(update.effective_chat.id)

        if not ctx.args:
            await update.message.reply_text(
                "⚠️ Dùng: /add <code>@username</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        username = ctx.args[0].lstrip("@").lower()
        added = await self.monitor.add(username)

        if added:
            await update.message.reply_text(
                f"✅ Đã thêm <b>@{username}</b> vào danh sách theo dõi.\n"
                f"Bot đang kiểm tra xem họ có đang live không...",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                f"⚠️ <b>@{username}</b> đã có trong danh sách rồi.",
                parse_mode=ParseMode.HTML,
            )

    # ──────────────────────────────────────────────────────────
    # LỆNH /remove @username
    # ──────────────────────────────────────────────────────────
    async def _cmd_remove(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text(
                "⚠️ Dùng: /remove <code>@username</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        username = ctx.args[0].lstrip("@").lower()
        removed = await self.monitor.remove(username)

        if removed:
            await update.message.reply_text(
                f"🗑️ Đã xóa <b>@{username}</b> khỏi danh sách.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                f"❌ Không tìm thấy <b>@{username}</b> trong danh sách.",
                parse_mode=ParseMode.HTML,
            )

    # ──────────────────────────────────────────────────────────
    # LỆNH /list
    # ──────────────────────────────────────────────────────────
    async def _cmd_list(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        usernames = self.monitor.get_list()
        if not usernames:
            await update.message.reply_text("📭 Chưa có streamer nào trong danh sách.")
            return

        lines = ["👀 <b>Đang theo dõi:</b>\n"]
        for i, name in enumerate(usernames, 1):
            lines.append(f"{i}. @{name}")
        lines.append(f"\n📡 Tổng: <b>{len(usernames)} streamer</b>")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    # ──────────────────────────────────────────────────────────
    # CHẠY BOT
    # ──────────────────────────────────────────────────────────
    async def run(self):
        await self.app.bot.set_my_commands([
            BotCommand("add",    "Thêm streamer theo dõi"),
            BotCommand("remove", "Xóa streamer khỏi danh sách"),
            BotCommand("list",   "Xem danh sách đang theo dõi"),
            BotCommand("help",   "Hướng dẫn sử dụng"),
        ])

        logger.info("🚀 Bot đang chạy...")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

        # Tự động quét TikTok tìm streamer đang live (không cần /add nữa)
        self.discovery.start()

        # Auto-start từ WATCH_LIST env (vẫn hỗ trợ nếu muốn ghim sẵn 1 vài streamer)
        if WATCH_LIST_RAW:
            auto_list = [u.strip().lstrip("@").lower()
                         for u in WATCH_LIST_RAW.split(",") if u.strip()]
            if auto_list:
                logger.info(f"⚡ Auto-start quét {len(auto_list)} streamer: {auto_list}")
                for username in auto_list:
                    await self.monitor.add(username)
                # Gửi thông báo về NOTIFY_CHAT_ID nếu có
                if NOTIFY_CHAT_ID:
                    names = ", ".join(f"@{u}" for u in auto_list)
                    try:
                        await self.app.bot.send_message(
                            chat_id=NOTIFY_CHAT_ID,
                            text=f"🤖 <b>Bot đã khởi động!</b>\n\n"
                                 f"⚡ Đang tự động quét <b>{len(auto_list)}</b> streamer:\n"
                                 f"{names}\n\n"
                                 f"Sẽ thông báo ngay khi có rương hoặc túi xuất hiện.",
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.error(f"Không gửi được thông báo khởi động: {e}")

        # Block mãi mãi
        import asyncio
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            await self.discovery.stop()
            await self.monitor.stop_all()
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
