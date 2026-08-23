"""Résolution géo Foncia — périmètre canonique -> slug de localité.

Contrairement au placeId SeLoger ou aux slugs d'Orpi et Century 21 (opaques,
résolus par autocomplete), le slug de localité Foncia se DÉRIVE du périmètre
canonique puis se VÉRIFIE contre l'API géo publique du site, trouvée dans le
TransferState des pages server-rendered et interrogée en direct le 23/08/2026
sans session ni authentification :

    GET https://fnc-api.prod.fonciatech.net/geo/localities/by-slug/<slug>
    -> {"items": [{"type": "ville"|"departement"|"region", "reference":
                   "ville_tout_toulouse", "slug": "toulouse-31",
                   "codeInsee": "31555", "codePostal": [...],
                   "pluriDistribue": true,
                   "departement": {"codeDepartement": "31", ...},
                   "region": {"codeRegion": "76", ...}}], "total": 1}

Dérivations vérifiées en direct :

- commune : `{ville-slugifiée}-{code postal}` (« Rosny-sous-Bois » ->
  `rosny-sous-bois-93110`, INSEE 93064 retourné) ; les slugs non canoniques
  sont acceptés par l'API (`toulouse-31000` rend la même ville que
  `toulouse-31`) ;
- ville entière : `{ville}-{département de l'INSEE}` (`toulouse-31`,
  `lyon-69`, `paris-75`) — entrées pluriDistribue qui couvrent tous les
  codes postaux, jamais une liste d'arrondissements ;
- département : `{nom-slugifié}-{code}`, code gardé tel quel car la Corse ne
  résout qu'en majuscules (`corse-du-sud-2A`, `haute-corse-2B` ; les
  variantes minuscules rendent une réponse vide). Le nom officiel est
  demandé à geo.api.gouv.fr quand la localisation n'en porte pas — tout
  département portant un code est donc résoluble ;
- région : `{nom-slugifié}` (« Occitanie » -> `occitanie`, codeRegion 76),
  même repli sur le nom officiel par code.

Chaque réponse porte l'identifiant officiel attendu (codeInsee /
codeDepartement / codeRegion) : la correspondance est REFUSÉE si elle ne
colle pas au périmètre canonique — un slug dérivé faux rend items vide ou
une localité différente, jamais un périmètre silencieusement faux. Le
contrôle en aval (matches_locations) garde de toute façon le résultat
honnête côté annonces.
"""

from __future__ import annotations

import re
import unicodedata

import requests
from loguru import logger

from core.criteria import source_overrides
from core.geocode import (
    CITY,
    DEPARTEMENTS_API,
    DEPARTMENT,
    REGION,
    REGIONS_API,
    WHOLE_CITY,
)

GEO_BASE_URL = "https://fnc-api.prod.fonciatech.net/geo"
LOCALITY_BY_SLUG_URL = f"{GEO_BASE_URL}/localities/by-slug"
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# Un échec de résolution est réessayé après ce délai — même politique que
# services/seloger_geocode.py et services/orpi_geocode.py.
_RETRY_COOLDOWN_SECONDS = 7 * 24 * 3600


def _slugify(text: str) -> str:
    """Le slug tel que Foncia le forme lui-même : minuscules, sans accents,
    tout séparateur en tiret (« Île-de-France » -> « ile-de-france » ;
    « L'Haÿ-les-Roses » -> « l-hay-les-roses », vérifié contre les valeurs
    renvoyées par son API géo)."""
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    segments = [s.lower() for s in re.split(r"[^A-Za-z0-9]+", ascii_text) if s]
    return "-".join(segments)


def _normalize_name(text: str) -> str:
    """Un nom pour comparaison : sans accents ni ponctuation, espaces
    resserrés, en majuscules (« Corse-du-Sud » == « corse du sud »)."""
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    compact = "".join(c if c.isalnum() else " " for c in ascii_text)
    return " ".join(compact.split()).upper()


def _department_code_of_insee(insee: str) -> str:
    """Le code département porté par un code INSEE de commune : deux chiffres
    en métropole (« 31555 » -> « 31 »), trois pour l'outre-mer
    (« 97123 » -> « 971 »). Les corses sortent tels quels
    (« 2A004 » -> « 2A »)."""
    return (insee or "")[:3] if (insee or "").startswith("97") else (insee or "")[:2]


