"""Admin Blueprint — all /admin/* endpoints.

Rendu (issue #16) : chaque onglet vit dans un partial `templates/admin/_<tab>.html`.
Une vue répond soit la page complète (`admin.html` incluant le partial), soit —
si la requête vient d'HTMX (en-tête `HX-Request`) — le seul fragment de l'onglet
plus la barre d'onglets « hors bande ». Une action POST répond de même, avec un
toast porté par l'en-tête `HX-Trigger` au lieu d'un flash de session.

Aucune logique métier ici : mêmes repositories, mêmes messages, mêmes écritures
au journal d'audit qu'avant le socle HTMX.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    g,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from core.web_utils import to_int
from routes.auth import require_admin

admin_bp = Blueprint("admin", __name__)

# Onglets servis par la vue principale. Le nom du partial rendu dépend de cette
# allowlist : une valeur inconnue de `?tab=` est ramenée au dashboard, jamais
# passée telle quelle au loader de templates.
_TABS = ("dashboard", "users", "searches", "listings", "database", "logs")


# ---------------------------------------------------------------------------
# Helpers de rendu — page complète vs fragment HTMX
# ---------------------------------------------------------------------------

def _est_requete_htmx() -> bool:
    """Vrai si la requête provient d'htmx (la librairie pose cet en-tête)."""
    return request.headers.get("HX-Request") == "true"


def _fragment_admin_tab(onglet: str, **ctx) -> str:
    """Fragment HTMX d'un onglet : contenu + barre d'onglets hors bande.

    La nav est réémise avec `hx-swap-oob` : htmx remplace celle de la page par
    le même id, l'onglet actif suit la navigation sans rechargement.
    """
    ctx["active_tab"] = onglet
    contenu = render_template(f"admin/_{onglet}.html", **ctx)
    nav = render_template("admin/_tabs_nav.html", oob=True, **ctx)
    return contenu + nav


def _render_admin_tab(onglet: str, **ctx) -> Response | str:
    """Réponse d'une vue GET d'onglet : page complète, ou fragment si requête HTMX."""
    if _est_requete_htmx():
        return _fragment_admin_tab(onglet, **ctx)
    ctx["active_tab"] = onglet
    return render_template("admin.html", **ctx)


def _reponse_action(message: str, categorie: str, url_redirection: str, onglet: str, **ctx) -> Response:
    """Réponse commune des actions POST, selon le client.

    - HTMX : le fragment de l'onglet mis à jour + toast via HX-Trigger. Le flash
      n'est PAS posé : il ressortirait au prochain chargement complet de page.
    - navigateur classique : flash + redirection, exactement comme avant le
      socle HTMX (dégradation gracieuse).
    """
    if _est_requete_htmx():
        reponse = make_response(_fragment_admin_tab(onglet, **ctx))
        reponse.headers["HX-Trigger"] = json.dumps({"admin:toast": {"message": message, "category": categorie}})
        # L'URL suit le contenu affiché (ex. suppression depuis une fiche détail :
        # le fragment retombe sur la liste, l'historique doit en tenir compte).
        reponse.headers["HX-Push-Url"] = url_redirection
        return reponse
    flash(message, categorie)
    return redirect(url_redirection)


def _reponse_rendue(message: str, categorie: str, onglet: str, **ctx) -> Response:
    """Comme `_reponse_action`, mais en mode classique la page est rendue
    directement au lieu de rediriger : comportement historique de l'exécution
    SQL, dont le résultat doit rester visible immédiatement."""
    if _est_requete_htmx():
        reponse = make_response(_fragment_admin_tab(onglet, **ctx))
        reponse.headers["HX-Trigger"] = json.dumps({"admin:toast": {"message": message, "category": categorie}})
        return reponse
    flash(message, categorie)
    return _render_admin_tab(onglet, **ctx)


