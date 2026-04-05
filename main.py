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

import json
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from flask import (
    Flask, Blueprint, request, jsonify, render_template,
    redirect, url_for, session, flash, g, current_app,
)
from loguru import logger

from config import load_config
from notifier import Notifier
from parsers import get_parser, list_sources
from storage import Storage

_scrape_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scrape")
_scrape_futures: dict[int, object] = {}


def create_app() -> Flask:
    import time
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

    @app.before_request
    def before_request():
        """Ouvre une connexion DB partagée pour toute la durée de la requête HTTP."""
        g._db_conn = app.storage._get_conn()

    @app.teardown_request
    def teardown_request(exception):
        """Ferme la connexion DB partagée à la fin de la requête."""
        if hasattr(g, '_db_conn') and g._db_conn is not None:
            try:
                if not g._db_conn.closed:
                    g._db_conn.close()
            except Exception:
                pass
            g._db_conn = None

    from datetime import datetime, timezone
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
        """Convert UTC datetime to Europe/Paris time for display (handles DST)."""
        if dt is None:
            return None
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

    def start_background_tasks():
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

                            fut = _scrape_executor.submit(_execute_scrape, app, search_id, user["id"])
                            _scrape_futures[search_id] = fut
            except Exception as e:
                logger.error(f"Erreur scheduled_scrape_job: {e}")

        scheduler.add_job(scheduled_scrape_job, "interval", seconds=30, id="scrape_scheduler", max_instances=1)
        scheduler.start()
        logger.info("Scrape scheduler démarré (toutes les 30s)")

    start_background_tasks()

    start_background_tasks()

    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(web_bp)

    startup_elapsed = time.monotonic() - startup_start
    logger.info(f"Appart Tracker — démarré (mode scraper) en {startup_elapsed:.2f}s")
    return app


def _execute_scrape(app, search_id: int, user_id: int) -> int:
    """Execute un scrape pour une search donnée. Retourne le nombre de nouvelles annonces."""
    import datetime
    from log_manager import SearchLogManager

    started_at = datetime.datetime.utcnow()
    log_mgr = SearchLogManager(search_id)
    log_mgr.start()

    try:
        with app.app_context():
            storage = app.storage
            notifier = app.notifier

            search = storage.get_search(search_id)
            if not search or search["user_id"] != user_id:
                return 0

            criteria = search.get("criteria", {})
            if not criteria or not criteria.get("placeIds"):
                logger.warning(f"[search:{search_id}] Critères vides, skip")
                storage.create_scrape_log(
                    search_id, "error",
                    error_message="Critères vides ou placeIds manquant",
                    started_at=started_at,
                )
                return 0

            use_bff = storage.get_setting("use_bff_api", "true") == "true"

            try:
                parser = get_parser(search["source"])
            except ValueError as e:
                logger.error(f"[search:{search_id}] Parser inconnu: {e}")
                storage.create_scrape_log(
                    search_id, "error",
                    error_message=f"Parser inconnu: {e}",
                    started_at=started_at,
                )
                return 0

            try:
                listings = parser.scrape(criteria, use_bff=use_bff)
            except Exception as e:
                err_msg = str(e)
                logger.error(f"[search:{search_id}] Erreur scraping: {err_msg}")
                storage.update_last_scraped(search_id)
                storage.create_scrape_log(
                    search_id, "error",
                    error_message=err_msg,
                    started_at=started_at,
                )
                return 0

            if not listings:
                logger.info(f"[search:{search_id}] Aucune annonce trouvée")
                storage.update_last_scraped(search_id)
                storage.create_scrape_log(
                    search_id, "error",
                    error_message="Aucune annonce trouvée",
                    listings_found=0, new_listings=0,
                    started_at=started_at,
                )
                return 0

            new_listings, already = storage.save_and_link(listings, search_id)
            storage.update_last_scraped(search_id)
            topic = search["ntfy_topic"]

            for listing in new_listings:
                notifier.notify_new_listing(topic, listing)
                time.sleep(0.3)

            if new_listings:
                notifier.notify_summary(topic, len(new_listings), len(listings))

            log_id = storage.create_scrape_log(
                search_id, "success",
                listings_found=len(listings),
                new_listings=len(new_listings),
                details={"already_known": len(already)},
                started_at=started_at,
            )

            raw_logs = log_mgr.stop()
            storage.update_scrape_log_raw(log_id, raw_logs)
            log_mgr.cleanup_old_logs()

            logger.info(
                f"[search:{search_id}] Scraped {len(listings)}, "
                f"{len(new_listings)} new, {len(already)} already known"
            )
            return len(new_listings)

    except Exception:
        log_mgr.stop()
        raise


