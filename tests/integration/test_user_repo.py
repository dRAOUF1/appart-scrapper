"""UserRepository contre un vrai Postgres.

Ce qui ne se prouve qu'ici : la contrainte `UNIQUE` sur `username` (et l'état
de la connexion après l'`IntegrityError` qu'elle provoque), les `ON DELETE
CASCADE` en deux niveaux, les sous-requêtes corrélées de comptage, et la
comparaison `found_at >= CURRENT_DATE` faite par le serveur.
"""

from __future__ import annotations

import pytest

from tests.helpers.factories import make_criteria, make_listing
from tests.integration.conftest import insert_search, insert_user

# ---------------------------------------------------------------------------
# create_user
# ---------------------------------------------------------------------------

class TestCreateUser:
    def test_created_user_is_readable_by_id_and_username(self, storage):
        created = storage.users.create_user("alice")

        by_id = storage.users.get_user_by_id(created["id"])
        by_username = storage.users.get_user_by_username("alice")
        detail = storage.users.get_user_detail(created["id"])

        assert created == {"id": created["id"], "username": "alice"}
        assert by_id["username"] == "alice"
        assert by_id["created_at"] is not None
        assert by_username["id"] == created["id"]
        assert detail["username"] == "alice"

    def test_duplicate_username_raises_value_error(self, storage):
        storage.users.create_user("alice")

        with pytest.raises(ValueError, match="'alice' est déjà pris"):
            storage.users.create_user("alice")

        assert len(storage.users.get_all_users()) == 1

    def test_the_connection_survives_the_integrity_error(self, storage):
        """Le point sensible : l'`IntegrityError` avorte la transaction. Sans
        le `conn.rollback()` du repo, la connexion repartirait au pool en
        transaction avortée et la requête suivante répondrait « current
        transaction is aborted ». On enchaîne donc plusieurs opérations
        réelles juste après l'échec.
        """
        storage.users.create_user("alice")
        with pytest.raises(ValueError, match="déjà pris"):
            storage.users.create_user("alice")

        second = storage.users.create_user("bob")
        assert storage.users.get_user_by_id(second["id"])["username"] == "bob"
        assert {u["username"] for u in storage.users.get_all_users()} == {"alice", "bob"}

    @pytest.mark.parametrize(
        "username",
        [
            "utilisateur.avec.points",
            "MAJUSCULES",           # la contrainte UNIQUE est sensible à la casse
            "majuscules",
            "espace et accent éà",
            "guillemet'simple",     # aucune interpolation : le paramètre est lié
            "'; DROP TABLE users; --",
        ],
    )
    def test_unusual_usernames_round_trip(self, storage, username):
        created = storage.users.create_user(username)

        assert storage.users.get_user_by_username(username)["id"] == created["id"]

    def test_unknown_id_and_username_return_none(self, storage, user):
        assert storage.users.get_user_by_id(999_999) is None
        assert storage.users.get_user_by_username("inconnu") is None
        assert storage.users.get_user_detail(999_999) is None


# ---------------------------------------------------------------------------
# Comptages
# ---------------------------------------------------------------------------

