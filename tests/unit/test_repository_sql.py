"""Contrats des repositories testables sans base.

`repositories/base.py` (connexions, pool) est couvert par test_repository_base.py,
et les constructeurs de clauses de `listing_repo` par test_listing_sql_builders.py.
Ce fichier couvre le reste de ce qui est vérifiable sans moteur :

- les **contrats de sécurité** : quelles requêtes portent un `user_id` dans leur
  WHERE et lesquelles n'en portent pas, et ce que valent les allowlists ;
- les **branches qui n'atteignent jamais la base** : liste vide, aucun champ
  fourni, valeur refusée ;
- le **contrat de retour** de `execute_query`, qui ne lève jamais.

Que le SQL produit s'exécute réellement — types, CASCADE, `READ ONLY` — est
prouvé contre un vrai Postgres dans tests/integration/. Ici on ne teste pas la
mise en forme du SQL, sauf là où elle *est* la protection.
"""

from __future__ import annotations

import inspect
import json

import psycopg2
import pytest

from repositories.admin_repo import AdminRepository
from repositories.search_repo import SearchRepository
from repositories.seloger_geo_repo import SelogerGeoRepository
from repositories.settings_repo import SettingsRepository
from repositories.user_repo import UserRepository
from tests.helpers.fakes import RecordingConnection, bind_repository

# Charges qui ne doivent jamais franchir une allowlist ni se retrouver dans du SQL.
HOSTILE_NAMES = [
    pytest.param("users; DROP DATABASE appart", id="chainage"),
    pytest.param("users --", id="commentaire"),
    pytest.param("*", id="joker"),
    pytest.param("", id="vide"),
    pytest.param("pg_shadow", id="table-systeme"),
    pytest.param("Users", id="casse-differente"),
    pytest.param("listings; TRUNCATE users", id="chainage-sur-table-valide"),
]


# ---------------------------------------------------------------------------
# 🔒 Portée des mises à jour : qui vérifie le propriétaire, et qui ne le fait pas
# ---------------------------------------------------------------------------

class TestUpdateScope:
    """Le point le plus fragile de la couche d'accès aux données.

    `update_search` filtre sur `id AND user_id` : l'autorisation est dans le
    SQL, donc infalsifiable. Les quatre autres méthodes de mise à jour ne
    filtrent que sur `id` : leur autorisation dépend **entièrement** d'une
    vérification préalable dans la route appelante.

    Ces tests documentent cette asymétrie plutôt que de la corriger. Elle est
    tenable aujourd'hui parce que chaque route fait bien son contrôle (balayé
    par tests/functional/test_authorization.py), mais toute nouvelle route qui
    appellerait ces méthodes sans vérifier serait une IDOR — et rien dans la
    signature ne l'en avertit.
    """

    def test_update_search_carries_the_owner_in_its_where_clause(self):
        conn = RecordingConnection(results=[None])
        repo = bind_repository(SearchRepository, conn)

        repo.update_search(42, 7, label="Nouveau")

        sql, params = conn.executed[0]
        assert "WHERE id = %s AND user_id = %s" in sql
        assert list(params[-2:]) == [42, 7]

    @pytest.mark.parametrize(
        ("method", "args"),
        [
            pytest.param("update_search_criteria", (42, {"priceMax": 900}), id="criteria"),
            pytest.param("update_scrape_interval", (42, 10), id="interval"),
            pytest.param("update_blacklisted_agencies", (42, ["Foncia"]), id="blacklist-agencies"),
            pytest.param("update_blacklist_mode", (42, "exclude"), id="blacklist-mode"),
            pytest.param("update_last_scraped", (42,), id="last-scraped"),
        ],
    )
    def test_the_other_updates_target_the_id_alone(self, method, args):
        """Contrat à connaître : ces méthodes modifient la ligne d'un autre
        utilisateur si on leur passe son id. La barrière est dans la route."""
        conn = RecordingConnection(results=[None])
        repo = bind_repository(SearchRepository, conn)

        getattr(repo, method)(*args)

        sql, _ = conn.executed[0]
        assert "WHERE id = %s" in sql
        assert "user_id" not in sql

    def test_update_search_without_any_field_never_reaches_the_database(self):
        """Un `UPDATE` sans clause SET est une erreur de syntaxe : la méthode
        doit sortir avant, pas laisser Postgres refuser."""
        conn = RecordingConnection()
        repo = bind_repository(SearchRepository, conn)

        assert repo.update_search(42, 7) is False
        assert conn.executed == []

    def test_the_values_are_parameterized_even_though_the_set_clause_is_built(self):
        """🔒 La clause SET est assemblée dynamiquement à partir des champs
        fournis — mais seuls les *noms* de colonnes viennent du code, les
        valeurs passent toutes par `params`."""
        conn = RecordingConnection(results=[None])
        repo = bind_repository(SearchRepository, conn)
        payload = "'; DROP TABLE searches; --"

        repo.update_search(42, 7, label=payload, ntfy_topic=payload)

        sql, params = conn.executed[0]
        assert payload not in sql
        assert params.count(payload) == 2

    def test_setting_sources_keeps_the_legacy_source_column_in_sync(self):
        """`source` (colonne historique) et `sources` (liste JSONB) doivent
        rester cohérents : le front lit encore la première."""
        conn = RecordingConnection(results=[None])
        repo = bind_repository(SearchRepository, conn)

        repo.update_search(42, 7, sources=["laforet", "seloger"])

        sql, params = conn.executed[0]
        assert "sources = %s" in sql
        assert "source = %s" in sql
        assert "laforet" in params

    @pytest.mark.parametrize("mode", ["nawak", "", "EXCLUDE", "no-notify", None])
    def test_an_invalid_blacklist_mode_is_refused_without_a_query(self, mode):
        """Double validation, volontaire : la route valide déjà, mais un appel
        interne ne doit pas pouvoir stocker un mode que le pipeline de scrape
        ne saurait pas interpréter."""
        conn = RecordingConnection()
        repo = bind_repository(SearchRepository, conn)

        assert repo.update_blacklist_mode(42, mode) is False
        assert conn.executed == []

    @pytest.mark.parametrize("mode", ["exclude", "no_notify"])
    def test_a_valid_blacklist_mode_is_written(self, mode):
        conn = RecordingConnection(results=[None])
        repo = bind_repository(SearchRepository, conn)

        repo.update_blacklist_mode(42, mode)

        _, params = conn.executed[0]
        assert mode in params


