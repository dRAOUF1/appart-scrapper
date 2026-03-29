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
COPY main.py config.py notifier.py storage.py ./
COPY parsers/ parsers/
COPY config.yaml .
COPY templates/ templates/
COPY static/ static/

# ---- Render uses $PORT (default 10000) ----
EXPOSE 10000

# ---- Run with gunicorn ----
CMD gunicorn --bind 0.0.0.0:${PORT:-10000} --workers 2 --timeout 120 "main:create_app()"
