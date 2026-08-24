"""Helpers partagés d'extraction des coordonnées natives des sources (#26).

Chaque source expose ses coordonnées sous son propre vocabulaire, avec ses
propres pièges (voir les docstrings de parsers/bienici.py, orpi.py, foncia.py
et essetpm.py) — mais la VALIDATION, elle, est la même partout :

* une valeur illisible (None, '', 'abc', NaN, infini) vaut ABSENCE de
  donnée, jamais 0 ;
* 0 est la SENTINELLE « pas de position » chez plusieurs sources
  (essetpm écrit null OU 0.0/0.0) : une composante exactement nulle est donc
  rejetée. Pour de l'immobilier français, une latitude OU une longitude
  exactement 0.0 n'est jamais une vraie position — les API émettent 6 à 7
  décimales, et le (0, 0) « golfe de Guinée » est le marqueur classique des
  coordonnées manquantes mal filtrées ;
* hors bornes (±90 / ±180) : rejeté, jamais supposé corrigé.

Le résultat d'une extraction ratée n'est PAS une erreur : une annonce sans
coordonnées reste valide, elle sera simplement absente de la carte (#26).
"""

from __future__ import annotations

import math

# Valeurs de listings.location_precision — le vocabulaire de précision
# canonique de la feature carte :
#   'exacte'          : position du bien fournie par la source ;
#   'approximative'   : position floutée par la source (bienici disque 50 m,
#                       ORPI blurredness) — rendue en cercle translucide côté UI ;
#   'commune'         : fallback centre de commune geo.api.gouv.fr
#                       (services/geocode_commune.py), faute de mieux.
PRECISION_EXACTE = "exacte"
PRECISION_APPROXIMATIVE = "approximative"
PRECISION_COMMUNE = "commune"

_LAT_MIN, _LAT_MAX = -90.0, 90.0
_LON_MIN, _LON_MAX = -180.0, 180.0


def _vers_float(value) -> float | None:
    """La valeur en float si c'est un nombre honnête, sinon None.

    Les chaînes numériques ('43.6') sont acceptées — certaines sources
    sérialisent leurs positions. `bool` est refusé : True/False ne sont pas
    des coordonnées, même si Python les somme volontiers à des nombres.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        nombre = float(value)
    elif isinstance(value, str):
        try:
            nombre = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    # NaN et infinis passent isinstance(float) : ils ne sont pas des positions.
    return nombre if math.isfinite(nombre) else None


def coordonnee_valide(value, *, min_bound: float, max_bound: float) -> float | None:
    """Une composante utilisable, ou None.

    Rejette l'illisible, l'infini, le ZÉRO (sentinelle « pas de position »,
    cf. docstring du module) et ce qui sort des bornes géographiques.
    """
    nombre = _vers_float(value)
    if nombre is None or nombre == 0.0:
        return None
    if nombre < min_bound or nombre > max_bound:
        return None
    return nombre


def extraire_coordonnees(latitude, longitude) -> tuple[float, float] | None:
    """Le couple (latitude, longitude) validé, ou None.

    LES DEUX composantes doivent être exploitables : une seule suffit à
    placer un point faux, qui est pire qu'aucun point (l'utilisateur irait
    regarder un quartier erroné plutôt que rien). C'est ici que meurt le
    piège essetpm — absence en DEUX formes, null ET 0.0/0.0.
    """
    lat = coordonnee_valide(latitude, min_bound=_LAT_MIN, max_bound=_LAT_MAX)
    lon = coordonnee_valide(longitude, min_bound=_LON_MIN, max_bound=_LON_MAX)
    if lat is None or lon is None:
        return None
    return (lat, lon)