class TestCounts:
    def test_get_all_users_counts_searches_and_distinct_listings(self, storage, user, other_user):
        """`listing_count` compte les annonces DISTINCTES de l'utilisateur :
        une annonce partagée par deux de ses recherches ne compte qu'une fois.
        C'est un `COUNT(DISTINCT)` sur une jointure à trois tables."""
        first = insert_search(storage, user["id"], "Première")
        second = insert_search(storage, user["id"], "Seconde")
        shared = make_listing(listing_id="shared")
        storage.listings.save_and_link([shared, make_listing(listing_id="solo")], first["id"])
        storage.listings.save_and_link([shared], second["id"])

        rows = {row["username"]: row for row in storage.users.get_all_users()}

        assert rows["alice"]["search_count"] == 2
        assert rows["alice"]["listing_count"] == 2
        assert rows["bob"]["search_count"] == 0
        assert rows["bob"]["listing_count"] == 0

    def test_get_all_users_is_ordered_by_creation_desc(self, storage, sql):
        for name in ("premier", "deuxieme", "troisieme"):
            insert_user(storage, name)
        # On fige les dates : sinon l'ordre repose sur la résolution de
        # CURRENT_TIMESTAMP entre deux transactions consécutives.
        sql.exec("UPDATE users SET created_at = NOW() - INTERVAL '3 days' WHERE username = 'premier'")
        sql.exec("UPDATE users SET created_at = NOW() - INTERVAL '2 days' WHERE username = 'deuxieme'")
        sql.exec("UPDATE users SET created_at = NOW() - INTERVAL '1 day' WHERE username = 'troisieme'")

        assert [u["username"] for u in storage.users.get_all_users()] == ["troisieme", "deuxieme", "premier"]

    def test_user_detail_aggregates_searches_and_the_ten_latest_listings(self, storage, user):
        search = insert_search(storage, user["id"], "Avec annonces")
        insert_search(storage, user["id"], "Sans annonce")
        storage.listings.save_and_link(
            [make_listing(listing_id=f"l{i:02d}", title=f"Annonce {i}") for i in range(12)], search["id"],
        )

        detail = storage.users.get_user_detail(user["id"])

        assert detail["search_count"] == 2
        assert detail["listing_count"] == 12
        assert {s["label"] for s in detail["searches"]} == {"Avec annonces", "Sans annonce"}
        assert {s["listing_count"] for s in detail["searches"]} == {12, 0}
        assert len(detail["recent_listings"]) == 10
        assert all(item["search_label"] == "Avec annonces" for item in detail["recent_listings"])

    def test_user_stats_counts_only_today_as_new(self, storage, user, sql):
        """`new_today` repose sur `sl.found_at >= CURRENT_DATE`, évalué par le
        serveur : c'est sa notion de « aujourd'hui », pas celle du process
        Python. On vieillit une ligne pour le prouver."""
        search = insert_search(storage, user["id"], "Stats")
        storage.listings.save_and_link(
            [make_listing(listing_id="hier"), make_listing(listing_id="aujourdhui")], search["id"],
        )
        sql.exec(
            "UPDATE search_listings SET found_at = NOW() - INTERVAL '2 days' WHERE listing_id = 'hier'",
        )

        stats = storage.users.get_user_stats(user["id"])

        assert stats == {"searches": 1, "total_listings": 2, "new_today": 1}

    def test_user_stats_of_an_unknown_user_are_all_zero(self, storage):
        assert storage.users.get_user_stats(999_999) == {"searches": 0, "total_listings": 0, "new_today": 0}

    def test_counts_never_leak_between_users(self, storage, user, other_user):
        mine = insert_search(storage, user["id"], "À moi")
        theirs = insert_search(storage, other_user["id"], "À eux")
        storage.listings.save_and_link([make_listing(listing_id="m1")], mine["id"])
        storage.listings.save_and_link(
            [make_listing(listing_id="t1"), make_listing(listing_id="t2")], theirs["id"],
        )

        assert storage.users.get_user_stats(user["id"])["total_listings"] == 1
        assert storage.users.get_user_stats(other_user["id"])["total_listings"] == 2
        assert storage.users.get_user_detail(user["id"])["listing_count"] == 1


# ---------------------------------------------------------------------------
# delete_user : CASCADE sur deux niveaux
# ---------------------------------------------------------------------------

class TestDeleteUser:
    def test_deleting_a_user_cascades_to_searches_and_links(self, storage, user, other_user, sql):
        """Deux `ON DELETE CASCADE` en chaîne : users → searches →
        search_listings. Seul le moteur peut le prouver ; le repo n'émet qu'un
        `DELETE FROM users`.

        Les annonces elles-mêmes survivent (elles sont partagées entre
        utilisateurs) et deviennent orphelines : c'est
        `delete_orphan_listings` qui s'en occupe, pas cette cascade.
        """
        mine = insert_search(storage, user["id"], "À supprimer")
        theirs = insert_search(storage, other_user["id"], "À garder")
        storage.listings.save_and_link([make_listing(listing_id="partagee")], mine["id"])
        storage.listings.save_and_link([make_listing(listing_id="partagee")], theirs["id"])

        assert storage.users.delete_user(user["id"]) is True

        assert storage.users.get_user_by_username("alice") is None
        assert storage.searches.get_search(mine["id"]) is None
        assert sql.one("SELECT COUNT(*) FROM search_listings WHERE search_id = %s", (mine["id"],)) == 0
        # L'autre utilisateur est intact, et l'annonce partagée n'a pas bougé.
        assert storage.searches.get_search(theirs["id"])["label"] == "À garder"
        assert sql.one("SELECT COUNT(*) FROM search_listings WHERE search_id = %s", (theirs["id"],)) == 1
        assert storage.listings.get_listing_detail("partagee") is not None

    def test_deleting_an_unknown_user_returns_false(self, storage):
        assert storage.users.delete_user(999_999) is False


