"""
SeLoger API Platform — Point d'entrée unique.

Sert à la fois l'API REST (/api/*) et le frontend web (/) depuis
un seul processus Flask, compatible Render (un seul web service).

Le scraping est fait côté serveur, source par source (voir parsers/).
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
from datetime import UTC
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, g
from flask_wtf import CSRFProtect
from loguru import logger

from config import load_config
from core.scrape_control import CLE_PAUSE_SCHEDULER, CLE_VERROU_SCHEDULER
from notifier import Notifier
from storage import Storage

_scrape_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scrape")
_scrape_futures: dict[int, object] = {}


def create_app() -> Flask:
    # Chargé ici et NON au niveau module : importer `main` pour un helper (ce que
    # font les tests du scheduler) ne doit jamais peupler os.environ avec le
    # DATABASE_URL de production. Voir tests/integration/conftest.py.
    load_dotenv(Path(__file__).parent / ".env")

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
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        raise RuntimeError(
            "SECRET_KEY manquante — définissez-la dans l'environnement (.env) avant de démarrer."
        )
    app.secret_key = secret_key
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True

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
            conn = g._db_conn
            g._db_conn = None
            try:
                app.storage.release_to_pool(conn)
            except Exception:
                pass

    from datetime import date, datetime
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
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(FR_TZ)

    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    @app.context_processor
    def inject_admin():
        import datetime

        from core.criteria import location_label

        return {
            "admin_username": os.environ.get("ADMIN_USERNAME", "admin"),
            "now": lambda: datetime.datetime.now(FR_TZ),
            # Un périmètre s'affiche de la même façon partout (formulaire,
            # étiquettes des recherches, suggestions de l'autocomplete).
            "location_label": location_label,
        }

    _start_background_tasks(app)

    from routes import admin_bp, api_bp, web_bp
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(web_bp)
    app.register_blueprint(admin_bp)

    csrf = CSRFProtect(app)
    csrf.exempt(api_bp)

    startup_elapsed = time.monotonic() - startup_start
    logger.info(f"Appart Tracker — démarré (mode scraper) en {startup_elapsed:.2f}s")
    return app


# Clé arbitraire pour le verrou consultatif Postgres (un seul process doit
# faire tourner le scheduler, même si le déploiement passe un jour à
# plusieurs workers gunicorn). La valeur vit dans core/scrape_control.py
# (source unique, lue aussi par la vue file d'attente de l'admin) ; le nom
# historique est conservé comme alias pour les tests existants.
_SCHEDULER_LOCK_KEY = CLE_VERROU_SCHEDULER

# Issue #17 : la clé app_settings portant la pause GLOBALE du scheduler
# (`CLE_PAUSE_SCHEDULER`, importée de core/scrape_control.py — source unique,
# partagée avec l'admin) vaut 'true'/'false' dans app_settings. En pause, les
# scrapes PLANIFIÉS sont sautés ; les soumissions manuelles (submit_scrape
# depuis API/web/admin) restent permises.
def _pause_scheduler_active(storage) -> bool:
    """Décision testable : le scheduler planifié est-il en pause ?

    Lit la clé 'scheduler_paused' du magasin clé/valeur app_settings via
    settings_repo. Tolérante sur la casse/espaces ; en cas d'échec de lecture
    (base injoignable…), la pause est considérée INACTIVE (comportement
    fail-open : le cycle suivant retentera, et un scrape planifié rate une
    exécution plutôt que d'en rater toutes les suivantes en silence).
    """
    try:
        valeur = storage.settings.get_setting(CLE_PAUSE_SCHEDULER, "false")
    except Exception as e:
        logger.error(f"Impossible de lire l'état de pause du scheduler: {e}")
        return False
    return str(valeur).strip().lower() == "true"


def _try_acquire_scheduler_lock(database_url: str):
    """Tente d'acquérir un verrou Postgres exclusif pour ce process.

    Retourne la connexion (à garder ouverte tant que le scheduler tourne)
    si le verrou est acquis, sinon None. Empêche plusieurs workers/process
    de faire tourner le scheduler en parallèle (scrapes dupliqués) — la
    dédup en mémoire de _scrape_futures ne fonctionne qu'au sein d'un
    même process.
    """
    import psycopg2

    try:
        conn = psycopg2.connect(database_url, connect_timeout=10)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_SCHEDULER_LOCK_KEY,))
            acquired = cur.fetchone()[0]
        if acquired:
            return conn
        conn.close()
        return None
    except Exception as e:
        logger.error(f"Impossible d'acquérir le verrou du scheduler: {e}")
        return None


def _source_has_valid_criteria(source: str, criteria: dict) -> bool:
    """Delegate "is this search runnable" to the source's own parser —
    each source encodes location differently (placeIds vs. city/postal
    code, ...), so there's no single hardcoded key to check here."""
    from parsers import get_parser
    try:
        parser = get_parser(source)
    except ValueError:
        return False
    return parser.has_valid_criteria(criteria)


def _start_background_tasks(app: Flask):
    """Démarre le scheduler APScheduler. Rien de bloquant."""
    from datetime import datetime, timedelta

    from apscheduler.schedulers.background import BackgroundScheduler

    from core.scrape_control import submit_scrape

    lock_conn = _try_acquire_scheduler_lock(app.storage.database_url)
    if lock_conn is None:
        logger.warning(
            "Scheduler non démarré dans ce process (verrou déjà détenu ailleurs) — "
            "évite les scrapes dupliqués si plusieurs workers tournent."
        )
        return
    app._scheduler_lock_conn = lock_conn  # gardé en vie tant que le process tourne

    scheduler = BackgroundScheduler(daemon=True)

    def scheduled_scrape_job():
        try:
            with app.app_context():
                # Issue #17 : pause globale lue À CHAQUE TICK. Les scrapes
                # planifiés sont sautés silencieusement ; les déclenchements
                # manuels (submit_scrape) ne passent pas par ici et restent
                # donc possibles pendant la pause.
                if _pause_scheduler_active(app.storage):
                    logger.info(
                        "Scheduler en pause globale — scrapes planifiés sautés "
                        "(les lancements manuels restent possibles)"
                    )
                    return
                all_users = app.storage.users.get_all_users()
                # Naive datetime representing UTC — matches the naive TIMESTAMP
                # columns populated by Postgres CURRENT_TIMESTAMP (DB session
                # timezone must be UTC for this comparison to be correct).
                now = datetime.now(UTC).replace(tzinfo=None)
                for user in all_users:
                    searches = app.storage.searches.get_user_searches(user["id"])
                    for s in searches:
                        if not s.get("is_active", True):
                            continue
                        criteria = s.get("criteria", {})
                        if not criteria or not isinstance(criteria, dict):
                            continue
                        sources = s.get("sources") or [s.get("source", "seloger")]
                        if not any(_source_has_valid_criteria(src, criteria) for src in sources):
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
                        submit_scrape(app, search_id, user["id"])
        except Exception as e:
            logger.error(f"Erreur scheduled_scrape_job: {e}")

    scheduler.add_job(scheduled_scrape_job, "interval", seconds=30, id="scrape_scheduler", max_instances=1)
    scheduler.start()
    # Issue #17 : parké sur l'app pour que la vue file d'attente de l'admin
    # puisse lire next_run_time du job (aucun autre usage — le verrou
    # consultatif et max_instances=1 restent inchangés).
    app._scheduler = scheduler
    logger.info("Scrape scheduler démarré (toutes les 30s)")


if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Serveur Flask sur le port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
