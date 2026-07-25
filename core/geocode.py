"""Résolution géographique partagée (autocomplete, codes INSEE, périmètres).

Toutes les sources passent par ici plutôt que d'appeler geo.api.gouv.fr
chacune de leur côté — un seul point de vérité sur la manière dont un lieu
saisi par l'utilisateur se traduit en périmètre exploitable.

L'autocomplete propose quatre niveaux, du plus large au plus précis :

    region      une région entière — « Île-de-France » couvre 8 départements
    department  un département — « Gironde », 534 communes
    whole_city  toute une commune, tous ses codes postaux — « Paris » couvre
                ses 20 arrondissements, « Bordeaux » ses 5 codes postaux
    city        un seul code postal — « Paris 15e », « Bordeaux 33000 »

Le niveau est porté par la localisation elle-même (clé `kind`) et chaque
source le traduit vers son propre identifiant à ce niveau : les deux sources
savent chercher sur un périmètre large en UNE requête (voir
parsers/laforet.py pour filter[departments][] et services/seloger_geocode.py
pour les placeIds AD04/AD06/AD08). Il n'est donc jamais nécessaire de
développer un périmètre en liste de communes — ce qui serait de toute façon
impossible : Laforet plafonne vers 100 communes par requête (HTTP 414) et
SeLoger vers 50 (HTTP 403), quand une région en compte plus de mille.
"""

from __future__ import annotations

import requests
from loguru import logger

GEO_API = "https://geo.api.gouv.fr"
COMMUNES_API = f"{GEO_API}/communes"
DEPARTEMENTS_API = f"{GEO_API}/departements"
REGIONS_API = f"{GEO_API}/regions"

# Les quatre niveaux de périmètre, du plus large au plus précis.
REGION = "region"
DEPARTMENT = "department"
WHOLE_CITY = "whole_city"
CITY = "city"
LOCATION_KINDS = (REGION, DEPARTMENT, WHOLE_CITY, CITY)

# INSEE code lookups never change during a process's life — cache them so a
# search scraped every few minutes forever doesn't hit the public geo API
# on every single run.
_INSEE_CACHE: dict[str, str | None] = {}

# Les départements d'une région ne changent pas non plus.
_REGION_DEPARTMENTS_CACHE: dict[str, list[str]] = {}

# Ni la ville principale d'un département (voir department_main_city).
_DEPARTMENT_MAIN_CITY_CACHE: dict[str, dict | None] = {}

# Le code postal se déduit du code département (Gironde 33 -> 33xxx, Guadeloupe
# 971 -> 971xx), SAUF en Corse : les départements 2A et 2B ont tous deux des
# codes postaux en 20xxx (vérifié via l'API : 59 codes postaux en 2A, 51 en 2B,
# tous préfixés "20"). Les deux départements corses partagent donc le même
# préfixe, et un filtrage local par préfixe ne les distingue pas — les sources,
# elles, filtrent correctement sur le code du département.
_POSTAL_PREFIX_OVERRIDES = {"2A": "20", "2B": "20"}


def postal_prefix(department_code: str) -> str:
    """Le préfixe de code postal d'un département."""
    return _POSTAL_PREFIX_OVERRIDES.get(department_code.upper(), department_code)


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


def department_main_city(department_code: str) -> dict | None:
    """La commune la plus peuplée d'un département : {"city", "postalCode"}.

    Sert à construire un chemin d'URL valide pour une source dont les pages
    sont organisées par ville alors que la recherche porte sur un département
    entier (Laforet : le chemin /ville/... est ignoré dès que
    filter[departments][] est présent, mais il doit exister — un chemin
    inventé renvoie 404). Prendre la ville principale garde l'URL lisible.

    Le tri est fait ici et non par l'API : son `boost=population` ne
    s'applique qu'à une recherche par nom, et sans lui `/communes` renvoie
    l'ordre alphabétique (soit « Abzac » pour la Gironde, au lieu de Bordeaux).
    """
    if department_code in _DEPARTMENT_MAIN_CITY_CACHE:
        return _DEPARTMENT_MAIN_CITY_CACHE[department_code]

    city = None
    try:
        communes = _query(COMMUNES_API, {
            "codeDepartement": department_code,
            "fields": "nom,codesPostaux,population",
        })
        peuplees = [c for c in communes if c.get("population") and c.get("codesPostaux")]
        if peuplees:
            top = max(peuplees, key=lambda c: c["population"])
            city = {"city": top["nom"], "postalCode": sorted(top["codesPostaux"])[0]}
    except Exception as e:
        logger.warning(f"[geocode] Ville principale introuvable pour le département {department_code}: {e}")

    _DEPARTMENT_MAIN_CITY_CACHE[department_code] = city
    return city


def region_departments(region_code: str) -> list[str]:
    """Les codes des départements d'une région, [] si indéterminable.

    Utilisé pour traduire une recherche régionale chez une source qui ne
    connaît que les départements (Laforet et son filter[departments][]).
    """
    if region_code in _REGION_DEPARTMENTS_CACHE:
        return _REGION_DEPARTMENTS_CACHE[region_code]
    try:
        resp = requests.get(f"{REGIONS_API}/{region_code}/departements", timeout=10)
        resp.raise_for_status()
        codes = [d["code"] for d in resp.json()]
    except Exception as e:
        logger.warning(f"[geocode] Départements introuvables pour la région {region_code}: {e}")
        codes = []
    if codes:
        _REGION_DEPARTMENTS_CACHE[region_code] = codes
    return codes


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------

