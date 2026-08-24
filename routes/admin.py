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

import csv
import io
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

try:  # module standard sous Unix ; absent sous Windows → repli RSS sur None
    import resource
except ImportError:  # pragma: no cover - plateforme sans /proc ni resource
    resource = None

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
    session,
    url_for,
)
from loguru import logger

from core.reglages import (
    CLE_PURGE_LOGS_AUDIT,
    CLE_RETENTION_ANNONCES,
    DEFAUT_PURGE_LOGS_AUDIT,
    DEFAUT_RETENTION_ANNONCES,
    lire_jours,
    valider_jours,
)
from core.scrape_control import dernier_tick
from core.web_utils import format_criteria_lisible, to_int
from parsers import list_sources, remember_manual_overrides
from parsers._dates import DATE_INCONNUE
from repositories.admin_repo import CACHES_GEO
from repositories.base import statistiques_pool
from repositories.listing_repo import _borne_date_comparee
from routes.auth import (
    CLE_IMPERSONE_USERNAME,
    demarrer_impersonation,
    est_impersonation_active,
    require_admin,
    terminer_impersonation,
)
from routes.web import (
    _location_error_message,
    _parse_notify_enabled_from_form,
    _parse_search_criteria_from_form,
    _validate_sources_criteria,
    _validation_error_message,
)

admin_bp = Blueprint("admin", __name__)

# Onglets servis par la vue principale. Le nom du partial rendu dépend de cette
# allowlist : une valeur inconnue de `?tab=` est ramenée au dashboard, jamais
# passée telle quelle au loader de templates.
_TABS = ("dashboard", "users", "searches", "listings", "scrapes", "systeme", "database", "logs")

# Issue #20 — export CSV des annonces : plafond documenté de lignes. Au-delà,
# le fichier est tronqué (et le dit dans son en-tête de commentaire) : un
# export n'a pas vocation à vider la table, et un tampon non borné est une
# DoS mémoire à soi tout seul.
EXPORT_CSV_PLAFOND = 50_000

# En-têtes français de l'export CSV (#20), dans l'ordre des colonnes.
COLONNES_CSV = ["id", "titre", "source", "prix", "surface", "pièces",
                "date publication", "première détection", "ville", "url"]


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
    # Issue #21 : la valeur du réglage pilote le libellé du bouton « Nettoyer
    # annonces » — le formulaire ne poste PAS de `days`, c'est donc bien ce
    # réglage (relu à chaque usage) que l'action appliquera.
    ctx["retention_listings_days"] = lire_jours(storage.settings.get_setting, CLE_RETENTION_ANNONCES,
                                                DEFAUT_RETENTION_ANNONCES)
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


def _lire_filtres_annonces() -> tuple[dict, list[str]]:
    """Filtres avancés des annonces (#20), lus via `request.values`.

    Pattern #19 : les valeurs viennent d'args GET **et** du form POST — les
    champs cachés des formulaires d'action transportent les filtres courants,
    la vue filtrée/paginée survit donc à l'action.

    Une borne malformée est signalée dans la liste retournée (jamais
    silencieuse : l'utilisateur voit pourquoi son filtre n'a pas mordu) et
    écartée ; les autres filtres restent appliqués. Le repository reste la
    dernière ligne de défense — il relève ValueError si on lui passe quand
    même une borne invalide.
    """
    filtres: dict = {}
    erreurs: list[str] = []

    for cle, libelle in (("price_min", "Prix minimum"), ("price_max", "Prix maximum")):
        brut = request.values.get(cle, "").strip()
        if not brut:
            continue
        try:
            filtres[cle] = int(brut)
        except ValueError:
            erreurs.append(f"{libelle} invalide (« {brut} ») — filtre ignoré")

    for cle, libelle in (("first_seen_min", "Première détection (depuis le)"),
                         ("first_seen_max", "Première détection (jusqu'au)")):
        brut = request.values.get(cle, "").strip()
        if not brut:
            continue
        try:
            _borne_date_comparee(brut, fin_de_journee=(cle == "first_seen_max"))
            filtres[cle] = brut
        except ValueError:
            erreurs.append(f"{libelle} : date invalide (« {brut} », format attendu AAAA-MM-JJ) — filtre ignoré")

    if request.values.get("orphelines") in ("1", "true", "on"):
        filtres["orphans_only"] = True

    return filtres, erreurs


