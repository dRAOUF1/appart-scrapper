"""Shared test fixtures et garde-fous globaux de la suite.

Garde-fou principal : `DATABASE_URL` est purgé de l'environnement pour toute la
durée du run. Cette variable pointe la base de PRODUCTION et peut atterrir dans
`os.environ` par simple effet de bord d'import (un `load_dotenv()` au niveau
module). Les tests d'intégration TRUNCATE toutes les tables : ils lisent
exclusivement `TEST_DATABASE_URL` (voir tests/integration/conftest.py).
"""

import os

import pytest


def pytest_configure(config):
    """Purge DATABASE_URL avant la collecte des tests."""
    os.environ.pop("DATABASE_URL", None)


def pytest_collection_finish(session):
    """Re-purge après la collecte : un import de test peut avoir appelé load_dotenv()."""
    os.environ.pop("DATABASE_URL", None)


@pytest.fixture(autouse=True, scope="session")
def _no_production_database_url():
    """Filet de sécurité : aucune étape de test ne doit voir DATABASE_URL."""
    os.environ.pop("DATABASE_URL", None)
    yield


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
            "locations": [{"city": "Paris", "postalCode": "75013", "inseeCode": "75113"}],
            "priceMax": 1500,
            "transaction": "rent",
            "propertyTypes": ["apartment"],
        },
        "scrape_interval": 5,
        "is_active": True,
        "blacklist_mode": "exclude",
    }
