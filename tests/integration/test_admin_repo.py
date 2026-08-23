"""AdminRepository contre un vrai Postgres.

Repo le moins couvert du projet (22 %), et celui dont la méthode centrale ne
*peut pas* être testée autrement : `execute_query` expose une console SQL à
l'admin et affirme qu'elle est en lecture seule. Cette garantie est entièrement
déléguée à Postgres (`SET TRANSACTION READ ONLY`) — un test à base de doubles ne
prouverait que l'envoi de la chaîne `"SET TRANSACTION READ ONLY"`, jamais que
Postgres refuse effectivement l'écriture. `TestExecuteQueryIsReallyReadOnly` fait
donc le seul test qui vaille : tenter une vraie écriture et vérifier que la ligne
survit.

Le reste du repo est du SQL d'introspection (`pg_stat_user_tables`, `pg_indexes`,
`pg_constraint`, `pg_stat_activity`), d'agrégation (huit sous-requêtes en une
passe, un `AVG` qui remonte en `Decimal`) et de DDL (`TRUNCATE`) : aucune de ces
requêtes ne s'exécute ailleurs que sur un vrai moteur.

Trois défauts confirmés sont figés ici, tous signalés par `# BUG :` —
`get_admin_logs`/`count_admin_logs` sur une date invalide, `get_table_details`
sur une table inexistante, et une entrée d'allowlist qui désigne une table qui
n'existe pas.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import psycopg2
import pytest

from repositories.admin_repo import AdminRepository
from tests.helpers.factories import make_listing
from tests.integration.conftest import insert_search

# ---------------------------------------------------------------------------
# execute_query — la console SQL de l'admin
# ---------------------------------------------------------------------------

WRITE_ATTEMPTS = [
    pytest.param("DELETE FROM users", id="delete"),
    pytest.param("DELETE FROM users WHERE username = 'alice'", id="delete-cible"),
    pytest.param("UPDATE users SET username = 'pirate'", id="update"),
    pytest.param("INSERT INTO users (username) VALUES ('pirate')", id="insert"),
    pytest.param("TRUNCATE users CASCADE", id="truncate"),
    pytest.param("DROP TABLE search_listings", id="drop-table"),
    pytest.param("CREATE TABLE porte_derobee (a int)", id="create-table"),
    pytest.param("ALTER TABLE users ADD COLUMN porte_derobee text", id="alter-table"),
    pytest.param("CREATE INDEX idx_pirate ON users(username)", id="create-index"),
    pytest.param("ALTER TABLE users RENAME TO anciens_users", id="rename-table"),
    pytest.param("CREATE OR REPLACE VIEW v_pirate AS SELECT 1", id="create-view"),
    # L'écriture cachée dans une CTE : la requête *ressemble* à un SELECT.
    pytest.param(
        "WITH supprimes AS (DELETE FROM users RETURNING *) SELECT COUNT(*) FROM supprimes",
        id="cte-avec-delete",
    ),
    pytest.param("SELECT * INTO copie_users FROM users", id="select-into"),
    # psycopg2 laisse passer plusieurs instructions dans un seul execute() :
    # l'écriture est déguisée derrière un SELECT anodin.
    pytest.param("SELECT 1; DELETE FROM users", id="multi-instructions"),
]


class TestExecuteQueryIsReallyReadOnly:
    def test_a_select_returns_dict_rows_a_row_count_and_no_error(self, storage, user):
        rows, row_count, error = storage.admin.execute_query(
            "SELECT username FROM users ORDER BY username",
        )

        assert error is None
        assert row_count == 1
        assert rows == [{"username": "alice"}]
        assert isinstance(rows[0], dict), "RealDictRow doit être converti en dict pur"

    @pytest.mark.parametrize("sql_text", WRITE_ATTEMPTS)
    def test_postgres_refuses_the_write_and_the_data_survives(self, storage, user, sql, sql_text):
        """🔒 LE test de ce fichier. Chaque instruction ci-dessus est refusée par
        le moteur lui-même, pas par un filtre de mots-clés côté Python (il n'y en
        a aucun) : c'est ce qui rend la garantie robuste aux contournements —
        commentaires, casse, écriture cachée dans une CTE, ou seconde instruction
        collée derrière un `SELECT` anodin.

        On vérifie les deux moitiés : l'erreur est bien remontée à l'appelant, ET
        la base est intacte après coup.
        """
        rows, row_count, error = storage.admin.execute_query(sql_text)

        assert error is not None, f"écriture NON refusée : {sql_text!r}"
        assert "read-only" in error.lower()
        assert rows == []
        assert row_count == 0

        # La ligne visée est toujours là, la table existe encore et porte les
        # mêmes colonnes.
        assert storage.users.get_user_by_username("alice")["id"] == user["id"]
        assert sql.one("SELECT COUNT(*) FROM users") == 1
        assert sql.one(
            "SELECT COUNT(*) FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name = 'users'",
        ) == 1

    def test_nothing_leaks_between_two_calls_of_the_transaction(self, storage, user, sql):
        """La transaction est rollbackée sur les deux chemins (succès et
        exception) : une écriture refusée ne laisse aucune trace, et un `SELECT`
        réussi ne valide rien non plus."""
        storage.admin.execute_query("DELETE FROM users")
        storage.admin.execute_query("SELECT COUNT(*) FROM users")
        storage.admin.execute_query("INSERT INTO users (username) VALUES ('x')")

        assert sql.all("SELECT username FROM users") == [("alice",)]

    def test_a_broken_query_never_raises_and_returns_the_message(self, storage):
        """L'appelant (routes/admin.py) affiche `error` dans la page : la méthode
        ne doit jamais lever, sinon la console SQL rendrait un 500 au lieu du
        message d'erreur SQL."""
        rows, row_count, error = storage.admin.execute_query("SELEKT * FROM users")

        assert (rows, row_count) == ([], 0)
        assert isinstance(error, str) and error

    @pytest.mark.parametrize(
        "sql_text",
        [
            pytest.param("SELECT * FROM table_qui_nexiste_pas", id="table-inconnue"),
            pytest.param("SELECT colonne_inconnue FROM users", id="colonne-inconnue"),
            pytest.param("SELECT 1/0", id="division-par-zero"),
            pytest.param("", id="chaine-vide"),
            pytest.param("SELECT 'abc'::int", id="cast-impossible"),
            pytest.param("SELECT * FROM users WHERE", id="where-incomplet"),
        ],
    )
    def test_every_kind_of_failure_comes_back_as_the_third_element(self, storage, sql_text):
        rows, row_count, error = storage.admin.execute_query(sql_text)

        assert rows == []
        assert row_count == 0
        assert error is not None

    def test_a_result_set_with_zero_columns_is_reported_as_zero_rows(self, storage):
        """# Piège : `SELECT` seul est du SQL *valide* en Postgres — il rend UNE
        ligne de ZÉRO colonne. `cur.description` est alors un tuple VIDE, donc
        falsy, donc la méthode prend la branche « pas de résultat » : elle rend
        `[]` alors que `rowcount` vaut 1.

        La page affiche donc « 1 ligne » avec un tableau vide. Inoffensif, mais
        c'est la preuve que le test se fait sur la vérité de `cur.description` et
        non sur `is None` — et cette nuance mordrait sur toute requête à zéro
        colonne. Comportement ACTUEL figé.
        """
        rows, row_count, error = storage.admin.execute_query("SELECT")

        assert (rows, row_count, error) == ([], 1, None)

    def test_a_failed_query_does_not_poison_the_connection(self, storage, user):
        """🔒 Le `conn.rollback()` du `except` est ce qui rend la console
        réutilisable : sans lui, la connexion repartirait au pool en transaction
        avortée et TOUTE requête suivante — y compris celles des autres pages —
        répondrait « current transaction is aborted »."""
        storage.admin.execute_query("SELECT * FROM table_qui_nexiste_pas")

        rows, _, error = storage.admin.execute_query("SELECT COUNT(*) AS n FROM users")
        assert error is None
        assert rows == [{"n": 1}]

        # Et le reste de l'application aussi, sur le même pool.
        assert storage.users.get_user_by_username("alice")["id"] == user["id"]
        assert storage.admin.get_admin_stats()["users"] == 1

    def test_ten_failures_in_a_row_leave_the_pool_healthy(self, storage, user):
        for _ in range(10):
            storage.admin.execute_query("DELETE FROM users")
            storage.admin.execute_query("SELEKT")

        assert storage.users.get_all_users()[0]["username"] == "alice"

    @pytest.mark.parametrize(
        "sql_text",
        [
            pytest.param("SET TIME ZONE 'UTC'", id="set"),
            pytest.param("DO $$ BEGIN END $$", id="bloc-do-vide"),
        ],
    )
    def test_a_statement_without_a_result_set_returns_an_empty_list(self, storage, sql_text):
        """`cur.description` est `None` quand l'instruction ne produit pas de
        lignes : la méthode doit rendre `([], rowcount, None)` au lieu d'appeler
        `fetchall()`, qui lèverait « no results to fetch ».

        `rowcount` vaut -1 (psycopg2 : « inconnu »), pas 0 : la page affiche donc
        « -1 ligne » sur ce chemin. Comportement ACTUEL figé.
        """
        rows, row_count, error = storage.admin.execute_query(sql_text)

        assert (rows, row_count, error) == ([], -1, None)

    def test_the_row_count_matches_the_number_of_rows_returned(self, storage, user, other_user):
        rows, row_count, error = storage.admin.execute_query("SELECT id FROM users")

        assert error is None
        assert row_count == len(rows) == 2

    def test_a_select_returning_no_row_is_not_an_error(self, storage):
        rows, row_count, error = storage.admin.execute_query("SELECT 1 WHERE FALSE")

        assert (rows, row_count, error) == ([], 0, None)