def _parametres_annonces_courants(search_term: str, source_filter: str, sort: str,
                                  filtres: dict) -> dict:
    """Querystring aplatie de l'état courant de la vue (#20).

    Sert à la fois aux liens de pagination, aux en-têtes de tri, aux champs
    cachés du formulaire de suppression groupée et au lien d'export : un seul
    endroit garantit que « l'export reflète exactement les filtres actifs » et
    que « la pagination est conservée après action groupée ».
    """
    params = {"search": search_term, "source": source_filter, "sort": sort}
    for cle, valeur in filtres.items():
        # La case « orphelines » voyage SOUS LE NOM DU FORMULAIRE : les URLs
        # (pagination, export, champs cachés du bulk) sont relues tel quel par
        # `_lire_filtres_annonces`, qui ne connaît que ce nom-là.
        if cle == "orphans_only":
            params["orphelines"] = "1"
        else:
            params[cle] = valeur
    return params


def _contexte_annonces(storage) -> dict:
    page = max(1, to_int(request.values.get("page", 1), 1))
    per_page = 30
    offset = (page - 1) * per_page
    search_term = request.values.get("search", "")
    source_filter = request.values.get("source", "")
    sort = request.values.get("sort", "")
    filtres, erreurs_filtre = _lire_filtres_annonces()

    listings = storage.listings.get_all_listings(
        limit=per_page, offset=offset, search_term=search_term,
        source_filter=source_filter, filters=filtres, sort=sort,
    )
    total = storage.listings.count_all_listings(
        search_term=search_term, source_filter=source_filter, filters=filtres,
    )
    return {
        "listings": listings,
        "total": total,
        "page": page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "search_term": search_term,
        "source_filter": source_filter,
        "sort": sort,
        "filtres": filtres,
        "erreurs_filtre": erreurs_filtre,
        # Deux vues du même état : AVEC tri (export, champs cachés du bulk)
        # et SANS (pagination, en-têtes cliquables qui passent leur propre
        # sort — un doublon de clé ferait lever url_for).
        "params_courants": _parametres_annonces_courants(search_term, source_filter, sort, filtres),
        "params_sans_sort": {
            cle: valeur for cle, valeur in _parametres_annonces_courants(
                search_term, source_filter, sort, filtres
            ).items() if cle != "sort"
        },
        "orphan_count": storage.listings.get_orphan_listings_count(),
    }


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
            "action_filter": action_filter, "date_from": date_from, "date_to": date_to,
            # Issue #21 : la purge des logs n'embarque plus de champ `days` —
            # elle applique le réglage relu à l'usage ; l'affiche en conséquence.
            "purge_logs_days": lire_jours(storage.settings.get_setting, CLE_PURGE_LOGS_AUDIT,
                                          DEFAUT_PURGE_LOGS_AUDIT)}


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


# ---------------------------------------------------------------------------
# Santé du process, caches géo et paramètres (issue #21) — tab « Système »
#
# Règle commune : chaque lecture périphérique (env, /proc, pool, base) est
# best-effort et dégrade en valeur neutre — l'onglet est pollé toutes les
# 30 s, il ne doit JAMAIS rendre 500, même base muette ou conteneur sans
# /proc. C'est le même contrat que la file d'attente (#17).
# ---------------------------------------------------------------------------

def _sha_deploye() -> str:
    """SHA git déployé : RENDER_GIT_COMMIT (Render), puis GIT_COMMIT, sinon
    'local'. Une variable présente mais vide compte pour absente."""
    for cle in ("RENDER_GIT_COMMIT", "GIT_COMMIT"):
        valeur = (os.environ.get(cle) or "").strip()
        if valeur:
            return valeur
    return "local"


def _uptime_secondes(application) -> float | None:
    """Secondes écoulées depuis le boot (time.monotonic posé par create_app).

    Absent de la config (app construite hors create_app) → None : l'affichage
    retombe sur un état neutre au lieu de deviner.
    """
    demarrage = application.config.get("BOOT_MONOTONIC")
    if demarrage is None:
        return None
    return max(0.0, time.monotonic() - float(demarrage))


def _format_duree(secondes: float | None) -> str | None:
    """Durée lisible « 2 j 03:04:05 » (le jour n'est affiché que si présent)."""
    if secondes is None:
        return None
    total = max(0, int(secondes))
    jours, reste = divmod(total, 86400)
    heures, reste = divmod(reste, 3600)
    minutes, secondes = divmod(reste, 60)
    prefixe = f"{jours} j " if jours else ""
    return f"{prefixe}{heures:02d}:{minutes:02d}:{secondes:02d}"


def _memoire_rss_ko() -> int | None:
    """Mémoire RSS courante en kB — lecture pure, fallback gracieux.

    1re voie : /proc/self/status (VmRSS = RSS *courant*, Linux/containers).
    Repli : resource.getrusage (ru_maxrss = PIC RSS sous Linux, suffisant
    comme indicateur de santé). Aucune des deux disponibles → None.
    """
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for ligne in status:
                if ligne.startswith("VmRSS:"):
                    return int(ligne.split()[1])
    except Exception:
        pass
    try:
        if resource is not None:
            return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except Exception:
        pass
    return None


