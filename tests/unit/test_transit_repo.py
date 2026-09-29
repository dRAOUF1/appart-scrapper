"""TransitRepository sans Postgres : SQL, formes de retour et cache JSON."""

import json

import pytest

from repositories.transit_repo import TransitRepository
from tests.helpers.fakes import RecordingConnection, bind_repository


def test_search_lines_passes_query_mode_and_limit_and_shapes_rows():
    conn = RecordingConnection(results=[[{"id": "M14", "mode": "metro", "code_ligne": "14"}]])
    repo = bind_repository(TransitRepository, conn)

    assert repo.search_lines("14", mode="metro", limit=3) == [
        {"id": "M14", "mode": "metro", "code_ligne": "14"}
    ]
    sql, params = conn.executed[0]
    assert "FROM transit_lines" in sql and "ORDER BY mode, code_ligne" in sql
    assert params == ("%14%", "%14%", "metro", "metro", 3)


@pytest.mark.parametrize(("row", "expected"), [({"id": "M14"}, {"id": "M14"}), (None, None)])
def test_get_line_returns_row_or_none(row, expected):
    conn = RecordingConnection(results=[row])
    repo = bind_repository(TransitRepository, conn)
    assert repo.get_line("M14") == expected
    assert conn.executed[0][1] == ("M14",)


def test_get_line_stops_and_get_stops_shape_rows_and_parameters():
    rows = [{"id": "A", "nom": "Auber", "lat": 48.8, "lon": 2.3}]
    line_conn = RecordingConnection(results=[rows])
    assert bind_repository(TransitRepository, line_conn).get_line_stops("RERA") == rows
    assert "JOIN transit_stops" in line_conn.executed[0][0]

    stops_conn = RecordingConnection(results=[rows])
    assert bind_repository(TransitRepository, stops_conn).get_stops(["A", "B"]) == rows
    assert stops_conn.executed[0][1] == (["A", "B"],)
    assert bind_repository(TransitRepository, RecordingConnection()).get_stops([]) == []


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, None),
        ({"communes": []}, []),
        ({"communes": [{"city": "Paris"}]}, [{"city": "Paris"}]),
        ({"communes": '[{"city": "Lyon"}]'}, [{"city": "Lyon"}]),
        ({"communes": {"invalide": True}}, []),
    ],
)
def test_communes_cache_distinguishes_absence_empty_json_and_invalid_shape(stored, expected):
    conn = RecordingConnection(results=[stored])
    repo = bind_repository(TransitRepository, conn)
    assert repo.get_communes_cache("station:A:1000m") == expected


def test_set_communes_cache_serializes_unicode_and_upserts():
    conn = RecordingConnection()
    repo = bind_repository(TransitRepository, conn)
    communes = [{"city": "Évry", "inseeCode": "91228"}]

    repo.set_communes_cache("station:A:1000m", communes)

    sql, params = conn.executed[0]
    encoded = json.dumps(communes, ensure_ascii=False)
    assert "INSERT INTO transit_communes_rayon" in sql
    assert "ON CONFLICT (area_key) DO UPDATE" in sql
    assert params == ("station:A:1000m", encoded, encoded)
    assert conn.commits == 1
