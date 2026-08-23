"""Contrats du cache PAP (identifiant numérique <- périmètre) testables sans base.

Le repository est calqué sur Century21GeoRepository / SelogerGeoRepository /
OrpiGeoRepository : une table à clé unique `area_key` et un upsert
`ON CONFLICT`. La distinction porteuse — pas de ligne (jamais tenté) vs ligne
à `geo_id` NULL (échec mémorisé, cooldown de 7 jours) — est rendue par
`get_cached` qui renvoie `None` ou un dict.

La valeur cachée est l'entier opaque propre à pap.fr (`439` pour Paris,
`37782` pour Paris 15e), sous forme de chaîne — une valeur unique, comme le
placeId de SeLoger ou le slug d'Orpi (pas une liste comme les zoneIds de
bienici).

Le SQL réel est validé contre un vrai Postgres dans
tests/integration/test_pap_geo_repo.py.
"""

from __future__ import annotations

import pytest

from repositories.pap_geo_repo import PapGeoRepository
from tests.helpers.fakes import RecordingConnection, bind_repository


class TestGetCached:
    def test_no_row_means_never_attempted(self):
        """Sémantique porteuse : `None` (pas de ligne) déclenche une résolution,
        une ligne avec `geo_id` à NULL active le délai de 7 jours avant nouvelle
        tentative — les confondre ferait soit ré-interroger l'autocomplete à
        chaque scrape, soit ne plus jamais réessayer."""
        conn = RecordingConnection(results=[None])
        repo = bind_repository(PapGeoRepository, conn)

        assert repo.get_cached("35238") is None

    def test_a_row_with_a_null_geo_id_is_a_remembered_failure(self):
        conn = RecordingConnection(results=[{"geo_id": None, "resolved_at": "2026-08-23T10:00:00"}])
        repo = bind_repository(PapGeoRepository, conn)

        cached = repo.get_cached("dept:99")

        assert cached is not None
        assert cached["geo_id"] is None

    def test_a_resolved_row_round_trips(self):
        conn = RecordingConnection(results=[{"geo_id": "37782", "resolved_at": "2026-08-23T10:00:00"}])
        repo = bind_repository(PapGeoRepository, conn)

        cached = repo.get_cached("75115")

        assert cached["geo_id"] == "37782"

    @pytest.mark.parametrize(
        "area_key",
        ["75115", "city:75056", "region:11", "dept:33"],
        ids=["commune-insee-nu", "ville-entiere", "region", "departement"],
    )
    def test_every_scope_level_uses_the_same_key_column(self, area_key):
        """Le niveau fait partie de la clé (`region:11` et `dept:11`
        coexistent) : une seule colonne suffit, à condition que le préfixe
        soit toujours posé par `area_cache_key`."""
        conn = RecordingConnection(results=[None])
        repo = bind_repository(PapGeoRepository, conn)

        repo.get_cached(area_key)

        _, params = conn.executed[0]
        assert params == (area_key,)
        assert "pap_geo_ids" in conn.sql[0]


class TestSetCached:
    def test_caching_a_geo_id_upserts(self):
        """`area_key` est PRIMARY KEY : sans le `ON CONFLICT DO UPDATE`, la
        seconde résolution d'un même périmètre lèverait une `UniqueViolation`
        au milieu d'un scrape."""
        conn = RecordingConnection(results=[None])
        repo = bind_repository(PapGeoRepository, conn)

        repo.set_cached("75115", "37782")

        assert "ON CONFLICT" in conn.sql[0]
        assert conn.commits == 1
        _, params = conn.executed[0]
        assert params == ("75115", "37782", "37782")

    def test_caching_a_failure_is_an_explicit_write(self):
        """Mémoriser l'échec est une écriture comme une autre : c'est ce qui
        évite de réinterroger l'autocomplete à chaque scrape pour un périmètre
        qu'il ne connaît pas (NULL accepté comme valeur de geo_id)."""
        conn = RecordingConnection(results=[None])
        repo = bind_repository(PapGeoRepository, conn)

        repo.set_cached("dept:99", None)

        _, params = conn.executed[0]
        assert params[0] == "dept:99"
        assert None in params
