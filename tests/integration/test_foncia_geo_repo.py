"""FonciaGeoRepository contre un vrai Postgres.

Même cache à trois états que Century 21 (voir
tests/integration/test_century21_geo_repo.py) : le slug de localité Foncia
(`toulouse-31`, `haute-garonne-31`, `occitanie`) est une chaîne unique, comme
le placeId de SeLoger — pas une liste comme les zoneIds de bienici.

    pas de ligne                -> jamais tenté          -> résoudre
    ligne, slug_id renseigné    -> résolu                -> réutiliser
    ligne, slug_id à NULL       -> échec MÉMORISÉ        -> ne pas réessayer
                                                             avant 7 jours

Ce que l'unitaire mocke via RecordingConnection, on le prouve ici contre un
vrai moteur : l'upsert `ON CONFLICT` écrase au lieu de dupliquer, la clé
primaire TEXT fait office d'espace de noms pour tous les niveaux, et le
`resolved_at TIMESTAMP` (sans fuseau) revient intact par psycopg2.

`foncia_geo_ids` n'est pas dans `clean_db` mais dans la fixture autouse
`clean_geo_cache` (conftest partagé) — chaque test part donc d'un cache vide.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from services.foncia_geocode import area_cache_key
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
        assert storage.foncia_geo.get_cached("31555") is None

        assert sql.one("SELECT COUNT(*) FROM foncia_geo_ids") == 0

    def test_a_resolved_key_round_trips_with_its_timestamp(self, storage, sql):
        # Bornes lues sur LE MÊME serveur : la colonne est TIMESTAMP (sans
        # fuseau), psycopg2 rend donc un datetime naïf — LOCALTIMESTAMP aussi.
        before = sql.one("SELECT LOCALTIMESTAMP")
        storage.foncia_geo.set_cached("31555", "toulouse-31")
        after = sql.one("SELECT LOCALTIMESTAMP")

        row = storage.foncia_geo.get_cached("31555")

        assert row["area_key"] == "31555"
        assert row["slug_id"] == "toulouse-31"
        assert isinstance(row["resolved_at"], datetime)
        assert before <= row["resolved_at"] <= after

    def test_a_failure_writes_a_row_with_a_null_slug_id(self, storage, sql):
        storage.foncia_geo.set_cached("99999", None)

        row = storage.foncia_geo.get_cached("99999")

        assert row is not None, "un échec doit laisser une trace, pas rien"
        assert row["slug_id"] is None
        assert row["resolved_at"] is not None
        assert sql.one("SELECT COUNT(*) FROM foncia_geo_ids WHERE slug_id IS NULL") == 1

    def test_absent_and_failed_are_two_different_return_values(self, storage):
        storage.foncia_geo.set_cached("echec", None)

        absent = storage.foncia_geo.get_cached("jamais-tentee")
        failed = storage.foncia_geo.get_cached("echec")

        assert absent is None
        assert failed is not None
        assert failed["slug_id"] is None


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

class TestUpsert:
    def test_writing_the_same_key_twice_updates_instead_of_duplicating(self, storage, sql):
        storage.foncia_geo.set_cached("31555", "toulouse-31")

        storage.foncia_geo.set_cached("31555", "balma-31130")

        assert storage.foncia_geo.get_cached("31555")["slug_id"] == "balma-31130"
        assert sql.one("SELECT COUNT(*) FROM foncia_geo_ids") == 1

    def test_a_failure_can_be_upgraded_to_a_success(self, storage, sql):
        storage.foncia_geo.set_cached("31555", None)

        storage.foncia_geo.set_cached("31555", "toulouse-31")

        assert storage.foncia_geo.get_cached("31555")["slug_id"] == "toulouse-31"
        assert sql.one("SELECT COUNT(*) FROM foncia_geo_ids") == 1

    def test_resolved_at_moves_forward_on_every_write(self, storage, sql):
        # On vieillit la ligne en SQL plutôt que d'attendre : l'assertion
        # reste stricte (> old) sans dépendre de l'horloge réelle.
        storage.foncia_geo.set_cached("31555", None)
        sql.exec("UPDATE foncia_geo_ids SET resolved_at = NOW() - INTERVAL '30 days'")
        old = storage.foncia_geo.get_cached("31555")["resolved_at"]

        storage.foncia_geo.set_cached("31555", None)

        assert storage.foncia_geo.get_cached("31555")["resolved_at"] > old


# ---------------------------------------------------------------------------
# Cohabitation des niveaux dans une clé primaire unique
# ---------------------------------------------------------------------------

class TestAreaKeyNamespace:
    def test_department_33_and_region_76_do_not_collide(self, storage, sql):
        storage.foncia_geo.set_cached("dept:33", "gironde-33")
        storage.foncia_geo.set_cached("region:76", "occitanie")

        assert storage.foncia_geo.get_cached("dept:33")["slug_id"] == "gironde-33"
        assert storage.foncia_geo.get_cached("region:76")["slug_id"] == "occitanie"
        assert sql.one("SELECT COUNT(*) FROM foncia_geo_ids") == 2

    @pytest.mark.parametrize(
        ("location", "expected_key"),
        [
            pytest.param(make_city_location(insee="31555"), "31555", id="commune-insee-nu"),
            pytest.param(make_whole_city_location(insee="86194"), "city:86194", id="ville-entiere"),
            pytest.param(make_department_location(code="2A"), "dept:2A", id="departement"),
            pytest.param(make_region_location(code="76"), "region:76", id="region"),
        ],
    )
    def test_the_key_produced_by_area_cache_key_is_the_one_stored(self, storage, sql, location, expected_key):
        key = area_cache_key(location)
        assert key == expected_key

        storage.foncia_geo.set_cached(key, "slug-x")

        assert sql.one("SELECT area_key FROM foncia_geo_ids") == expected_key
