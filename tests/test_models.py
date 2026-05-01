"""Tests for data models."""
import json
from datetime import datetime, timedelta

from models.listing import Listing
from models.search import Search


class TestListing:
    def test_defaults(self):
        listing = Listing(listing_id="sl_123", url="https://example.com")
        assert listing.listing_id == "sl_123"
        assert listing.source == ""
        assert listing.is_private is False
        assert listing.photos == "[]"

    def test_to_dict(self):
        listing = Listing(listing_id="sl_1", url="https://x.com", title="Test", price="500€")
        d = listing.to_dict()
        assert d["listing_id"] == "sl_1"
        assert d["title"] == "Test"
        assert d["price"] == "500€"
        assert len(d) == 29

    def test_from_dict(self):
        data = {"listing_id": "sl_2", "url": "https://x.com", "title": "Nice flat", "extra_field": "ignored"}
        listing = Listing.from_dict(data)
        assert listing.listing_id == "sl_2"
        assert listing.title == "Nice flat"
        assert not hasattr(listing, "extra_field")

    def test_full_listing(self):
        listing = Listing(
            listing_id="sl_99",
            url="https://example.com/99",
            title="Appartement T3",
            price="1200 €/mois",
            surface="65",
            rooms="3",
            location="Paris 13e",
            city="Paris",
            price_value=1200.0,
            is_private=False,
            source="seloger",
        )
        d = listing.to_dict()
        assert d["listing_id"] == "sl_99"
        assert d["price_value"] == 1200.0
        assert d["is_private"] is False


class TestSearch:
    def test_should_scrape_active(self):
        search = Search(
            id=1, user_id=1, label="Test", ntfy_topic="test",
            criteria={"placeIds": ["123"]}, scrape_interval=5,
            is_active=True, last_scraped=None, blacklist_mode="exclude",
        )
        assert search.should_scrape(datetime.utcnow()) is True

    def test_should_scrape_inactive(self):
        search = Search(
            id=1, user_id=1, label="Test", ntfy_topic="test",
            criteria={"placeIds": ["123"]}, is_active=False, blacklist_mode="exclude",
        )
        assert search.should_scrape(datetime.utcnow()) is False

    def test_should_scrape_too_soon(self):
        search = Search(
            id=1, user_id=1, label="Test", ntfy_topic="test",
            criteria={"placeIds": ["123"]}, scrape_interval=5,
            is_active=True, last_scraped=datetime.utcnow(), blacklist_mode="exclude",
        )
        assert search.should_scrape(datetime.utcnow()) is False

    def test_should_scrape_after_interval(self):
        search = Search(
            id=1, user_id=1, label="Test", ntfy_topic="test",
            criteria={"placeIds": ["123"]}, scrape_interval=5,
            is_active=True, last_scraped=datetime.utcnow() - timedelta(minutes=10), blacklist_mode="exclude",
        )
        assert search.should_scrape(datetime.utcnow()) is True

    def test_should_scrape_empty_criteria(self):
        search = Search(
            id=1, user_id=1, label="Test", ntfy_topic="test",
            criteria={}, is_active=True, blacklist_mode="exclude",
        )
        assert search.should_scrape(datetime.utcnow()) is False

    def test_should_scrape_no_placeids(self):
        search = Search(
            id=1, user_id=1, label="Test", ntfy_topic="test",
            criteria={"priceMax": 1500}, is_active=True, blacklist_mode="exclude",
        )
        assert search.should_scrape(datetime.utcnow()) is False

    def test_to_dict(self):
        search = Search(id=1, user_id=1, label="Paris", ntfy_topic="paris", blacklist_mode="exclude")
        d = search.to_dict()
        assert d["label"] == "Paris"
        assert d["criteria"] == {}
        assert d["is_active"] is True
        assert d["blacklist_mode"] == "exclude"

    def test_has_valid_criteria(self):
        search = Search(
            id=1, user_id=1, label="Test", ntfy_topic="test",
            criteria={"placeIds": ["750113"]}, blacklist_mode="exclude",
        )
        assert search.has_valid_criteria() is True

    def test_has_valid_criteria_empty(self):
        search = Search(
            id=1, user_id=1, label="Test", ntfy_topic="test",
            criteria={}, blacklist_mode="exclude",
        )
        assert search.has_valid_criteria() is False