class TestWhatReadOnlyDoesNotProtect:
    """Ce que `READ ONLY` ne couvre PAS, documenté explicitement.

    La docstring de `execute_query` dit « cannot be used to mutate data ». C'est
    exact, et c'est tout : la console reste un accès brut à la base, en lecture
    totale et sans budget de ressources. Ces tests figent le périmètre réel pour
    que personne ne prenne `READ ONLY` pour un bac à sable.
    """

    def test_another_users_searches_and_listings_are_readable(self, storage, other_search):
        """Aucune notion de propriétaire : la console ignore le `user_id` que
        toutes les routes applicatives filtrent."""
        rows, _, error = storage.admin.execute_query(
            "SELECT label, ntfy_topic FROM searches",
        )

        assert error is None
        assert rows == [{"label": "Bordeaux", "ntfy_topic": "topic-bordeaux"}]

    def test_a_server_side_sleep_is_allowed(self, storage):
        """`pg_sleep` n'écrit rien, donc `READ ONLY` l'autorise : la console
        permet d'occuper une connexion du pool (qui n'en compte que 20) aussi
        longtemps que le `statement_timeout` de 30 s le tolère. Une poignée de
        requêtes de ce genre suffit à saturer le pool et à rendre l'application
        indisponible."""
        rows, _, error = storage.admin.execute_query("SELECT pg_sleep(0.05) AS dodo")

        assert error is None
        assert len(rows) == 1

    def test_a_cartesian_product_is_allowed_and_only_bounded_by_the_statement_timeout(self, storage):
        """Aucun `LIMIT` n'est imposé : la seule borne est le
        `statement_timeout = 30 s` posé par `_get_conn`. Une requête lourde
        remonte donc en erreur de timeout, pas en refus — et elle a bien
        consommé 30 secondes de CPU serveur."""
        rows, _, error = storage.admin.execute_query(
            "SELECT COUNT(*) AS n FROM generate_series(1, 200000)",
        )

        assert error is None
        assert rows[0]["n"] == 200_000

    def test_the_session_settings_of_the_pooled_connection_are_reachable(self, storage):
        """La console tourne sur une connexion du pool applicatif, pas sur une
        connexion dédiée à privilèges réduits : tout ce que voit l'application
        est visible ici."""
        rows, _, error = storage.admin.execute_query("SHOW statement_timeout")

        assert error is None
        assert rows == [{"statement_timeout": "30s"}]


