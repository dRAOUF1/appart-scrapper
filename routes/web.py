"""Web Frontend Blueprint — user-facing routes (/)."""

from __future__ import annotations

import json
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from loguru import logger

from core.criteria import has_transit, location_label
from core.geocode import CITY
from core.schemas import validate_criteria, validate_scrape_interval
from core.web_utils import to_int
from parsers import list_sources, remember_manual_overrides
from parsers._coords import coordonnee_valide
from routes.auth import _reponse_lecture_seule, est_impersonation_active, require_login
from services import poi_overpass

web_bp = Blueprint(
    "web", __name__,
    template_folder="templates",
    static_folder="static",
)

# Méthodes HTTP considérées MUTANTES par le guard d'impersonation (#22).
# GET/HEAD/OPTIONS restent ouverts — c'est tout l'objet de la vue administrateur :
# voir le compte comme son propriétaire. Tout le reste est une écriture.
_METHODES_MUTANTES = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@web_bp.before_request
def refuser_mutations_en_impersonation():
    """GUARD SERVEUR de la vue administrateur lecture seule (issue #22).

    Choix d'implémentation, documenté : un hook `before_request` AU NIVEAU DU
    BLUEPRINT plutôt qu'un décorateur à recopier sur chaque route mutante. Un
    décorateur oublié sur UNE route est un trou silencieux ; ici, toute route
    future ajoutée à web_bp avec une méthode de `_METHODES_MUTANTES` est
    automatiquement bloquée en impersonation — l'oubli est structurellement
    impossible. Le balayage dynamique des tests (test_admin_impersonate.py)
    rejoue chaque règle mutante de l'url_map pour vérifier ce contrat.

    Le contrôle est côté SERVEUR, avant même la résolution de l'utilisateur :
    la bannière et les boutons masqués du front ne sont qu'un confort UI, jamais
    la protection. Réponse : 403 avec page française « Lecture seule ».
    """
    if request.method not in _METHODES_MUTANTES:
        return None
    if not est_impersonation_active():
        return None
    return _reponse_lecture_seule()


def _form_list(form_data: dict, key: str) -> list:
    """Les valeurs multiples d'un champ, que `form_data` soit un MultiDict
    Flask ou un simple dict (tests)."""
    if hasattr(form_data, "getlist"):
        return form_data.getlist(key)
    value = form_data.get(key, [])
    return value if isinstance(value, list) else [value]


_TYPED_POSTAL_CODE_RE = re.compile(r"\b(\d{5})\b")


def _location_from_free_text(text: str) -> dict | None:
    """Une saisie libre -> une commune, si elle contient de quoi la situer.

    Filet de sécurité pour une ligne remplie à la main sans passer par les
    suggestions : « Poitiers 86000 » ou « Poitiers (86000) » restent
    exploitables. Sans code postal, il n'y a rien à chercher — et surtout pas
    de commune à deviner, plusieurs pouvant porter le même nom.

    Une localisation obtenue ainsi n'a pas de code INSEE : les sources qui en
    ont besoin le diront (voir SeLogerParser.cannot_search_reason).
    """
    match = _TYPED_POSTAL_CODE_RE.search(text)
    if not match:
        return None
    city = _TYPED_POSTAL_CODE_RE.sub("", text).strip(" ()-—,").strip()
    if not city:
        return None
    return {"kind": CITY, "city": city, "postalCode": match.group(1)}


def _normalize_label_spacing(text: str) -> str:
    """Un libellé comparé à espaces superflus près : retours à la ligne,
    doubles espaces d'un copier-coller ne doivent pas faire échouer la
    correspondance avec une localisation déjà stockée."""
    return " ".join(text.split())


def _matching_stored_location(text: str, stored_locations: list[dict] | None) -> dict | None:
    """La localisation stockée dont le libellé est exactement `text`.

    Filet de l'issue #24 : en édition, un champ retouché cosmétiquement (ou un
    POST dont le payload caché a été perdu) réaffiche le libellé d'une
    localisation déjà enregistrée. Si ce texte est identique à son libellé,
    c'est la MÊME localisation : on la réutilise — avec son inseeCode — au lieu
    de l'abandonner en silence. Copie défensive : la recherche existante ne
    doit jamais partager ses dicts avec les nouveaux critères.
    """
    if not stored_locations:
        return None
    wanted = _normalize_label_spacing(text)
    for location in stored_locations:
        if _normalize_label_spacing(location_label(location)) == wanted:
            return dict(location)
    return None


