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

SCAN_INTERVAL   = 30    # quét mỗi 30 giây — gần như liên tục
MAX_STREAMERS   = 150   # tối đa theo dõi cùng lúc
STALE_TIMEOUT   = 600   # xóa streamer offline quá 10 phút
ROOM_LIST_PAGES = 6     # số trang phân trang lấy ở method 2 mỗi lần quét (6×50 = tới 300 phòng)

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

        # Chạy CẢ 3 phương pháp mỗi lần quét và gộp kết quả — trước đây dừng
        # ngay khi 1 phương pháp có kết quả, bỏ lỡ rất nhiều streamer mà 2
        # phương pháp còn lại tìm được (mỗi nguồn phủ 1 tập streamer khác nhau).
        try:
            r1 = self._method_live_page()
            usernames |= r1
            logger.info(f"[Discovery] Method 1 tìm được {len(r1)} streamer")
        except Exception as e:
            logger.warning(f"Method 1 thất bại: {type(e).__name__}: {e}")

        try:
            r2 = self._method_webcast_api()
            usernames |= r2
            logger.info(f"[Discovery] Method 2 tìm được {len(r2)} streamer")
        except Exception as e:
            logger.warning(f"Method 2 thất bại: {type(e).__name__}: {e}")

        try:
            r3 = self._method_explore_api()
            usernames |= r3
            logger.info(f"[Discovery] Method 3 tìm được {len(r3)} streamer")
        except Exception as e:
            logger.warning(f"Method 3 thất bại: {type(e).__name__}: {e}")

        if not usernames:
            logger.warning("[Discovery] Cả 3 phương pháp đều không tìm được streamer nào ở vòng quét này")

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

        if not usernames:
            # Chẩn đoán: log status + đoạn đầu response để biết TikTok trả về gì
            # (trang thật, trang chặn/captcha, hay redirect) thay vì đoán mò.
            snippet = resp.text[:300].replace("\n", " ")
            logger.warning(
                f"[Discovery] Method 1: status={resp.status_code}, "
                f"độ dài={len(resp.text)}, có_UNIVERSAL_DATA={bool(match)}, "
                f"đoạn đầu='{snippet}'"
            )

        return usernames

    def _method_webcast_api(self) -> set[str]:
        """Hit webcast API của TikTok để lấy danh sách live rooms — có phân trang."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://www.tiktok.com/",
        }

        usernames: set[str] = set()
        cursor = "0"
        last_data = None

        for _ in range(ROOM_LIST_PAGES):
            params = {
                "aid": "1988",
                "app_name": "tiktok_web",
                "type_id": "0",
                "count": "50",
                "cursor": cursor,
            }
            resp = self._scraper.get(
                "https://webcast.tiktok.com/webcast/room/list/",
                params=params, headers=headers, timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
            last_data = data

            payload = data.get("data") or {}
            rooms = payload.get("room_infos") or []
            if not rooms:
                break

            for room in rooms:
                owner = room.get("owner") or {}
                uid = owner.get("display_id") or owner.get("unique_id") or ""
                if uid:
                    usernames.add(uid.lower())

            # Dừng nếu API báo hết trang hoặc không có cursor mới
            next_cursor = payload.get("cursor")
            has_more = payload.get("has_more", True)
            if not has_more or not next_cursor or next_cursor == cursor:
                break
            cursor = str(next_cursor)

        if not usernames:
            snippet = str(last_data)[:300] if last_data is not None else "(request lỗi trước khi có response)"
            logger.warning(f"[Discovery] Method 2: raw response mẫu='{snippet}'")

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

        if not usernames:
            snippet = str(data)[:300]
            logger.warning(f"[Discovery] Method 3: raw response mẫu='{snippet}'")

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
