"""Listing model — real estate listing dataclass."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class Listing:
    """Represents a single real estate listing."""

    listing_id: str
    url: str
    title: str = ""
    price: str = ""
    surface: str = ""
    rooms: str = ""
    location: str = ""
    image_url: str = ""
    description: str = ""
    agency: str = ""
    source: str = ""
    legacy_id: str = ""
    price_value: float | None = None
    price_details: str = ""
    city: str = ""
    district: str = ""
    zip_code: str = ""
    property_type: str = ""
    is_private: bool = False
    phone: str = "[]"
    epc: str = ""
    ges: str = ""
    is_new: bool = False
    is_exclusive: bool = False
    has_3d_visit: bool = False
    # Issue #12 : date de PUBLICATION de l'annonce chez sa source (pas la
    # date de récupération par le scraper, portée par first_seen et
    # search_listings.found_at). Toujours en ISO-8601 UTC canonique
    # (« YYYY-MM-DDTHH:MM:SS+00:00 », cf. parsers/_dates.py) ou la sentinelle
    # « unknown » quand la source ne fournit rien — jamais de chaîne vide.
    creation_date: str = "unknown"
    update_date: str = ""
    headline: str = ""
    photos: str = "[]"
    # Issue #26 : géolocalisation hybride. Remplies au scrape par
    # l'extraction native de la source (parsers/_coords.py) ou, à défaut, par
    # le fallback centre de commune (services/geocode_commune.py — précision
    # 'commune'). NULL/'' = pas de coordonnées : l'annonce reste valide,
    # simplement absente de la carte. Jamais 0.0/0.0 (piège essetpm).
    latitude: float | None = None
    longitude: float | None = None
    location_precision: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Listing:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
