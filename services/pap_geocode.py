"""Résolution géo PAP — périmètre canonique -> identifiant numérique opaque.

Contrairement au code INSEE, l'identifiant de lieu de pap.fr est un entier
propre au site (`439` pour Paris, `37782` pour Paris 15e, `397` pour la
Gironde), renvoyé par l'autocomplete public du site (trouvé par inspection du
bundle JS du champ de recherche, vérifié en direct le 22/08/2026, sans
session ni authentification) :

    GET https://www.pap.fr/json/ac-geo?q=<texte ou code>
    -> [{"id": 439, "name": "Paris (75)"}, {"id": 37782, "name": "Paris 15e"},
        {"id": 397, "name": "Gironde - 33"}, {"id": 471, "name": "Île-de-France"}]

Deux particularités par rapport aux autres sources :

- Cloudflare filtre par EMPREINTE TLS : curl et requests nus reçoivent un
  challenge 403 « Just a moment », mais une requête à empreinte navigateur
  passe systématiquement (vérifié en direct) — d'où curl_cffi avec
  impersonation Chrome, ici comme dans parsers.pap.
- TOUS les niveaux de périmètre ont un identifiant natif, y compris la RÉGION
  (« Île-de-France » -> 471, « Corse » -> 468, sous les noms officiels actuels
  « Auvergne-Rhône-Alpes », « Nouvelle-Aquitaine »... vérifiés en direct) et
  les départements corses sous leurs vrais codes (« Corse-du-Sud - 2A » ->
  383) : pas d'élargissement région -> départements ici, contrairement à
  bienici et Century 21.

Les noms affichés embarquent soit le code postal (« Courbevoie (92400) »),
soit le numéro de département pour les grandes villes (« Rennes (35) »,
« Saint-Étienne (42) »), soit rien pour les arrondissements (« Lyon 3e ») ;
les départements s'affichent « {nom} - {code} » (« Gironde - 33 »). La
désambiguïsation tient compte de ces formes (voir _pick_geo_id).

Cas particulier vérifié en direct : le département 75 n'a aucune entrée
« - 75 » dans l'autocomplete (q=75 renvoie la ville entière puis ses
arrondissements) — repli sur la ville entière de même nom que le libellé du
département (« Paris »), qui couvre tout le département (Paris = une seule
commune). Même convention que le repli v-paris de Century 21.
"""

from __future__ import annotations

import re
import unicodedata

from curl_cffi import requests as curl_requests
from loguru import logger

from core.geocode import CITY, DEPARTMENT, REGION, WHOLE_CITY

AC_GEO_URL = "https://www.pap.fr/json/ac-geo"

# Empreinte TLS navigateur : sans elle, Cloudflare répond un challenge 403
# « Just a moment » aux clients curl/requests (vérifié en direct).
IMPERSONATE = "chrome124"

# Un échec de résolution est réessayé après ce délai — même politique que
# services/seloger_geocode.py, services/bienici_geocode.py et
# services/century21_geocode.py.
_RETRY_COOLDOWN_SECONDS = 7 * 24 * 3600

# Les entrées d'arrondissement portent un suffixe ordinal (« Paris 15e »,
# « Lyon 1er », « Marseille 3eme ») : retiré pour comparer avec la ville.
_ORDINAL_SUFFIX_RE = re.compile(r"\s+\d{1,2}\s*(?:er|e|eme|ème|nd|nde)?\s*$", re.IGNORECASE)
_PARENS_RE = re.compile(r"\([^)]*\)")
_DEPARTMENT_NAME_RE = re.compile(r"\s*-\s*([0-9A-B]{2,3})$")


