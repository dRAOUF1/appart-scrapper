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


def _geo_mock(communes=None, departements=None, regions=None):
    """Mock de requests.get qui répond selon l'endpoint interrogé.

    search_locations() interroge régions, départements et communes : un mock
    unique renverrait des communes en guise de régions.
    """
    def fake_get(url, **kwargs):
        if "/regions" in url and "/departements" in url:
            return _resp([{"code": d} for d in (regions or {}).get("departements", [])])
        if url.endswith("/regions"):
            return _resp(regions.get("results", []) if regions else [])
        if url.endswith("/departements"):
            return _resp(departements or [])
        return _resp(communes or [])
    return fake_get


def _resp(payload):
    r = MagicMock(status_code=200)
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


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
        with patch("core.geocode.requests.get", side_effect=_geo_mock(communes=resp.json.return_value)):
            suggestions = search_locations("nantes")
        assert suggestions == [{
            "kind": "city",
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
        with patch("core.geocode.requests.get", side_effect=_geo_mock(communes=resp.json.return_value)):
            suggestions = search_locations("paris")
        by_postal = {s["postalCode"]: s["inseeCode"] for s in suggestions if s["kind"] == "city"}
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
        with patch("core.geocode.requests.get", side_effect=_geo_mock(communes=resp.json.return_value)):
            suggestions = search_locations("nantes")
        assert len(suggestions) == 1

    def test_commune_without_postal_code_is_skipped(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = [{"nom": "Nowhere", "code": "00000", "codesPostaux": []}]
        resp.raise_for_status.return_value = None
        with patch("core.geocode.requests.get", side_effect=_geo_mock(communes=resp.json.return_value)):
            assert search_locations("nowhere") == []

    def test_network_error_returns_empty_list(self):
        with patch("core.geocode.requests.get", side_effect=Exception("boom")):
            assert search_locations("paris") == []


class TestWideAreaSuggestions:
    """L'autocomplete doit proposer des périmètres plus larges qu'une commune :
    « Île-de-France » ou « Gironde » plutôt qu'une saisie ville par ville. Les
    deux sources savent chercher à ces niveaux en une seule requête."""

    def test_region_is_suggested_with_its_departments(self):
        regions = {"results": [{"nom": "Île-de-France", "code": "11"}],
                   "departements": ["75", "77", "78", "91", "92", "93", "94", "95"]}
        with patch("core.geocode.requests.get", side_effect=_geo_mock(regions=regions)):
            suggestions = search_locations("ile de france")
        assert len(suggestions) == 1
        region = suggestions[0]
        assert region["kind"] == "region"
        assert region["code"] == "11"
        assert region["name"] == "Île-de-France"
        # Les départements sont mémorisés à la saisie : les sources qui ne
        # connaissent que les départements en ont besoin au scrape.
        assert region["departments"] == ["75", "77", "78", "91", "92", "93", "94", "95"]

    def test_department_is_suggested(self):
        with patch("core.geocode.requests.get",
                   side_effect=_geo_mock(departements=[{"nom": "Gironde", "code": "33"}])):
            suggestions = search_locations("gironde")
        assert [s["kind"] for s in suggestions] == ["department"]
        assert suggestions[0]["code"] == "33"

    def test_wide_areas_come_before_communes(self):
        """« gironde » doit proposer le département avant les communes
        homonymes (Gironde-sur-Dropt, Castres-Gironde...), sinon il est noyé."""
        with patch("core.geocode.requests.get", side_effect=_geo_mock(
            departements=[{"nom": "Gironde", "code": "33"}],
            communes=[{"nom": "Gironde-sur-Dropt", "code": "33190",
                       "codesPostaux": ["33190"], "centre": {"coordinates": [0, 0]}}],
        )):
            suggestions = search_locations("gironde")
        assert [s["kind"] for s in suggestions] == ["department", "city"]

    def test_multi_postal_code_city_gets_a_whole_city_entry(self):
        """Bordeaux couvre 5 codes postaux : il faut pouvoir prendre la ville
        entière d'un coup, au lieu d'ajouter 5 lignes à la main."""
        with patch("core.geocode.requests.get", side_effect=_geo_mock(communes=[{
            "nom": "Bordeaux", "code": "33063",
            "codesPostaux": ["33000", "33100", "33200", "33300", "33800"],
            "centre": {"coordinates": [-0.57, 44.84]},
        }])):
            suggestions = search_locations("bordeaux")
        whole = suggestions[0]
        assert whole["kind"] == "whole_city"
        assert whole["inseeCode"] == "33063"
        assert whole["postalCodes"] == ["33000", "33100", "33200", "33300", "33800"]
        # Les codes postaux restent proposés individuellement derrière.
        assert [s["kind"] for s in suggestions[1:]] == ["city"] * 5

    def test_single_postal_code_city_has_no_whole_city_entry(self):
        """Poitiers n'a qu'un code postal : une entrée « toute la ville »
        ferait doublon avec l'entrée du code postal."""
        with patch("core.geocode.requests.get", side_effect=_geo_mock(communes=[{
            "nom": "Poitiers", "code": "86194", "codesPostaux": ["86000"],
            "centre": {"coordinates": [0.37, 46.58]},
        }])):
            suggestions = search_locations("poitiers")
        assert [s["kind"] for s in suggestions] == ["city"]

    def test_department_covering_the_same_city_is_dropped(self):
        """Paris est à la fois une commune (75056) et un département (75) sur
        le même territoire : les deux entrées seraient indiscernables pour
        l'utilisateur, on ne garde que la ville."""
        with patch("core.geocode.requests.get", side_effect=_geo_mock(
            departements=[{"nom": "Paris", "code": "75"}],
            communes=[{"nom": "Paris", "code": "75056",
                       "codesPostaux": ["75001", "75002"],
                       "centre": {"coordinates": [2.35, 48.86]}}],
        )):
            suggestions = search_locations("paris")
        assert "department" not in [s["kind"] for s in suggestions]
        assert suggestions[0]["kind"] == "whole_city"


class TestRegionDepartments:
    def test_returns_department_codes(self):
        from core.geocode import region_departments
        with patch("core.geocode.requests.get",
                   side_effect=_geo_mock(regions={"departements": ["2A", "2B"]})):
            assert region_departments("94") == ["2A", "2B"]

    def test_result_is_cached(self):
        from core.geocode import _REGION_DEPARTMENTS_CACHE, region_departments
        _REGION_DEPARTMENTS_CACHE.clear()
        mock = MagicMock(side_effect=_geo_mock(regions={"departements": ["33"]}))
        with patch("core.geocode.requests.get", mock):
            region_departments("75")
            region_departments("75")
        assert mock.call_count == 1
        _REGION_DEPARTMENTS_CACHE.clear()

    def test_network_error_returns_empty(self):
        from core.geocode import _REGION_DEPARTMENTS_CACHE, region_departments
        _REGION_DEPARTMENTS_CACHE.clear()
        with patch("core.geocode.requests.get", side_effect=Exception("boom")):
            assert region_departments("11") == []


class TestPostalPrefix:
    """Le code postal se déduit du code département, sauf en Corse."""

    def test_mainland_and_overseas(self):
        from core.geocode import postal_prefix
        assert postal_prefix("33") == "33"
        assert postal_prefix("75") == "75"
        assert postal_prefix("971") == "971"

    def test_corsica_uses_20(self):
        """2A et 2B ont tous deux des codes postaux en 20xxx — vérifié via
        l'API : aucun code postal corse ne commence par « 2A » ou « 2B »."""
        from core.geocode import postal_prefix
        assert postal_prefix("2A") == "20"
        assert postal_prefix("2B") == "20"
        assert postal_prefix("2a") == "20"

    def test_respects_limit(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = [{
            "nom": "Paris", "code": "75056",
            "codesPostaux": [f"7500{i}" for i in range(1, 10)],
            "centre": {"coordinates": [2.347, 48.8589]},
        }]
        resp.raise_for_status.return_value = None
        with patch("core.geocode.requests.get", side_effect=_geo_mock(communes=resp.json.return_value)):
            suggestions = search_locations("paris", limit=3)
        assert len(suggestions) == 3
