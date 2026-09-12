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

SCAN_INTERVAL   = 90
MAX_STREAMERS   = 150
STALE_TIMEOUT   = 600
PAGE_TIMEOUT_MS = 35000

# CHỈ dùng endpoint thuộc namespace "webcast" (hệ thống live của TikTok) hoặc
# "api/live/". KHÔNG dùng "api/recommend/itemlist", "aweme/v1/feed",
# "api/mix/list", "api/explore/item_list" — đó là feed VIDEO thường (For You /
# Explore), trả về uniqueId của người đăng video, không liên quan gì đến live.
# Đây chính là nguyên nhân bot trước đó thêm hàng loạt acc "không live".
WATCHED_URL_PATTERNS = (
    "webcast/room/list",
    "webcast/room/search",
    "webcast/feed/",
    "api/live/",
)

OnNewStreamer  = Callable[[str], Awaitable[None]]
OnDropStreamer = Callable[[str], Awaitable[None]]


class LiveDiscovery:
    def __init__(self, on_add: OnNewStreamer, on_remove: OnDropStreamer):
        self.on_add    = on_add
        self.on_remove = on_remove
        self._active: dict[str, float] = {}
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
            logger.warning("playwright chưa cài")
            return False

        # BUG CŨ: chỉ check `self._browser is not None` — nếu Chromium đã
        # crash (ví dụ bị OOM-kill), biến này vẫn giữ tham chiếu tới 1 browser
        # đã chết, khiến hàm này luôn trả True mà không bao giờ khởi động lại,
        # làm mọi lần quét sau đó thất bại vĩnh viễn với "Connection closed".
        if self._browser is not None:
            if self._browser.is_connected():
                return True
            logger.warning("[Discovery] Chromium cũ đã chết (mất kết nối) — khởi động lại")
            await self._close_browser()

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            logger.info("[Discovery] Đã khởi động Chromium headless")
            return True
        except Exception as e:
            is_missing = "Executable doesn't exist" in str(e)
            if is_missing and not self._install_attempted:
                self._install_attempted = True
                logger.warning("[Discovery] Chưa có Chromium — đang tự cài...")
                if await self._install_chromium():
                    return await self._ensure_browser()
            logger.error(f"[Discovery] Không khởi động được Chromium: {type(e).__name__}: {e}")
            await self._close_browser()
            return False

    async def _install_chromium(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "playwright", "install", "chromium",
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            if proc.returncode == 0:
                logger.info("[Discovery] Cài Chromium thành công")
                return True
            logger.error(f"[Discovery] Cài Chromium thất bại: {out.decode(errors='ignore')[-300:]}")
            return False
        except Exception as e:
            logger.error(f"[Discovery] Lỗi cài Chromium: {e}")
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
                else:
                    logger.warning("[Discovery] Không tìm được streamer nào lần này")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Discovery lỗi: {type(e).__name__}: {e}")
            await asyncio.sleep(SCAN_INTERVAL)

    # ──────────────────────────────────────────
    # FETCH — 2 chiến lược song song
    # ──────────────────────────────────────────
    async def _fetch_live_usernames(self) -> set[str]:
        if not await self._ensure_browser():
            return set()

        usernames: set[str] = set()
        captured_responses = 0
        context = None

        try:
            context = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="vi-VN",
                viewport={"width": 1280, "height": 900},
                # Tắt WebDriver flag để TikTok không detect bot
                extra_http_headers={
                    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
                },
            )

            # Chặn resource không cần thiết để load nhanh hơn
            await context.route(
                "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,mp4,mp3}",
                lambda route: route.abort()
            )

            page = await context.new_page()

            # Ẩn navigator.webdriver
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            """)

            async def on_response(response):
                nonlocal captured_responses
                url = response.url
                if not any(p in url for p in WATCHED_URL_PATTERNS):
                    return
                try:
                    data = await response.json()
                    captured_responses += 1
                    usernames.update(self._walk_extract_live_usernames(data))
                except Exception:
                    pass

            page.on("response", on_response)

            # ── Chiến lược 1: trang /live chính
            try:
                await page.goto(
                    "https://www.tiktok.com/live",
                    wait_until="domcontentloaded",   # nhanh hơn networkidle
                    timeout=PAGE_TIMEOUT_MS,
                )
                # Đợi thêm để XHR kịp fire
                await page.wait_for_timeout(4000)

                # Cuộn để kích lazy-load
                for _ in range(3):
                    await page.mouse.wheel(0, 2000)
                    await page.wait_for_timeout(1500)

                # Chiến lược 2: extract từ SIGI_STATE / UNIVERSAL_DATA nhúng trong
                # HTML của chính trang /live — CHỈ dùng trang /live, không dùng
                # /explore, vì /explore là feed video thường, không phải live.
                html = await page.content()
                usernames.update(self._extract_from_html(html))

            except Exception as e:
                logger.warning(f"[Discovery] Load /live lỗi: {type(e).__name__}: {e}")

            await page.close()
            await context.close()

        except Exception as e:
            # BUG CŨ: lỗi ở đây (ví dụ Chromium đã crash, "Connection closed
            # while reading from the driver") chỉ bị log ở _loop() rồi bỏ qua,
            # còn self._browser vẫn giữ tham chiếu chết → mọi lần quét sau đó
            # thất bại y hệt, vĩnh viễn, không bao giờ tự hồi phục.
            # Giờ reset ngay để lần quét kế tiếp tự khởi động Chromium mới.
            logger.error(
                f"[Discovery] Lỗi khi dùng browser — reset để lần sau khởi động lại: "
                f"{type(e).__name__}: {e}"
            )
            await self._close_browser()
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            return usernames

        logger.info(
            f"[Discovery] Kết quả: {len(usernames)} username | "
            f"XHR bắt được: {captured_responses} response"
        )
        return usernames

    # ──────────────────────────────────────────
    # EXTRACT từ JSON response (XHR)
    # ──────────────────────────────────────────
    @staticmethod
    def _extract_usernames(data) -> set[str]:
        # Giữ lại (không còn gọi) — thay bằng _walk_extract_live_usernames bên dưới.
        return set()

    # ──────────────────────────────────────────
    # EXTRACT có kiểm tra tín hiệu LIVE thật
    # ──────────────────────────────────────────
    # Trước đây bot lấy MỌI uniqueId xuất hiện bất kỳ đâu trong response,
    # kể cả response của feed video thường → thêm nhầm acc không live.
    # Giờ chỉ lấy uniqueId nằm TRONG (hoặc bên dưới) 1 node JSON có tín hiệu
    # phòng live thật (status==2 / live_status==1 / có room_id kèm stream info).
    @classmethod
    def _walk_extract_live_usernames(cls, node, live_ctx: bool = False, usernames: set | None = None) -> set[str]:
        if usernames is None:
            usernames = set()

        if isinstance(node, dict):
            self_live = False

            status = node.get("status")
            if isinstance(status, int) and status == 2:
                self_live = True

            live_status = node.get("live_status", node.get("liveStatus"))
            if isinstance(live_status, int) and live_status == 1:
                self_live = True

            room_id = node.get("room_id") or node.get("roomId") or node.get("id_str")
            has_stream_info = any(
                k in node for k in ("stream_url", "streamUrl", "rtmp_pull_url", "hls_pull_url")
            )
            if room_id and str(room_id) not in ("0", "") and has_stream_info:
                self_live = True

            new_ctx = live_ctx or self_live

            if new_ctx:
                for key in ("uniqueId", "unique_id", "display_id"):
                    val = node.get(key)
                    if isinstance(val, str) and len(val) >= 3:
                        usernames.add(val.lower())

            for v in node.values():
                cls._walk_extract_live_usernames(v, new_ctx, usernames)

        elif isinstance(node, list):
            for item in node:
                cls._walk_extract_live_usernames(item, live_ctx, usernames)

        return usernames

    # ──────────────────────────────────────────
    # EXTRACT trực tiếp từ HTML page /live (fallback)
    # ──────────────────────────────────────────
    @classmethod
    def _extract_from_html(cls, html: str) -> set[str]:
        usernames: set[str] = set()
        if not html:
            return usernames

        for pattern in [
            r'<script id="SIGI_STATE"[^>]*>(.*?)</script>',
            r'id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
            r'window\.__INIT_PROPS__\s*=\s*(\{.*?\});',
        ]:
            match = re.search(pattern, html, re.DOTALL)
            if not match:
                continue
            blob = match.group(1)
            try:
                # Parse JSON thật để duyệt cây có ngữ cảnh, thay vì regex mù toàn blob
                data = json.loads(blob)
                found = cls._walk_extract_live_usernames(data)
                if found:
                    usernames.update(found)
                    logger.info(f"[Discovery] HTML fallback: {len(found)} username từ SIGI_STATE (có kiểm tra live)")
                    break
            except Exception:
                continue

        return usernames

    # ──────────────────────────────────────────
    # SYNC
    # ──────────────────────────────────────────
    async def _sync(self, found: set[str]):
        now = time.time()
        for u in found:
            self._active[u] = now
        for u in found:
            if len(self._active) <= MAX_STREAMERS:
                await self.on_add(u)
        stale = [u for u, ts in self._active.items() if now - ts > STALE_TIMEOUT]
        for u in stale:
            logger.info(f"[Discovery] @{u} offline → xóa")
            del self._active[u]
            await self.on_remove(u)
        logger.info(
            f"[Discovery] Theo dõi {len(self._active)} | Tìm thấy {len(found)} live"
        )
