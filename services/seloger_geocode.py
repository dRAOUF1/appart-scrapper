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


def _find_place_id(city: str, postal_code: str) -> str | None:
    """Single best-effort attempt to resolve a placeId for this location.
    Returns None on anything but a clean match — callers cache that as a
    (retriable) failure, never raise.

    Deliberately queries by postal code ONLY, no city-name fallback: a
    plain city-name query (e.g. "Paris") caps at 10 results, which for a
    20-arrondissement city can leave the specific match we need outside
    that window — _pick_best_match would then have nothing exact to pick
    from and could fall through to a much broader (wrong) match. Querying
    by postal code directly reliably returns the specific match first
    (verified live across several cities), so there's no real upside to
    the fallback, only a correctness risk.
    """
    try:
        results = _query_autocomplete(postal_code)
        match = _pick_best_match(results, postal_code)
        return match["id"] if match else None
    except Exception as e:
        logger.debug(f"[seloger_geocode] Résolution échouée pour {city} ({postal_code}): {e}")
        return None


def resolve_place_id(insee_code: str, city: str, postal_code: str, repo) -> str | None:
    """INSEE code -> SeLoger placeId, cache-first.

    `repo` (un SelogerGeoRepository) est obligatoire et explicite : il n'est
    surtout pas lu depuis `flask.current_app`, car le scraping s'exécute sur un
    thread de fond, hors contexte d'application — voir
    SeLogerParser._geo_repo() et core.scrape_control.
    """
    cached = repo.get_cached(insee_code)
    if cached is not None:
        if cached["place_id"]:
            return cached["place_id"]
        age = _seconds_since(cached["resolved_at"])
        if age is not None and age < _RETRY_COOLDOWN_SECONDS:
            return None  # recent failure, don't hammer the site again yet

    place_id = _find_place_id(city, postal_code)
    repo.set_cached(insee_code, place_id)
    if place_id:
        logger.info(f"[seloger_geocode] {city} ({postal_code}) -> {place_id}")
    else:
        logger.warning(f"[seloger_geocode] Aucun placeId trouvé pour {city} ({postal_code})")
    return place_id


def _seconds_since(resolved_at) -> float | None:
    if resolved_at is None:
        return None
    import datetime
    now = datetime.datetime.now(resolved_at.tzinfo) if resolved_at.tzinfo else datetime.datetime.utcnow()
    return (now - resolved_at).total_seconds()


def remember_manual_place_id(criteria: dict, repo) -> None:
    """Bank a manually-pasted SeLoger placeId against its location's INSEE
    code, if the mapping is unambiguous (exactly one location, exactly one
    placeId — SeLoger's placeIds list isn't paired to specific locations,
    so a multi-location manual entry can't be safely attributed to any one
    of them). Called once at search-creation time; a no-op otherwise.

    Le placeId saisi à la main vit dans les surcharges de source des
    critères canoniques (voir core.criteria), pas au premier niveau.
    """
    from core.criteria import source_overrides
    from parsers.base import get_locations

    place_ids = source_overrides(criteria, "seloger").get("placeIds") or []
    locations = get_locations(criteria)
    if len(place_ids) != 1 or len(locations) != 1:
        return
    insee_code = locations[0].get("inseeCode")
    if not insee_code:
        return

    if repo.get_cached(insee_code) is None:
        repo.set_cached(insee_code, place_ids[0])
        logger.info(f"[seloger_geocode] placeId manuel banqué pour INSEE {insee_code}: {place_ids[0]}")
