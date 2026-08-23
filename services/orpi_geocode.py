"""Résolution géo Orpi — périmètre canonique -> slug d'URL.

Comme le placeId de SeLoger ou le slug de Century 21, l'identifiant de lieu
d'orpi.com est un slug propre au site (`rosny-sous-bois`, `gironde`,
`ile-de-france`) qui ne se dérive PAS du code INSEE par formule. Il est
renvoyé par l'autocomplete public du site (trouvé par inspection du bundle JS
de la barre de recherche, vérifié en direct le 22/08/2026, sans session ni
authentification) :

    GET https://www.orpi.com/recherche/autocompletion/<texte ou code>
    -> {"zipcode": [{"value": "cp-93110", ...}],
        "city": [{"value": "rosny-sous-bois", "name": "Rosny-sous-Bois (93110)",
                  "area": "city", "zipcode": ["93110"], "parents": [...]}],
        "department": [{"value": "gironde", "area": "department"}],
        "region": [{"value": "ile-de-france", "area": "region"}]}

La requête est un segment de CHEMIN (pas un paramètre ?q=) et la réponse est
un dict de groupes par type de périmètre — à ne pas confondre avec les
listes plates de Century 21 et PAP.

Particularités vérifiées en direct :

- les villes multi-arrondissements sont éclatées en communes distinctes
  (« Paris » -> `paris-1`... `paris-20`, « Lyon » -> `lyon-1`...) MAIS le
  moteur accepte le slug nu (`paris` rend les 20 arrondissements, 510
  annonces ; `lyon`, 399) : une ville entière se résout donc sur l'entrée
  départementale homonyme quand elle existe (Paris), sinon sur le slug nu
  dérivé du nom (Lyon, Marseille) — jamais en liste d'arrondissements ;
- la Corse n'a AUCUNE entrée par code (q=2A -> []) : ses départements se
  résolvent par leur NOM (« Corse-du-Sud » -> `corse-du-sud`) ;
- les régions ont un identifiant natif (« Île-de-France » ->
  `ile-de-france`, « Corse » -> `corse`) mais PAS de requête possible par
  code : « 11 » renvoie l'Aude, les codes région et département partagent le
  même espace ambigu. La requête passe donc par le nom, slugifié.
"""

from __future__ import annotations

import re
import unicodedata

import requests
from loguru import logger

from core.geocode import CITY, DEPARTMENT, REGION, WHOLE_CITY

AUTOCOMPLETION_URL = "https://www.orpi.com/recherche/autocompletion/"
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# Un échec de résolution est réessayé après ce délai — même politique que
# services/seloger_geocode.py, services/bienici_geocode.py,
# services/century21_geocode.py et services/pap_geocode.py.
_RETRY_COOLDOWN_SECONDS = 7 * 24 * 3600

# Les départements corses n'ont aucune entrée autocomplete par code
# (« 2A » -> [], vérifié en direct) : ils se requêtent par leur nom.
_CORSE_DEPARTMENT_NAMES = {"2A": "Corse-du-Sud", "2B": "Haute-Corse"}


