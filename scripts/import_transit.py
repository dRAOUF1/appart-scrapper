"""Import du référentiel transports franciliens (issue #28) — GTFS ÎDF Mobilités.

Script HORS Flask, relançable, idempotent : lit l'archive GTFS d'ÎDF Mobilités
et remplit transit_lines / transit_stops / transit_line_stops. L'application
elle-même n'écrit jamais dans ces tables.

Périmètre : les modes FERRÉS uniquement (route_type GTFS 0 tram, 1 métro,
2 rail) — pas de bus. Le route_type 2 couvre indifféremment RER et trains
Transilien dans ce flux : ils sont importés en mode « train », le code ligne
(RER A, L…) restant ce qui distingue visuellement.

Usage :
    python -m scripts.import_transit                    # télécharge le zip officiel
    python -m scripts.import_transit --zip /chemin.zip  # réutilise une archive locale
    python -m scripts.import_transit --database-url ... # cible explicite (sinon DATABASE_URL)

Le téléchargement est streamé sur disque (l'archive dépasse 100 Mo), puis la
lecture des membres se fait fichier par fichier via zipfile (aucun chargement
complet en mémoire) : stop_times.txt compte plusieurs millions de lignes et ne
doit JAMAIS être lu d'un bloc.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

GTFS_URL = "https://eu.ftp.opendatasoft.com/stif/GTFS/IDFM-gtfs.zip"
DEFAULT_ZIP_PATH = "/tmp/opencode/gtfs/IDFM-gtfs.zip"

# Modes ferrés du périmètre : route_type GTFS -> mode canonique.
# (2 = rail couvre RER + Transilien ; voir docstring.)
MODES_FERRES = {0: "tram", 1: "metro", 2: "train"}

FICHIERS_REQUIS = ("routes.txt", "trips.txt", "stops.txt", "stop_times.txt")


class GtfsError(ValueError):
    """Archive ou fichier GTFS inexploitable — message prêt à afficher."""


# ---------------------------------------------------------------------------
# Lecture CSV streamée
# ---------------------------------------------------------------------------


def _lit_csv_zip(archive: zipfile.ZipFile, nom_fichier: str):
    """Itérateur de dicts sur un membre CSV de l'archive (utf-8-sig : le BOM
    que certains exports GTFS placent en tête casse la première colonne)."""
    try:
        membre = archive.open(nom_fichier)
    except KeyError as exc:
        raise GtfsError(
            f"Fichier « {nom_fichier} » absent de l'archive GTFS : "
            "cette archive n'est pas un export ÎDF Mobilités complet."
        ) from exc
    lecteur = csv.DictReader(io.TextIOWrapper(membre, encoding="utf-8-sig", newline=""))
    if lecteur.fieldnames is None:
        raise GtfsError(f"Fichier « {nom_fichier} » vide ou illisible.")
    for ligne in lecteur:
        yield {k: (v or "").strip() for k, v in ligne.items() if k}


def _entier_gtfs(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _flottant_gtfs(value: str) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


# ---------------------------------------------------------------------------
# Parsing pur (testé hors base sur mini-fixtures)
# ---------------------------------------------------------------------------


def lire_routes(archive: zipfile.ZipFile) -> dict[str, dict]:
    """routes.txt -> {route_id: {mode, code_ligne, nom_ligne}}, modes ferrés
    seulement. Une route sans identifiant est ignorée (jamais devinée)."""
    routes: dict[str, dict] = {}
    for row in _lit_csv_zip(archive, "routes.txt"):
        type_route = _entier_gtfs(row.get("route_type", ""))
        mode = MODES_FERRES.get(type_route)
        route_id = row.get("route_id", "")
        if mode is None or not route_id:
            continue
        routes[route_id] = {
            "mode": mode,
            "code_ligne": row.get("route_short_name", "") or row.get("route_long_name", ""),
            "nom_ligne": row.get("route_long_name", ""),
        }
    if not routes:
        raise GtfsError(
            "Aucune ligne ferrée trouvée dans routes.txt "
            "(route_type attendu parmi 0/1/2) : archive inattendue."
        )
    return routes


def lire_stops(archive: zipfile.ZipFile) -> tuple[dict[str, str], dict[str, dict]]:
    """stops.txt -> ({quai: station}, {station: {nom, lat, lon}}).

    Les stations commerciales (StopArea) sont les lignes location_type=1 ; un
    quai (location_type=0) pointe vers sa station via parent_station. Un arrêt
    sans parent ni location_type est traité comme sa propre station : mieux
    vaut un référentiel légèrement redondant qu'une ligne entière perdue.
    """
    parent_de: dict[str, str] = {}
    stations: dict[str, dict] = {}
    for row in _lit_csv_zip(archive, "stops.txt"):
        stop_id = row.get("stop_id", "")
        lat = _flottant_gtfs(row.get("stop_lat", ""))
        lon = _flottant_gtfs(row.get("stop_lon", ""))
        if not stop_id or lat is None or lon is None:
            continue
        location_type = _entier_gtfs(row.get("location_type", "0") or "0")
        parent = row.get("parent_station", "")
        if location_type == 1:
            stations[stop_id] = {"nom": row.get("stop_name", "") or stop_id, "lat": lat, "lon": lon}
        elif parent:
            parent_de[stop_id] = parent
        else:
            stations[stop_id] = {"nom": row.get("stop_name", "") or stop_id, "lat": lat, "lon": lon}
    return parent_de, stations


def associer_lignes_stations(
    archive: zipfile.ZipFile, routes: dict[str, dict], parent_de: dict[str, str]
) -> set[tuple[str, str]]:
    """trips.txt + stop_times.txt -> {(route_id, station_id)}, DISTINCT.

    Streaming strict : trips.txt construit trip_id -> route_id pour les seuls
    trajets ferrés, puis stop_times.txt est parcouru ligne à ligne (des
    millions) sans rien accumuler au-delà des paires distinctes.
    """
    trips_ferres: dict[str, str] = {}
    for row in _lit_csv_zip(archive, "trips.txt"):
        route_id = row.get("route_id", "")
        trip_id = row.get("trip_id", "")
        if trip_id and route_id in routes:
            trips_ferres[trip_id] = route_id

    paires: set[tuple[str, str]] = set()
    for row in _lit_csv_zip(archive, "stop_times.txt"):
        route_id = trips_ferres.get(row.get("trip_id", ""))
        if route_id is None:
            continue
        # Remonter du quai à sa station commerciale quand il en a une.
        stop_id = row.get("stop_id", "")
        station_id = parent_de.get(stop_id, stop_id)
        if station_id:
            paires.add((route_id, station_id))
    return paires


def extraire_donnees(zip_path: str | Path) -> tuple[list[dict], list[dict], list[tuple[str, str]]]:
    """Lecture complète de l'archive -> (lines, stops, associations).

    Seules les stations réellement reliées à une ligne ferrée sont retenues :
    le référentiel reste petit (~quelques milliers de lignes).
    """
    with zipfile.ZipFile(zip_path) as archive:
        routes = lire_routes(archive)
        parent_de, stations = lire_stops(archive)
        associations = associer_lignes_stations(archive, routes, parent_de)

    lignes = [
        {"id": route_id, **donnees} for route_id, donnees in sorted(routes.items())
    ]
    stations_utilisees = {stop_id for _, stop_id in associations}
    arrets = [
        {"id": stop_id, **stations[stop_id]}
        for stop_id in sorted(stations_utilisees)
        if stop_id in stations
    ]
    orphelines = stations_utilisees - set(stations)
    if orphelines:
        print(
            f"Avertissement : {len(orphelines)} station(s) référencée(s) par des lignes "
            "ferroviaires absentes de stops.txt (coordonnées inconnues) — ignorées.",
            file=sys.stderr,
        )
    # Une liaison vers une station sans coordonnées violerait la clé étrangère :
    # elles sont filtrées comme les stations qu'elles pointent.
    associations = [
        (line_id, stop_id)
        for line_id, stop_id in sorted(associations)
        if stop_id not in orphelines
    ]
    return lignes, arrets, associations


# ---------------------------------------------------------------------------
# Écriture en base (upserts idempotents)
# ---------------------------------------------------------------------------

_UPSERT_LINE = """
    INSERT INTO transit_lines (id, mode, code_ligne, nom_ligne)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (id) DO UPDATE
        SET mode = EXCLUDED.mode, code_ligne = EXCLUDED.code_ligne,
            nom_ligne = EXCLUDED.nom_ligne