# ---------------------------------------------------------------------------
# Normalisation à la lecture
# ---------------------------------------------------------------------------

class TestLoadCriteria:
    """`_load_criteria` est le point unique de normalisation à la lecture.

    Les recherches créées avant l'unification du vocabulaire ne sont pas
    migrées en base : c'est cette fonction qui garantit que tout *lecteur* ne
    voit que du canonique. Un chemin de lecture qui l'oublie expose l'ancien
    vocabulaire à l'interface.
    """

    LEGACY = {
        "placeIds": ["AD08FR31096"],
        "distributionTypes": ["Rent"],
        "estateTypes": ["Apartment"],
        "spaceMin": 40,
    }

    def test_the_legacy_vocabulary_is_translated_on_read(self):
        repo = SearchRepository("postgresql://fake/fake")

        row = repo._load_criteria({"criteria": json.dumps(self.LEGACY)})

        criteria = row["criteria"]
        assert criteria["transaction"] == "rent"
        assert criteria["propertyTypes"] == ["apartment"]
        assert criteria["surfaceMin"] == 40
        assert criteria["sourceOverrides"]["seloger"]["placeIds"] == ["AD08FR31096"]

    @pytest.mark.parametrize("stored", ["", "{pas du json", "null", "[]"])
    def test_unreadable_criteria_degrade_to_an_empty_dict(self, stored):
        """Une colonne corrompue ne doit pas faire tomber la page de liste : la
        recherche s'affiche, simplement sans critères exploitables."""
        repo = SearchRepository("postgresql://fake/fake")

        row = repo._load_criteria({"criteria": stored})

        assert row["criteria"] == {}

    def test_the_dashboard_read_path_skips_this_normalization(self):
        """# BUG (incohérence) : `UserRepository.get_dashboard_data` parse la
        colonne `criteria` avec `_parse_json_column` mais n'appelle PAS
        `normalize_criteria`, contrairement à tous les autres chemins de lecture
        (`get_search`, `get_user_searches`, `get_all_searches`,
        `get_search_detail`).

        Conséquence : pour une recherche jamais réenregistrée depuis
        l'unification du vocabulaire, le tableau de bord affiche l'ancien
        vocabulaire SeLoger (`distributionTypes`, `estateTypes`) tandis que la
        page des recherches affiche le canonique — pour la même recherche.

        Test structurel : il compare les deux chemins dans le code, faute de
        pouvoir exécuter le SQL ici. La preuve par les données est faite dans
        tests/integration/test_user_repo.py.
        """
        search_src = inspect.getsource(SearchRepository._load_criteria)
        dashboard_src = inspect.getsource(UserRepository.get_dashboard_data)

        assert "normalize_criteria" in search_src
        assert "_parse_json_column" in dashboard_src
        assert "normalize_criteria" not in dashboard_src

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            pytest.param('["seloger", "laforet"]', ["seloger", "laforet"], id="json-valide"),
            # BUG : un JSON valide mais scalaire est conservé tel quel — le
            # repli ne se déclenche que sur une valeur *falsy*. `sources`
            # devient alors une chaîne, et l'itération qui suit la parcourt
            # caractère par caractère au lieu de lever. Cas improbable en base
            # (la colonne est écrite par `json.dumps` d'une liste), mais le
            # garde-fou « rester défensif ici aussi » ne couvre pas ce cas.
            pytest.param('"pas une liste"', "pas une liste", id="json-scalaire-conserve-tel-quel"),
            pytest.param("{pas du json", ["seloger"], id="json-casse-retombe-sur-source"),
            pytest.param(None, ["seloger"], id="colonne-nulle"),
            pytest.param([], ["seloger"], id="liste-vide"),
            pytest.param(["laforet"], ["laforet"], id="deja-une-liste"),
        ],
    )
    def test_sources_fall_back_to_the_legacy_column(self, stored, expected):
        """`sources` a été ajouté après coup : les lignes anciennes ont la
        colonne à NULL, et il faut retomber sur `source` — jamais sur une liste
        vide, qui rendrait la recherche inexécutable.

        Note : `_normalize_sources` mute la ligne en place et la retourne, comme
        `_parse_json_column`.
        """
        row = {"sources": stored, "source": "seloger"}

        assert SearchRepository._normalize_sources(row)["sources"] == expected

    def test_sources_fall_back_to_seloger_when_both_columns_are_empty(self):
        """Ni `sources` ni `source` : le défaut historique est SeLoger, la seule
        source qui existait avant l'ajout de la colonne."""
        assert SearchRepository._normalize_sources({})["sources"] == ["seloger"]


