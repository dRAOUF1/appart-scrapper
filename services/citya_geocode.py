"""Résolution géo Citya — périmètre canonique -> slug de recherche.

Le suffixe numérique des slugs de recherche Citya (`toulouse-31555`,
`lyon-69000`, `haute-garonne-31`) est OPAQUE : tantôt le code INSEE de la
commune, tantôt son code postal (Toulouse 31555 mais Lyon 69000), et le
libellé du slug compte autant que le code — une URL mal formée ne renvoie
pas d'erreur mais la FRANCE ENTIÈRE en 200 (vérifié en direct le 23/08/2026 :
`foo-bar-31555` et `toulouse-31000` répondent tous deux le stock national).
On ne dérive donc JAMAIS un slug localement : il vient de l'autocomplete du
site, trouvé dans ses bundles JS (ApiService.getVilles) :

    GET https://www.citya.com/api/search_place_results?q=<texte|code>
    Referer: https://www.citya.com/...          <- OBLIGATOIRE (401 sinon)
    -> {"villes": [{"id": "31555", "codesPostaux": [...], "ville": "Toulouse",
                    "slug": "toulouse-31555"}],
        "departements": [{"code": "31", "slug": "haute-garonne-31"}],
        "regions": [{"libelle": "Occitanie", "slug": "occitanie-76"}]}

Correspondances vérifiées en direct :

- commune : `id` vaut l'INSEE officiel (« Paris » 75056, « Lyon » 69123,
  arrondissements inclus comme entrées séparées `Paris 15e Arrondissement`
  id 75114) ; les homonymies se lèvent par code postal (`saint-maur` en
  rend cinq) ; les libellés accentués/multi-mots passent tels quels
  (« Le Mans », « Saint-Étienne ») — c'est la forme DASHÉE qui échoue ;
- département : la requête accepte le CODE directement (`q=31` ->
  haute-garonne-31, `q=2A` -> corse-du-sud-2a, `q=971` -> guadeloupe-971) ;
- région : requête par nom officiel (« Occitanie » -> occitanie-76, suffixe
  = code INSEE de région). La Corse (94) et les régions DROM (01/02/03/04/06)
  n'apparaissent PAS dans `regions` — non résolubles, comme chez Guy Hoquet.

La couverture des entrées « ville principale » est vérifiée : lyon-69000
rend des annonces dans tous les arrondissements (69001..69009), paris-75
couvre la capitale entière — whole_city et city partagent donc la même
entrée quand elle est unique.
"""

from __future__ import annotations

import unicodedata

import requests
from loguru import logger

from core.criteria import source_overrides
from core.geocode import CITY, DEPARTMENT, REGION, REGIONS_API, WHOLE_CITY

SUGGEST_URL = "https://www.citya.com/api/search_place_results"
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# Le Referer interne est OBLIGATOIRE : sans lui l'endpoint répond 401
# « Full authentication is required » alors qu'avec lui il répond sans
# aucune session ni cookie (vérifié en direct le 23/08/2026, dans les deux
# sens). C'est un contrôle de provenance, pas une authentification.
_HEADERS = {
    "User-Agent": DESKTOP_UA,
    "Accept": "application/json",
    "Referer": "https://www.citya.com/annonces/location",
}

# Un échec de résolution est réessayé après ce délai — même politique que
# services/seloger_geocode.py, services/orpi_geocode.py et services/foncia_geocode.py.
_RETRY_COOLDOWN_SECONDS = 7 * 24 * 3600

# Codes INSEE de région absents de l'autocomplete Citya (vérifié en direct :
# « Corse », « Guadeloupe », « La Réunion » ne rendent AUCUNE entrée région,
# seul leur niveau département existe côté site) -> non résolubles à ce niveau.
_UNRESOLVABLE_REGION_CODES = {"01", "02", "03", "04", "06", "94"}


