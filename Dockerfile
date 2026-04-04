# ---- Base image ----
FROM python:3.12-slim

# ---- System deps + Firefox for Camoufox ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    gnupg \
    ca-certificates \
    libgtk-3-0 \
    libdbus-glib-1-2 \
    libasound2 \
    libx11-xcb1 \
    libxtst6 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libpango-1.0-0 \
    libcairo2 \
    libatk1.0-0 \
    libgbm1 \
    fonts-liberation \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# ---- Working directory ----
WORKDIR /app

# ---- Python dependencies ----
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Download Camoufox browser binary ----
RUN python -m camoufox fetch

# ---- Application code ----
COPY main.py config.py notifier.py storage.py log_manager.py ./
COPY parsers/ parsers/
COPY scraper/ scraper/
COPY config.yaml .
COPY templates/ templates/
COPY static/ static/

# ---- Render uses $PORT (default 10000) ----
EXPOSE 10000

# ---- Run with gunicorn ----
CMD gunicorn --bind 0.0.0.0:${PORT:-10000} --workers 2 --timeout 120 "main:create_app()"
