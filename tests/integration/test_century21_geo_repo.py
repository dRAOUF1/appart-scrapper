"""Century21GeoRepository contre un vrai Postgres.

Même cache à trois états que SelogerGeoRepository (voir
tests/integration/test_seloger_geo_repo.py) : le slug d'URL de Century 21 est
une chaîne unique, comme le placeId de SeLoger (pas une liste comme les zoneIds
de bienici).

    pas de ligne                -> jamais tenté          -> résoudre
    ligne, slug_id renseigné    -> résolu                -> réutiliser
    ligne, slug_id à NULL       -> échec MÉMORISÉ        -> ne pas réessayer
                                                             avant 7 jours

`century21_geo_ids` n'est pas dans `clean_db` mais dans la fixture autouse
`clean_geo_cache` — chaque test part donc d'un cache vide.
"""

from __future__ import annotations

import pytest

from services.century21_geocode import area_cache_key, resolve_slug_id
from tests.helpers.factories import (
    make_city_location,
    make_department_location,
    make_region_location,
    make_whole_city_location,
)

# ---------------------------------------------------------------------------
# Les trois états
# ---------------------------------------------------------------------------

class TestThreeStates:
    def test_a_key_never_attempted_has_no_row_at_all(self, storage, sql):
        assert storage.century21_geo.get_cached("75113") is None

        assert sql.one("SELECT COUNT(*) FROM century21_geo_ids") == 0

    def test_a_resolved_key_round_trips_with_its_timestamp(self, storage):
        storage.century21_geo.set_cached("75113", "cp-75015")

        row = storage.century21_geo.get_cached("75113")

        assert row["area_key"] == "75113"
        assert row["slug_id"] == "cp-75015"
        assert row["resolved_at"] is not None

    def test_a_failure_writes_a_row_with_a_null_slug_id(self, storage, sql):
        storage.century21_geo.set_cached("99999", None)

        row = storage.century21_geo.get_cached("99999")

        assert row is not None, "un échec doit laisser une trace, pas rien"
        assert row["slug_id"] is None
        assert row["resolved_at"] is not None
        assert sql.one("SELECT COUNT(*) FROM century21_geo_ids WHERE slug_id IS NULL") == 1

    def test_absent_and_failed_are_two_different_return_values(self, storage):
        storage.century21_geo.set_cached("echec", None)

        absent = storage.century21_geo.get_cached("jamais-tentee")
        failed = storage.century21_geo.get_cached("echec")

        assert absent is None
        assert failed is not None
        assert failed["slug_id"] is None


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

class TestUpsert:
    def test_writing_the_same_key_twice_updates_instead_of_duplicating(self, storage, sql):
        storage.century21_geo.set_cached("75113", "cp-75015")

        storage.century21_geo.set_cached("75113", "v-paris")

        assert storage.century21_geo.get_cached("75113")["slug_id"] == "v-paris"
        assert sql.one("SELECT COUNT(*) FROM century21_geo_ids") == 1

    def test_a_failure_can_be_upgraded_to_a_success(self, storage, sql):
        storage.century21_geo.set_cached("75113", None)

        storage.century21_geo.set_cached("75113", "cp-75015")

        assert storage.century21_geo.get_cached("75113")["slug_id"] == "cp-75015"
        assert sql.one("SELECT COUNT(*) FROM century21_geo_ids") == 1

    def test_resolved_at_moves_forward_on_every_write(self, storage, sql):
        storage.century21_geo.set_cached("75113", None)
        sql.exec("UPDATE century21_geo_ids SET resolved_at = NOW() - INTERVAL '30 days'")
        old = storage.century21_geo.get_cached("75113")["resolved_at"]

        storage.century21_geo.set_cached("75113", None)

        assert storage.century21_geo.get_cached("75113")["resolved_at"] > old


# ---------------------------------------------------------------------------
# Cohabitation des niveaux dans une clé primaire unique
# ---------------------------------------------------------------------------

