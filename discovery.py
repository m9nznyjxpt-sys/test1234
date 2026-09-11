import asyncio
import json
import logging
import re
import subprocess
import sys
import time
from typing import Set, Callable, Awaitable

try:
    from playwright.async_api import async_playwright, Browser, Playwright
except ImportError:
    async_playwright = None
    Browser = None
    Playwright = None

logger = logging.getLogger(__name__)

SCAN_INTERVAL   = 75     # quét mỗi 75s — chậm hơn bản HTTP thô vì cần load trang thật
MAX_STREAMERS   = 150    # tối đa theo dõi cùng lúc
STALE_TIMEOUT   = 600    # xóa streamer offline quá 10 phút
PAGE_TIMEOUT_MS = 30000  # timeout tải trang (30s)
SCROLL_PASSES   = 3      # số lần cuộn trang để kích hoạt load thêm phòng (lazy-load)

# Các endpoint mà JS của TikTok tự gọi (kèm chữ ký hợp lệ) khi trang render —
# ta không tự gọi các endpoint này nữa, mà "nghe lén" response thật của trình
# duyệt khi nó tự chạy, nên luôn có chữ ký đúng do chính TikTok tạo ra.
WATCHED_URL_PATTERNS = (
    "webcast/room/list",
    "api/recommend/itemlist",
    "api/live/",
    "webcast/feed",
)

OnNewStreamer = Callable[[str], Awaitable[None]]
OnDropStreamer = Callable[[str], Awaitable[None]]