# ======================================================================
# API Blueprint  (/api/*)
# ======================================================================

api_bp = Blueprint("api", __name__)


def require_token(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        from flask import current_app
        token = request.headers.get("X-API-Token", "")
        if not token:
            return jsonify({"error": "Header X-API-Token manquant"}), 401
        user = current_app.storage.get_user_by_token(token)
        if not user:
            return jsonify({"error": "Token invalide"}), 401
        g.user = user
        return f(*args, **kwargs)
    return wrapper


@api_bp.route("/users", methods=["POST"])
def create_user():
    from flask import current_app
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip().lower()
    if not username:
        return jsonify({"error": "username requis"}), 400
    try:
        user = current_app.storage.create_user(username)
        return jsonify(user), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 409


@api_bp.route("/users/login", methods=["POST"])
def login_user():
    from flask import current_app
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip().lower()
    if not username:
        return jsonify({"error": "username requis"}), 400
    user = current_app.storage.get_user_by_username(username)
    if not user:
        return jsonify({"error": "Utilisateur introuvable"}), 404
    return jsonify(user), 200


@api_bp.route("/sources", methods=["GET"])
def get_sources():
    return jsonify(list_sources()), 200


@api_bp.route("/searches", methods=["POST"])
@require_token
def create_search():
    from flask import current_app
    data = request.get_json(silent=True) or {}
    label = data.get("label", "").strip()
    ntfy_topic = data.get("ntfy_topic", "").strip()
    source = data.get("source", "seloger").strip()
    criteria = data.get("criteria", {})
    scrape_interval = data.get("scrape_interval", 5)
    if not label or not ntfy_topic:
        return jsonify({"error": "label et ntfy_topic requis"}), 400
    try:
        get_parser(source)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    search = current_app.storage.create_search(
        g.user["id"], label, ntfy_topic, source, criteria, scrape_interval
    )
    return jsonify(search), 201


@api_bp.route("/searches", methods=["GET"])
@require_token
def list_searches():
    from flask import current_app
    searches = current_app.storage.get_user_searches(g.user["id"])
    return jsonify(searches), 200


@api_bp.route("/searches/<int:search_id>", methods=["DELETE"])
@require_token
def delete_search(search_id: int):
    from flask import current_app
    search = current_app.storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404
    current_app.storage.delete_search(search_id)
    return jsonify({"ok": True}), 200


@api_bp.route("/searches/<int:search_id>/criteria", methods=["PUT"])
@require_token
def update_criteria(search_id: int):
    from flask import current_app
    search = current_app.storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404
    data = request.get_json(silent=True) or {}
    criteria = data.get("criteria")
    if criteria is not None:
        current_app.storage.update_search_criteria(search_id, criteria)
    if "scrape_interval" in data:
        current_app.storage.update_scrape_interval(search_id, data["scrape_interval"])
    return jsonify({"ok": True}), 200


@api_bp.route("/searches/<int:search_id>/toggle-active", methods=["POST"])
@require_token
def toggle_search_active(search_id: int):
    from flask import current_app
    search = current_app.storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404
    new_value = current_app.storage.toggle_search_active(search_id)
    if new_value is None:
        return jsonify({"error": "Recherche introuvable"}), 404
    return jsonify({"ok": True, "is_active": new_value}), 200


@api_bp.route("/scrape/<int:search_id>", methods=["POST"])
@require_token
def scrape_search(search_id: int):
    from flask import current_app
    storage = current_app.storage
    search = storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404

    if search_id in _scrape_futures:
        fut = _scrape_futures[search_id]
        if not fut.done():
            return jsonify({"error": "Scraping déjà en cours pour cette recherche"}), 409
        else:
            del _scrape_futures[search_id]

    fut = _scrape_executor.submit(_execute_scrape, current_app._get_current_object(), search_id, g.user["id"])
    _scrape_futures[search_id] = fut

    return jsonify({"message": "Scraping démarré en arrière-plan"}), 202


@api_bp.route("/listings/<int:search_id>", methods=["GET"])
@require_token
def get_listings(search_id: int):
    from flask import current_app
    storage = current_app.storage
    search = storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404

    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))

    listings = storage.get_listings_for_search(search_id, limit=limit, offset=offset)
    total = storage.count_listings_for_search(search_id)

    return jsonify({
        "search": search,
        "total": total,
        "limit": limit,
        "offset": offset,
        "listings": listings,
    }), 200