# ---------------------------------------------------------------------------
# Statistiques
# ---------------------------------------------------------------------------

class TestAdminStats:
    def test_the_four_counters_are_computed_in_a_single_query(self, storage, user, other_user, sql):
        first = insert_search(storage, user["id"], "Première")
        insert_search(storage, other_user["id"], "Chez bob")
        storage.listings.save_and_link(
            [make_listing(listing_id="hier"), make_listing(listing_id="aujourdhui")], first["id"],
        )
        sql.exec("UPDATE search_listings SET found_at = NOW() - INTERVAL '2 days' WHERE listing_id = 'hier'")

        assert storage.admin.get_admin_stats() == {
            "users": 2,
            "searches": 2,
            "total_listings": 2,
            "new_today": 1,
        }

    def test_new_today_uses_the_servers_notion_of_today(self, storage, search, sql):
        """`found_at >= CURRENT_DATE` est évalué par Postgres : c'est minuit sur
        le fuseau de la SESSION, pas celui du process Python. Vieillir la ligne
        d'une seconde avant minuit la ferait basculer — d'où le vieillissement en
        SQL, seule façon d'exercer la comparaison réelle."""
        storage.listings.save_and_link([make_listing(listing_id="a")], search["id"])
        assert storage.admin.get_admin_stats()["new_today"] == 1

        sql.exec("UPDATE search_listings SET found_at = CURRENT_DATE - INTERVAL '1 second'")

        assert storage.admin.get_admin_stats()["new_today"] == 0

    def test_an_empty_database_gives_zeros_not_nulls(self, storage):
        """Les quatre sous-requêtes sont des `COUNT(*)`, qui rendent 0 et non
        NULL sur un ensemble vide : le tableau de bord affiche « 0 » et ne casse
        pas sur un `None`."""
        assert storage.admin.get_admin_stats() == {
            "users": 0, "searches": 0, "total_listings": 0, "new_today": 0,
        }