# ---------------------------------------------------------------------------
# 🔒 execute_query — SQL arbitraire d'administration
# ---------------------------------------------------------------------------

class TestExecuteQuery:
    """Le contrat de retour compte autant que la protection elle-même : la
    route affiche l'erreur au lieu de rendre une 500, et ne peut le faire que
    parce que cette méthode ne lève jamais.

    Que `SET TRANSACTION READ ONLY` empêche *réellement* une écriture n'est
    prouvable que contre un vrai moteur — c'est fait dans
    tests/integration/test_admin_repo.py.
    """

    def test_rows_are_returned_as_plain_dicts_with_their_count(self):
        conn = RecordingConnection(results=[None, [{"n": 1}, {"n": 2}]])
        repo = bind_repository(AdminRepository, conn)

        rows, count, error = repo.execute_query("SELECT n FROM t")

        assert rows == [{"n": 1}, {"n": 2}]
        assert count == 2
        assert error is None

    def test_a_statement_without_result_set_yields_no_rows_and_no_error(self):
        """`cur.description` est None pour un ordre qui ne renvoie rien : il
        faut le distinguer d'une erreur, sinon la page afficherait un échec
        pour une commande qui a fonctionné."""
        conn = RecordingConnection(results=[None, None])
        repo = bind_repository(AdminRepository, conn)

        assert repo.execute_query("SET work_mem = '4MB'") == ([], 0, None)

    def test_a_failing_query_is_returned_as_an_error_never_raised(self):
        conn = RecordingConnection()

        def boom(sql, params=None):
            raise psycopg2.ProgrammingError('relation "nawak" does not exist')

        repo = bind_repository(AdminRepository, conn)
        repo._get_conn_for_request = lambda: conn
        cur = conn.cursor()
        cur.execute = boom
        conn.cursor = lambda *a, **kw: cur

        rows, count, error = repo.execute_query("SELECT * FROM nawak")

        assert rows == []
        assert count == 0
        assert "nawak" in error

    def test_the_transaction_is_rolled_back_before_and_after_the_query(self):
        """Deux rollbacks encadrent l'exécution : le premier repart d'une
        transaction propre (la connexion vient du pool et peut traîner un état),
        le second annule tout ce que la requête aurait pu commencer.

        Sans le second, une connexion rendue au pool en transaction ouverte
        garderait ses verrous pour l'emprunteur suivant.
        """
        conn = RecordingConnection(results=[None, [{"n": 1}]])
        repo = bind_repository(AdminRepository, conn)

        repo.execute_query("SELECT 1")

        assert conn.rollbacks == 2
        assert conn.commits == 0

    def test_a_failing_query_still_leaves_the_connection_clean(self):
        conn = RecordingConnection()

        def boom(sql, params=None):
            raise psycopg2.ProgrammingError("boom")

        repo = bind_repository(AdminRepository, conn)
        cur = conn.cursor()
        cur.execute = boom
        conn.cursor = lambda *a, **kw: cur

        repo.execute_query("SELECT 1")

        assert conn.rollbacks >= 1
        assert conn.commits == 0

    def test_the_read_only_mode_is_requested_before_the_user_sql(self):
        """L'ordre est la protection : inverser les deux exécuterait la requête
        de l'administrateur dans une transaction encore modifiable."""
        conn = RecordingConnection(results=[None, [{"n": 1}]])
        repo = bind_repository(AdminRepository, conn)

        repo.execute_query("DELETE FROM users")

        assert conn.sql[0] == "SET TRANSACTION READ ONLY"
        assert conn.sql[1] == "DELETE FROM users"