@api_bp.route("/stats", methods=["GET"])
@require_token
def get_stats():
    stats = current_app.storage.get_user_stats(g.user["id"])
    return jsonify(stats), 200


@api_bp.route("/cleanup", methods=["POST"])
@require_token
def cleanup_listings():
    days = int(request.args.get("days", 4))
    deleted = current_app.storage.delete_old_listings(days=days)
    return jsonify({"deleted": deleted, "days": days}), 200


# ======================================================================
# Web Frontend Blueprint  (/)
# ======================================================================

web_bp = Blueprint(
    "web", __name__,
    template_folder="templates",
    static_folder="static",
)


def require_login(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("web.login"))
        from flask import current_app
        user = current_app.storage.get_user_by_token(session.get("api_token", ""))
        if not user:
            session.clear()
            return redirect(url_for("web.login"))
        g.user = user
        return f(*args, **kwargs)
    return wrapper


@web_bp.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("web.dashboard"))
    return redirect(url_for("web.login"))


@web_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        from flask import current_app
        username = request.form.get("username", "").strip().lower()
        if not username:
            flash("Nom d'utilisateur requis", "error")
            return render_template("login.html")

        user = current_app.storage.get_user_by_username(username)
        if not user:
            try:
                user = current_app.storage.create_user(username)
                flash(f"Compte créé ! Votre token API : {user['api_token']}", "success")
            except ValueError:
                flash("Erreur lors de la création du compte", "error")
                return render_template("login.html")

        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["api_token"] = user["api_token"]
        return redirect(url_for("web.dashboard"))

    return render_template("login.html")


@web_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("web.login"))


@web_bp.route("/dashboard")
@require_login
def dashboard():
    from flask import current_app
    from datetime import datetime
    data = current_app.storage.get_dashboard_data(g.user["id"])
    return render_template(
        "dashboard.html",
        stats=data["stats"],
        searches=data["searches"],
        recent=data["recent"][:10],
        api_token=session.get("api_token"),
        now=datetime.utcnow,
    )


@web_bp.route("/searches", methods=["GET", "POST"])
@require_login
def searches():
    from flask import current_app
    sources = list_sources()
    if request.method == "POST":
        label = request.form.get("label", "").strip()
        ntfy_topic = request.form.get("ntfy_topic", "").strip()
        source = request.form.get("source", "seloger").strip()
        search_url = request.form.get("search_url", "").strip()
        scrape_interval = int(request.form.get("scrape_interval", 5))

        criteria = {}

        if search_url:
            from scraper.seloger import parse_search_url
            criteria = parse_search_url(search_url)
            if not criteria.get("placeIds"):
                flash("L'URL ne contient pas de lieu valide (locations=...)", "error")
                return redirect(url_for("web.searches"))
        else:
            place_ids = request.form.get("place_ids", "").strip()
            price_min = request.form.get("price_min", "").strip()
            price_max = request.form.get("price_max", "").strip()
            space_min = request.form.get("space_min", "").strip()
            distribution = request.form.get("distribution", "Rent")
            estate_type = request.form.get("estate_type", "Apartment")

            if place_ids:
                criteria["placeIds"] = [p.strip() for p in place_ids.split(",")]
                criteria["location"] = {"placeIds": criteria["placeIds"]}
            if price_min:
                criteria["priceMin"] = int(price_min)
            if price_max:
                criteria["priceMax"] = int(price_max)
            if space_min:
                criteria["spaceMin"] = int(space_min)
            criteria["distributionTypes"] = [distribution]
            criteria["estateTypes"] = [estate_type]

        if label and ntfy_topic and criteria.get("placeIds"):
            current_app.storage.create_search(
                g.user["id"], label, ntfy_topic, source, criteria, scrape_interval
            )
            flash(f"Recherche « {label} » créée !", "success")
        else:
            flash("Label, topic ntfy et au moins un lieu requis", "error")
        return redirect(url_for("web.searches"))

    all_searches = current_app.storage.get_user_searches(g.user["id"])
    from datetime import datetime
    base_url = request.url_root.rstrip("/")
    return render_template(
        "searches.html",
        searches=all_searches,
        api_token=session.get("api_token"),
        base_url=base_url,
        sources=sources,
        now=datetime.utcnow,
    )


