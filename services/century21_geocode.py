"""Century 21 slug resolution — périmètre canonique -> slug d'URL.

Contrairement au placeId de SeLoger ou au zoneId de bienici, le slug de
localisation de Century 21 est opaque mais non chiffré : c'est l'identifiant
d'URL de la page de résultats (`v-paris`, `cp-75001`, `cpv-69003_villeurbanne`,
`v-st+etienne`), renvoyé par l'autocomplete public du site (trouvé par
network-capture de la barre de recherche, vérifié en direct le 15/08/2026,
sans session ni authentification) :

    GET https://www.century21.fr/autocomplete/localite/?q=<texte ou code>
    -> [{"id": "v-paris", "name": "PARIS", "cp": "75015", ...},
        {"id": "cp-75015", "name": "PARIS (75015)", "cp": "75015", ...}, ...]

Ce slug ne se dérive PAS du code INSEE par formule : Century 21 normalise
lui-même les noms (« SAINT-ÉTIENNE » devient « ST ETIENNE » -> slug
`v-st+etienne`), il faut donc toujours l'interroger — jamais de
slugification locale.

Century 21 connaît trois niveaux de périmètre résolus ici :

    city        un code postal précis — commune ordinaire (`v-montrouge`) ou
                arrondissement de Paris/Lyon/Marseille (`cp-75015`, seul moyen
                de viser un arrondissement précis)
    whole_city  toute une commune (`v-paris`) — résolu par NOM (le code postal
                d'une ville multi-arrondissements trié renverrait le premier
                arrondissement, pas la ville entière)
    department  un département entier (`d-92_hauts_de_seine`) — l'autocomplete
                renvoie une entrée `d-{code}` à l'id INCOMPLET (`d-92`) ; le
                suffixe de nom se dérive du libellé affiché (partie après
                « code - »), voir _pick_department_slug

Deux dérogations vérifiées en direct (15/08/2026) : la Corse — 2A/2B n'ont
AUCUNE entrée autocomplete, le site utilise 201/202
(_DEPARTMENT_CODE_OVERRIDES) — et Paris, dont le département 75 n'a pas
d'entrée `d-75` : repli sur la première ville entière (`v-paris`).

Une RÉGION n'a pas de slug propre : l'autocomplete ne propose aucune région
(vérifié : `q=ile de france` -> `[]`). C'est le parser qui élargit la région
à ses départements avant de résoudre (comme Laforêt) — jamais ici, et jamais
en liste de communes.

Un même code postal peut couvrir deux villes (69003 = Lyon 3e ET
Villeurbanne) : la résolution croise donc toujours le code postal avec le NOM
de la ville de la localisation canonique, avec un repli sur la première
candidature de même code postal quand les noms ne correspondent pas (cas des
noms abrégés par le site, ex. Saint-Étienne).
"""

from __future__ import annotations

import re
import unicodedata

import requests
from loguru import logger

from core.geocode import CITY, DEPARTMENT, REGION, WHOLE_CITY

SUGGEST_URL = "https://www.century21.fr/autocomplete/localite/"
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# Un échec de résolution est réessayé après ce délai — même politique que
# services/seloger_geocode.py et services/bienici_geocode.py.
_RETRY_COOLDOWN_SECONDS = 7 * 24 * 3600

# La Corse (codes 2A/2B) n'a AUCUNE entrée autocomplete : le site référence
# ses départements sous leurs codes postaux 201/202 (vérifié en direct :
# q=2A -> [], q=201 -> « 201 - Corse-du-Sud »). La requête autocomplete d'un
# département passe donc par ce code de substitution.
_DEPARTMENT_CODE_OVERRIDES = {"2A": "201", "2B": "202"}


