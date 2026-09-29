"""Cache Bien'ici sans Postgres : absence, JSON et upsert."""

import pytest

from repositories.bienici_geo_repo import BienIciGeoRepository
from tests.helpers.fakes import RecordingConnection, bind_repository


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (None, None),
        ({"area_key": "x", "zone_ids": None}, None),
        ({"area_key": "x", "zone_ids": '["zone-a", "zone-b"]'}, ["zone-a", "zone-b"]),
    ],
)
def test_get_cached_distinguishes_absence_failure_and_resolved_zones(row, expected):
    conn = RecordingConnection(results=[row])
    repo = bind_repository(BienIciGeoRepository, conn)

    cached = repo.get_cached("dept:33")

    assert (cached is None) is (row is None)
    if cached is not None:
        assert cached["zone_ids"] == expected
    assert conn.executed[0][1] == ("dept:33",)


@pytest.mark.parametrize(("zone_ids", "encoded"), [(["zone-a", "zone-b"], '["zone-a", "zone-b"]'), (None, None)])
def test_set_cached_upserts_success_or_failure(zone_ids, encoded):
    conn = RecordingConnection()
    repo = bind_repository(BienIciGeoRepository, conn)

    repo.set_cached("region:75", zone_ids)

    sql, params = conn.executed[0]
    assert "INSERT INTO bienici_zone_ids" in sql
    assert "ON CONFLICT (area_key) DO UPDATE" in sql
    assert params == ("region:75", encoded, encoded)
    assert conn.commits == 1