def _format_mo(ko: int | None) -> str | None:
    """kB → Mo arrondi, None si la mesure n'a pas été possible."""
    if ko is None:
        return None
    return f"{ko / 1024:.0f} Mo"


def _format_age(secondes: int | None) -> str | None:
    """Âge lisible court : « il y a 42 s », « il y a 5 min », « il y a 2 h »."""
    if secondes is None:
        return None
    if secondes < 60:
        return f"il y a {secondes} s"
    if secondes < 3600:
        return f"il y a {secondes // 60} min"
    return f"il y a {secondes // 3600} h"


def _contexte_sante(application) -> dict:
    """Cartes de santé du process (#21), chaque pièce fail-open.

    Le tick scheduler vient de core.scrape_control (état PROCESS LOCAL : seul
    le process qui détient le verrou consultatif tick — un process sans
    scheduler montre « aucun passage », ce qui est la vérité). Les stats du
    pool lisent le pool partagé de CE process via statistiques_pool().
    """
    try:
        tick = dernier_tick()
    except Exception as e:
        logger.error(f"Impossible de lire le dernier tick du scheduler: {e}")
        tick = None
    try:
        pool = statistiques_pool(application.storage.database_url)
    except Exception as e:
        logger.error(f"Impossible de lire l'état du pool de connexions: {e}")
        pool = None

    sha = _sha_deploye()
    return {
        "sha": sha,
        "sha_court": sha if sha == "local" or len(sha) <= 7 else sha[:7],
        "uptime_lisible": _format_duree(_uptime_secondes(application)),
        "rss_lisible": _format_mo(_memoire_rss_ko()),
        "dernier_tick": tick,
        "tick_age_lisible": _format_age(tick["age_s"]) if tick else None,
        "pool": pool,
    }


def _contexte_systeme(storage) -> dict:
    """Contexte de l'onglet SYSTÈME (#21) : santé + caches géo + réglages."""
    application = current_app._get_current_object()
    try:
        bruts = storage.admin.get_geo_cache_stats() or []
    except Exception as e:
        logger.error(f"Impossible de lire les stats des caches géo: {e}")
        bruts = []

    caches = []
    for brut in bruts:
        ligne = dict(brut)
        if ligne.get("suivi_echecs"):
            entrees = int(ligne.get("entrees") or 0)
            manquees = int(ligne.get("manquees") or 0)
            ligne["manquees_lisible"] = (
                f"{manquees} ({round(100 * manquees / entrees)} %)" if entrees else "0"
            )
        else:
            # Sans échec mémorisé (commune_centres), un taux serait mensonger.
            ligne["manquees_lisible"] = "—"
        caches.append(ligne)

    return {
        **_contexte_sante(application),
        "caches_geo": caches,
        # Réglages lus à CHAQUE rendu (pattern #17) : le formulaire montre
        # toujours ce qu'une routine appliquerait maintenant.
        "retention_listings_days": lire_jours(storage.settings.get_setting, CLE_RETENTION_ANNONCES,
                                              DEFAUT_RETENTION_ANNONCES),
        "purge_logs_days": lire_jours(storage.settings.get_setting, CLE_PURGE_LOGS_AUDIT,
                                      DEFAUT_PURGE_LOGS_AUDIT),
    }


