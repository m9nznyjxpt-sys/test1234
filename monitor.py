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

logger = logging.getLogger(__name__)

# Callback type: (event_type, username, data_dict)
NotifyCallback = Callable[[str, str, dict], Awaitable[None]]

RETRY_DELAY_NOT_LIVE = 60       # giây chờ khi streamer chưa live
RETRY_DELAY_DISCONNECTED = 30   # giây chờ sau khi bị ngắt kết nối
RETRY_DELAY_ERROR = 45          # giây chờ khi lỗi bất thường
COOLDOWN_SECONDS = 600          # 10 phút cooldown cùng streamer + cùng loại event


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
        while self._running:
            try:
                is_live = await asyncio.get_event_loop().run_in_executor(
                    None, TikTokLiveClient.is_live, None, self.username
                )
                if not is_live:
                    logger.info(f"@{self.username} chưa live, thử lại sau {RETRY_DELAY_NOT_LIVE}s")
                    await asyncio.sleep(RETRY_DELAY_NOT_LIVE)
                    continue

                await self._connect_and_listen()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"@{self.username} lỗi không mong muốn: {e}")
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
            if isinstance(event, SuperFanBoxEvent):
                return
            if event.display != EnvelopeDisplay.NEW:
                return

            info = event.envelope_info
            if not info:
                return

            # Chống gửi trùng cùng 1 envelope_id
            eid = info.envelope_id or ""
            if eid and eid in self._seen_envelopes:
                return
            if eid:
                self._seen_envelopes.add(eid)
                if len(self._seen_envelopes) > 100:
                    self._seen_envelopes.clear()

            # Cooldown: không gửi nếu vừa báo túi từ streamer này
            now = time.time()
            if now - self._last_notify.get("bag", 0) < COOLDOWN_SECONDS:
                return
            self._last_notify["bag"] = now

            data = {
                "diamond_count": info.diamond_count or 0,
                "people_count":  info.people_count or 0,
                "vote_count":    info.vote_count or 0,
                "sender":        info.send_user_name or self.username,
            }
            await self.notify("bag", self.username, data)

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

            now = time.time()
            if now - self._last_notify.get("chest", 0) < COOLDOWN_SECONDS:
                return
            self._last_notify["chest"] = now

            data = {
                "diamond_count": info.diamond_count or 0,
                "people_count":  info.people_count or 0,
            }
            await self.notify("chest", self.username, data)

        try:
            await client.connect()
            # connect() là blocking cho đến khi live kết thúc
        except Exception as e:
            logger.warning(f"@{self.username} mất kết nối: {e}")
            await asyncio.sleep(RETRY_DELAY_DISCONNECTED)
        finally:
            self._client = None


class MultiMonitor:
    """Quản lý nhiều StreamerMonitor song song."""

    def __init__(self, notify: NotifyCallback):
        self.notify = notify
        self._monitors: Dict[str, StreamerMonitor] = {}

    def get_list(self) -> list[str]:
        return sorted(self._monitors.keys())

    async def add(self, username: str) -> bool:
        """Thêm streamer. Trả về False nếu đã tồn tại."""
        key = username.lstrip("@").lower()
        if key in self._monitors:
            return False
        monitor = StreamerMonitor(key, self.notify)
        self._monitors[key] = monitor
        monitor.start()
        return True

    async def remove(self, username: str) -> bool:
        """Xóa streamer. Trả về False nếu không tìm thấy."""
        key = username.lstrip("@").lower()
        if key not in self._monitors:
            return False
        await self._monitors[key].stop()
        del self._monitors[key]
        return True

    async def stop_all(self):
        for monitor in list(self._monitors.values()):
            await monitor.stop()
        self._monitors.clear()
