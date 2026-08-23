"""Le pool de connexions (repositories/base.py) contre un vrai moteur.

Ce que seul un vrai Postgres peut établir ici : l'état de transaction réel
d'une connexion rendue au pool, les verrous qu'elle détient encore ou non, le
`statement_timeout` effectivement appliqué à la session, et le comportement du
pool à saturation. Un double de psycopg2 dirait ce qu'on lui a soufflé.
"""

from __future__ import annotations

import psycopg2
import psycopg2.extensions
import psycopg2.pool
import pytest
from flask import Flask, g

from repositories.base import BaseRepository
from repositories.listing_repo import ListingRepository
from repositories.search_repo import SearchRepository
from repositories.user_repo import UserRepository

INERROR = psycopg2.extensions.TRANSACTION_STATUS_INERROR
INTRANS = psycopg2.extensions.TRANSACTION_STATUS_INTRANS
IDLE = psycopg2.extensions.TRANSACTION_STATUS_IDLE


def _observer(pg_url):
    """Connexion indépendante du pool, pour observer le serveur de l'extérieur."""
    conn = psycopg2.connect(pg_url, connect_timeout=10)
    conn.autocommit = True
    return conn


def _scalar(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


# ---------------------------------------------------------------------------
# Un seul pool par database_url
# ---------------------------------------------------------------------------

class TestPoolSharing:
    def test_all_repositories_of_one_storage_share_the_same_pool_object(self, storage):
        """Le pool est un attribut de CLASSE indexé par URL : les neuf repos
        d'un `Storage` doivent tomber sur le même objet, sinon chaque repo
        ouvrirait ses propres 20 connexions (180 au total)."""
        pools = {
            id(repo._get_pool())
            for repo in (
                storage.users, storage.searches, storage.listings,
                storage.scrape_logs, storage.admin, storage.settings,
                storage.seloger_geo, storage.bienici_geo, storage.century21_geo,
            )
        }

        assert len(pools) == 1
        # Les helpers de `Storage` (_get_conn, release_to_pool...) délèguent à
        # `users`, donc ils empruntent bien dans ce pool unique.
        assert pools == {id(storage.users._get_pool())}

    def test_repositories_built_separately_still_share_the_pool(self, pg_url):
        """Deux instances créées à la main (comme le fait `run_migrations`)
        partagent le pool du process : aucune connexion en double."""
        assert UserRepository(pg_url)._get_pool() is SearchRepository(pg_url)._get_pool()
        assert ListingRepository(pg_url)._get_pool() is UserRepository(pg_url)._get_pool()

    def test_a_different_url_gets_its_own_pool(self, pg_url, storage):
        """L'indexation par URL est ce qui permet à un test de migration de
        travailler sur une base jetable sans voler les connexions de l'autre."""
        other = UserRepository(pg_url + "?application_name=autre")

        assert other._get_pool() is not storage.users._get_pool()
        other._get_pool().closeall()
        BaseRepository._pools.pop(other.database_url, None)


# ---------------------------------------------------------------------------
# statement_timeout
# ---------------------------------------------------------------------------

class TestStatementTimeout:
    def test_borrowed_connection_has_the_thirty_second_statement_timeout(self, storage):
        conn = storage._get_conn()
        try:
            assert _scalar(conn, "SHOW statement_timeout") == "30s"
        finally:
            storage._release_conn(conn)

    def test_timeout_is_reapplied_on_every_borrow(self, storage):
        """Le `SET` est annulé par le rollback que le pool effectue au retour
        de la connexion (un `SET` non-LOCAL reste transactionnel). Il doit donc
        être posé à CHAQUE emprunt — c'est bien ce que fait `_get_conn`."""
        first = storage._get_conn()
        first_pid = first.get_backend_pid()
        storage._release_conn(first)

        second = storage._get_conn()
        try:
            assert second.get_backend_pid() == first_pid  # même connexion physique, réutilisée
            assert _scalar(second, "SHOW statement_timeout") == "30s"
        finally:
            storage._release_conn(second)

    def test_a_freshly_borrowed_connection_is_already_inside_a_transaction(self, storage, pg_url):
        """# BUG : `_get_conn` exécute `SET statement_timeout` sans commit, ce
        qui ouvre une transaction implicite. La connexion sort donc du pool en
        `idle in transaction`, et sous Flask elle le reste de `before_request`
        jusqu'au `teardown_request` — pour toute requête, même en lecture
        seule, y compris pendant que le template se rend.

        Conséquence côté serveur : le snapshot est retenu tout du long, donc
        l'horizon de VACUUM est bloqué par la durée de la requête HTTP la plus
        lente. Comportement documenté, pas corrigé ici.
        """
        conn = storage._get_conn()
        observer = _observer(pg_url)
        try:
            assert conn.get_transaction_status() == INTRANS
            state = _scalar(
                observer, "SELECT state FROM pg_stat_activity WHERE pid = %s", (conn.get_backend_pid(),),
            )
            assert state == "idle in transaction"
        finally:
            observer.close()
            storage._release_conn(conn)


# ---------------------------------------------------------------------------
# Retour au pool d'une connexion en transaction avortée — LE test qui compte
# ---------------------------------------------------------------------------

class TestReleaseOfADirtyConnection:
    def test_a_connection_left_in_error_does_not_poison_the_next_borrower(self, storage, sql):
        """Le scénario réel : une méthode d'écriture lève avant son propre
        rollback, la connexion part au pool en transaction avortée, et la
        requête HTTP suivante l'emprunte. Sans le rollback de
        `release_to_pool`, toute requête sur cette connexion répondrait
        « current transaction is aborted » jusqu'au redémarrage du process.
        """
        conn = storage._get_conn()
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.UndefinedTable):
                cur.execute("SELECT * FROM table_qui_nexiste_pas")
        assert conn.get_transaction_status() == INERROR

        storage.release_to_pool(conn)

        reborrowed = storage._get_conn()
        try:
            assert reborrowed.get_transaction_status() != INERROR
            # Et une vraie requête applicative passe.
            assert _scalar(reborrowed, "SELECT COUNT(*) FROM users") == 0
        finally:
            storage._release_conn(reborrowed)

        # Le chemin complet aussi : un repo qui lève sur IntegrityError laisse
        # une connexion réutilisable derrière lui.
        storage.users.create_user("carol")
        with pytest.raises(ValueError, match="déjà pris"):
            storage.users.create_user("carol")
        assert storage.users.get_all_users()[0]["username"] == "carol"

    def test_an_open_transaction_is_discarded_at_release_but_not_by_release_to_pool(self, storage, pg_url):
        """`release_to_pool` ne traite QUE `INERROR` : une connexion « idle in
        transaction » (INTRANS) le traverse sans rollback explicite.

        Ce qui la nettoie malgré tout, c'est `psycopg2.pool.putconn`, qui
        rollback (ou ferme) toute connexion dont le statut n'est pas IDLE. La
        garantie est donc HÉRITÉE de psycopg2, pas implémentée ici — voir
        `test_the_rollback_comes_from_psycopg2_putconn_not_from_the_repository`
        qui épingle cette dépendance. Le travail non commité est perdu et les
        verrous sont bien relâchés : pas de fuite de verrou observable.
        """
        conn = storage._get_conn()
        pid = conn.get_backend_pid()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (username) VALUES ('fantome')")
        assert conn.get_transaction_status() == INTRANS

        storage.release_to_pool(conn)

        observer = _observer(pg_url)
        try:
            assert conn.get_transaction_status() == IDLE
            assert _scalar(observer, "SELECT COUNT(*) FROM users WHERE username = 'fantome'") == 0
            locks_held = _scalar(
                observer,
                "SELECT COUNT(*) FROM pg_locks WHERE pid = %s AND locktype = 'relation' "
                "AND relation = 'users'::regclass",
                (pid,),
            )
            assert locks_held == 0
        finally:
            observer.close()

    def test_the_rollback_comes_from_psycopg2_putconn_not_from_the_repository(self, pg_url):
        """Épingle la dépendance décrite juste au-dessus, directement sur
        psycopg2 : `putconn` rollback toute connexion non-IDLE. Si une montée
        de version retirait ce comportement, `release_to_pool` laisserait
        repartir des connexions en transaction ouverte — et c'est ce test-ci
        qui tomberait, pas un test de repo obscur trois semaines plus tard.
        """
        pool = psycopg2.pool.ThreadedConnectionPool(1, 20, dsn=pg_url, connect_timeout=10)
        try:
            conn = pool.getconn()
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            assert conn.get_transaction_status() == INTRANS

            pool.putconn(conn)

            assert conn.get_transaction_status() == IDLE
        finally:
            pool.closeall()

    def test_releasing_none_is_a_no_op(self, storage):
        storage.release_to_pool(None)  # ne doit pas lever

    def test_a_connection_unknown_to_the_pool_is_hard_closed_instead(self, storage):
        """Une connexion hors pool (celle de `_get_ddl_conn`) rendue par
        mégarde : `putconn` refuse une clé inconnue, le repli la ferme au lieu
        de laisser filer un socket."""
        stray = storage._get_ddl_conn()

        storage.release_to_pool(stray)

        assert stray.closed


