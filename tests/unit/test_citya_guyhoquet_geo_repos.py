"""Contrats SQL identiques des caches géographiques Citya et Guy Hoquet."""

import pytest

from repositories.citya_geo_repo import CityaGeoRepository
from repositories.guyhoquet_geo_repo import GuyHoquetGeoRepository
from tests.helpers.fakes import RecordingConnection, bind_repository


@pytest.mark.parametrize(
    ("repo_cls", "table"),
    [(CityaGeoRepository, "citya_geo_ids"), (GuyHoquetGeoRepository, "guyhoquet_geo_ids")],
)
def test_get_cached_distinguishes_absence_failure_and_success(repo_cls, table):
    cases = [
        ("jamais", None, None),
        ("echec", {"area_key": "x", "slug_id": None}, None),
        ("succes", {"slug_id": "slug-ok"}, "slug-ok"),
    ]
    for key, row, expected_slug in cases:
        conn = RecordingConnection(results=[row])
        repo = bind_repository(repo_cls, conn)
        cached = repo.get_cached(key)
        assert (cached is None) is (row is None)
        assert (cached["slug_id"] if cached is not None else None) == expected_slug
        assert f"FROM {table}" in conn.executed[0][0]
        assert conn.executed[0][1] == (key,)


@pytest.mark.parametrize(
    ("repo_cls", "table"),
    [(CityaGeoRepository, "citya_geo_ids"), (GuyHoquetGeoRepository, "guyhoquet_geo_ids")],
)
@pytest.mark.parametrize("slug", ["toulouse-31555", None])
def test_set_cached_upserts_success_or_failure(repo_cls, table, slug):
    conn = RecordingConnection()
    repo = bind_repository(repo_cls, conn)

    repo.set_cached("city:31555", slug)

    sql, params = conn.executed[0]
    assert f"INSERT INTO {table}" in sql
    assert "ON CONFLICT (area_key) DO UPDATE" in sql
    assert params == ("city:31555", slug, slug)
    assert conn.commits == 1