# ---------------------------------------------------------------------------
# Contextes de vue — partagés entre les GET d'onglets et les fragments rendus
# après une action POST. Ils lisent `request.args` : depuis une action POST
# (pas d'args), ils produisent l'état par défaut de l'onglet, soit exactement
# ce que donnaient les redirections avant le socle HTMX.
# ---------------------------------------------------------------------------

def _contexte_dashboard(storage) -> dict:
    return {"stats": storage.admin.get_enhanced_admin_stats()}


def _contexte_utilisateurs(storage) -> dict:
    search_term = request.args.get("search", "")
    users = storage.users.get_all_users()
    if search_term:
        users = [u for u in users if search_term.lower() in u["username"].lower()]
    return {"users": users, "search_term": search_term, "user_count": len(users)}


def _contexte_recherches(storage) -> dict:
    user_filter = request.args.get("user", "")
    source_filter = request.args.get("source", "")
    searches = storage.searches.get_all_searches(user_filter=user_filter, source_filter=source_filter)
    return {"searches": searches, "user_filter": user_filter, "source_filter": source_filter,
            "search_count": len(searches)}


def _contexte_annonces(storage) -> dict:
    page = to_int(request.args.get("page", 1), 1)
    per_page = 30
    offset = (page - 1) * per_page
    search_term = request.args.get("search", "")
    source_filter = request.args.get("source", "")
    listings = storage.listings.get_all_listings(
        limit=per_page, offset=offset, search_term=search_term, source_filter=source_filter
    )
    total = storage.listings.count_all_listings(search_term=search_term, source_filter=source_filter)
    return {"listings": listings, "total": total, "page": page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
            "search_term": search_term, "source_filter": source_filter,
            "orphan_count": storage.listings.get_orphan_listings_count()}


def _contexte_base(storage, **extras) -> dict:
    ctx = {"db_stats": storage.admin.get_db_stats(),
           "connections": storage.admin.get_active_connections()}
    ctx.update(extras)
    return ctx


