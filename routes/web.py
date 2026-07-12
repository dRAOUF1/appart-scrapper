"""Web Frontend Blueprint — user-facing routes (/)."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

from flask import (
    Blueprint, request, render_template, redirect, url_for,
    session, flash, g, current_app, jsonify, send_file,
)

from parsers import list_sources
from routes.auth import require_login
from web_utils import to_int

web_bp = Blueprint(
    "web", __name__,
    template_folder="templates",
    static_folder="static",
)


def _parse_search_criteria_from_form(form_data: dict) -> dict:
    """Build criteria dict from web form fields."""
    criteria = {}
    search_url = form_data.get("search_url", "").strip()

    if search_url:
        from scraper.seloger import parse_search_url
        criteria = parse_search_url(search_url)
    else:
        place_ids = form_data.get("place_ids", "").strip()
        price_min = form_data.get("price_min", "").strip()
        price_max = form_data.get("price_max", "").strip()
        space_min = form_data.get("space_min", "").strip()
        space_max = form_data.get("space_max", "").strip()
        distribution = form_data.get("distribution", "Rent")
        estate_type = form_data.get("estate_type", "Apartment")
        rooms = form_data.getlist("rooms") if hasattr(form_data, "getlist") else form_data.get("rooms", [])
        bedrooms = form_data.getlist("bedrooms") if hasattr(form_data, "getlist") else form_data.get("bedrooms", [])
        order = form_data.get("order", "").strip()
        if place_ids:
            criteria["placeIds"] = [p.strip() for p in place_ids.split(",")]
            criteria["location"] = {"placeIds": criteria["placeIds"]}
        if price_min:
            criteria["priceMin"] = int(price_min)
        if price_max:
            criteria["priceMax"] = int(price_max)
        if space_min:
            criteria["spaceMin"] = int(space_min)
        if space_max:
            criteria["spaceMax"] = int(space_max)
        criteria["distributionTypes"] = [distribution]
        criteria["estateTypes"] = [estate_type]
        if rooms:
            criteria["rooms"] = rooms if isinstance(rooms, list) else [rooms]
        if bedrooms:
            criteria["bedrooms"] = bedrooms if isinstance(bedrooms, list) else [bedrooms]
        criteria["order"] = order or "DateDesc"

    return criteria


def _submit_scrape(search_id: int, user_id: int):
    """Submit a scrape job, checking for existing futures."""
    from scrape_control import submit_scrape
    return submit_scrape(current_app._get_current_object(), search_id, user_id)


@web_bp.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("web.dashboard"))
    return redirect(url_for("web.login"))


@web_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        if not username:
            flash("Nom d'utilisateur requis", "error")
            return render_template("login.html")

        user = current_app.storage.users.get_user_by_username(username)
        if not user:
            try:
                user = current_app.storage.users.create_user(username)
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
    data = current_app.storage.users.get_dashboard_data(g.user["id"])
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
    sources = list_sources()
    if request.method == "POST":
        label = request.form.get("label", "").strip()
        ntfy_topic = request.form.get("ntfy_topic", "").strip()
        source = request.form.get("source", "seloger").strip()
        scrape_interval = to_int(request.form.get("scrape_interval", 5), 5)

        criteria = _parse_search_criteria_from_form(request.form)

        if not criteria.get("placeIds"):
            flash("L'URL ne contient pas de lieu valide (locations=...)", "error")
            return redirect(url_for("web.searches"))

        if label and ntfy_topic and criteria.get("placeIds"):
            current_app.storage.searches.create_search(
                g.user["id"], label, ntfy_topic, source, criteria, scrape_interval
            )
            flash(f"Recherche « {label} » créée !", "success")
        else:
            flash("Label, topic ntfy et au moins un lieu requis", "error")
        return redirect(url_for("web.searches"))

    all_searches = current_app.storage.searches.get_user_searches(g.user["id"])
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
    search = current_app.storage.searches.get_search(search_id)
    if search and search["user_id"] == g.user["id"]:
        current_app.storage.searches.delete_search(search_id)
        flash("Recherche supprimée", "success")
    return redirect(url_for("web.searches"))


@web_bp.route("/searches/<int:search_id>/scrape", methods=["POST"])
@require_login
def scrape_search_web(search_id: int):
    search = current_app.storage.searches.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))

    ok, msg = _submit_scrape(search_id, g.user["id"])
    flash(msg, "success" if ok else "warning")
    return redirect(url_for("web.searches"))


@web_bp.route("/searches/<int:search_id>/interval", methods=["POST"])
@require_login
def update_interval_web(search_id: int):
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))

    interval = to_int(request.form.get("scrape_interval", 5), 5)
    if interval < 1:
        interval = 1
    storage.searches.update_scrape_interval(search_id, interval)
    flash(f"Intervalle mis à jour : {interval} minutes", "success")
    return redirect(url_for("web.searches"))


@web_bp.route("/searches/<int:search_id>/toggle-active", methods=["POST"])
@require_login
def toggle_search_active_web(search_id: int):
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))
    new_value = storage.searches.toggle_search_active(search_id)
    if new_value is not None:
        flash("Recherche activée" if new_value else "Recherche désactivée", "success")
    return redirect(url_for("web.searches"))


@web_bp.route("/searches/<int:search_id>/blacklist-agencies", methods=["POST"])
@require_login
def update_blacklist_agencies(search_id: int):
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))

    agencies = request.form.getlist("agencies")
    storage.searches.update_blacklisted_agencies(search_id, agencies)
    flash(f"Blacklist mise à jour : {len(agencies)} agences", "success")
    return redirect(url_for("web.listings", search_id=search_id))


@web_bp.route("/searches/<int:search_id>/blacklist-mode", methods=["POST"])
@require_login
def update_blacklist_mode(search_id: int):
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))

    mode = request.form.get("mode", "exclude")
    if mode not in ("exclude", "no_notify"):
        flash("Mode invalide", "error")
        return redirect(url_for("web.listings", search_id=search_id))

    storage.searches.update_blacklist_mode(search_id, mode)
    mode_label = "Exclure complètement" if mode == "exclude" else "Ne pas notifier"
    flash(f"Mode blacklist: {mode_label}", "success")
    return redirect(url_for("web.listings", search_id=search_id))


@web_bp.route("/searches/<int:search_id>/edit", methods=["GET", "POST"])
@require_login
def edit_search(search_id: int):
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))

    if request.method == "POST":
        label = request.form.get("label", "").strip()
        ntfy_topic = request.form.get("ntfy_topic", "").strip()
        scrape_interval = to_int(request.form.get("scrape_interval", 5), 5)

        criteria = _parse_search_criteria_from_form(request.form)

        if not criteria.get("placeIds"):
            flash("L'URL ne contient pas de lieu valide", "error")
            return render_template("search_edit.html", search=search, sources=list_sources(), now=datetime.utcnow)

        if label and ntfy_topic and criteria.get("placeIds"):
            storage.searches.update_search(
                search_id, g.user["id"],
                label=label, ntfy_topic=ntfy_topic,
                criteria=criteria, scrape_interval=scrape_interval,
            )
            flash("Recherche mise à jour !", "success")
            return redirect(url_for("web.searches"))
        else:
            flash("Label, topic ntfy et au moins un lieu requis", "error")

    stats = storage.scrape_logs.get_scrape_stats(search_id)
    return render_template("search_edit.html", search=search, stats=stats, sources=list_sources(), now=datetime.utcnow)


@web_bp.route("/searches/<int:search_id>/logs", methods=["GET"])
@require_login
def search_logs(search_id: int):
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))

    page = to_int(request.args.get("page", 1), 1)
    per_page = 30
    offset = (page - 1) * per_page
    status_filter = request.args.get("status", "")

    logs = storage.scrape_logs.get_scrape_logs(search_id, limit=per_page, offset=offset, status_filter=status_filter)
    total = storage.scrape_logs.count_scrape_logs(search_id, status_filter=status_filter)
    total_pages = max(1, (total + per_page - 1) // per_page)
    stats = storage.scrape_logs.get_scrape_stats(search_id)
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
    from log_manager import SearchLogManager
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Not found"}), 404

    offset = to_int(request.args.get("offset", 0), 0)
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
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))

    log_entry = storage.scrape_logs.get_scrape_log_raw(log_id, g.user["id"])
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
    from log_manager import SearchLogManager
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Not found"}), 404

    log_entry = storage.scrape_logs.get_scrape_log_raw(log_id, g.user["id"])
    if not log_entry:
        return jsonify({"error": "Log not found"}), 404

    if log_entry.get("raw_logs"):
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


@web_bp.route("/searches/<int:search_id>/logs/export", methods=["GET"])
@require_login
def search_logs_export(search_id: int):
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))

    zip_path = storage.scrape_logs.export_scrape_logs(search_id)
    return send_file(
        zip_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=Path(zip_path).name,
    )


@web_bp.route("/searches/<int:search_id>/logs/import", methods=["POST"])
@require_login
def search_logs_import(search_id: int):
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))

    upload = request.files.get("log_archive")
    if not upload or not upload.filename:
        flash("Fichier manquant", "error")
        return redirect(url_for("web.search_logs", search_id=search_id))

    upload.seek(0, 2)
    size = upload.tell()
    upload.seek(0)
    if size > 200 * 1024 * 1024:
        flash("Fichier trop volumineux (max 200MB)", "error")
        return redirect(url_for("web.search_logs", search_id=search_id))

    allow_override = request.form.get("allow_override") == "true"
    tmp_dir = Path("/tmp")
    tmp_path = tmp_dir / f"logs_import_{search_id}_{int(datetime.utcnow().timestamp())}.zip"
    upload.save(tmp_path)

    try:
        result = storage.scrape_logs.import_scrape_logs(
            search_id,
            str(tmp_path),
            allow_override=allow_override,
            performed_by=g.user.get("username", ""),
        )
        flash(
            f"Import terminé — {result['imported']} ajoutés, {result['skipped']} ignorés",
            "success",
        )
    except ValueError as e:
        if str(e) == "override_required":
            flash("Archive d'un autre search_id — cochez l'override pour forcer", "warning")
        else:
            flash(f"Import impossible: {e}", "error")
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

    return redirect(url_for("web.search_logs", search_id=search_id))


def _parse_listing_filters(args: dict) -> dict:
    filters = {}
    if args.get("q", "").strip():
        filters["q"] = args["q"].strip()
    if args.get("price_min", "").strip():
        try:
            filters["price_min"] = float(args["price_min"])
        except ValueError:
            pass
    if args.get("price_max", "").strip():
        try:
            filters["price_max"] = float(args["price_max"])
        except ValueError:
            pass
    if args.get("surface_min", "").strip():
        try:
            filters["surface_min"] = float(args["surface_min"])
        except ValueError:
            pass
    if args.get("surface_max", "").strip():
        try:
            filters["surface_max"] = float(args["surface_max"])
        except ValueError:
            pass
    if args.get("rooms_min", "").strip():
        try:
            filters["rooms_min"] = float(args["rooms_min"])
        except ValueError:
            pass
    if args.get("rooms_max", "").strip():
        try:
            filters["rooms_max"] = float(args["rooms_max"])
        except ValueError:
            pass
    if args.get("city", "").strip():
        filters["city"] = args["city"].strip()
    if args.get("district", "").strip():
        filters["district"] = args["district"].strip()
    if args.get("zip_code", "").strip():
        filters["zip_code"] = args["zip_code"].strip()
    if args.get("property_type", "").strip():
        filters["property_type"] = args["property_type"].strip()
    if args.get("agency", "").strip():
        filters["agency"] = args["agency"].strip()
    if args.get("epc", "").strip():
        filters["epc"] = args["epc"].strip()
    if args.get("ges", "").strip():
        filters["ges"] = args["ges"].strip()
    if args.get("is_private", "").strip():
        val = args["is_private"].strip()
        if val in ("true", "false"):
            filters["is_private"] = val == "true"
    if args.get("is_new", "").strip():
        val = args["is_new"].strip()
        if val in ("true", "false"):
            filters["is_new"] = val == "true"
    if args.get("date_min", "").strip():
        filters["date_min"] = args["date_min"].strip()
    return filters


@web_bp.route("/listings/<int:search_id>")
@require_login
def listings(search_id: int):
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))

    page = to_int(request.args.get("page", 1), 1)
    per_page = 20
    offset = (page - 1) * per_page

    blacklisted = search.get("blacklisted_agencies") or []
    blacklist_mode = search.get("blacklist_mode", "exclude")

    agencies_to_filter = []
    if blacklist_mode == "exclude" and blacklisted:
        agencies_to_filter = blacklisted

    filters = _parse_listing_filters(request.args)
    sort = request.args.get("sort", "found_at_desc")

    all_listings = storage.listings.get_listings_for_search(
        search_id, limit=per_page, offset=offset,
        blacklisted_agencies=agencies_to_filter,
        filters=filters, sort=sort,
    )
    total = storage.listings.count_listings_for_search(
        search_id, blacklisted_agencies=agencies_to_filter,
        filters=filters,
    )
    total_pages = max(1, (total + per_page - 1) // per_page)

    available_agencies = storage.listings.get_unique_agencies_for_user(g.user["id"])
    filter_options = storage.listings.get_filter_options(search_id)

    active_filters = {k: str(v) for k, v in request.args.items() if k != "page" and v}
    query_string = urlencode(active_filters)

    return render_template(
        "listings.html",
        search=search,
        listings=all_listings,
        total=total,
        page=page,
        total_pages=total_pages,
        available_agencies=available_agencies,
        blacklist_mode=blacklist_mode,
        filter_options=filter_options,
        active_filters=active_filters,
        sort=sort,
        query_string=query_string,
    )


@web_bp.route("/health")
def health():
    return "OK", 200


@web_bp.route("/cleanup", methods=["POST"])
@require_login
def cleanup():
    days = to_int(request.form.get("days", 4), 4)
    deleted = current_app.storage.listings.delete_old_listings(days=days)
    flash(f"{deleted} ancienne(s) annonce(s) supprimée(s)", "success")
    return redirect(url_for("web.dashboard"))
