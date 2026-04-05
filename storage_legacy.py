"""Legacy Listing class — kept for backward compatibility with parsers/scraper.

New code should use models.listing.Listing instead.
"""

from __future__ import annotations


class Listing:
    """Represents a single real estate listing."""

    def __init__(
        self,
        listing_id: str,
        url: str,
        title: str = "",
        price: str = "",
        surface: str = "",
        rooms: str = "",
        location: str = "",
        image_url: str = "",
        description: str = "",
        agency: str = "",
        source: str = "",
        legacy_id: str = "",
        price_value: float | None = None,
        price_details: str = "",
        city: str = "",
        district: str = "",
        zip_code: str = "",
        property_type: str = "",
        is_private: bool = False,
        phone: str = "[]",
        epc: str = "",
        ges: str = "",
        is_new: bool = False,
        is_exclusive: bool = False,
        has_3d_visit: bool = False,
        creation_date: str = "",
        update_date: str = "",
        headline: str = "",
        photos: str = "[]",
    ):
        self.listing_id = listing_id
        self.url = url
        self.title = title
        self.price = price
        self.surface = surface
        self.rooms = rooms
        self.location = location
        self.image_url = image_url
        self.description = description
        self.agency = agency
        self.source = source
        self.legacy_id = legacy_id
        self.price_value = price_value
        self.price_details = price_details
        self.city = city
        self.district = district
        self.zip_code = zip_code
        self.property_type = property_type
        self.is_private = is_private
        self.phone = phone
        self.epc = epc
        self.ges = ges
        self.is_new = is_new
        self.is_exclusive = is_exclusive
        self.has_3d_visit = has_3d_visit
        self.creation_date = creation_date
        self.update_date = update_date
        self.headline = headline
        self.photos = photos

    def __repr__(self) -> str:
        return f"Listing({self.listing_id}, {self.title}, {self.price})"

    def to_dict(self) -> dict:
        return {
            "listing_id": self.listing_id,
            "url": self.url,
            "title": self.title,
            "price": self.price,
            "surface": self.surface,
            "rooms": self.rooms,
            "location": self.location,
            "image_url": self.image_url,
            "description": self.description,
            "agency": self.agency,
            "source": self.source,
            "legacy_id": self.legacy_id,
            "price_value": self.price_value,
            "price_details": self.price_details,
            "city": self.city,
            "district": self.district,
            "zip_code": self.zip_code,
            "property_type": self.property_type,
            "is_private": self.is_private,
            "phone": self.phone,
            "epc": self.epc,
            "ges": self.ges,
            "is_new": self.is_new,
            "is_exclusive": self.is_exclusive,
            "has_3d_visit": self.has_3d_visit,
            "creation_date": self.creation_date,
            "update_date": self.update_date,
            "headline": self.headline,
            "photos": self.photos,
        }
