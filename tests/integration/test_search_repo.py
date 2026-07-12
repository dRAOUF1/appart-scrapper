"""Integration tests for SearchRepository against a real Postgres."""

from __future__ import annotations

import time

import pytest

from models.listing import Listing

pytestmark = pytest.mark.integration


def _make_user(storage):
    return storage.users.create_user(f"user_{time.time_ns()}")


class TestCreateAndGetSearch:
    def test_round_trip_preserves_jsonb_criteria(self, storage):
        user = _make_user(storage)
        criteria = {"placeIds": ["750113"], "priceMax": 1500, "rooms": ["2", "3"]}

        created = storage.searches.create_search(
            user["id"], "Paris 13e", "topic-x", "seloger", criteria, 10,
        )
        fetched = storage.searches.get_search(created["id"])

        assert fetched is not None
        assert fetched["criteria"] == criteria
        assert fetched["label"] == "Paris 13e"
        assert fetched["scrape_interval"] == 10


class TestDeleteSearchCascade:
    def test_deleting_search_cascades_to_search_listings(self, storage):
        user = _make_user(storage)
        search = storage.searches.create_search(
            user["id"], "Test", "topic", "seloger", {"placeIds": ["1"]}, 5,
        )
        listing = Listing(listing_id="sl_cascade", url="https://example.com/sl_cascade")
        storage.listings.save_and_link([listing], search["id"])

        deleted = storage.searches.delete_search(search["id"])
        assert deleted is True

        conn = storage._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM search_listings WHERE search_id = %s",
                    (search["id"],),
                )
                count = cur.fetchone()[0]
        finally:
            storage._release_conn(conn)

        assert count == 0
        assert storage.searches.get_search(search["id"]) is None