def _parse_locations_from_form(
    form_data: dict, existing_locations: list[dict] | None = None
) -> tuple[list[dict], list[str]]:
    """Les périmètres de recherche saisis, un par ligne du formulaire.

    Chaque ligne n'a qu'un seul champ visible, doublé d'un champ caché
    `location_payload` que l'autocomplete remplit avec le périmètre choisi,
    sérialisé en JSON. C'est exactement une entrée de `locations` au format
    canonique (region, department, whole_city ou city selon ce qui a été
    choisi) : il n'y a donc ni champ par niveau, ni champ par attribut, et le
    code postal n'a pas à être ressaisi puisque la suggestion le porte déjà.

    Une ligne sans payload exploitable est résolue dans cet ordre :
    1. le texte comme libellé d'une localisation déjà stockée (édition :
       on réutilise celle-ci — inseeCode compris — plutôt que de perdre le
       périmètre, #24),
    2. le texte comme commune + code postal (_location_from_free_text).

    Retourne `(locations, textes_non_exploitables)` : les textes restés sans
    résolution sont remontés à la route pour un message explicite, jamais
    abandonnés en silence.
    """
    payloads = _form_list(form_data, "location_payload")
    typed = _form_list(form_data, "location_city")

    locations = []
    hand_typed_failures = []
    for index in range(max(len(payloads), len(typed))):
        payload = payloads[index].strip() if index < len(payloads) else ""
        if payload:
            try:
                parsed = json.loads(payload)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                # `label` n'est qu'un texte d'affichage : la normalisation
                # l'écarte, mais autant ne pas le transporter jusque-là.
                locations.append({k: v for k, v in parsed.items() if k != "label"})
                continue

        text = typed[index].strip() if index < len(typed) else ""
        if not text:
            continue
        # La correspondance stockée AVANT le repli libre : « Poitiers (86000) »
        # se laisse interpréter comme commune + code postal, mais si c'est le
        # libellé d'une localisation déjà enregistrée, celle-ci porte en plus
        # son inseeCode — la version la plus riche doit gagner (#24).
        location = _matching_stored_location(text, existing_locations)
        if location:
            locations.append(location)
            continue
        location = _location_from_free_text(text)
        if location:
            locations.append(location)
            continue
        hand_typed_failures.append(text)
    return locations, hand_typed_failures


def _parse_transit_from_form(form_data: dict) -> list[dict]:
    """Le champ caché `transit_payload` (issue #28) -> sélections brutes.

    Le payload est un JSON de liste ({mode, line_id, stop_ids[], radius_m}),
    écrit par static/search_form.js et re-normalisé par le serveur
    (normalize_criteria) : le front ne fait jamais autorité sur la validité.

    Un payload CORROMPU (JSON illisible, forme inattendue) est une erreur
    explicite en français — jamais un crash ni une donnée abandonnée en
    silence. Une liste valide mais contenant des entrées boiteuses reste
    acceptée : la normalisation écarte ce qui est inexploitable.
    """
    raw = (form_data.get("transit_payload") or "").strip() if hasattr(form_data, "get") else ""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise ValueError(
            "Le bloc Transports contient des données corrompues : "
            "dépliez-le et re-sélectionnez vos lignes avant d'enregistrer."
        ) from e
    if not isinstance(parsed, list):
        raise ValueError(
            "Le bloc Transports est invalide (liste attendue) : "
            "re-sélectionnez vos lignes avant d'enregistrer."
        )
    return parsed


def _parse_search_criteria_from_form(
    form_data: dict, existing_locations: list[dict] | None = None
) -> tuple[dict, list[str]]:
    """Construit des critères au vocabulaire canonique depuis le formulaire.

    Un seul formulaire pour toutes les sources : l'utilisateur décrit ce
    qu'il cherche (où, quoi, quel budget), et c'est chaque parser qui traduit
    ensuite ces critères vers le format de sa source (voir
    BaseParser.to_native). Le formulaire ne connaît donc le vocabulaire
    d'aucune source en particulier.

    `existing_locations` (édition) permet de réutiliser une localisation déjà
    stockée quand sa ligne revient sans payload mais avec son libellé exact —
    voir _parse_locations_from_form.

    Issue #28 : le champ caché transit_payload est parsé ici aussi (voir
    _parse_transit_from_form). Un payload corrompu lève ValueError — les
    routes affichent ce message, jamais un crash.

    Retourne `(critères, textes_non_exploitables)`, les seconds alimentant un
    message d'erreur dédié dans la route (_location_error_message).

    Seule exception assumée au vocabulaire canonique : le champ de saisie
    libre qu'une source peut déclarer comme repli (`override_<source>`, voir
    BaseParser.MANUAL_OVERRIDE_LABEL). Ce qui y est saisi est rangé dans
    `sourceOverrides`, jamais mélangé aux critères.
    """
    criteria: dict = {}

    locations, hand_typed_failures = _parse_locations_from_form(form_data, existing_locations)
    if locations:
        criteria["locations"] = locations

    transit = _parse_transit_from_form(form_data)
    if transit:
        criteria["transit"] = transit

    criteria["transaction"] = form_data.get("transaction", "rent")
    property_types = _form_list(form_data, "property_types")
    if property_types:
        criteria["propertyTypes"] = property_types

    for key, field in (
        ("priceMin", "price_min"),
        ("priceMax", "price_max"),
        ("surfaceMin", "surface_min"),
        ("surfaceMax", "surface_max"),
    ):
        value = form_data.get(field, "").strip()
        if value:
            criteria[key] = value

    for key in ("rooms", "bedrooms"):
        values = _form_list(form_data, key)
        if values:
            criteria[key] = values

    overrides = _parse_source_overrides_from_form(form_data)
    if overrides:
        criteria["sourceOverrides"] = overrides

    # La validation précède la normalisation afin qu'une valeur fournie mais
    # invalide ne disparaisse jamais silencieusement du formulaire.
    return validate_criteria(criteria), hand_typed_failures


