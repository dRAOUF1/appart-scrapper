"""Contrats du MapPinRepository testables sans base (issue #26).

Le repository est le rempart anti-IDOR côté données : TOUTES les méthodes
portent `user_id` dans leur WHERE, même quand la cible est désignée par son
`id`. Le SQL réel (FK, CHECK non-zéro, CASCADE) est validé contre un vrai
Postgres dans tests/integration/test_map_pins.py.
"""

from __future__ import annotations

from repositories.map_pin_repo import MapPinRepository
from tests.helpers.fakes import RecordingConnection, bind_repository

PIN_ROW = {
    "id": 7, "user_id": 42, "label": "Bon boulanger", "note": "",
    "icon": "📍", "latitude": 48.85, "longitude": 2.35,
    "created_at": None,
}


def repo_on(results=None):
    conn = RecordingConnection(results=results)
    return bind_repository(MapPinRepository, conn), conn


class TestCreate:
    def test_a_pin_is_created_for_its_owner_and_returns_the_row(self):
        repo, conn = repo_on(results=[dict(PIN_ROW)])

        pin = repo.create(42, "Bon boulanger", 48.85, 2.35)

        assert pin["id"] == 7
        assert "INSERT INTO map_pins" in conn.sql[0]
        assert "RETURNING" in conn.sql[0]
        assert conn.commits == 1
        _, params = conn.executed[0]
        assert params[:5] == (42, "Bon boulanger", "", "📍", 48.85)


class TestListForUser:
    def test_only_the_owner_rows_are_selected(self):
        repo, conn = repo_on(results=[])

        assert repo.list_for_user(42) == []
        assert "user_id = %s" in conn.sql[0]
        assert conn.executed[0][1] == (42,)


class TestGetOwned:
    def test_both_id_and_user_id_are_checked(self):
        """Le scoping vit dans LE MÊME WHERE : une ligne existante d'un autre
        utilisateur renvoie None — la route répondra 404, sans révéler quoi
        que ce soit (balayage IDOR stérile)."""
        repo, conn = repo_on(results=[None])

        assert repo.get_owned(42, 7) is None
        assert "id = %s AND user_id = %s" in conn.sql[0]
        assert conn.executed[0][1] == (7, 42)

    def test_an_owned_pin_is_returned(self):
        repo, _ = repo_on(results=[dict(PIN_ROW, label="Lycée")])

        pin = repo.get_owned(42, 7)

        assert pin["label"] == "Lycée"


class TestUpdate:
    def test_only_allowlisted_columns_reach_the_sql(self):
        """Un nom de colonne venant du payload ne doit JAMAIS entrer dans le
        SQL — ni `evil`, ni des colonnes figées comme la position ou le
        propriétaire."""
        repo, conn = repo_on(results=[dict(PIN_ROW, label="Nouveau", icon="⭐")])

        updated = repo.update(
            42, 7,
            {"label": "Nouveau", "icon": "⭐", "evil": "x",
             "latitude": 999.0, "user_id": 99},
        )

        assert updated["label"] == "Nouveau"
        set_clause = conn.sql[0].split("SET")[1].split("WHERE")[0]
        assert "label" in set_clause and "icon" in set_clause
        assert "evil" not in set_clause
        assert "latitude" not in set_clause
        assert "user_id" not in set_clause

    def test_update_is_scoped_to_the_owner(self):
        repo, conn = repo_on(results=[None])

        assert repo.update(42, 7, {"label": "Piraté"}) is None
        assert "WHERE id = %s AND user_id = %s" in conn.sql[0]

    def test_an_empty_payload_reads_instead_of_writing(self):
        repo_read, conn_read = repo_on(results=[dict(PIN_ROW)])
        repo, conn = repo_on()
        repo.get_owned = repo_read.get_owned

        result = repo.update(42, 7, {})

        assert result is not None
        assert conn.sql == []  # aucun UPDATE émis


class TestDelete:
    def test_delete_scopes_on_both_id_and_user(self):
        # Une « ligne résultat » pour que le curseur factice rapporte
        # rowcount == 1 (le double ne modélise pas DELETE nativement).
        repo, conn = repo_on(results=[[(7,)]])

        deleted = repo.delete(42, 7)

        assert deleted is True
        assert "DELETE FROM map_pins WHERE id = %s AND user_id = %s" in conn.sql[0]

    def test_deleting_someone_elses_pin_changes_nothing(self):
        repo, conn = repo_on()  # pas de résultat -> rowcount 0

        assert repo.delete(99, 7) is False