@web_bp.route("/searches/<int:search_id>/delete", methods=["POST"])
@require_login
def delete_search_web(search_id: int):
    from flask import current_app
    search = current_app.storage.get_search(search_id)
    if search and search["user_id"] == g.user["id"]:
        current_app.storage.delete_search(search_id)
        flash("Recherche supprimée", "success")
    return redirect(url_for("web.searches"))


@web_bp.route("/searches/<int:search_id>/scrape", methods=["POST"])
@require_login
def scrape_search_web(search_id: int):
    from flask import current_app
    storage = current_app.storage
    search = storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))

    if search_id in _scrape_futures:
        fut = _scrape_futures[search_id]
        if not fut.done():
            flash("Scraping déjà en cours pour cette recherche", "warning")
            return redirect(url_for("web.searches"))
        else:
            del _scrape_futures[search_id]

    app = current_app._get_current_object()
    fut = _scrape_executor.submit(_execute_scrape, app, search_id, g.user["id"])
    _scrape_futures[search_id] = fut
    flash("Scraping démarré en arrière-plan !", "success")
    return redirect(url_for("web.searches"))


@web_bp.route("/searches/<int:search_id>/interval", methods=["POST"])
@require_login
def update_interval_web(search_id: int):
    from flask import current_app
    storage = current_app.storage
    search = storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))

    interval = int(request.form.get("scrape_interval", 5))
    if interval < 1:
        interval = 1
    storage.update_scrape_interval(search_id, interval)
    flash(f"Intervalle mis à jour : {interval} minutes", "success")
    return redirect(url_for("web.searches"))


@web_bp.route("/searches/<int:search_id>/toggle-active", methods=["POST"])
@require_login
def toggle_search_active_web(search_id: int):
    from flask import current_app
    storage = current_app.storage
    search = storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))
    new_value = storage.toggle_search_active(search_id)
    if new_value is None:
        flash("Recherche introuvable", "error")
    else:
        status = "activée" if new_value else "désactivée"
        flash(f"Recherche {status}", "success")
    return redirect(url_for("web.searches"))


@web_bp.route("/searches/<int:search_id>/edit", methods=["GET", "POST"])
@require_login
def edit_search(search_id: int):
    from flask import current_app
    storage = current_app.storage
    search = storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))

    if request.method == "POST":
        label = request.form.get("label", "").strip()
        ntfy_topic = request.form.get("ntfy_topic", "").strip()
        search_url = request.form.get("search_url", "").strip()
        scrape_interval = int(request.form.get("scrape_interval", 5))

        criteria = {}
        if search_url:
            from scraper.seloger import parse_search_url
            criteria = parse_search_url(search_url)
            if not criteria.get("placeIds"):
                flash("L'URL ne contient pas de lieu valide", "error")
                return render_template("search_edit.html", search=search, now=datetime.utcnow)
        else:
            place_ids = request.form.get("place_ids", "").strip()
            price_min = request.form.get("price_min", "").strip()
            price_max = request.form.get("price_max", "").strip()
            space_min = request.form.get("space_min", "").strip()
            distribution = request.form.get("distribution", "Rent")
            estate_type = request.form.get("estate_type", "Apartment")

            if place_ids:
                criteria["placeIds"] = [p.strip() for p in place_ids.split(",")]
                criteria["location"] = {"placeIds": criteria["placeIds"]}
            if price_min:
                criteria["priceMin"] = int(price_min)
            if price_max:
                criteria["priceMax"] = int(price_max)
            if space_min:
                criteria["spaceMin"] = int(space_min)
            criteria["distributionTypes"] = [distribution]
            criteria["estateTypes"] = [estate_type]

        if label and ntfy_topic and criteria.get("placeIds"):
            storage.update_search(
                search_id, g.user["id"],
                label=label, ntfy_topic=ntfy_topic,
                criteria=criteria, scrape_interval=scrape_interval,
            )
            flash("Recherche mise à jour !", "success")
            return redirect(url_for("web.searches"))
        else:
            flash("Label, topic ntfy et au moins un lieu requis", "error")

    stats = storage.get_scrape_stats(search_id)
    from datetime import datetime
    return render_template("search_edit.html", search=search, stats=stats, now=datetime.utcnow)


