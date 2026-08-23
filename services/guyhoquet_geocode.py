"""Guy Hoquet slug resolution — périmètre canonique -> identifiant de lieu.

L'identifiant de localisation de guy-hoquet.com est un « slug » suffixé par
le type de périmètre (`toulouse-31000_c3`, `lyon-69123_c4`, `31_c2`,
`76_c1`), renvoyé par l'autocomplete public du site (vérifié en direct le
23/08/2026, curl nu, sans session ni header particulier) :

    GET https://www.guy-hoquet.com/biens/search-localization?q=<texte|CP>
    -> {"success": true, "cities": [{"slug": "toulouse-31000_c3",
        "name": "Toulouse", "zip": "31000", "location_type": 3, ...}]}

`location_type` observés en direct :
    1 région (`76_c1`)      2 département (`31_c2`)
    3 commune + code postal (`toulouse-31000_c3`, arrondissements inclus)
    4 toute la ville, avec le code INSEE RÉEL embarqué dans le slug
      (`lyon-69123_c4`, `rennes-35238_c4`, `ajaccio-2A004_c4`)
    5 « toutes les communes » (Martinique/Mayotte) — ignoré.

La particularité de cette source est que départements et régions se
DÉRIVENT PUREMENT du code canonique (`31` -> `31_c2`, Corse comprise avec
ses codes minuscules `2a_c2`/`2b_c2`, DROM inclus) : zéro réseau, zéro
cache pour ces deux niveaux. Seules villes (city) et communes entières
(whole_city) demandent l'autocomplete — et encore, avec un contrôle fort
possible pour whole_city puisque le site embarque l'INSEE réel dans le
slug `_c4`, comparable directement au `inseeCode` canonique.

Deux limites vérifiées en direct : les régions DROM (codes 01/02/03/04/06,
différents des codes de département) n'existent pas dans l'autocomplete ->
non résolubles ; et un slug de ville mal formé fait planter la recherche
en HTTP 500 côté site — on ne fabrique donc JAMAIS un slug city/
whole_city localement, uniquement via l'autocomplete (les slugs dept/
région, eux, sont sûrs : `44_c2` et `76_c1` répondent en direct).

Bruit non français : l'autocomplete mélange des entrées canadiennes dont
le slug est préfixé `ca-` (ex. `ca-qc-11_c2`) — écartées systématiquement.
Les noms multi-mots exigent des tirets dans la requête (`ile-de-france`
trouve, `ile de france` ne trouve rien) : une requête vide est retentée
avec le nom slugifié en tirets.
"""

from __future__ import annotations

import re
import unicodedata

import requests
from loguru import logger

from core.geocode import CITY, DEPARTMENT, REGION, WHOLE_CITY

SUGGEST_URL = "https://www.guy-hoquet.com/biens/search-localization"
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# Un échec de résolution est réessayé après ce délai — même politique que
# services/seloger_geocode.py et services/century21_geocode.py.
_RETRY_COOLDOWN_SECONDS = 7 * 24 * 3600

# Codes de région DROM : absents de l'autocomplete (vérifié en direct), à la
# différence des départements DROM 971..976 qui passent. Sans entrée côté
# site, aucune région ultramarine n'est résoluble.
_UNRESOLVABLE_REGION_CODES = {"01", "02", "03", "04", "06"}

_LOCATION_TYPE_REGION = 1
_LOCATION_TYPE_DEPARTMENT = 2
_LOCATION_TYPE_CITY = 3
_LOCATION_TYPE_WHOLE_CITY = 4


