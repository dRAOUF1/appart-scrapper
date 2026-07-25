"""Le vocabulaire canonique des critères de recherche.

C'est LE contrat entre le front, la base et les parsers : l'utilisateur
définit ses critères une seule fois, dans ce vocabulaire-là, et chaque
source les traduit ensuite vers son propre format (voir
BaseParser.to_native). Ajouter une source ne demande donc jamais de
toucher au front ni au schéma stocké — seulement d'écrire sa traduction.

Aucun terme de ce vocabulaire n'est emprunté à une source en particulier.
C'était précisément le problème d'avant : les critères étaient stockés dans
le vocabulaire SeLoger (`distributionTypes: ["Rent"]`, `estateTypes:
["Apartment"]`, `placeIds`), donc SeLoger n'avait rien à traduire tandis
que toute autre source devait apprendre à parler SeLoger.

Le vocabulaire :

    locations     [{city, postalCode, inseeCode, lat, lon}] — le code INSEE
                  et les coordonnées viennent de l'autocomplete
                  (core.geocode) et sont ce qui permet à chaque source de
                  retrouver son propre identifiant de lieu
    transaction   "rent" | "buy"
    propertyTypes ["apartment", "house", "parking", "land"]
    priceMin/Max  entiers, en euros (loyer mensuel ou prix de vente selon
                  `transaction`)
    surfaceMin/Max  entiers, en m²
    rooms         [int] — nombre de pièces ; 5 signifie "5 et plus"
    bedrooms      [int] — idem pour les chambres

Une seule échappatoire, explicitement rangée à part :

    sourceOverrides  {"<source>": {...}} — les valeurs propres à une source
                     que l'utilisateur a fournies à la main. Aujourd'hui
                     uniquement le placeId SeLoger collé en repli quand la
                     résolution automatique ne trouve rien (voir
                     services/seloger_geocode). Elles sont ici plutôt qu'au
                     premier niveau pour que le canonique reste neutre, et
                     pour qu'une source future puisse avoir la sienne sans
                     rien polluer.

`normalize_criteria()` accepte AUSSI l'ancien vocabulaire et le convertit :
les recherches déjà en base ne sont pas migrées, elles sont normalisées à
la lecture. Une recherche créée avant ce changement continue donc de
tourner sans intervention, et repasse au canonique dès qu'elle est rééditée.
"""

from __future__ import annotations

# --- Transactions ---------------------------------------------------------
RENT = "rent"
BUY = "buy"
TRANSACTIONS = (RENT, BUY)

TRANSACTION_LABELS = {RENT: "Location", BUY: "Achat"}

# --- Types de bien --------------------------------------------------------
APARTMENT = "apartment"
HOUSE = "house"
PARKING = "parking"
LAND = "land"
PROPERTY_TYPES = (APARTMENT, HOUSE, PARKING, LAND)

PROPERTY_TYPE_LABELS = {
    APARTMENT: "Appartement",
    HOUSE: "Maison",
    PARKING: "Parking",
    LAND: "Terrain",
}

# --- Correspondances depuis l'ancien vocabulaire --------------------------
# L'ancien format stockait les valeurs SeLoger telles quelles. La casse est
# ignorée à la lecture (les valeurs SeLoger sont capitalisées, le canonique
# est en minuscules).
_TRANSACTION_ALIASES = {
    "rent": RENT,
    "sale": BUY,       # valeur SeLoger pour l'achat
    "buy": BUY,
}

_PROPERTY_TYPE_ALIASES = {
    "apartment": APARTMENT,
    "house": HOUSE,
    "parking": PARKING,
    "land": LAND,
}