@web_bp.route("/searches/<int:search_id>/logs", methods=["GET"])
@require_login
def search_logs(search_id: int):
    from flask import current_app
    storage = current_app.storage
    search = storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))

    page = int(request.args.get("page", 1))
    per_page = 30
    offset = (page - 1) * per_page
    status_filter = request.args.get("status", "")

    logs = storage.get_scrape_logs(search_id, limit=per_page, offset=offset, status_filter=status_filter)
    total = storage.count_scrape_logs(search_id, status_filter=status_filter)
    total_pages = max(1, (total + per_page - 1) // per_page)
    stats = storage.get_scrape_stats(search_id)
    from datetime import datetime
    return render_template(
        "search_logs.html",
        search=search,
        logs=logs,
        total=total,
        page=page,
        total_pages=total_pages,
        status_filter=status_filter,
        stats=stats,
        now=datetime.utcnow,
    )


@web_bp.route("/searches/<int:search_id>/logs/live", methods=["GET"])
@require_login
def search_logs_live(search_id: int):
    """Polling endpoint: returns new log lines since offset."""
    from flask import current_app
    from log_manager import SearchLogManager
    storage = current_app.storage
    search = storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Not found"}), 404

    offset = int(request.args.get("offset", 0))
    log_mgr = SearchLogManager(search_id)
    new_text, new_offset = log_mgr.read_tail(offset)

    return jsonify({
        "text": new_text,
        "offset": new_offset,
        "has_content": bool(new_text),
    })


@web_bp.route("/searches/<int:search_id>/logs/<int:log_id>/raw", methods=["GET"])
@require_login
def search_log_raw(search_id: int, log_id: int):
    """View raw logs for a specific scrape entry."""
    from flask import current_app
    storage = current_app.storage
    search = storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))

    log_entry = storage.get_scrape_log_raw(log_id, g.user["id"])
    if not log_entry:
        flash("Log introuvable", "error")
        return redirect(url_for("web.search_logs", search_id=search_id))

    return render_template(
        "search_log_raw.html",
        search=search,
        log_entry=log_entry,
    )


@web_bp.route("/searches/<int:search_id>/logs/<int:log_id>/download", methods=["GET"])
@require_login
def search_log_download(search_id: int, log_id: int):
    """Download raw log file for a scrape entry."""
    from flask import current_app, send_file
    from log_manager import SearchLogManager
    storage = current_app.storage
    search = storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Not found"}), 404

    log_entry = storage.get_scrape_log_raw(log_id, g.user["id"])
    if not log_entry:
        return jsonify({"error": "Log not found"}), 404

    if log_entry.get("raw_logs"):
        from io import BytesIO
        buf = BytesIO(log_entry["raw_logs"].encode("utf-8"))
        buf.seek(0)
        return send_file(
            buf,
            mimetype="text/plain",
            as_attachment=True,
            download_name=f"search_{search_id}_log_{log_id}.log",
        )

    log_mgr = SearchLogManager(search_id)
    if log_mgr.file_exists():
        return send_file(
            log_mgr.log_file,
            mimetype="text/plain",
            as_attachment=True,
            download_name=f"search_{search_id}_log_{log_id}.log",
        )

    return "No logs available", 404


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("web.login"))
        user = current_app.storage.get_user_by_token(session.get("api_token", ""))
        if not user:
            session.clear()
            return redirect(url_for("web.login"))
        admin_username = os.environ.get("ADMIN_USERNAME", "admin")
        if user["username"] != admin_username:
            flash("Accès refusé", "error")
            return redirect(url_for("web.dashboard"))
        g.user = user
        return f(*args, **kwargs)
    return wrapper


