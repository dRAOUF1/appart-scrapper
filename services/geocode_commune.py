"""Fallback géocodage commune — geo.api.gouv.fr (issue #26).

Les sources vague 1 (bienici, ORPI, Foncia, essetpm) exposent leurs
coordonnées nativement ; mais une annonce peut rester sans position (le champ
est absent côté source). Plutôt que de la perdre pour la carte, ce service la
situe par le CENTRE DE SA COMMUNE :

    GET https://geo.api.gouv.fr/communes?codePostal=<cp>&fields=centre&format=json
    -> [{"nom": "Toulouse", "code": "31555",
         "centre": {"type": "Point", "coordinates": [1.4328, 43.6007]}}, ...]

(format vérifié sur une réponse réelle : `coordinates` est du GeoJSON —
LONGITUDE d'abord, latitude ensuite.)

C'est un REPLI assumé, jamais une vérité : tout le lot d'annonces d'une même
commune se partage le même point. D'où :
* précision 'commune', distincte de l'extraction native ('exacte' /
  'approximative') ;
* appelé AU SCRAPE seulement, best effort : timeout court, aucun échec ne
  remonte à l'appelant — un geo.api.gouv.fr indisponible ne doit jamais faire
  échouer un scrape qui a réussi ;
* cache en base (repositories/commune_geo_repo.py, pattern des caches géo),
  SUCCÈS SEULS mémorisés : un CP non résolu est re-tenté au scrape suivant,
  pas gelé ;
* les coordonnées passent par parsers._coords.extraire_coordonnees avant
  écriture : jamais de 0.0/0.0 stocké, même si l'API renvoyait des déchets.
"""

from __future__ import annotations

import requests
from loguru import logger

GEO_API_URL = "https://geo.api.gouv.fr/communes"

# L'app part du thread de scraping : 3 s suffisent largement pour une API
# publique française, et bornent le coût ajouté au pire cas d'indisponibilité.
_TIMEOUT_SECONDS = 3


def area_cache_key(listing) -> str | None:
    """La clé de cache du fallback pour une annonce : « postal:<cp> ».

    Le code postal est la seule localisation fiable dont dispose une annonce
    (les listings n'ont pas de code INSEE). Sans CP, il n'y a rien à chercher
    — et surtout pas de commune à deviner d'après le nom seul, plusieurs
    communes françaises portant le même nom.
    """
    zip_code = (getattr(listing, "zip_code", "") or "").strip()
    return f"postal:{zip_code}" if zip_code else None


def _pick_commune(communes: list[dict], city: str) -> dict | None:
    """La commune dont le centre sera utilisé : celle dont le `nom` correspond
    exactement à la ville de l'annonce quand elle est connue (un même CP peut
    couvrir plusieurs communes), sinon la première porteuse d'un centre."""
    wanted = (city or "").strip().casefold()
    if wanted:
        for commune in communes:
            if (commune.get("nom") or "").strip().casefold() == wanted:
                return commune
    return next((c for c in communes if c.get("centre")), None)


def _resolve_uncached(zip_code: str, city: str) -> tuple[float, float] | None:
    """Le centre (latitude, longitude) d'un code postal, sans cache.

    None sur toute réponse inattendue ou erreur réseau — jamais d'exception,
    l'appelant traite ça comme un simple échec non bloquant.
    """
    try:
        resp = requests.get(
            GEO_API_URL,
            params={"codePostal": zip_code, "fields": "centre", "format": "json"},
            timeout=_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        communes = resp.json()
        if not isinstance(communes, list):
            return None
        commune = _pick_commune(communes, city)
        coordinates = ((commune or {}).get("centre") or {}).get("coordinates")
        if not isinstance(coordinates, (list, tuple)) or len(coordinates) != 2:
            return None
        # GeoJSON : [longitude, latitude].
        lon, lat = coordinates

        from parsers._coords import extraire_coordonnees

        return extraire_coordonnees(lat, lon)
    except Exception as e:
        logger.debug(f"[geocode_commune] Résolution échouée pour {city} ({zip_code}) : {e}")
        return None


def completer_coordonnees_manquantes(listings, repo) -> int:
    """Complète EN PLACE les annonces restées sans coordonnées, par centre de
    commune. Retourne le nombre d'annonces complétées.

    `repo` (CommuneGeoRepository) est obligatoire et explicite : le scraping
    tourne sur un thread de fond, hors contexte d'application Flask — jamais
    lu depuis flask.current_app. Une résolution ratée laisse l'annonce telle
    quelle (valide, absente de la carte) et ne bloque jamais le scrape.
    """
    if not listings or repo is None:
        return 0

    from parsers._coords import PRECISION_COMMUNE

    # Un groupe par clé de cache : toutes les annonces d'un même CP partagent
    # UNE résolution (réseau au plus une fois par commune, et un seul point).
    groupes: dict[str, list] = {}
    for listing in listings:
        if listing.latitude is not None and listing.longitude is not None:
            continue
        key = area_cache_key(listing)
        if key:
            groupes.setdefault(key, []).append(listing)

    completes = 0
    for key, groupe in groupes.items():
        cached = repo.get_cached(key)
        if cached and cached["latitude"] is not None and cached["longitude"] is not None:
            coords = (cached["latitude"], cached["longitude"])
        else:
            first = groupe[0]
            zip_code = (first.zip_code or "").strip()
            coords = _resolve_uncached(zip_code, getattr(first, "city", "") or "")
            if not coords:
                logger.debug(f"[geocode_commune] Pas de centre pour {key} ({len(groupe)} annonce(s))")
                continue
            repo.set_cached(key, coords[0], coords[1])
            logger.info(f"[geocode_commune] {key} -> centre ({coords[0]:.4f}, {coords[1]:.4f})")

        for listing in groupe:
            listing.latitude, listing.longitude = coords
            listing.location_precision = PRECISION_COMMUNE
            completes += 1
    return completes
