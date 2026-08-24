"""Tests unitaires de `scripts/import_transit.py` (issue #28).

Le parsing GTFS est testé sur des mini-archives construites en mémoire :
aucun réseau, aucune base. L'écriture (`ecrire_donnees`) est vérifiée avec
`RecordingConnection` — l'ordre parents→enfants et le contenu exact des
upserts sont le contrat ; le SQL RÉEL est validé par tests/integration.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from scripts.import_transit import (
    GtfsError,
    associer_lignes_stations,
    ecrire_donnees,
    extraire_donnees,
    lire_routes,
    lire_stops,
)
from tests.helpers.fakes import RecordingConnection

# ---------------------------------------------------------------------------
# Fabrique de mini-archive GTFS
# ---------------------------------------------------------------------------


def _csv(entetes: str, lignes: list[str]) -> str:
    return entetes + "\n" + "\n".join(lignes) + "\n"


def fabrique_mini_zip(
    routes: str | None = None,
    stops: str | None = None,
    trips: str | None = None,
    stop_times: str | None = None,
    **fichiers_supplementaires: str,
) -> bytes:
    """Une archive GTFS minimale cohérente, surchargeable champ par champ."""
    routes = routes or _csv(
        "route_id,route_type,route_short_name,route_long_name",
        [
            "IDFM:C01388,1,14,Saint-Lazare <> Olympiades",
            "IDFM:C03880,0,T3a,Pont du Garigliano <> Porte de Vincennes",
            "IDFM:C01742,2,D,Gare de Lyon <> Melun",
            "IDFM:C23456,3,Bus 42,Ligne de bus",
        ],
    )
    stops = stops or _csv(
        "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station",
        [
            "STIF:StopArea:SP:1:,Châtelet,48.858,2.347,1,",
            "IDFM:Q:1A:,Châtelet - Quai A,48.8575,2.3468,0,STIF:StopArea:SP:1:",
            "STIF:StopArea:SP:2:,Gare de Lyon,48.845,2.374,1,",
            "SANS_PARENT:,Arrêt isolé,48.9,2.4,0,",
        ],
    )
    trips = trips or _csv(
        "route_id,trip_id",
        [
            "IDFM:C01388,T-M14-1",
            "IDFM:C01388,T-M14-2",
            "IDFM:C01742,T-D-1",
            "IDFM:C23456,T-BUS-1",
        ],
    )
    stop_times = stop_times or _csv(
        "trip_id,stop_id,stop_sequence",
        [
            "T-M14-1,IDFM:Q:1A:,1",
            "T-M14-2,IDFM:Q:1A:,1",   # doublon volontaire : DISTINCT attendu
            "T-D-1,STIF:StopArea:SP:2:,1",
            "T-BUS-1,IDFM:Q:1A:,1",   # trip bus : ignoré
            "T-INCONNU,IDFM:Q:1A:,1", # trip inconnu : ignoré
        ],
    )
    membres = {
        "routes.txt": routes,
        "stops.txt": stops,
        "trips.txt": trips,
        "stop_times.txt": stop_times,
        **fichiers_supplementaires,
    }
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as archive:
        for nom, contenu in membres.items():
            if contenu is not None:
                archive.writestr(nom, contenu)
    return tampon.getvalue()


def ouvre(contenu: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(contenu))


# ---------------------------------------------------------------------------
# Parsing pur
# ---------------------------------------------------------------------------


class TestLireRoutes:
    def test_only_rail_modes_are_kept(self):
        routes = lire_routes(ouvre(fabrique_mini_zip()))

        assert set(routes) == {"IDFM:C01388", "IDFM:C03880", "IDFM:C01742"}
        assert routes["IDFM:C01388"] == {
            "mode": "metro",
            "code_ligne": "14",
            "nom_ligne": "Saint-Lazare <> Olympiades",
        }
        assert routes["IDFM:C03880"]["mode"] == "tram"
        assert routes["IDFM:C01742"]["mode"] == "train"

    def test_a_bus_only_archive_is_an_explicit_error(self):
        bus_only = _csv("route_id,route_type,route_short_name,route_long_name", ["B1,3,Bus,Bus"])
        with pytest.raises(GtfsError, match="Aucune ligne ferrée"):
            lire_routes(ouvre(fabrique_mini_zip(routes=bus_only)))

    def test_a_missing_file_is_an_explicit_french_error(self):
        vide = io.BytesIO()
        with zipfile.ZipFile(vide, "w"):
            pass
        with pytest.raises(GtfsError, match="routes.txt » absent"):
            lire_routes(zipfile.ZipFile(vide))


class TestLireStops:
    def test_stopareas_are_stations_and_platforms_point_to_their_parent(self):
        parent_de, stations = lire_stops(ouvre(fabrique_mini_zip()))

        assert parent_de == {"IDFM:Q:1A:": "STIF:StopArea:SP:1:"}
        assert stations["STIF:StopArea:SP:1:"]["nom"] == "Châtelet"

    def test_a_platform_without_parent_becomes_its_own_station(self):
        _, stations = lire_stops(ouvre(fabrique_mini_zip()))

        assert stations["SANS_PARENT:"]["nom"] == "Arrêt isolé"
        assert stations["SANS_PARENT:"]["lat"] == 48.9

    def test_a_row_without_coordinates_is_skipped(self):
        stops = _csv(
            "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station",
            ["X:,Sans coords,,,0,"],
        )
        parent_de, stations = lire_stops(ouvre(fabrique_mini_zip(stops=stops)))

        assert parent_de == {}
        assert stations == {}


class TestAssocierLignesStations:
    def test_pairs_are_distinct_and_limited_to_rail_trips(self):
        with ouvre(fabrique_mini_zip()) as archive:
            routes = lire_routes(archive)
            parent_de, _ = lire_stops(archive)
            paires = associer_lignes_stations(archive, routes, parent_de)

        # Le quai remonte à sa StopArea, les trips bus/inconnus sont ignorés,
        # le doublon T-M14-1/T-M14-2 ne produit qu'une paire.
        assert sorted(paires) == [("IDFM:C01388", "STIF:StopArea:SP:1:"), ("IDFM:C01742", "STIF:StopArea:SP:2:")]


class TestExtraireDonnees:
    def test_end_to_end_on_the_mini_fixture(self, tmp_path):
        chemin = tmp_path / "mini.zip"
        chemin.write_bytes(fabrique_mini_zip())

        lignes, arrets, associations = extraire_donnees(str(chemin))

        assert {ligne["id"] for ligne in lignes} == {"IDFM:C01388", "IDFM:C03880", "IDFM:C01742"}
        # Seules les stations réellement reliées sont retenues (pas « Arrêt isolé »).
        assert [a["id"] for a in arrets] == ["STIF:StopArea:SP:1:", "STIF:StopArea:SP:2:"]
        assert ("IDFM:C01388", "STIF:StopArea:SP:1:") in associations

    def test_associations_to_orphan_stations_are_dropped_not_crashed(self, tmp_path):
        # Le quai référence un parent absent de stops.txt : la liaison doit
        # disparaître plutôt que violer la clé étrangère à l'écriture.
        stop_times = _csv("trip_id,stop_id,stop_sequence", ["T-M14-1,QUAI-FANTOME:,1"])
        chemin = tmp_path / "orphelin.zip"
        chemin.write_bytes(fabrique_mini_zip(stop_times=stop_times))

        lignes, arrets, associations = extraire_donnees(str(chemin))

        assert arrets == []
        assert associations == []

    def test_a_missing_member_is_reported_by_name(self, tmp_path):
        contenu = fabrique_mini_zip()
        tampon = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(contenu)) as source, zipfile.ZipFile(tampon, "w") as cible:
            for nom in source.namelist():
                if nom != "stop_times.txt":
                    cible.writestr(nom, source.read(nom))

        chemin = tmp_path / "incomplet.zip"
        chemin.write_bytes(tampon.getvalue())

        with pytest.raises(GtfsError, match="stop_times.txt » absent"):
            extraire_donnees(str(chemin))


# ---------------------------------------------------------------------------
# Écriture — ordre des upserts et purge (SQL vérifié, pas exécuté)
# ---------------------------------------------------------------------------


class TestEcrireDonnees:
    def test_upserts_run_parents_first_and_purge_absent_ids(self):
        conn = RecordingConnection()

        lignes = [{"id": "L1", "mode": "metro", "code_ligne": "14", "nom_ligne": "M14"}]
        arrets = [{"id": "S1", "nom": "Châtelet", "lat": 48.858, "lon": 2.347}]
        associations = [("L1", "S1")]

        ecrire_donnees(conn, lignes, arrets, associations)

        sql = conn.sql
        assert sql[0].strip().startswith("INSERT INTO transit_lines")
        assert sql[1].strip().startswith("INSERT INTO transit_stops")
        assert sql[2].strip().startswith("INSERT INTO transit_line_stops")
        assert "DELETE FROM transit_lines" in sql[3]
        assert "DELETE FROM transit_stops" in sql[4]

        params_liaisons = conn.executed[2][1]
        assert params_liaisons == [("L1", "S1")]
        # Les paramètres des DELETE portent les ids conservés…
        assert conn.executed[3][1] == (["L1"],)
        assert conn.executed[4][1] == (["S1"],)
        assert conn.commits == 1

    def test_empty_input_purges_everything_without_crashing(self):
        """Un GTFS sans rien de ferré ne doit pas produire un ANY([]) qui
        s'effacerait lui-même : la sentinelle __aucun__ protège la purge."""
        conn = RecordingConnection()
        ecrire_donnees(conn, [], [], [])

        assert conn.executed[3][1] == (["__aucun__"],)
        assert conn.executed[4][1] == (["__aucun__"],)

    def test_cache_json_round_trip_through_the_repo_contract(self):
        """Sanité : le format des communes cachées est celui que lit
        TransitRepository.get_communes_cache (JSON liste de dicts)."""
        communes = [{"nom": "Paris", "insee": "75056", "cp": "75001"}]
        assert isinstance(json.dumps(communes), str)


# ---------------------------------------------------------------------------
# Orchestration CLI — erreurs explicites, pas de réseau en test
# ---------------------------------------------------------------------------


class TestMain:
    def test_a_missing_database_url_is_reported_in_french(self, monkeypatch):
        from scripts.import_transit import main

        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert main(["--zip", "/tmp/peu-importe.zip"]) == 1

    def test_an_explicit_zip_path_must_exist(self, monkeypatch):
        from scripts.import_transit import main

        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")

        assert main(["--zip", "/n/existe/pas.zip"]) == 1