def _query_autocomplete(text: str) -> list[dict]:
    """L'autocomplete de PAP pour un texte ou un code.

    X-Requested-With est requis (l'endpoint est appelé en AJAX par le site).
    Une réponse inattendue (pas une liste) est traitée comme vide plutôt que
    de lever — l'échec sera mémorisé comme tel par l'appelant."""
    if not text or not text.strip():
        return []
    resp = curl_requests.get(
        AC_GEO_URL,
        params={"q": text.strip()},
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=10,
        impersonate=IMPERSONATE,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _normalize_name(text: str) -> str:
    """Un nom pour comparaison : parenthèses et suffixes ordinaux retirés,
    sans accents ni ponctuation, espaces resserrés, en majuscules.

    « Paris (75) », « Paris 5e » et « Paris » convergent vers « PARIS » ;
    « Courbevoie (92400) » vers « COURBEVOIE ». Le retrait ordinal reste borné
    aux fins de nom (« ... 15e ») : il ne touche pas les noms sans chiffre."""
    stripped = _PARENS_RE.sub("", text or "")
    stripped = _ORDINAL_SUFFIX_RE.sub("", stripped)
    normalized = unicodedata.normalize("NFKD", stripped)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    compact = "".join(c if c.isalnum() else " " for c in ascii_text)
    return " ".join(compact.split()).upper()


def _names_match(entry_name: str, target_name: str) -> bool:
    if not entry_name or not target_name:
        return False
    return _normalize_name(entry_name) == _normalize_name(target_name)


def _paren_content(entry_name: str) -> str:
    m = re.search(r"\(([^)]*)\)", entry_name or "")
    return m.group(1).strip() if m else ""


def _pick_city(results: list[dict], city_name: str) -> dict | None:
    """L'entrée d'un code postal précis (commune ou arrondissement).

    La requête étant déjà scopée par code postal, la première entrée dont le
    nom correspond à la ville convient — arrondissements compris (« Paris
    15e » se rapproche de « Paris » après retrait de l'ordinal). En repli, la
    première entrée du code postal (un CP partagé dont aucune entrée ne porte
    le nom demandé est rarissime, et le contrôle de périmètre en aval,
    matches_locations, garde le résultat honnête)."""
    for r in results:
        if _names_match(r.get("name", ""), city_name):
            return r
    return results[0] if results else None


def _pick_whole_city(results: list[dict], city_name: str) -> dict | None:
    """L'entrée « ville entière » d'une recherche par nom.

    Parmi les entrées dont le nom correspond, on préfère celle qui porte la
    marque du site pour une ville entière : parenthèse courte = numéro de
    département (« Paris (75) », « Rennes (35) », « Saint-Étienne (42) ») ou
    pas de parenthèse du tout ; une longue parenthèse est un code postal
    (« Courbevoie (92400) », commune unique de son nom, acceptée en repli).
    Jamais de repli aveugle sur une entrée qui ne porterait pas le nom :
    une requête par nom n'est pas scopée (« Rennes » renvoie aussi
    « Rennes-sur-Loue »)."""
    candidates = [r for r in results if _names_match(r.get("name", ""), city_name)]
    for r in candidates:
        paren = _paren_content(r.get("name", ""))
        if not paren or len(paren) <= 3:
            return r
    return candidates[0] if candidates else None


def _pick_department(results: list[dict], code: str, dept_name: str) -> dict | None:
    """L'entrée d'un département : « {nom} - {code} » (« Gironde - 33 »).

    Cas particulier vérifié en direct : le 75 n'a pas d'entrée « - 75 »
    (l'autocomplete renvoie la ville puis ses arrondissements) — repli sur
    l'entrée de même nom que le libellé du département (« Paris (75) »), la
    commune unique couvrant tout le département."""
    for r in results:
        m = _DEPARTMENT_NAME_RE.search(r.get("name") or "")
        if m and m.group(1).upper() == code.upper():
            return r
    if dept_name:
        return _pick_whole_city(results, dept_name)
    return None


def _pick_region(results: list[dict], region_name: str) -> dict | None:
    """L'entrée d'une région, par correspondance exacte du nom normalisé.

    PAP référence les régions sous leurs noms officiels actuels (« Île-de-
    France » -> 471, « Auvergne-Rhône-Alpes », « Nouvelle-Aquitaine »...
    vérifiés en direct), ceux-là mêmes que renvoie geo.api.gouv.fr. Aucun
    repli flou : un nom non reconnu doit rester un échec visible plutôt que
    risquer un périmètre faux."""
    for r in results:
        if _names_match(r.get("name", ""), region_name):
            return r
    return None


def area_cache_key(location: dict) -> str | None:
    """La clé de cache identifiant le périmètre — même convention exacte que
    services.seloger_geocode.area_cache_key, services.bienici_geocode et
    services.century21_geocode.area_cache_key : les sources partagent le
    vocabulaire de périmètres canoniques, pas de raison que leurs clés
    diffèrent."""
    kind = location.get("kind", CITY)
    if kind == CITY:
        insee = location.get("inseeCode")
        if insee:
            return insee
        postal_code = location.get("postalCode")
        return f"postal:{postal_code}" if postal_code else None
    if kind == WHOLE_CITY:
        insee = location.get("inseeCode")
        if insee:
            return f"city:{insee}"
        city = location.get("city")
        return f"city_name:{city.strip().casefold()}" if city else None
    code = location.get("code")
    if not code:
        return None
    return f"region:{code}" if kind == REGION else f"dept:{code}"


def _describe(location: dict) -> str:
    kind = location.get("kind", CITY)
    if kind == REGION:
        return f"région {location.get('name') or location.get('code')}"
    if kind == DEPARTMENT:
        return f"département {location.get('name') or location.get('code')}"
    if kind == WHOLE_CITY:
        return f"{location.get('city')} (toute la ville)"
    return f"{location.get('city')} ({location.get('postalCode')})"


def _resolve_uncached(location: dict) -> str | None:
    """Une tentative de résolution, sans cache. None sur tout ce qui n'est
    pas une correspondance nette — jamais d'exception levée, les appelants
    mémorisent ça comme un échec réessayable."""
    kind = location.get("kind", CITY)
    try:
        if kind == CITY:
            results = _query_autocomplete(location.get("postalCode") or "")
            entry = _pick_city(results, location.get("city") or "")
        elif kind == WHOLE_CITY:
            results = _query_autocomplete(location.get("city") or "")
            entry = _pick_whole_city(results, location.get("city") or "")
        elif kind == DEPARTMENT:
            code = location.get("code")
            if not code:
                return None
            results = _query_autocomplete(str(code))
            entry = _pick_department(results, str(code), location.get("name") or "")
        elif kind == REGION:
            region_name = location.get("name") or ""
            if not region_name:
                # Pas de requête par code possible : « 11 » renvoie l'Aude,
                # les codes région et département partagent le même espace
                # de valeurs ambigu pour l'autocomplete.
                return None
            results = _query_autocomplete(region_name)
            entry = _pick_region(results, region_name)
        else:
            return None
        return str(entry["id"]) if entry and entry.get("id") is not None else None
    except Exception as e:
        logger.debug(f"[pap_geocode] Résolution échouée pour {_describe(location)}: {e}")
        return None


def resolve_geo_id(location: dict, repo) -> str | None:
    """Un périmètre canonique -> son identifiant numérique PAP, cache d'abord.

    `repo` (un PapGeoRepository) est obligatoire et explicite : jamais lu
    depuis `flask.current_app`, le scraping tourne sur un thread de fond hors
    contexte d'application — voir PapParser._geo_repo()."""
    key = area_cache_key(location)
    if not key:
        logger.warning(
            f"[pap_geocode] Périmètre non identifiable ({_describe(location)}), "
            "résolution impossible"
        )
        return None

    cached = repo.get_cached(key)
    if cached is not None:
        if cached["geo_id"]:
            return cached["geo_id"]
        age = _seconds_since(cached["resolved_at"])
        if age is not None and age < _RETRY_COOLDOWN_SECONDS:
            return None  # échec récent, ne pas marteler le site à nouveau

    geo_id = _resolve_uncached(location)
    repo.set_cached(key, geo_id)
    if geo_id:
        logger.info(f"[pap_geocode] {_describe(location)} -> g{geo_id}")
    else:
        logger.warning(f"[pap_geocode] Aucun identifiant trouvé pour {_describe(location)}")
    return geo_id


def _seconds_since(resolved_at) -> float | None:
    if resolved_at is None:
        return None
    import datetime
    if resolved_at.tzinfo:
        now = datetime.datetime.now(resolved_at.tzinfo)
    else:
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    return (now - resolved_at).total_seconds()


def remember_manual_geo_ids(criteria: dict, repo) -> None:
    """Mémorise des identifiants saisis à la main contre le périmètre auquel
    ils correspondent, si l'association est sans ambiguïté (exactement un
    périmètre dans la recherche) — même principe que
    services.seloger_geocode.remember_manual_place_id."""
    from core.criteria import source_overrides
    from parsers.base import get_locations

    geo_ids = source_overrides(criteria, "pap").get("geoIds") or []
    locations = get_locations(criteria)
    if not geo_ids or len(locations) != 1:
        return

    key = area_cache_key(locations[0])
    if not key:
        return

    if repo.get_cached(key) is None:
        repo.set_cached(key, str(geo_ids[0]))
        logger.info(f"[pap_geocode] Identifiant manuel banqué pour {key}: {geo_ids[0]}")