@web_bp.route("/admin")
@require_admin
def admin():
    tab = request.args.get("tab", "dashboard")
    storage = current_app.storage
    stats = storage.get_enhanced_admin_stats()
    stats["bff_enabled"] = storage.get_setting("use_bff_api", "true") == "true"
    return render_template("admin.html", stats=stats, active_tab=tab)


@web_bp.route("/admin/users")
@require_admin
def admin_users():
    search_term = request.args.get("search", "")
    storage = current_app.storage
    users = storage.get_all_users()
    if search_term:
        users = [u for u in users if search_term.lower() in u["username"].lower()]
    return render_template("admin.html", active_tab="users", users=users, search_term=search_term,
                           user_count=len(users))


@web_bp.route("/admin/users/<int:user_id>")
@require_admin
def admin_user_detail(user_id):
    storage = current_app.storage
    user = storage.get_user_detail(user_id)
    if not user:
        flash("Utilisateur introuvable", "error")
        return redirect(url_for("web.admin"))
    return render_template("admin.html", active_tab="users", user_detail=user)


@web_bp.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@require_admin
def admin_delete_user(user_id):
    storage = current_app.storage
    user = storage.get_user_detail(user_id)
    if user:
        storage.delete_user(user_id)
        storage.log_admin_action("user_deleted", f"User '{user['username']}' (ID:{user_id}) deleted", g.user["username"])
        flash(f"Utilisateur '{user['username']}' supprimé", "success")
    return redirect(url_for("web.admin_users"))


@web_bp.route("/admin/users/<int:user_id>/reset-token", methods=["POST"])
@require_admin
def admin_reset_token(user_id):
    storage = current_app.storage
    user = storage.get_user_detail(user_id)
    if user:
        new_token = storage.reset_user_token(user_id)
        storage.log_admin_action("user_token_reset", f"Token reset for '{user['username']}' (ID:{user_id})", g.user["username"])
        flash(f"Nouveau token pour '{user['username']}': {new_token}", "success")
    return redirect(url_for("web.admin_user_detail", user_id=user_id))


@web_bp.route("/admin/users/create", methods=["POST"])
@require_admin
def admin_create_user():
    username = request.form.get("username", "").strip().lower()
    if not username:
        flash("Nom d'utilisateur requis", "error")
        return redirect(url_for("web.admin_users"))
    try:
        user = current_app.storage.create_user(username)
        current_app.storage.log_admin_action("user_created", f"User '{username}' (ID:{user['id']}) created", g.user["username"])
        flash(f"Utilisateur '{username}' créé. Token: {user['api_token']}", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("web.admin_users"))


@web_bp.route("/admin/searches")
@require_admin
def admin_searches():
    user_filter = request.args.get("user", "")
    source_filter = request.args.get("source", "")
    searches = current_app.storage.get_all_searches(user_filter=user_filter, source_filter=source_filter)
    return render_template("admin.html", active_tab="searches", searches=searches,
                           user_filter=user_filter, source_filter=source_filter, search_count=len(searches))


@web_bp.route("/admin/searches/<int:search_id>")
@require_admin
def admin_search_detail(search_id):
    storage = current_app.storage
    search = storage.get_search_detail(search_id)
    if not search:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.admin_searches"))
    return render_template("admin.html", active_tab="searches", search_detail=search)


@web_bp.route("/admin/searches/<int:search_id>/delete", methods=["POST"])
@require_admin
def admin_delete_search(search_id):
    storage = current_app.storage
    search = storage.get_search(search_id)
    if search:
        storage.delete_search_admin(search_id)
        storage.log_admin_action("search_deleted", f"Search '{search['label']}' (ID:{search_id}) deleted by {g.user['username']}", g.user["username"])
        flash(f"Recherche '{search['label']}' supprimée", "success")
    return redirect(url_for("web.admin_searches"))


