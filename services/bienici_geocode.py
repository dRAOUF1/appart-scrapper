"""BienIci zoneId resolution — périmètre canonique -> zoneIds propriétaires.

Comme le placeId de SeLoger, le zoneId de bienici n'est dérivable d'aucun
code INSEE par formule, mais bienici expose son propre endpoint public
d'autocomplete pour le résoudre (trouvé par network-capture de la barre de
recherche du site, vérifié en direct le 26/07/2026, sans session ni
authentification) :

    GET https://res.bienici.com/suggest.json?q=<texte ou code>
    -> [{"name": "Paris 15e", "type": "arrondissement", "insee_code": "75115",
         "postalCodes": ["75015"], "zoneIds": ["-9520"]}, ...]

Vérifié en direct : une commune ou un code postal se résout en interrogeant
directement le code postal (comme SeLoger), et surtout un DÉPARTEMENT se
résout en interrogeant directement son code INSEE (`q=33` renvoie
`{"type": "department", "insee_code": "33", ...}` en tête) — plus simple que
la recherche par nom de SeLoger, et sans ambiguïté.

bienici n'a en revanche AUCUNE entité de niveau région dans cet endpoint
(vérifié sur plusieurs régions : "Ile-de-France", "Bretagne",
"Auvergne-Rhone-Alpes" ne renvoient que des communes/départements homonymes,
jamais un type "region"). Une région se résout donc en UNION des zoneIds de
chacun de ses départements (core.geocode.region_departments) — même principe
que filter[departments][] chez Laforet pour couvrir un périmètre large en
une seule requête.

C'est pourquoi cette résolution renvoie une LISTE de zoneIds (pluriel),
contrairement au placeId singulier de SeLoger : une région en a plusieurs,
et bienici lui-même regroupe parfois deux zones sous un même résultat (ex.
"Rhône et Grand Lyon" -> zoneIds: ["-4850450", "-4850451"]).
"""

from __future__ import annotations

import requests
from loguru import logger

from core.geocode import CITY, DEPARTMENT, REGION, WHOLE_CITY

SUGGEST_URL = "https://res.bienici.com/suggest.json"
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# Un échec de résolution est réessayé après ce délai — même politique que
# services/seloger_geocode.py.
_RETRY_COOLDOWN_SECONDS = 7 * 24 * 3600


