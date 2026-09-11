import asyncio
import json
import logging
import re
import time
from typing import Set, Callable, Awaitable

try:
    import cloudscraper
except ImportError:
    cloudscraper = None

logger = logging.getLogger(__name__)

SCAN_INTERVAL   = 180   # quét mỗi 3 phút
MAX_STREAMERS   = 60    # tối đa theo dõi cùng lúc
STALE_TIMEOUT   = 600   # xóa streamer offline quá 10 phút

OnNewStreamer = Callable[[str], Awaitable[None]]
OnDropStreamer = Callable[[str], Awaitable[None]]


class LiveDiscovery:
    """Tự động tìm streamer đang live trên TikTok và quản lý danh sách."""

    def __init__(self, on_add: OnNewStreamer, on_remove: OnDropStreamer):
        self.on_add    = on_add
        self.on_remove = on_remove
        self._active: dict[str, float] = {}   # username → last_seen timestamp
        self._task: asyncio.Task | None = None
        self._running = False
        self._scraper = None

    def start(self):
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._loop(), name="discovery")
            logger.info("🔍 Auto-discovery đã bắt đầu")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ──────────────────────────────────────────
    # MAIN LOOP
    # ──────────────────────────────────────────
    async def _loop(self):
        while self._running:
            try:
                found = await asyncio.get_event_loop().run_in_executor(
                    None, self._fetch_live_usernames
                )
                if found:
                    await self._sync(found)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Discovery lỗi: {e}")

            await asyncio.sleep(SCAN_INTERVAL)

    # ──────────────────────────────────────────
    # FETCH — thử nhiều endpoint
    # ──────────────────────────────────────────
    def _fetch_live_usernames(self) -> set[str]:
        if self._scraper is None:
            if cloudscraper is None:
                logger.warning("cloudscraper chưa cài — pip install cloudscraper")
                return set()
            self._scraper = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )

        usernames: set[str] = set()

        # Phương pháp 1: TikTok LIVE page (embedded JSON)
        try:
            usernames |= self._method_live_page()
            if usernames:
                logger.info(f"[Discovery] Method 1 tìm được {len(usernames)} streamer")
                return usernames
        except Exception as e:
            logger.debug(f"Method 1 thất bại: {e}")

        # Phương pháp 2: Webcast API room list
        try:
            usernames |= self._method_webcast_api()
            if usernames:
                logger.info(f"[Discovery] Method 2 tìm được {len(usernames)} streamer")
                return usernames
        except Exception as e:
            logger.debug(f"Method 2 thất bại: {e}")

        # Phương pháp 3: TikTok explore API
        try:
            usernames |= self._method_explore_api()
            if usernames:
                logger.info(f"[Discovery] Method 3 tìm được {len(usernames)} streamer")
        except Exception as e:
            logger.debug(f"Method 3 thất bại: {e}")

        return usernames

    def _method_live_page(self) -> set[str]:
        """Scrape trang tiktok.com/live — lấy JSON nhúng trong page."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
            "Referer": "https://www.tiktok.com/",
        }
        resp = self._scraper.get(
            "https://www.tiktok.com/live",
            headers=headers, timeout=15
        )
        resp.raise_for_status()

        usernames: set[str] = set()

        # Tìm JSON blob __UNIVERSAL_DATA_FOR_REHYDRATION__
        match = re.search(
            r'id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
            resp.text, re.DOTALL
        )
        if match:
            try:
                data = json.loads(match.group(1))
                raw = json.dumps(data)
                # Extract uniqueId từ JSON
                for uid in re.findall(r'"uniqueId"\s*:\s*"([^"]{3,})"', raw):
                    usernames.add(uid.lower())
            except Exception:
                pass

        # Fallback: regex trực tiếp trong HTML
        for uid in re.findall(r'"uniqueId"\s*:\s*"([A-Za-z0-9_.]{3,})"', resp.text):
            usernames.add(uid.lower())

        return usernames

    def _method_webcast_api(self) -> set[str]:
        """Hit webcast API của TikTok để lấy danh sách live rooms."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://www.tiktok.com/",
        }
        params = {
            "aid": "1988",
            "app_name": "tiktok_web",
            "type_id": "0",
            "count": "50",
            "cursor": "0",
        }
        resp = self._scraper.get(
            "https://webcast.tiktok.com/webcast/room/list/",
            params=params, headers=headers, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()

        usernames: set[str] = set()
        rooms = (data.get("data") or {}).get("room_infos") or []
        for room in rooms:
            owner = room.get("owner") or {}
            uid = owner.get("display_id") or owner.get("unique_id") or ""
            if uid:
                usernames.add(uid.lower())

        return usernames

    def _method_explore_api(self) -> set[str]:
        """TikTok explore/recommend API."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://www.tiktok.com/",
        }
        params = {
            "aid": "1988",
            "count": "30",
            "sourceType": "live",
        }
        resp = self._scraper.get(
            "https://www.tiktok.com/api/recommend/itemlist/",
            params=params, headers=headers, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()

        usernames: set[str] = set()
        items = data.get("itemList") or []
        for item in items:
            author = item.get("author") or {}
            uid = author.get("uniqueId") or ""
            # Chỉ lấy nếu đang live
            if uid and item.get("isLive"):
                usernames.add(uid.lower())

        return usernames

    # ──────────────────────────────────────────
    # SYNC — thêm mới / xóa stale
    # ──────────────────────────────────────────
    async def _sync(self, found: set[str]):
        now = time.time()

        # Cập nhật last_seen
        for u in found:
            self._active[u] = now

        # Thêm streamer mới (nếu chưa đủ MAX)
        for u in found:
            if len(self._active) <= MAX_STREAMERS:
                await self.on_add(u)   # MultiMonitor.add() tự bỏ qua nếu đã có

        # Xóa streamer offline quá lâu
        stale = [u for u, ts in self._active.items() if now - ts > STALE_TIMEOUT]
        for u in stale:
            logger.info(f"[Discovery] @{u} offline, xóa khỏi danh sách")
            del self._active[u]
            await self.on_remove(u)

        logger.info(
            f"[Discovery] Đang theo dõi {len(self._active)} streamer | "
            f"Tìm thấy {len(found)} live lần này"
        )
