"""API Blueprint — all /api/* endpoints."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from flask import Blueprint, request, jsonify, current_app, g, send_file

from parsers import list_sources
from routes.auth import require_token

api_bp = Blueprint("api", __name__)


@api_bp.route("/users", methods=["POST"])
def create_user():
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
    from parsers import get_parser
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
    searches = current_app.storage.get_user_searches(g.user["id"])
    return jsonify(searches), 200


@api_bp.route("/searches/<int:search_id>/urls", methods=["GET"])
@require_token
def get_search_urls(search_id: int):
    """Get reconstructed search URLs for the search's source."""
    search = current_app.storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404

    source = search.get("source", "seloger")
    criteria = search.get("criteria", {})

    try:
        from parsers import get_parser
        parser = get_parser(source)
        
        if hasattr(parser, 'build_search_url') and callable(parser.build_search_url):
            url = parser.build_search_url(criteria)
            if url:
                return jsonify({
                    "source": source,
                    "url": url,
                    "source_name": parser.SOURCE_NAME
                }), 200
        
        return jsonify({
            "source": source,
            "url": None,
            "source_name": parser.SOURCE_NAME,
            "error": "URL reconstruction non disponible pour cette source"
        }), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@api_bp.route("/searches/<int:search_id>", methods=["DELETE"])
@require_token
def delete_search(search_id: int):
    search = current_app.storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404
    current_app.storage.delete_search(search_id)
    return jsonify({"ok": True}), 200


@api_bp.route("/searches/<int:search_id>/criteria", methods=["PUT"])
@require_token
def update_criteria(search_id: int):
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
    search = current_app.storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404
    new_value = current_app.storage.toggle_search_active(search_id)
    if new_value is None:
        return jsonify({"error": "Recherche introuvable"}), 404
    return jsonify({"ok": True, "is_active": new_value}), 200


@api_bp.route("/searches/<int:search_id>/blacklist-mode", methods=["PUT"])
@require_token
def update_blacklist_mode(search_id: int):
    search = current_app.storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404
    
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "exclude")
    
    if mode not in ("exclude", "no_notify"):
        return jsonify({"error": "Mode invalide. Options: exclude, no_notify"}), 400
    
    current_app.storage.update_blacklist_mode(search_id, mode)
    return jsonify({"ok": True, "blacklist_mode": mode}), 200


@api_bp.route("/searches/<int:search_id>/blacklist-agencies", methods=["PUT"])
@require_token
def update_blacklist_agencies(search_id: int):
    search = current_app.storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404
    
    data = request.get_json(silent=True) or {}
    agencies = data.get("agencies", [])
    
    current_app.storage.update_blacklisted_agencies(search_id, agencies)
    return jsonify({"ok": True, "blacklisted_agencies": agencies}), 200


@api_bp.route("/scrape/<int:search_id>", methods=["POST"])
@require_token
def scrape_search(search_id: int):
    search = current_app.storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404

    if search_id in current_app._scrape_futures:
        fut = current_app._scrape_futures[search_id]
        if not fut.done():
            return jsonify({"error": "Scraping déjà en cours pour cette recherche"}), 409
        else:
            del current_app._scrape_futures[search_id]

    from services.scrape_service import ScrapeService
    fut = current_app._scrape_executor.submit(
        ScrapeService(current_app._get_current_object()).execute,
        search_id, g.user["id"],
    )
    current_app._scrape_futures[search_id] = fut
    return jsonify({"message": "Scraping démarré en arrière-plan"}), 202


def _parse_listing_filters(args: dict) -> dict:
    filters = {}
    if args.get("q", "").strip():
        filters["q"] = args["q"].strip()
    for key in ("price_min", "price_max", "surface_min", "surface_max", "rooms_min", "rooms_max"):
        if args.get(key, "").strip():
            try:
                filters[key] = float(args[key])
            except ValueError:
                pass
    for key in ("city", "district", "zip_code", "property_type", "agency", "epc", "ges"):
        if args.get(key, "").strip():
            filters[key] = args[key].strip()
    for key in ("is_private", "is_new"):
        val = args.get(key, "").strip()
        if val in ("true", "false"):
            filters[key] = val == "true"
    if args.get("date_min", "").strip():
        filters["date_min"] = args["date_min"].strip()
    return filters


@api_bp.route("/listings/<int:search_id>", methods=["GET"])
@require_token
def get_listings(search_id: int):
    storage = current_app.storage
    search = storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404

    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))

    blacklisted = search.get("blacklisted_agencies") or []
    blacklist_mode = search.get("blacklist_mode", "exclude")
    agencies_to_filter = blacklisted if blacklist_mode == "exclude" and blacklisted else []

    filters = _parse_listing_filters(request.args)
    sort = request.args.get("sort", "found_at_desc")

    listings = storage.get_listings_for_search(
        search_id, limit=limit, offset=offset,
        blacklisted_agencies=agencies_to_filter,
        filters=filters, sort=sort,
    )
    total = storage.count_listings_for_search(
        search_id, blacklisted_agencies=agencies_to_filter,
        filters=filters,
    )

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


@api_bp.route("/searches/<int:search_id>/logs/export", methods=["GET"])
@require_token
def export_search_logs_api(search_id: int):
    search = current_app.storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404
    zip_path = current_app.storage.export_scrape_logs(search_id)
    return send_file(
        zip_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=Path(zip_path).name,
    )


@api_bp.route("/searches/<int:search_id>/logs/import", methods=["POST"])
@require_token
def import_search_logs_api(search_id: int):
    search = current_app.storage.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404

    upload = request.files.get("log_archive")
    if not upload or not upload.filename:
        return jsonify({"error": "Fichier manquant"}), 400

    upload.seek(0, 2)
    size = upload.tell()
    upload.seek(0)
    if size > 200 * 1024 * 1024:
        return jsonify({"error": "Fichier trop volumineux (max 200MB)"}), 413

    allow_override = request.args.get("allow_override") == "true"
    tmp_path = Path("/tmp") / f"logs_import_{search_id}_{int(datetime.utcnow().timestamp())}.zip"
    upload.save(tmp_path)
    try:
        result = current_app.storage.import_scrape_logs(
            search_id,
            str(tmp_path),
            allow_override=allow_override,
            performed_by=g.user.get("username", ""),
        )
        return jsonify(result), 200
    except ValueError as e:
        if str(e) == "override_required":
            return jsonify({"error": "override_required"}), 409
        return jsonify({"error": str(e)}), 400
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