def _submitted_form_values(form_data) -> dict[str, list[str]]:
    """Copie sérialisable de la soumission, valeurs multiples comprises.

    Contrat de vue : après une erreur, les templates utilisent `form_values`
    pour rendre exactement ce que l'utilisateur vient d'envoyer (sources,
    compteurs et localisations inclus), sans relire l'ancienne recherche.
    """
    if hasattr(form_data, "to_dict"):
        values = form_data.to_dict(flat=False)
    else:
        values = {
            key: list(value) if isinstance(value, list) else [value]
            for key, value in form_data.items()
        }

    # Les clients normaux postent le libellé visible et son payload caché.
    # Les clients programmatiques peuvent n'envoyer que le payload : dériver
    # alors le libellé évite une ligne visuellement vide tout en conservant le
    # payload canonique, notamment son inseeCode.
    payloads = values.get("location_payload", [])
    if payloads and not values.get("location_city"):
        cities = []
        for payload in payloads:
            try:
                location = json.loads(payload)
            except (TypeError, ValueError):
                location = None
            cities.append(location_label(location) if isinstance(location, dict) else "")
        values["location_city"] = cities
    return values


def _scrape_stats_for_view(storage, search_id: int) -> dict:
    """Statistiques au contrat complet attendu par toutes les vues."""
    stats = dict(storage.scrape_logs.get_scrape_stats(search_id) or {})
    stats.setdefault("partial_count", 0)
    return stats


def _location_error_message(criteria: dict, hand_typed_failures: list[str]) -> str | None:
    """Le message dédié à ce qui n'a pas pu être résolu comme localisation.

    Deux cas bien distincts au lieu d'un « aucune localisation exploitable
    (ville + code postal requis) » par source, trompeur quand les champs
    s'affichent remplis (#24) :
    - un texte saisi à la main que ni le payload ni une localisation stockée
      n'expliquent -> dire de choisir dans les suggestions ;
    - aucune localisation du tout -> le dire tel quel.
    """
    if hand_typed_failures:
        if len(hand_typed_failures) == 1:
            return (
                f"Localisation « {hand_typed_failures[0]} » saisie à la main non exploitable"
                " — choisissez-la dans les suggestions"
            )
        quoted = ", ".join(f"« {text} »" for text in hand_typed_failures)
        return f"Localisations {quoted} saisies à la main non exploitables — choisissez-les dans les suggestions"
    if not criteria.get("locations") and not has_transit(criteria):
        # Issue #28 : une recherche « transit-seule » est valide (l'expansion
        # produira ses localisations au scrape) — elle ne déclenche pas
        # l'exigence de ville.
        return "Aucune localisation renseignée"
    return None


def _parse_source_overrides_from_form(form_data: dict) -> dict:
    """Les surcharges manuelles saisies, par source qui en propose une."""
    from parsers import get_parser

    overrides = {}
    for source in list_sources():
        if not source["manual_override_label"]:
            continue
        raw = form_data.get(f"override_{source['id']}", "").strip()
        if not raw:
            continue
        parsed = get_parser(source["id"]).parse_manual_override(raw)
        if parsed:
            overrides[source["id"]] = parsed
    return overrides


def _parse_notify_enabled_from_form(form_data: dict) -> bool:
    """Le flag de notifications ntfy (#10) depuis le formulaire.

    Une case à cocher HTML décochée n'est PAS envoyée : sans champ marqueur,
    impossible de distinguer « l'utilisateur a décoché » de « champ absent ».
    Les templates postent donc toujours `notify_enabled_present` quand ils
    rendent la case. En l'absence du marqueur (POST programmatique, client qui
    ne connaît pas le flag), on retombe sur le défaut rétrocompatible :
    notifications activées — une création sans mention du flag vaut True.
    """
    if "notify_enabled_present" not in form_data:
        return True
    return "notify_enabled" in form_data