def _query_autocomplete(text: str) -> dict:
    """L'autocomplete d'Orpi pour un texte ou un code.

    Le terme est un segment de chemin (`/recherche/autocompletion/93110`),
    pas un paramètre. Une réponse inattendue (pas un dict de groupes) est
    traitée comme vide plutôt que de lever — l'échec sera mémorisé comme tel
    par l'appelant."""
    query = (text or "").strip()
    if len(query) < 2:
        return {}
    resp = requests.get(
        f"{AUTOCOMPLETION_URL}{requests.utils.quote(query)}",
        headers={"User-Agent": DESKTOP_UA, "Accept": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _normalize_name(text: str) -> str:
    """Un nom pour comparaison : parenthèse de code postal retirée, sans
    accents ni ponctuation, espaces resserrés, en majuscules.

    « Rosny-sous-Bois (93110) » et « Rosny-sous-Bois » convergent vers
    « ROSNY SOUS BOIS »."""
    stripped = re.sub(r"\([^)]*\)", "", text or "")
    normalized = unicodedata.normalize("NFKD", stripped)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    compact = "".join(c if c.isalnum() else " " for c in ascii_text)
    return " ".join(compact.split()).upper()


def _slugify(text: str) -> str:
    """Le slug d'URL tel qu'Orpi le forme lui-même : minuscules, sans
    accents, tout séparateur en tiret (« Île-de-France » ->
    « ile-de-france », identique aux valeurs renvoyées par son autocomplete,
    comparaison faite)."""
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    segments = [s.lower() for s in re.split(r"[^A-Za-z0-9]+", ascii_text) if s]
    return "-".join(segments)


def _group(results: dict, name: str) -> list[dict]:
    """Les entrées d'un groupe de l'autocomplete (« city », « department »,
    « region »), jamais None."""
    entries = results.get(name)
    return entries if isinstance(entries, list) else []


def _entry_name(entry: dict) -> str:
    """Le libellé d'une entrée : champ `name`, sinon `label` débarrassé du
    code postal accolé (« Rosny-sous-Bois (93110) » -> « Rosny-sous-Bois »)."""
    name = entry.get("name") or entry.get("label") or ""
    return re.sub(r"\s*\(\d{4,5}\)\s*$", "", name)


def _names_match(entry: dict, target_name: str) -> bool:
    if not target_name:
        return False
    return _normalize_name(_entry_name(entry)) == _normalize_name(target_name)


def _pick_city(results: dict, postal_code: str, city_name: str) -> str | None:
    """Le slug d'une commune précise, depuis une requête par CODE POSTAL.

    1. Une ville dont le nom correspond ET qui porte ce code postal
       (« Haybes » et « Fumay » partagent le 08170 : seul le nom tranche).
    2. En repli, la première ville de ce code postal — un CP partagé dont
       aucun nom ne correspond est rarissime (nom abrégé par le site), et le
       contrôle de périmètre en aval (matches_locations) garde le résultat
       honnête.
    """
    cities = _group(results, "city")
    for entry in cities:
        if postal_code and postal_code not in (entry.get("zipcode") or []):
            continue
        if _names_match(entry, city_name):
            return entry.get("value")
    for entry in cities:
        if postal_code and postal_code in (entry.get("zipcode") or []):
            return entry.get("value")
    return None


def _pick_whole_city(results: dict, city_name: str) -> str | None:
    """Le slug d'une commune ENTIÈRE (tous arrondissements confondus).

    1. L'entrée départementale homonyme (« Paris » : q=75 comme q=paris
       renvoient un département `paris`, qui couvre les 20 arrondissements —
       vérifié en direct, 510 annonces).
    2. Sinon le slug nu dérivé du nom (« Lyon » -> `lyon`) :
       l'autocomplete n'expose QUE les arrondissements (`lyon-1`...) mais le
       moteur accepte le slug nu (vérifié en direct : `lyon` rend 399
       annonces couvrant Lyon 1 à 9). Un slug dérivé faux rendrait count=0,
       jamais un périmètre silencieusement faux.
    """
    for entry in _group(results, "department"):
        if _names_match(entry, city_name):
            return entry.get("value")
    slug = _slugify(city_name)
    return slug if slug else None


def _pick_department(results: dict, dept_name: str) -> str | None:
    """Le slug d'un département, depuis une requête par code (« 33 » ->
    groupe department [`gironde`] ; le district « neuilly-sur-marne-33-
    hectares » du même groupe est ignoré : seule l'entrée area=department
    compte). Le nom, quand la localisation canonique en porte un, sert à
    désambiguïser entre plusieurs entrées départementales."""
    departments = [
        e for e in _group(results, "department") if e.get("area") == "department"
    ]
    for entry in departments:
        if _names_match(entry, dept_name):
            return entry.get("value")
    return departments[0].get("value") if departments else None


def _pick_region(results: dict, region_name: str) -> str | None:
    """Le slug d'une région, par correspondance exacte du nom normalisé.

    La requête « ile-de-france » renvoie aussi « hauts-de-france » et
    « france-d-outre-mer » (correspondance floue du site) : seul le nom
    demandé est retenu, sans repli flou — un nom non reconnu doit rester un
    échec visible plutôt que risquer un périmètre faux."""
    for entry in _group(results, "region"):
        if _names_match(entry, region_name):
            return entry.get("value")
    return None


def area_cache_key(location: dict) -> str | None:
    """La clé de cache identifiant le périmètre — même convention exacte que
    services.seloger_geocode.area_cache_key, services.bienici_geocode,
    services.century21_geocode.area_cache_key et services.pap_geocode.
    area_cache_key : les sources partagent le vocabulaire de périmètres
    canoniques, pas de raison que leurs clés diffèrent."""
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
            return _pick_city(
                results, location.get("postalCode") or "", location.get("city") or ""
            )
        if kind == WHOLE_CITY:
            results = _query_autocomplete(location.get("city") or "")
            return _pick_whole_city(results, location.get("city") or "")
        if kind == DEPARTMENT:
            code = str(location.get("code") or "")
            if not code:
                return None
            if code.upper() in _CORSE_DEPARTMENT_NAMES:
                # La Corse n'a aucune entrée par code (« 2A » -> [], vérifié
                # en direct) : requête par le nom du département.
                results = _query_autocomplete(_CORSE_DEPARTMENT_NAMES[code.upper()])
                return _pick_department(results, code)
            results = _query_autocomplete(code)
            return _pick_department(results, location.get("name") or "")
        if kind == REGION:
            region_name = location.get("name") or ""
            if not region_name:
                # Pas de requête par code possible (« 11 » renvoie l'Aude) :
                # sans nom, c'est le parser qui élargit aux départements.
                return None
            results = _query_autocomplete(_slugify(region_name))
            return _pick_region(results, region_name)
        return None
    except Exception as e:
        logger.debug(f"[orpi_geocode] Résolution échouée pour {_describe(location)}: {e}")
        return None


def resolve_slug_id(location: dict, repo) -> str | None:
    """Un périmètre canonique -> son slug Orpi, cache d'abord.

    `repo` (un OrpiGeoRepository) est obligatoire et explicite : jamais lu
    depuis `flask.current_app`, le scraping tourne sur un thread de fond hors
    contexte d'application — voir OrpiParser._geo_repo()."""
    key = area_cache_key(location)
    if not key:
        logger.warning(
            f"[orpi_geocode] Périmètre non identifiable ({_describe(location)}), "
            "résolution impossible"
        )
        return None

    cached = repo.get_cached(key)
    if cached is not None:
        if cached["slug_id"]:
            return cached["slug_id"]
        age = _seconds_since(cached["resolved_at"])
        if age is not None and age < _RETRY_COOLDOWN_SECONDS:
            return None  # échec récent, ne pas marteler le site à nouveau

    slug_id = _resolve_uncached(location)
    repo.set_cached(key, slug_id)
    if slug_id:
        logger.info(f"[orpi_geocode] {_describe(location)} -> {slug_id}")
    else:
        logger.warning(f"[orpi_geocode] Aucun slug trouvé pour {_describe(location)}")
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
    services.seloger_geocode.remember_manual_place_id."""
    from core.criteria import source_overrides
    from parsers.base import get_locations

    slugs = source_overrides(criteria, "orpi").get("slugs") or []
    locations = get_locations(criteria)
    if not slugs or len(locations) != 1:
        return

    key = area_cache_key(locations[0])
    if not key:
        return

    if repo.get_cached(key) is None:
        repo.set_cached(key, str(slugs[0]))
        logger.info(f"[orpi_geocode] Slug manuel banqué pour {key}: {slugs[0]}")
