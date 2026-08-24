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
from loguru import logger

from core.web_utils import to_int
from routes.auth import require_admin

admin_bp = Blueprint("admin", __name__)

# Onglets servis par la vue principale. Le nom du partial rendu dépend de cette
# allowlist : une valeur inconnue de `?tab=` est ramenée au dashboard, jamais
# passée telle quelle au loader de templates.
_TABS = ("dashboard", "users", "searches", "listings", "scrapes", "database", "logs")


# ---------------------------------------------------------------------------
# Helpers de rendu — page complète vs fragment HTMX
# ---------------------------------------------------------------------------

def _est_requete_htmx() -> bool:
    """Vrai si la requête provient d'htmx (la librairie pose cet en-tête)."""
    return request.headers.get("HX-Request") == "true"


def _pause_active(storage) -> bool:
    """État affiché de la pause globale du scheduler (#17).

    Fail-open comme la lecture du tick planifié (main._pause_scheduler_active) :
    un échec de lecture n'a jamais à masquer l'admin ni casser un fragment.
    """
    from core.scrape_control import CLE_PAUSE_SCHEDULER

    try:
        valeur = storage.settings.get_setting(CLE_PAUSE_SCHEDULER, "false")
        return str(valeur).strip().lower() == "true"
    except Exception as e:
        logger.error(f"Impossible de lire l'état de pause du scheduler: {e}")
        return False


def _fragment_admin_tab(onglet: str, **ctx) -> str:
    """Fragment HTMX d'un onglet : contenu + barre d'onglets hors bande.

    La nav est réémise avec `hx-swap-oob` : htmx remplace celle de la page par
    le même id, l'onglet actif suit la navigation sans rechargement. Le bandeau
    de pause (#17) suit le même chemin : son état est relu à chaque fragment,
    donc un toggle effectué ailleurs (autre onglet, autre admin) se propage.
    """
    ctx["active_tab"] = onglet
    ctx.setdefault("scheduler_paused", _pause_active(current_app.storage))
    contenu = render_template(f"admin/_{onglet}.html", **ctx)
    nav = render_template("admin/_tabs_nav.html", oob=True, **ctx)
    bandeau = render_template("admin/_pause_banner.html", oob=True, **ctx)
    return contenu + nav + bandeau


def _render_admin_tab(onglet: str, **ctx) -> Response | str:
    """Réponse d'une vue GET d'onglet : page complète, ou fragment si requête HTMX."""
    ctx.setdefault("scheduler_paused", _pause_active(current_app.storage))
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

