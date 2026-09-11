import asyncio
import logging
import os
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from monitor import MultiMonitor
from discovery import LiveDiscovery

logger = logging.getLogger(__name__)

# ⚠️ KHÔNG hardcode token ở đây — để Railway Variables lo
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "8619018586:AAGP0VUnBspppJo7yhp32P0g_j7avtmsVBM")
NOTIFY_CHAT_ID = os.environ.get("NOTIFY_CHAT_ID", "8093889693")

# Khoảng cách tối thiểu giữa 2 tin nhắn — tránh Telegram spam filter
MSG_INTERVAL = 3.0


def _format_bag(username: str, data: dict) -> str:
    diamonds = data["diamond_count"]
    people   = data["people_count"]
    joining  = data["vote_count"]
    sender   = data["sender"]
    link     = data.get("link", f"https://www.tiktok.com/@{username}/live")

    lines = [
        "🎁 <b>TÚI MAY MẮN xuất hiện!</b>",
        "",
        f"👤 Streamer:  <b>@{username}</b>",
        f"💎 Giá trị:  <b>{diamonds:,} kim cương</b>",
        f"🏆 Số người nhận:   <b>{people:,} người</b>",
    ]
    if joining > 0:
        lines.append(f"🎯 Đang tham gia: <b>{joining:,} người</b>")
    if sender.lower() != username.lower():
        lines.append(f"🎀 Người gửi: <b>{sender}</b>")
    lines += ["", "⏰ Còn 1 phút — vào ngay!", f'🔗 <a href="{link}">Vào LIVE @{username}</a>']
    return "\n".join(lines)


def _format_chest(username: str, data: dict) -> str:
    diamonds = data["diamond_count"]
    people   = data["people_count"]
    link     = data.get("link", f"https://www.tiktok.com/@{username}/live")
    return "\n".join([
        "📦 <b>RƯƠNG xuất hiện!</b>",
        "",
        f"👤 Streamer:  <b>@{username}</b>",
        f"💎 Giá trị:  <b>{diamonds:,} kim cương</b>",
        f"🏆 Số người nhận:   <b>{people:,} người</b>",
        "",
        "⏰ Còn 1 phút — vào ngay!",
        f'🔗 <a href="{link}">Vào LIVE @{username}</a>',
    ])