class TestTruncateTable:
    @pytest.mark.parametrize("table_name", HOSTILE_NAMES)
    def test_a_name_outside_the_allowlist_never_produces_a_query(self, table_name):
        """🔒 Le nom de table est interpolé dans le SQL (`TRUNCATE TABLE {name}
        CASCADE`) : il n'existe pas de paramètre pour un identifiant en SQL.
        L'allowlist est donc la seule protection, et doit refuser avant
        d'exécuter quoi que ce soit."""
        conn = RecordingConnection()
        repo = bind_repository(AdminRepository, conn)

        assert repo.truncate_table(table_name) is False
        assert conn.executed == []

    @pytest.mark.parametrize(
        "table_name",
        ["users", "searches", "listings", "search_listings", "admin_logs", "app_settings"],
    )
    def test_an_allowed_table_is_truncated_with_cascade(self, table_name):
        conn = RecordingConnection(results=[None])
        repo = bind_repository(AdminRepository, conn)

        assert repo.truncate_table(table_name) is True
        assert conn.sql[0] == f"TRUNCATE TABLE {table_name} CASCADE"
        assert conn.commits == 1

    def test_the_allowlist_still_contains_a_table_that_no_longer_exists(self):
        """# BUG : `scrape_logs` figure dans l'allowlist, mais cette table
        n'existe plus — les logs de scrape sont stockés en fichiers depuis
        `scrape_logs/storage.py`. La demander produit une erreur Postgres au
        lieu d'un refus clair, et laisse croire à l'administrateur que cette
        donnée vit en base."""
        conn = RecordingConnection(results=[None])
        repo = bind_repository(AdminRepository, conn)

        assert repo.truncate_table("scrape_logs") is True
        assert "TRUNCATE TABLE scrape_logs CASCADE" in conn.sql

    def test_the_repository_allows_a_table_the_route_forbids(self):
        """# BUG (divergence) : `app_settings` est autorisée ici mais absente de
        l'allowlist de `routes/admin.py`. Deux listes pour une même règle : elles
        ont déjà divergé, et divergeront encore."""
        from routes import admin as admin_routes

        route_src = inspect.getsource(admin_routes.admin_truncate_table)

        conn = RecordingConnection(results=[None])
        assert bind_repository(AdminRepository, conn).truncate_table("app_settings") is True
        assert "app_settings" not in route_src

    def test_a_failing_truncate_rolls_back_and_reports_false(self):
        conn = RecordingConnection()

        def boom(sql, params=None):
            raise psycopg2.InternalError("no space left")

        repo = bind_repository(AdminRepository, conn)
        cur = conn.cursor()
        cur.execute = boom
        conn.cursor = lambda *a, **kw: cur

        assert repo.truncate_table("listings") is False
        assert conn.rollbacks == 1


