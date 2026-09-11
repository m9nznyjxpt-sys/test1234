# TikTok LIVE Scanner Bot

Bot Telegram quét **rương (chest)** và **túi may mắn (lucky bag)** trong TikTok LIVE.

---

## Deploy trên Replit

### 1. Tạo Replit mới (Python)

Upload 3 file: `main.py`, `monitor.py`, `bot.py`

### 2. Cài thư viện

Trong Shell:
```bash
pip install TikTokLive "python-telegram-bot[ext]"
```

Hoặc tạo file `requirements.txt` và Replit sẽ tự cài.

### 3. Cấu hình Secrets

Vào **Secrets** (🔒) trong Replit, thêm:

| Key | Value |
|-----|-------|
| `BOT_TOKEN` | Token từ @BotFather |
| `NOTIFY_CHAT_ID` | ID nhóm/kênh nhận thông báo (xem bên dưới) |

**Lấy NOTIFY_CHAT_ID:**
- Thêm bot vào nhóm → gửi `/start` → bot sẽ tự ghi lại chat ID
- Hoặc dùng `@userinfobot` để lấy ID nhóm

> Nếu không set `NOTIFY_CHAT_ID`, bot sẽ gửi thông báo về chat của người dùng cuối cùng dùng lệnh `/add`

### 4. Chạy

```bash
python main.py
```

---

## Lệnh Bot

| Lệnh | Mô tả |
|------|-------|
| `/add @username` | Thêm streamer vào danh sách theo dõi |
| `/remove @username` | Xóa streamer khỏi danh sách |
| `/list` | Xem danh sách đang theo dõi |
| `/help` | Hướng dẫn |

---

## Thông báo mẫu

**Túi may mắn:**
```
🎁 TÚI MAY MẮN xuất hiện!

👤 Streamer:  @username
💎 Giá trị:  5,000 kim cương
🏆 Số người nhận: 10 người
🎯 Đang tham gia: 234 người

⚡ Vào @username ngay để nhận!
```

**Rương:**
```
📦 RƯƠNG xuất hiện!

👤 Streamer:  @username  
💎 Giá trị:  2,000 kim cương
🏆 Số người nhận: 5 người

⚡ Vào @username ngay để mở rương!
```

---

## Lưu ý

- Bot tự động kiểm tra lại mỗi 60 giây nếu streamer chưa live
- Khi streamer bắt đầu live, bot tự kết nối
- Có thể theo dõi không giới hạn số streamer cùng lúc
- `kim cương` = đơn vị Diamond của TikTok (hiển thị số thô từ API)