class TestAreaKeyNamespace:
    def test_department_75_and_region_75_do_not_collide(self, storage):
        storage.century21_geo.set_cached("dept:75", "v-paris")
        storage.century21_geo.set_cached("region:75", "v-paris")

        assert storage.century21_geo.get_cached("dept:75")["slug_id"] == "v-paris"
        assert storage.century21_geo.get_cached("region:75")["slug_id"] == "v-paris"

    @pytest.mark.parametrize(
        ("location", "expected_key"),
        [
            pytest.param(make_city_location(insee="75113"), "75113", id="commune-insee-nu"),
            pytest.param(make_whole_city_location(insee="86194"), "city:86194", id="ville-entiere"),
            pytest.param(make_department_location(code="33"), "dept:33", id="departement"),
            pytest.param(make_region_location(code="75"), "region:75", id="region"),
        ],
    )
    def test_the_key_produced_by_area_cache_key_is_the_one_stored(self, storage, sql, location, expected_key):
        key = area_cache_key(location)
        assert key == expected_key

        storage.century21_geo.set_cached(key, "v-x")

        assert sql.one("SELECT area_key FROM century21_geo_ids") == expected_key


# ---------------------------------------------------------------------------
# Le consommateur : le cooldown de 7 jours
# ---------------------------------------------------------------------------

@pytest.fixture
def resolver_calls(monkeypatch):
    calls: list[dict] = []
    result: dict = {"slug_id": None}

    def _fake(location):
        calls.append(location)
        return result["slug_id"]

    monkeypatch.setattr("services.century21_geocode._resolve_uncached", _fake)
    return calls, result


class TestCooldownDependsOnTheThreeStates:
    def test_a_cached_slug_short_circuits_the_network(self, storage, resolver_calls):
        calls, _ = resolver_calls
        storage.century21_geo.set_cached("75113", "cp-75015")

        assert resolve_slug_id(make_city_location(insee="75113"), storage.century21_geo) == "cp-75015"

        assert calls == []

    def test_no_row_triggers_a_resolution_and_banks_the_result(self, storage, resolver_calls):
        calls, result = resolver_calls
        result["slug_id"] = "cp-75015"

        assert resolve_slug_id(make_city_location(insee="75113"), storage.century21_geo) == "cp-75015"

        assert len(calls) == 1
        assert storage.century21_geo.get_cached("75113")["slug_id"] == "cp-75015"

    def test_a_failed_resolution_writes_the_null_row_that_arms_the_cooldown(self, storage, resolver_calls):
        calls, _ = resolver_calls

        assert resolve_slug_id(make_city_location(insee="75113"), storage.century21_geo) is None

        assert len(calls) == 1
        row = storage.century21_geo.get_cached("75113")
        assert row is not None and row["slug_id"] is None

    def test_a_recent_failure_is_not_retried(self, storage, resolver_calls):
        calls, _ = resolver_calls
        resolve_slug_id(make_city_location(insee="75113"), storage.century21_geo)
        assert len(calls) == 1

        assert resolve_slug_id(make_city_location(insee="75113"), storage.century21_geo) is None

        assert len(calls) == 1

    @pytest.mark.parametrize(
        ("age_days", "expect_retry"),
        [
            pytest.param(1, False, id="1-jour"),
            pytest.param(6, False, id="6-jours"),
            pytest.param(8, True, id="8-jours"),
        ],
    )
    def test_the_cooldown_boundary_is_seven_days(self, storage, sql, resolver_calls, age_days, expect_retry):
        calls, _ = resolver_calls
        storage.century21_geo.set_cached("75113", None)
        sql.exec(
            "UPDATE century21_geo_ids SET resolved_at = NOW() - make_interval(days => %s)",
            (age_days,),
        )

        resolve_slug_id(make_city_location(insee="75113"), storage.century21_geo)

        assert (len(calls) == 1) is expect_retry

    def test_a_location_without_an_identifiable_key_never_touches_the_table(self, storage, sql, resolver_calls):
        calls, _ = resolver_calls

        assert resolve_slug_id({"kind": "city", "city": "Nulle-Part"}, storage.century21_geo) is None

        assert calls == []
        assert sql.one("SELECT COUNT(*) FROM century21_geo_ids") == 0