class TestLogAdminAction:
    def test_an_audit_failure_never_breaks_the_audited_action(self):
        """Le journal est important, mais pas au point d'annuler la suppression
        qu'il devait tracer : l'exception est avalée après rollback.

        Contrepartie assumée : une action peut être exécutée sans laisser de
        trace. C'est le compromis choisi, ce test le rend explicite.
        """
        conn = RecordingConnection()

        def boom(sql, params=None):
            raise psycopg2.OperationalError("connexion perdue")

        repo = bind_repository(AdminRepository, conn)
        cur = conn.cursor()
        cur.execute = boom
        conn.cursor = lambda *a, **kw: cur

        repo.log_admin_action("user_deleted", "détails", "root")

        assert conn.rollbacks == 1

    def test_the_action_details_are_parameterized(self):
        conn = RecordingConnection(results=[None])
        repo = bind_repository(AdminRepository, conn)
        payload = "'; DROP TABLE admin_logs; --"

        repo.log_admin_action("db_query", payload, "root")

        sql, params = conn.executed[0]
        assert payload not in sql
        assert payload in params


# ---------------------------------------------------------------------------
# Utilisateurs
# ---------------------------------------------------------------------------

class TestCreateUser:
    def test_the_token_is_generated_with_a_cryptographic_source(self, monkeypatch):
        """Le jeton est le seul secret du compte : il doit venir de `secrets`,
        pas de `random`. On le fige ici pour rendre le test déterministe, ce qui
        vérifie au passage que c'est bien ce module qui est utilisé."""
        monkeypatch.setattr("repositories.user_repo.secrets.token_urlsafe", lambda n: f"jeton-{n}")
        conn = RecordingConnection(results=[{"id": 1}])
        repo = bind_repository(UserRepository, conn)

        user = repo.create_user("alice")

        assert user == {"id": 1, "username": "alice", "api_token": "jeton-32"}

    def test_a_duplicate_username_becomes_a_user_facing_error(self):
        """Le message est affiché tel quel (409 dans l'API, flash sur le web) :
        c'est un contrat de chaîne, et le rollback qui l'accompagne évite de
        rendre au pool une connexion en transaction avortée."""
        conn = RecordingConnection()

        def boom(sql, params=None):
            raise psycopg2.IntegrityError("duplicate key")

        repo = bind_repository(UserRepository, conn)
        cur = conn.cursor()
        cur.execute = boom
        conn.cursor = lambda *a, **kw: cur

        with pytest.raises(ValueError, match=r"Le nom d'utilisateur 'alice' est déjà pris"):
            repo.create_user("alice")

        assert conn.rollbacks == 1

    def test_the_original_error_is_kept_as_the_cause(self):
        """`raise ... from e` : la trace psycopg2 complète reste disponible côté
        serveur sans polluer le message montré à l'utilisateur."""
        conn = RecordingConnection()

        def boom(sql, params=None):
            raise psycopg2.IntegrityError("duplicate key")

        repo = bind_repository(UserRepository, conn)
        cur = conn.cursor()
        cur.execute = boom
        conn.cursor = lambda *a, **kw: cur

        with pytest.raises(ValueError, match=r"déjà pris") as excinfo:
            repo.create_user("alice")

        assert isinstance(excinfo.value.__cause__, psycopg2.IntegrityError)

    def test_resetting_a_token_does_not_check_that_the_user_exists(self, monkeypatch):
        """# BUG : `reset_user_token` renvoie un jeton neuf même quand aucune
        ligne n'a été modifiée (`rowcount == 0`). L'appelant croit avoir
        réinitialisé le compte et affiche un jeton qui n'existe nulle part.
        Vérifier `rowcount` suffirait à renvoyer None."""
        monkeypatch.setattr("repositories.user_repo.secrets.token_urlsafe", lambda n: "jeton-orphelin")
        conn = RecordingConnection(results=[None])
        repo = bind_repository(UserRepository, conn)

        assert repo.reset_user_token(9999) == "jeton-orphelin"


