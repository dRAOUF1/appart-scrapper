"""Contrats du cache Foncia (slug <- périmètre) testables sans base.

Le repository est calqué sur OrpiGeoRepository (lui-même repris de
SelogerGeoRepository) : une table à clé unique `area_key` et un upsert
`ON CONFLICT`. La distinction porteuse — pas de ligne (jamais tenté) vs ligne
à `slug_id` NULL (échec mémorisé) — est rendue par `get_cached` qui renvoie
`None` ou un dict.

Le SQL réel est validé contre un vrai Postgres dans
tests/integration/ (mêmes conventions que les autres caches géo).
"""

from __future__ import annotations

import pytest

from repositories.foncia_geo_repo import FonciaGeoRepository
from tests.helpers.fakes import RecordingConnection, bind_repository


class TestGetCached:
    def test_no_row_means_never_attempted(self):
        """Sémantique porteuse : `None` (pas de ligne) déclenche une
        résolution, une ligne avec `slug_id` à NULL active le délai de 7 jours
        avant nouvelle tentative — les confondre ferait soit ré-interroger
        l'API géo à chaque scrape, soit ne plus jamais réessayer."""
        conn = RecordingConnection(results=[None])
        repo = bind_repository(FonciaGeoRepository, conn)

        assert repo.get_cached("31555") is None

    def test_a_row_with_a_null_slug_id_is_a_remembered_failure(self):
        conn = RecordingConnection(
            results=[{"slug_id": None, "resolved_at": "2026-08-15T10:00:00"}]
        )
        repo = bind_repository(FonciaGeoRepository, conn)

        cached = repo.get_cached("dept:999")

        assert cached is not None
        assert cached["slug_id"] is None

    def test_a_resolved_row_round_trips(self):
        conn = RecordingConnection(
            results=[{"slug_id": "toulouse-31", "resolved_at": "2026-08-15T10:00:00"}]
        )
        repo = bind_repository(FonciaGeoRepository, conn)

        cached = repo.get_cached("city:31555")

        assert cached["slug_id"] == "toulouse-31"

    @pytest.mark.parametrize(
        "area_key",
        [
            "56260",
            "city:31555",
            "dept:33",
            "region:76",
        ],
        ids=["commune-insee-nu", "ville-entiere", "departement", "region"],
    )
    def test_every_scope_level_uses_the_same_key_column(self, area_key):
        """Le niveau fait partie de la clé (`dept:33` et `region:33`
        coexistent) : une seule colonne suffit, à condition que le préfixe
        soit toujours posé par `area_cache_key`."""
        conn = RecordingConnection(results=[None])
        repo = bind_repository(FonciaGeoRepository, conn)

        assert repo.get_cached(area_key) is None


class TestSetCached:
    def test_a_first_write_inserts_the_slug(self):
        conn = RecordingConnection()
        repo = bind_repository(FonciaGeoRepository, conn)

        repo.set_cached("city:69123", "lyon-69")

        sql, params = conn.executed[0]
        assert "INSERT INTO foncia_geo_ids" in sql
        assert "ON CONFLICT (area_key) DO UPDATE" in sql  # idempotent
        assert params == ("city:69123", "lyon-69", "lyon-69")
        assert conn.commits == 1

    def test_an_empty_result_is_stored_as_null(self):
        """Un échec se mémorise : slug NULL + resolved_at frais, que
        resolve_slug_id interprète comme « réessayer plus tard »."""
        conn = RecordingConnection()
        repo = bind_repository(FonciaGeoRepository, conn)

        repo.set_cached("dept:2A", None)

        params = conn.executed[0][1]
        assert params == ("dept:2A", None, None)
