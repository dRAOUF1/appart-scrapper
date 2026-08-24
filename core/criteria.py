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

    locations     une liste de périmètres, chacun portant son niveau (`kind`,
                  voir core.geocode) :
                    {kind: "region",     name, code, departments[]}
                    {kind: "department", name, code}
                    {kind: "whole_city", city, postalCodes[], inseeCode}
                    {kind: "city",       city, postalCode, inseeCode}
                  Le code INSEE et les coordonnées viennent de l'autocomplete
                  et sont ce qui permet à chaque source de retrouver son
                  propre identifiant de lieu. Une entrée sans `kind` vaut
                  "city" : c'est le format d'avant les périmètres larges.
    transit       une liste de sélections de transports en commun (issue #28,
                  façon Jinka) :
                    {mode: "tram"|"metro"|"rer"|"train",
                     line_id: "<route_id GTFS>",
                     stop_ids: ["<station GTFS>", ...],
                     radius_m: 500|1000|2000}
                  `stop_ids` vide signifie « toute la ligne ». Cette clé reste
                  au vocabulaire NEUTRE : aucun parser ne la lit jamais — le
                  service d'expansion (services/transit_expansion.py) la
                  convertit en localisations classiques AVANT to_native().
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

from core.geocode import CITY, DEPARTMENT, REGION, WHOLE_CITY

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

# --- Transports en commun (issue #28) --------------------------------------
# Les modes ferrés du référentiel GTFS francilien (voir scripts/import_transit).
TRANSIT_MODES = ("tram", "metro", "rer", "train")
# Rayons proposés à la saisie (à vol d'oiseau depuis la station). Tout rayon
# illisible ou hors liste retombe sur le défaut : le formulaire ne propose
# que ces valeurs, mais un payload retouché à la main ne doit jamais produire
# un périmètre fantaisiste.
TRANSIT_RADII = (500, 1000, 2000)
TRANSIT_RADIUS_DEFAULT = 1000


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


def _normalize_wide_location(loc: dict, kind: str) -> dict | None:
    """Une région ou un département : identifié par son code, pas par une
    ville. Sans code, le périmètre est inexploitable et la localisation est
    écartée."""
    code = str(loc.get("code") or "").strip()
    if not code:
        return None
    normalized = {"kind": kind, "code": code}
    name = (loc.get("name") or "").strip()
    if name:
        normalized["name"] = name
    if kind == REGION:
        # Les départements de la région, mémorisés à la saisie pour ne pas
        # avoir à réinterroger l'API géo à chaque scrape (les sources qui ne
        # connaissent que les départements en ont besoin).
        departments = [str(d).strip() for d in _as_list(loc.get("departments")) if str(d).strip()]
        if departments:
            normalized["departments"] = departments
    return normalized


def _normalize_whole_city(loc: dict) -> dict | None:
    """Toute une commune : tous ses codes postaux d'un coup."""
    city = (loc.get("city") or "").strip()
    postal_codes = [str(cp).strip() for cp in _as_list(loc.get("postalCodes")) if str(cp).strip()]
    if not city or not postal_codes:
        return None
    normalized = {"kind": WHOLE_CITY, "city": city, "postalCodes": sorted(set(postal_codes))}
    for optional in ("inseeCode", "lat", "lon"):
        if loc.get(optional) is not None:
            normalized[optional] = loc[optional]
    return normalized


def _normalize_city(loc: dict) -> dict | None:
    """Un seul code postal — le niveau par défaut, et le seul qui existait
    avant l'introduction des périmètres larges."""
    city = (loc.get("city") or "").strip()
    postal_code = str(loc.get("postalCode") or "").strip()
    if not city or not postal_code:
        return None
    normalized = {"kind": CITY, "city": city, "postalCode": postal_code}
    for optional in ("inseeCode", "lat", "lon"):
        if loc.get(optional) is not None:
            normalized[optional] = loc[optional]
    return normalized