def _contexte_file(application) -> dict:
    """Contexte du fragment « file d'attente live » des scrapes (#17).

    Agrège, sans JAMAIS lever (le fragment est pollé toutes les 5 s, il doit
    rester rendable en tout état de cause) :
      - les scrapes non terminés (app._scrape_futures via core.file_dattente),
        enrichis du label/source lus en base ;
      - l'occupation de l'executor (capacité = max_workers) ;
      - le prochain passage planifié du scheduler ;
      - le détenteur du verrou consultatif 727271 (pg_locks/pg_stat_activity,
        lus par la voie readonly existante execute_query — aucun schéma touché) ;
      - l'état de la pause globale.
    """
    from core.scrape_control import CLE_VERROU_SCHEDULER, file_dattente

    storage = application.storage

    # Scrapes en cours / en attente — enrichissement DB best-effort.
    entrees: list[dict] = []
    nb_termines = 0
    try:
        for entree in file_dattente(application):
            if entree["termine"]:
                nb_termines += 1
                continue
            recherche = storage.searches.get_search(entree["search_id"]) or {}
            entrees.append(
                {
                    **entree,
                    "label": recherche.get("label") or f"Recherche #{entree['search_id']}",
                    "source": recherche.get("source") or "—",
                }
            )
    except Exception as e:
        logger.error(f"Impossible de lire la file des scrapes: {e}")

    # Occupation de l'executor : max_workers est privé mais stable ; en test,
    # le double MagicMock rend l'attribut non chiffrable → repli neutre.
    try:
        capacite = max(1, int(getattr(application._scrape_executor, "_max_workers", 1)))
    except Exception:
        capacite = 1

    # Prochain passage planifié : APScheduler parké sur l'app par
    # _start_background_tasks ; absent (test, verrou pris ailleurs) → None.
    prochain_passage = None
    scheduler = getattr(application, "_scheduler", None)
    try:
        job = scheduler.get_job("scrape_scheduler") if scheduler is not None else None
        prochain_passage = job.next_run_time if job is not None else None
    except Exception:
        prochain_passage = None

    # Détenteur du verrou consultatif : lecture readonly via le repo admin.
    # L'entête SQL est une f-string, mais la seule valeur interpolée est
    # CLE_VERROU_SCHEDULER — constante entière du code, jamais une entrée
    # utilisateur (execute_query est de toute façon en transaction READ ONLY).
    verrou_detenu_par = None
    try:
        rows, _count, error = storage.admin.execute_query(
            "SELECT a.usename, a.application_name FROM pg_locks l"
            " JOIN pg_stat_activity a ON a.pid = l.pid"
            f" WHERE l.locktype = 'advisory' AND l.objid = {CLE_VERROU_SCHEDULER}"
        )
        if not error and rows:
            verrou_detenu_par = rows[0].get("application_name") or rows[0].get("usename")
    except Exception as e:
        logger.error(f"Impossible de lire le détenteur du verrou du scheduler: {e}")

    return {
        "file_entrees": entrees,
        "nb_termines_non_purges": nb_termines,
        "nb_actifs": len(entrees),
        "capacite_executor": capacite,
        "prochain_passage": prochain_passage,
        "verrou_detenu_par": verrou_detenu_par,
        "scheduler_paused": _pause_active(storage),
    }


def _sources_actives(storage) -> dict:
    """Sources distinctes des recherches actives + effectifs (#17).

    Retourne `{"sources_actives": [{"source", "count"}...], "nb_recherches_actives": N}`
    pour alimenter les boutons de scrape en masse. Une recherche dont `sources`
    est vide retombe sur sa colonne legacy `source`, comme le fait le tick
    planifié ; une recherche multi-sources compte pour chacune.
    """
    compteur: dict[str, int] = {}
    nb_recherches_actives = 0
    try:
        for s in storage.searches.get_all_searches():
            if not s.get("is_active", True):
                continue
            nb_recherches_actives += 1
            for source in s.get("sources") or [s.get("source")]:
                if source:
                    compteur[source] = compteur.get(source, 0) + 1
    except Exception as e:
        logger.error(f"Impossible de lister les sources actives: {e}")
    return {
        "sources_actives": [{"source": src, "count": cnt} for src, cnt in sorted(compteur.items())],
        "nb_recherches_actives": nb_recherches_actives,
    }


def _contexte_dashboard(storage) -> dict:
    """Dashboard = page de pilotage (#17) : stats + état de la file + bulk."""
    application = current_app._get_current_object()
    ctx = {"stats": storage.admin.get_enhanced_admin_stats()}
    ctx.update(_contexte_file(application))
    ctx.update(_sources_actives(storage))
    return ctx


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