def _query_autocomplete(text: str) -> dict:
    """L'autocomplete Citya pour un texte ou un code : les trois listes
    `villes` / `departements` / `regions`, toujours présentes.

    Les libellés accentués et multi-mots passent TELS QUELS (« Le Mans »,
    « Saint-Étienne », « Provence-Alpes-Côte d'Azur » trouvent ; c'est la
    forme dashée qui ne trouve rien, vérifié en direct) — aucun retravail de
    la requête. Une réponse inattendue (pas de JSON, liste au lieu d'objet)
    vaut un dict vide plutôt qu'une exception : l'appelant mémorisera un
    échec réessayable."""
    query = (text or "").strip()
    if not query:
        return {}
    try:
        resp = requests.get(
            SUGGEST_URL,
            params={"q": query},
            headers=_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.debug(f"[citya_geocode] Autocomplete échoué pour « {query} » : {e}")
        return {}

    def _entries(key: str) -> list[dict]:
        raw = data.get(key) if isinstance(data, dict) else None
        return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []

    return {
        "villes": _entries("villes"),
        "departements": _entries("departements"),
        "regions": _entries("regions"),
    }


def _normalize_name(text: str) -> str:
    """Un nom pour comparaison : majuscules, sans accents ni ponctuation,
    espaces resserrés — même normalisation que les autres sources
    (« Ivry-sur-Seine » == « IVRY SUR SEINE »)."""
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    compact = "".join(c if c.isalnum() else " " for c in ascii_text)
    return " ".join(compact.split()).upper()


def _names_match(entry_name: str, city_name: str) -> bool:
    if not entry_name or not city_name:
        return False
    return _normalize_name(entry_name) == _normalize_name(city_name)


def _official_region_name(code: str) -> str | None:
    """Le nom officiel d'une région sur geo.api.gouv.fr (source partagée
    core.geocode), ou None si l'API échoue. L'autocomplete Citya ne connaît
    pas les régions par code : il lui faut leur libellé."""
    try:
        resp = requests.get(f"{REGIONS_API}/{code}", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.debug(f"[citya_geocode] Nom officiel introuvable pour la région {code} : {e}")
        return None
    name = data.get("nom") if isinstance(data, dict) else None
    return name or None


def area_cache_key(location: dict) -> str | None:
    """La clé de cache identifiant le périmètre — même convention exacte que
    services.foncia_geocode.area_cache_key : les sources partagent le
    vocabulaire de périmètres canoniques, pas de raison que leurs clés
    diffèrent (INSEE nu pour une commune, `city:<insee>`, `city_name:<ville>`,
    `dept:<code>`, `region:<code>`)."""
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
    """Ce périmètre se résout-il sans aucun réseau ? Jamais chez Citya :
    même le département passe par l'autocomplete (`q=<code>`), tout slug
    dérivé localement risquant le repli national silencieux du site. Tous
    les niveaux passent donc par le cache + l'autocomplete."""
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


def _pick_city_entry(villes: list[dict], name: str, insee: str | None,
                     postal_code: str | None) -> dict | None:
    """L'entrée ville de l'autocomplete correspondant au périmètre canonique.

    Par ordre de confiance :
    1. l'INSEE attendu ET le code postal d'accord (le cas nominal : l'INSEE
       vient de l'autocomplete du front et ne doit jamais être perdu) ;
    2. l'INSEE seul (les arrondissements Paris/Lyon/Marseille ont chacun
       leur entrée : « Paris 15e Arrondissement » id 75114 — le libellé du
       site diffère alors du nom canonique, seul l'id fait foi) ;
    3. le libellé ET le code postal (fusion de communes : l'INSEE du site
       peut diverger de celui de geo.api.gouv.fr alors que la commune est
       la bonne — même tolérance que services/foncia_geocode).

    Sans accord net -> None : un slug douteux ne devient jamais un
    périmètre faux, le site replierait silencieusement sur la France
    entière."""
    for entry in villes:
        if entry.get("id") == insee and (not postal_code or postal_code in (entry.get("codesPostaux") or [])):
            return entry
    if insee:
        for entry in villes:
            if entry.get("id") == insee:
                return entry
    for entry in villes:
        if postal_code and postal_code in (entry.get("codesPostaux") or []) \
                and _names_match(entry.get("ville"), name):
            logger.debug(
                f"[citya_geocode] {name} acceptée sur CP+libellé "
                f"(INSEE site {entry.get('id')} != attendu {insee})"
            )
            return entry
    return None


def _resolve_city(location: dict) -> str | None:
    """Le slug d'une commune précise (un code postal)."""
    villes = _query_autocomplete(location.get("city") or "")["villes"]
    entry = _pick_city_entry(
        villes,
        location.get("city") or "",
        location.get("inseeCode") or None,
        str(location.get("postalCode") or "") or None,
    )
    return entry.get("slug") if entry else None


def _resolve_whole_city(location: dict) -> str | None:
    """Le slug d'une commune ENTIÈRE, tous codes postaux.

    L'entrée « ville principale » du site couvre les arrondissements :
    lyon-69000 rend des biens dans tous les CP 69001..69009, paris-75 toute
    la capitale (vérifié en direct sur les codes postaux des cartes). Le
    choix suit l'INSEE d'abord (69123 -> lyon-69000), le libellé ensuite —
    une commune mono-code postal n'a qu'une entrée, qui EST la commune
    entière."""
    name = location.get("city") or ""
    villes = _query_autocomplete(name)["villes"]
    entry = _pick_city_entry(villes, name, location.get("inseeCode") or None, None)
    return entry.get("slug") if entry else None


def _resolve_department(location: dict) -> str | None:
    """Le slug d'un département : la requête par CODE est acceptée
    directement par l'autocomplete (`q=31`, `q=2A`, `q=971` vérifiés),
    Corse et DROM compris — pas besoin du nom officiel."""
    code = str(location.get("code") or "")
    if not code:
        return None
    departements = _query_autocomplete(code)["departements"]
    for entry in departements:
        if str(entry.get("code", "")).upper() == code.upper():
            return entry.get("slug")
    return None


def _resolve_region(location: dict) -> str | None:
    """Le slug d'une région métropolitaine : requête par NOM OFFICIEL
    (l'autocomplete ignore les régions par code), correspondance vérifiée
    par le suffixe du slug = code INSEE attendu (`occitanie-76`). La Corse
    et les régions DROM n'existent pas à ce niveau côté site."""
    code = str(location.get("code") or "")
    if not code:
        return None
    if code in _UNRESOLVABLE_REGION_CODES:
        logger.warning(
            f"[citya_geocode] {_describe(location)} : région non référencée par "
            "l'autocomplete Citya, ignorée"
        )
        return None
    name = location.get("name") or _official_region_name(code)
    if not name:
        return None
    regions = _query_autocomplete(name)["regions"]
    for entry in regions:
        if _names_match(entry.get("libelle"), name) \
                and str(entry.get("slug", "")).endswith(f"-{code}"):
            return entry.get("slug")
    return None


def _resolve_uncached(location: dict) -> str | None:
    """Une tentative de résolution réseau, sans cache. None sur tout ce qui
    n'est pas une correspondance vérifiée — jamais d'exception levée, les
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
        logger.debug(f"[citya_geocode] Résolution échouée pour {_describe(location)}: {e}")
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


def resolve_slug_id(location: dict, repo) -> str | None:
    """Un périmètre canonique -> son slug de recherche Citya, cache d'abord.

    `repo` (un CityaGeoRepository) est obligatoire et explicite — jamais lu
    depuis `flask.current_app`, le scraping tourne sur un thread de fond
    hors contexte d'application — voir CityaParser._geo_repo().

    Un périmètre non identifiable (sans code ni nom) vaut None avec un
    warning ; un échec réseau/mémorisé vaut None sans casser le scrape des
    autres périmètres de la même recherche."""
    key = area_cache_key(location)
    if not key:
        logger.warning(
            f"[citya_geocode] Périmètre non identifiable ({_describe(location)}), "
            "résolution impossible"
        )
        return None

    if repo is None:
        logger.warning(
            "[citya_geocode] Aucun storage fourni au parser, résolution du slug "
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

    slug_id = _resolve_uncached(location)
    repo.set_cached(key, slug_id)
    if slug_id:
        logger.info(f"[citya_geocode] {_describe(location)} -> {slug_id}")
    else:
        logger.warning(f"[citya_geocode] Aucun slug trouvé pour {_describe(location)}")
    return slug_id


def remember_manual_slugs(criteria: dict, repo) -> None:
    """Mémorise des slugs saisis à la main contre le périmètre auquel ils
    correspondent, si l'association est sans ambiguïté (exactement un
    périmètre dans la recherche) — même principe que
    services.foncia_geocode.remember_manual_slugs."""
    from parsers.base import get_locations

    slugs = source_overrides(criteria, "citya").get("slugs") or []
    locations = get_locations(criteria)
    if not slugs or len(locations) != 1:
        return

    key = area_cache_key(locations[0])
    if not key:
        return

    if repo.get_cached(key) is None:
        repo.set_cached(key, str(slugs[0]))
        logger.info(f"[citya_geocode] Slug manuel banqué pour {key}: {slugs[0]}")