def _official_name(url: str) -> str | None:
    """Le nom officiel d'un territoire sur geo.api.gouv.fr
    (/departements/31 -> « Haute-Garonne »), ou None si l'API échoue."""
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.debug(f"[foncia_geocode] Nom officiel introuvable ({url}) : {e}")
        return None
    name = data.get("nom") if isinstance(data, dict) else None
    return name or None


def _fetch_locality(slug: str) -> dict | None:
    """La première localité répondant à ce slug, ou None.

    La réponse est toujours enveloppée ({items: [...], total, count}, formes
    ville et département/région vérifiées identiques). Une réponse
    inattendue est traitée comme vide plutôt que levée : l'échec sera
    mémorisé comme tel par l'appelant."""
    query = (slug or "").strip()
    if not query:
        return None
    try:
        resp = requests.get(
            f"{LOCALITY_BY_SLUG_URL}/{query}",
            headers={"User-Agent": DESKTOP_UA, "Accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.debug(f"[foncia_geocode] Requête géo échouée pour « {query} » : {e}")
        return None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    return items[0] if items and isinstance(items[0], dict) else None


def area_cache_key(location: dict) -> str | None:
    """La clé de cache identifiant le périmètre — même convention exacte que
    services.orpi_geocode.area_cache_key (les sources partagent le
    vocabulaire de périmètres canoniques, pas de raison que leurs clés
    diffèrent)."""
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


def _names_match(item: dict, name: str) -> bool:
    libelle = item.get("libelle") or ""
    return bool(name) and _normalize_name(libelle) == _normalize_name(name)


def _resolve_city(location: dict) -> str | None:
    """Le slug d'une commune précise : `{ville}-{CP}` dérivé, puis
    vérification INSEE.

    Le code INSEE attendu fait foi ; en repli, l'accord du code postal ET du
    libellé est accepté (fusion de communes : l'INSEE du site peut diverger
    de celui de geo.api.gouv.fr alors que la commune est la bonne). Sans
    accord net -> None : un slug douteux ne devient jamais un périmètre
    faux."""
    postal_code = str(location.get("postalCode") or "")
    city = location.get("city") or ""
    base = _slugify(city)
    if not base or not postal_code:
        return None
    item = _fetch_locality(f"{base}-{postal_code}")
    if item is None or item.get("type") != "ville":
        return None
    expected_insee = location.get("inseeCode")
    if expected_insee and item.get("codeInsee") == expected_insee:
        return item.get("slug")
    if postal_code in (item.get("codePostal") or []) and _names_match(item, city):
        logger.debug(
            f"[foncia_geocode] {city} acceptée sur CP+libellé "
            f"(INSEE site {item.get('codeInsee')} != attendu {expected_insee})"
        )
        return item.get("slug")
    return None


def _resolve_whole_city(location: dict) -> str | None:
    """Le slug d'une commune ENTIÈRE : `{ville}-{département de l'INSEE}`
    (`paris-75`, `lyon-69`, `toulouse-31`), l'entrée pluriDistribue qui
    couvre tous les arrondissements/codes postaux — jamais une liste
    d'arrondissements.

    Vérification par INSEE d'abord ; en repli, libellé + nature
    pluriDistribue (l'entrée « tout X » d'une commune fusionnée peut porter
    un INSEE divergent)."""
    city = location.get("city") or ""
    base = _slugify(city)
    if not base:
        return None
    dept = _department_code_of_insee(str(location.get("inseeCode") or ""))
    if not dept:
        return None
    item = _fetch_locality(f"{base}-{dept}")
    if item is None or item.get("type") != "ville":
        return None
    expected_insee = location.get("inseeCode")
    if expected_insee and item.get("codeInsee") == expected_insee:
        return item.get("slug")
    if item.get("pluriDistribue") and _names_match(item, city):
        logger.debug(
            f"[foncia_geocode] {city} (entière) acceptée sur libellé+pluriDistribue"
        )
        return item.get("slug")
    return None


def _resolve_department(location: dict) -> str | None:
    """Le slug d'un département : `{nom-slugifié}-{code}` (`haute-garonne-31`,
    `corse-du-sud-2A` — le code reste tel quel, les minuscules corses ne
    résolvent pas). Sans nom dans la localisation, le nom officiel est
    demandé à geo.api.gouv.fr depuis le code : un département réduit à son
    code reste résoluble."""
    code = str(location.get("code") or "")
    if not code:
        return None
    name = location.get("name") or _official_name(f"{DEPARTEMENTS_API}/{code}")
    if not name:
        return None
    item = _fetch_locality(f"{_slugify(name)}-{code}")
    if item is None or item.get("type") != "departement":
        return None
    if str(item.get("codeDepartement") or "") != code:
        return None
    return item.get("slug")


def _resolve_region(location: dict) -> str | None:
    """Le slug d'une région : `{nom-slugifié}` (`occitanie`, codeRegion 76).
    Sans nom dans la localisation, le nom officiel est demandé à
    geo.api.gouv.fr depuis le code."""
    code = str(location.get("code") or "")
    if not code:
        return None
    name = location.get("name") or _official_name(f"{REGIONS_API}/{code}")
    if not name:
        return None
    item = _fetch_locality(_slugify(name))
    if item is None or item.get("type") != "region":
        return None
    if str(item.get("codeRegion") or "") != code:
        return None
    return item.get("slug")


def _resolve_uncached(location: dict) -> str | None:
    """Une tentative de résolution, sans cache. None sur tout ce qui n'est
    pas une correspondance vérifiée — jamais d'exception levée, les
    appelants mémorisent ça comme un échec réessayable."""
    kind = location.get("kind", CITY)
    try:
        if kind == CITY:
            return _resolve_city(location)
        if kind == WHOLE_CITY:
            return _resolve_whole_city(location)
        if kind == DEPARTMENT:
            return _resolve_department(location)
        if kind == REGION:
            return _resolve_region(location)
        return None
    except Exception as e:
        logger.debug(f"[foncia_geocode] Résolution échouée pour {_describe(location)}: {e}")
        return None


def resolve_slug_id(location: dict, repo) -> str | None:
    """Un périmètre canonique -> son slug de localité Foncia, cache d'abord.

    `repo` (un FonciaGeoRepository) est obligatoire et explicite : jamais lu
    depuis `flask.current_app`, le scraping tourne sur un thread de fond hors
    contexte d'application — voir FonciaParser._geo_repo()."""
    key = area_cache_key(location)
    if not key:
        logger.warning(
            f"[foncia_geocode] Périmètre non identifiable ({_describe(location)}), "
            "résolution impossible"
        )
        return None

    cached = repo.get_cached(key)
    if cached is not None:
        if cached["slug_id"]:
            return cached["slug_id"]
        age = _seconds_since(cached["resolved_at"])
        if age is not None and age < _RETRY_COOLDOWN_SECONDS:
            return None  # échec récent, ne pas marteler l'API à nouveau

    slug_id = _resolve_uncached(location)
    repo.set_cached(key, slug_id)
    if slug_id:
        logger.info(f"[foncia_geocode] {_describe(location)} -> {slug_id}")
    else:
        logger.warning(f"[foncia_geocode] Aucun slug trouvé pour {_describe(location)}")
    return slug_id


def _seconds_since(resolved_at) -> float | None:
    if resolved_at is None:
        return None
    import datetime
    if resolved_at.tzinfo:
        now = datetime.datetime.now(resolved_at.tzinfo)
    else:
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    return (now - resolved_at).total_seconds()


def remember_manual_slugs(criteria: dict, repo) -> None:
    """Mémorise des slugs saisis à la main contre le périmètre auquel ils
    correspondent, si l'association est sans ambiguïté (exactement un
    périmètre dans la recherche) — même principe que
    services.orpi_geocode.remember_manual_slugs."""
    from parsers.base import get_locations

    slugs = source_overrides(criteria, "foncia").get("slugs") or []
    locations = get_locations(criteria)
    if not slugs or len(locations) != 1:
        return

    key = area_cache_key(locations[0])
    if not key:
        return

    if repo.get_cached(key) is None:
        repo.set_cached(key, str(slugs[0]))
        logger.info(f"[foncia_geocode] Slug manuel banqué pour {key}: {slugs[0]}")
