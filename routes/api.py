"""API Blueprint — réduite à l'autocomplete interne de localisation.

L'API JSON publique (utilisateurs, recherches, annonces, stats, logs), authentifiée
par X-API-Token, a été supprimée (issue #30) : l'interface web couvre déjà tous ces
usages via les sessions Flask, et cette surface exposait un secret en clair dans
l'UI et permettait de créer un compte sans authentification. Seule subsiste
l'autocomplete consommée par le formulaire (static/search_form.js).
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from core.web_utils import to_int

api_bp = Blueprint("api", __name__)


@api_bp.route("/locations", methods=["GET"])
def search_locations_endpoint():
    """Autocomplete de localisation, commun à toutes les sources.

    Renvoie des localisations canoniques (ville, code postal, code INSEE,
    coordonnées) : c'est la seule saisie de lieu du formulaire, et le code
    INSEE qu'elle fournit est ce qui permet ensuite à chaque source de
    retrouver son propre identifiant de lieu.
    """
    from core.geocode import search_locations

    query = request.args.get("q", "")
    limit = to_int(request.args.get("limit", 10), 10)
    return jsonify(search_locations(query, limit=max(1, min(limit, 20)))), 200
