"""Shared test fixtures."""
import pytest


@pytest.fixture
def sample_listing_data():
    return {
        "listing_id": "sl_12345",
        "url": "https://www.seloger.com/annonces/12345.htm",
        "title": "Appartement 3 pièces 65m²",
        "price": "1 200 €/mois",
        "surface": "65",
        "rooms": "3",
        "location": "Paris 13e",
        "source": "seloger",
        "city": "Paris",
        "price_value": 1200.0,
    }


@pytest.fixture
def sample_search_data():
    return {
        "id": 1,
        "user_id": 1,
        "label": "Paris 13e T2-T3",
        "ntfy_topic": "test-topic",
        "source": "seloger",
        "criteria": {
            "placeIds": ["750113"],
            "priceMax": 1500,
            "distributionTypes": ["Rent"],
            "estateTypes": ["Apartment"],
        },
        "scrape_interval": 5,
        "is_active": True,
    }
