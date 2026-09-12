import asyncio
import logging
from bot import TelegramBot
import storage

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("TikTokLive").setLevel(logging.WARNING)

if __name__ == "__main__":
    storage.init_db()
    bot = TelegramBot()
    asyncio.run(bot.run())