def _query(url: str, params: dict) -> list[dict]:
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _labelled(location: dict) -> dict:
    """Ajoute le libellé d'affichage à un périmètre.

    Importé tardivement : core.criteria dépend de ce module pour les constantes
    de niveau, l'import en tête créerait un cycle.
    """
    from core.criteria import location_label

    return {**location, "label": location_label(location)}


def _region_suggestions(query: str) -> list[dict]:
    suggestions = []
    for region in _query(REGIONS_API, {"nom": query}):
        code = region.get("code")
        name = region.get("nom")
        if not code or not name:
            continue
        suggestions.append(_labelled({
            "kind": REGION,
            "name": name,
            "code": code,
            "departments": region_departments(code),
        }))
    return suggestions


def _department_suggestions(query: str) -> list[dict]:
    suggestions = []
    for dept in _query(DEPARTEMENTS_API, {"nom": query}):
        code = dept.get("code")
        name = dept.get("nom")
        if not code or not name:
            continue
        suggestions.append(_labelled({
            "kind": DEPARTMENT,
            "name": name,
            "code": code,
        }))
    return suggestions


def _commune_suggestions(commune: dict) -> list[dict]:
    """Une commune -> une entrée « toute la ville » quand elle couvre
    plusieurs codes postaux, plus une entrée par code postal.

    Une commune ordinaire n'a qu'un seul code postal : une seule entrée, et
    pas de « toute la ville » qui ferait doublon. Paris/Lyon/Marseille sont
    renvoyés par l'API comme un agrégat portant tous les codes postaux de
    leurs arrondissements sous UN seul code INSEE (75056/69123/13055) : les
    associer tel quel produirait des paires fausses (« Paris (75001) » tagué
    INSEE 75056 au lieu de 75101), d'où la résolution par arrondissement.
    """
    postal_codes = sorted(commune.get("codesPostaux") or [])
    if not postal_codes:
        return []

    city = commune.get("nom")
    insee = commune.get("code")
    centre = commune.get("centre") or {}
    coords = centre.get("coordinates") or [None, None]
    lon, lat = coords[0], coords[1]

    suggestions = []
    if len(postal_codes) > 1:
        suggestions.append(_labelled({
            "kind": WHOLE_CITY,
            "city": city,
            "inseeCode": insee,
            "postalCodes": postal_codes,
            "lat": lat,
            "lon": lon,
        }))

    for postal_code in postal_codes:
        suggestions.append(_labelled({
            "kind": CITY,
            "city": city,
            "postalCode": postal_code,
            "inseeCode": (
                _arrondissement_insee_code(postal_code) if len(postal_codes) > 1 else insee
            ) or insee,
            "lat": lat,
            "lon": lon,
        }))
    return suggestions


def _drop_redundant_departments(suggestions: list[dict]) -> list[dict]:
    """Écarte un département qui recouvre exactement une ville déjà proposée.

    Paris est à la fois une commune (INSEE 75056) et un département (75) sur
    le même territoire : les deux entrées apparaîtraient côte à côte avec le
    même libellé, sans que l'utilisateur puisse deviner laquelle choisir. On
    ne garde alors que la ville, qui porte les codes postaux et fonctionne
    pour les deux sources.
    """
    city_names = {
        s["city"].casefold()
        for s in suggestions
        if s["kind"] == WHOLE_CITY and s.get("city")
    }
    return [
        s for s in suggestions
        if not (s["kind"] == DEPARTMENT and s.get("name", "").casefold() in city_names)
    ]


def search_locations(query: str, limit: int = 20) -> list[dict]:
    """Autocomplete : un texte libre -> des périmètres de recherche.

    Interroge les trois niveaux de geo.api.gouv.fr (régions, départements,
    communes) et renvoie les suggestions du plus large au plus précis, pour
    que « paris » propose d'abord toute la ville puis chaque arrondissement,
    et que « gironde » propose le département avant les communes homonymes.

    Chaque suggestion porte son niveau (`kind`) et les champs de ce niveau :
    c'est directement le format d'une entrée de `locations` dans les critères
    canoniques (voir core.criteria).
    """
    query = query.strip()
    if len(query) < 2:
        return []

    suggestions: list[dict] = []

    # Les périmètres larges d'abord : ils sont peu nombreux et ne doivent
    # jamais être noyés par les communes (Paris seul en produit 21).
    for finder in (_region_suggestions, _department_suggestions):
        try:
            suggestions.extend(finder(query))
        except Exception as e:
            logger.warning(f"[geocode] Autocomplete {finder.__name__} échoué pour '{query}': {e}")

    try:
        communes = _query(COMMUNES_API, {
            "nom": query,
            "boost": "population",
            "fields": "nom,code,codesPostaux,centre",
            "limit": 5,
        })
    except Exception as e:
        logger.warning(f"[geocode] Autocomplete communes échoué pour '{query}': {e}")
        communes = []

    for commune in communes:
        suggestions.extend(_commune_suggestions(commune))

    suggestions = _drop_redundant_departments(suggestions)

    # Dédoublonnage sur l'identité réelle du périmètre, pas sur le libellé.
    seen: set[tuple] = set()
    unique = []
    for s in suggestions:
        key = (s["kind"], s.get("code") or s.get("postalCode") or s.get("inseeCode"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(s)

    return unique[:limit]
