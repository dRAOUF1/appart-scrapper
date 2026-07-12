"""Integration tests for ListingRepository against a real Postgres."""

from __future__ import annotations

import time

import pytest

from models.listing import Listing

pytestmark = pytest.mark.integration


def _make_search(storage):
    user = storage.users.create_user(f"user_{time.time_ns()}")
    search = storage.searches.create_search(
        user["id"], "Test search", "test-topic", "seloger",
        {"placeIds": ["123"]}, 5,
    )
    return user, search


def _listing(listing_id="sl_1", agency="Agence A"):
    return Listing(listing_id=listing_id, url=f"https://example.com/{listing_id}", agency=agency)


class TestSaveAndLink:
    def test_new_listing_is_linked_and_unnotified(self, storage):
        _, search = _make_search(storage)
        new_listings, already = storage.listings.save_and_link([_listing()], search["id"])

        assert len(new_listings) == 1
        assert len(already) == 0

        unnotified = storage.listings.get_unnotified_listings_for_search(search["id"])
        assert [l.listing_id for l in unnotified] == ["sl_1"]

    def test_linking_same_listing_twice_does_not_duplicate(self, storage):
        _, search = _make_search(storage)
        storage.listings.save_and_link([_listing()], search["id"])
        new_listings, already = storage.listings.save_and_link([_listing()], search["id"])

        # Second call: PRIMARY KEY(search_id, listing_id) already exists.
        assert len(new_listings) == 0
        assert len(already) == 1

        conn = storage._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM search_listings WHERE search_id = %s AND listing_id = %s",
                    (search["id"], "sl_1"),
                )
                count = cur.fetchone()[0]
        finally:
            storage._release_conn(conn)
        assert count == 1

    def test_mark_listings_notified_removes_them_from_unnotified(self, storage):
        _, search = _make_search(storage)
        storage.listings.save_and_link([_listing()], search["id"])

        storage.listings.mark_listings_notified(search["id"], ["sl_1"])

        unnotified = storage.listings.get_unnotified_listings_for_search(search["id"])
        assert unnotified == []

    def test_two_searches_linking_same_listing_each_get_their_own_unnotified_state(self, storage):
        user = storage.users.create_user(f"user_{time.time_ns()}")
        search_a = storage.searches.create_search(user["id"], "A", "topic-a", "seloger", {"placeIds": ["1"]}, 5)
        search_b = storage.searches.create_search(user["id"], "B", "topic-b", "seloger", {"placeIds": ["1"]}, 5)

        storage.listings.save_and_link([_listing()], search_a["id"])
        storage.listings.save_and_link([_listing()], search_b["id"])
        storage.listings.mark_listings_notified(search_a["id"], ["sl_1"])

        assert storage.listings.get_unnotified_listings_for_search(search_a["id"]) == []
        assert len(storage.listings.get_unnotified_listings_for_search(search_b["id"])) == 1


class TestDeleteOldListings:
    def test_make_interval_syntax_executes_without_error(self, storage):
        """Regression: INTERVAL '%s days' used to be invalid SQL (psycopg2
        quotes inside the literal). make_interval() must run cleanly."""
        _, search = _make_search(storage)
        storage.listings.save_and_link([_listing()], search["id"])

        deleted = storage.listings.delete_old_listings(days=4)

        assert deleted == 0  # just-created listing isn't old enough yet

    def test_deletes_listings_older_than_cutoff(self, storage):
        _, search = _make_search(storage)
        storage.listings.save_and_link([_listing()], search["id"])

        conn = storage._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE listings SET first_seen = NOW() - INTERVAL '10 days' WHERE listing_id = %s",
                    ("sl_1",),
                )
            conn.commit()
        finally:
            storage._release_conn(conn)

        deleted = storage.listings.delete_old_listings(days=4)
        assert deleted == 1
