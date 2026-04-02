"""
SeLoger API Platform — Point d'entrée unique.

Sert à la fois l'API REST (/api/*) et le frontend web (/) depuis
un seul processus Flask, compatible Render (un seul web service).
"""

from __future__ import annotations

import os
import sys
import time
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv

# Load .env file before anything else
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

# ======================================================================
# App factory
# ======================================================================

def create_app() -> Flask:
    config = load_config()

    # Logging
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

    # Shared objects stored on app
    app.config["APP_CONFIG"] = config
    app.storage = Storage(database_url=config.database.database_url)
    app.notifier = Notifier(
        server=config.ntfy.server,
        priority=config.ntfy.priority,
    )

    # Silence werkzeug spam
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    # Inject admin_username into all templates (used by navbar in base.html)
    @app.context_processor
    def inject_admin():
        return {"admin_username": os.environ.get("ADMIN_USERNAME", "admin")}

    # Run cleanup at startup
    def run_startup_cleanup():
        try:
            deleted = app.storage.delete_old_listings(days=4)
            if deleted:
                logger.info(f"Startup cleanup: {deleted} anciennes annonces supprimées")
        except Exception as e:
            logger.error(f"Erreur lors du cleanup au démarrage: {e}")

    # Start background scheduler for periodic cleanup (every 4 days)
    def start_cleanup_scheduler():
        import threading
        import time

        def scheduler_loop():
            while True:
                time.sleep(24 * 60 * 60)  # 1 jour en secondes
                try:
                    deleted = app.storage.delete_old_listings(days=4)
                    if deleted:
                        logger.info(f"Scheduler cleanup: {deleted} anciennes annonces supprimées")
                except Exception as e:
                    logger.error(f"Erreur lors du cleanup planifié: {e}")

        t = threading.Thread(target=scheduler_loop, daemon=True)
        t.start()

    # Only run startup cleanup and scheduler in the first worker
    # Use a file lock to prevent duplicate execution across Gunicorn workers
    lock_file = "/tmp/appart_cleanup_started"
    if not os.path.exists(lock_file):
        try:
            with open(lock_file, "w") as f:
                f.write(str(os.getpid()))
            run_startup_cleanup()
            start_cleanup_scheduler()
            logger.info("Cleanup scheduler démarré (toutes les 4 jours)")
        except Exception as e:
            logger.error(f"Erreur lors du démarrage du scheduler: {e}")

    # Register blueprints
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(web_bp)

    logger.info("Appart Tracker — démarré")
    return app


# ======================================================================
# API Blueprint  (/api/*)
# ======================================================================

api_bp = Blueprint("api", __name__)


def require_token(f):
    """Decorator: require X-API-Token header and set g.user."""
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


# --- Users ---

@api_bp.route("/users", methods=["POST"])
def create_user():
    """Create a new user.  Body: {"username": "..."}"""
    from flask import current_app
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    if not username:
        return jsonify({"error": "username requis"}), 400
    try:
        user = current_app.storage.create_user(username)
        return jsonify(user), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 409


@api_bp.route("/users/login", methods=["POST"])
def login_user():
    """Login by username — returns the existing token."""
    from flask import current_app
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    if not username:
        return jsonify({"error": "username requis"}), 400
    user = current_app.storage.get_user_by_username(username)
    if not user:
        return jsonify({"error": "Utilisateur introuvable"}), 404
    return jsonify(user), 200


@api_bp.route("/sources", methods=["GET"])
def get_sources():
    """List all available parser sources."""
    return jsonify(list_sources()), 200


# --- Searches ---

@api_bp.route("/searches", methods=["POST"])
@require_token
def create_search():
    """Create a search.  Body: {"label": "...", "ntfy_topic": "..."}"""
    from flask import current_app
    data = request.get_json(silent=True) or {}
    label = data.get("label", "").strip()
    ntfy_topic = data.get("ntfy_topic", "").strip()
    source = data.get("source", "seloger").strip()
    if not label or not ntfy_topic:
        return jsonify({"error": "label et ntfy_topic requis"}), 400
    # Validate source exists
    try:
        get_parser(source)  # will raise if unknown
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    search = current_app.storage.create_search(g.user["id"], label, ntfy_topic, source)
    return jsonify(search), 201


@api_bp.route("/searches", methods=["GET"])
@require_token
def list_searches():
    """List searches for the authenticated user."""
    from flask import current_app
    searches = current_app.storage.get_user_searches(g.user["id"])
    return jsonify(searches), 200


@api_bp.route("/searches/<int:search_id>", methods=["DELETE"])
@require_token
def delete_search(search_id: int):
    """Delete a search."""
    from flask import current_app
    search = current_app.storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404
    current_app.storage.delete_search(search_id)
    return jsonify({"ok": True}), 200


# --- Parse ---

