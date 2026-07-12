# ---- Base image ----
FROM python:3.12-slim

# ---- Minimal system deps ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ---- Working directory ----
WORKDIR /app

# ---- Python dependencies ----
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Application code ----
COPY main.py notifier.py storage.py ./
COPY config/ config/
COPY core/ core/
COPY scrape_logs/ scrape_logs/
COPY models/ models/
COPY repositories/ repositories/
COPY services/ services/
COPY routes/ routes/
COPY parsers/ parsers/
COPY scraper/ scraper/
COPY templates/ templates/
COPY static/ static/

# ---- Render uses $PORT (default 10000) ----
EXPOSE 10000

# ---- Healthcheck ----
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${PORT:-10000}/health || exit 1

# ---- Run with gunicorn ----
# --workers doit rester à 1 : le scheduler et la dédup de scrapes en mémoire
# (_scrape_futures) ne sont pas partagés entre process. Voir core/scrape_control.py
# et le verrou consultatif Postgres dans main.py::_try_acquire_scheduler_lock.
CMD gunicorn --bind 0.0.0.0:${PORT:-10000} --workers 1 --timeout 30 --graceful-timeout 10 "main:create_app()"