class TestEnhancedAdminStats:
    def test_every_aggregate_is_computed_over_a_realistic_dataset(self, storage, user, other_user, sql):
        busy = insert_search(storage, user["id"], "Chargée")
        quiet = insert_search(storage, user["id"], "Calme", source="laforet")
        theirs = insert_search(storage, other_user["id"], "Chez bob", source="laforet")
        storage.listings.save_and_link(
            [make_listing(listing_id=f"l{i}") for i in range(4)], busy["id"],
        )
        storage.listings.save_and_link([make_listing(listing_id="l0")], quiet["id"])
        storage.listings.save_and_link([make_listing(listing_id="chez-bob")], theirs["id"])
        # Une annonce devenue orpheline : plus aucun lien.
        sql.exec("INSERT INTO listings (listing_id, url) VALUES ('orpheline', 'http://x')")
        # Un utilisateur sans aucune recherche.
        storage.users.create_user("carol")

        stats = storage.admin.get_enhanced_admin_stats()

        assert stats["users"] == 3
        assert stats["searches"] == 3
        assert stats["total_listings"] == 6      # 4 + 1 chez bob + 1 orpheline
        assert stats["search_listings"] == 6     # 4 + 1 (l0 relié 2 fois) + 1
        assert stats["new_today"] == 6
        assert stats["orphan_listings"] == 1
        assert stats["users_without_searches"] == 1
        assert stats["avg_listings_per_search"] == 2  # (4 + 1 + 1) / 3
        assert stats["top_users"] == [
            {"username": "alice", "listing_count": 4},
            {"username": "bob", "listing_count": 1},
        ]
        assert [s["label"] for s in stats["top_searches"]] == ["Chargée", "Calme", "Chez bob"]
        assert stats["top_searches"][0] == {
            "id": busy["id"], "label": "Chargée", "username": "alice", "listing_count": 4,
        }
        assert stats["sources_breakdown"] == [
            {"source": "laforet", "cnt": 2},
            {"source": "seloger", "cnt": 1},
        ]

    def test_the_average_comes_back_as_a_decimal_not_a_float(self, storage, user):
        """`AVG` sur un `COUNT(*)` (bigint) rend un `NUMERIC`, que psycopg2 adapte
        en `Decimal` — et `round(Decimal, 1)` rend encore un `Decimal`. La valeur
        traverse donc les templates et la sérialisation JSON en `Decimal`, pas en
        `float`. Rien ne casse aujourd'hui (Flask sait sérialiser un `Decimal` en
        chaîne), mais un `jsonify` de cette clé produirait `"1.5"` et non `1.5` :
        comportement ACTUEL épinglé.
        """
        first = insert_search(storage, user["id"], "Deux")
        second = insert_search(storage, user["id"], "Une")
        storage.listings.save_and_link(
            [make_listing(listing_id="a"), make_listing(listing_id="b")], first["id"],
        )
        storage.listings.save_and_link([make_listing(listing_id="c")], second["id"])

        avg = storage.admin.get_enhanced_admin_stats()["avg_listings_per_search"]

        assert isinstance(avg, Decimal)
        assert avg == Decimal("1.5")

    def test_the_average_is_zero_and_not_none_without_any_link(self, storage, user):
        """`AVG` sur un ensemble vide rend NULL : sans le `COALESCE(..., 0)`, le
        `round(None, 1)` lèverait un `TypeError` et le tableau de bord admin
        rendrait un 500 sur une base neuve."""
        insert_search(storage, user["id"], "Sans annonce")

        stats = storage.admin.get_enhanced_admin_stats()

        assert stats["avg_listings_per_search"] == 0
        assert stats["search_listings"] == 0

    def test_an_empty_database_gives_a_complete_and_usable_payload(self, storage):
        stats = storage.admin.get_enhanced_admin_stats()

        assert stats == {
            "users": 0, "searches": 0, "total_listings": 0, "search_listings": 0,
            "new_today": 0, "orphan_listings": 0, "avg_listings_per_search": 0,
            "top_users": [], "top_searches": [], "activity_7d": [], "sources_breakdown": [],
            "users_without_searches": 0,
        }

    def test_the_seven_day_activity_is_grouped_by_day_and_ordered(self, storage, search, sql):
        """`DATE(found_at)` rend un `datetime.date`, `COUNT(DISTINCT listing_id)`
        évite de compter deux fois une annonce partagée, et le `>= CURRENT_DATE -
        INTERVAL '7 days'` coupe l'historique. Trois comportements du moteur en
        une requête."""
        storage.listings.save_and_link(
            [make_listing(listing_id=f"l{i}") for i in range(4)], search["id"],
        )
        sql.exec("UPDATE search_listings SET found_at = NOW() - INTERVAL '2 days' WHERE listing_id = 'l1'")
        sql.exec("UPDATE search_listings SET found_at = NOW() - INTERVAL '2 days' WHERE listing_id = 'l2'")
        # Hors fenêtre : ne doit pas apparaître du tout.
        sql.exec("UPDATE search_listings SET found_at = NOW() - INTERVAL '30 days' WHERE listing_id = 'l3'")

        activity = storage.admin.get_enhanced_admin_stats()["activity_7d"]

        assert [row["count"] for row in activity] == [2, 1]
        assert all(isinstance(row["day"], date) for row in activity)
        assert activity[0]["day"] < activity[1]["day"]
        assert sum(row["count"] for row in activity) == 3

    def test_the_top_lists_are_capped_at_five(self, storage, user):
        for i in range(7):
            s = insert_search(storage, user["id"], f"Recherche {i}")
            storage.listings.save_and_link([make_listing(listing_id=f"l{i}")], s["id"])

        stats = storage.admin.get_enhanced_admin_stats()

        assert len(stats["top_searches"]) == 5
        assert len(stats["top_users"]) == 1

    def test_a_search_without_listings_is_absent_from_the_top_list(self, storage, user):
        """Les deux `top_*` utilisent des `JOIN` et non des `LEFT JOIN` : une
        recherche vide n'apparaît pas avec 0, elle disparaît. Voulu (c'est un
        classement), mais ça veut dire que la somme des `listing_count` du top
        n'est pas le total affiché juste au-dessus."""
        insert_search(storage, user["id"], "Vide")

        assert storage.admin.get_enhanced_admin_stats()["top_searches"] == []

    def test_a_listing_shared_by_two_searches_counts_once_per_user(self, storage, user):
        """`COUNT(DISTINCT sl.listing_id)` côté utilisateur contre `COUNT(
        sl.listing_id)` côté recherche : le même jeu de données donne 1 pour
        l'utilisateur et 1+1 pour ses deux recherches. Asymétrie volontaire,
        épinglée ici parce qu'elle rend les deux tableaux non additionnables."""
        first = insert_search(storage, user["id"], "Première")
        second = insert_search(storage, user["id"], "Seconde")
        storage.listings.save_and_link([make_listing(listing_id="partagee")], first["id"])
        storage.listings.save_and_link([make_listing(listing_id="partagee")], second["id"])

        stats = storage.admin.get_enhanced_admin_stats()

        assert stats["top_users"] == [{"username": "alice", "listing_count": 1}]
        assert [s["listing_count"] for s in stats["top_searches"]] == [1, 1]


# ---------------------------------------------------------------------------
# Journal d'administration
# ---------------------------------------------------------------------------