def _validate_sources_criteria(sources: list[str], criteria: dict) -> list[dict]:
    """Per-source validity of `criteria`, with a precise, actionable reason
    when a selected source can't run — instead of one generic "invalid
    criteria" message covering every source indiscriminately.

    La raison vient de la source elle-même (BaseParser.cannot_search_reason) :
    lieu inexploitable, ou critère qu'elle ne sait pas honorer.
    """
    from parsers import get_parser

    results = []
    for src in sources:
        try:
            parser = get_parser(src)
        except ValueError:
            results.append({"id": src, "name": src, "ok": False, "reason": "Source inconnue"})
            continue

        reason = parser.cannot_search_reason(criteria)
        results.append({
            "id": src,
            "name": parser.SOURCE_NAME,
            "ok": reason is None,
            "reason": reason or "",
        })
    return results


def _validation_error_message(results: list[dict]) -> str:
    """Format a precise, per-source error message from _validate_sources_criteria().

    A source's own reason (e.g. SeLoger's EXTRA_LOCATION_HELP) may already
    name that source — avoid an awkward "SeLoger : SeLoger ne peut pas...".
    """
    failing = [r for r in results if not r["ok"]]
    parts = []
    for r in failing:
        if r["reason"] and r["name"] in r["reason"]:
            parts.append(r["reason"])
        else:
            parts.append(f"{r['name']} : {r['reason']}")
    return " / ".join(parts)


def _submit_scrape(search_id: int, user_id: int):
    """Submit a scrape job, checking for existing futures."""
    from core.scrape_control import submit_scrape
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
                flash("Compte créé !", "success")
            except ValueError:
                flash("Erreur lors de la création du compte", "error")
                return render_template("login.html")

        session["user_id"] = user["id"]
        session["username"] = user["username"]
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
        now=datetime.utcnow,
    )


