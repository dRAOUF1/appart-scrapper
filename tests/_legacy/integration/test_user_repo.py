"""Integration tests for UserRepository against a real Postgres."""

from __future__ import annotations

import time

import pytest

from models.listing import Listing

pytestmark = pytest.mark.integration


class TestUniqueness:
    def test_duplicate_username_raises_value_error(self, storage):
        username = f"dup_{time.time_ns()}"
        storage.users.create_user(username)

        with pytest.raises(ValueError):
            storage.users.create_user(username)

    def test_created_user_has_unique_token(self, storage):
        u1 = storage.users.create_user(f"u1_{time.time_ns()}")
        u2 = storage.users.create_user(f"u2_{time.time_ns()}")
        assert u1["api_token"] != u2["api_token"]


class TestDeleteUserCascade:
    def test_deleting_user_cascades_to_searches_and_search_listings(self, storage):
        user = storage.users.create_user(f"cascade_{time.time_ns()}")
        search = storage.searches.create_search(
            user["id"], "Test", "topic", "seloger", {"placeIds": ["1"]}, 5,
        )
        listing = Listing(listing_id="sl_user_cascade", url="https://example.com/sl_user_cascade")
        storage.listings.save_and_link([listing], search["id"])

        deleted = storage.users.delete_user(user["id"])
        assert deleted is True

        assert storage.searches.get_search(search["id"]) is None

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
