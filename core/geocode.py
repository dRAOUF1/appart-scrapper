"""Résolution géographique partagée (code INSEE, autocomplete ville).

Toutes les sources sont censées passer par ici plutôt que d'appeler
geo.api.gouv.fr chacune de leur côté — un seul point de vérité pour la
manière dont "ville + code postal" se traduit en code INSEE.
"""

from __future__ import annotations

import requests
from loguru import logger

COMMUNES_API = "https://geo.api.gouv.fr/communes"

# INSEE code lookups never change during a process's life — cache them so a
# search scraped every few minutes forever doesn't hit the public geo API
# on every single run.
_INSEE_CACHE: dict[str, str | None] = {}


def _arrondissement_insee_code(postal_code: str) -> str | None:
    """Paris/Lyon/Marseille arrondissements: INSEE's `/communes` API only
    tracks these at the whole-city level (75056/69123/13055), but per-
    arrondissement filtering (e.g. Laforet's filter[cities][]) needs the
    arrondissement-specific "commune associée" code. Formulas verified
    against Laforet's own embedded page state for several arrondissements
    of each city (75014->75114, 69007->69387, 13001->13201, etc.) — not
    guessed, checked against real values Laforet itself computes for its
    own default single-arrondissement pages.
    """
    if len(postal_code) != 5 or not postal_code.isdigit():
        return None
    if postal_code.startswith("75"):
        arr = int(postal_code[-2:])
        if 1 <= arr <= 20:
            return f"751{arr:02d}"
    elif postal_code.startswith("690"):
        arr = int(postal_code[-1])
        if 1 <= arr <= 9:
            return f"693{80 + arr}"
    elif postal_code.startswith("130"):
        arr = int(postal_code[-2:])
        if 1 <= arr <= 16:
            return f"132{arr:02d}"
    return None


def _lookup_insee_code(postal_code: str) -> str | None:
    """Resolve any other French postal code via the official, free, public
    geo.api.gouv.fr API (no key, no auth) — the same API Laforet's own city
    autocomplete calls (verified live via network capture)."""
    try:
        resp = requests.get(
            COMMUNES_API,
            params={"codePostal": postal_code, "fields": "code"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data[0]["code"] if data else None
    except Exception as e:
        logger.warning(f"[geocode] Résolution INSEE échouée pour {postal_code}: {e}")
        return None


def resolve_insee_code(postal_code: str) -> str | None:
    """Postal code -> INSEE commune code. None if it can't be resolved
    (unknown/foreign postal code, or the geo API is unreachable) — callers
    should fall back to whatever they do without an INSEE code."""
    if postal_code not in _INSEE_CACHE:
        code = _arrondissement_insee_code(postal_code)
        if code is None:
            code = _lookup_insee_code(postal_code)
        _INSEE_CACHE[postal_code] = code
    return _INSEE_CACHE[postal_code]


def _query_communes(query: str, limit: int, commune_type: str | None = None) -> list[dict]:
    params = {
        "nom": query,
        "boost": "population",
        "fields": "nom,code,codesPostaux,centre",
        "limit": limit,
    }
    if commune_type:
        params["type"] = commune_type
    resp = requests.get(COMMUNES_API, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _commune_to_suggestions(commune: dict) -> list[dict]:
    """One commune record -> one suggestion per postal code it covers.

    A regular commune has exactly one postal code, so this is a single
    suggestion. A whole-city aggregate for Paris/Lyon/Marseille (returned
    by the default commune-actuelle search) lists every postal code of
    every arrondissement under ONE INSEE code (75056/69123/13055) — pairing
    that single code with any one of those postal codes would silently
    mismatch (e.g. "Paris (75001)" tagged with INSEE 75056, not 75101),
    so those are expanded into one suggestion per postal code with the
    arrondissement-specific INSEE code resolved the same way Laforet
    already does (see _arrondissement_insee_code).
    """
    postal_codes = commune.get("codesPostaux") or []
    if not postal_codes:
        return []
    city = commune.get("nom")
    centre = commune.get("centre") or {}
    coords = centre.get("coordinates") or [None, None]
    lon, lat = coords[0], coords[1]
    whole_city_code = commune.get("code")

    if len(postal_codes) == 1:
        return [{
            "label": f"{city} ({postal_codes[0]})",
            "city": city,
            "postalCode": postal_codes[0],
            "inseeCode": whole_city_code,
            "lat": lat,
            "lon": lon,
        }]

    suggestions = []
    for postal_code in sorted(postal_codes):
        insee_code = _arrondissement_insee_code(postal_code) or whole_city_code
        suggestions.append({
            "label": f"{city} ({postal_code})",
            "city": city,
            "postalCode": postal_code,
            "inseeCode": insee_code,
            "lat": lat,
            "lon": lon,
        })
    return suggestions


def search_locations(query: str, limit: int = 20) -> list[dict]:
    """Autocomplete: French city name -> list of canonical location
    suggestions, one per city/postal-code match.

    Uses geo.api.gouv.fr/communes?nom=... (same API as resolve_insee_code,
    same API Laforet's own autocomplete calls). Paris/Lyon/Marseille are
    expanded into one suggestion per arrondissement/postal code (see
    _commune_to_suggestions) instead of one ambiguous whole-city entry —
    fetching only a handful of base communes so that expansion (up to 20
    postal codes for Paris alone) doesn't crowd out every other match.
    """
    query = query.strip()
    if len(query) < 2:
        return []
    try:
        communes = _query_communes(query, limit=5)
    except Exception as e:
        logger.warning(f"[geocode] Autocomplete échoué pour '{query}': {e}")
        return []

    suggestions: list[dict] = []
    seen_codes: set[str] = set()
    for commune in communes:
        for s in _commune_to_suggestions(commune):
            if s["inseeCode"] in seen_codes:
                continue
            seen_codes.add(s["inseeCode"])
            suggestions.append(s)

    return suggestions[:limit]