class LiveDiscovery:
    """Tự động tìm streamer đang live trên TikTok bằng trình duyệt ẩn (Playwright).

    Lý do đổi từ gọi HTTP trực tiếp sang headless browser: TikTok yêu cầu các
    request tới API nội bộ (room list, recommend...) phải có chữ ký (X-Bogus,
    msToken...) được JavaScript của chính TikTok sinh ra lúc chạy trong trình
    duyệt thật. Gọi thẳng bằng requests/cloudscraper sẽ luôn bị từ chối với
    lỗi "Url does not match" dù request trông giống hệt request thật.
    """

    def __init__(self, on_add: OnNewStreamer, on_remove: OnDropStreamer):
        self.on_add    = on_add
        self.on_remove = on_remove
        self._active: dict[str, float] = {}   # username → last_seen timestamp
        self._task: asyncio.Task | None = None
        self._running = False
        self._playwright: "Playwright | None" = None
        self._browser: "Browser | None" = None
        self._install_attempted = False

    def start(self):
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._loop(), name="discovery")
            logger.info("🔍 Auto-discovery (Playwright) đã bắt đầu")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._close_browser()

    # ──────────────────────────────────────────
    # BROWSER LIFECYCLE
    # ──────────────────────────────────────────
    async def _ensure_browser(self) -> bool:
        if async_playwright is None:
            logger.warning(
                "playwright chưa cài — pip install playwright && playwright install chromium"
            )
            return False
        if self._browser is not None:
            return True
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-setuid-sandbox",
                ],
            )
            logger.info("[Discovery] Đã khởi động Chromium headless")
            return True
        except Exception as e:
            is_missing_binary = "Executable doesn't exist" in str(e)
            if is_missing_binary and not self._install_attempted:
                # Chromium chưa được cài lúc build (ví dụ Railway bỏ qua bước
                # nixpacks.toml) — tự cài ngay lúc chạy, chỉ thử 1 lần để
                # tránh lặp vô hạn nếu môi trường không cho phép cài.
                self._install_attempted = True
                logger.warning(
                    "[Discovery] Chưa có Chromium — đang tự cài lúc runtime "
                    "(có thể mất 1-2 phút, chỉ xảy ra 1 lần)..."
                )
                if await self._install_chromium():
                    return await self._ensure_browser()

            logger.error(
                f"[Discovery] Không khởi động được Chromium: {type(e).__name__}: {e}"
            )
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
            self._playwright = None
            self._browser = None
            return False

    async def _install_chromium(self) -> bool:
        """Chạy `playwright install chromium` ngay lúc runtime nếu chưa có sẵn."""
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "playwright", "install", "chromium",
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            if proc.returncode == 0:
                logger.info("[Discovery] Cài Chromium thành công")
                return True
            logger.error(
                f"[Discovery] Cài Chromium thất bại (mã {proc.returncode}): "
                f"{out.decode(errors='ignore')[-500:]}"
            )
            return False
        except Exception as e:
            logger.error(f"[Discovery] Lỗi khi tự cài Chromium: {type(e).__name__}: {e}")
            return False

    async def _close_browser(self):
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    # ──────────────────────────────────────────
    # MAIN LOOP
    # ──────────────────────────────────────────
    async def _loop(self):
        while self._running:
            try:
                found = await self._fetch_live_usernames()
                if found:
                    await self._sync(found)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Discovery lỗi: {type(e).__name__}: {e}")

            await asyncio.sleep(SCAN_INTERVAL)

    # ──────────────────────────────────────────
    # FETCH — mở trang thật, nghe lén response JS tự gọi
    # ──────────────────────────────────────────
    async def _fetch_live_usernames(self) -> set[str]:
        if not await self._ensure_browser():
            return set()

        usernames: set[str] = set()
        captured = 0

        context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="vi-VN",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        async def on_response(response):
            nonlocal captured
            url = response.url
            if not any(p in url for p in WATCHED_URL_PATTERNS):
                return
            try:
                data = await response.json()
            except Exception:
                return
            captured += 1
            usernames.update(self._extract_usernames(data))

        page.on("response", on_response)

        try:
            await page.goto(
                "https://www.tiktok.com/live",
                wait_until="networkidle",
                timeout=PAGE_TIMEOUT_MS,
            )
            # Cuộn trang để kích hoạt lazy-load thêm phòng live
            for _ in range(SCROLL_PASSES):
                await page.mouse.wheel(0, 2500)
                await page.wait_for_timeout(1200)
        except Exception as e:
            logger.warning(f"[Discovery] Playwright load trang lỗi: {type(e).__name__}: {e}")
        finally:
            await page.close()
            await context.close()

        if not usernames:
            logger.warning(
                f"[Discovery] Không tìm được streamer nào (đã bắt {captured} response JSON khớp pattern theo dõi)"
            )
        else:
            logger.info(f"[Discovery] Tìm được {len(usernames)} streamer từ {captured} response")

        return usernames

    @staticmethod
    def _extract_usernames(data) -> set[str]:
        """Bóc uniqueId/display_id từ bất kỳ response JSON nào TikTok trả về."""
        usernames: set[str] = set()
        try:
            raw = json.dumps(data)
        except Exception:
            return usernames

        for uid in re.findall(
            r'"(?:uniqueId|unique_id|display_id)"\s*:\s*"([A-Za-z0-9_.]{3,})"', raw
        ):
            usernames.add(uid.lower())

        return usernames

    # ──────────────────────────────────────────
    # SYNC — thêm mới / xóa stale
    # ──────────────────────────────────────────
    async def _sync(self, found: set[str]):
        now = time.time()

        for u in found:
            self._active[u] = now

        for u in found:
            if len(self._active) <= MAX_STREAMERS:
                await self.on_add(u)   # MultiMonitor.add() tự bỏ qua nếu đã có

        stale = [u for u, ts in self._active.items() if now - ts > STALE_TIMEOUT]
        for u in stale:
            logger.info(f"[Discovery] @{u} offline, xóa khỏi danh sách")
            del self._active[u]
            await self.on_remove(u)

        logger.info(
            f"[Discovery] Đang theo dõi {len(self._active)} streamer | "
            f"Tìm thấy {len(found)} live lần này"
        )
