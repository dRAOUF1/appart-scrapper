"""SeLoger placeId resolution — city -> opaque `AD0{n}FR{id}` identifier.

SeLoger's placeId isn't derivable from an INSEE code by any formula
(verified live: AD08FR31096 resolves to all of Paris, not INSEE 75056 or
any per-arrondissement code) — but SeLoger's own front-end location search
bar calls a real, public JSON endpoint to resolve a typed city/postal code
into exactly this placeId format. Found by network-capturing SeLoger's own
homepage search bar with a headless browser (same technique already used
for Laforet — see parsers/laforet.py's module docstring) and confirmed to
work with plain `requests`, no session warm-up, no cookies, no anti-bot
gating of any kind:

    POST https://www.seloger.com/search-mfe-bff/autocomplete
    {"text": "75015", "limit": 10,
     "placeTypes": [...], "parentTypes": [...], "locale": "fr"}
    -> [{"id": "AD09FR40", "type_key": "AD09",
         "labels": ["Paris 15ème arrondissement (75015)"],
         "postal_codes": ["75015"], "coordinates": {...}}, ...]

Querying by the *postal code* (not the city name) is what disambiguates
Paris/Lyon/Marseille arrondissements automatically: the API itself ranks a
POCO/AD09 entry whose postal_codes is exactly `[postal_code]` first, ahead
of the whole-city AD08 entry — see _pick_best_match.

As a second, complementary path: every placeId a user has ever manually
pasted (search URL or raw Place ID — see parsers.seloger.EXTRA_LOCATION_HELP)
is banked against that location's INSEE code the moment the search is
created (see remember_manual_place_id), so a manual paste, if ever needed
again for some edge case, only ever has to happen once per city.

Both paths write into the same persistent cache (repositories.seloger_geo_repo),
so whichever one finds a placeId first is all that's ever needed again.
"""

from __future__ import annotations

import requests
from loguru import logger

from core.geocode import CITY, DEPARTMENT, REGION, WHOLE_CITY

AUTOCOMPLETE_URL = "https://www.seloger.com/search-mfe-bff/autocomplete"
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
# Every place type the front-end's own search bar requests — city (AD08),
# arrondissement (AD09), postal-code area (POCO), neighborhood (NBH*),
# region/department (AD02/AD04/AD06), house number (HONU).
PLACE_TYPES = ["HONU", "NBH1", "NBH3", "AD09", "NBH2", "AD08", "AD06", "AD04", "POCO", "AD02"]

# A cached failure is retried after this long — in case the endpoint's
# shape changes or is temporarily unreachable.
_RETRY_COOLDOWN_SECONDS = 7 * 24 * 3600

# Le type d'entrée SeLoger correspondant à chaque niveau de périmètre
# canonique. Vérifié en live : chercher le nom du périmètre suffit, le type
# discrimine ensuite l'entrée voulue parmi les résultats.
#
#   "Île-de-France"  -> AD04FR5     (AD04, région)
#   "Gironde"        -> AD06FR34    (AD06, département)
#   "Paris"          -> AD08FR31096 (AD08, ville entière, 21 codes postaux)
#
# Et chacun couvre bien tout son périmètre : AD04FR5 rend des annonces
# réparties sur les 8 départements d'Île-de-France. Les suffixes ne sont pas
# dérivables du code officiel (Gironde = département 33 mais AD06FR34), d'où
# l'interrogation de l'autocomplete.
_KIND_PLACE_TYPES = {
    REGION: "AD04",
    DEPARTMENT: "AD06",
    WHOLE_CITY: "AD08",
}