def _query_autocomplete(text: str) -> list[dict]:
    """Les entrées `cities` de l'autocomplete Guy Hoquet pour un texte.

    Endpoint public sans header requis (vérifié en direct au curl nu). Une
    réponse inattendue (pas de JSON, success faux, cities absent) vaut liste
    vide plutôt qu'une exception : l'appelant mémorisera un échec réessayable."""
    if not text or len(text) < 2:
        return []
    resp = requests.get(
        SUGGEST_URL,
        params={"q": text},
        headers={"User-Agent": DESKTOP_UA, "Accept": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not data.get("success"):
        return []
    cities = data.get("cities")
    return [c for c in cities if isinstance(c, dict)] if isinstance(cities, list) else []


def _usable_entries(results: list[dict], location_type: int) -> list[dict]:
    """Les entrées d'un `location_type` donné, bruit non français écarté.

    L'autocomplete mélange des adresses canadiennes reconnaissables à leur
    slug préfixé `ca-` (ex. `ca-qc-11_c2`, vérifié en direct) : elles ne
    correspondent jamais à un périmètre français canonique."""
    entries = []
    for r in results:
        if r.get("location_type") != location_type:
            continue
        slug = r.get("slug") or ""
        if slug.startswith("ca-"):
            continue
        entries.append(r)
    return entries


def _normalize_name(text: str) -> str:
    """Un nom de localité pour comparaison : majuscules, sans accents, sans
    ponctuation, espaces resserrés — même normalisation que les autres
    sources (« Ivry-sur-Seine », « IVRY SUR SEINE » convergent)."""
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    compact = "".join(c if c.isalnum() else " " for c in ascii_text)
    return " ".join(compact.split()).upper()


def _names_match(entry_name: str, city_name: str) -> bool:
    """Le nom affiché par l'autocomplete correspond-il à la ville demandée ?"""
    if not entry_name or not city_name:
        return False
    return _normalize_name(entry_name) == _normalize_name(city_name)


def _dashed_slug(text: str) -> str:
    """Un nom slugifié en tirets, tel que l'autocomplete l'attend pour les
    noms multi-mots (« Île-de-France » -> « ile-de-france » ; la forme brute
    avec espaces ne renvoie rien, vérifié en direct)."""
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()


def _query_with_dash_retry(text: str) -> list[dict]:
    """L'autocomplete pour un nom, retenté slugifié en tirets si vide.

    Les libellés multi-mots arrivant des critères canoniques portent des
    espaces (« Saint-Étienne » passe tel quel grâce à ses tirets déjà présents,
    mais « Le Mans » ne trouve pas alors que « le-mans » trouve) : une seule
    retente, pas de martèlement du endpoint."""
    results = _query_autocomplete(text)
    if results:
        return results
    dashed = _dashed_slug(text)
    if dashed and dashed != text:
        return _query_autocomplete(dashed)
    return []


def _insee_from_whole_city_slug(slug: str) -> str | None:
    """Le code INSEE embarqué dans un slug `_c4` (« lyon-69123_c4 » ->
    « 69123 », « ajaccio-2A004_c4 » -> « 2A004 »), ou None s'il n'a pas la
    forme attendue. C'est ce code, fourni par le site lui-même, qui permet
    de contrôler la correspondance avec l'`inseeCode` canonique."""
    base = slug[: -len("_c4")] if slug.endswith("_c4") else slug
    candidate = base.rsplit("-", 1)[-1] if base else ""
    candidate = candidate.upper()
    return candidate if re.fullmatch(r"\d[A-B0-9]{4}", candidate) else None


def _pick_city_slug(results: list[dict], postal_code: str, city_name: str) -> str | None:
    """Le slug commune+CP (`_c3`) d'un code postal précis.

    1. Une entrée dont le code postal correspond ET le nom aussi (le seul
       cas ambigu étant les codes postaux partagés, ex. 69003 Lyon 3e /
       Villeurbanne — l'autocomplete par CP liste les deux).
    2. En repli, la première entrée du même code postal : les noms abrégés
       par le site ne matchent pas toujours, et le contrôle de périmètre en
       aval (matches_locations) garde le résultat honnête."""
    entries = _usable_entries(results, _LOCATION_TYPE_CITY)
    for e in entries:
        if e.get("zip") == postal_code and _names_match(e.get("name", ""), city_name):
            return e["slug"]
    for e in entries:
        if e.get("zip") == postal_code:
            return e["slug"]
    return None


def _pick_whole_city_slug(results: list[dict], city_name: str, expected_insee: str | None,
                          postal_codes: list[str] | None = None) -> str | None:
    """Le slug ville entière d'une commune.

    Quand l'`inseeCode` canonique est connu (cas nominal : il vient de
    l'autocomplete du front et ne doit jamais être perdu en route), le slug
    choisi est celui dont l'INSEE embarqué correspond EXACTEMENT — contrôle
    impossible sur la plupart des autres sources, possible ici parce que le
    site publie l'INSEE réel dans son slug. Sinon, repli sur le nom.

    REPLI vérifié en direct (23/08/2026) : les petites communes n'ont PAS de
    slug `_c4` (« ivry » ne renvoie que des `_c3`) — le site ne crée une
    entrée ville entière que pour les communes multi-codes postaux. Une
    commune mono-CP y supplée par son `_c3` au nom correspondant (et au CP
    couvert par la localisation quand elle en porte) : ce slug couvre alors
    déjà toute la commune. Aucune dérivation locale : un slug fabriqué hors
    autocomplete fait planter la recherche en HTTP 500 côté site."""
    entries = _usable_entries(results, _LOCATION_TYPE_WHOLE_CITY)
    if expected_insee:
        for e in entries:
            if _insee_from_whole_city_slug(e.get("slug", "")) == expected_insee.upper():
                return e["slug"]
    else:
        for e in entries:
            if _names_match(e.get("name", ""), city_name):
                return e["slug"]

    wanted = {str(cp) for cp in (postal_codes or [])}
    for e in _usable_entries(results, _LOCATION_TYPE_CITY):
        if not _names_match(e.get("name", ""), city_name):
            continue
        if wanted and str(e.get("zip") or "") not in wanted:
            continue
        return e["slug"]
    return None


def _region_slug(code: str | None) -> str | None:
    """Le slug d'une région, dérivé purement du code canonique (`76` ->
    `76_c1`). Les régions DROM n'existent pas côté site -> None (vérifié en
    direct : seuls les codes métropole 11..94 ont leur `_c1`)."""
    if not code or code in _UNRESOLVABLE_REGION_CODES:
        return None
    return f"{code}_c1"


def _department_slug(code: str | None) -> str | None:
    """Le slug d'un département, dérivé purement du code canonique, Corse
    minuscule comprise (`2A` -> `2a_c2`, vérifié en direct) et DROM inclus
    (971..976 passent). Jamais de réseau : la forme du slug est stable et
    un mauvais code ne plante pas la recherche (contrairement aux villes)."""
    if not code:
        return None
    return f"{code.lower()}_c2"


def area_cache_key(location: dict) -> str | None:
    """La clé de cache identifiant le périmètre — même convention exacte que
    services.century21_geocode.area_cache_key : les sources partagent le
    vocabulaire de périmètres canoniques, pas de raison que leurs clés
    diffèrent.

    Seules city et whole_city sont réellement mises en cache : région et
    département se dérivent purement du code, sans réseau — les mettre en
    cache n'apporterait rien."""
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


def is_statically_resolvable(location: dict) -> bool:
    """Ce périmètre se résout-il sans aucun réseau ni cache (région/département) ?

    Utilisé par has_valid_criteria du parser : une recherche ne portant que
    sur des départements/régions est valide même sans storage injecté."""
    kind = location.get("kind", CITY)
    if kind == REGION:
        code = location.get("code")
        return bool(code) and code not in _UNRESOLVABLE_REGION_CODES
    if kind == DEPARTMENT:
        return bool(location.get("code"))
    return False


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
    """Une tentative de résolution réseau, sans cache (villes uniquement :
    région/département se dérivent statiquement, voir resolve_slug). None
    sur tout ce qui n'est pas une correspondance nette — jamais d'exception
    levée, l'appelant mémorisera un échec réessayable."""
    kind = location.get("kind", CITY)
    try:
        if kind == CITY:
            postal_code = location.get("postalCode") or ""
            results = _query_with_dash_retry(postal_code)
            return _pick_city_slug(results, postal_code, location.get("city") or "")
        if kind == WHOLE_CITY:
            city = location.get("city") or ""
            results = _query_with_dash_retry(city)
            return _pick_whole_city_slug(
                results, city,
                location.get("inseeCode") or None,
                location.get("postalCodes") or [],
            )
        return None
    except Exception as e:
        logger.debug(f"[guyhoquet_geocode] Résolution échouée pour {_describe(location)}: {e}")
        return None


def _seconds_since(resolved_at) -> float | None:
    if resolved_at is None:
        return None
    import datetime
    if resolved_at.tzinfo:
        now = datetime.datetime.now(resolved_at.tzinfo)
    else:
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    return (now - resolved_at).total_seconds()


def resolve_slug(location: dict, repo) -> str | None:
    """Un périmètre canonique -> son slug Guy Hoquet, cache d'abord.

    Interface uniforme avec services.century21_geocode.resolve_slug_id.
    `repo` (un GuyHoquetGeoRepository) est obligatoire pour les villes et
    explicite — jamais lu depuis `flask.current_app`, le scraping tourne
    sur un thread de fond hors contexte d'application. Il est ignoré pour
    région/département : leur dérivation est pure, rien à mémoriser.

    Un périmètre non identifiable (sans code ni nom) vaut None avec un
    warning ; un échec réseau/mémorisé vaut None sans casser le scrape des
    autres périmètres de la même recherche."""
    key = area_cache_key(location)
    if not key:
        logger.warning(
            f"[guyhoquet_geocode] Périmètre non identifiable ({_describe(location)}), "
            "résolution impossible"
        )
        return None

    # Région et département : dérivation pure, jamais de cache ni de réseau.
    kind = location.get("kind", CITY)
    if kind == REGION:
        slug = _region_slug(location.get("code"))
        if slug is None:
            logger.warning(
                f"[guyhoquet_geocode] {_describe(location)} : région DROM non "
                "référencée par le site, ignorée"
            )
        return slug
    if kind == DEPARTMENT:
        return _department_slug(location.get("code"))

    # Villes : cache persistant d'abord (échecs compris, avec cooldown).
    if repo is None:
        logger.warning(
            "[guyhoquet_geocode] Aucun storage fourni au parser, résolution du slug "
            "impossible (voir get_parser(source, storage=...))"
        )
        return None

    cached = repo.get_cached(key)
    if cached is not None:
        if cached["slug_id"]:
            return cached["slug_id"]
        age = _seconds_since(cached["resolved_at"])
        if age is not None and age < _RETRY_COOLDOWN_SECONDS:
            return None  # échec récent, ne pas marteler le site à nouveau

    slug = _resolve_uncached(location)
    repo.set_cached(key, slug)
    if slug:
        logger.info(f"[guyhoquet_geocode] {_describe(location)} -> {slug}")
    else:
        logger.warning(f"[guyhoquet_geocode] Aucun slug trouvé pour {_describe(location)}")
    return slug