def _query_suggest(text: str) -> list[dict]:
    if not text or len(text) < 2:
        return []
    resp = requests.get(
        SUGGEST_URL,
        params={"q": text},
        headers={
            "User-Agent": DESKTOP_UA,
            "Accept": "*/*",
            "Referer": "https://www.bienici.com/",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _pick_city_match(results: list[dict], postal_code: str) -> dict | None:
    """L'entrée (arrondissement ou commune) dont postalCodes est exactement
    ce code postal — même logique que SeLoger's _pick_best_match : une
    correspondance exacte prime sur toute autre, pour ne jamais confondre un
    arrondissement avec la ville entière."""
    exact = [
        r for r in results
        if r.get("type") in ("arrondissement", "city")
        and r.get("postalCodes") == [postal_code]
    ]
    if exact:
        return exact[0]
    contains = [
        r for r in results
        if r.get("type") in ("arrondissement", "city")
        and postal_code in (r.get("postalCodes") or [])
    ]
    return contains[0] if contains else None


def _find_city_zone_ids(postal_code: str) -> list[str] | None:
    """Le(s) zoneId(s) d'un code postal précis (commune ou arrondissement)."""
    match = _pick_city_match(_query_suggest(postal_code), postal_code)
    return match.get("zoneIds") or None if match else None


def _find_whole_city_zone_ids(city: str, insee_code: str | None) -> list[str] | None:
    """Le(s) zoneId(s) de toute une commune — recherche par nom, ne retient
    que le type "city" dont l'insee_code correspond quand on le connaît,
    pour ne pas confondre deux communes homonymes."""
    for result in _query_suggest(city):
        if result.get("type") != "city":
            continue
        if insee_code and result.get("insee_code") != insee_code:
            continue
        zone_ids = result.get("zoneIds")
        if zone_ids:
            return zone_ids
    return None


def _find_department_zone_ids(code: str) -> list[str] | None:
    """Le(s) zoneId(s) d'un département — interrogé directement par son code
    INSEE (vérifié en direct : `q=33` renvoie le département Gironde en
    tête, sans ambiguïté aucune, contrairement à une recherche par nom)."""
    for result in _query_suggest(code):
        if result.get("type") == "department" and result.get("insee_code") == code:
            return result.get("zoneIds") or None
    return None


def _find_region_zone_ids(location: dict) -> list[str] | None:
    """L'union des zoneIds de chaque département de la région : bienici n'a
    pas d'entité région, cf. docstring du module."""
    from core.geocode import region_departments

    code = location.get("code")
    if not code:
        return None

    zone_ids: list[str] = []
    for dept_code in region_departments(code):
        for zid in _find_department_zone_ids(dept_code) or []:
            if zid not in zone_ids:
                zone_ids.append(zid)
    return zone_ids or None


def area_cache_key(location: dict) -> str | None:
    """La clé de cache identifiant le périmètre, tous niveaux confondus —
    même convention exacte que services.seloger_geocode.area_cache_key : les
    deux sources partagent le même vocabulaire de périmètres canoniques, pas
    de raison que leurs clés de cache diffèrent."""
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


def _describe(location: dict) -> str:
    kind = location.get("kind", CITY)
    if kind == REGION:
        return f"région {location.get('name') or location.get('code')}"
    if kind == DEPARTMENT:
        return f"département {location.get('name') or location.get('code')}"
    if kind == WHOLE_CITY:
        return f"{location.get('city')} (toute la ville)"
    return f"{location.get('city')} ({location.get('postalCode')})"


def _resolve_uncached(location: dict) -> list[str] | None:
    """Une tentative de résolution, sans cache. None sur tout ce qui n'est
    pas une correspondance nette — jamais d'exception levée, les appelants
    mémorisent ça comme un échec réessayable."""
    kind = location.get("kind", CITY)
    try:
        if kind == CITY:
            return _find_city_zone_ids(location["postalCode"])
        if kind == WHOLE_CITY:
            return _find_whole_city_zone_ids(location.get("city"), location.get("inseeCode"))
        if kind == DEPARTMENT:
            return _find_department_zone_ids(location["code"])
        if kind == REGION:
            return _find_region_zone_ids(location)
        return None
    except Exception as e:
        logger.debug(f"[bienici_geocode] Résolution échouée pour {_describe(location)}: {e}")
        return None


def resolve_zone_ids(location: dict, repo) -> list[str] | None:
    """Un périmètre canonique -> ses zoneIds bienici, cache d'abord.

    `repo` (un BienIciGeoRepository) est obligatoire et explicite : jamais lu
    depuis `flask.current_app`, le scraping tourne sur un thread de fond hors
    contexte d'application — voir BienIciParser._geo_repo()."""
    key = area_cache_key(location)
    if not key:
        logger.warning(
            f"[bienici_geocode] Périmètre non identifiable ({_describe(location)}), "
            "résolution impossible"
        )
        return None

    cached = repo.get_cached(key)
    if cached is not None:
        if cached["zone_ids"]:
            return cached["zone_ids"]
        age = _seconds_since(cached["resolved_at"])
        if age is not None and age < _RETRY_COOLDOWN_SECONDS:
            return None  # échec récent, ne pas marteler le site à nouveau

    zone_ids = _resolve_uncached(location)
    repo.set_cached(key, zone_ids)
    if zone_ids:
        logger.info(f"[bienici_geocode] {_describe(location)} -> {zone_ids}")
    else:
        logger.warning(f"[bienici_geocode] Aucun zoneId trouvé pour {_describe(location)}")
    return zone_ids


def _seconds_since(resolved_at) -> float | None:
    if resolved_at is None:
        return None
    import datetime
    if resolved_at.tzinfo:
        now = datetime.datetime.now(resolved_at.tzinfo)
    else:
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    return (now - resolved_at).total_seconds()


def remember_manual_zone_ids(criteria: dict, repo) -> None:
    """Mémorise des zoneIds saisis à la main contre le périmètre auquel ils
    correspondent, si l'association est sans ambiguïté (exactement un
    périmètre dans la recherche) — même principe que
    services.seloger_geocode.remember_manual_place_id."""
    from core.criteria import source_overrides
    from parsers.base import get_locations

    zone_ids = source_overrides(criteria, "bienici").get("zoneIds") or []
    locations = get_locations(criteria)
    if not zone_ids or len(locations) != 1:
        return

    key = area_cache_key(locations[0])
    if not key:
        return

    if repo.get_cached(key) is None:
        repo.set_cached(key, list(zone_ids))
        logger.info(f"[bienici_geocode] zoneIds manuels banqués pour {key}: {zone_ids}")
