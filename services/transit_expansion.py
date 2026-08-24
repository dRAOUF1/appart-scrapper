"""Expansion des sélections de transports en localisations classiques (#28).

Une recherche peut porter une clé canonique `transit` (voir core.criteria) :
des lignes ferrées franciliennes, des stations éventuellement épinglées et un
rayon à vol d'oiseau. AUCUN parser ne lit cette clé : ce module la convertit
en localisations `city` classiques AVANT to_native() — le scrape se déroule
ensuite exactement comme si l'utilisateur avait choisi ces communes à la main
(les sources élargissent parfois ; matches_locations garde le contrôle final).

Sémantique d'union demandée par l'issue : une annonce remonte si elle est
dans les villes choisies OU près des stations choisies — les localisations
générées s'ajoutent donc aux localisations existantes, sans doublon.

Communes ∩ rayon :
  - le résultat d'une station×rayon ne dépend que du référentiel GTFS : il est
    mis en cache en base (`transit_communes_rayon`, clé
    « station:<stop_id>:<rayon>m », SUCCÈS-SEULS — un échec API géo est
    retenté au scrape suivant) ;
  - au premier calcul, les candidates viennent de geo.api.gouv.fr dans une
    bbox autour de la station, puis sont filtrées au haversine (Python pur).

Plafond : les locations générées sont plafonnées (PLAFOND_LOCATIONS) pour ne
jamais produire des centaines de zones qu'aucune source n'accepte (SeLoger
plafonne vers ~50 zones par requête, Laforêt ~100 communes). Priorité au
rognage : villes choisies par l'utilisateur (intouchées), puis communes des
stations ÉPINGLÉES, puis communes de lignes entières — alphabétique dans
chaque groupe. Tout rognage produit un avertissement, remonté dans les logs
et le journal de scrape.
"""

from __future__ import annotations

import math

import requests
from loguru import logger

from core.criteria import CITY, normalize_locations, normalize_transit

COMMUNES_API = "https://geo.api.gouv.fr/communes"
_TIMEOUT_S = 10

# Plafond global des localisations APRÈS union et déduplication.
PLAFOND_LOCATIONS = 60

_RAYON_TERRE_M = 6_371_000


# ---------------------------------------------------------------------------
# Géométrie pure (testée sur cas connus)
# ---------------------------------------------------------------------------


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance à vol d'oiseau entre deux points (mètres)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * _RAYON_TERRE_M * math.asin(math.sqrt(a))


def bbox_autour(lat: float, lon: float, rayon_m: float) -> tuple[float, float, float, float]:
    """La bbox (sud, ouest, nord, est) contenant à coup sûr un cercle de
    `rayon_m` centré sur le point — candidates SUR-estimées : le filtre exact
    est fait ensuite au haversine."""
    delta_lat = math.degrees(rayon_m / _RAYON_TERRE_M)
    cos_lat = max(abs(math.cos(math.radians(lat))), 1e-6)
    delta_lon = math.degrees(rayon_m / _RAYON_TERRE_M) / cos_lat
    return (
        max(-90.0, lat - delta_lat),
        max(-180.0, lon - delta_lon),
        min(90.0, lat + delta_lat),
        min(180.0, lon + delta_lon),
    )


def communes_cache_key(stop_id: str, rayon_m: int) -> str:
    """La clé de cache d'un périmètre station×rayon (même esprit `area_key`
    que les autres caches géo)."""
    return f"station:{stop_id}:{rayon_m}m"


# ---------------------------------------------------------------------------
# Communes ∩ rayon — cache base puis calcul geo.api.gouv.fr + haversine
# ---------------------------------------------------------------------------


