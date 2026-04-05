"""Listing model — real estate listing dataclass."""

from __future__ import annotations

from dataclasses import dataclass, asdict


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
    creation_date: str = ""
    update_date: str = ""
    headline: str = ""
    photos: str = "[]"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Listing":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
