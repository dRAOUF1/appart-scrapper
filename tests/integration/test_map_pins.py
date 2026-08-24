"""Migrations et SQL réel de la carte (#26) contre un vrai Postgres.

Ce que les doubles ne peuvent pas prouver :

* les colonnes ``listings.latitude/longitude/location_precision`` EXISTENT
  en base et survivent à un aller-retour ``save_and_link`` ->
  ``get_map_points_for_search`` — avec le filtrage SQL des annonces non
  géolocalisées ;
* la table ``map_pins`` porte ses contraintes : FK utilisateurs, CHECK
  non-zéro et bornes géographiques (le golfe de Guinée est refusé PAR LA BASE,
  pas seulement par la route), CASCADE à la suppression du compte ;
* le scoping utilisateur du ``MapPinRepository`` tient sur le SQL réel ;
* le cache ``commune_centres`` upserte proprement.

Migrations jouées sur des bases VIERGES jetables (fixture ``blank_db``) :
idempotence comprise.
"""

from __future__ import annotations

import psycopg2
import pytest

from storage import Storage
from tests.helpers.factories import make_listing

# ---------------------------------------------------------------------------
# Migration : colonnes de coordonnées + table map_pins
# ---------------------------------------------------------------------------


def test_migration_adds_coords_columns_and_map_pins_table(blank_db):
    url = blank_db()
    Storage.run_migrations(url)

    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'listings' AND column_name IN "
                "('latitude', 'longitude', 'location_precision')"
            )
            colonnes = {r[0] for r in cur.fetchall()}
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name = 'map_pins'"
            )
            tables = {r[0] for r in cur.fetchall()}
    finally:
        conn.close()

    assert colonnes == {"latitude", "longitude", "location_precision"}
    assert tables == {"map_pins"}


def test_migration_is_idempotent_for_the_map_schema(blank_db):
    url = blank_db()
    Storage.run_migrations(url)
    Storage.run_migrations(url)  # ne lève pas, n'efface rien
    Storage.run_migrations(url)

    storage = Storage(url)
    user = storage.users.create_user("survivant")
    assert storage.map_pins.create(user["id"], "Toujours là", 48.85, 2.35)["id"] >= 1


def test_coords_round_trip_through_save_and_link_and_map_points(storage, clean_db):
    """LA preuve utile : une annonce géolocalisée écrite par le pipeline est
    relue par l'endpoint carte ; celle sans coordonnées est filtrée par le
    SQL (jamais envoyée au client)."""
    user = storage.users.create_user("alice")
    search = storage.searches.create_search(
        user["id"], "Bordeaux", "topic-bdx", "seloger",
        {"locations": [{"kind": "city", "city": "Bordeaux", "postalCode": "33000"}]}, 5,
    )

    geolocalisee = make_listing(
        listing_id="sl_geo", source="bienici",
        latitude=44.85786105489427, longitude=-0.5764371365744323,
        location_precision="approximative",
    )
    sans_coords = make_listing(listing_id="sl_nu", source="orpi")

    new_listings, _ = storage.listings.save_and_link([geolocalisee, sans_coords], search["id"])
    assert len(new_listings) == 2

    points = storage.listings.get_map_points_for_search(search["id"])

    assert [p["listing_id"] for p in points] == ["sl_geo"]
    point = points[0]
    assert point["latitude"] == pytest.approx(44.85786105489427)
    assert point["longitude"] == pytest.approx(-0.5764371365744323)
    assert point["location_precision"] == "approximative"


def test_a_pre_existing_listings_table_receives_the_columns(blank_db):
    """Le cas « base déjà déployée » : la table existe SANS les colonnes ;
    l'ALTER idempotent doit les ajouter sans perdre les données."""
    url = blank_db()
    Storage.run_migrations(url)
    storage = Storage(url)
    user = storage.users.create_user("avant_26")
    search = storage.searches.create_search(user["id"], "S", "t", "seloger", {}, 5)
    storage.listings.save_and_link([make_listing(listing_id="ancienne")], search["id"])

    conn = psycopg2.connect(url)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE listings DROP COLUMN latitude,"
                " DROP COLUMN longitude, DROP COLUMN location_precision"
            )
    finally:
        conn.close()

    Storage.run_migrations(url)

    detail = storage.listings.get_listing_detail("ancienne")
    assert detail["title"] is not None  # les données ont survécu
    assert detail["latitude"] is None   # et naissent sans coordonnées
    points = storage.listings.get_map_points_for_search(search["id"])
    assert points == []


# ---------------------------------------------------------------------------
# MapPinRepository : CRUD scoping sur le SQL réel
# ---------------------------------------------------------------------------


