"""Tests unitaires de `repositories/base.py` — pool, emprunts et restitutions.

Ce module est le seul endroit du dépôt qui touche au cycle de vie des
connexions Postgres, et chacun de ses chemins est *silencieux par
construction* : `release_to_pool` avale ses exceptions, `_close_conn` avale les
siennes, `_get_conn_for_request` avale l'absence de contexte Flask. Une
régression ici ne lève rien — elle fuit des connexions ou en empoisonne une, et
se manifeste beaucoup plus tard sous forme de `getconn` qui bloque.

Aucun test n'ouvre de vraie connexion : la fixture autouse `no_real_database`
fait lever `psycopg2.connect` et `ThreadedConnectionPool`, on les remplace donc
localement par des doubles *utilisables*. Le fait que le SQL émis soit valide
relève de tests/integration/ ; ici on vérifie qu'il est émis, et quand.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import psycopg2
import psycopg2.extensions
import psycopg2.extras
import psycopg2.pool
import pytest
from flask import Flask, g

from repositories.base import BaseRepository
from repositories.listing_repo import ListingRepository
from repositories.search_repo import SearchRepository
from repositories.user_repo import UserRepository
from tests.helpers.fakes import RecordingConnection

FAKE_URL = "postgresql://fake/fake"
OTHER_URL = "postgresql://fake/autre"

INERROR = psycopg2.extensions.TRANSACTION_STATUS_INERROR
INTRANS = psycopg2.extensions.TRANSACTION_STATUS_INTRANS
IDLE = psycopg2.extensions.TRANSACTION_STATUS_IDLE
ACTIVE = psycopg2.extensions.TRANSACTION_STATUS_ACTIVE


# ---------------------------------------------------------------------------
# Outillage
# ---------------------------------------------------------------------------

@pytest.fixture
def pool_factory(monkeypatch):
    """Remplace `ThreadedConnectionPool` par une usine à doubles qui s'enregistre.

    La valeur renvoyée est la liste des `(args, kwargs)` de chaque construction :
    son *nombre d'éléments* est l'assertion centrale de la plupart des tests de
    pool (un pool et un seul par URL).
    """
    calls: list[tuple[tuple, dict]] = []

    def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return MagicMock(name=f"pool-{len(calls)}")

    monkeypatch.setattr(psycopg2.pool, "ThreadedConnectionPool", factory)
    return calls


@pytest.fixture
def flask_app():
    """Une app Flask nue : on ne veut qu'un `app_context` pour peupler `g`."""
    return Flask(__name__)


def repo_with_pool(pool, url: str = FAKE_URL) -> BaseRepository:
    """Un repository dont `_get_pool` renvoie `pool`, sans passer par `_pools`."""
    repo = BaseRepository(url)
    repo._get_pool = lambda: pool
    return repo


# ---------------------------------------------------------------------------
# _get_pool — un pool par URL, partagé par toutes les instances
# ---------------------------------------------------------------------------

class TestGetPool:
    def test_init_stores_the_database_url(self):
        assert BaseRepository(FAKE_URL).database_url == FAKE_URL

    def test_pool_is_created_once_and_memoized(self, pool_factory):
        repo = BaseRepository(FAKE_URL)

        first = repo._get_pool()
        second = repo._get_pool()

        assert first is second
        assert len(pool_factory) == 1

    def test_pool_is_created_with_bounds_and_connect_timeout(self, pool_factory):
        """1..20 connexions et `connect_timeout=10` : sans ce timeout, un
        Postgres injoignable fait pendre le thread appelant indéfiniment."""
        BaseRepository(FAKE_URL)._get_pool()

        args, kwargs = pool_factory[0]
        assert args == (1, 20)
        assert kwargs == {"dsn": FAKE_URL, "connect_timeout": 10}

    def test_pool_is_shared_across_repository_classes_for_the_same_url(self, pool_factory):
        """`_pools` est un dict de CLASSE : deux repositories différents branchés
        sur la même base partagent le pool. C'est ce qui borne le nombre de
        connexions ouvertes au niveau du process, et non par repository."""
        users = UserRepository(FAKE_URL)
        searches = SearchRepository(FAKE_URL)
        listings = ListingRepository(FAKE_URL)

        pools = [users._get_pool(), searches._get_pool(), listings._get_pool()]

        assert pools[0] is pools[1] is pools[2]
        assert len(pool_factory) == 1

    def test_distinct_urls_get_distinct_pools(self, pool_factory):
        pool_a = BaseRepository(FAKE_URL)._get_pool()
        pool_b = BaseRepository(OTHER_URL)._get_pool()

        assert pool_a is not pool_b
        assert len(pool_factory) == 2
        assert set(BaseRepository._pools) == {FAKE_URL, OTHER_URL}

    def test_second_check_inside_the_lock_yields_to_the_winning_thread(self, monkeypatch, pool_factory):
        """Le double-checked locking : le `if pool is None` *interne au verrou*
        doit voir le pool posé par un thread concurrent et ne pas en créer un
        second (qui fuiterait, personne ne gardant sa référence).

        La course est rendue déterministe en faisant déposer le pool du
        « gagnant » par le verrou lui-même, au moment de son acquisition.
        """
        winner = MagicMock(name="pool-du-gagnant")

        class HandOffLock:
            def __enter__(self):
                BaseRepository._pools[FAKE_URL] = winner
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(BaseRepository, "_pools_lock", HandOffLock())

        assert BaseRepository(FAKE_URL)._get_pool() is winner
        assert pool_factory == []

    def test_concurrent_first_access_creates_exactly_one_pool(self, pool_factory):
        """Huit threads démarrés en même temps par une barrière : le verrou doit
        sérialiser la création. Sans lui, plusieurs pools seraient construits et
        tous sauf le dernier deviendraient des connexions orphelines."""
        repo = BaseRepository(FAKE_URL)
        barrier = threading.Barrier(8)
        seen: list[object] = []
        seen_lock = threading.Lock()

        def worker():
            barrier.wait(timeout=5)
            pool = repo._get_pool()
            with seen_lock:
                seen.append(pool)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert len(seen) == 8
        assert all(pool is seen[0] for pool in seen)
        assert len(pool_factory) == 1


