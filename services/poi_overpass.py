"""Couches POI contextuelles de la carte (#27) — proxy Overpass + cache bbox.

La carte des biens (#26) peut superposer des couches de points d'intérêt façon
Google Maps : le MVP n'ajoute que TRANSPORTS (arrêts bus/tram/métro/gares/
ferries via OpenStreetMap), mais le mécanisme est GÉNÉRIQUE.

Architecture extensible — une couche = UNE entrée du registre `COUCHES_POI` :
    {"ecoles": {
        "label": "Écoles", "icone": "🏫",
        "requete_overpass": 'node["amenity"="school"];',
    }}
… et RIEN d'autre : la route proxy lit le registre, le front construit son
contrôle Calques depuis le catalogue JSON (`GET /listings/poi`) et rend chaque
point par layer-id/type. Aucun schéma, aucune API existante modifiée.

Pourquoi un proxy Flask plutôt qu'Overpass en direct depuis le navigateur :
pas de CORS à négocier, un seul point pour y mettre le cache par bbox (les
limites de rate d'Overpass sont réelles), et un timeout court côté serveur.

Contrats de robustesse :
- La bbox du client est VALIDÉE strictement puis ARRONDIE (~3 décimales) avant
  requête : deux pans de carte quasi identiques partagent la même clé de cache.
- Tout échec Overpass (timeout, 5xx, JSON malformé) est AVALLÉ ici : la couche
  revient vide avec `degrade=True`, jamais d'exception vers la route — la carte
  reste utilisable sans la couche.
- Les échecs ne sont PAS mis en cache : un incident transitoire ne doit pas
  être servi pendant tout le TTL.
- Cache mémoire process (dict + verrou thread, TTL ~10 min, éviction simple) :
  suffisant pour un service mono-worker (gunicorn --workers 1), sans table DB.
"""

from __future__ import annotations

import math
import threading
import time

import requests
from loguru import logger

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Timeout COURT : la couche POI est un confort, jamais un bloquant de l'UI.
_TIMEOUT_S = 8
_TTL_CACHE_S = 600.0
_TAILLE_CACHE_MAX = 64
_PLAFOND_POINTS = 300
_ARRONDI_BBOX = 3
# Plafond d'amplitude par axe, exposé : le message d'erreur de la route le cite.
AMPLITUDE_MAX_DEGRES = 0.5

Bbox = tuple[float, float, float, float]  # (sud, ouest, nord, est)


class PointPoi(dict):
    """Point normalisé renvoyé au front : {lat, lon, nom, type}."""


# ---------------------------------------------------------------------------
# Registre des couches — AJOUTER UNE COUCHE = AJOUTER UNE ENTRÉE ICI
# ---------------------------------------------------------------------------

COUCHES_POI: dict[str, dict[str, str]] = {
    "transports": {
        "label": "Transports",
        "icone": "🚌",
        # Nodes uniquement : arrêts et gares OSM pertinents sont des nodes ;
        # le typage fin (bus/tram/metro/gare/ferry) est fait au parsing, ce qui
        # évite quatre requêtes distinctes et surcharge moins la réponse.
        "requete_overpass": (
            'node["highway"~"^(bus_stop|tram_stop)$"];'
            'node["railway"~"^(station|halt|tram_stop)$"];'
            'node["station"="subway"];'
            'node["amenity"="ferry_terminal"];'
        ),
    },
}


_LIBELLES_TYPES = {
    "bus": "Arrêt de bus",
    "tram": "Arrêt de tram",
    "metro": "Station de métro",
    "gare": "Gare",
    "ferry": "Terminal ferry",
}

# Tri « pertinence » : les nœuds structurants d'abord (gare > métro > tram >
# ferry > bus), puis alphabétique — le plafond garde ainsi les plus utiles.
_ORDRE_TRI = {"gare": 0, "metro": 1, "tram": 2, "ferry": 3, "bus": 4}


# ---------------------------------------------------------------------------
# Validation de bbox
# ---------------------------------------------------------------------------


def parse_bbox(raw: str | None) -> Bbox | None:
    """Valide `sud,ouest,nord,est` et renvoie la bbox ARRONDIE, None sinon.

    Strict : 4 floats finis, ordonnés SANS dégénérescence (sud < nord,
    ouest < est), bornes mondiales respectées, amplitude plafonnée par axe
    (AMPLITUDE_MAX_DEGRES) — une bbox planétaire ne doit jamais partir chez
    Overpass. L'arrondi (~3 décimales, ~100 m) maximise les hits de cache.
    """
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) != 4:
        return None
    try:
        valeurs = [float(part) for part in parts]
    except ValueError:
        return None
    if not all(math.isfinite(valeur) for valeur in valeurs):
        return None

    sud, ouest, nord, est = valeurs
    if not (-90 <= sud < nord <= 90):
        return None
    if not (-180 <= ouest < est <= 180):
        return None
    if nord - sud > AMPLITUDE_MAX_DEGRES or est - ouest > AMPLITUDE_MAX_DEGRES:
        return None

    return (
        round(sud, _ARRONDI_BBOX),
        round(ouest, _ARRONDI_BBOX),
        round(nord, _ARRONDI_BBOX),
        round(est, _ARRONDI_BBOX),
    )


# ---------------------------------------------------------------------------
# Requête Overpass QL
# ---------------------------------------------------------------------------


