"""Admin Blueprint — all /admin/* endpoints."""

from __future__ import annotations

from flask import Blueprint, request, render_template, redirect, url_for, flash, current_app, g

from routes.auth import require_admin

admin_bp = Blueprint("admin", __name__)


@admin_bp.route("/admin")
@require_admin
def admin():
    tab = request.args.get("tab", "dashboard")
    storage = current_app.storage
    stats = storage.get_enhanced_admin_stats()
    stats["bff_enabled"] = storage.get_setting("use_bff_api", "true") == "true"
    return render_template("admin.html", stats=stats, active_tab=tab)


@admin_bp.route("/admin/users")
@require_admin
def admin_users():
    search_term = request.args.get("search", "")
    storage = current_app.storage
    users = storage.get_all_users()
    if search_term:
        users = [u for u in users if search_term.lower() in u["username"].lower()]
    return render_template("admin.html", active_tab="users", users=users, search_term=search_term,
                           user_count=len(users))


@admin_bp.route("/admin/users/<int:user_id>")
@require_admin
def admin_user_detail(user_id):
    storage = current_app.storage
    user = storage.get_user_detail(user_id)
    if not user:
        flash("Utilisateur introuvable", "error")
        return redirect(url_for("admin.admin"))
    return render_template("admin.html", active_tab="users", user_detail=user)


@admin_bp.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@require_admin
def admin_delete_user(user_id):
    storage = current_app.storage
    user = storage.get_user_detail(user_id)
    if user:
        storage.delete_user(user_id)
        storage.log_admin_action("user_deleted", f"User '{user['username']}' (ID:{user_id}) deleted", g.user["username"])
        flash(f"Utilisateur '{user['username']}' supprimé", "success")
    return redirect(url_for("admin.admin_users"))


@admin_bp.route("/admin/users/<int:user_id>/reset-token", methods=["POST"])
@require_admin
def admin_reset_token(user_id):
    storage = current_app.storage
    user = storage.get_user_detail(user_id)
    if user:
        new_token = storage.reset_user_token(user_id)
        storage.log_admin_action("user_token_reset", f"Token reset for '{user['username']}' (ID:{user_id})", g.user["username"])
        flash(f"Nouveau token pour '{user['username']}': {new_token}", "success")
    return redirect(url_for("admin.admin_user_detail", user_id=user_id))


@admin_bp.route("/admin/users/create", methods=["POST"])
@require_admin
def admin_create_user():
    username = request.form.get("username", "").strip().lower()
    if not username:
        flash("Nom d'utilisateur requis", "error")
        return redirect(url_for("admin.admin_users"))
    try:
        user = current_app.storage.create_user(username)
        current_app.storage.log_admin_action("user_created", f"User '{username}' (ID:{user['id']}) created", g.user["username"])
        flash(f"Utilisateur '{username}' créé. Token: {user['api_token']}", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("admin.admin_users"))


@admin_bp.route("/admin/searches")
@require_admin
def admin_searches():
    user_filter = request.args.get("user", "")
    source_filter = request.args.get("source", "")
    searches = current_app.storage.get_all_searches(user_filter=user_filter, source_filter=source_filter)
    return render_template("admin.html", active_tab="searches", searches=searches,
                           user_filter=user_filter, source_filter=source_filter, search_count=len(searches))


@admin_bp.route("/admin/searches/<int:search_id>")
@require_admin
def admin_search_detail(search_id):
    storage = current_app.storage
    search = storage.get_search_detail(search_id)
    if not search:
        flash("Recherche introuvable", "error")
        return redirect(url_for("admin.admin_searches"))
    return render_template("admin.html", active_tab="searches", search_detail=search)


@admin_bp.route("/admin/searches/<int:search_id>/delete", methods=["POST"])
@require_admin
def admin_delete_search(search_id):
    storage = current_app.storage
    search = storage.get_search(search_id)
    if search:
        storage.delete_search_admin(search_id)
        storage.log_admin_action("search_deleted", f"Search '{search['label']}' (ID:{search_id}) deleted by {g.user['username']}", g.user["username"])
        flash(f"Recherche '{search['label']}' supprimée", "success")
    return redirect(url_for("admin.admin_searches"))