# ---------------------------------------------------------------------------
# _get_conn / _get_ddl_conn — ce qui est émis à l'emprunt
# ---------------------------------------------------------------------------

class TestGetConn:
    def test_statement_timeout_is_set_on_every_single_borrow(self):
        """`SET statement_timeout` est une propriété de *session* : le pool
        recycle les connexions, donc l'ordre doit être rejoué à chaque emprunt
        et pas seulement à la création du pool."""
        conn = RecordingConnection()
        pool = MagicMock()
        pool.getconn.return_value = conn
        repo = repo_with_pool(pool)

        assert repo._get_conn() is conn
        assert repo._get_conn() is conn

        assert conn.sql == ["SET statement_timeout = '30000'"] * 2
        assert pool.getconn.call_count == 2

    def test_the_timeout_cursor_is_closed(self):
        """Un curseur laissé ouvert sur une connexion rendue au pool fuite côté
        serveur ; ici il n'y a rien à lire, il doit être refermé aussitôt."""
        conn = RecordingConnection()
        pool = MagicMock()
        pool.getconn.return_value = conn

        repo_with_pool(pool)._get_conn()

        assert [cur.closed for cur in conn.cursors] == [True]

    def test_ddl_conn_bypasses_the_pool_and_sets_a_lock_timeout(self, monkeypatch):
        """Le DDL de démarrage ouvre une connexion hors pool (elle sera fermée,
        pas rendue) avec un `lock_timeout` : un ALTER TABLE bloqué derrière une
        transaction longue doit abandonner au lieu de figer le boot."""
        conn = RecordingConnection()
        captured: dict = {}

        def fake_connect(dsn, **kwargs):
            captured["dsn"] = dsn
            captured["kwargs"] = kwargs
            return conn

        monkeypatch.setattr(psycopg2, "connect", fake_connect)

        assert BaseRepository(FAKE_URL)._get_ddl_conn() is conn
        assert captured == {"dsn": FAKE_URL, "kwargs": {"connect_timeout": 30}}
        assert conn.sql == ["SET lock_timeout = '60000'"]
        assert [cur.closed for cur in conn.cursors] == [True]


# ---------------------------------------------------------------------------
# _get_conn_for_request — la même méthode dans une requête et dans un thread
# ---------------------------------------------------------------------------

class TestGetConnForRequest:
    def test_reuses_the_shared_flask_request_connection(self, flask_app):
        """Dans une requête, tous les repositories doivent taper la même
        connexion : c'est ce qui rend une requête transactionnellement
        cohérente, et ce qui l'empêche de consommer N connexions du pool."""
        shared = RecordingConnection()
        pool = MagicMock()
        repo = repo_with_pool(pool)

        with flask_app.app_context():
            g._db_conn = shared
            assert repo._get_conn_for_request() is shared

        pool.getconn.assert_not_called()
        # Pas de `SET statement_timeout` : il a été posé lors de l'emprunt initial.
        assert shared.executed == []

    @pytest.mark.parametrize("preset", [None, "absent"])
    def test_borrows_from_the_pool_when_the_request_has_no_connection_yet(self, flask_app, preset):
        """Dans un contexte Flask mais sans connexion partagée (première requête,
        ou `g._db_conn` remis à None au teardown), on emprunte normalement."""
        borrowed = RecordingConnection()
        pool = MagicMock()
        pool.getconn.return_value = borrowed
        repo = repo_with_pool(pool)

        with flask_app.app_context():
            if preset is None:
                g._db_conn = None
            assert repo._get_conn_for_request() is borrowed

        pool.getconn.assert_called_once()
        assert borrowed.sql == ["SET statement_timeout = '30000'"]

    def test_borrows_from_the_pool_outside_any_flask_context(self):
        """Le `except Exception: pass` autour de `g` est ce qui permet au
        scheduler (thread de fond, aucun contexte d'application) d'appeler les
        mêmes méthodes de repository que les routes."""
        borrowed = RecordingConnection()
        pool = MagicMock()
        pool.getconn.return_value = borrowed
        repo = repo_with_pool(pool)

        assert repo._get_conn_for_request() is borrowed
        pool.getconn.assert_called_once()


