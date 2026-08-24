"""Intégration du référentiel transit (#28) contre un vrai Postgres.

Ce que les unitaires ne prouvent PAS : que le DDL des quatre tables s'exécute
et se relit, que les upserts idempotents du script d'import respectent les FK
et purgent réellement, et que le cache communes∩rayon fait son aller-retour
(y compris un [] stocké, distinct de « jamais calculé »).
"""

from __future__ import annotations

import psycopg2
import pytest

from storage import Storage

# La fabrique de mini-GTFS vit dans les tests unitaires du script : une seule
# source de vérité sur la forme d'une archive.
from tests.unit.test_import_transit import fabrique_mini_zip


@pytest.fixture
def transit_repo(storage):
    from repositories.transit_repo import TransitRepository

    return TransitRepository(storage.database_url)


def _importe_mini_gtfs(pg_url, tmp_path):
    chemin = tmp_path / "mini-gtfs.zip"
    chemin.write_bytes(fabrique_mini_zip())
    from scripts.import_transit import ecrire_donnees, extraire_donnees

    lignes, arrets, associations = extraire_donnees(str(chemin))
    conn = psycopg2.connect(pg_url, connect_timeout=10)
    try:
        ecrire_donnees(conn, lignes, arrets, associations)
    finally:
        conn.close()
    return lignes, arrets, associations


class TestMigrationsTransit:
    def test_the_four_tables_exist_after_migration(self, storage, clean_db, sql):
        for table in (
            "transit_lines", "transit_stops", "transit_line_stops",
            "transit_communes_rayon",
        ):
            noms = [r[0] for r in sql.all(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (table,),
            )]
            assert noms, f"table {table} absente après migration"

    def test_migrations_run_twice_without_error(self, pg_url, clean_db):
        """Idempotence : le second run ne doit ni échouer ni dupliquer."""
        Storage.run_migrations(pg_url)

        conn = psycopg2.connect(pg_url, connect_timeout=10)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name LIKE 'transit_%'"
                )
                assert cur.fetchone()[0] >= 4
        finally:
            conn.close()


class TestImportUpserts:
    def test_import_persists_lines_stops_and_associations(
        self, pg_url, transit_repo, clean_db, tmp_path,
    ):
        _importe_mini_gtfs(pg_url, tmp_path)

        assert {ligne["code_ligne"] for ligne in transit_repo.search_lines("", limit=100)} == {
            "14", "T3a", "D",
        }
        stations = transit_repo.get_line_stops("IDFM:C01388")
        assert [s["nom"] for s in stations] == ["Châtelet"]

    def test_a_second_import_is_idempotent_and_purges_deleted_lines(
        self, pg_url, transit_repo, clean_db, tmp_path,
    ):
        _importe_mini_gtfs(pg_url, tmp_path)

        # Second import amputé de la ligne D : elle doit disparaître (cascade
        # sur ses liaisons), les autres rester inchangées.
        contenu = fabrique_mini_zip(routes=(
            "route_id,route_type,route_short_name,route_long_name\n"
            "IDFM:C01388,1,14,Saint-Lazare <> Olympiades\n"
        ))
        chemin = tmp_path / "mini-gtfs-ampute.zip"
        chemin.write_bytes(contenu)

        from scripts.import_transit import ecrire_donnees, extraire_donnees

        lignes, arrets, associations = extraire_donnees(str(chemin))
        conn = psycopg2.connect(pg_url, connect_timeout=10)
        try:
            ecrire_donnees(conn, lignes, arrets, associations)
        finally:
            conn.close()

        codes = {ligne["code_ligne"] for ligne in transit_repo.search_lines("", limit=100)}
        assert codes == {"14"}
        # Les stations non reliées à une ligne restante sont purgées aussi.
        assert transit_repo.get_stops(["STIF:StopArea:SP:2:"]) == []


class TestCacheCommunesRayon:
    def test_round_trip(self, transit_repo, clean_db):
        communes = [{"nom": "Paris", "insee": "75056", "cp": "75001"}]

        assert transit_repo.get_communes_cache("station:S1:500m") is None

        transit_repo.set_communes_cache("station:S1:500m", communes)
        assert transit_repo.get_communes_cache("station:S1:500m") == communes

    def test_an_empty_list_is_stored_and_read_as_empty_not_none(self, transit_repo, clean_db):
        """[] = « calcul fait, aucune commune » : il doit survivre au
        round-trip sans devenir un cache miss."""
        transit_repo.set_communes_cache("station:S2:2000m", [])

        assert transit_repo.get_communes_cache("station:S2:2000m") == []

    def test_an_overwrite_replaces_the_value(self, transit_repo, clean_db):
        transit_repo.set_communes_cache("station:S3:500m", [{"nom": "A"}])
        transit_repo.set_communes_cache("station:S3:500m", [{"nom": "B"}])

        assert transit_repo.get_communes_cache("station:S3:500m") == [{"nom": "B"}]