def _contexte_logs(storage) -> dict:
    page = to_int(request.args.get("page", 1), 1)
    per_page = 50
    offset = (page - 1) * per_page
    action_filter = request.args.get("action", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    logs = storage.admin.get_admin_logs(limit=per_page, offset=offset, action_filter=action_filter,
                                  date_from=date_from, date_to=date_to)
    total = storage.admin.count_admin_logs(action_filter=action_filter, date_from=date_from, date_to=date_to)
    return {"logs": logs, "total_logs": total, "page": page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
            "action_filter": action_filter, "date_from": date_from, "date_to": date_to}


# Onglet → constructeur de son contexte de vue.
_CONTEXTE_ONGLETS = {
    "dashboard": _contexte_dashboard,
    "users": _contexte_utilisateurs,
    "searches": _contexte_recherches,
    "listings": _contexte_annonces,
    "database": lambda storage: _contexte_base(storage),
    "logs": _contexte_logs,
}


@admin_bp.route("/admin")
@require_admin
def admin():
    tab = request.args.get("tab", "dashboard")
    if tab not in _TABS:
        tab = "dashboard"
    storage = current_app.storage
    if tab == "dashboard" and request.args.get("fragment") == "stats":
        # Zone vivante (#16) : le polling HTMX ne recharge que cartes + alertes.
        return render_template("admin/_stats_zone.html", stats=storage.admin.get_enhanced_admin_stats())
    return _render_admin_tab(tab, **_CONTEXTE_ONGLETS[tab](storage))


@admin_bp.route("/admin/users")
@require_admin
def admin_users():
    return _render_admin_tab("users", **_CONTEXTE_ONGLETS["users"](current_app.storage))


@admin_bp.route("/admin/users/<int:user_id>")
@require_admin
def admin_user_detail(user_id):
    storage = current_app.storage
    user = storage.users.get_user_detail(user_id)
    if not user:
        flash("Utilisateur introuvable", "error")
        return redirect(url_for("admin.admin"))
    return _render_admin_tab("users", user_detail=user)


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
        return _reponse_action(
            f"Utilisateur '{user['username']}' supprimé", "success",
            url_for("admin.admin_users"), "users", **_CONTEXTE_ONGLETS["users"](storage),
        )
    return redirect(url_for("admin.admin_users"))


@admin_bp.route("/admin/users/create", methods=["POST"])
@require_admin
def admin_create_user():
    username = request.form.get("username", "").strip().lower()
    if not username:
        return _reponse_action(
            "Nom d'utilisateur requis", "error",
            url_for("admin.admin_users"), "users", **_CONTEXTE_ONGLETS["users"](current_app.storage),
        )
    try:
        user = current_app.storage.users.create_user(username)
        current_app.storage.admin.log_admin_action(
            "user_created", f"User '{username}' (ID:{user['id']}) created", g.user["username"]
        )
        message, categorie = f"Utilisateur '{username}' créé.", "success"
    except ValueError as e:
        message, categorie = str(e), "error"
    return _reponse_action(
        message, categorie,
        url_for("admin.admin_users"), "users", **_CONTEXTE_ONGLETS["users"](current_app.storage),
    )


@admin_bp.route("/admin/searches")
@require_admin
def admin_searches():
    return _render_admin_tab("searches", **_CONTEXTE_ONGLETS["searches"](current_app.storage))


@admin_bp.route("/admin/searches/<int:search_id>")
@require_admin
def admin_search_detail(search_id):
    storage = current_app.storage
    search = storage.searches.get_search_detail(search_id)
    if not search:
        flash("Recherche introuvable", "error")
        return redirect(url_for("admin.admin_searches"))
    return _render_admin_tab("searches", search_detail=search)


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
        return _reponse_action(
            f"Recherche '{search['label']}' supprimée", "success",
            url_for("admin.admin_searches"), "searches", **_CONTEXTE_ONGLETS["searches"](storage),
        )
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
    # Comme la redirection d'avant, on retombe sur la fiche de la recherche ;
    # si le détail est indisponible, on rend l'état par défaut de l'onglet.
    detail = storage.searches.get_search_detail(search_id)
    ctx = {"search_detail": detail} if detail else _CONTEXTE_ONGLETS["searches"](storage)
    return _reponse_action(
        msg, "success" if ok else "warning",
        url_for("admin.admin_search_detail", search_id=search_id), "searches", **ctx,
    )


@admin_bp.route("/admin/listings")
@require_admin
def admin_listings():
    return _render_admin_tab("listings", **_CONTEXTE_ONGLETS["listings"](current_app.storage))


@admin_bp.route("/admin/listings/<listing_id>")
@require_admin
def admin_listing_detail(listing_id):
    storage = current_app.storage
    listing = storage.listings.get_listing_detail(listing_id)
    if not listing:
        flash("Annonce introuvable", "error")
        return redirect(url_for("admin.admin_listings"))
    return _render_admin_tab("listings", listing_detail=listing)


@admin_bp.route("/admin/listings/<listing_id>/delete", methods=["POST"])
@require_admin
def admin_delete_listing(listing_id):
    storage = current_app.storage
    storage.listings.delete_listing(listing_id)
    storage.admin.log_admin_action("listing_deleted", f"Listing '{listing_id}' deleted", g.user["username"])
    return _reponse_action(
        "Annonce supprimée", "success",
        url_for("admin.admin_listings"), "listings", **_CONTEXTE_ONGLETS["listings"](storage),
    )


@admin_bp.route("/admin/listings/cleanup-orphan", methods=["POST"])
@require_admin
def admin_cleanup_orphan():
    storage = current_app.storage
    deleted = storage.listings.delete_orphan_listings()
    storage.admin.log_admin_action("orphan_cleanup", f"{deleted} orphan listings deleted", g.user["username"])
    return _reponse_action(
        f"{deleted} annonce(s) orpheline(s) supprimée(s)", "success",
        url_for("admin.admin_listings"), "listings", **_CONTEXTE_ONGLETS["listings"](storage),
    )


@admin_bp.route("/admin/database")
@require_admin
def admin_database():
    return _render_admin_tab("database", **_CONTEXTE_ONGLETS["database"](current_app.storage))


@admin_bp.route("/admin/database/table/<table_name>")
@require_admin
def admin_table_detail(table_name):
    storage = current_app.storage
    details = storage.admin.get_table_details(table_name)
    return _render_admin_tab("database", **_contexte_base(storage, table_name=table_name, table_details=details))


@admin_bp.route("/admin/database/query", methods=["POST"])
@require_admin
def admin_execute_query():
    sql = request.form.get("sql", "").strip()
    storage = current_app.storage
    if not sql:
        return _reponse_action(
            "Requête vide", "error",
            url_for("admin.admin_database"), "database", **_contexte_base(storage),
        )
    rows, row_count, error = storage.admin.execute_query(sql)
    storage.admin.log_admin_action("db_query", sql[:200], g.user["username"])
    if error:
        return _reponse_action(
            f"Erreur: {error}", "error",
            url_for("admin.admin_database"), "database", **_contexte_base(storage, query_sql=sql),
        )
    return _reponse_rendue(
        f"Requête exécutée — {row_count} ligne(s) affectée(s)", "success",
        "database",
        **_contexte_base(storage, query_result=rows, query_row_count=row_count, query_sql=sql),
    )


@admin_bp.route("/admin/database/truncate", methods=["POST"])
@require_admin
def admin_truncate_table():
    table_name = request.form.get("table_name", "").strip()
    storage = current_app.storage
    if not table_name:
        return _reponse_action(
            "Nom de table requis", "error",
            url_for("admin.admin_database"), "database", **_contexte_base(storage),
        )
    ALLOWED_TABLES = {"users", "searches", "listings", "search_listings", "scrape_logs", "admin_logs"}
    if table_name not in ALLOWED_TABLES:
        return _reponse_action(
            f"Table '{table_name}' non autorisée", "error",
            url_for("admin.admin_database"), "database", **_contexte_base(storage),
        )
    if storage.admin.truncate_table(table_name):
        storage.admin.log_admin_action("table_truncated", f"Table '{table_name}' truncated", g.user["username"])
        return _reponse_action(
            f"Table '{table_name}' vidée", "success",
            url_for("admin.admin_database"), "database", **_contexte_base(storage),
        )
    return _reponse_action(
        f"Impossible de vider la table '{table_name}'", "error",
        url_for("admin.admin_database"), "database", **_contexte_base(storage),
    )


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
    """Import d'archive : volontairement hors HTMX (upload multipart), la
    navigation classique avec flashs reste la voie normale ici."""
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
    return _render_admin_tab("logs", **_CONTEXTE_ONGLETS["logs"](current_app.storage))


@admin_bp.route("/admin/logs/purge", methods=["POST"])
@require_admin
def admin_purge_logs():
    days = to_int(request.form.get("days", 30), 30)
    storage = current_app.storage
    deleted = storage.admin.purge_old_logs(days=days)
    return _reponse_action(
        f"{deleted} ancien(s) log(s) supprimé(s)", "success",
        url_for("admin.admin_logs"), "logs", **_CONTEXTE_ONGLETS["logs"](storage),
    )


@admin_bp.route("/admin/cleanup", methods=["POST"])
@require_admin
def admin_cleanup():
    days = to_int(request.form.get("days", 4), 4)
    storage = current_app.storage
    deleted = storage.listings.delete_old_listings(days=days)
    storage.admin.log_admin_action(
        "cleanup_executed", f"{deleted} listings older than {days} days deleted", g.user["username"]
    )
    return _reponse_action(
        f"{deleted} ancienne(s) annonce(s) supprimée(s)", "success",
        url_for("admin.admin"), "dashboard", **_CONTEXTE_ONGLETS["dashboard"](storage),
    )