"""

_UPSERT_STOP = """
    INSERT INTO transit_stops (id, nom, lat, lon)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (id) DO UPDATE
        SET nom = EXCLUDED.nom, lat = EXCLUDED.lat, lon = EXCLUDED.lon
"""

_UPSERT_LIAISON = """
    INSERT INTO transit_line_stops (line_id, stop_id)
    VALUES (%s, %s)
    ON CONFLICT (line_id, stop_id) DO NOTHING
"""


def ecrire_donnees(conn, lignes: list[dict], arrets: list[dict], associations: list[tuple[str, str]]) -> None:
    """Upserts en trois temps (parents avant enfants : FK transit_line_stops)."""
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_LINE, [
            (ligne["id"], ligne["mode"], ligne["code_ligne"], ligne["nom_ligne"]) for ligne in lignes
        ])
        cur.executemany(_UPSERT_STOP, [
            (arret["id"], arret["nom"], arret["lat"], arret["lon"]) for arret in arrets
        ])
        cur.executemany(_UPSERT_LIAISON, associations)

        # Les lignes disparues du dernier GTFS doivent sortir du référentiel :
        # sinon l'autocomplete propose des lignes qui n'existent plus. Les
        # liaisons et stations orphelines suivent par ON DELETE CASCADE.
        cur.execute(
            "DELETE FROM transit_lines WHERE NOT (id = ANY(%s))",
            ([ligne["id"] for ligne in lignes] or ["__aucun__"],),
        )
        cur.execute(
            "DELETE FROM transit_stops WHERE NOT (id = ANY(%s))",
            ([a["id"] for a in arrets] or ["__aucun__"],),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Téléchargement & orchestration
# ---------------------------------------------------------------------------


def telecharger(destination: Path) -> Path:
    """Télécharge l'archive officielle en streaming (pas en mémoire)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"Téléchargement de {GTFS_URL} …")
    requete = urllib.request.Request(GTFS_URL, headers={"User-Agent": "appart-scrapper-import-transit"})
    with urllib.request.urlopen(requete, timeout=120) as reponse, open(destination, "wb") as sortie:
        while True:
            morceau = reponse.read(1024 * 1024)
            if not morceau:
                break
            sortie.write(morceau)
    print(f"Archive récupérée : {destination}")
    return destination