# Onglet → constructeur de son contexte de vue.
_CONTEXTE_ONGLETS = {
    "dashboard": _contexte_dashboard,
    "users": _contexte_utilisateurs,
    "searches": _contexte_recherches,
    "listings": _contexte_annonces,
    "scrapes": _contexte_scrapes,
    "systeme": _contexte_systeme,
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
    if tab == "systeme" and request.args.get("fragment") == "sante":
        # Zone vivante (#21) : santé du process, pollée toutes les 30 s.
        return render_template("admin/_sante_zone.html", **_contexte_sante(current_app._get_current_object()))
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


# ---------------------------------------------------------------------------
# Système : santé process, caches géo, paramètres de scraping (issue #21)
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/system")
@require_admin
def admin_system():
    """Onglet SYSTÈME (#21) : santé du process, caches géo, paramètres."""
    return _render_admin_tab("systeme", **_CONTEXTE_ONGLETS["systeme"](current_app.storage))


def _entrees_cache(storage, table: str) -> int | None:
    """Compteur d'entrées actuelles d'un cache géo (pour l'audit avant/après).

    None si la lecture échoue : l'audit mentionne « n/a » plutôt que de
    masquer la purge pour un compteur indisponible.
    """
    try:
        for cache in storage.admin.get_geo_cache_stats() or []:
            if cache["table"] == table:
                return int(cache.get("entrees") or 0)
    except Exception as e:
        logger.error(f"Compteur indisponible pour le cache {table}: {e}")
    return None


@admin_bp.route("/admin/system/caches-geo/purge", methods=["POST"])
@require_admin
def admin_purge_geo_cache():
    """Purge d'UN cache géo (#21), scoping par table du registre CACHES_GEO.

    Le compte AVANT/APRÈS est relu en base (pas déduit) : il alimente le
    toast ET le journal d'audit `geo_cache_purged` — une purge sans compteur
    serait invérifiable après coup.
    """
    storage = current_app.storage
    table = request.form.get("table", "").strip()

    registre = {c["table"]: c["source"] for c in CACHES_GEO}
    if table not in registre:
        return _reponse_action(
            f"Cache géo inconnu : « {table} »", "error",
            url_for("admin.admin_system"), "systeme",
            **_CONTEXTE_ONGLETS["systeme"](storage),
        )
    source = registre[table]

    avant = _entrees_cache(storage, table)
    supprimees = storage.admin.purge_geo_cache(table)
    apres = _entrees_cache(storage, table)

    storage.admin.log_admin_action(
        "geo_cache_purged",
        f"Cache géo '{source}' ({table}) purgé : {supprimees} entrée(s) supprimée(s)"
        f" ({avant if avant is not None else 'n/a'} avant,"
        f" {apres if apres is not None else 'n/a'} après) by {g.user['username']}",
        g.user["username"],
    )

    if supprimees:
        message = f"Cache « {source} » purgé — {supprimees} entrée(s) supprimée(s)"
        categorie = "success"
    else:
        message = f"Cache « {source} » déjà vide"
        categorie = "info"
    return _reponse_action(
        message, categorie,
        url_for("admin.admin_system"), "systeme",
        **_CONTEXTE_ONGLETS["systeme"](storage),
    )


@admin_bp.route("/admin/system/settings", methods=["POST"])
@require_admin
def admin_save_settings():
    """Enregistrement des paramètres de scraping éditables (#21).

    La pause scheduler n'est PAS ici — c'est le toggle du dashboard (#17),
    ne pas dupliquer. Chaque valeur est validée (entier strictement positif,
    ≤ 365 jours) puis persistée via settings_repo ; les routines qui la
    consomment la RELISENT à chaque usage (`lire_jours`, pattern #17), donc
    l'effet est immédiat au prochain tour concerné, sans redémarrage.
    """
    storage = current_app.storage
    try:
        retention = valider_jours(request.form.get("retention_listings_days"),
                                  "Rétention des annonces")
        purge_logs = valider_jours(request.form.get("purge_logs_days"),
                                   "Purge des logs")
    except ValueError as e:
        return _reponse_action(
            str(e), "error",
            url_for("admin.admin_system"), "systeme",
            **_CONTEXTE_ONGLETS["systeme"](storage),
        )

    storage.settings.set_setting(CLE_RETENTION_ANNONCES, str(retention))
    storage.settings.set_setting(CLE_PURGE_LOGS_AUDIT, str(purge_logs))
    storage.admin.log_admin_action(
        "settings_updated",
        f"Paramètres mis à jour : rétention annonces={retention} j,"
        f" purge logs={purge_logs} j by {g.user['username']}",
        g.user["username"],
    )
    return _reponse_action(
        f"Paramètres enregistrés — rétention annonces {retention} j,"
        f" purge logs {purge_logs} j",
        "success",
        url_for("admin.admin_system"), "systeme",
        **_CONTEXTE_ONGLETS["systeme"](storage),
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
    # Issue #22 : cartes statistiques du compte. Les fenêtres 7 j / 30 j et les
    # comptes actif/inactif sortent d'UNE requête agrégée (user_repo), le
    # dernier scrape réussi/échec d'une passe sur les logs JSONL des recherches
    # DE CET utilisateur (scrape_log_repo). Un échec de lecture ne prive que sa
    # carte : chaque pièce retombe sur un état neutre affiché « — ».
    try:
        stats_utilisateur = storage.users.get_user_stats(user_id) or {}
    except Exception as e:
        logger.error(f"Impossible de lire les stats de l'utilisateur {user_id}: {e}")
        stats_utilisateur = {}
    derniers_scrapes: dict = {}
    try:
        ids_recherches = [s["id"] for s in user.get("searches") or []]
        derniers_scrapes = storage.scrape_logs.get_dernier_scrape_resultat(ids_recherches) or {}
    except Exception as e:
        logger.error(f"Impossible de lire les derniers scrapes de l'utilisateur {user_id}: {e}")
    return _render_admin_tab(
        "users",
        user_detail=user,
        stats_utilisateur=stats_utilisateur,
        dernier_scrape_succes=derniers_scrapes.get("dernier_succes"),
        dernier_scrape_echec=derniers_scrapes.get("dernier_echec"),
    )


# ---------------------------------------------------------------------------
# Vue administrateur lecture seule (issue #22) — entrée et sortie.
#
# Choix documenté : pendant une impersonation, TOUTES les routes admin sont
# inaccessibles SAUF la sortie (`require_admin` renvoie 403, cf. routes/auth.py).
# Un admin qui consulte un compte ne peut rien piloter « depuis » cette vue ;
# la seule action admin disponible est de la quitter proprement. La sortie vit
# donc SANS @require_admin : elle n'exige que la marque d'impersonation dans la
# session — clé qu'un tiers ne peut pas poser (l'entrée est admin-only + CSRF).
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/users/<int:user_id>/impersonate", methods=["POST"])
@require_admin
def admin_impersonate_start(user_id):
    """Entrée en vue administrateur : la session bascule vers la cible.

    Refus explicites (message français, session INTACTE, audit non écrit) :
    - s'impersonner soi-même ;
    - impersonner le compte administrateur (cascade impossible autrement,
      cf. require_admin bloqué pendant l'impersonation) ;
    - cible inexistante.
    """
    storage = current_app.storage

    if est_impersonation_active():
        # Défense en profondeur : require_admin bloque déjà toute route admin
        # en impersonation ; ce test explicite garde le refus si le décorateur
        # évolue un jour.
        flash("Une vue administrateur est déjà active — quittez-la d'abord", "error")
        return redirect(url_for("web.dashboard"))

    if user_id == g.user["id"]:
        flash("Impossible de consulter votre propre compte : vous y êtes déjà", "error")
        return redirect(url_for("admin.admin_user_detail", user_id=user_id))

    cible = storage.users.get_user_by_id(user_id)
    if not cible:
        flash("Utilisateur introuvable", "error")
        return redirect(url_for("admin.admin_users"))

    admin_username = os.environ.get("ADMIN_USERNAME")
    if admin_username and cible["username"] == admin_username:
        flash("Le compte administrateur ne peut pas être ouvert en vue lecture seule", "error")
        return redirect(url_for("admin.admin_user_detail", user_id=user_id))

    demarrer_impersonation(session, g.user, cible)
    storage.admin.log_admin_action(
        "impersonation_started",
        f"Vue administrateur ouverte : admin '{g.user['username']}'"
        f" (ID:{g.user['id']}) consulte '{cible['username']}' (ID:{cible['id']})",
        g.user["username"],
    )
    flash(f"Vue administrateur : vous consultez « {cible['username']} » en lecture seule", "warning")
    return redirect(url_for("web.dashboard"))


@admin_bp.route("/admin/impersonate/exit", methods=["POST"])
def admin_impersonation_exit():
    """Sortie de la vue administrateur — restauration GARANTIE de l'admin.

    La session est restaurée depuis les clés figées à l'entrée : la cible a pu
    être supprimée entre-temps sans conséquence (ses données ne sont plus
    lues). Si l'ADMIN lui-même a disparu, aucune identité valide ne peut être
    reconstituée : la session est purgée et on repasse par /login plutôt que de
    laisser un cookie à moitié restauré.
    """
    if not est_impersonation_active():
        if "user_id" not in session:
            return redirect(url_for("web.login"))
        flash("Aucune vue administrateur active", "info")
        return redirect(url_for("web.dashboard"))

    cible_consultee = session.get(CLE_IMPERSONE_USERNAME, "")
    storage = current_app.storage
    admin_restaure = terminer_impersonation(session, storage)

    if admin_restaure is None:
        flash("Session administrateur introuvable — reconnectez-vous", "error")
        return redirect(url_for("web.login"))

    storage.admin.log_admin_action(
        "impersonation_ended",
        f"Vue administrateur fermée : admin '{admin_restaure['username']}'"
        f" (ID:{admin_restaure['id']}) quitte la consultation de '{cible_consultee}'",
        admin_restaure["username"],
    )
    flash(f"Vue administrateur terminée — bon retour, {admin_restaure['username']}", "success")
    return redirect(url_for("admin.admin"))


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


def _contexte_recherche_detail(storage, search_id: int, **extras) -> dict | None:
    """Contexte de la fiche détail d'une recherche, GET comme après action.

    Porte depuis l'issue #18 les critères déjà mis en forme
    (`criteria_lignes`, via core.web_utils.format_criteria_lisible) pour que
    le partial affiche un résumé français au lieu du JSON brut — qui reste
    disponible en <details> pour le debug avancé.
    """
    detail = storage.searches.get_search_detail(search_id)
    if not detail:
        return None
    ctx = {
        "search_detail": detail,
        # Issue #19 : bloc santé des scrapes de CETTE recherche dans la fiche.
        "scrape_stats": storage.scrape_logs.get_scrape_stats(search_id),
        "criteria_lignes": format_criteria_lisible(detail.get("criteria")),
    }
    ctx.update(extras)
    return ctx


@admin_bp.route("/admin/searches/<int:search_id>")
@require_admin
def admin_search_detail(search_id):
    storage = current_app.storage
    ctx = _contexte_recherche_detail(storage, search_id)
    if ctx is None:
        flash("Recherche introuvable", "error")
        return redirect(url_for("admin.admin_searches"))
    return _render_admin_tab("searches", **ctx)


@admin_bp.route("/admin/searches/<int:search_id>/edit", methods=["GET", "POST"])
@require_admin
def admin_edit_search(search_id):
    """Édition des critères d'une recherche (issue #18), vue par l'admin.

    L'admin édite la recherche D'UN AUTRE utilisateur sans s'en approprier
    rien : la mise à jour passe par `update_search` avec le user_id DU
    PROPRIÉTAIRE (le repository vérifie cette correspondance), jamais celui
    de la session. Le parsing du formulaire et la re-normalisation canonique
    sont EXACTEMENT ceux du flux utilisateur (routes/web.py) : payload
    d'autocomplete repris tel quel (l'inseeCode ne peut pas se perdre),
    correspondance avec une localisation stockée si le payload manque (#24),
    ancien vocabulaire SeLoger converti à la lecture ET à l'écriture,
    validation par source avant enregistrement (#10 : notify_enabled suit).
    """
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search:
        flash("Recherche introuvable", "error")
        return redirect(url_for("admin.admin_searches"))

    if request.method == "POST":
        label = request.form.get("label", "").strip()
        ntfy_topic = request.form.get("ntfy_topic", "").strip()
        scrape_interval = to_int(request.form.get("scrape_interval", 5), 5)
        selected_sources = (
            request.form.getlist("sources")
            or search.get("sources")
            or [search.get("source", "seloger")]
        )

        existing_locations = (search.get("criteria") or {}).get("locations") or []
        criteria, hand_typed_failures = _parse_search_criteria_from_form(request.form, existing_locations)

        location_error = _location_error_message(criteria, hand_typed_failures)
        validation = _validate_sources_criteria(selected_sources, criteria)
        if not location_error and not all(r["ok"] for r in validation):
            location_error = _validation_error_message(validation)

        def _retour_formulaire() -> dict:
            """Le formulaire réaffiché avec la saisie en cours ; si la fiche
            n'est plus lisible, l'état par défaut de l'onglet (jamais 500)."""
            return (
                _contexte_recherche_detail(
                    storage, search_id, edit_mode=True, sources=list_sources(),
                    form_values=request.form,
                )
                or _CONTEXTE_ONGLETS["searches"](storage)
            )

        if location_error:
            return _reponse_rendue(location_error, "error", "searches", **_retour_formulaire())

        if not (label and ntfy_topic):
            return _reponse_rendue("Label et topic ntfy requis", "error", "searches", **_retour_formulaire())

        storage.searches.update_search(
            search_id, search["user_id"],
            label=label, ntfy_topic=ntfy_topic,
            criteria=criteria, scrape_interval=scrape_interval,
            sources=selected_sources,
            notify_enabled=_parse_notify_enabled_from_form(request.form),
        )
        remember_manual_overrides(selected_sources, criteria, storage=storage)
        storage.admin.log_admin_action(
            "search_edited_admin",
            f"Search '{label}' (ID:{search_id}, owner user_id:{search['user_id']})"
            f" edited by {g.user['username']}",
            g.user["username"],
        )
        return _reponse_action(
            f"Recherche « {label} » mise à jour", "success",
            url_for("admin.admin_search_detail", search_id=search_id), "searches",
            **(_contexte_recherche_detail(storage, search_id) or _CONTEXTE_ONGLETS["searches"](storage)),
        )

    ctx = _contexte_recherche_detail(storage, search_id, edit_mode=True, sources=list_sources())
    if ctx is None:
        flash("Recherche introuvable", "error")
        return redirect(url_for("admin.admin_searches"))
    return _render_admin_tab("searches", **ctx)


@admin_bp.route("/admin/searches/<int:search_id>/duplicate", methods=["POST"])
@require_admin
def admin_duplicate_search(search_id):
    """Duplication d'une recherche (issue #18).

    La copie porte le label « Copie de <label> », les mêmes critères
    canoniques, le même propriétaire, `notify_enabled`, mais naît INACTIVE :
    rien ne se scrape tant qu'un admin ne l'a pas activée. Elle est vierge
    de toute annonce liée (create_search n'écrit rien dans search_listings).
    """
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search:
        flash("Recherche introuvable", "error")
        return redirect(url_for("admin.admin_searches"))

    copie = storage.searches.duplicate_search(search_id, f"Copie de {search['label']}")
    if not copie:
        # La ligne source a disparu entre la lecture et la copie.
        return _reponse_action(
            "Recherche introuvable — duplication impossible", "error",
            url_for("admin.admin_searches"), "searches",
            **_CONTEXTE_ONGLETS["searches"](storage),
        )

    storage.admin.log_admin_action(
        "search_duplicated",
        f"Search '{search['label']}' (ID:{search_id}) duplicated into"
        f" '{copie['label']}' (ID:{copie['id']}) by {g.user['username']}",
        g.user["username"],
    )
    return _reponse_action(
        f"Copie créée : « {copie['label']} » (inactive)", "success",
        url_for("admin.admin_search_detail", search_id=copie["id"]), "searches",
        **(_contexte_recherche_detail(storage, copie["id"]) or _CONTEXTE_ONGLETS["searches"](storage)),
    )


@admin_bp.route("/admin/searches/<int:search_id>/urls", methods=["GET"])
@require_admin
def admin_search_urls(search_id):
    """URLs natives construites par CHAQUE source de la recherche (issue #18).

    Construction pure (`build_search_urls`), SANS scrape : c'est exactement
    ce que le scraper visiterait, précieux pour diagnostiquer un scrape vide.

    Décision vis-à-vis de la route web `/searches/<id>/urls` (#30) : son
    contrat JSON + restriction propriétaire convient à l'espace utilisateur
    (fetch client) mais pas à l'admin, qui consulte des recherches d'AUTRES
    utilisateurs et veut un rendu HTML dans la fiche. Cet endpoint admin
    réutilise la même mécanique (`get_parser(...).build_search_urls(criteria
    normalisés)`), isolée par source : une source dont la construction échoue
    prive seulement sa propre ligne, jamais les autres. Rendu en fragment
    HTML, chargé à la demande depuis la fiche (pas d'appel géo réseau au
    simple chargement du détail).
    """
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    results = []
    if search:
        from parsers import get_parser

        criteria = search.get("criteria") or {}
        for source in search.get("sources") or [search.get("source", "seloger")]:
            try:
                parser = get_parser(source, storage=storage)
            except ValueError as e:
                results.append({"source": source, "name": source, "urls": [], "error": str(e), "note": None})
                continue
            try:
                urls = parser.build_search_urls(criteria)
                error = None if urls else "Pas d'URL reconstruisible pour cette source"
            except Exception as e:  # isolation par source, comme côté web
                logger.warning(f"[admin:{search_id}] URL non reconstructible ({source}): {e}")
                urls, error = [], f"URL non reconstructible : {e}"
            results.append({
                "source": source,
                "name": parser.SOURCE_NAME,
                "urls": urls,
                "error": error,
                "note": parser.URL_NOTE or None,
            })
    return render_template(
        "admin/_search_urls.html",
        urls_results=results,
        search_introuvable=search is None,
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
        ctx = _contexte_recherche_detail(storage, search_id) or _CONTEXTE_ONGLETS["searches"](storage)
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
    ctx = _contexte_recherche_detail(storage, search_id) or _CONTEXTE_ONGLETS["searches"](storage)
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


@admin_bp.route("/admin/listings/bulk-delete", methods=["POST"])
@require_admin
def admin_bulk_delete_listings():
    """Suppression GROUPÉE des annonces cochées (issue #20).

    Le repository reçoit la sélection brute et renvoie le nombre RÉELlement
    supprimé (IDs déjà disparus compris) : c'est CE compte qui est journalisé
    — un seul log récapitulatif `listings_bulk_deleted` portant LA LISTE des
    IDs demandés, jamais un log par ligne. Les filtres et la page courants
    voyagent en champs cachés (pattern #19) : le fragment reconstruit par
    `_contexte_annonces` (qui lit `request.values`) retombe exactement sur la
    vue d'où venait l'action.
    """
    storage = current_app.storage
    ids = [identifiant.strip() for identifiant in request.form.getlist("ids") if identifiant.strip()]

    supprimees = storage.listings.delete_listings(ids)
    storage.admin.log_admin_action(
        "listings_bulk_deleted",
        f"{supprimees} listing(s) deleted from a selection of {len(ids)}"
        f" [{', '.join(ids) or 'none'}] by {g.user['username']}",
        g.user["username"],
    )

    if not ids:
        message, categorie = "Aucune annonce sélectionnée", "info"
    elif supprimees == len(ids):
        message, categorie = f"{supprimees} annonce(s) supprimée(s)", "success"
    else:
        message = (
            f"{supprimees} annonce(s) supprimée(s) sur {len(ids)} sélectionnée(s)"
            " — les autres avaient déjà disparu"
        )
        categorie = "warning"

    return _reponse_action(
        message, categorie,
        url_for("admin.admin_listings"), "listings",
        **_CONTEXTE_ONGLETS["listings"](storage),
    )


def _cellule_publication(creation_date) -> str:
    """Date de PUBLICATION (#12) formatée pour l'export : vide si sentinelle.

    `creation_date` est du TEXT ISO-8601 UTC ou la sentinelle « unknown » ;
    une valeur non parsable reste brute plutôt que perdue.
    """
    if not creation_date or creation_date == DATE_INCONNUE:
        return ""
    try:
        return datetime.fromisoformat(str(creation_date).replace("Z", "+00:00")).strftime("%d/%m/%Y")
    except ValueError:
        return str(creation_date)


def _cellules_csv_annonce(li: dict) -> list[str]:
    """Une ligne d'annonce → les colonnes françaises de l'export (#20)."""
    premiere_detection = li.get("first_seen")
    return [
        str(li.get("listing_id") or ""),
        li.get("title") or "",
        li.get("source") or "",
        li.get("price") or "",
        li.get("surface") or "",
        li.get("rooms") or "",
        _cellule_publication(li.get("creation_date")),
        premiere_detection.strftime("%d/%m/%Y %H:%M") if premiere_detection else "",
        li.get("city") or "",
        li.get("url") or "",
    ]


@admin_bp.route("/admin/listings/export")
@require_admin
def admin_listings_export():
    """Export CSV du résultat filtré courant (issue #20).

    MÊMES filtres / recherche / tri que la vue (lus depuis request.args, sans
    pagination), plafonnés à EXPORT_CSV_PLAFOND lignes documenté. Format FR :
    séparateur « ; », CRLF, BOM UTF-8 pour qu'Excel ouvre le fichier sans
    mojibake. La réponse n'est PAS rendue via HTMX : un téléchargement ne swap
    rien, le navigateur gère l'attachment tout seul.
    """
    storage = current_app.storage
    search_term = request.args.get("search", "")
    source_filter = request.args.get("source", "")
    sort = request.args.get("sort", "")
    filtres, _erreurs = _lire_filtres_annonces()

    lignes = storage.listings.get_all_listings(
        limit=EXPORT_CSV_PLAFOND, offset=0, search_term=search_term,
        source_filter=source_filter, filters=filtres, sort=sort,
    )

    tampon = io.StringIO()
    writer = csv.writer(tampon, delimiter=";", lineterminator="\r\n")
    writer.writerow(COLONNES_CSV)
    for ligne in lignes:
        writer.writerow(_cellules_csv_annonce(ligne))

    nom_fichier = f"annonces_{datetime.now(UTC):%Y%m%d}.csv"
    return Response(
        "\ufeff" + tampon.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={nom_fichier}"},
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


def _jours_demandes(storage, cle: str, defaut: int) -> int:
    """Jours à appliquer à une purge : le champ explicite du formulaire gagne,
    sinon le réglage persisté relu À CHAQUE USAGE (pattern #17).

    Un `days` fourni mais illisible retombe sur le défaut historique
    (comportement `to_int` inchangé), pas sur le réglage : la demande
    explicite reste maîtresse.
    """
    brut = (request.form.get("days") or "").strip()
    if brut:
        return to_int(brut, defaut)
    return lire_jours(storage.settings.get_setting, cle, defaut)


@admin_bp.route("/admin/logs/purge", methods=["POST"])
@require_admin
def admin_purge_logs():
    storage = current_app.storage
    jours = _jours_demandes(storage, CLE_PURGE_LOGS_AUDIT, DEFAUT_PURGE_LOGS_AUDIT)
    deleted = storage.admin.purge_old_logs(days=jours)
    return _reponse_action(
        f"{deleted} ancien(s) log(s) supprimé(s)", "success",
        url_for("admin.admin_logs"), "logs", **_CONTEXTE_ONGLETS["logs"](storage),
    )


@admin_bp.route("/admin/cleanup", methods=["POST"])
@require_admin
def admin_cleanup():
    storage = current_app.storage
    jours = _jours_demandes(storage, CLE_RETENTION_ANNONCES, DEFAUT_RETENTION_ANNONCES)
    deleted = storage.listings.delete_old_listings(days=jours)
    storage.admin.log_admin_action(
        "cleanup_executed", f"{deleted} listings older than {jours} days deleted", g.user["username"]
    )
    return _reponse_action(
        f"{deleted} ancienne(s) annonce(s) supprimée(s)", "success",
        url_for("admin.admin"), "dashboard", **_CONTEXTE_ONGLETS["dashboard"](storage),
    )
