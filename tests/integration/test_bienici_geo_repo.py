"""BienIciGeoRepository contre un vrai Postgres.

Même cache à trois états que SelogerGeoRepository (voir
tests/integration/test_seloger_geo_repo.py), avec une différence de forme :
`zone_ids` est un tableau JSON (une région = union de plusieurs zoneIds),
là où `place_id` de SeLoger est une chaîne unique.

    pas de ligne                -> jamais tenté          -> résoudre
    ligne, zone_ids renseigné    -> résolu                -> réutiliser
    ligne, zone_ids à NULL       -> échec MÉMORISÉ        -> ne pas réessayer
                                                             avant 7 jours
"""

from __future__ import annotations

import pytest

from services.bienici_geocode import area_cache_key, resolve_zone_ids
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
        assert storage.bienici_geo.get_cached("75113") is None

        assert sql.one("SELECT COUNT(*) FROM bienici_zone_ids") == 0

    def test_a_resolved_key_round_trips_with_its_timestamp(self, storage):
        storage.bienici_geo.set_cached("75113", ["-9520"])

        row = storage.bienici_geo.get_cached("75113")

        assert row["area_key"] == "75113"
        assert row["zone_ids"] == ["-9520"]
        assert row["resolved_at"] is not None

    def test_several_zone_ids_survive_the_round_trip(self, storage):
        """Le cas région : plusieurs zoneIds unis dans une seule ligne."""
        storage.bienici_geo.set_cached("region:75", ["-7405", "-7440", "-7441"])

        row = storage.bienici_geo.get_cached("region:75")

        assert row["zone_ids"] == ["-7405", "-7440", "-7441"]

    def test_a_failure_writes_a_row_with_a_null_zone_ids(self, storage, sql):
        storage.bienici_geo.set_cached("99999", None)

        row = storage.bienici_geo.get_cached("99999")

        assert row is not None, "un échec doit laisser une trace, pas rien"
        assert row["zone_ids"] is None
        assert row["resolved_at"] is not None
        assert sql.one("SELECT COUNT(*) FROM bienici_zone_ids WHERE zone_ids IS NULL") == 1

    def test_absent_and_failed_are_two_different_return_values(self, storage):
        storage.bienici_geo.set_cached("echec", None)

        absent = storage.bienici_geo.get_cached("jamais-tentee")
        failed = storage.bienici_geo.get_cached("echec")

        assert absent is None
        assert failed is not None
        assert failed["zone_ids"] is None


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

class TestUpsert:
    def test_writing_the_same_key_twice_updates_instead_of_duplicating(self, storage, sql):
        storage.bienici_geo.set_cached("75113", ["-9520"])

        storage.bienici_geo.set_cached("75113", ["-9521"])

        assert storage.bienici_geo.get_cached("75113")["zone_ids"] == ["-9521"]
        assert sql.one("SELECT COUNT(*) FROM bienici_zone_ids") == 1

    def test_a_failure_can_be_upgraded_to_a_success(self, storage, sql):
        storage.bienici_geo.set_cached("75113", None)

        storage.bienici_geo.set_cached("75113", ["-9520"])

        assert storage.bienici_geo.get_cached("75113")["zone_ids"] == ["-9520"]
        assert sql.one("SELECT COUNT(*) FROM bienici_zone_ids") == 1

    def test_resolved_at_moves_forward_on_every_write(self, storage, sql):
        storage.bienici_geo.set_cached("75113", None)
        sql.exec("UPDATE bienici_zone_ids SET resolved_at = NOW() - INTERVAL '30 days'")
        old = storage.bienici_geo.get_cached("75113")["resolved_at"]

        storage.bienici_geo.set_cached("75113", None)

        assert storage.bienici_geo.get_cached("75113")["resolved_at"] > old


# ---------------------------------------------------------------------------
# Cohabitation des niveaux dans une clé primaire unique
# ---------------------------------------------------------------------------

class TestAreaKeyNamespace:
    def test_department_75_and_region_75_do_not_collide(self, storage):
        storage.bienici_geo.set_cached("dept:75", ["-71525"])
        storage.bienici_geo.set_cached("region:75", ["-999"])

        assert storage.bienici_geo.get_cached("dept:75")["zone_ids"] == ["-71525"]
        assert storage.bienici_geo.get_cached("region:75")["zone_ids"] == ["-999"]

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

        storage.bienici_geo.set_cached(key, ["-1"])

        assert sql.one("SELECT area_key FROM bienici_zone_ids") == expected_key


# ---------------------------------------------------------------------------
# Le consommateur : le cooldown de 7 jours
# ---------------------------------------------------------------------------

@pytest.fixture
def resolver_calls(monkeypatch):
    calls: list[dict] = []
    result: dict = {"zone_ids": None}

    def _fake(location):
        calls.append(location)
        return result["zone_ids"]

    monkeypatch.setattr("services.bienici_geocode._resolve_uncached", _fake)
    return calls, result


class TestCooldownDependsOnTheThreeStates:
    def test_a_cached_zone_ids_short_circuits_the_network(self, storage, resolver_calls):
        calls, _ = resolver_calls
        storage.bienici_geo.set_cached("75113", ["-9520"])

        assert resolve_zone_ids(make_city_location(insee="75113"), storage.bienici_geo) == ["-9520"]

        assert calls == []

    def test_no_row_triggers_a_resolution_and_banks_the_result(self, storage, resolver_calls):
        calls, result = resolver_calls
        result["zone_ids"] = ["-9520"]

        assert resolve_zone_ids(make_city_location(insee="75113"), storage.bienici_geo) == ["-9520"]

        assert len(calls) == 1
        assert storage.bienici_geo.get_cached("75113")["zone_ids"] == ["-9520"]

    def test_a_failed_resolution_writes_the_null_row_that_arms_the_cooldown(self, storage, resolver_calls):
        calls, _ = resolver_calls

        assert resolve_zone_ids(make_city_location(insee="75113"), storage.bienici_geo) is None

        assert len(calls) == 1
        row = storage.bienici_geo.get_cached("75113")
        assert row is not None and row["zone_ids"] is None

    def test_a_recent_failure_is_not_retried(self, storage, resolver_calls):
        calls, _ = resolver_calls
        resolve_zone_ids(make_city_location(insee="75113"), storage.bienici_geo)
        assert len(calls) == 1

        assert resolve_zone_ids(make_city_location(insee="75113"), storage.bienici_geo) is None

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
        storage.bienici_geo.set_cached("75113", None)
        sql.exec(
            "UPDATE bienici_zone_ids SET resolved_at = NOW() - make_interval(days => %s)",
            (age_days,),
        )

        resolve_zone_ids(make_city_location(insee="75113"), storage.bienici_geo)

        assert (len(calls) == 1) is expect_retry

    def test_a_location_without_an_identifiable_key_never_touches_the_table(self, storage, sql, resolver_calls):
        calls, _ = resolver_calls

        assert resolve_zone_ids({"kind": "city", "city": "Nulle-Part"}, storage.bienici_geo) is None

        assert calls == []
        assert sql.one("SELECT COUNT(*) FROM bienici_zone_ids") == 0