class TestLogAdminAction:
    def test_an_action_is_written_with_its_details_and_author(self, storage, sql):
        storage.admin.log_admin_action("delete_user", "user_id=42", "admin")

        assert sql.row("SELECT action, details, performed_by FROM admin_logs") == (
            "delete_user", "user_id=42", "admin",
        )

    def test_the_optional_arguments_default_to_empty_strings(self, storage, sql):
        storage.admin.log_admin_action("purge")

        assert sql.row("SELECT action, details, performed_by FROM admin_logs") == ("purge", "", "")

    def test_the_timestamp_comes_from_the_server(self, storage):
        storage.admin.log_admin_action("action")

        (log,) = storage.admin.get_admin_logs()
        assert isinstance(log["created_at"], datetime)
        assert log["id"] == 1, "RESTART IDENTITY : la séquence repart de 1 à chaque test"

    def test_it_never_raises_when_the_table_is_missing(self, storage, blank_db):
        """🔒 `log_admin_action` avale toutes ses exceptions : c'est délibéré,
        car il est appelé *après* l'action qu'il journalise (suppression d'un
        utilisateur, purge, TRUNCATE). Une exception ici transformerait une
        opération réussie en erreur 500, et l'admin la relancerait.

        Le seul moyen honnête de le prouver est une base sans la table : sur la
        base de la session, il n'y a rien à casser sans casser aussi le reste des
        tests.
        """
        repo = AdminRepository(blank_db())

        assert repo.log_admin_action("delete_user", "user_id=42", "admin") is None

    def test_a_failed_log_leaves_the_connection_usable(self, storage, blank_db):
        """Le `conn.rollback()` de l'`except` : sans lui, la connexion du pool de
        cette base repartirait en transaction avortée."""
        repo = AdminRepository(blank_db())
        repo.log_admin_action("action")

        rows, _, error = repo.execute_query("SELECT 1 AS n")

        assert error is None
        assert rows == [{"n": 1}]

    @pytest.mark.parametrize(
        "action",
        [
            pytest.param("action normale", id="simple"),
            pytest.param("'; DROP TABLE admin_logs; --", id="charge-hostile"),
            pytest.param("accentué éàü 🏠", id="unicode"),
            pytest.param("", id="chaine-vide"),
        ],
    )
    def test_the_action_is_stored_verbatim(self, storage, action):
        storage.admin.log_admin_action(action, "d", "p")

        assert storage.admin.get_admin_logs()[0]["action"] == action


LOG_FILTER_CASES = [
    pytest.param({}, {"a1", "a2", "b1"}, id="aucun-filtre"),
    pytest.param({"action_filter": "purge"}, {"a1", "a2"}, id="action-exacte"),
    pytest.param({"action_filter": "PURGE"}, set(), id="action-sensible-a-la-casse"),
    pytest.param({"action_filter": "purg"}, set(), id="action-est-une-egalite-pas-un-like"),
    pytest.param({"action_filter": "inconnue"}, set(), id="action-sans-resultat"),
    pytest.param({"date_from": "2026-07-10"}, {"a2", "b1"}, id="depuis"),
    pytest.param({"date_to": "2026-07-10"}, {"a1"}, id="jusqua"),
    pytest.param({"date_from": "2026-07-05", "date_to": "2026-07-20"}, {"a2"}, id="fourchette"),
    pytest.param({"action_filter": "purge", "date_from": "2026-07-10"}, {"a2"}, id="action-et-date"),
    pytest.param({"date_from": "2026-08-01"}, set(), id="fourchette-vide"),
]


class TestAdminLogs:
    @pytest.fixture
    def logs(self, storage, sql):
        """Trois entrées à des dates figées. Les dates sont posées en SQL brut :
        `created_at` a un `DEFAULT CURRENT_TIMESTAMP` et `log_admin_action` ne
        permet pas de la choisir."""
        storage.admin.log_admin_action("purge", "a1")
        storage.admin.log_admin_action("purge", "a2")
        storage.admin.log_admin_action("truncate", "b1")
        sql.exec("UPDATE admin_logs SET created_at = '2026-07-01 10:00:00' WHERE details = 'a1'")
        sql.exec("UPDATE admin_logs SET created_at = '2026-07-15 10:00:00' WHERE details = 'a2'")
        sql.exec("UPDATE admin_logs SET created_at = '2026-07-20 10:00:00' WHERE details = 'b1'")

    @pytest.mark.parametrize(("filters", "expected"), LOG_FILTER_CASES)
    def test_the_count_matches_the_rows_for_every_filter_combination(self, storage, logs, filters, expected):
        """Même invariant de pagination qu'ailleurs : `get_admin_logs` et
        `count_admin_logs` construisent leur `WHERE` chacun de leur côté, à
        l'identique. On compare les deux résultats plutôt que les deux chaînes.

        Les dates arrivent en `str` depuis le formulaire et sont comparées à une
        colonne `TIMESTAMP` : c'est Postgres qui les convertit, avec une heure
        implicite à minuit — d'où `date_to='2026-07-10'` qui exclut le 10 juillet
        à 10 h.
        """
        rows = storage.admin.get_admin_logs(limit=1000, **filters)
        count = storage.admin.count_admin_logs(**filters)

        assert {row["details"] for row in rows} == expected
        assert count == len(rows) == len(expected)

    def test_logs_are_ordered_most_recent_first(self, storage, logs):
        assert [row["details"] for row in storage.admin.get_admin_logs()] == ["b1", "a2", "a1"]

    def test_pagination_walks_every_row_exactly_once(self, storage, logs):
        seen = []
        for offset in (0, 2):
            seen.extend(row["details"] for row in storage.admin.get_admin_logs(limit=2, offset=offset))

        assert seen == ["b1", "a2", "a1"]

    def test_an_offset_past_the_end_is_empty(self, storage, logs):
        assert storage.admin.get_admin_logs(offset=100) == []
        assert storage.admin.count_admin_logs() == 3

    def test_an_empty_journal_yields_no_rows_and_a_zero_count(self, storage):
        assert storage.admin.get_admin_logs() == []
        assert storage.admin.count_admin_logs() == 0

    @pytest.mark.parametrize(
        "bad_date",
        [
            pytest.param("pas-une-date", id="texte"),
            pytest.param("2026-13-45", id="mois-et-jour-hors-bornes"),
            pytest.param("31/12/2026", id="format-francais"),
            pytest.param("2026-02-30", id="jour-inexistant"),
        ],
    )
    @pytest.mark.parametrize("field", ["date_from", "date_to"])
    def test_an_invalid_date_raises_a_data_error_that_nobody_catches(self, storage, logs, bad_date, field):
        """# BUG : `date_from` et `date_to` arrivent brutes de `request.args` et
        sont comparées à une colonne `TIMESTAMP` sans aucune validation. Postgres
        refuse la conversion et lève une `DataError`, qui n'est rattrapée ni dans
        le repo ni dans la route : la page de journal rend un 500.

        Un simple `?date_from=hier` dans l'URL suffit — et le format français
        `31/12/2026`, que n'importe quel utilisateur peut taper dans un champ
        texte, en fait partie. Comportement ACTUEL figé dans les deux sens.
        """
        with pytest.raises(psycopg2.DataError):
            storage.admin.get_admin_logs(**{field: bad_date})

        with pytest.raises(psycopg2.DataError):
            storage.admin.count_admin_logs(**{field: bad_date})

    def test_the_connection_is_reusable_after_that_data_error(self, storage, logs):
        """Le repo ne rollback pas (il n'a pas d'`except`), mais `_release_conn`
        rattrape la transaction avortée au retour au pool : le 500 n'empoisonne
        pas les requêtes suivantes."""
        with pytest.raises(psycopg2.DataError):
            storage.admin.get_admin_logs(date_from="pas-une-date")

        assert storage.admin.count_admin_logs() == 3

    def test_a_valid_timestamp_with_a_time_is_accepted(self, storage, logs):
        """Le corollaire du bug ci-dessus : tout ce que Postgres sait convertir
        passe, y compris un timestamp complet — la validation est déléguée au
        moteur, pas absente."""
        assert storage.admin.count_admin_logs(date_from="2026-07-15 09:59:59") == 2

    @pytest.mark.parametrize("payload", ["'; DROP TABLE admin_logs; --", "' OR 1=1 --"])
    def test_a_hostile_action_filter_is_a_parameter_not_a_fragment(self, storage, logs, payload):
        assert storage.admin.count_admin_logs(action_filter=payload) == 0
        assert storage.admin.count_admin_logs() == 3