@admin_bp.route("/admin/searches/<int:search_id>/scrape", methods=["POST"])
@require_admin
def admin_scrape_search(search_id):
    storage = current_app.storage
    search = storage.get_search(search_id)
    if not search:
        flash("Recherche introuvable", "error")
        return redirect(url_for("admin.admin_searches"))

    if search_id in current_app._scrape_futures:
        fut = current_app._scrape_futures[search_id]
        if not fut.done():
            flash("Scraping déjà en cours", "warning")
            return redirect(url_for("admin.admin_search_detail", search_id=search_id))
        else:
            del current_app._scrape_futures[search_id]

    from services.scrape_service import ScrapeService
    fut = current_app._scrape_executor.submit(
        ScrapeService(current_app._get_current_object()).execute,
        search_id, search["user_id"],
    )
    current_app._scrape_futures[search_id] = fut
    flash("Scraping démarré en arrière-plan !", "success")
    return redirect(url_for("admin.admin_search_detail", search_id=search_id))


@admin_bp.route("/admin/listings")
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


@admin_bp.route("/admin/listings/<listing_id>")
@require_admin
def admin_listing_detail(listing_id):
    storage = current_app.storage
    listing = storage.get_listing_detail(listing_id)
    if not listing:
        flash("Annonce introuvable", "error")
        return redirect(url_for("admin.admin_listings"))
    return render_template("admin.html", active_tab="listings", listing_detail=listing)


@admin_bp.route("/admin/listings/<listing_id>/delete", methods=["POST"])
@require_admin
def admin_delete_listing(listing_id):
    storage = current_app.storage
    storage.delete_listing(listing_id)
    storage.log_admin_action("listing_deleted", f"Listing '{listing_id}' deleted", g.user["username"])
    flash("Annonce supprimée", "success")
    return redirect(url_for("admin.admin_listings"))


@admin_bp.route("/admin/listings/cleanup-orphan", methods=["POST"])
@require_admin
def admin_cleanup_orphan():
    storage = current_app.storage
    deleted = storage.delete_orphan_listings()
    storage.log_admin_action("orphan_cleanup", f"{deleted} orphan listings deleted", g.user["username"])
    flash(f"{deleted} annonce(s) orpheline(s) supprimée(s)", "success")
    return redirect(url_for("admin.admin_listings"))


@admin_bp.route("/admin/database")
@require_admin
def admin_database():
    storage = current_app.storage
    db_stats = storage.get_db_stats()
    connections = storage.get_active_connections()
    return render_template("admin.html", active_tab="database", db_stats=db_stats, connections=connections)


@admin_bp.route("/admin/database/table/<table_name>")
@require_admin
def admin_table_detail(table_name):
    storage = current_app.storage
    details = storage.get_table_details(table_name)
    db_stats = storage.get_db_stats()
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
    rows, row_count, error = storage.execute_query(sql)
    storage.log_admin_action("db_query", sql[:200], g.user["username"])
    if error:
        flash(f"Erreur: {error}", "error")
        return redirect(url_for("admin.admin_database"))
    flash(f"Requête exécutée — {row_count} ligne(s) affectée(s)", "success")
    return render_template("admin.html", active_tab="database", db_stats=storage.get_db_stats(),
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
    if storage.truncate_table(table_name):
        storage.log_admin_action("table_truncated", f"Table '{table_name}' truncated", g.user["username"])
        flash(f"Table '{table_name}' vidée", "success")
    else:
        flash(f"Impossible de vider la table '{table_name}'", "error")
    return redirect(url_for("admin.admin_database"))


@admin_bp.route("/admin/logs")
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


@admin_bp.route("/admin/logs/purge", methods=["POST"])
@require_admin
def admin_purge_logs():
    days = int(request.form.get("days", 30))
    storage = current_app.storage
    deleted = storage.purge_old_logs(days=days)
    flash(f"{deleted} ancien(s) log(s) supprimé(s)", "success")
    return redirect(url_for("admin.admin_logs"))


@admin_bp.route("/admin/cleanup", methods=["POST"])
@require_admin
def admin_cleanup():
    days = int(request.form.get("days", 4))
    storage = current_app.storage
    deleted = storage.delete_old_listings(days=days)
    storage.log_admin_action("cleanup_executed", f"{deleted} listings older than {days} days deleted", g.user["username"])
    flash(f"{deleted} ancienne(s) annonce(s) supprimée(s)", "success")
    return redirect(url_for("admin.admin"))


@admin_bp.route("/admin/settings/toggle-bff", methods=["POST"])
@require_admin
def admin_toggle_bff():
    storage = current_app.storage
    current = storage.get_setting("use_bff_api", "true")
    new_val = "false" if current == "true" else "true"
    storage.set_setting("use_bff_api", new_val)
    storage.log_admin_action("bff_toggled", f"BFF API {'disabled' if new_val == 'false' else 'enabled'}", g.user["username"])
    flash(f"API BFF {'désactivée' if new_val == 'false' else 'activée'}", "success")
    return redirect(url_for("admin.admin"))