def normalize_locations(criteria: dict) -> list[dict]:
    """Les localisations, depuis `locations` ou l'ancien couple à plat
    city/postalCode.

    Chaque entrée porte son niveau de périmètre (`kind`) : region,
    department, whole_city ou city (voir core.geocode). Une entrée sans
    `kind` est traitée comme `city` — c'est le format d'avant l'introduction
    des périmètres larges, et les recherches déjà enregistrées continuent
    donc de fonctionner sans migration.

    `inseeCode`/`lat`/`lon` sont conservés quand ils sont là (l'autocomplete
    les fournit) et simplement absents sinon — une localisation tapée à la
    main reste exploitable par les sources qui se contentent de ville + code
    postal.
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
        kind = (loc.get("kind") or CITY).strip().lower()
        if kind in (REGION, DEPARTMENT):
            normalized = _normalize_wide_location(loc, kind)
        elif kind == WHOLE_CITY:
            normalized = _normalize_whole_city(loc)
        elif kind == CITY:
            normalized = _normalize_city(loc)
        else:
            normalized = None  # niveau inconnu : jamais deviné
        if normalized:
            locations.append(normalized)
    return locations


def location_postal_prefixes(location: dict) -> list[str]:
    """Les préfixes de code postal couverts par une localisation.

    Sert à vérifier localement qu'une annonce est bien dans le périmètre
    demandé, quel que soit le niveau — une source peut élargir d'elle-même
    (Laforet inclut la métropole autour d'une commune) et ce contrôle est le
    garde-fou. Un préfixe vide n'est jamais renvoyé : ce serait « tout code
    postal accepté », l'inverse du but recherché.
    """
    from core.geocode import postal_prefix

    kind = location.get("kind", CITY)
    if kind == CITY:
        return [location["postalCode"]] if location.get("postalCode") else []
    if kind == WHOLE_CITY:
        return list(location.get("postalCodes") or [])
    if kind == DEPARTMENT:
        code = location.get("code")
        return [postal_prefix(code)] if code else []
    if kind == REGION:
        return [postal_prefix(d) for d in (location.get("departments") or []) if d]
    return []


def location_label(location: dict) -> str:
    """Le périmètre en clair, tel qu'on l'affiche à l'utilisateur.

    Même formulation partout : suggestions de l'autocomplete, champ du
    formulaire, étiquettes des cartes de recherche.
    """
    kind = location.get("kind", CITY)
    if kind == REGION:
        return f"{location.get('name') or location.get('code')} (région)"
    if kind == DEPARTMENT:
        code = location.get("code")
        name = location.get("name") or code
        return f"{name} ({code}) — tout le département" if code else str(name)
    if kind == WHOLE_CITY:
        count = len(location.get("postalCodes") or [])
        return f"{location.get('city')} — toute la ville ({count} codes postaux)"
    return f"{location.get('city')} ({location.get('postalCode')})"


def matches_locations(postal_code: str, locations: list[dict]) -> bool:
    """Le code postal d'une annonce tombe-t-il dans l'un des périmètres ?

    Faux si le code postal est absent ou si aucun périmètre ne le couvre :
    on n'accorde jamais le bénéfice du doute sur la localisation (contrairement
    au prix ou à la surface, une annonce dont on ne sait pas situer le bien
    n'a pas à être remontée).
    """
    if not postal_code:
        return False
    for location in locations:
        for prefix in location_postal_prefixes(location):
            if postal_code.startswith(prefix):
                return True
    return False


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


def _normalize_transit(criteria: dict) -> list[dict]:
    """Les sélections de transports en commun, normalisées.

    Une entrée valide porte au minimum `line_id` : sans elle, rien n'est
    exploitable ni chez nous ni dans le GTFS — l'entrée est écartée, jamais
    devinée. Les champs secondaires sont assouplis :
      - mode illisible -> clé simplement omise (la ligne reste cherchable) ;
      - rayon hors liste -> fallback TRANSIT_RADIUS_DEFAULT (comportement
        demandé par l'issue, documenté à la saisie) ;
      - stop_ids coercés en chaînes non vides, dédoublonnés et triés (une
        station cochée deux fois ne doit pas produire deux périmètres).
    Deux entrées portant la même ligne sont dédupliquées (première gagnante) :
    le formulaire ne peut pas les produire, mais un payload édité à la main
    si — une seule sélection par ligne garde le canonique prévisible.
    """
    entries: list[dict] = []
    seen_lines: set[str] = set()
    for entry in _as_list(criteria.get("transit")):
        if not isinstance(entry, dict):
            continue
        line_id = str(entry.get("line_id") or "").strip()
        if not line_id or line_id in seen_lines:
            continue
        seen_lines.add(line_id)

        normalized: dict = {"line_id": line_id}

        mode = entry.get("mode")
        if isinstance(mode, str) and mode.strip().lower() in TRANSIT_MODES:
            normalized["mode"] = mode.strip().lower()

        stop_ids = sorted({
            str(stop).strip()
            for stop in _as_list(entry.get("stop_ids"))
            if str(stop).strip()
        })
        if stop_ids:
            normalized["stop_ids"] = stop_ids

        radius = _to_int(entry.get("radius_m"))
        normalized["radius_m"] = radius if radius in TRANSIT_RADII else TRANSIT_RADIUS_DEFAULT

        entries.append(normalized)
    return entries


def normalize_transit(criteria: dict | None) -> list[dict]:
    """Les sélections `transit` des critères, normalisées — [] si absentes.

    Point d'entrée public pour tout ce qui lit cette clé (expansion,
    formulaire, hydratation) : personne d'autre ne doit interpréter le brut.
    """
    if not criteria or not isinstance(criteria, dict):
        return []
    return _normalize_transit(criteria)


def has_transit(criteria: dict | None) -> bool:
    """Au moins une sélection de transport valide ? Sert au contrat « une
    recherche transit-seule est valide » (issue #28) : les sources recevront
    des localisations classiques après expansion."""
    return bool(normalize_transit(criteria))


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

    # Issue #28 : les sélections transit sont normalisées mais la clé n'est
    # AJOUTÉE que si non vide — comme les autres clés, une absence ne devient
    # jamais un champ vide en base (rétrocompatibilité totale des critères
    # existants, idempotence garantie). La lecture passe par
    # normalize_transit(), qui renvoie [] sur l'absence.
    transit = _normalize_transit(criteria)
    if transit:
        normalized["transit"] = transit

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
