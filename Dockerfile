# ---- Base image ----
FROM python:3.12-slim

# ---- System dependencies: Chromium + driver ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    fonts-liberation \
    libnss3 \
    libxss1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libgbm1 \
    wget \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ---- Chrome env vars (used by scraper.py _find_chrome_binary) ----
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver
ENV DISPLAY=:99

# ---- Working directory ----
WORKDIR /app

# ---- Python dependencies ----
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Application code ----
COPY main.py scraper.py config.py notifier.py storage.py ./

# ---- Config (can be overridden via volume/mount) ----
COPY config.yaml .

# ---- Render uses $PORT (default 10000) ----
EXPOSE 10000

# ---- Run ----
ENTRYPOINT ["python", "main.py"]