class TestPurgeOldLogs:
    def test_make_interval_runs_and_spares_fresh_logs(self, storage):
        """Régression : `INTERVAL '%s days'` n'était pas du SQL valide.
        `make_interval(days => %s)` prend le nombre en paramètre — ça ne se
        vérifie qu'en l'exécutant."""
        storage.admin.log_admin_action("action")

        assert storage.admin.purge_old_logs(days=30) == 0

        assert storage.admin.count_admin_logs() == 1

    @pytest.mark.parametrize(("days", "expected_deleted"), [(30, 1), (60, 1), (61, 0), (365, 0)])
    def test_the_cutoff_is_a_real_parameter(self, storage, sql, days, expected_deleted):
        storage.admin.log_admin_action("vieille")
        sql.exec("UPDATE admin_logs SET created_at = NOW() - INTERVAL '60 days 1 hour'")

        assert storage.admin.purge_old_logs(days=days) == expected_deleted

    def test_only_the_old_logs_go(self, storage, sql):
        storage.admin.log_admin_action("vieille")
        storage.admin.log_admin_action("recente")
        sql.exec("UPDATE admin_logs SET created_at = NOW() - INTERVAL '60 days' WHERE action = 'vieille'")

        assert storage.admin.purge_old_logs(days=30) == 1

        assert [row["action"] for row in storage.admin.get_admin_logs()] == ["recente"]

    def test_purging_an_empty_journal_returns_zero(self, storage):
        assert storage.admin.purge_old_logs() == 0


# ---------------------------------------------------------------------------
# Introspection du schéma
# ---------------------------------------------------------------------------

class TestDbStats:
    def test_the_payload_describes_the_real_schema(self, storage, search):
        """Requêtes sur `pg_stat_user_tables` et `pg_indexes` : elles n'existent
        pas hors d'un vrai serveur, et leurs noms de colonnes changent d'une
        version majeure à l'autre. C'est un test de non-régression sur le
        contrat de ces vues autant que sur le repo."""
        storage.listings.save_and_link([make_listing(listing_id="a")], search["id"])

        stats = storage.admin.get_db_stats()

        assert isinstance(stats["db_size"], str) and stats["db_size"]
        table_names = {row["table_name"] for row in stats["tables"]}
        assert {"users", "searches", "listings", "search_listings", "admin_logs"} <= table_names
        assert all(
            set(row) == {"table_name", "row_count", "total_size", "data_size"}
            for row in stats["tables"]
        )

    def test_the_indexes_of_the_public_schema_are_listed_with_their_size(self, storage):
        stats = storage.admin.get_db_stats()

        index_names = {row["indexname"] for row in stats["indexes"]}
        assert "idx_search_listings_search" in index_names
        assert "idx_listings_first_seen" in index_names
        assert all(row["schemaname"] == "public" for row in stats["indexes"])
        assert all(isinstance(row["index_size"], str) for row in stats["indexes"])

    def test_the_indexes_are_ordered_by_table_then_name(self, storage):
        stats = storage.admin.get_db_stats()
        keys = [(row["tablename"], row["indexname"]) for row in stats["indexes"]]

        assert keys == sorted(keys)