class TestMapPinRepositoryReal:
    def test_create_list_get_scoped(self, storage, user, other_user):
        sien = storage.map_pins.create(user["id"], "Bon boulanger", 48.85, 2.35,
                                       note="baguettes", icon="⭐")
        etrange = storage.map_pins.create(other_user["id"], "Chez Bob", 43.6, 1.44)

        liste = storage.map_pins.list_for_user(user["id"])

        assert [p["id"] for p in liste] == [sien["id"]]
        assert all(p["user_id"] == user["id"] for p in liste)

        # get_owned : le repère d'autrui est invisible pour alice...
        assert storage.map_pins.get_owned(user["id"], etrange["id"]) is None
        # ...mais visible pour son propriétaire.
        assert storage.map_pins.get_owned(other_user["id"], etrange["id"])["label"] == "Chez Bob"

    def test_update_only_touches_an_owned_pin(self, storage, user, other_user):
        pin = storage.map_pins.create(user["id"], "Avant", 48.85, 2.35)

        piraterie = storage.map_pins.update(other_user["id"], pin["id"],
                                            {"label": "Piraté"})
        legitime = storage.map_pins.update(user["id"], pin["id"],
                                           {"label": "Après", "note": "maj", "icon": "🏠"})

        assert piraterie is None
        assert legitime["label"] == "Après"
        assert legitime["note"] == "maj"
        assert legitime["icon"] == "🏠"
        assert (legitime["latitude"], legitime["longitude"]) == (48.85, 2.35)

    def test_delete_is_scoped_and_idempotent(self, storage, user, other_user):
        pin = storage.map_pins.create(user["id"], "À supprimer", 48.85, 2.35)

        assert storage.map_pins.delete(other_user["id"], pin["id"]) is False
        assert storage.map_pins.delete(user["id"], pin["id"]) is True
        assert storage.map_pins.delete(user["id"], pin["id"]) is False
        assert storage.map_pins.list_for_user(user["id"]) == []

    def test_deleting_the_user_cascades_to_his_pins(self, storage, user):
        pin = storage.map_pins.create(user["id"], "Éphémère", 48.85, 2.35)

        storage.users.delete_user(user["id"])

        assert storage.map_pins.get_owned(user["id"], pin["id"]) is None


# ---------------------------------------------------------------------------
# Contraintes CHECK : la base refuse ce que la route aurait raté
# ---------------------------------------------------------------------------


@pytest.fixture
def pin_sql(storage, user):
    """Un INSERT brut paramétrable, pour viser directement les contraintes."""
    def _insert(lat, lon):
        conn = storage._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO map_pins (user_id, label, latitude, longitude)"
                    " VALUES (%s, %s, %s, %s)",
                    (user["id"], "Test contrainte", lat, lon),
                )
                conn.commit()
                return True
        except psycopg2.IntegrityError:
            conn.rollback()
            return False
        finally:
            storage._release_conn(conn)
    return _insert


class TestCheckConstraints:
    def test_zero_zero_is_refused_by_the_database(self, pin_sql):
        assert pin_sql(0.0, 0.0) is False

    @pytest.mark.parametrize(("lat", "lon"), [(0.0, 2.35), (48.85, 0.0)])
    def test_each_component_alone_cannot_be_zero(self, pin_sql, lat, lon):
        assert pin_sql(lat, lon) is False

    @pytest.mark.parametrize(
        ("lat", "lon"),
        [(999.0, 2.35), (-91.0, 2.35), (48.85, 181.0), (48.85, -999.0)],
    )
    def test_out_of_bounds_values_are_refused(self, pin_sql, lat, lon):
        assert pin_sql(lat, lon) is False

    def test_french_metropolitan_coordinates_are_accepted(self, pin_sql):
        assert pin_sql(48.8566, 2.3522) is True

    def test_null_latitude_is_refused(self, storage, user):
        conn = storage._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO map_pins (user_id, label, latitude, longitude)"
                    " VALUES (%s, %s, NULL, 2.35)",
                    (user["id"], "Sans position"),
                )
        except psycopg2.IntegrityError:
            conn.rollback()
        else:
            conn.rollback()
            pytest.fail("latitude NOT NULL attendue")
        finally:
            storage._release_conn(conn)


# ---------------------------------------------------------------------------
# Cache commune_centres (#26 transverse)
# ---------------------------------------------------------------------------


class TestCommuneCentresCache:
    def test_upsert_then_read(self, storage):
        storage.commune_geo.set_cached("postal:31500", 43.6007, 1.4328)
        storage.commune_geo.set_cached("postal:31500", 43.6046, 1.4442)  # conflit -> update

        cached = storage.commune_geo.get_cached("postal:31500")

        assert cached["latitude"] == pytest.approx(43.6046)
        assert cached["longitude"] == pytest.approx(1.4442)
        assert cached["resolved_at"] is not None

    def test_an_unresolved_key_reads_none(self, storage):
        assert storage.commune_geo.get_cached("postal:00000") is None