@web_bp.route("/admin/searches/<int:search_id>/scrape", methods=["POST"])
@require_admin
def admin_scrape_search(search_id):
    storage = current_app.storage
    search = storage.get_search(search_id)
    if not search:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.admin_searches"))

    if search_id in _scrape_futures:
        fut = _scrape_futures[search_id]
        if not fut.done():
            flash("Scraping déjà en cours", "warning")
            return redirect(url_for("web.admin_search_detail", search_id=search_id))
        else:
            del _scrape_futures[search_id]

    fut = _scrape_executor.submit(_execute_scrape, current_app._get_current_object(), search_id, search["user_id"])
    _scrape_futures[search_id] = fut
    flash("Scraping démarré en arrière-plan !", "success")
    return redirect(url_for("web.admin_search_detail", search_id=search_id))


@web_bp.route("/admin/listings")
@require_admin
def admin_listings():
    page = int(request.args.get("page", 1))
    per_page = 30
    offset = (page - 1) * per_page
    search_term = request.args.get("search", "")
    source_filter = request.args.get("source", "")
    storage = current_app.storage
    listings = storage.get_all_listings(limit=per_page, offset=offset, search_term=search_term, source_filter=source_filter)
    total = storage.count_all_listings(search_term=search_term, source_filter=source_filter)
    total_pages = max(1, (total + per_page - 1) // per_page)
    orphan_count = storage.get_orphan_listings_count()
    return render_template("admin.html", active_tab="listings", listings=listings,
                           total=total, page=page, total_pages=total_pages,
                           search_term=search_term, source_filter=source_filter,
                           orphan_count=orphan_count)


@web_bp.route("/admin/listings/<listing_id>")
@require_admin
def admin_listing_detail(listing_id):
    storage = current_app.storage
    listing = storage.get_listing_detail(listing_id)
    if not listing:
        flash("Annonce introuvable", "error")
        return redirect(url_for("web.admin_listings"))
    return render_template("admin.html", active_tab="listings", listing_detail=listing)


@web_bp.route("/admin/listings/<listing_id>/delete", methods=["POST"])
@require_admin
def admin_delete_listing(listing_id):
    storage = current_app.storage
    storage.delete_listing(listing_id)
    storage.log_admin_action("listing_deleted", f"Listing '{listing_id}' deleted", g.user["username"])
    flash("Annonce supprimée", "success")
    return redirect(url_for("web.admin_listings"))


@web_bp.route("/admin/listings/cleanup-orphan", methods=["POST"])
@require_admin
def admin_cleanup_orphan():
    storage = current_app.storage
    deleted = storage.delete_orphan_listings()
    storage.log_admin_action("orphan_cleanup", f"{deleted} orphan listings deleted", g.user["username"])
    flash(f"{deleted} annonce(s) orpheline(s) supprimée(s)", "success")
    return redirect(url_for("web.admin_listings"))


@web_bp.route("/admin/database")
@require_admin
def admin_database():
    storage = current_app.storage
    db_stats = storage.get_db_stats()
    connections = storage.get_active_connections()
    return render_template("admin.html", active_tab="database", db_stats=db_stats, connections=connections)


@web_bp.route("/admin/database/table/<table_name>")
@require_admin
def admin_table_detail(table_name):
    storage = current_app.storage
    details = storage.get_table_details(table_name)
    db_stats = storage.get_db_stats()
    return render_template("admin.html", active_tab="database", db_stats=db_stats,
                           table_name=table_name, table_details=details)


@web_bp.route("/admin/database/query", methods=["POST"])
@require_admin
def admin_execute_query():
    sql = request.form.get("sql", "").strip()
    if not sql:
        flash("Requête vide", "error")
        return redirect(url_for("web.admin_database"))
    storage = current_app.storage
    rows, row_count, error = storage.execute_query(sql)
    storage.log_admin_action("db_query", sql[:200], g.user["username"])
    if error:
        flash(f"Erreur: {error}", "error")
        return redirect(url_for("web.admin_database"))
    flash(f"Requête exécutée — {row_count} ligne(s) affectée(s)", "success")
    return render_template("admin.html", active_tab="database", db_stats=storage.get_db_stats(),
                           query_result=rows, query_row_count=row_count, query_sql=sql)


@web_bp.route("/admin/database/truncate", methods=["POST"])
@require_admin
def admin_truncate_table():
    table_name = request.form.get("table_name", "").strip()
    if not table_name:
        flash("Nom de table requis", "error")
        return redirect(url_for("web.admin_database"))
    storage = current_app.storage
    if storage.truncate_table(table_name):
        storage.log_admin_action("table_truncated", f"Table '{table_name}' truncated", g.user["username"])
        flash(f"Table '{table_name}' vidée", "success")
    else:
        flash(f"Impossible de vider la table '{table_name}'", "error")
    return redirect(url_for("web.admin_database"))


@web_bp.route("/admin/logs")
@require_admin
def admin_logs():
    page = int(request.args.get("page", 1))
    per_page = 50
    offset = (page - 1) * per_page
    action_filter = request.args.get("action", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    storage = current_app.storage
    logs = storage.get_admin_logs(limit=per_page, offset=offset, action_filter=action_filter,
                                  date_from=date_from, date_to=date_to)
    total = storage.count_admin_logs(action_filter=action_filter, date_from=date_from, date_to=date_to)
    total_pages = max(1, (total + per_page - 1) // per_page)
    return render_template("admin.html", active_tab="logs", logs=logs, total_logs=total,
                           page=page, total_pages=total_pages, action_filter=action_filter,
                           date_from=date_from, date_to=date_to)


@web_bp.route("/admin/logs/purge", methods=["POST"])
@require_admin
def admin_purge_logs():
    days = int(request.form.get("days", 30))
    storage = current_app.storage
    deleted = storage.purge_old_logs(days=days)
    flash(f"{deleted} ancien(s) log(s) supprimé(s)", "success")
    return redirect(url_for("web.admin_logs"))


@web_bp.route("/admin/cleanup", methods=["POST"])
@require_admin
def admin_cleanup():
    days = int(request.form.get("days", 4))
    storage = current_app.storage
    deleted = storage.delete_old_listings(days=days)
    storage.log_admin_action("cleanup_executed", f"{deleted} listings older than {days} days deleted", g.user["username"])
    flash(f"{deleted} ancienne(s) annonce(s) supprimée(s)", "success")
    return redirect(url_for("web.admin"))


@web_bp.route("/admin/settings/toggle-bff", methods=["POST"])
@require_admin
def admin_toggle_bff():
    storage = current_app.storage
    current = storage.get_setting("use_bff_api", "true")
    new_val = "false" if current == "true" else "true"
    storage.set_setting("use_bff_api", new_val)
    storage.log_admin_action("bff_toggled", f"BFF API {'disabled' if new_val == 'false' else 'enabled'}", g.user["username"])
    flash(f"API BFF {'désactivée' if new_val == 'false' else 'activée'}", "success")
    return redirect(url_for("web.admin"))


@web_bp.route("/listings/<int:search_id>")
@require_login
def listings(search_id: int):
    from flask import current_app
    storage = current_app.storage
    search = storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))

    page = int(request.args.get("page", 1))
    per_page = 20
    offset = (page - 1) * per_page

    all_listings = storage.get_listings_for_search(search_id, limit=per_page, offset=offset)
    total = storage.count_listings_for_search(search_id)
    total_pages = max(1, (total + per_page - 1) // per_page)

    return render_template(
        "listings.html",
        search=search,
        listings=all_listings,
        total=total,
        page=page,
        total_pages=total_pages,
    )


@web_bp.route("/health")
def health():
    return "OK", 200


@web_bp.route("/cleanup", methods=["POST"])
@require_login
def cleanup():
    days = int(request.form.get("days", 4))
    deleted = current_app.storage.delete_old_listings(days=days)
    flash(f"{deleted} ancienne(s) annonce(s) supprimée(s)", "success")
    return redirect(url_for("web.dashboard"))


if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Serveur Flask sur le port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