# ---------------------------------------------------------------------------
# Réglages et cache géo
# ---------------------------------------------------------------------------

class TestSettingsRepository:
    def test_a_missing_key_yields_the_default(self):
        conn = RecordingConnection(results=[None])
        repo = bind_repository(SettingsRepository, conn)

        assert repo.get_setting("absente", default="repli") == "repli"

    def test_a_stored_value_is_returned(self):
        conn = RecordingConnection(results=[{"value": "true"}])
        repo = bind_repository(SettingsRepository, conn)

        assert repo.get_setting("un_reglage") == "true"

    def test_writing_a_setting_upserts_instead_of_failing_on_conflict(self):
        """`ON CONFLICT (key) DO UPDATE` : écrire deux fois la même clé met à
        jour, sans que l'appelant ait à savoir si elle existait."""
        conn = RecordingConnection(results=[None])
        repo = bind_repository(SettingsRepository, conn)

        repo.set_setting("cle", "valeur")

        assert "ON CONFLICT" in conn.sql[0]
        assert conn.commits == 1

    def test_this_repository_has_no_caller_in_production(self):
        """Aucun appel à `settings.get_setting`/`set_setting` dans le code de
        production : ce repository est du code mort candidat. Il reste testé
        (il est instancié par `Storage`), mais ce test signale qu'il pourrait
        disparaître — et échouera le jour où quelqu'un s'en sert, invitant à
        le retirer d'ici.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2]
        callers = []
        for path in root.rglob("*.py"):
            parts = set(path.parts)
            if parts & {"venv", "tests", "_legacy", "graphify-out"}:
                continue
            text = path.read_text(encoding="utf-8")
            if "settings.get_setting" in text or "settings.set_setting" in text:
                callers.append(path.name)

        assert callers == []


class TestSelogerGeoRepository:
    def test_no_row_means_never_attempted(self):
        """Sémantique porteuse : `None` (pas de ligne) et une ligne avec
        `place_id` à NULL ne veulent pas dire la même chose. La première
        déclenche une résolution, la seconde active le délai de 7 jours avant
        nouvelle tentative — les confondre ferait soit re-interroger SeLoger à
        chaque scrape, soit ne plus jamais réessayer."""
        conn = RecordingConnection(results=[None])
        repo = bind_repository(SelogerGeoRepository, conn)

        assert repo.get_cached("75113") is None

    def test_a_row_with_a_null_place_id_is_a_remembered_failure(self):
        conn = RecordingConnection(results=[{"place_id": None, "resolved_at": "2026-07-01T10:00:00"}])
        repo = bind_repository(SelogerGeoRepository, conn)

        cached = repo.get_cached("75113")

        assert cached is not None
        assert cached["place_id"] is None

    @pytest.mark.parametrize(
        "area_key",
        ["75113", "city:86194", "region:75", "dept:33"],
        ids=["commune-insee-nu", "ville-entiere", "region", "departement"],
    )
    def test_every_scope_level_uses_the_same_key_column(self, area_key):
        """Le niveau fait partie de la clé (`region:75` et `dept:75` coexistent) :
        une seule colonne suffit, à condition que le préfixe soit toujours posé
        par `area_cache_key`."""
        conn = RecordingConnection(results=[None])
        repo = bind_repository(SelogerGeoRepository, conn)

        repo.get_cached(area_key)

        _, params = conn.executed[0]
        assert params == (area_key,)

    def test_caching_a_place_id_upserts(self):
        conn = RecordingConnection(results=[None])
        repo = bind_repository(SelogerGeoRepository, conn)

        repo.set_cached("75113", "AD08FR31096")

        assert "ON CONFLICT" in conn.sql[0]
        assert conn.commits == 1

    def test_caching_a_failure_is_an_explicit_write(self):
        """Mémoriser l'échec est une écriture comme une autre : c'est ce qui
        évite de réinterroger SeLoger à chaque scrape pour un périmètre qu'il
        ne connaît pas."""
        conn = RecordingConnection(results=[None])
        repo = bind_repository(SelogerGeoRepository, conn)

        repo.set_cached("dept:99", None)

        _, params = conn.executed[0]
        assert params[0] == "dept:99"
        assert None in params