# ---------------------------------------------------------------------------
# _get_conn_for_request
# ---------------------------------------------------------------------------

class TestConnForRequest:
    def test_outside_flask_it_borrows_an_ordinary_pooled_connection(self, storage):
        """Hors contexte Flask, `from flask import g` réussit mais `hasattr`
        lève : le `except Exception` retombe sur un emprunt normal. C'est ce
        qui permet au scheduler et aux scripts d'utiliser les mêmes repos."""
        conn = storage._get_conn_for_request()
        try:
            assert _scalar(conn, "SELECT 1") == 1
            assert _scalar(conn, "SHOW statement_timeout") == "30s"  # passé par _get_conn
        finally:
            storage._release_conn(conn)

    def test_inside_a_request_the_shared_connection_is_reused_and_not_released(self, storage):
        """Sous Flask, tous les repos d'une requête partagent UNE connexion,
        et `_release_conn` ne la rend pas au pool — c'est `teardown_request`
        qui s'en charge. Sinon la connexion serait rendue au milieu de la
        requête et un autre thread pourrait l'emprunter."""
        app = Flask(__name__)
        shared = storage._get_conn()
        try:
            with app.app_context():
                g._db_conn = shared

                assert storage._get_conn_for_request() is shared
                assert storage.searches._get_conn_for_request() is shared
                assert storage.listings._get_conn_for_request() is shared

                storage._release_conn(shared)
                assert not shared.closed
                assert _scalar(shared, "SELECT 1") == 1  # toujours utilisable
                assert shared not in storage.users._get_pool()._pool
        finally:
            storage.release_to_pool(shared)


