import asyncio
import logging
import time
from typing import Callable, Awaitable, Dict, Set
from TikTokLive import TikTokLiveClient
from TikTokLive.events import (
    EnvelopeEvent,
    SuperFanBoxEvent,
    ConnectEvent,
    DisconnectEvent,
    LiveEndEvent,
)
from TikTokLive.proto import EnvelopeDisplay, EnvelopeBusinessType
import storage

logger = logging.getLogger(__name__)

# Callback type: (event_type, username, data_dict)
NotifyCallback = Callable[[str, str, dict], Awaitable[None]]

RETRY_DELAY_NOT_LIVE = 60       # giây chờ khi streamer chưa live
RETRY_DELAY_DISCONNECTED = 30   # giây chờ sau khi bị ngắt kết nối
RETRY_DELAY_ERROR = 45          # giây chờ khi lỗi bất thường
COOLDOWN_SECONDS = 600          # 10 phút cooldown cùng streamer + cùng loại event
NOTIFY_LEAD_SECONDS = 60        # chỉ báo khi còn ~1 phút nữa là rương/túi đóng


class StreamerMonitor:
    """Theo dõi một streamer TikTok duy nhất."""

    def __init__(self, username: str, notify: NotifyCallback):
        self.username = username
        self.notify = notify
        self._running = False
        self._task: asyncio.Task | None = None
        self._client: TikTokLiveClient | None = None
        # Chống spam: last_notify[event_type] = timestamp
        self._last_notify: dict[str, float] = {}
        # Chống gửi trùng cùng 1 envelope
        self._seen_envelopes: set[str] = set()

    def start(self):
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._loop(), name=f"monitor:{self.username}")

    async def stop(self):
        self._running = False
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        # Lỗi cứng — user không thể live, bỏ qua hoàn toàn
        _SKIP_ERRORS = (
            "not capable of going LIVE",
            "does not exist",
            "never gone live",
        )
        # Lỗi mềm — TikTok rate limit hoặc SSL tạm thời, thử lại sau
        _SOFT_ERRORS = (
            "Expecting value",          # empty response / rate limit
            "TLSV1_ALERT",             # SSL error tạm thời
            "JSONDecodeError",
            "ConnectionError",
            "TimeoutError",
        )

        while self._running:
            try:
                check_client = TikTokLiveClient(unique_id=self.username)
                is_live = await check_client.is_live()
                if not is_live:
                    logger.info(f"@{self.username} chưa live, thử lại sau {RETRY_DELAY_NOT_LIVE}s")
                    await asyncio.sleep(RETRY_DELAY_NOT_LIVE)
                    continue

                await self._connect_and_listen()

            except asyncio.CancelledError:
                break
            except Exception as e:
                err_str = str(e)

                # User không thể live → dừng hẳn, không retry
                if any(kw in err_str for kw in _SKIP_ERRORS):
                    logger.debug(f"@{self.username} không hỗ trợ live → bỏ qua")
                    self._running = False
                    return

                # Rate limit / SSL tạm thời → thử lại nhanh hơn
                if any(kw in err_str for kw in _SOFT_ERRORS):
                    logger.debug(f"@{self.username} lỗi tạm thời ({type(e).__name__}), thử lại sau {RETRY_DELAY_DISCONNECTED}s")
                    await asyncio.sleep(RETRY_DELAY_DISCONNECTED)
                    continue

                logger.error(f"@{self.username} lỗi: {e}")
                await asyncio.sleep(RETRY_DELAY_ERROR)

    async def _connect_and_listen(self):
        client = TikTokLiveClient(unique_id=self.username)
        self._client = client

        @client.on(ConnectEvent)
        async def on_connect(_):
            logger.info(f"✅ Đã kết nối vào live @{self.username}")

        @client.on(DisconnectEvent)
        async def on_disconnect(_):
            logger.info(f"🔌 Ngắt kết nối @{self.username}")

        @client.on(LiveEndEvent)
        async def on_live_end(_):
            logger.info(f"📴 Live kết thúc @{self.username}")
            await client.disconnect()

        # ──────────────────────────────────────────────────
        # TÚI MAY MẮN (Lucky Bag / Red Envelope)
        # ──────────────────────────────────────────────────
        @client.on(EnvelopeEvent)
        async def on_envelope(event: EnvelopeEvent):
            if event.display != EnvelopeDisplay.NEW:
                return

            info = event.envelope_info
            if not info:
                return

            # ── FIX QUAN TRỌNG ──
            # Thư viện TikTokLive, khi nhận 1 sự kiện RƯƠNG (Super Fan Box),
            # thực chất phát ra HAI object khác nhau cho cùng 1 gói tin gốc:
            #   1. Một EnvelopeEvent "chung chung" (parse mặc định)
            #   2. Một SuperFanBoxEvent "chuyên biệt" (parse lại có phân loại)
            # Vì đây là 2 instance Python khác nhau, `isinstance(event,
            # SuperFanBoxEvent)` ở handler này luôn False với bản EnvelopeEvent
            # chung — nên rương vẫn lọt qua đây và bị báo nhầm thành túi.
            # Tệ hơn: việc báo nhầm này còn tự set cooldown "bag" cho streamer,
            # khiến túi thật xuất hiện ngay sau đó trong 10 phút bị nuốt mất
            # vì tưởng vừa mới báo túi rồi.
            #
            # Sửa bằng cách tự kiểm tra business_type / display marker —
            # đúng cách chính thư viện dùng để phân loại rương — thay vì dựa
            # vào isinstance trên instance có thể không phải bản đã phân loại.
            business_type = getattr(info, "business_type", None)
            is_actually_chest = business_type == EnvelopeBusinessType.SUPER_FAN_BOX
            if not is_actually_chest:
                try:
                    from TikTokLive.proto.proto_utils import common_display_type
                    if "ttlive_superfanbox" in common_display_type(event.common).lower():
                        is_actually_chest = True
                except Exception:
                    pass
            if is_actually_chest:
                return  # Đây thực chất là rương — để on_chest xử lý

            # Chống gửi trùng cùng 1 envelope_id
            eid = info.envelope_id or ""
            if eid and eid in self._seen_envelopes:
                return
            if eid:
                self._seen_envelopes.add(eid)
                if len(self._seen_envelopes) > 100:
                    self._seen_envelopes.clear()

            data = {
                "diamond_count": info.diamond_count or 0,
                "people_count":  info.people_count or 0,
                "vote_count":    info.vote_count or 0,
                "sender":        info.send_user_name or self.username,
                "link":          f"https://www.tiktok.com/@{self.username}/live",
            }
            # Không gửi ngay — đợi tới khi còn ~1 phút nữa mới báo
            asyncio.create_task(self._schedule_notify("bag", info.unpack_at, data))

        # ──────────────────────────────────────────────────
        # RƯƠNG (Super Fan Box / Chest)
        # ──────────────────────────────────────────────────
        @client.on(SuperFanBoxEvent)
        async def on_chest(event: SuperFanBoxEvent):
            if event.display != EnvelopeDisplay.NEW:
                return

            info = event.envelope_info
            if not info:
                return

            eid = info.envelope_id or ""
            if eid and eid in self._seen_envelopes:
                return
            if eid:
                self._seen_envelopes.add(eid)

            data = {
                "diamond_count": info.diamond_count or 0,
                "people_count":  info.people_count or 0,
                "link":          f"https://www.tiktok.com/@{self.username}/live",
            }
            asyncio.create_task(self._schedule_notify("chest", info.unpack_at, data))

        try:
            await client.connect()
            # connect() là blocking cho đến khi live kết thúc
        except Exception as e:
            logger.warning(f"@{self.username} mất kết nối: {e}")
            await asyncio.sleep(RETRY_DELAY_DISCONNECTED)
        finally:
            self._client = None

    # ──────────────────────────────────────────
    # GỬI THÔNG BÁO TRỄ — chỉ báo khi còn ~1 phút
    # ──────────────────────────────────────────
    def _compute_delay(self, unpack_at) -> float:
        """Trả về số giây cần chờ trước khi gửi thông báo (còn ~NOTIFY_LEAD_SECONDS giây)."""
        try:
            ts = float(unpack_at)
        except (TypeError, ValueError):
            return 0.0

        if ts <= 0:
            return 0.0
        if ts > 1e12:          # timestamp ở dạng mili-giây
            ts /= 1000.0

        remaining = ts - time.time()
        # Nếu remaining bất thường (âm, hoặc quá xa >1h) thì gửi ngay, không đoán mò
        if remaining <= NOTIFY_LEAD_SECONDS or remaining > 3600:
            return 0.0
        return remaining - NOTIFY_LEAD_SECONDS

    async def _schedule_notify(self, event_type: str, unpack_at, data: dict):
        delay = self._compute_delay(unpack_at)
        if delay > 0:
            logger.info(f"@{self.username} [{event_type}] đợi {delay:.0f}s rồi mới báo")
            await asyncio.sleep(delay)

        # Cooldown check thực hiện ngay trước khi gửi (không phải lúc phát hiện event)
        now = time.time()
        if now - self._last_notify.get(event_type, 0) < COOLDOWN_SECONDS:
            return
        self._last_notify[event_type] = now

        await self.notify(event_type, self.username, data)


class MultiMonitor:
    """Quản lý nhiều StreamerMonitor song song."""

    def __init__(self, notify: NotifyCallback):
        self.notify = notify
        self._monitors: Dict[str, StreamerMonitor] = {}

    def get_list(self) -> list[str]:
        return sorted(self._monitors.keys())

    async def add(self, username: str, source: str = "manual") -> bool:
        """Thêm streamer. Trả về False nếu đã tồn tại."""
        key = username.lstrip("@").lower()
        if key in self._monitors:
            return False
        monitor = StreamerMonitor(key, self.notify)
        self._monitors[key] = monitor
        monitor.start()
        storage.save_watch(key, source)   # lưu xuống DB để nhớ sau restart
        return True

    async def remove(self, username: str) -> bool:
        """Xóa streamer. Trả về False nếu không tìm thấy."""
        key = username.lstrip("@").lower()
        if key not in self._monitors:
            return False
        await self._monitors[key].stop()
        del self._monitors[key]
        storage.remove_watch(key)
        return True

    async def stop_all(self):
        for monitor in list(self._monitors.values()):
            await monitor.stop()
        self._monitors.clear()
