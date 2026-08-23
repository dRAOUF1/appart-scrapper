"""Contrats du cache Orpi (slug <- périmètre) testables sans base.

Le repository est calqué sur Century21GeoRepository / SelogerGeoRepository :
une table à clé unique `area_key` et un upsert `ON CONFLICT`. La distinction
porteuse — pas de ligne (jamais tenté) vs ligne à `slug_id` NULL (échec
mémorisé, cooldown de 7 jours) — est rendue par `get_cached` qui renvoie
`None` ou un dict.

Le SQL réel est validé contre un vrai Postgres dans
tests/integration/test_orpi_geo_repo.py.
"""

from __future__ import annotations

import pytest

from repositories.orpi_geo_repo import OrpiGeoRepository
from tests.helpers.fakes import RecordingConnection, bind_repository


class TestGetCached:
    def test_no_row_means_never_attempted(self):
        """Sémantique porteuse : `None` (pas de ligne) déclenche une résolution,
        une ligne avec `slug_id` à NULL active le délai de 7 jours avant nouvelle
        tentative — les confondre ferait soit ré-interroger l'autocomplete à
        chaque scrape, soit ne plus jamais réessayer."""
        conn = RecordingConnection(results=[None])
        repo = bind_repository(OrpiGeoRepository, conn)

        assert repo.get_cached("93066") is None

    def test_a_row_with_a_null_slug_id_is_a_remembered_failure(self):
        conn = RecordingConnection(results=[{"slug_id": None, "resolved_at": "2026-08-23T10:00:00"}])
        repo = bind_repository(OrpiGeoRepository, conn)

        cached = repo.get_cached("dept:99")

        assert cached is not None
        assert cached["slug_id"] is None

    def test_a_resolved_row_round_trips(self):
        conn = RecordingConnection(
            results=[{"slug_id": "rosny-sous-bois", "resolved_at": "2026-08-23T10:00:00"}]
        )
        repo = bind_repository(OrpiGeoRepository, conn)

        cached = repo.get_cached("93066")

        assert cached["slug_id"] == "rosny-sous-bois"

    @pytest.mark.parametrize(
        "area_key",
        ["93066", "city:93000", "region:11", "dept:93"],
        ids=["commune-insee-nu", "ville-entiere", "region", "departement"],
    )
    def test_every_scope_level_uses_the_same_key_column(self, area_key):
        """Le niveau fait partie de la clé (`region:11` et `dept:11`
        coexistent) : une seule colonne suffit, à condition que le préfixe
        soit toujours posé par `area_cache_key`."""
        conn = RecordingConnection(results=[None])
        repo = bind_repository(OrpiGeoRepository, conn)

        repo.get_cached(area_key)

        _, params = conn.executed[0]
        assert params == (area_key,)
        assert "orpi_geo_ids" in conn.sql[0]


class TestSetCached:
    def test_caching_a_slug_upserts(self):
        """`area_key` est PRIMARY KEY : sans le `ON CONFLICT DO UPDATE`, la
        seconde résolution d'un même périmètre lèverait une `UniqueViolation` au
        milieu d'un scrape."""
        conn = RecordingConnection(results=[None])
        repo = bind_repository(OrpiGeoRepository, conn)

        repo.set_cached("93066", "rosny-sous-bois")

        assert "ON CONFLICT" in conn.sql[0]
        assert conn.commits == 1
        _, params = conn.executed[0]
        assert params == ("93066", "rosny-sous-bois", "rosny-sous-bois")

    def test_caching_a_failure_is_an_explicit_write(self):
        """Mémoriser l'échec est une écriture comme une autre : c'est ce qui
        évite de réinterroger l'autocomplete à chaque scrape pour un périmètre
        qu'il ne connaît pas (NULL accepté comme valeur de slug_id)."""
        conn = RecordingConnection(results=[None])
        repo = bind_repository(OrpiGeoRepository, conn)

        repo.set_cached("dept:99", None)

        _, params = conn.executed[0]
        assert params[0] == "dept:99"
        assert None in params
