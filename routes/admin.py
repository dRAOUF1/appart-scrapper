"""Admin Blueprint — all /admin/* endpoints."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, send_file, url_for

from core.web_utils import to_int
from routes.auth import require_admin

admin_bp = Blueprint("admin", __name__)


@admin_bp.route("/admin")
@require_admin
def admin():
    tab = request.args.get("tab", "dashboard")
    storage = current_app.storage
    stats = storage.admin.get_enhanced_admin_stats()
    return render_template("admin.html", stats=stats, active_tab=tab)


@admin_bp.route("/admin/users")
@require_admin
def admin_users():
    search_term = request.args.get("search", "")
    storage = current_app.storage
    users = storage.users.get_all_users()
    if search_term:
        users = [u for u in users if search_term.lower() in u["username"].lower()]
    return render_template("admin.html", active_tab="users", users=users, search_term=search_term,
                           user_count=len(users))


@admin_bp.route("/admin/users/<int:user_id>")
@require_admin
def admin_user_detail(user_id):
    storage = current_app.storage
    user = storage.users.get_user_detail(user_id)
    if not user:
        flash("Utilisateur introuvable", "error")
        return redirect(url_for("admin.admin"))
    return render_template("admin.html", active_tab="users", user_detail=user)


@admin_bp.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@require_admin
def admin_delete_user(user_id):
    storage = current_app.storage
    user = storage.users.get_user_detail(user_id)
    if user:
        storage.users.delete_user(user_id)
        storage.admin.log_admin_action(
            "user_deleted", f"User '{user['username']}' (ID:{user_id}) deleted", g.user["username"]
        )
        flash(f"Utilisateur '{user['username']}' supprimé", "success")
    return redirect(url_for("admin.admin_users"))


@admin_bp.route("/admin/users/create", methods=["POST"])
@require_admin
def admin_create_user():
    username = request.form.get("username", "").strip().lower()
    if not username:
        flash("Nom d'utilisateur requis", "error")
        return redirect(url_for("admin.admin_users"))
    try:
        user = current_app.storage.users.create_user(username)
        current_app.storage.admin.log_admin_action(
            "user_created", f"User '{username}' (ID:{user['id']}) created", g.user["username"]
        )
        flash(f"Utilisateur '{username}' créé.", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("admin.admin_users"))


@admin_bp.route("/admin/searches")
@require_admin
def admin_searches():
    user_filter = request.args.get("user", "")
    source_filter = request.args.get("source", "")
    searches = current_app.storage.searches.get_all_searches(user_filter=user_filter, source_filter=source_filter)
    return render_template("admin.html", active_tab="searches", searches=searches,
                           user_filter=user_filter, source_filter=source_filter, search_count=len(searches))


@admin_bp.route("/admin/searches/<int:search_id>")
@require_admin
def admin_search_detail(search_id):
    storage = current_app.storage
    search = storage.searches.get_search_detail(search_id)
    if not search:
        flash("Recherche introuvable", "error")
        return redirect(url_for("admin.admin_searches"))
    return render_template("admin.html", active_tab="searches", search_detail=search)


@admin_bp.route("/admin/searches/<int:search_id>/delete", methods=["POST"])
@require_admin
def admin_delete_search(search_id):
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if search:
        storage.searches.delete_search(search_id)
        storage.admin.log_admin_action(
            "search_deleted",
            f"Search '{search['label']}' (ID:{search_id}) deleted by {g.user['username']}",
            g.user["username"],
        )
        flash(f"Recherche '{search['label']}' supprimée", "success")
    return redirect(url_for("admin.admin_searches"))


@admin_bp.route("/admin/searches/<int:search_id>/scrape", methods=["POST"])
@require_admin
def admin_scrape_search(search_id):
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search:
        flash("Recherche introuvable", "error")
        return redirect(url_for("admin.admin_searches"))

    from core.scrape_control import submit_scrape
    ok, msg = submit_scrape(current_app._get_current_object(), search_id, search["user_id"])
    flash(msg, "success" if ok else "warning")
    return redirect(url_for("admin.admin_search_detail", search_id=search_id))


@admin_bp.route("/admin/listings")
@require_admin
def admin_listings():
    page = to_int(request.args.get("page", 1), 1)
    per_page = 30
    offset = (page - 1) * per_page
    search_term = request.args.get("search", "")
    source_filter = request.args.get("source", "")
    storage = current_app.storage
    listings = storage.listings.get_all_listings(
        limit=per_page, offset=offset, search_term=search_term, source_filter=source_filter
    )
    total = storage.listings.count_all_listings(search_term=search_term, source_filter=source_filter)
    total_pages = max(1, (total + per_page - 1) // per_page)
    orphan_count = storage.listings.get_orphan_listings_count()
    return render_template("admin.html", active_tab="listings", listings=listings,
                           total=total, page=page, total_pages=total_pages,
                           search_term=search_term, source_filter=source_filter,
                           orphan_count=orphan_count)


@admin_bp.route("/admin/listings/<listing_id>")
@require_admin
def admin_listing_detail(listing_id):
    storage = current_app.storage
    listing = storage.listings.get_listing_detail(listing_id)
    if not listing:
        flash("Annonce introuvable", "error")
        return redirect(url_for("admin.admin_listings"))
    return render_template("admin.html", active_tab="listings", listing_detail=listing)


@admin_bp.route("/admin/listings/<listing_id>/delete", methods=["POST"])
@require_admin
def admin_delete_listing(listing_id):
    storage = current_app.storage
    storage.listings.delete_listing(listing_id)
    storage.admin.log_admin_action("listing_deleted", f"Listing '{listing_id}' deleted", g.user["username"])
    flash("Annonce supprimée", "success")
    return redirect(url_for("admin.admin_listings"))


@admin_bp.route("/admin/listings/cleanup-orphan", methods=["POST"])
@require_admin
def admin_cleanup_orphan():
    storage = current_app.storage
    deleted = storage.listings.delete_orphan_listings()
    storage.admin.log_admin_action("orphan_cleanup", f"{deleted} orphan listings deleted", g.user["username"])
    flash(f"{deleted} annonce(s) orpheline(s) supprimée(s)", "success")
    return redirect(url_for("admin.admin_listings"))


@admin_bp.route("/admin/database")
@require_admin
def admin_database():
    storage = current_app.storage
    db_stats = storage.admin.get_db_stats()
    connections = storage.admin.get_active_connections()
    return render_template("admin.html", active_tab="database", db_stats=db_stats, connections=connections)


@admin_bp.route("/admin/database/table/<table_name>")
@require_admin
def admin_table_detail(table_name):
    storage = current_app.storage
    details = storage.admin.get_table_details(table_name)
    db_stats = storage.admin.get_db_stats()
    return render_template("admin.html", active_tab="database", db_stats=db_stats,
                           table_name=table_name, table_details=details)


@admin_bp.route("/admin/database/query", methods=["POST"])
@require_admin
def admin_execute_query():
    sql = request.form.get("sql", "").strip()
    if not sql:
        flash("Requête vide", "error")
        return redirect(url_for("admin.admin_database"))
    storage = current_app.storage
    rows, row_count, error = storage.admin.execute_query(sql)
    storage.admin.log_admin_action("db_query", sql[:200], g.user["username"])
    if error:
        flash(f"Erreur: {error}", "error")
        return redirect(url_for("admin.admin_database"))
    flash(f"Requête exécutée — {row_count} ligne(s) affectée(s)", "success")
    return render_template("admin.html", active_tab="database", db_stats=storage.admin.get_db_stats(),
                           query_result=rows, query_row_count=row_count, query_sql=sql)


@admin_bp.route("/admin/database/truncate", methods=["POST"])
@require_admin
def admin_truncate_table():
    table_name = request.form.get("table_name", "").strip()
    if not table_name:
        flash("Nom de table requis", "error")
        return redirect(url_for("admin.admin_database"))
    storage = current_app.storage
    ALLOWED_TABLES = {"users", "searches", "listings", "search_listings", "scrape_logs", "admin_logs"}
    if table_name not in ALLOWED_TABLES:
        flash(f"Table '{table_name}' non autorisée", "error")
        return redirect(url_for("admin.admin_database"))
    if storage.admin.truncate_table(table_name):
        storage.admin.log_admin_action("table_truncated", f"Table '{table_name}' truncated", g.user["username"])
        flash(f"Table '{table_name}' vidée", "success")
    else:
        flash(f"Impossible de vider la table '{table_name}'", "error")
    return redirect(url_for("admin.admin_database"))


@admin_bp.route("/admin/searches/<int:search_id>/logs/export", methods=["GET"])
@require_admin
def admin_export_search_logs(search_id: int):
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search:
        flash("Recherche introuvable", "error")
        return redirect(url_for("admin.admin_searches"))
    zip_path = storage.scrape_logs.export_scrape_logs(search_id)
    return send_file(
        zip_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=Path(zip_path).name,
    )


@admin_bp.route("/admin/searches/<int:search_id>/logs/import", methods=["POST"])
@require_admin
def admin_import_search_logs(search_id: int):
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search:
        flash("Recherche introuvable", "error")
        return redirect(url_for("admin.admin_searches"))

    upload = request.files.get("log_archive")
    if not upload or not upload.filename:
        flash("Fichier manquant", "error")
        return redirect(url_for("admin.admin_search_detail", search_id=search_id))

    upload.seek(0, 2)
    size = upload.tell()
    upload.seek(0)
    if size > 200 * 1024 * 1024:
        flash("Fichier trop volumineux (max 200MB)", "error")
        return redirect(url_for("admin.admin_search_detail", search_id=search_id))

    allow_override = request.form.get("allow_override") == "true"
    tmp_path = Path("/tmp") / f"admin_logs_import_{search_id}_{int(datetime.utcnow().timestamp())}.zip"
    upload.save(tmp_path)
    try:
        result = storage.scrape_logs.import_scrape_logs(
            search_id,
            str(tmp_path),
            allow_override=allow_override,
            performed_by=g.user.get("username", "admin"),
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

    return redirect(url_for("admin.admin_search_detail", search_id=search_id))


@admin_bp.route("/admin/logs")
@require_admin
def admin_logs():
    page = to_int(request.args.get("page", 1), 1)
    per_page = 50
    offset = (page - 1) * per_page
    action_filter = request.args.get("action", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    storage = current_app.storage
    logs = storage.admin.get_admin_logs(limit=per_page, offset=offset, action_filter=action_filter,
                                  date_from=date_from, date_to=date_to)
    total = storage.admin.count_admin_logs(action_filter=action_filter, date_from=date_from, date_to=date_to)
    total_pages = max(1, (total + per_page - 1) // per_page)
    return render_template("admin.html", active_tab="logs", logs=logs, total_logs=total,
                           page=page, total_pages=total_pages, action_filter=action_filter,
                           date_from=date_from, date_to=date_to)


@admin_bp.route("/admin/logs/purge", methods=["POST"])
@require_admin
def admin_purge_logs():
    days = to_int(request.form.get("days", 30), 30)
    storage = current_app.storage
    deleted = storage.admin.purge_old_logs(days=days)
    flash(f"{deleted} ancien(s) log(s) supprimé(s)", "success")
    return redirect(url_for("admin.admin_logs"))


@admin_bp.route("/admin/cleanup", methods=["POST"])
@require_admin
def admin_cleanup():
    days = to_int(request.form.get("days", 4), 4)
    storage = current_app.storage
    deleted = storage.listings.delete_old_listings(days=days)
    storage.admin.log_admin_action(
        "cleanup_executed", f"{deleted} listings older than {days} days deleted", g.user["username"]
    )
    flash(f"{deleted} ancienne(s) annonce(s) supprimée(s)", "success")
    return redirect(url_for("admin.admin"))