@web_bp.route("/searches", methods=["GET", "POST"])
@require_login
def searches():
    sources = list_sources()
    if request.method == "POST":
        label = request.form.get("label", "").strip()
        ntfy_topic = request.form.get("ntfy_topic", "").strip()
        selected_sources = request.form.getlist("sources") or [request.form.get("source", "seloger").strip()]

        try:
            scrape_interval = validate_scrape_interval(request.form.get("scrape_interval", 5))
            criteria, hand_typed_failures = _parse_search_criteria_from_form(request.form)
        except ValueError as e:
            flash(str(e), "error")
            all_searches = current_app.storage.searches.get_user_searches(g.user["id"])
            return render_template(
                "searches.html",
                searches=all_searches,
                base_url=request.url_root.rstrip("/"),
                sources=sources,
                form_values=_submitted_form_values(request.form),
                now=datetime.utcnow,
            )

        location_error = _location_error_message(criteria, hand_typed_failures)
        if location_error:
            flash(location_error, "error")
            all_searches = current_app.storage.searches.get_user_searches(g.user["id"])
            return render_template(
                "searches.html", searches=all_searches, base_url=request.url_root.rstrip("/"), sources=sources,
                form_values=_submitted_form_values(request.form), now=datetime.utcnow,
            )

        validation = _validate_sources_criteria(selected_sources, criteria)
        if not all(r["ok"] for r in validation):
            flash(_validation_error_message(validation), "error")
            all_searches = current_app.storage.searches.get_user_searches(g.user["id"])
            return render_template(
                "searches.html", searches=all_searches, base_url=request.url_root.rstrip("/"), sources=sources,
                form_values=_submitted_form_values(request.form), now=datetime.utcnow,
            )

        if label and ntfy_topic:
            current_app.storage.searches.create_search(
                g.user["id"], label, ntfy_topic, selected_sources[0], criteria, scrape_interval,
                sources=selected_sources,
                notify_enabled=_parse_notify_enabled_from_form(request.form),
            )
            remember_manual_overrides(selected_sources, criteria, storage=current_app.storage)
            flash(f"Recherche « {label} » créée !", "success")
        else:
            flash("Label et topic ntfy requis", "error")
        if label and ntfy_topic:
            return redirect(url_for("web.searches"))
        all_searches = current_app.storage.searches.get_user_searches(g.user["id"])
        return render_template(
            "searches.html", searches=all_searches, base_url=request.url_root.rstrip("/"), sources=sources,
            form_values=_submitted_form_values(request.form), now=datetime.utcnow,
        )

    all_searches = current_app.storage.searches.get_user_searches(g.user["id"])
    base_url = request.url_root.rstrip("/")
    return render_template(
        "searches.html",
        searches=all_searches,
        base_url=base_url,
        sources=sources,
        form_values=None,
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


@web_bp.route("/searches/<int:search_id>/urls")
@require_login
def search_urls_web(search_id: int):
    """URLs de recherche reconstruites pour chaque source de la recherche.

    Reprise de l'ex-endpoint API `/api/searches/<id>/urls`, supprimé avec le
    token (issue #30) : le modal « Voir l'URL » de la page Recherches l'appelle
    désormais en fetch authentifié par le cookie de session.
    """
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404

    sources = search.get("sources") or [search.get("source", "seloger")]
    criteria = search.get("criteria", {})

    from parsers import get_parser
    results = []
    for source in sources:
        try:
            parser = get_parser(source, storage=current_app.storage)
        except ValueError as e:
            results.append({"source": source, "url": None, "error": str(e)})
            continue
        # Reconstruire une URL peut demander un appel réseau (résolution du
        # lieu propre à la source) et échouer : une erreur ici ne doit pas
        # renvoyer un 500 pour toute la page, seulement priver cette source
        # de son lien.
        try:
            urls = parser.build_search_urls(criteria)
            error = None if urls else "URL reconstruction non disponible pour cette source"
        except Exception as e:
            logger.warning(f"[search:{search_id}] URL non reconstructible ({source}): {e}")
            urls, error = [], f"URL non reconstructible : {e}"

        results.append({
            "source": source,
            "url": urls[0] if urls else None,
            "urls": urls,
            "source_name": parser.SOURCE_NAME,
            "error": error,
            "note": parser.URL_NOTE or None,
        })

    # Les champs de premier niveau reflètent la première source (comportement
    # hérité de l'API, conservé pour le front).
    first = results[0] if results else {}
    return jsonify({
        "source": first.get("source"),
        "url": first.get("url"),
        "source_name": first.get("source_name"),
        "sources": results,
    }), 200


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


@web_bp.route("/searches/<int:search_id>/toggle-notify", methods=["POST"])
@require_login
def toggle_search_notify_web(search_id: int):
    """Bascule rapide des notifications ntfy (#10) depuis la liste.

    Le scraping n'est pas suspendu : seul l'envoi ntfy est piloté ici. Les
    annonces trouvées pendant la période désactivée sont marquées traitées
    par le service — la réactivation ne provoque jamais de salve rétrospective.
    """
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))
    new_value = storage.searches.toggle_search_notifications(search_id)
    if new_value is not None:
        flash(
            "Notifications activées" if new_value else "Notifications désactivées",
            "success",
        )
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
        selected_sources = request.form.getlist("sources") or search.get("sources") or [search.get("source", "seloger")]

        # Filet #24 : une ligne dont le payload caché a été perdu mais dont le
        # texte est le libellé exact d'une localisation déjà enregistrée
        # réutilise celle-ci (inseeCode compris) au lieu d'être abandonnée.
        existing_locations = (search.get("criteria") or {}).get("locations") or []
        try:
            scrape_interval = validate_scrape_interval(request.form.get("scrape_interval", 5))
            criteria, hand_typed_failures = _parse_search_criteria_from_form(
                request.form, existing_locations
            )
        except ValueError as e:
            flash(str(e), "error")
            stats = _scrape_stats_for_view(storage, search_id)
            return render_template(
                "search_edit.html", search=search, stats=stats, sources=list_sources(),
                form_values=_submitted_form_values(request.form), now=datetime.utcnow,
            )

        location_error = _location_error_message(criteria, hand_typed_failures)
        if location_error:
            flash(location_error, "error")
            stats = _scrape_stats_for_view(storage, search_id)
            return render_template(
                "search_edit.html", search=search, stats=stats, sources=list_sources(),
                form_values=_submitted_form_values(request.form), now=datetime.utcnow,
            )

        validation = _validate_sources_criteria(selected_sources, criteria)
        if not all(r["ok"] for r in validation):
            flash(_validation_error_message(validation), "error")
            stats = _scrape_stats_for_view(storage, search_id)
            return render_template(
                "search_edit.html", search=search, stats=stats, sources=list_sources(),
                form_values=_submitted_form_values(request.form), now=datetime.utcnow,
            )

        if label and ntfy_topic:
            storage.searches.update_search(
                search_id, g.user["id"],
                label=label, ntfy_topic=ntfy_topic,
                criteria=criteria, scrape_interval=scrape_interval,
                sources=selected_sources,
                notify_enabled=_parse_notify_enabled_from_form(request.form),
            )
            remember_manual_overrides(selected_sources, criteria, storage=current_app.storage)
            flash("Recherche mise à jour !", "success")
            return redirect(url_for("web.searches"))
        flash("Label et topic ntfy requis", "error")
        stats = _scrape_stats_for_view(storage, search_id)
        return render_template(
            "search_edit.html", search=search, stats=stats, sources=list_sources(),
            form_values=_submitted_form_values(request.form), now=datetime.utcnow,
        )

    stats = _scrape_stats_for_view(storage, search_id)
    return render_template(
        "search_edit.html", search=search, stats=stats, sources=list_sources(), form_values=None, now=datetime.utcnow,
    )


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
    stats = _scrape_stats_for_view(storage, search_id)
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
    from scrape_logs.manager import SearchLogManager
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
    from scrape_logs.manager import SearchLogManager
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


_LISTINGS_PER_PAGE = 20
_DEFAULT_LISTINGS_SORT = "found_at_desc"