# ---------------------------------------------------------------------------
# Saturation — borné, jamais bloquant
# ---------------------------------------------------------------------------

class TestPoolExhaustion:
    def test_the_twenty_first_simultaneous_borrow_raises_instead_of_blocking(self, storage):
        """`ThreadedConnectionPool(1, 20)` ne fait PAS attendre : au-delà de
        20 emprunts simultanés il lève `PoolError`. C'est ce qui rend le test
        borné — aucun risque de suspendre la suite — mais c'est aussi ce que
        verrait la 21ᵉ requête concurrente en production : une erreur 500
        immédiate, pas une file d'attente.
        """
        borrowed = []
        try:
            for _ in range(20):
                borrowed.append(storage._get_conn())
            assert len(borrowed) == 20

            with pytest.raises(psycopg2.pool.PoolError, match="exhausted"):
                storage._get_conn()
        finally:
            for conn in borrowed:
                storage.release_to_pool(conn)

        # Le pool est de nouveau utilisable après libération.
        conn = storage._get_conn()
        try:
            assert _scalar(conn, "SELECT 1") == 1
        finally:
            storage._release_conn(conn)

    def test_only_one_idle_connection_is_ever_kept(self, storage):
        """# BUG : `minconn=1` fait plus que fixer un plancher — `putconn` ne
        remet une connexion dans le pool que si `len(pool) < minconn`, donc le
        pool ne conserve JAMAIS plus d'une connexion inactive : toutes les
        autres sont fermées pour de bon.

        Autrement dit ce « pool de 20 » ne recycle qu'une seule connexion ;
        dès qu'il y a deux requêtes en vol, chacune rouvre une connexion TCP
        (poignée de main + authentification) à chaque emprunt. Le remède est
        `minconn=maxconn`, côté production — hors périmètre ici.
        """
        first, second = storage._get_conn(), storage._get_conn()
        pool = storage.users._get_pool()

        storage.release_to_pool(first)
        assert not first.closed
        assert len(pool._pool) == 1

        storage.release_to_pool(second)
        assert second.closed, "la 2e connexion rendue est fermée, pas mise en réserve"
        assert len(pool._pool) == 1