def _interroge_communes_bbox(bbox: tuple[float, float, float, float]) -> list[dict]:
    """Les communes dont le CENTRE tombe dans la bbox (ouest,sud,est,nord au
    format geo.api.gouv.fr). Lève sur tout échec : l'appelant décide."""
    sud, ouest, nord, est = bbox
    resp = requests.get(
        COMMUNES_API,
        params={
            "bbox": f"{ouest},{sud},{est},{nord}",
            "fields": "nom,code,codesPostaux,centre",
        },
        timeout=_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _commune_de_brute(commune: dict) -> dict | None:
    """Une commune geo.api.gouv.fr -> {nom, insee, cp}, None si incomplète."""
    centre = commune.get("centre") or {}
    coords = centre.get("coordinates") or [None, None]
    lon, lat = coords[0], coords[1]
    codes_postaux = sorted(c for c in (commune.get("codesPostaux") or []) if c)
    nom = commune.get("nom")
    insee = commune.get("code")
    if not nom or not insee or not codes_postaux or lat is None or lon is None:
        return None
    return {"nom": nom, "insee": insee, "cp": codes_postaux[0], "lat": lat, "lon": lon}


def communes_dans_rayon(station: dict, rayon_m: int, storage) -> list[dict]:
    """Les communes touchées par le rayon d'une station.

    Cache hit -> renvoyé tel quel (y compris [] légitime : aucune commune
    dans le rayon). Cache miss -> calcul bbox+haversine puis mise en cache
    (succès-seuls : toute exception remonte SANS écriture, pour retenter).
    """
    cle = communes_cache_key(station["id"], rayon_m)
    en_cache = storage.transit.get_communes_cache(cle)
    if en_cache is not None:
        return en_cache

    brutes = _interroge_communes_bbox(
        bbox_autour(float(station["lat"]), float(station["lon"]), rayon_m)
    )
    communes = []
    for brute in brutes:
        commune = _commune_de_brute(brute)
        if commune is None:
            continue
        if haversine_m(float(station["lat"]), float(station["lon"]), commune["lat"], commune["lon"]) > rayon_m:
            continue
        communes.append({"nom": commune["nom"], "insee": commune["insee"], "cp": commune["cp"]})

    communes.sort(key=lambda c: (c["nom"].casefold(), c["cp"]))
    storage.transit.set_communes_cache(cle, communes)
    return communes


# ---------------------------------------------------------------------------
# Union + priorité + plafond
# ---------------------------------------------------------------------------


def _cle_location(location: dict) -> tuple:
    """L'identité d'un périmètre pour la déduplication : kind + identifiant
    géographique le plus fin disponible (inseeCode, sinon code/postalCode,
    sinon ville). Le code INSEE n'est jamais perdu : c'est lui qui fait
    fusionner « commune générée » et « même commune choisie à la main »."""
    return (
        location.get("kind", CITY),
        location.get("inseeCode")
        or location.get("code")
        or location.get("postalCode")
        or str(location.get("city") or "").casefold(),
    )


def _tri_alpha(locations: list[dict]) -> list[dict]:
    return sorted(
        locations,
        key=lambda loc: (
            str(loc.get("city") or "").casefold(),
            str(loc.get("postalCode") or ""),
        ),
    )


def etendre_locations(criteria: dict, storage) -> tuple[list[dict], list[str]]:
    """Localisations finales = villes choisies ∪ communes des transports.

    Retourne `(locations_étendues, avertissements)` : les avertissements sont
    des messages français prêts à logger (stations inconnues ignorées,
    calculs indisponibles, plafond atteint). Ne modifie jamais `criteria`.
    """
    selections = normalize_transit(criteria)
    if not selections:
        return [], []

    avertissements: list[str] = []
    epingles: dict[tuple, dict] = {}
    ligne_entiere: dict[tuple, dict] = {}

    for selection in selections:
        stop_ids = selection.get("stop_ids") or []
        if stop_ids:
            stations = storage.transit.get_stops(stop_ids)
            trouves = {s["id"] for s in stations}
            manquants = sorted(set(stop_ids) - trouves)
            if manquants:
                avertissements.append(
                    "Stations ignorées (absentes du référentiel des transports) : "
                    + ", ".join(manquants)
                )
            cible = epingles
        else:
            stations = storage.transit.get_line_stops(selection["line_id"])
            if not stations:
                avertissements.append(
                    f"Ligne « {selection['line_id']} » sans station connue — sélection ignorée"
                )
                continue
            cible = ligne_entiere

        for station in stations:
            try:
                communes = communes_dans_rayon(station, selection["radius_m"], storage)
            except Exception as exc:
                logger.warning(
                    f"[transit] Calcul impossible autour de « {station['nom']} » : {exc}"
                )
                avertissements.append(
                    f"Communes autour de « {station['nom']} » indisponibles pour l'instant"
                )
                continue
            for commune in communes:
                location = {
                    "kind": CITY,
                    "city": commune["nom"],
                    "postalCode": commune["cp"],
                    "inseeCode": commune["insee"],
                }
                cible.setdefault(_cle_location(location), location)

    # Ordre de priorité du plafond : villes choisies (jamais rognées), puis
    # communes des stations épinglées, puis communes de lignes entières.
    union: list[dict] = []
    vues: set[tuple] = set()
    for location in (
        normalize_locations(criteria)
        + _tri_alpha(list(epingles.values()))
        + _tri_alpha(list(ligne_entiere.values()))
    ):
        cle = _cle_location(location)
        if cle in vues:
            continue
        vues.add(cle)
        union.append(location)

    if len(union) > PLAFOND_LOCATIONS:
        rognees = len(union) - PLAFOND_LOCATIONS
        avertissements.insert(0, (
            f"Plafond des localisations atteint ({PLAFOND_LOCATIONS}) : "
            f"{rognees} commune(s) générée(s) par les transports ignorée(s) — "
            "priorité aux villes choisies, aux stations épinglées, puis ordre alphabétique."
        ))
        union = union[:PLAFOND_LOCATIONS]

    return union, avertissements


def etendre_criteres(criteria: dict, storage) -> tuple[dict, list[str]]:
    """Critères prêts pour les parsers : `transit` remplacé par ses
    localisations. Une recherche transit-seule ressort avec uniquement des
    locations ; une recherche sans transit sort inchangée."""
    locations, avertissements = etendre_locations(criteria, storage)
    if not normalize_transit(criteria):
        return criteria, avertissements
    etendus = {k: v for k, v in criteria.items() if k != "transit"}
    etendus["locations"] = locations
    return etendus, avertissements