# ---------------------------------------------------------------------------
# get_user_by_id
# ---------------------------------------------------------------------------

class TestGetUserById:
    def test_the_session_resolution_reads_a_real_row(self, storage, user):
        """`get_user_by_id` est ce que les décorateurs de session appellent à
        CHAQUE requête : il doit relire la vraie ligne, `created_at` compris."""
        found = storage.users.get_user_by_id(user["id"])

        assert found == {
            "id": user["id"],
            "username": "alice",
            "created_at": user["created_at"],
        }

    def test_an_unknown_id_returns_none_instead_of_raising(self, storage):
        """Un `user_id` de session qui ne correspond plus à rien (compte
        supprimé) doit mener au `session.clear()` du décorateur, pas à une 500."""
        assert storage.users.get_user_by_id(999_999) is None


# ---------------------------------------------------------------------------
# get_dashboard_data
# ---------------------------------------------------------------------------

class TestDashboardData:
    def test_dashboard_returns_stats_searches_and_ten_recent_listings(self, storage, user):
        search = insert_search(storage, user["id"], "Tableau de bord", criteria=make_criteria())
        storage.listings.save_and_link(
            [make_listing(listing_id=f"d{i:02d}") for i in range(11)], search["id"],
        )

        data = storage.users.get_dashboard_data(user["id"])

        assert data["stats"] == {"searches": 1, "total_listings": 11, "new_today": 11}
        assert [s["label"] for s in data["searches"]] == ["Tableau de bord"]
        assert data["searches"][0]["listing_count"] == 11
        assert data["searches"][0]["criteria"] == make_criteria()
        assert len(data["recent"]) == 10
        assert data["recent"][0]["search_label"] == "Tableau de bord"

    def test_dashboard_does_not_normalize_criteria_unlike_the_search_repository(
        self, storage, user, sql,
    ):
        """# BUG : incohérence entre deux chemins de lecture d'une MÊME ligne.

        `SearchRepository._load_criteria` est décrit comme « le point unique de
        normalisation à la lecture » : les recherches d'avant l'unification
        restent stockées dans le vocabulaire SeLoger et sont converties au
        canonique à chaque lecture. Mais `get_dashboard_data` refait sa propre
        requête et n'appelle que `_parse_json_column` : le tableau de bord voit
        `distributionTypes`/`estateTypes`/`spaceMin`, là où toutes les autres
        vues voient `transaction`/`propertyTypes`/`surfaceMin`.

        Ce test compare les deux chemins sur la même ligne pour figer l'écart.
        """
        legacy = (
            '{"placeIds": ["AD08FR31096"], "city": "Paris", "postalCode": "75013",'
            ' "distributionTypes": ["Rent"], "estateTypes": ["Apartment"],'
            ' "spaceMin": 40, "rooms": ["2", "3"]}'
        )
        search_id = sql.one(
            "INSERT INTO searches (user_id, label, ntfy_topic, source, criteria, sources)"
            " VALUES (%s, 'Ancienne', 'topic', 'seloger', %s, '[\"seloger\"]') RETURNING id",
            (user["id"], legacy),
        )

        via_dashboard = storage.users.get_dashboard_data(user["id"])["searches"][0]["criteria"]
        via_search_repo = storage.searches.get_search(search_id)["criteria"]

        # Chemin tableau de bord : l'ancien vocabulaire ressort tel quel.
        assert via_dashboard["distributionTypes"] == ["Rent"]
        assert via_dashboard["estateTypes"] == ["Apartment"]
        assert via_dashboard["spaceMin"] == 40
        assert via_dashboard["rooms"] == ["2", "3"]       # chaînes, pas entiers
        assert "transaction" not in via_dashboard
        assert "locations" not in via_dashboard

        # Chemin repo : canonique.
        assert via_search_repo == {
            "locations": [{"kind": "city", "city": "Paris", "postalCode": "75013"}],
            "transaction": "rent",
            "propertyTypes": ["apartment"],
            "surfaceMin": 40,
            "rooms": [2, 3],
            "sourceOverrides": {"seloger": {"placeIds": ["AD08FR31096"]}},
        }
        assert via_dashboard != via_search_repo

    def test_dashboard_of_a_user_without_anything_is_empty_but_valid(self, storage, user):
        data = storage.users.get_dashboard_data(user["id"])

        assert data == {
            "stats": {"searches": 0, "total_listings": 0, "new_today": 0},
            "searches": [],
            "recent": [],
        }