def _construit_requete(requete_couche: str, bbox: Bbox) -> str:
    """Assemble l'en-tête QL (bbox globale arrondie) + clauses + sortie plafonnée."""
    sud, ouest, nord, est = bbox
    entete = f"[out:json][timeout:{_TIMEOUT_S}][bbox:{sud},{ouest},{nord},{est}];"
    return entete + requete_couche + f"out body {_PLAFOND_POINTS};"


def _type_point(tags: dict) -> str | None:
    """Regroupe les tags OSM en type canonique du front — None si hors périmètre."""
    if tags.get("station") == "subway":
        return "metro"
    railway = tags.get("railway")
    highway = tags.get("highway")
    if railway == "tram_stop" or highway == "tram_stop":
        return "tram"
    if railway in ("station", "halt"):
        return "gare"
    if highway == "bus_stop":
        return "bus"
    if tags.get("amenity") == "ferry_terminal":
        return "ferry"
    return None


def _normalise_element(element: dict) -> PointPoi | None:
    """Un node Overpass -> PointPoi ; None si incomplet ou hors typage.

    Un nom manquant retombe sur le libellé du type (« Arrêt de bus ») : mieux
    qu'un popup vide, et le front n'a aucun fallback à coder.
    """
    if not isinstance(element, dict):
        return None
    tags = element.get("tags")
    if not isinstance(tags, dict):
        return None
    type_point = _type_point(tags)
    if type_point is None:
        return None
    try:
        lat = float(element["lat"])
        lon = float(element["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None

    return PointPoi(
        lat=lat,
        lon=lon,
        nom=tags.get("name") or _LIBELLES_TYPES[type_point],
        type=type_point,
    )


def _trie_et_plafonne(points: list[PointPoi]) -> list[PointPoi]:
    points.sort(key=lambda point: (_ORDRE_TRI.get(point["type"], len(_ORDRE_TRI)), point["nom"]))
    del points[_PLAFOND_POINTS:]
    return points


# ---------------------------------------------------------------------------
# Client HTTP — échecs avallés, JAMAIS d'exception propagée
# ---------------------------------------------------------------------------


def _interroge_overpass(requete: str) -> tuple[list[dict], bool]:
    """Exécute la requête -> (elements bruts, ok).

    `ok=False` signale un échec transport/status/parsing : la route répondra
    200 avec `degrade=True` plutôt qu'un 500 — l'échec upstream n'est pas une
    erreur de NOTRE API.
    """
    try:
        reponse = requests.post(
            OVERPASS_URL,
            data={"data": requete},
            timeout=_TIMEOUT_S,
            headers={"User-Agent": "appart-scrapper/1.0 (carte couches POI)"},
        )
        reponse.raise_for_status()
        payload = reponse.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning(f"[poi_overpass] Requête Overpass échouée : {exc}")
        return [], False

    elements = payload.get("elements") if isinstance(payload, dict) else None
    if not isinstance(elements, list):
        logger.warning("[poi_overpass] Réponse Overpass inattendue : « elements » absent")
        return [], False
    return elements, True


# ---------------------------------------------------------------------------
# Cache bbox mémoire — verrou thread, TTL, éviction simple
# ---------------------------------------------------------------------------

# clé bbox arrondie -> (instant monotonic de mise en cache, points)
_CACHE: dict[Bbox, tuple[float, list[PointPoi]]] = {}
_CACHE_LOCK = threading.Lock()


def vider_cache() -> None:
    """Vide le cache POI (tests, ou purge manuelle)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _cache_get(cle: Bbox) -> list[PointPoi] | None:
    """Points en cache et encore frais, None sinon (et entrée expirée purgée)."""
    with _CACHE_LOCK:
        entree = _CACHE.get(cle)
        if entree is None:
            return None
        instant, points = entree
        if time.monotonic() - instant < _TTL_CACHE_S:
            return points
        del _CACHE[cle]
        return None


def _cache_put(cle: Bbox, points: list[PointPoi]) -> None:
    """Mise en cache avec éviction FIFO simple à la taille max."""
    with _CACHE_LOCK:
        while len(_CACHE) >= _TAILLE_CACHE_MAX:
            plus_ancienne = min(_CACHE, key=lambda k: _CACHE[k][0])
            del _CACHE[plus_ancienne]
        _CACHE[cle] = (time.monotonic(), points)


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------


def recuperer_poi(couche_id: str, bbox: Bbox) -> tuple[list[PointPoi], bool]:
    """Points d'une couche dans la bbox -> (points, degrade).

    `degrade=True` = Overpass indisponible : liste vide, message discret côté
    front, carte intacte. Un résultat en cache n'est JAMAIS dégradé ; un échec
    n'est JAMAIS mis en cache. Couche inconnue : ValueError (appel interne
    invalide — la route valide avant).
    """
    couche = COUCHES_POI.get(couche_id)
    if couche is None:
        raise ValueError(f"Couche POI inconnue : {couche_id}")

    cle = tuple(bbox)
    en_cache = _cache_get(cle)
    if en_cache is not None:
        return list(en_cache), False

    elements, ok = _interroge_overpass(_construit_requete(couche["requete_overpass"], cle))
    points = _trie_et_plafonne([point for point in map(_normalise_element, elements) if point])
    if ok:
        _cache_put(cle, points)
    else:
        logger.warning(f"[poi_overpass] Couche « {couche_id} » servie dégradée pour bbox={cle}")
    return points, not ok
