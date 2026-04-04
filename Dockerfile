# ---- Base image ----
FROM python:3.12-slim

# ---- System deps + Chrome for SeleniumBase ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    gnupg \
    ca-certificates \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# ---- Working directory ----
WORKDIR /app

# ---- Python dependencies ----
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