def _contexte_scrapes(storage) -> dict:
    """Contexte de l'onglet SCRAPES (#19) : historique global + cartes santé.

    Les filtres sont lus via `request.values` (args GET **et** form POST) : le
    formulaire de relance transporte les filtres courants en champs cachés, la
    réponse HTMX de l'action reconstruit donc exactement la vue filtrée/paginée
    d'où elle vient — la pagination et les filtres survivent à l'action.

    La source n'existe pas dans les entrées de log (stockage fichier par
    recherche) : filtrer par source revient à restreindre aux recherches qui
    la déclarent ; idem pour le filtre « recherche ». Le repository reçoit une
    liste `search_ids`, jamais de logique métier.
    """
    page = max(1, to_int(request.values.get("page", 1), 1))
    per_page = 20
    statut_filter = request.values.get("statut", "")
    source_filter = request.values.get("source", "")
    recherche_filter = request.values.get("recherche", "")
    date_from = request.values.get("date_from", "")
    date_to = request.values.get("date_to", "")

    recherches = storage.searches.get_all_searches()
    par_id = {s["id"]: s for s in recherches}

    search_ids = None
    if recherche_filter:
        try:
            search_ids = [int(recherche_filter)]
        except ValueError:
            search_ids = []  # valeur non numérique : aucun résultat, pas d'erreur 500
    elif source_filter:
        search_ids = [
            sid for sid, s in par_id.items()
            if source_filter in (s.get("sources") or [s.get("source")])
        ]

    kwargs_filtres = {
        "status_filter": statut_filter,
        "search_ids": search_ids,
        "date_from": date_from,
        "date_to": date_to,
    }
    offset = (page - 1) * per_page
    logs = storage.scrape_logs.get_all_scrape_logs(limit=per_page, offset=offset, **kwargs_filtres)
    total = storage.scrape_logs.count_all_scrape_logs(**kwargs_filtres)

    lignes = []
    for log in logs:
        recherche = par_id.get(log.get("search_id"))
        lignes.append({
            **log,
            "search_label": (recherche or {}).get("label") or f"Recherche #{log.get('search_id')}",
            "search_source": (recherche or {}).get("source") or "—",
            # Le bouton « Relancer » n'a de sens que si la recherche existe
            # ENCORE et reste active (défense en profondeur côté route aussi).
            "peut_relancer": bool(recherche) and bool(recherche.get("is_active", True)),
        })

    return {
        "logs": lignes,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "statut_filter": statut_filter,
        "source_filter": source_filter,
        "recherche_filter": recherche_filter,
        "date_from": date_from,
        "date_to": date_to,
        "recherches": [{"id": s["id"], "label": s.get("label") or f"Recherche #{s['id']}"} for s in recherches],
        "sources_disponibles": sorted({
            src for s in recherches for src in (s.get("sources") or [s.get("source")]) if src
        }),
        "stats_globales": storage.scrape_logs.get_global_scrape_stats(),
    }


def _contexte_scrape_log_detail(storage, log_id: int) -> dict | None:
    """Contexte du viewer de log brut (#19) dans l'onglet scrapes.

    `get_scrape_log_raw` SANS user_id : l'admin consulte les logs de TOUTES les
    recherches (la restriction propriétaire est le chemin utilisateur web.py).
    """
    entree = storage.scrape_logs.get_scrape_log_raw(log_id)
    if not entree:
        return None
    recherche = storage.searches.get_search(entree.get("search_id"))
    return {"scrape_log_detail": entree, "scrape_log_recherche": recherche}