# ---------------------------------------------------------------------------
# _release_conn — ne jamais rendre la connexion de la requête en cours
# ---------------------------------------------------------------------------

class TestReleaseConn:
    def test_keeps_the_shared_request_connection(self, flask_app):
        """La connexion de requête est rendue au teardown, pas par le
        repository : la rendre ici la ferait recycler alors que la requête
        continue de s'en servir — deux requêtes se marcheraient dessus."""
        conn = MagicMock()
        pool = MagicMock()
        repo = repo_with_pool(pool)

        with flask_app.app_context():
            g._db_conn = conn
            repo._release_conn(conn)

        pool.putconn.assert_not_called()
        conn.close.assert_not_called()
        conn.rollback.assert_not_called()

    def test_releases_a_connection_that_is_not_the_request_one(self, flask_app):
        """Comparaison par identité : dans un contexte Flask, une *autre*
        connexion (empruntée à part par un thread, par exemple) est bien rendue."""
        request_conn = MagicMock()
        other = MagicMock()
        other.get_transaction_status.return_value = IDLE
        pool = MagicMock()
        repo = repo_with_pool(pool)

        with flask_app.app_context():
            g._db_conn = request_conn
            repo._release_conn(other)

        pool.putconn.assert_called_once_with(other)

    def test_releases_outside_any_flask_context(self):
        conn = MagicMock()
        conn.get_transaction_status.return_value = IDLE
        pool = MagicMock()

        repo_with_pool(pool)._release_conn(conn)

        pool.putconn.assert_called_once_with(conn)


# ---------------------------------------------------------------------------
# release_to_pool — le rollback défensif et son chemin de repli
# ---------------------------------------------------------------------------

class TestReleaseToPool:
    def test_none_is_a_no_op(self):
        """Les `finally: self._release_conn(conn)` peuvent s'exécuter avant que
        `conn` ait été affecté ; le None ne doit pas masquer l'exception initiale
        par un AttributeError."""
        pool = MagicMock()
        repo_with_pool(pool).release_to_pool(None)
        pool.putconn.assert_not_called()

    @pytest.mark.parametrize(
        ("status", "expect_rollback"),
        [
            (INERROR, True),
            (IDLE, False),
            # BUG : INTRANS (« idle in transaction ») n'est PAS traité. Une
            # méthode de lecture qui a ouvert une transaction implicite sans
            # jamais commiter rend la connexion au pool transaction ouverte —
            # elle garde ses verrous et son snapshot MVCC, et le prochain
            # emprunteur hérite d'une vue figée dans le passé. Seul INERROR est
            # rattrapé ici ; ce test fige le comportement ACTUEL.
            (INTRANS, False),
            (ACTIVE, False),
        ],
    )
    def test_rollback_only_happens_for_an_aborted_transaction(self, status, expect_rollback):
        conn = MagicMock()
        conn.get_transaction_status.return_value = status
        pool = MagicMock()

        repo_with_pool(pool).release_to_pool(conn)

        assert conn.rollback.call_count == (1 if expect_rollback else 0)
        pool.putconn.assert_called_once_with(conn)
        conn.close.assert_not_called()

    def test_a_failing_transaction_status_probe_does_not_prevent_the_release(self):
        """Sur une connexion déjà cassée, `get_transaction_status` peut lever :
        la connexion doit quand même repartir au pool (qui la recyclera)."""
        conn = MagicMock()
        conn.get_transaction_status.side_effect = psycopg2.InterfaceError("connection already closed")
        pool = MagicMock()

        repo_with_pool(pool).release_to_pool(conn)

        pool.putconn.assert_called_once_with(conn)

    def test_a_failing_rollback_does_not_prevent_the_release(self):
        conn = MagicMock()
        conn.get_transaction_status.return_value = INERROR
        conn.rollback.side_effect = psycopg2.InterfaceError("connection already closed")
        pool = MagicMock()

        repo_with_pool(pool).release_to_pool(conn)

        pool.putconn.assert_called_once_with(conn)

    def test_a_rejected_putconn_falls_back_to_closing_the_connection(self):
        """Chemin de repli jamais couvert jusqu'ici, et pourtant le seul rempart
        contre une fuite : `putconn` lève quand la connexion n'appartient pas à
        ce pool (pool recréé après un reload) ou quand le pool est fermé. Sans
        le `_close_conn`, le socket resterait ouvert jusqu'au GC.
        """
        conn = RecordingConnection()
        pool = MagicMock()
        pool.putconn.side_effect = psycopg2.pool.PoolError("trying to put unkeyed connection")

        repo_with_pool(pool).release_to_pool(conn)

        assert conn.closed is True

    def test_the_fallback_does_not_reclose_an_already_closed_connection(self):
        conn = MagicMock()
        conn.closed = 1  # psycopg2 expose un int, pas un bool
        conn.get_transaction_status.return_value = IDLE
        pool = MagicMock()
        pool.putconn.side_effect = psycopg2.pool.PoolError("pool is closed")

        repo_with_pool(pool).release_to_pool(conn)

        conn.close.assert_not_called()