def _fetch_listing_page(storage, search: dict, args) -> tuple[list, int, int]:
    """Une tranche d'annonces pour une recherche : `(annonces, total, page)`.

    Filtres, tri et blacklist agences sont reconstruits depuis `args` (la
    querystring de la requête). La vue initiale ET les tranches du scroll
    infini (#14) passent par ici : c'est ce qui garantit que le fragment
    renvoyé par `/listings/<id>/page` montre exactement ce que la page
    complète aurait montré au même endroit — mêmes filtres, même tri,
    même exclusion des agences blacklistées en mode « exclude ».
    """
    blacklisted = search.get("blacklisted_agencies") or []
    blacklist_mode = search.get("blacklist_mode", "exclude")

    agencies_to_filter = []
    if blacklist_mode == "exclude" and blacklisted:
        agencies_to_filter = blacklisted

    filters = _parse_listing_filters(args)
    sort = args.get("sort", _DEFAULT_LISTINGS_SORT)
    page = to_int(args.get("page", 1), 1)
    offset = (page - 1) * _LISTINGS_PER_PAGE

    rows = storage.listings.get_listings_for_search(
        search["id"], limit=_LISTINGS_PER_PAGE, offset=offset,
        blacklisted_agencies=agencies_to_filter,
        filters=filters, sort=sort,
    )
    total = storage.listings.count_listings_for_search(
        search["id"], blacklisted_agencies=agencies_to_filter,
        filters=filters,
    )
    return rows, total, page


@web_bp.route("/listings/<int:search_id>")
@require_login
def listings(search_id: int):
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        flash("Recherche introuvable", "error")
        return redirect(url_for("web.searches"))

    all_listings, total, page = _fetch_listing_page(storage, search, request.args)
    total_pages = max(1, (total + _LISTINGS_PER_PAGE - 1) // _LISTINGS_PER_PAGE)
    has_more = page < total_pages

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
        has_more=has_more,
        available_agencies=available_agencies,
        blacklist_mode=search.get("blacklist_mode", "exclude"),
        filter_options=filter_options,
        active_filters=active_filters,
        sort=request.args.get("sort", _DEFAULT_LISTINGS_SORT),
        query_string=query_string,
    )