def main(argv: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parseur.add_argument("--zip", default=None, help="Chemin d'une archive GTFS déjà téléchargée")
    parseur.add_argument("--database-url", default=None, help="Cible PostgreSQL (défaut : DATABASE_URL)")
    parseur.add_argument(
        "--cache-dir", default=os.path.dirname(DEFAULT_ZIP_PATH),
        help=f"Dossier de téléchargement (défaut : {os.path.dirname(DEFAULT_ZIP_PATH)})",
    )
    args = parseur.parse_args(argv)

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL manquant (ni variable d'env, ni --database-url).", file=sys.stderr)
        return 1

    chemin_zip = args.zip or str(Path(args.cache_dir) / os.path.basename(DEFAULT_ZIP_PATH))
    if not os.path.exists(chemin_zip):
        if args.zip:
            print(f"Archive introuvable : {chemin_zip}", file=sys.stderr)
            return 1
        chemin_zip = str(telecharger(Path(chemin_zip)))

    print(f"Lecture de {chemin_zip} …")
    lignes, arrets, associations = extraire_donnees(chemin_zip)
    print(f"GTFS parsé : {len(lignes)} ligne(s), {len(arrets)} station(s), {len(associations)} liaison(s).")

    import psycopg2

    connexion = psycopg2.connect(database_url, connect_timeout=30)
    try:
        ecrire_donnees(connexion, lignes, arrets, associations)
    finally:
        connexion.close()
    print("Import terminé (upserts idempotents — relançable sans risque).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
