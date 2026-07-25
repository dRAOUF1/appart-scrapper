"""Validation for user-supplied search payloads (criteria, scrape_interval).

Les critères arrivent dans le vocabulaire canonique (voir core.criteria) —
ou dans l'ancien vocabulaire SeLoger, qui reste accepté et converti. Cette
validation contrôle les types AVANT normalisation, pour renvoyer une erreur
parlante au client plutôt que de laisser une valeur mal typée être
silencieusement écartée par la normalisation.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, ValidationError

from core.criteria import normalize_criteria


class SearchCriteria(BaseModel):
    """Types attendus des champs connus.

    `extra="allow"` reste nécessaire : le payload peut contenir des clés de
    l'ancien vocabulaire (city/postalCode/spaceMin/...) ou une surcharge
    propre à une source. C'est normalize_criteria() qui décide ensuite ce
    qui est retenu — ce modèle ne fait que refuser les types absurdes.
    """

    model_config = ConfigDict(extra="allow")

    # --- vocabulaire canonique ---
    locations: list[dict] | None = None
    transaction: str | None = None
    propertyTypes: list[str] | None = None
    priceMin: int | None = None
    priceMax: int | None = None
    surfaceMin: int | None = None
    surfaceMax: int | None = None
    rooms: list | None = None
    bedrooms: list | None = None
    sourceOverrides: dict | None = None

    # --- ancien vocabulaire, encore accepté en entrée ---
    placeIds: list[str] | None = None
    city: str | None = None
    postalCode: str | None = None
    spaceMin: int | None = None
    spaceMax: int | None = None
    distributionTypes: list[str] | None = None
    estateTypes: list[str] | None = None
    order: str | None = None


def validate_criteria(data) -> dict:
    """Valide un payload de critères et le renvoie au format canonique.

    Lève ValueError sur un type invalide. Le résultat est du canonique pur :
    ce qui est stocké en base pour toute recherche créée ou modifiée à
    partir de maintenant (les anciennes sont normalisées à la lecture, voir
    SearchRepository._load_criteria).
    """
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("criteria doit être un objet JSON")
    try:
        model = SearchCriteria.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"Critères invalides: {e.errors()[0]['msg']}")
    return normalize_criteria(model.model_dump(exclude_none=True))


def validate_scrape_interval(value, minimum: int = 1, maximum: int = 1440) -> int:
    """Validate scrape_interval (minutes). Raises ValueError on bad input."""
    try:
        interval = int(value)
    except (TypeError, ValueError):
        raise ValueError("scrape_interval doit être un entier")
    if interval < minimum or interval > maximum:
        raise ValueError(f"scrape_interval doit être entre {minimum} et {maximum} minutes")
    return interval
