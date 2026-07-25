"""Integration tests for SelogerGeoRepository against a real Postgres."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


class TestGetCachedSetCached:
    def test_never_attempted_returns_none(self, storage):
        assert storage.seloger_geo.get_cached("75115") is None

    def test_round_trips_a_resolved_place_id(self, storage):
        storage.seloger_geo.set_cached("75115", "AD08FR31096")
        row = storage.seloger_geo.get_cached("75115")
        assert row["insee_code"] == "75115"
        assert row["place_id"] == "AD08FR31096"
        assert row["resolved_at"] is not None

    def test_failed_resolution_is_cached_as_null_not_absent(self, storage):
        """A crawl that couldn't find a placeId still writes a row (place_id
        NULL) — distinguishing "tried and failed" from "never tried", so
        the resolver can apply a retry cooldown instead of re-crawling
        every single scrape cycle."""
        storage.seloger_geo.set_cached("99999", None)
        row = storage.seloger_geo.get_cached("99999")
        assert row is not None
        assert row["place_id"] is None

    def test_set_cached_upserts_on_conflict(self, storage):
        storage.seloger_geo.set_cached("75115", None)
        storage.seloger_geo.set_cached("75115", "AD08FR31096")
        row = storage.seloger_geo.get_cached("75115")
        assert row["place_id"] == "AD08FR31096"