# Clés de l'ancien format qui sont en réalité propres à SeLoger : elles
# partent dans sourceOverrides["seloger"] au lieu d'être perdues.
_SELOGER_OWN_KEYS = ("placeIds", "locationsInBuildingExcluded")


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _to_int(value) -> int | None:
    """None si la valeur n'est pas convertible — un critère illisible est
    ignoré plutôt que de faire échouer toute la recherche."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_locations(criteria: dict) -> list[dict]:
    """Les localisations, depuis `locations` ou l'ancien couple à plat
    city/postalCode. `inseeCode`/`lat`/`lon` sont conservés quand ils sont
    là (l'autocomplete les fournit) et simplement absents sinon — une
    localisation tapée à la main reste exploitable par les sources qui se
    contentent de ville + code postal.
    """
    raw = _as_list(criteria.get("locations"))
    if not raw:
        city, postal_code = criteria.get("city"), criteria.get("postalCode")
        if city and postal_code:
            raw = [{"city": city, "postalCode": postal_code}]

    locations = []
    for loc in raw:
        if not isinstance(loc, dict):
            continue
        city = (loc.get("city") or "").strip()
        postal_code = str(loc.get("postalCode") or "").strip()
        if not city or not postal_code:
            continue
        normalized = {"city": city, "postalCode": postal_code}
        for optional in ("inseeCode", "lat", "lon"):
            if loc.get(optional) is not None:
                normalized[optional] = loc[optional]
        locations.append(normalized)
    return locations


def _normalize_transaction(criteria: dict) -> str | None:
    """`transaction` canonique, ou l'ancien `distributionTypes: ["Rent"]`.

    L'ancien format était une liste parce que SeLoger l'accepte, mais une
    recherche ne mélange jamais location et achat (le front n'a jamais
    proposé que l'un ou l'autre) — on ne garde donc que la première valeur.
    """
    value = criteria.get("transaction")
    if value is None:
        value = next(iter(_as_list(criteria.get("distributionTypes"))), None)
    if not isinstance(value, str):
        return None
    return _TRANSACTION_ALIASES.get(value.strip().lower())


def _normalize_property_types(criteria: dict) -> list[str]:
    """`propertyTypes` canonique, ou l'ancien `estateTypes`. Les valeurs
    non reconnues sont écartées (elles ne correspondraient à rien chez
    aucune source)."""
    raw = _as_list(criteria.get("propertyTypes")) or _as_list(criteria.get("estateTypes"))
    types = []
    for value in raw:
        if not isinstance(value, str):
            continue
        canonical = _PROPERTY_TYPE_ALIASES.get(value.strip().lower())
        if canonical and canonical not in types:
            types.append(canonical)
    return types


def _normalize_counts(values) -> list[int]:
    """rooms/bedrooms en entiers triés et dédoublonnés. L'ancien format les
    stockait en chaînes (`["1", "2"]`, venant des cases à cocher du
    formulaire), le canonique les veut en entiers pour que les sources
    puissent comparer sans reconvertir."""
    counts = set()
    for value in _as_list(values):
        as_int = _to_int(value)
        if as_int is not None:
            counts.add(as_int)
    return sorted(counts)


def _normalize_source_overrides(criteria: dict) -> dict:
    """Les surcharges par source, depuis `sourceOverrides` ou depuis les
    clés SeLoger de l'ancien format restées au premier niveau."""
    overrides: dict[str, dict] = {}

    existing = criteria.get("sourceOverrides")
    if isinstance(existing, dict):
        for source, values in existing.items():
            if isinstance(values, dict) and values:
                overrides[source] = dict(values)

    legacy = {
        key: criteria[key]
        for key in _SELOGER_OWN_KEYS
        if criteria.get(key)
    }
    if legacy:
        overrides.setdefault("seloger", {}).update(legacy)

    return overrides


def normalize_criteria(criteria: dict | None) -> dict:
    """Des critères dans n'importe quel format accepté -> le canonique.

    Idempotent : normaliser des critères déjà canoniques les rend
    inchangés. Les clés absentes ou illisibles sont simplement omises,
    jamais devinées.
    """
    if not criteria or not isinstance(criteria, dict):
        return {}

    normalized: dict = {}

    locations = normalize_locations(criteria)
    if locations:
        normalized["locations"] = locations

    transaction = _normalize_transaction(criteria)
    if transaction:
        normalized["transaction"] = transaction

    property_types = _normalize_property_types(criteria)
    if property_types:
        normalized["propertyTypes"] = property_types

    for canonical_key, legacy_key in (
        ("priceMin", None),
        ("priceMax", None),
        ("surfaceMin", "spaceMin"),
        ("surfaceMax", "spaceMax"),
    ):
        value = criteria.get(canonical_key)
        if value is None and legacy_key:
            value = criteria.get(legacy_key)
        as_int = _to_int(value)
        if as_int is not None:
            normalized[canonical_key] = as_int

    for key in ("rooms", "bedrooms"):
        counts = _normalize_counts(criteria.get(key))
        if counts:
            normalized[key] = counts

    overrides = _normalize_source_overrides(criteria)
    if overrides:
        normalized["sourceOverrides"] = overrides

    return normalized


def source_overrides(criteria: dict, source: str) -> dict:
    """Les surcharges manuelles que l'utilisateur a fournies pour `source`.

    À n'utiliser que par le parser de cette source : c'est son repli quand
    la traduction automatique du canonique ne suffit pas.
    """
    overrides = criteria.get("sourceOverrides")
    if not isinstance(overrides, dict):
        return {}
    values = overrides.get(source)
    return values if isinstance(values, dict) else {}


def with_source_override(criteria: dict, source: str, values: dict) -> dict:
    """Copie de `criteria` avec les surcharges de `source` mises à jour.

    Ne modifie pas l'original : les critères d'une recherche sont partagés
    entre toutes ses sources pendant un scrape, la traduction de l'une ne
    doit jamais fuiter dans celle d'une autre.
    """
    updated = dict(criteria)
    overrides = {
        key: dict(existing)
        for key, existing in (updated.get("sourceOverrides") or {}).items()
    }
    if values:
        overrides.setdefault(source, {}).update(values)
        updated["sourceOverrides"] = overrides
    return updated