@api_bp.route("/parse/<int:search_id>", methods=["POST"])
@require_token
def parse_html(search_id: int):
    """
    Receive HTML, parse SeLoger listings, save & notify.

    Accepts:
        Content-Type: text/html  →  raw HTML body
        Content-Type: multipart/form-data  →  file field 'file'
    """
    from flask import current_app
    storage = current_app.storage
    notifier = current_app.notifier

    # Auth: check search belongs to user
    search = storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404

    # Resolve parser for this search's source
    try:
        parser = get_parser(search["source"])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # Get HTML
    if request.content_type and "multipart/form-data" in request.content_type:
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "Champ 'file' manquant"}), 400
        html = f.read().decode("utf-8", errors="replace")
    else:
        html = request.get_data(as_text=True)

    if not html or len(html) < 100:
        return jsonify({"error": "HTML vide ou trop court"}), 400

    # Parse
    listings = parser.parse(html)
    if not listings:
        return jsonify({
            "total_parsed": 0,
            "new_listings": 0,
            "listings": [],
        }), 200

    # Save & link
    new_listings, already = storage.save_and_link(listings, search_id)
    topic = search["ntfy_topic"]

    # Notify for new ones
    for listing in new_listings:
        notifier.notify_new_listing(topic, listing)
        time.sleep(0.3)

    if new_listings:
        notifier.notify_summary(topic, len(new_listings), len(listings))

    logger.info(
        f"[search:{search_id}] Parsed {len(listings)}, "
        f"{len(new_listings)} new, {len(already)} already known"
    )

    return jsonify({
        "total_parsed": len(listings),
        "new_listings": len(new_listings),
        "listings": [
            {**li.to_dict(), "is_new": True} for li in new_listings
        ] + [
            {**li.to_dict(), "is_new": False} for li in already
        ],
    }), 200


# --- Listings ---

@api_bp.route("/listings/<int:search_id>", methods=["GET"])
@require_token
def get_listings(search_id: int):
    """Get listings for a search (paginated)."""
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


# --- Stats ---

@api_bp.route("/stats", methods=["GET"])
@require_token
def get_stats():
    """Get user statistics."""
    stats = current_app.storage.get_user_stats(g.user["id"])
    return jsonify(stats), 200


# --- Cleanup ---

@api_bp.route("/cleanup", methods=["POST"])
@require_token
def cleanup_listings():
    """Delete listings older than 4 days."""
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
    """Decorator: redirect to login if no session."""
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
        username = request.form.get("username", "").strip()
        if not username:
            flash("Nom d'utilisateur requis", "error")
            return render_template("login.html")

        # Try to find existing user, otherwise create
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
    stats = current_app.storage.get_user_stats(g.user["id"])
    searches = current_app.storage.get_user_searches(g.user["id"])

    # Get recent listings across all searches
    recent = []
    for s in searches[:5]:
        listings = current_app.storage.get_listings_for_search(s["id"], limit=3)
        for li in listings:
            li["search_label"] = s["label"]
            recent.append(li)
    recent.sort(key=lambda x: x.get("found_at", ""), reverse=True)

    return render_template(
        "dashboard.html",
        stats=stats,
        searches=searches,
        recent=recent[:10],
        api_token=session.get("api_token"),
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
        if label and ntfy_topic:
            current_app.storage.create_search(g.user["id"], label, ntfy_topic, source)
            flash(f"Recherche « {label} » créée !", "success")
        else:
            flash("Label et topic ntfy requis", "error")
        return redirect(url_for("web.searches"))

    all_searches = current_app.storage.get_user_searches(g.user["id"])
    base_url = request.url_root.rstrip("/")
    return render_template(
        "searches.html",
        searches=all_searches,
        api_token=session.get("api_token"),
        base_url=base_url,
        sources=sources,
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


@web_bp.route("/admin")
@require_login
def admin():
    from flask import current_app
    admin_username = os.environ.get("ADMIN_USERNAME", "admin")
    if g.user["username"] != admin_username:
        flash("Accès refusé", "error")
        return redirect(url_for("web.dashboard"))
    stats = current_app.storage.get_admin_stats()
    users = current_app.storage.get_all_users()
    return render_template("admin.html", stats=stats, users=users)


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


# ======================================================================
# Health check (root-level for Render)
# ======================================================================

# The web_bp already handles "/" via index(), which redirects.
# Render pings "/" expecting 200; the redirect (302) works, but we also
# expose a dedicated /health that returns plain 200.

@web_bp.route("/health")
def health():
    return "OK", 200


@web_bp.route("/cleanup", methods=["POST"])
@require_login
def cleanup_web():
    """Web route to trigger cleanup from dashboard."""
    days = int(request.form.get("days", 4))
    deleted = current_app.storage.delete_old_listings(days=days)
    flash(f"{deleted} ancienne(s) annonce(s) supprimée(s)", "success")
    return redirect(url_for("web.dashboard"))


# ======================================================================
# Entry point
# ======================================================================

if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Serveur Flask sur le port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