class TestCloseConn:
    def test_none_is_a_no_op(self):
        BaseRepository(FAKE_URL)._close_conn(None)

    def test_closes_an_open_connection(self):
        conn = RecordingConnection()
        BaseRepository(FAKE_URL)._close_conn(conn)
        assert conn.closed is True

    def test_skips_an_already_closed_connection(self):
        conn = MagicMock()
        conn.closed = 1
        BaseRepository(FAKE_URL)._close_conn(conn)
        conn.close.assert_not_called()

    def test_swallows_a_failing_close(self):
        """`_close_conn` est appelé depuis des `finally` et depuis le repli de
        `release_to_pool` : s'il levait, il masquerait l'erreur d'origine."""
        conn = MagicMock()
        conn.closed = 0
        conn.close.side_effect = psycopg2.InterfaceError("already closed")

        BaseRepository(FAKE_URL)._close_conn(conn)

        conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# _parse_json_column — psycopg2 renvoie parfois du JSONB en str
# ---------------------------------------------------------------------------

class TestParseJsonColumn:
    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ('{"priceMax": 900}', {"priceMax": 900}),
            ("{}", {}),
            ('{"nested": {"a": [1, 2]}}', {"nested": {"a": [1, 2]}}),
            ('["pas-un-objet"]', ["pas-un-objet"]),  # json.loads ne contraint pas le type
        ],
    )
    def test_a_json_string_is_decoded(self, stored, expected):
        row = {"criteria": stored}
        BaseRepository(FAKE_URL)._parse_json_column(row, "criteria")
        assert row["criteria"] == expected

    @pytest.mark.parametrize("broken", ["not-json", "", "{", "{'simple': 'quotes'}", "undefined"])
    def test_undecodable_content_degrades_to_an_empty_dict(self, broken):
        """Des critères illisibles ne doivent pas faire tomber toute la page de
        recherches : on préfère une recherche vide à un 500."""
        row = {"criteria": broken}
        BaseRepository(FAKE_URL)._parse_json_column(row, "criteria")
        assert row["criteria"] == {}

    @pytest.mark.parametrize(
        "already_decoded",
        [
            {"priceMax": 900},
            [1, 2, 3],
            None,
            42,
            True,
        ],
    )
    def test_a_non_string_value_is_left_untouched(self, already_decoded):
        """psycopg2 décode déjà le JSONB la plupart du temps : re-parser
        (ou remplacer par {}) écraserait la valeur. Un None reste None — le
        `.get()` d'un appelant doit pouvoir distinguer « absent » de « vide »."""
        row = {"criteria": already_decoded}
        BaseRepository(FAKE_URL)._parse_json_column(row, "criteria")
        assert row["criteria"] is already_decoded

    def test_a_missing_column_is_not_invented(self):
        row = {"autre": "valeur"}
        BaseRepository(FAKE_URL)._parse_json_column(row, "criteria")
        assert "criteria" not in row

    def test_the_row_is_mutated_in_place_and_returned(self):
        """Les appelants font indifféremment `self._parse_json_column(d, ...)` et
        `d = self._parse_json_column(d, ...)` : les deux doivent marcher."""
        row = {"criteria": '{"a": 1}'}
        returned = BaseRepository(FAKE_URL)._parse_json_column(row, "criteria")
        assert returned is row


# ---------------------------------------------------------------------------
# _dict_cursor
# ---------------------------------------------------------------------------

def test_dict_cursor_asks_for_a_real_dict_cursor():
    """Toutes les méthodes qui font `dict(row)` en dépendent : avec un curseur
    ordinaire, `dict(tuple)` lèverait."""
    conn = MagicMock()

    BaseRepository(FAKE_URL)._dict_cursor(conn)

    conn.cursor.assert_called_once_with(cursor_factory=psycopg2.extras.RealDictCursor)
