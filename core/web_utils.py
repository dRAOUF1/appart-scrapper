"""Small helpers shared by the route blueprints."""

from __future__ import annotations

from core.criteria import (
    PROPERTY_TYPE_LABELS,
    TRANSACTION_LABELS,
    location_label,
)


def to_int(value, default: int) -> int:
    """Parse value as int, falling back to default on any invalid input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bornes_lisibles(borne_min: int | None, borne_max: int | None, unite: str) -> str | None:
    """Une borne double en texte français (« 800 – 1 200 € »), None si vide.

    Une seule borne reste explicite sur son sens (« 900 € (max) ») : une
    valeur seule « 900 € » serait ambiguë entre plancher et plafond."""
    if borne_min is None and borne_max is None:
        return None
    if borne_min is not None and borne_max is not None:
        return f"{borne_min} – {borne_max} {unite}"
    seul = borne_min if borne_min is not None else borne_max
    sens = "min" if borne_min is not None else "max"
    return f"{seul} {unite} ({sens})"


def _compteurs_lisibles(values: list[int] | None) -> str | None:
    """Des compteurs triés en texte (« 2, 3, 5+ »), None si absent.

    Le 5 du formulaire signifie « 5 et plus » : il porte le suffixe « + »."""
    if not values:
        return None
    return ", ".join(f"{v}+" if v == 5 else str(v) for v in values)


def format_criteria_lisible(criteria: dict | None) -> list[tuple[str, str]]:
    """Les critères canoniques en lignes françaises lisibles (issue #18).

    Remplace l'affichage JSON brut de la fiche admin : une liste de paires
    (libellé, valeur), une par critère présent, prête à itérer en Jinja.
    Le vocabulaire reste celui du contrat central — cette fonction ne fait
    que le TRADUIRE en clair, elle n'en introduit aucun terme nouveau.

    Les localisations sont affichées ENTIÈRES, code INSEE compris : c'est
    un écran de debug admin, masquer l'identifiant qui permet à chaque
    source de retrouver son lieu serait se priver de l'info utile quand
    un scrape revient vide. Les surcharges par source (sourceOverrides)
    sont listées pour la même raison.
    """
    if not criteria or not isinstance(criteria, dict):
        return []

    lignes: list[tuple[str, str]] = []

    for loc in criteria.get("locations") or []:
        label = location_label(loc)
        insee = loc.get("inseeCode")
        lignes.append(("Localisation", f"{label} · INSEE {insee}" if insee else label))

    transaction = criteria.get("transaction")
    if transaction:
        lignes.append(("Transaction", TRANSACTION_LABELS.get(transaction, transaction)))

    types = [PROPERTY_TYPE_LABELS.get(t, t) for t in criteria.get("propertyTypes") or []]
    if types:
        lignes.append(("Types de bien", ", ".join(types)))

    prix = _bornes_lisibles(criteria.get("priceMin"), criteria.get("priceMax"), "€")
    if prix:
        lignes.append(("Prix", prix))
    surface = _bornes_lisibles(criteria.get("surfaceMin"), criteria.get("surfaceMax"), "m²")
    if surface:
        lignes.append(("Surface", surface))

    pieces = _compteurs_lisibles(criteria.get("rooms"))
    if pieces:
        lignes.append(("Pièces", pieces))
    chambres = _compteurs_lisibles(criteria.get("bedrooms"))
    if chambres:
        lignes.append(("Chambres", chambres))

    for source, valeurs in (criteria.get("sourceOverrides") or {}).items():
        detail = ", ".join(f"{k}={', '.join(map(str, v)) if isinstance(v, list) else v}"
                           for k, v in valeurs.items())
        lignes.append((f"Surcharges ({source})", detail))

    return lignes