@web_bp.route("/listings/<int:search_id>/page")
@require_login
def listings_page(search_id: int):
    """Tranche d'annonces du scroll infini (#14) — fragment HTML.

    Rend EXACTEMENT les mêmes cartes que la vue initiale (même partial
    `_listings_slice.html`, donc même macro de dates #12), pour la page
    demandée et avec les mêmes filtres. Le client insère la tranche dans la
    grille quand il approche du bas de liste ; l'état « fin de liste » est
    porté par `data-end-of-list` sur le conteneur de la tranche.

    Contrat d'erreur :
    - recherche inexistante ou d'un autre utilisateur -> 404 (jamais le
      contenu d'autrui, même en fragment) ;
    - page absente, illisible ou hors plage -> 400 : le client connaît
      `data-total-pages` et ne doit jamais demander au-delà, ce contrôle est
      le filet défensif côté serveur.
    """
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404

    # Le client connaît toujours le numéro de la tranche voulue : une requête
    # sans `page`, illisible ou sous 1 n'a pas de sens contractuel -> 400.
    if to_int(request.args.get("page", ""), 0) < 1:
        return jsonify({"error": "Page invalide"}), 400

    rows, total, page = _fetch_listing_page(storage, search, request.args)
    total_pages = max(1, (total + _LISTINGS_PER_PAGE - 1) // _LISTINGS_PER_PAGE)

    if page > total_pages:
        return jsonify({"error": "Page hors plage"}), 400

    return render_template(
        "_listings_slice.html",
        search=search,
        listings=rows,
        page=page,
        total_pages=total_pages,
        has_more=page < total_pages,
    )


@web_bp.route("/health")
def health():
    return "OK", 200


# ---------------------------------------------------------------------------
# Carte d'une recherche et repères personnels (issue #26)
#
# Routes de SESSION web (web_bp + @require_login) — PAS d'API token : le
# /api/* piloté par script a été supprimé (#30). Les mutations passent par la
# protection CSRF globale de l'app ; la validation des coordonnées réutilise
# parsers._coords.coordonnee_valide — un pin à 999 ou (0, 0) n'existe jamais,
# côté base ET côté route.
# ---------------------------------------------------------------------------

_PIN_ICONS = ("📍", "🏠", "⭐", "🚉", "🏫", "🛒")
_PIN_LABEL_MAX = 80
_PIN_NOTE_MAX = 500


def _request_payload() -> dict:
    """Le corps de la requête, formulaire OU JSON — le client JS envoie du
    FormData (pour passer CSRF naturellement), mais un appelant JSON reste
    toléré."""
    if request.form:
        return dict(request.form.items())
    json_body = request.get_json(silent=True)
    return json_body if isinstance(json_body, dict) else {}


def _pin_creation_payload() -> tuple[dict, str | None]:
    """Valide le payload de création d'un repère. `(champs, erreur)`.

    Toute valeur invalide est une erreur EXPLICITE (jamais une correction
    silencieuse) : latitude/longitude illisibles, nulles (sentinelle « pas de
    position ») ou hors bornes sont refusées, l'icône doit appartenir à la
    liste proposée par l'UI.
    """
    data = _request_payload()

    label = str(data.get("label") or "").strip()
    if not label:
        return {}, "Le libellé du repère est obligatoire"
    if len(label) > _PIN_LABEL_MAX:
        return {}, f"Le libellé dépasse {_PIN_LABEL_MAX} caractères"

    note = str(data.get("note") or "").strip()
    if len(note) > _PIN_NOTE_MAX:
        return {}, f"La note dépasse {_PIN_NOTE_MAX} caractères"

    icon = str(data.get("icon") or "📍").strip() or "📍"
    if icon not in _PIN_ICONS:
        return {}, "Icône inconnue"

    latitude = coordonnee_valide(data.get("latitude"), min_bound=-90.0, max_bound=90.0)
    if latitude is None:
        return {}, "Latitude invalide (nombre entre -90 et 90, non nulle)"
    longitude = coordonnee_valide(data.get("longitude"), min_bound=-180.0, max_bound=180.0)
    if longitude is None:
        return {}, "Longitude invalide (nombre entre -180 et 180, non nulle)"

    fields = {"label": label, "note": note, "icon": icon, "latitude": latitude, "longitude": longitude}
    return fields, None


def _owned_pin_or_none(storage, pin_id: int):
    """Le repère de CET utilisateur — None sinon : la route répond 404 sans
    révéler qu'une ligne d'un autre utilisateur existe (balayage IDOR)."""
    return storage.map_pins.get_owned(g.user["id"], pin_id)


@web_bp.route("/listings/<int:search_id>/map-data")
@require_login
def listings_map_data(search_id: int):
    """Données carte d'une recherche : ses biens géolocalisés + les repères
    personnels (GLOBAUX) de l'utilisateur connecté.

    Contrat d'autorisation identique à /listings/<id> : recherche inexistante
    ou d'un autre utilisateur -> 404, jamais les données d'autrui.
    """
    storage = current_app.storage
    search = storage.searches.get_search(search_id)
    if not search or search["user_id"] != g.user["id"]:
        return jsonify({"error": "Recherche introuvable"}), 404

    points = storage.listings.get_map_points_for_search(search_id)
    pins = storage.map_pins.list_for_user(g.user["id"])
    return jsonify({"points": points, "pins": pins})


@web_bp.route("/pins", methods=["POST"])
@require_login
def create_pin():
    """Crée un repère personnel pour l'utilisateur connecté."""
    fields, error = _pin_creation_payload()
    if error:
        return jsonify({"error": error}), 400
    pin = current_app.storage.map_pins.create(user_id=g.user["id"], **fields)
    return jsonify(pin), 201


@web_bp.route("/pins/<int:pin_id>", methods=["PATCH"])
@require_login
def update_pin(pin_id: int):
    """Renomme/reprécise un repère APPARTENANT à l'utilisateur (label, note,
    icône) — la position est fixe après création."""
    data = _request_payload()
    fields: dict = {}
    if "label" in data:
        label = str(data.get("label") or "").strip()
        if not label or len(label) > _PIN_LABEL_MAX:
            return jsonify({"error": "Libellé obligatoire (80 caractères max)"}), 400
        fields["label"] = label
    if "note" in data:
        note = str(data.get("note") or "").strip()
        if len(note) > _PIN_NOTE_MAX:
            return jsonify({"error": f"La note dépasse {_PIN_NOTE_MAX} caractères"}), 400
        fields["note"] = note
    if "icon" in data:
        icon = str(data.get("icon") or "").strip()
        if icon not in _PIN_ICONS:
            return jsonify({"error": "Icône inconnue"}), 400
        fields["icon"] = icon
    if not fields:
        return jsonify({"error": "Aucun champ modifiable fourni"}), 400

    storage = current_app.storage
    if _owned_pin_or_none(storage, pin_id) is None:
        return jsonify({"error": "Repère introuvable"}), 404

    pin = storage.map_pins.update(g.user["id"], pin_id, fields)
    if pin is None:
        return jsonify({"error": "Repère introuvable"}), 404
    return jsonify(pin)


@web_bp.route("/pins/<int:pin_id>", methods=["DELETE"])
@require_login
def delete_pin(pin_id: int):
    """Supprime un repère APPARTENANT à l'utilisateur — 404 sinon."""
    deleted = current_app.storage.map_pins.delete(g.user["id"], pin_id)
    if not deleted:
        return jsonify({"error": "Repère introuvable"}), 404
    return jsonify({"status": "supprimé"})


# ---------------------------------------------------------------------------
# Couches POI contextuelles de la carte (issue #27)
#
# Proxy serveur vers Overpass (OpenStreetMap) : pas de CORS, cache par bbox,
# timeout court. Session-only comme le reste du web_bp. Aucun échec upstream
# ne devient un 500 : la couche revient vide avec `degrade=True`, la carte
# reste utilisable sans la couche.
# ---------------------------------------------------------------------------


@web_bp.route("/listings/poi")
@require_login
def poi_catalogue():
    """Catalogue des couches POI — le front construit son contrôle Calques
    depuis ce JSON : ajouter une couche côté serveur ne demande RIEN d'autre."""
    couches = [
        {"id": identifiant, "label": couche["label"], "icone": couche["icone"]}
        for identifiant, couche in poi_overpass.COUCHES_POI.items()
    ]
    return jsonify({"couches": couches})


@web_bp.route("/listings/poi/<couche>")
@require_login
def poi_couche(couche: str):
    """Points d'une couche dans la bbox visible (`bbox=sud,ouest,nord,est`).

    bbox invalide -> 400 explicite ; couche inconnue -> 404 ; échec Overpass
    -> 200 avec `points: []` et `degrade: true` (jamais d'erreur interne pour
    un problème chez le fournisseur).
    """
    if couche not in poi_overpass.COUCHES_POI:
        return jsonify({"error": "Couche inconnue"}), 404

    bbox = poi_overpass.parse_bbox(request.args.get("bbox"))
    if bbox is None:
        return jsonify(
            {"error": "bbox invalide : attendu sud,ouest,nord,est "
                      f"(floats ordonnés, amplitude ≤ {poi_overpass.AMPLITUDE_MAX_DEGRES}°)"}
        ), 400

    points, degrade = poi_overpass.recuperer_poi(couche, bbox)
    return jsonify({"couche": couche, "degrade": degrade, "points": points})


@web_bp.route("/cleanup", methods=["POST"])
@require_login
def cleanup():
    days = to_int(request.form.get("days", 4), 4)
    deleted = current_app.storage.listings.delete_old_listings(days=days)
    flash(f"{deleted} ancienne(s) annonce(s) supprimée(s)", "success")
    return redirect(url_for("web.dashboard"))


# ---------------------------------------------------------------------------
# Autocomplete des transports en commun (issue #28)
#
# Session-only (@require_login) : ces endpoints lisent NOTRE référentiel GTFS,
# consommés uniquement par le formulaire connecté — même philosophie que les
# routes web depuis la suppression de l'API à token (#30). Contrat JSON
# {items: [{id, label}...]} comme l'autocomplete de localisation.
# ---------------------------------------------------------------------------

_TRANSIT_MODE_LABELS = {
    "tram": "Tram",
    "metro": "Métro",
    "rer": "RER",
    "train": "Train",
}
# L'autocomplete de lignes n'affiche qu'une liste courte (typeahead) ; les
# STATIONS, elles, alimentent des cases à cocher et doivent être complètes :
# cap défensif à 100 (la plus longue ligne ferrée francilienne en compte ~50).
_TRANSIT_LINES_LIMIT = 10
_TRANSIT_STOPS_LIMIT = 100


def _ligne_label(ligne: dict) -> str:
    """« Métro 14 · Saint-Denis – Orly », prêt pour un <option>."""
    mode = _TRANSIT_MODE_LABELS.get(ligne.get("mode") or "", "")
    code = ligne.get("code_ligne") or ""
    nom = ligne.get("nom_ligne") or ""
    titre = f"{mode} {code}".strip()
    return f"{titre} · {nom}" if nom and nom != code else titre


@web_bp.route("/locations/transit/lines")
@require_login
def transit_lines_autocomplete():
    """Lignes ferrées dont le code ou le nom contient `q`, triées par mode
    puis par code. `mode` optionnel filtre le type."""
    from core.criteria import TRANSIT_MODES

    query = request.args.get("q", "").strip()
    mode = request.args.get("mode", "").strip() or None
    if mode not in TRANSIT_MODES:
        mode = None
    if not query:
        return jsonify({"items": []})
    lignes = current_app.storage.transit.search_lines(query, mode=mode, limit=_TRANSIT_LINES_LIMIT)
    return jsonify({"items": [{"id": ligne["id"], "label": _ligne_label(ligne)} for ligne in lignes]})


@web_bp.route("/locations/transit/stops")
@require_login
def transit_stops_autocomplete():
    """Toutes les stations d'une ligne, alphabétique — la liste COMPLÈTE est
    nécessaire au multi-select (« toute la ligne » si rien n'est coché)."""
    line_id = request.args.get("line", "").strip()
    if not line_id:
        return jsonify({"items": []})
    stations = current_app.storage.transit.get_line_stops(line_id)[:_TRANSIT_STOPS_LIMIT]
    return jsonify({"items": [{"id": station["id"], "label": station["nom"]} for station in stations]})
