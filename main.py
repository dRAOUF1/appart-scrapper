"""
SeLoger API Platform — Point d'entrée unique.

Sert à la fois l'API REST (/api/*) et le frontend web (/) depuis
un seul processus Flask, compatible Render (un seul web service).

Le scraping est fait côté serveur via les API SeLoger (BFF + classified-search).
Architecture parallèle :
  - ThreadPoolExecutor(max_workers=1) : max 1 scrape à la fois (évite OOM)
  - APScheduler : scheduling propre sans threads bloqués
  - Connexions DB thread-safe avec health check
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from flask import Flask, g
from loguru import logger

from config import load_config
from notifier import Notifier
from storage import Storage

_scrape_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scrape")
_scrape_futures: dict[int, object] = {}


def create_app() -> Flask:
    startup_start = time.monotonic()
    config = load_config()

    logger.remove()
    logger.add(
        sys.stderr,
        level=config.log_level,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{module}</cyan>:<cyan>{function}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    app.config["APP_CONFIG"] = config
    app.storage = Storage(database_url=config.database.database_url)
    app.notifier = Notifier(
        server=config.ntfy.server,
        priority=config.ntfy.priority,
    )
    app._scrape_executor = _scrape_executor
    app._scrape_futures = _scrape_futures

    @app.before_request
    def before_request():
        g._db_conn = app.storage._get_conn()

    @app.teardown_request
    def teardown_request(exception):
        if hasattr(g, '_db_conn') and g._db_conn is not None:
            try:
                if not g._db_conn.closed:
                    g._db_conn.close()
            except Exception:
                pass
            g._db_conn = None

    from datetime import datetime, timezone, date
    from zoneinfo import ZoneInfo

    @app.template_filter("parse_iso_date")
    def parse_iso_date(value):
        if not value:
            return None
        try:
            value = value.replace("Z", "+00:00")
            return datetime.fromisoformat(value)
        except (ValueError, AttributeError):
            return None

    FR_TZ = ZoneInfo("Europe/Paris")

    @app.template_filter("fr_time")
    def fr_time(dt):
        if dt is None:
            return None
        if isinstance(dt, date) and not isinstance(dt, datetime):
            return datetime(dt.year, dt.month, dt.day, tzinfo=FR_TZ)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(FR_TZ)

    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    @app.context_processor
    def inject_admin():
        import datetime
        return {
            "admin_username": os.environ.get("ADMIN_USERNAME", "admin"),
            "now": lambda: datetime.datetime.now(FR_TZ),
        }

    _start_background_tasks(app)

    from routes import api_bp, web_bp, admin_bp
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(web_bp)
    app.register_blueprint(admin_bp)

    startup_elapsed = time.monotonic() - startup_start
    logger.info(f"Appart Tracker — démarré (mode scraper) en {startup_elapsed:.2f}s")
    return app


def _start_background_tasks(app: Flask):
    """Démarre le scheduler APScheduler. Rien de bloquant."""
    from apscheduler.schedulers.background import BackgroundScheduler
    from datetime import datetime, timedelta

    scheduler = BackgroundScheduler(daemon=True)

    def scheduled_scrape_job():
        try:
            with app.app_context():
                all_users = app.storage.get_all_users()
                now = datetime.utcnow()
                for user in all_users:
                    searches = app.storage.get_user_searches(user["id"])
                    for s in searches:
                        if not s.get("is_active", True):
                            continue
                        criteria = s.get("criteria", {})
                        if not criteria or not isinstance(criteria, dict):
                            continue
                        if not criteria.get("placeIds"):
                            continue

                        interval = s.get("scrape_interval", 5)
                        last_scraped = s.get("last_scraped")

                        if last_scraped:
                            if isinstance(last_scraped, str):
                                last_scraped = datetime.fromisoformat(last_scraped)
                            threshold = now - timedelta(minutes=interval)
                            if last_scraped > threshold:
                                continue

                        search_id = s["id"]
                        if search_id in _scrape_futures:
                            fut = _scrape_futures[search_id]
                            if not fut.done():
                                continue
                            else:
                                del _scrape_futures[search_id]

                        from services.scrape_service import ScrapeService
                        fut = _scrape_executor.submit(
                            ScrapeService(app).execute, search_id, user["id"]
                        )
                        _scrape_futures[search_id] = fut
        except Exception as e:
            logger.error(f"Erreur scheduled_scrape_job: {e}")

    scheduler.add_job(scheduled_scrape_job, "interval", seconds=30, id="scrape_scheduler", max_instances=1)
    scheduler.start()
    logger.info("Scrape scheduler démarré (toutes les 30s)")


if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Serveur Flask sur le port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
