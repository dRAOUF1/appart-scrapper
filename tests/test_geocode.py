"""Tests for core/geocode.py — shared location resolution/autocomplete."""
from unittest.mock import MagicMock, patch

import pytest

from core.geocode import (
    _arrondissement_insee_code,
    resolve_insee_code,
    search_locations,
)


@pytest.fixture(autouse=True)
def _clear_insee_cache():
    from core.geocode import _INSEE_CACHE
    _INSEE_CACHE.clear()
    yield
    _INSEE_CACHE.clear()


class TestArrondissementInseeCode:
    """Paris/Lyon/Marseille arrondissements need a special code (e.g.
    Laforet's filter[cities][]) — formulas verified against Laforet's own
    embedded page state (see parsers/laforet.py module docstring), not
    guessed: 75014->75114, 69007->69387, 13001->13201, etc."""

    def test_paris(self):
        assert _arrondissement_insee_code("75014") == "75114"
        assert _arrondissement_insee_code("75001") == "75101"
        assert _arrondissement_insee_code("75020") == "75120"

    def test_lyon(self):
        assert _arrondissement_insee_code("69001") == "69381"
        assert _arrondissement_insee_code("69007") == "69387"
        assert _arrondissement_insee_code("69009") == "69389"

    def test_marseille(self):
        assert _arrondissement_insee_code("13001") == "13201"
        assert _arrondissement_insee_code("13008") == "13208"
        assert _arrondissement_insee_code("13016") == "13216"

    def test_non_special_cased_postal_code_returns_none(self):
        assert _arrondissement_insee_code("86000") is None
        assert _arrondissement_insee_code("44000") is None

    def test_out_of_range_or_malformed_returns_none(self):
        assert _arrondissement_insee_code("75000") is None
        assert _arrondissement_insee_code("7500") is None
        assert _arrondissement_insee_code("abcde") is None


class TestResolveInseeCode:
    def test_special_case_never_hits_the_network(self):
        with patch("core.geocode.requests.get") as mock_get:
            code = resolve_insee_code("75014")
        assert code == "75114"
        mock_get.assert_not_called()

    def test_general_case_uses_the_public_geo_api(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = [{"code": "86194"}]
        resp.raise_for_status.return_value = None
        with patch("core.geocode.requests.get", return_value=resp) as mock_get:
            code = resolve_insee_code("86000")
        assert code == "86194"
        mock_get.assert_called_once()

    def test_result_is_cached(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = [{"code": "86194"}]
        resp.raise_for_status.return_value = None
        with patch("core.geocode.requests.get", return_value=resp) as mock_get:
            resolve_insee_code("86000")
            resolve_insee_code("86000")
        mock_get.assert_called_once()

    def test_returns_none_on_network_error(self):
        with patch("core.geocode.requests.get", side_effect=Exception("boom")):
            assert resolve_insee_code("99999") is None

    def test_returns_none_when_no_commune_matches(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = []
        resp.raise_for_status.return_value = None
        with patch("core.geocode.requests.get", return_value=resp):
            assert resolve_insee_code("99999") is None


class TestSearchLocations:
    """Autocomplete: city name -> canonical location suggestions. Backs the
    unified "just type Paris" search-creation UI."""

    def test_short_query_returns_empty_without_network_call(self):
        with patch("core.geocode.requests.get") as mock_get:
            assert search_locations("p") == []
        mock_get.assert_not_called()

    def test_regular_single_postal_code_commune(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = [{
            "nom": "Nantes", "code": "44109",
            "codesPostaux": ["44000"],
            "centre": {"type": "Point", "coordinates": [-1.5603, 47.2382]},
        }]
        resp.raise_for_status.return_value = None
        with patch("core.geocode.requests.get", return_value=resp):
            suggestions = search_locations("nantes")
        assert suggestions == [{
            "label": "Nantes (44000)",
            "city": "Nantes",
            "postalCode": "44000",
            "inseeCode": "44109",
            "lat": 47.2382,
            "lon": -1.5603,
        }]

    def test_whole_city_aggregate_is_expanded_per_arrondissement(self):
        """A city like Paris comes back from the API as ONE commune-actuelle
        record listing every arrondissement's postal code under the
        whole-city INSEE code (75056) — pairing that single code with any
        one postal code would silently mismatch (e.g. tagging "Paris
        (75001)" with 75056 instead of 75101), so it must be expanded into
        one suggestion per postal code with the correct per-arrondissement
        INSEE code."""
        resp = MagicMock(status_code=200)
        resp.json.return_value = [{
            "nom": "Paris", "code": "75056",
            "codesPostaux": ["75001", "75002", "75015"],
            "centre": {"type": "Point", "coordinates": [2.347, 48.8589]},
        }]
        resp.raise_for_status.return_value = None
        with patch("core.geocode.requests.get", return_value=resp):
            suggestions = search_locations("paris")
        by_postal = {s["postalCode"]: s["inseeCode"] for s in suggestions}
        assert by_postal == {"75001": "75101", "75002": "75102", "75015": "75115"}

    def test_deduplicates_by_insee_code(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = [
            {"nom": "Nantes", "code": "44109", "codesPostaux": ["44000"],
             "centre": {"coordinates": [-1.5603, 47.2382]}},
            {"nom": "Nantes", "code": "44109", "codesPostaux": ["44000"],
             "centre": {"coordinates": [-1.5603, 47.2382]}},
        ]
        resp.raise_for_status.return_value = None
        with patch("core.geocode.requests.get", return_value=resp):
            suggestions = search_locations("nantes")
        assert len(suggestions) == 1

    def test_commune_without_postal_code_is_skipped(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = [{"nom": "Nowhere", "code": "00000", "codesPostaux": []}]
        resp.raise_for_status.return_value = None
        with patch("core.geocode.requests.get", return_value=resp):
            assert search_locations("nowhere") == []

    def test_network_error_returns_empty_list(self):
        with patch("core.geocode.requests.get", side_effect=Exception("boom")):
            assert search_locations("paris") == []

    def test_respects_limit(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = [{
            "nom": "Paris", "code": "75056",
            "codesPostaux": [f"7500{i}" for i in range(1, 10)],
            "centre": {"coordinates": [2.347, 48.8589]},
        }]
        resp.raise_for_status.return_value = None
        with patch("core.geocode.requests.get", return_value=resp):
            suggestions = search_locations("paris", limit=3)
        assert len(suggestions) == 3
