"""Constructeurs d'objets de test.

Chaque fonction renvoie une valeur *valide par défaut* et accepte des
surcharges par mot-clé. Un test ne précise donc que ce qui compte pour lui, et
un nouveau champ obligatoire ne casse pas 200 tests.

    listing = make_listing(price_value=1200.0)
    row = make_search_row(criteria={"priceMax": 900})
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta

from models.listing import Listing

_ids = itertools.count(1)


def next_id() -> int:
    """Identifiant croissant, unique au sein d'un run."""
    return next(_ids)


# ---------------------------------------------------------------------------
# Critères canoniques (voir core/criteria.py)
# ---------------------------------------------------------------------------

def make_city_location(city: str = "Paris", postal_code: str = "75013", insee: str = "75113") -> dict:
    return {"kind": "city", "city": city, "postalCode": postal_code, "inseeCode": insee}


def make_whole_city_location(
    city: str = "Poitiers",
    postal_codes: tuple[str, ...] = ("86000",),
    insee: str = "86194",
) -> dict:
    return {"kind": "whole_city", "city": city, "postalCodes": list(postal_codes), "inseeCode": insee}


def make_department_location(code: str = "33", name: str = "Gironde") -> dict:
    return {"kind": "department", "code": code, "name": name}


def make_region_location(
    code: str = "75",
    name: str = "Nouvelle-Aquitaine",
    departments: tuple[str, ...] = ("33", "40"),
) -> dict:
    return {"kind": "region", "code": code, "name": name, "departments": list(departments)}


def make_criteria(**overrides) -> dict:
    """Critères canoniques valides : une ville, location, appartement."""
    criteria = {
        "locations": [make_city_location()],
        "transaction": "rent",
        "propertyTypes": ["apartment"],
        "priceMax": 1500,
    }
    criteria.update(overrides)
    return criteria


# ---------------------------------------------------------------------------
# Annonces
# ---------------------------------------------------------------------------

def make_listing(**overrides) -> Listing:
    """Annonce complète et cohérente (`price` et `price_value` s'accordent)."""
    listing_id = overrides.pop("listing_id", None) or f"sl_{next_id()}"
    defaults = {
        "listing_id": listing_id,
        "url": f"https://www.seloger.com/annonces/{listing_id}.htm",
        "title": "Appartement 3 pièces 65 m²",
        "price": "1 200 €/mois",
        "price_value": 1200.0,
        "surface": "65",
        "rooms": "3",
        "location": "Paris 13e",
        "city": "Paris",
        "zip_code": "75013",
        "source": "seloger",
        "agency": "Agence Test",
        "property_type": "apartment",
        "phone": "[]",
        "photos": "[]",
    }
    defaults.update(overrides)
    return Listing(**defaults)


def make_listing_row(**overrides) -> dict:
    """Annonce sous forme de ligne DB (dict), telle que la renvoient les repos."""
    return make_listing(**overrides).to_dict()


def make_photos_json(count: int = 2) -> str:
    """Colonne `photos` sérialisée, au format produit par les parsers."""
    return json.dumps(
        [{"url": f"https://cdn.example/p{i}.jpg?sig=abc", "alt": f"Photo {i}", "key": f"p{i}"} for i in range(count)]
    )


# ---------------------------------------------------------------------------
# Recherches et utilisateurs (lignes DB, telles que les routes les manipulent)
# ---------------------------------------------------------------------------

def make_search_row(**overrides) -> dict:
    """Ligne de `searches` normalisée, telle que la renvoie SearchRepository."""
    defaults = {
        "id": 1,
        "user_id": 1,
        "label": "Paris 13e T2-T3",
        "ntfy_topic": "test-topic",
        "source": "seloger",
        "sources": ["seloger"],
        "criteria": make_criteria(),
        "scrape_interval": 5,
        "last_scraped": None,
        "created_at": datetime(2026, 1, 1, 12, 0, 0),
        "is_active": True,
        "blacklisted_agencies": [],
        "blacklist_mode": "exclude",
    }
    defaults.update(overrides)
    return defaults


def make_user_row(**overrides) -> dict:
    defaults = {
        "id": 1,
        "username": "alice",
        "created_at": datetime(2026, 1, 1, 12, 0, 0),
    }
    defaults.update(overrides)
    return defaults


def make_scrape_log_entry(**overrides) -> dict:
    """Entrée de log de scrape, au format JSONL de scrape_logs.storage."""
    started = overrides.pop("started_at", datetime(2026, 7, 1, 10, 0, 0))
    defaults = {
        "id": next_id(),
        "search_id": 1,
        "status": "success",
        "listings_found": 12,
        "new_listings": 3,
        "error_message": "",
        "details": {},
        "started_at": started,
        "completed_at": started + timedelta(seconds=30),
        "duration_sec": 30,
        "raw_logs_file": "",
    }
    defaults.update(overrides)
    return defaults