class TelegramBot:
    def __init__(self):
        if not BOT_TOKEN:
            raise ValueError(
                "Thiếu BOT_TOKEN! Vào Railway → service → Variables → thêm BOT_TOKEN"
            )

        self.app = Application.builder().token(BOT_TOKEN).build()

        # Queue tin nhắn — gửi tuần tự, cách nhau MSG_INTERVAL giây
        self._msg_queue: asyncio.Queue = asyncio.Queue()

        self.monitor   = MultiMonitor(notify=self._on_event)
        self.discovery = LiveDiscovery(
            on_add=self._on_discovery_add,
            on_remove=self._on_discovery_remove,
        )

        self.app.add_handler(CommandHandler("start",  self._cmd_start))
        self.app.add_handler(CommandHandler("add",    self._cmd_add))
        self.app.add_handler(CommandHandler("remove", self._cmd_remove))
        self.app.add_handler(CommandHandler("list",   self._cmd_list))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("help",   self._cmd_help))

    # ──────────────────────────────────────────────────────────
    # NOTIFICATION QUEUE — 1 tin / MSG_INTERVAL giây, không spam
    # ──────────────────────────────────────────────────────────
    async def _queue_worker(self):
        while True:
            text = await self._msg_queue.get()
            if NOTIFY_CHAT_ID:
                try:
                    await self.app.bot.send_message(
                        chat_id=NOTIFY_CHAT_ID,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                except Exception as e:
                    logger.error(f"Gửi thông báo thất bại: {e}")
            self._msg_queue.task_done()
            await asyncio.sleep(MSG_INTERVAL)

    async def _on_event(self, event_type: str, username: str, data: dict):
        if event_type == "bag":
            text = _format_bag(username, data)
        elif event_type == "chest":
            text = _format_chest(username, data)
        else:
            return
        await self._msg_queue.put(text)
        logger.info(f"[Queue] {event_type} @{username} | queue={self._msg_queue.qsize()}")

    # ──────────────────────────────────────────────────────────
    # CALLBACK TỰ ĐỘNG TỪ DISCOVERY
    # ──────────────────────────────────────────────────────────
    async def _on_discovery_add(self, username: str):
        added = await self.monitor.add(username)
        if added:
            logger.info(f"[Auto-discovery] Thêm @{username}")

    async def _on_discovery_remove(self, username: str):
        await self.monitor.remove(username)

    # ──────────────────────────────────────────────────────────
    # LỆNH
    # ──────────────────────────────────────────────────────────
    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤖 <b>TikTok LIVE Scanner</b>\n\n"
            "Bot tự động quét rương và túi may mắn trong TikTok LIVE.\n\n"
            "Dùng /help để xem danh sách lệnh.",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📋 <b>Danh sách lệnh</b>\n\n"
            "/add <code>@username</code>  —  Thêm streamer thủ công\n"
            "/remove <code>@username</code>  —  Xóa streamer\n"
            "/list  —  Xem danh sách đang theo dõi\n"
            "/status  —  Xem trạng thái bot\n\n"
            "<b>Chế độ tự động:</b>\n"
            "• Bot tự tìm streamer đang live mỗi 75 giây\n"
            "• Thông báo gửi cách nhau ít nhất 3 giây\n"
            "• Mỗi streamer có cooldown 10 phút/lần",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_add(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("⚠️ Dùng: /add <code>@username</code>", parse_mode=ParseMode.HTML)
            return
        username = ctx.args[0].lstrip("@").lower()
        added = await self.monitor.add(username)
        if added:
            await update.message.reply_text(f"✅ Đã thêm <b>@{username}</b>.", parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(f"⚠️ <b>@{username}</b> đã có rồi.", parse_mode=ParseMode.HTML)

    async def _cmd_remove(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("⚠️ Dùng: /remove <code>@username</code>", parse_mode=ParseMode.HTML)
            return
        username = ctx.args[0].lstrip("@").lower()
        removed = await self.monitor.remove(username)
        if removed:
            await update.message.reply_text(f"🗑️ Đã xóa <b>@{username}</b>.", parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(f"❌ Không tìm thấy <b>@{username}</b>.", parse_mode=ParseMode.HTML)

    async def _cmd_list(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        usernames = self.monitor.get_list()
        if not usernames:
            await update.message.reply_text("📭 Chưa có streamer nào.")
            return
        lines = ["👀 <b>Đang theo dõi:</b>\n"]
        for i, name in enumerate(usernames, 1):
            lines.append(f"{i}. @{name}")
        lines.append(f"\n📡 Tổng: <b>{len(usernames)} streamer</b>")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        count  = len(self.monitor.get_list())
        q_size = self._msg_queue.qsize()
        target = NOTIFY_CHAT_ID or "chưa set"
        await update.message.reply_text(
            f"📊 <b>Trạng thái bot</b>\n\n"
            f"🔍 Auto-discovery: <b>đang chạy</b>\n"
            f"📡 Streamer đang theo dõi: <b>{count}</b>\n"
            f"📬 Tin nhắn trong queue: <b>{q_size}</b>\n"
            f"📢 Gửi về: <code>{target}</code>",
            parse_mode=ParseMode.HTML,
        )

    # ──────────────────────────────────────────────────────────
    # CHẠY BOT
    # ──────────────────────────────────────────────────────────
    async def run(self):
        if not BOT_TOKEN:
            raise ValueError("Thiếu BOT_TOKEN!")

        await self.app.initialize()

        # ✅ FIX CONFLICT: Xóa webhook + đợi polling cũ timeout hẳn
        # delete_webhook chỉ kill webhook — không kill polling connection cũ.
        # Phải gọi getUpdates(timeout=0) để "cướp" offset, rồi sleep 5s để
        # Telegram server xác nhận instance cũ đã chết trước khi ta poll mới.
        try:
            await self.app.bot.delete_webhook(drop_pending_updates=True)
            logger.info("✅ Đã xóa webhook cũ")
        except Exception as e:
            logger.warning(f"delete_webhook: {e}")
        try:
            await self.app.bot.get_updates(offset=-1, timeout=0)
        except Exception:
            pass
        await asyncio.sleep(5)
        logger.info("✅ Đã đợi instance cũ ngắt — bắt đầu polling mới")

        await self.app.bot.set_my_commands([
            BotCommand("add",    "Thêm streamer thủ công"),
            BotCommand("remove", "Xóa streamer"),
            BotCommand("list",   "Xem danh sách đang theo dõi"),
            BotCommand("status", "Trạng thái bot"),
            BotCommand("help",   "Hướng dẫn"),
        ])

        await self.app.start()
        await self.app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )

        asyncio.create_task(self._queue_worker(), name="msg_queue_worker")
        self.discovery.start()

        if NOTIFY_CHAT_ID:
            try:
                await self.app.bot.send_message(
                    chat_id=NOTIFY_CHAT_ID,
                    text="🤖 <b>Bot đã khởi động!</b>\n\n"
                         "🔍 Đang tự động tìm và quét streamer TikTok LIVE...\n"
                         "Sẽ thông báo ngay khi có rương hoặc túi xuất hiện.",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Không gửi được thông báo khởi động: {e}")

        logger.info("🚀 Bot + Auto-discovery đang chạy...")

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
