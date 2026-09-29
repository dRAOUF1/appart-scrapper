"""Résolution Citya hors réseau : autocomplete, sélection et cache."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time

from repositories.citya_geo_repo import CityaGeoRepository
from services import citya_geocode as geo
from tests.helpers.factories import (
    make_city_location,
    make_department_location,
    make_region_location,
    make_whole_city_location,
)

FROZEN = "2026-08-20 12:00:00"
TOULOUSE = {"id": "31555", "codesPostaux": ["31000"], "ville": "Toulouse", "slug": "toulouse-31555"}


@pytest.fixture
def repo():
    double = MagicMock(spec=CityaGeoRepository)
    double.get_cached.return_value = None
    return double


def test_query_autocomplete_sends_required_headers_and_filters_entries(requests_mock):
    requests_mock.get(
        geo.SUGGEST_URL,
        json={"villes": [TOULOUSE, "invalide"], "departements": None, "regions": [{"slug": "occitanie-76"}]},
    )

    result = geo._query_autocomplete(" Toulouse ")

    assert result == {"villes": [TOULOUSE], "departements": [], "regions": [{"slug": "occitanie-76"}]}
    request = requests_mock.last_request
    assert request.qs == {"q": ["toulouse"]}
    assert request.headers["Referer"] == geo._HEADERS["Referer"]


@pytest.mark.parametrize(("payload", "expected"), [([], "lists"), ("texte", "lists"), (None, "empty")])
def test_query_autocomplete_rejects_non_object_payloads(requests_mock, payload, expected):
    requests_mock.get(geo.SUGGEST_URL, json=payload)

    result = geo._query_autocomplete("X")
    assert result == ({"villes": [], "departements": [], "regions": []} if expected == "lists" else {})


def test_query_autocomplete_handles_http_and_json_failures(requests_mock):
    requests_mock.get(geo.SUGGEST_URL, [{"status_code": 503}, {"text": "pas-json"}])

    assert geo._query_autocomplete("X") == {}
    assert geo._query_autocomplete("X") == {}
    assert geo._query_autocomplete(" ") == {}


def test_name_normalization_ignores_accents_and_punctuation():
    assert geo._names_match("Saint-Étienne", "SAINT ETIENNE")
    assert not geo._names_match("", "Paris")
    assert not geo._names_match("Lyon", "Paris")


def test_official_region_name_success_and_failures(requests_mock):
    requests_mock.get(f"{geo.REGIONS_API}/76", json={"nom": "Occitanie"})
    assert geo._official_region_name("76") == "Occitanie"

    requests_mock.get(f"{geo.REGIONS_API}/99", status_code=500)
    requests_mock.get(f"{geo.REGIONS_API}/98", text="pas-json")
    assert geo._official_region_name("99") is None
    assert geo._official_region_name("98") is None


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        (make_city_location(), "75113"),
        (make_city_location(insee="", postal_code="75013"), "postal:75013"),
        (make_whole_city_location(), "city:86194"),
        (make_whole_city_location(insee="", city=" Poitiers "), "city_name:poitiers"),
        (make_department_location(code="33"), "dept:33"),
        (make_region_location(code="75"), "region:75"),
        ({"kind": "city"}, None),
    ],
)
def test_area_cache_key(location, expected):
    assert geo.area_cache_key(location) == expected
    assert geo.is_statically_resolvable(location) is False


def test_describe_covers_every_scope():
    assert "région" in geo._describe(make_region_location())
    assert "département" in geo._describe(make_department_location())
    assert "toute la ville" in geo._describe(make_whole_city_location())
    assert "Paris" in geo._describe(make_city_location())


def test_pick_city_prefers_insee_then_accepts_name_and_postal_code():
    other = {**TOULOUSE, "id": "99999"}
    assert geo._pick_city_entry([other, TOULOUSE], "Toulouse", "31555", "31000") == TOULOUSE
    assert geo._pick_city_entry([TOULOUSE], "Autre nom", "31555", "99999") == TOULOUSE
    assert geo._pick_city_entry([other], "Toulouse", "31555", "31000") == other
    assert geo._pick_city_entry([other], "Lyon", "31555", "31000") is None


def test_each_scope_resolves_from_verified_autocomplete(monkeypatch):
    def autocomplete(query):
        return {
            "Toulouse": {"villes": [TOULOUSE], "departements": [], "regions": []},
            "31": {"villes": [], "departements": [{"code": "31", "slug": "haute-garonne-31"}], "regions": []},
            "Occitanie": {
                "villes": [], "departements": [],
                "regions": [{"libelle": "Occitanie", "slug": "occitanie-76"}],
            },
        }[query]

    monkeypatch.setattr(geo, "_query_autocomplete", autocomplete)
    city = make_city_location(city="Toulouse", postal_code="31000", insee="31555")
    assert geo._resolve_city(city) == "toulouse-31555"
    assert geo._resolve_whole_city(
        make_whole_city_location(city="Toulouse", postal_codes=("31000",), insee="31555")
    ) == "toulouse-31555"
    assert geo._resolve_department(make_department_location(code="31")) == "haute-garonne-31"
    assert geo._resolve_region(make_region_location(code="76", name="Occitanie")) == "occitanie-76"


def test_scope_resolution_rejects_missing_or_mismatched_entries(monkeypatch):
    monkeypatch.setattr(geo, "_query_autocomplete", lambda query: {"villes": [], "departements": [], "regions": []})
    assert geo._resolve_city(make_city_location()) is None
    assert geo._resolve_whole_city(make_whole_city_location()) is None
    assert geo._resolve_department({"kind": "department"}) is None
    assert geo._resolve_department(make_department_location(code="33")) is None
    assert geo._resolve_region({"kind": "region"}) is None
    assert geo._resolve_region(make_region_location(code="94", name="Corse")) is None


def test_region_can_fetch_its_official_name(monkeypatch):
    monkeypatch.setattr(geo, "_official_region_name", lambda code: "Occitanie")
    monkeypatch.setattr(
        geo,
        "_query_autocomplete",
        lambda query: {"villes": [], "departements": [], "regions": [{"libelle": query, "slug": "occitanie-76"}]},
    )
    assert geo._resolve_region(make_region_location(code="76", name="")) == "occitanie-76"


@pytest.mark.parametrize(
    ("location", "resolver", "expected"),
    [
        (make_city_location(), "_resolve_city", "ville"),
        (make_whole_city_location(), "_resolve_whole_city", "ville-entiere"),
        (make_department_location(), "_resolve_department", "departement"),
        (make_region_location(), "_resolve_region", "region"),
    ],
)
def test_resolve_uncached_dispatches(monkeypatch, location, resolver, expected):
    monkeypatch.setattr(geo, resolver, lambda value: expected)
    assert geo._resolve_uncached(location) == expected
    assert geo._resolve_uncached({"kind": "inconnu"}) is None


def test_resolve_uncached_degrades_on_unexpected_exception(monkeypatch):
    monkeypatch.setattr(geo, "_resolve_city", lambda location: (_ for _ in ()).throw(RuntimeError("boom")))
    assert geo._resolve_uncached(make_city_location()) is None


@freeze_time(FROZEN)
def test_seconds_since_supports_naive_aware_and_none():
    now = datetime.datetime(2026, 8, 20, 12, 0)
    assert geo._seconds_since(now - datetime.timedelta(seconds=30)) == 30
    aware = now.replace(tzinfo=datetime.UTC) - datetime.timedelta(seconds=45)
    assert geo._seconds_since(aware) == 45
    assert geo._seconds_since(None) is None


def test_resolve_slug_uses_success_cache_and_recent_failure(repo, monkeypatch):
    repo.get_cached.return_value = {"slug_id": "toulouse-31555", "resolved_at": None}
    assert geo.resolve_slug_id(make_city_location(), repo) == "toulouse-31555"

    repo.get_cached.return_value = {"slug_id": None, "resolved_at": datetime.datetime.now()}
    monkeypatch.setattr(geo, "_resolve_uncached", lambda location: pytest.fail("réseau interdit"))
    assert geo.resolve_slug_id(make_city_location(), repo) is None


def test_resolve_slug_refreshes_old_failure_and_caches_result(repo, monkeypatch):
    repo.get_cached.return_value = {
        "slug_id": None,
        "resolved_at": datetime.datetime.now() - datetime.timedelta(days=8),
    }
    monkeypatch.setattr(geo, "_resolve_uncached", lambda location: "paris-75")

    assert geo.resolve_slug_id(make_city_location(), repo) == "paris-75"
    repo.set_cached.assert_called_once_with("75113", "paris-75")


def test_resolve_slug_handles_missing_key_repo_and_failed_resolution(repo, monkeypatch):
    assert geo.resolve_slug_id({"kind": "city"}, repo) is None
    assert geo.resolve_slug_id(make_city_location(), None) is None
    monkeypatch.setattr(geo, "_resolve_uncached", lambda location: None)
    assert geo.resolve_slug_id(make_city_location(), repo) is None
    repo.set_cached.assert_called_once_with("75113", None)


def test_remember_manual_slug_only_for_one_uncached_location(repo):
    criteria = {
        "locations": [make_city_location()],
        "sourceOverrides": {"citya": {"slugs": ["paris-75"]}},
    }
    geo.remember_manual_slugs(criteria, repo)
    repo.set_cached.assert_called_once_with("75113", "paris-75")

    repo.reset_mock()
    repo.get_cached.return_value = {"slug_id": "déjà-là"}
    geo.remember_manual_slugs(criteria, repo)
    repo.set_cached.assert_not_called()
    geo.remember_manual_slugs({"locations": []}, repo)
    geo.remember_manual_slugs({"locations": [make_city_location(), make_city_location()]}, repo)
    repo.set_cached.assert_not_called()