class TestTableDetails:
    def test_the_details_of_a_real_table_are_complete(self, storage):
        details = storage.admin.get_table_details("users")

        columns = {row["column_name"]: row for row in details["columns"]}
        assert list(columns) == ["id", "username", "created_at"], "ordinal_position"
        assert columns["username"]["data_type"] == "text"
        assert columns["username"]["is_nullable"] == "NO"
        assert columns["created_at"]["column_default"] == "CURRENT_TIMESTAMP"
        assert isinstance(details["total_size"], str) and details["total_size"]

    def test_the_constraints_carry_their_postgres_type_code_and_definition(self, storage):
        """`contype` est un `char` d'un octet ('p' = primary, 'u' = unique,
        'f' = foreign) : psycopg2 le rend en chaîne d'un caractère, et le
        template l'affiche tel quel. Épinglé parce que c'est le genre de détail
        qu'aucun double ne reproduit."""
        details = storage.admin.get_table_details("search_listings")

        types = {row["constraint_type"] for row in details["constraints"]}
        assert "p" in types and "f" in types
        assert any("PRIMARY KEY (search_id, listing_id)" in row["definition"] for row in details["constraints"])
        assert sum(1 for row in details["constraints"] if row["constraint_type"] == "f") == 2

    def test_the_indexes_of_the_table_are_listed_with_their_definition(self, storage):
        details = storage.admin.get_table_details("searches")

        assert {row["indexname"] for row in details["indexes"]} >= {"idx_searches_user", "idx_searches_source"}
        assert all(row["indexdef"].startswith("CREATE") for row in details["indexes"])

    @pytest.mark.parametrize(
        ("table_name", "expected"),
        [
            pytest.param("table_qui_nexiste_pas", psycopg2.errors.UndefinedTable, id="table-inconnue"),
            pytest.param("scrape_logs", psycopg2.errors.UndefinedTable, id="table-de-l-allowlist-qui-nexiste-pas"),
            pytest.param("", psycopg2.errors.InvalidName, id="chaine-vide"),
            pytest.param("'; DROP TABLE users; --", psycopg2.errors.InvalidName, id="charge-hostile"),
        ],
    )
    def test_an_unknown_table_name_raises_instead_of_returning_empty(self, storage, table_name, expected):
        """# BUG : `table_name` vient du chemin d'URL de l'admin. Les deux
        premières requêtes rendent simplement des listes vides pour une table
        inconnue, mais la troisième la passe à `%s::regclass` — qui lève.
        Aucun `except` nulle part : la page rend un 500 au lieu d'un « table
        inconnue », et le type d'exception dépend de la charge (`UndefinedTable`
        pour un nom bien formé mais absent, `InvalidName` pour un nom qui n'est
        même pas un identifiant valide).

        Pas une injection : le nom est bien un paramètre lié, et la charge
        hostile est rejetée par l'analyseur d'identifiants de `regclass` au lieu
        d'être exécutée. Juste un 500 déclenchable par une URL forgée.
        Comportement ACTUEL figé.
        """
        with pytest.raises(expected):
            storage.admin.get_table_details(table_name)

        # Les deux premières requêtes, elles, ne lèvent pas : c'est bien
        # `::regclass` le fautif, et non l'absence de la table en soi.
        assert storage.admin.execute_query(
            "SELECT COUNT(*) AS n FROM information_schema.columns WHERE table_name = 'introuvable'",
        ) == ([{"n": 0}], 1, None)

    def test_an_uppercase_table_name_produces_a_half_filled_page(self, storage):
        """# BUG (incohérence de casse) : les deux premières requêtes comparent
        `table_name` en TEXTE à `information_schema` / `pg_indexes`, qui stockent
        les identifiants en minuscules — « USERS » n'y correspond à rien. La
        troisième, elle, passe par `%s::regclass`, qui applique les règles
        d'identifiant de Postgres et replie donc « USERS » sur `users`.

        Résultat : `/admin/table/USERS` ne lève pas, mais rend une page où les
        colonnes et les index sont vides tandis que les contraintes et la taille
        sont celles de la vraie table. Un affichage muet et faux, plus trompeur
        que le 500 du test précédent.
        """
        details = storage.admin.get_table_details("USERS")

        assert details["columns"] == []
        assert details["indexes"] == []
        assert any("PRIMARY KEY (id)" in row["definition"] for row in details["constraints"])
        assert details["total_size"] == storage.admin.get_table_details("users")["total_size"]

    def test_the_connection_is_reusable_after_that_failure(self, storage):
        with pytest.raises(psycopg2.errors.UndefinedTable):
            storage.admin.get_table_details("table_qui_nexiste_pas")

        assert storage.admin.get_table_details("users")["columns"]


class TestActiveConnections:
    def test_other_connections_to_this_database_are_listed_but_not_our_own(self, storage, pg_url):
        """`WHERE datname = current_database() AND pid != pg_backend_pid()` : la
        vue `pg_stat_activity` est globale au serveur, ces deux filtres sont ce
        qui rend la page lisible. On ouvre une connexion identifiable pour
        vérifier qu'elle apparaît, et on vérifie que celle qui exécute la requête
        s'exclut elle-même.
        """
        observer = psycopg2.connect(pg_url, connect_timeout=10, application_name="temoin_test")
        try:
            observer_pid = observer.get_backend_pid()
            rows = storage.admin.get_active_connections()
        finally:
            observer.close()

        by_pid = {row["pid"]: row for row in rows}
        assert observer_pid in by_pid
        assert by_pid[observer_pid]["application_name"] == "temoin_test"
        assert set(rows[0]) == {
            "pid", "usename", "application_name", "client_addr",
            "backend_start", "state", "query", "query_start",
        }

    def test_the_rows_are_ordered_by_backend_start(self, storage, pg_url):
        observer = psycopg2.connect(pg_url, connect_timeout=10)
        try:
            starts = [row["backend_start"] for row in storage.admin.get_active_connections()]
        finally:
            observer.close()

        assert starts == sorted(starts)

    def test_connections_to_another_database_are_excluded(self, storage, pg_url, blank_db):
        """`datname = current_database()` : une connexion à une autre base du même
        serveur ne doit pas apparaître, sinon l'admin verrait l'activité de bases
        qui ne sont pas la sienne."""
        other_url = blank_db()
        outsider = psycopg2.connect(other_url, connect_timeout=10)
        try:
            outsider_pid = outsider.get_backend_pid()
            pids = {row["pid"] for row in storage.admin.get_active_connections()}
        finally:
            outsider.close()

        assert outsider_pid not in pids