# Onglet → constructeur de son contexte de vue.
_CONTEXTE_ONGLETS = {
    "dashboard": _contexte_dashboard,
    "users": _contexte_utilisateurs,
    "searches": _contexte_recherches,
    "listings": _contexte_annonces,
    "scrapes": _contexte_scrapes,
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
    if tab == "dashboard" and request.args.get("fragment") == "queue":
        # Zone vivante (#17) : file d'attente des scrapes, pollée toutes les 5 s.
        return render_template("admin/_queue_zone.html", **_contexte_file(current_app._get_current_object()))
    return _render_admin_tab(tab, **_CONTEXTE_ONGLETS[tab](storage))


# ---------------------------------------------------------------------------
# Pilotage du scraping (issue #17) — pause globale, file d'attente, bulk
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/scheduler/pause", methods=["POST"])
@require_admin
def admin_toggle_pause_scheduler():
    """Bascule de la pause GLOBALE du scheduler (#17).

    La pause ne bloque QUE les scrapes planifiés (le tick la relit à chaque
    passage) : un lancement manuel reste possible pendant la pause. Chaque
    bascule est tracée dans le journal d'audit.
    """
    storage = current_app.storage
    from core.scrape_control import CLE_PAUSE_SCHEDULER

    en_pause = not _pause_active(storage)
    storage.settings.set_setting(CLE_PAUSE_SCHEDULER, "true" if en_pause else "false")
    storage.admin.log_admin_action(
        "scheduler_pause_toggled",
        f"Scheduler {'mis en pause' if en_pause else 'repris'} by {g.user['username']}",
        g.user["username"],
    )
    message = (
        "Scheduler mis en pause — plus aucun scrape planifié tant que la pause est active"
        if en_pause
        else "Scheduler repris — les scrapes planifiés reprennent"
    )
    return _reponse_action(
        message, "warning" if en_pause else "success",
        url_for("admin.admin"), "dashboard", **_CONTEXTE_ONGLETS["dashboard"](storage),
    )


@admin_bp.route("/admin/scrapes/bulk", methods=["POST"])
@require_admin
def admin_bulk_scrape():
    """Scrape EN MASSE (#17) : toutes les recherches actives, ou d'une source.

    Les soumissions passent par submit_scrape : la déduplication existante
    évite de doubler un scrape déjà en cours ou en file, et la file
    séquentielle (max_workers=1) reste la règle. Le rang de chaque recherche
    dans cette file est visible dans la zone vivante du dashboard.
    """
    storage = current_app.storage
    cible = request.form.get("cible", "").strip().lower()

    actives = [s for s in storage.searches.get_all_searches() if s.get("is_active", True)]
    libelle_cible = "toutes recherches actives"
    if cible and cible != "all":
        actives = [s for s in actives if cible in (s.get("sources") or [s.get("source")])]
        libelle_cible = f"source '{cible}'"

    soumises: list[int] = []
    deja_en_file: list[int] = []
    from core.scrape_control import submit_scrape

    application = current_app._get_current_object()
    for s in actives:
        ok, _msg = submit_scrape(application, s["id"], s["user_id"])
        (soumises if ok else deja_en_file).append(s["id"])

    storage.admin.log_admin_action(
        "bulk_scrape",
        f"Bulk scrape {libelle_cible}: {len(soumises)} submitted, "
        f"{len(deja_en_file)} already queued ({', '.join(map(str, soumises)) or 'none'})"
        f" by {g.user['username']}",
        g.user["username"],
    )

    message = f"{len(soumises)} scrape(s) lancé(s) pour {libelle_cible}"
    categorie = "success"
    if deja_en_file:
        message += f" — {len(deja_en_file)} déjà en file d'attente"
    if not soumises and not deja_en_file:
        message = f"Aucune recherche active pour {libelle_cible}"
        categorie = "info"

    return _reponse_action(
        message, categorie,
        url_for("admin.admin"), "dashboard", **_CONTEXTE_ONGLETS["dashboard"](storage),
    )


@admin_bp.route("/admin/users")
@require_admin
def admin_users():
    return _render_admin_tab("users", **_CONTEXTE_ONGLETS["users"](current_app.storage))


# ---------------------------------------------------------------------------
# Historique & diagnostic des scrapes (issue #19) — tab globale, viewer brut,
# relance d'un scrape en échec
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/scrapes")
@require_admin
def admin_scrapes():
    """Onglet SCRAPES (#19) : historique paginé de tous les scrape_logs."""
    return _render_admin_tab("scrapes", **_CONTEXTE_ONGLETS["scrapes"](current_app.storage))


@admin_bp.route("/admin/scrapes/logs/<int:log_id>")
@require_admin
def admin_scrape_log_detail(log_id):
    """Viewer du log brut (#19), rendu dans la tab scrapes.

    Réutilise le rendu du log brut de l'espace utilisateur via la macro
    partagée `log_brut` (templates/_macros.html) — même mise en évidence
    INFO/WARNING/ERROR, sans polling ni rechargement lourd.
    """
    storage = current_app.storage
    ctx = _contexte_scrape_log_detail(storage, log_id)
    if ctx is None:
        flash("Log de scrape introuvable", "error")
        return redirect(url_for("admin.admin_scrapes"))
    return _render_admin_tab("scrapes", **ctx)


@admin_bp.route("/admin/scrapes/<int:search_id>/retry", methods=["POST"])
@require_admin
def admin_retry_scrape(search_id):
    """Relance d'un scrape en ÉCHEC (#19) — même mécanique qu'un lancement manuel.

    Passe par submit_scrape : la déduplication existante refuse de doubler un
    scrape déjà en cours/en file, et la file séquentielle (max_workers=1) est
    respectée. Conditions défendues ici ET au rendu du bouton : le log doit
    être un échec et la recherche exister ENCORE et être active. La pause
    globale (#17) ne bloque pas les lancements manuels ; son état reste
    visible (bandeau hors bande + badge de l'onglet).
    """
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search:
        return _reponse_action(
            "Recherche introuvable — relance impossible", "error",
            url_for("admin.admin_scrapes"), "scrapes", **_CONTEXTE_ONGLETS["scrapes"](storage),
        )
    if not search.get("is_active", True):
        return _reponse_action(
            f"Recherche « {search['label']} » en pause — activez-la avant de relancer",
            "warning",
            url_for("admin.admin_scrapes"), "scrapes", **_CONTEXTE_ONGLETS["scrapes"](storage),
        )

    from core.scrape_control import file_dattente, submit_scrape

    application = current_app._get_current_object()
    ok, msg = submit_scrape(application, search_id, search["user_id"])
    if ok:
        rang = next(
            (e["rang"] for e in file_dattente(application) if e["search_id"] == search_id),
            None,
        )
        storage.admin.log_admin_action(
            "scrape_retried",
            f"Scrape relancé après échec pour '{search['label']}' (ID:{search_id})"
            f" — rang en file {rang} by {g.user['username']}",
            g.user["username"],
        )
        message = f"Scrape relancé — rang en file : {rang}"
        categorie = "success"
    else:
        # Refus de déduplication (« déjà en cours ») : on l'annonce tel quel.
        message = msg
        categorie = "warning"

    return _reponse_action(
        message, categorie,
        url_for("admin.admin_scrapes"), "scrapes", **_CONTEXTE_ONGLETS["scrapes"](storage),
    )


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
    # Issue #19 : bloc santé des scrapes de CETTE recherche dans la fiche.
    return _render_admin_tab(
        "searches", search_detail=search,
        scrape_stats=storage.scrape_logs.get_scrape_stats(search_id),
    )


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


@admin_bp.route("/admin/searches/<int:search_id>/toggle-active", methods=["POST"])
@require_admin
def admin_toggle_search_active(search_id):
    """Bascule actif/pause d'UNE recherche (#17), via toggle_search_active.

    Une recherche en pause n'est plus planifiée (le tick saute is_active falsy)
    mais reste lançable manuellement. Retombe sur la fiche détail si l'action
    venait de là (champ caché `origine`), sinon sur la liste.
    """
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search:
        flash("Recherche introuvable", "error")
        return redirect(url_for("admin.admin_searches"))

    nouvelle_valeur = storage.searches.toggle_search_active(search_id)
    if nouvelle_valeur is None:
        # La ligne a disparu entre la lecture et la bascule : rien à annoncer.
        flash("Recherche introuvable", "error")
        return redirect(url_for("admin.admin_searches"))

    storage.admin.log_admin_action(
        "search_active_toggled",
        f"Search '{search['label']}' (ID:{search_id}) "
        f"{'activated' if nouvelle_valeur else 'paused'} by {g.user['username']}",
        g.user["username"],
    )

    message = "Recherche activée" if nouvelle_valeur else "Recherche mise en pause"
    if request.form.get("origine") == "detail":
        detail = storage.searches.get_search_detail(search_id)
        ctx = {"search_detail": detail} if detail else _CONTEXTE_ONGLETS["searches"](storage)
        url_retour = url_for("admin.admin_search_detail", search_id=search_id)
    else:
        ctx = _CONTEXTE_ONGLETS["searches"](storage)
        url_retour = url_for("admin.admin_searches")
    return _reponse_action(message, "success", url_retour, "searches", **ctx)


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
