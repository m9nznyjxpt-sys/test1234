"""
Lưu trữ dữ liệu tự động bằng SQLite — chạy local, không cần dịch vụ ngoài.

Lưu 2 thứ:
1. watchlist  — danh sách streamer đang theo dõi (để bot tự nhớ lại sau khi
   restart, không cần /add lại từ đầu hay chờ discovery tìm lại)
2. events     — lịch sử mỗi lần rương/túi xuất hiện (nền tảng cho /stats)

⚠️ LƯU Ý QUAN TRỌNG VỀ RAILWAY:
Theo mặc định, ổ đĩa của container trên Railway là "ephemeral" — nghĩa là
file database này SẼ MẤT mỗi khi bạn redeploy (không phải restart thường,
mà là khi build lại từ code mới). Muốn dữ liệu sống sót qua các lần redeploy,
cần vào Railway → service → Settings → Volumes → tạo 1 Volume, mount vào
đúng thư mục project (ví dụ /app/data), rồi set biến môi trường DB_PATH
trỏ vào đó (ví dụ DB_PATH=/app/data/bot_data.db). Không làm bước này thì
storage vẫn hoạt động bình thường trong lúc bot chạy, chỉ là mất sạch mỗi
lần bạn deploy code mới — không phải bug, mà là bản chất hạ tầng Railway.
"""

import os
import sqlite3
import time
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "bot_data.db")


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Tạo bảng nếu chưa có. Gọi 1 lần lúc bot khởi động."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                username TEXT PRIMARY KEY,
                source   TEXT DEFAULT 'manual',
                added_at REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL,
                event_type    TEXT NOT NULL,
                diamond_count INTEGER DEFAULT 0,
                people_count  INTEGER DEFAULT 0,
                ts            REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
    logger.info(f"[Storage] SQLite sẵn sàng tại '{DB_PATH}'")


# ──────────────────────────────────────────
# WATCHLIST — để bot tự nhớ streamer sau restart
# ──────────────────────────────────────────
def save_watch(username: str, source: str = "manual"):
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (username, source, added_at) VALUES (?, ?, ?)",
            (username, source, time.time()),
        )


def remove_watch(username: str):
    with _conn() as conn:
        conn.execute("DELETE FROM watchlist WHERE username = ?", (username,))


def load_watchlist() -> list[str]:
    with _conn() as conn:
        rows = conn.execute("SELECT username FROM watchlist").fetchall()
    return [r["username"] for r in rows]


# ──────────────────────────────────────────
# EVENTS — lịch sử rương/túi, dùng cho /stats
# ──────────────────────────────────────────
def log_event(username: str, event_type: str, diamond_count: int, people_count: int):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO events (username, event_type, diamond_count, people_count, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, event_type, diamond_count, people_count, time.time()),
        )


def get_stats(since_seconds: float | None = None) -> dict:
    """Trả về {'bag': {'count': N, 'diamonds': X}, 'chest': {...}}"""
    query = "SELECT event_type, COUNT(*) AS cnt, SUM(diamond_count) AS total FROM events"
    params: list = []
    if since_seconds is not None:
        query += " WHERE ts >= ?"
        params.append(time.time() - since_seconds)
    query += " GROUP BY event_type"

    with _conn() as conn:
        rows = conn.execute(query, params).fetchall()

    return {
        r["event_type"]: {"count": r["cnt"], "diamonds": r["total"] or 0}
        for r in rows
    }


def get_top_streamers(limit: int = 5, since_seconds: float | None = None) -> list[dict]:
    """Top streamer theo số lần rương/túi xuất hiện."""
    query = "SELECT username, COUNT(*) AS cnt, SUM(diamond_count) AS total FROM events"
    params: list = []
    if since_seconds is not None:
        query += " WHERE ts >= ?"
        params.append(time.time() - since_seconds)
    query += " GROUP BY username ORDER BY cnt DESC LIMIT ?"
    params.append(limit)

    with _conn() as conn:
        rows = conn.execute(query, params).fetchall()

    return [
        {"username": r["username"], "count": r["cnt"], "diamonds": r["total"] or 0}
        for r in rows
    ]