# ---------------------------------------------------------------------------
# truncate_table
# ---------------------------------------------------------------------------

class TestTruncateTable:
    def test_truncating_an_allowed_table_really_empties_it(self, storage, search, sql):
        storage.listings.save_and_link(
            [make_listing(listing_id="a"), make_listing(listing_id="b")], search["id"],
        )

        assert storage.admin.truncate_table("listings") is True

        assert sql.one("SELECT COUNT(*) FROM listings") == 0

    def test_the_cascade_reaches_the_dependent_table(self, storage, search, sql):
        """`TRUNCATE ... CASCADE` : sans le CASCADE, vider `listings` échouerait
        sur la clé étrangère de `search_listings`. Avec lui, les liens partent
        aussi — la recherche, elle, survit."""
        storage.listings.save_and_link([make_listing(listing_id="a")], search["id"])

        assert storage.admin.truncate_table("listings") is True

        assert sql.one("SELECT COUNT(*) FROM search_listings") == 0
        assert storage.searches.get_search(search["id"])["label"] == "Paris 13e"

    def test_truncating_users_cascades_two_levels_down(self, storage, search, sql):
        """La cascade suit toute la chaîne `users → searches → search_listings`.
        C'est l'entrée d'allowlist la plus destructrice, et elle fonctionne."""
        storage.listings.save_and_link([make_listing(listing_id="a")], search["id"])

        assert storage.admin.truncate_table("users") is True

        assert sql.one("SELECT COUNT(*) FROM users") == 0
        assert sql.one("SELECT COUNT(*) FROM searches") == 0
        assert sql.one("SELECT COUNT(*) FROM search_listings") == 0
        # Les annonces survivent : elles ne dépendent d'aucun utilisateur.
        assert sql.one("SELECT COUNT(*) FROM listings") == 1

    @pytest.mark.parametrize(
        "table_name",
        ["users", "searches", "listings", "search_listings", "admin_logs", "app_settings"],
    )
    def test_every_existing_entry_of_the_allowlist_is_truncatable(self, storage, table_name):
        assert storage.admin.truncate_table(table_name) is True

    def test_the_allowlist_entry_scrape_logs_designates_a_table_that_does_not_exist(self, storage):
        """# BUG (incohérence) : `scrape_logs` figure dans l'allowlist alors que
        les logs de scrape sont stockés en FICHIERS depuis leur refonte (voir
        scrape_logs/storage.py) — la table n'existe plus dans les migrations. Le
        bouton correspondant de l'admin échoue donc toujours, silencieusement
        (`return False` après un log d'erreur), et ne vide surtout PAS les logs
        que l'admin croit purger. Comportement ACTUEL figé.
        """
        assert storage.admin.truncate_table("scrape_logs") is False

    @pytest.mark.parametrize(
        "table_name",
        [
            pytest.param("seloger_place_ids", id="table-reelle-hors-allowlist"),
            pytest.param("pg_class", id="catalogue-systeme"),
            pytest.param("USERS", id="casse-differente"),
            pytest.param("users ", id="espace-final"),
            pytest.param("", id="chaine-vide"),
            pytest.param("users; DROP TABLE searches", id="instruction-collee"),
            pytest.param("users CASCADE; DELETE FROM searches WHERE 1=1; --", id="charge-hostile"),
        ],
    )
    def test_anything_outside_the_allowlist_is_refused_before_touching_the_database(
        self, storage, user, search, table_name, sql,
    ):
        """🔒 `table_name` est interpolé en f-string dans le `TRUNCATE` : c'est la
        seule interpolation d'identifiant du repo, donc l'allowlist EST la
        défense contre l'injection. Le test le vérifie contre le vrai moteur —
        une entrée refusée ne doit produire aucun effet, y compris sur les tables
        qu'une instruction collée viserait.

        La comparaison est un `in` exact : ni normalisation de casse, ni rognage
        des espaces. `"users "` est donc refusé — ce qui est le comportement sûr.
        """
        storage.listings.save_and_link([make_listing(listing_id="a")], search["id"])
        storage.seloger_geo.set_cached("75113", "AD09FR40")

        assert storage.admin.truncate_table(table_name) is False

        assert sql.one("SELECT COUNT(*) FROM users") == 1
        assert sql.one("SELECT COUNT(*) FROM searches") == 1
        assert sql.one("SELECT COUNT(*) FROM listings") == 1
        assert sql.one("SELECT COUNT(*) FROM seloger_place_ids") == 1

    def test_truncating_an_already_empty_table_still_returns_true(self, storage):
        """Le retour est « l'instruction a été exécutée », pas « des lignes ont
        été supprimées » : `TRUNCATE` n'a pas de rowcount exploitable."""
        assert storage.admin.truncate_table("admin_logs") is True

    def test_the_sequence_is_not_restarted(self, storage, sql):
        """Le `TRUNCATE` du repo n'a pas de `RESTART IDENTITY` (contrairement à
        celui de `clean_db`) : les identifiants continuent après le vidage. Un
        export réalisé avant et un après ne se recouvrent donc pas — épinglé
        parce que c'est une différence visible avec la fixture de test."""
        storage.admin.log_admin_action("avant")
        assert sql.one("SELECT id FROM admin_logs") == 1

        storage.admin.truncate_table("admin_logs")
        storage.admin.log_admin_action("apres")

        assert sql.one("SELECT id FROM admin_logs") == 2