def _query_autocomplete(text: str) -> list[dict]:
    """L'autocomplete de Century 21 pour un texte ou un code postal.

    Le Referer + X-Requested-With sont requis : sans eux l'endpoint répond
    par un corps vide (vérifié en direct). Une réponse inattendue (pas une
    liste) est traitée comme vide plutôt que de lever — l'échec sera mémorisé
    comme tel par l'appelant."""
    if not text or len(text) < 2:
        return []
    resp = requests.get(
        SUGGEST_URL,
        params={"q": text},
        headers={
            "User-Agent": DESKTOP_UA,
            "Accept": "*/*",
            "Referer": "https://www.century21.fr/",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _normalize_name(text: str) -> str:
    """Un nom de localité pour comparaison : majuscules, sans accents, sans
    ponctuation, espaces resserrés. Le « (75015) » accolé par les entrées de
    code postal est retiré : « PARIS (75015) » et « Paris » convergent tous
    deux vers « PARIS »."""
    stripped = re.sub(r"\(\d{2,5}\)", "", text)
    normalized = unicodedata.normalize("NFKD", stripped)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    compact = "".join(c if c.isalnum() else " " for c in ascii_text)
    return " ".join(compact.split()).upper()


def _names_match(entry_name: str, city_name: str) -> bool:
    """Le nom affiché par l'autocomplete correspond-il à la ville demandée ?
    (Compare « PARIS (75015) » à « Paris » ; écarte « LYON (69003) » quand on
    cherche Villeurbanne, même code postal.)"""
    if not entry_name or not city_name:
        return False
    return _normalize_name(entry_name) == _normalize_name(city_name)


def _pick_city_slug(results: list[dict], postal_code: str, city_name: str) -> str | None:
    """Le slug Century 21 d'un code postal précis.

    1. Un arrondissement exact (`cp-75015`) quand son nom correspond à la
       ville — seul moyen de viser Paris/Lyon/Marseille arrondissement par
       arrondissement.
    2. Sinon une ville (`v-*`/`cpv-*`) du même code postal dont le nom
       correspond (Villeurbanne 69003 : `v-villeurbanne`, jamais `v-lyon`
       pourtant aussi de code 69003).
    3. En repli, la première ville de ce code postal (cas des noms abrégés
       par le site, ex. « SAINT-ÉTIENNE » -> « ST ETIENNE », que l'étape 2
       ne peut pas rapprocher) — un code postal partagé dont aucun nom ne
       correspond est rarissime, et le contrôle de périmètre en aval
       (`matches_locations`) garde le résultat honnête."""
    exact_cp = f"cp-{postal_code}"
    for r in results:
        if r.get("id") == exact_cp and _names_match(r.get("name", ""), city_name):
            return exact_cp

    for r in results:
        if (
            r.get("id", "").startswith(("v-", "cpv-"))
            and r.get("cp") == postal_code
            and _names_match(r.get("name", ""), city_name)
        ):
            return r["id"]

    for r in results:
        if r.get("id", "").startswith("v-") and r.get("cp") == postal_code:
            return r["id"]

    return None


def _pick_whole_city_slug(results: list[dict]) -> str | None:
    """Le slug de toute une commune : la première entrée `v-*` de
    l'autocomplete par nom — c'est l'entrée « ville entière », distincte des
    arrondissements `cp-*` qui suivent (vérifié en direct : « Paris » ->
    `v-paris`, puis `cp-75001`... `cp-75020`)."""
    for r in results:
        if r.get("id", "").startswith("v-"):
            return r["id"]
    return None


def _department_query_code(code: str) -> str:
    """Le code à requêter dans l'autocomplete pour un département canonique :
    la Corse par son code postal de substitution, le reste tel quel."""
    return _DEPARTMENT_CODE_OVERRIDES.get(code, code)


def _slug_suffix_from_label(label: str, query_code: str) -> str | None:
    """« 92 - Hauts-de-Seine » + « 92 » -> « hauts_de_seine ».

    Le suffixe est la partie du libellé après « {code} - », normalisée comme
    le site lui-même dans ses slugs (minuscules sans accents, tout séparateur
    en underscore). None si le libellé n'a pas la forme attendue."""
    prefix = f"{query_code} - "
    if not label.startswith(prefix):
        return None
    normalized = unicodedata.normalize("NFKD", label[len(prefix):])
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    segments = [s.lower() for s in re.split(r"[^A-Za-z0-9]+", ascii_text) if s]
    return "_".join(segments) or None


def _pick_department_slug(results: list[dict], query_code: str) -> str | None:
    """Le slug COMPLET d'un département (`d-33_gironde`).

    L'autocomplete renvoie l'entrée à l'id INCOMPLET (« d-33 », sans nom) et
    le slug nu est refusé par le site (/annonces/f/achat/d-33/ renvoie 410,
    vérifié en direct, contre 200 pour /d-33_gironde/) : le suffixe de nom se
    dérive du libellé affiché via _slug_suffix_from_label."""
    exact = f"d-{query_code}"
    for r in results:
        if r.get("id") != exact:
            continue
        suffix = _slug_suffix_from_label(r.get("name", ""), query_code)
        if not suffix:
            return None
        return f"{exact}_{suffix}"
    return None


def area_cache_key(location: dict) -> str | None:
    """La clé de cache identifiant le périmètre — même convention exacte que
    services.seloger_geocode.area_cache_key et services.bienici_geocode.
    area_cache_key : les sources partagent le vocabulaire de périmètres
    canoniques, pas de raison que leurs clés diffèrent.

    Century 21 ne résolvant que les villes (city/whole_city), seules ces deux
    clés sont réellement utilisées : une région ou un département n'aura
    jamais de slug et restera non supporté (voir la docstring du module)."""
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
            return _pick_city_slug(results, location.get("postalCode"), location.get("city") or "")
        if kind == WHOLE_CITY:
            results = _query_autocomplete(location.get("city") or "")
            return _pick_whole_city_slug(results)
        if kind == DEPARTMENT:
            code = location.get("code")
            if not code:
                return None
            query_code = _department_query_code(code)
            results = _query_autocomplete(query_code)
            dept_slug = _pick_department_slug(results, query_code)
            if dept_slug:
                return dept_slug
            # Pas d'entrée département (Paris : l'autocomplete ne propose
            # ni « d-75 » ni « d-75056 », seulement la ville entière et ses
            # arrondissements, vérifié en direct) : repli sur la première
            # ville — pour Paris, une seule commune couvre tout le
            # département.
            return _pick_whole_city_slug(results)
        # Une région n'a pas de slug propre (aucune entrée autocomplete,
        # vérifié en direct : `q=ile de france` -> []) : c'est le parser qui
        # l'élargit à ses départements avant de résoudre chacun — jamais ici,
        # jamais en liste de communes.
        return None
    except Exception as e:
        logger.debug(f"[century21_geocode] Résolution échouée pour {_describe(location)}: {e}")
        return None


def resolve_slug_id(location: dict, repo) -> str | None:
    """Un périmètre canonique -> son slug Century 21, cache d'abord.

    `repo` (un Century21GeoRepository) est obligatoire et explicite : jamais
    lu depuis `flask.current_app`, le scraping tourne sur un thread de fond
    hors contexte d'application — voir Century21Parser._geo_repo()."""
    key = area_cache_key(location)
    if not key:
        logger.warning(
            f"[century21_geocode] Périmètre non identifiable ({_describe(location)}), "
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
        logger.info(f"[century21_geocode] {_describe(location)} -> {slug_id}")
    else:
        logger.warning(f"[century21_geocode] Aucun slug trouvé pour {_describe(location)}")
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

    slugs = source_overrides(criteria, "century21").get("slugs") or []
    locations = get_locations(criteria)
    if not slugs or len(locations) != 1:
        return

    key = area_cache_key(locations[0])
    if not key:
        return

    if repo.get_cached(key) is None:
        repo.set_cached(key, str(slugs[0]))
        logger.info(f"[century21_geocode] Slug manuel banqué pour {key}: {slugs[0]}")
