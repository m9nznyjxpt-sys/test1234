# Dùng Dockerfile thay nixpacks.toml vì Railway/Nix thiếu libglib cho Chromium.
# Ubuntu slim có đủ apt packages mà Playwright cần.
FROM python:3.11-slim

# Cài system libraries mà Chromium headless cần
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxcb1 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libwayland-client0 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cài Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cài Chromium cho Playwright (chỉ chromium, không cài Firefox/WebKit)
RUN playwright install chromium

# Copy code
COPY . .

CMD ["python", "main.py"]