def _query_autocomplete(text: str) -> list[dict]:
    if len(text) < 2:
        return []
    resp = requests.post(
        AUTOCOMPLETE_URL,
        json={"text": text, "limit": 10, "placeTypes": PLACE_TYPES, "parentTypes": PLACE_TYPES, "locale": "fr"},
        headers={
            "User-Agent": DESKTOP_UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Referer": "https://www.seloger.com/",
            "Accept-Language": "fr-FR,fr;q=0.9",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _pick_best_match(results: list[dict], postal_code: str) -> dict | None:
    """The result whose postal_codes is exactly this one postal code (a
    POCO or per-arrondissement AD09 entry) beats a broader match (e.g. the
    whole-city AD08 entry, whose postal_codes includes many others) —
    querying by postal code already ranks these first, but don't rely on
    result order alone."""
    exact = [r for r in results if r.get("postal_codes") == [postal_code]]
    if exact:
        return exact[0]
    contains = [r for r in results if postal_code in (r.get("postal_codes") or [])]
    if contains:
        return contains[0]
    return results[0] if results else None


def _find_city_place_id(postal_code: str) -> str | None:
    """Le placeId d'un code postal précis.

    Deliberately queries by postal code ONLY, no city-name fallback: a
    plain city-name query (e.g. "Paris") caps at 10 results, which for a
    20-arrondissement city can leave the specific match we need outside
    that window — _pick_best_match would then have nothing exact to pick
    from and could fall through to a much broader (wrong) match. Querying
    by postal code directly reliably returns the specific match first
    (verified live across several cities), so there's no real upside to
    the fallback, only a correctness risk.
    """
    results = _query_autocomplete(postal_code)
    match = _pick_best_match(results, postal_code)
    return match["id"] if match else None


def _find_wide_area_place_id(name: str, kind: str) -> str | None:
    """Le placeId d'un périmètre large (région, département, ville entière).

    Recherche par nom, puis on ne retient qu'une entrée du type attendu : sans
    ce filtre, « Gironde » renverrait la commune Gironde-sur-Dropt et
    « Corse » une commune homonyme, au lieu du périmètre demandé.
    """
    expected_type = _KIND_PLACE_TYPES.get(kind)
    if not expected_type or not name:
        return None
    for result in _query_autocomplete(name):
        if result.get("type_key") == expected_type:
            return result.get("id")
    return None


def area_cache_key(location: dict) -> str | None:
    """La clé de cache identifiant le périmètre, tous niveaux confondus.

    Le niveau fait partie de la clé : une même valeur peut désigner deux
    périmètres différents selon le niveau (le département 75 et la région 75
    — Nouvelle-Aquitaine — existent tous les deux).

    Le niveau commune garde le code INSEE nu comme clé : c'est la convention
    d'avant l'introduction des périmètres larges, et la conserver évite
    d'invalider les résolutions déjà en cache.
    """
    kind = location.get("kind", CITY)
    if kind == CITY:
        return location.get("inseeCode") or None
    if kind == WHOLE_CITY:
        insee = location.get("inseeCode")
        return f"city:{insee}" if insee else None
    code = location.get("code")
    if not code:
        return None
    return f"{'region' if kind == REGION else 'dept'}:{code}"


def _resolve_uncached(location: dict) -> str | None:
    """Une tentative de résolution, sans cache. None sur tout ce qui n'est pas
    une correspondance nette — les appelants la mémorisent comme un échec
    (réessayable), jamais d'exception levée."""
    kind = location.get("kind", CITY)
    try:
        if kind == CITY:
            return _find_city_place_id(location["postalCode"])
        if kind == WHOLE_CITY:
            return _find_wide_area_place_id(location.get("city"), kind)
        return _find_wide_area_place_id(location.get("name"), kind)
    except Exception as e:
        logger.debug(f"[seloger_geocode] Résolution échouée pour {_describe(location)}: {e}")
        return None


def _describe(location: dict) -> str:
    kind = location.get("kind", CITY)
    if kind == REGION:
        return f"région {location.get('name') or location.get('code')}"
    if kind == DEPARTMENT:
        return f"département {location.get('name') or location.get('code')}"
    if kind == WHOLE_CITY:
        return f"{location.get('city')} (toute la ville)"
    return f"{location.get('city')} ({location.get('postalCode')})"


def resolve_place_id(location: dict, repo) -> str | None:
    """Un périmètre canonique -> son placeId SeLoger, cache d'abord.

    Fonctionne à tous les niveaux (région, département, ville entière, code
    postal) : SeLoger a un identifiant pour chacun, et un seul suffit à couvrir
    tout le périmètre — inutile de le développer en liste de communes.

    `repo` (un SelogerGeoRepository) est obligatoire et explicite : il n'est
    surtout pas lu depuis `flask.current_app`, car le scraping s'exécute sur un
    thread de fond, hors contexte d'application — voir
    SeLogerParser._geo_repo() et core.scrape_control.
    """
    key = area_cache_key(location)
    if not key:
        logger.warning(
            f"[seloger_geocode] Périmètre non identifiable ({_describe(location)}), "
            "résolution impossible"
        )
        return None

    cached = repo.get_cached(key)
    if cached is not None:
        if cached["place_id"]:
            return cached["place_id"]
        age = _seconds_since(cached["resolved_at"])
        if age is not None and age < _RETRY_COOLDOWN_SECONDS:
            return None  # recent failure, don't hammer the site again yet

    place_id = _resolve_uncached(location)
    repo.set_cached(key, place_id)
    if place_id:
        logger.info(f"[seloger_geocode] {_describe(location)} -> {place_id}")
    else:
        logger.warning(f"[seloger_geocode] Aucun placeId trouvé pour {_describe(location)}")
    return place_id


def _seconds_since(resolved_at) -> float | None:
    if resolved_at is None:
        return None
    import datetime
    now = datetime.datetime.now(resolved_at.tzinfo) if resolved_at.tzinfo else datetime.datetime.utcnow()
    return (now - resolved_at).total_seconds()


def remember_manual_place_id(criteria: dict, repo) -> None:
    """Mémorise un placeId saisi à la main contre le périmètre auquel il
    correspond, si l'association est sans ambiguïté (exactement un périmètre,
    exactement un placeId — la liste de placeIds de SeLoger n'est pas appariée
    aux localisations, une saisie multi-périmètres ne peut donc être attribuée
    à aucun en particulier). Appelé une fois à l'enregistrement d'une
    recherche ; sans effet le reste du temps.

    Le placeId saisi à la main vit dans les surcharges de source des
    critères canoniques (voir core.criteria), pas au premier niveau.
    """
    from core.criteria import source_overrides
    from parsers.base import get_locations

    place_ids = source_overrides(criteria, "seloger").get("placeIds") or []
    locations = get_locations(criteria)
    if len(place_ids) != 1 or len(locations) != 1:
        return

    key = area_cache_key(locations[0])
    if not key:
        return

    if repo.get_cached(key) is None:
        repo.set_cached(key, place_ids[0])
        logger.info(f"[seloger_geocode] placeId manuel banqué pour {key}: {place_ids[0]}")
