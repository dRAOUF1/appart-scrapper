"""Le verrou consultatif du scheduler (main.py) contre un vrai Postgres.

`_try_acquire_scheduler_lock` sert à garantir qu'un seul process fait tourner le
scheduler, même si le déploiement passe à plusieurs workers gunicorn : sinon
chaque worker soumet ses propres scrapes et l'utilisateur reçoit chaque annonce
en double (la dédup de `_scrape_futures` est en mémoire, donc par process).

Un test unitaire de cette fonction ne peut que vérifier qu'elle appelle
`pg_try_advisory_lock` — il ne prouve pas la *contention*, qui est tout l'objet
du verrou. Il faut deux vraies connexions au même serveur.

Le verrou est de portée SESSION, pas transaction : il survit aux commits et
n'est relâché qu'à la fermeture de la connexion. Chaque test le libère donc dans
un `finally`, sinon il fuiterait sur toute la suite.
"""

from __future__ import annotations

import psycopg2
import pytest

from main import _SCHEDULER_LOCK_KEY, _try_acquire_scheduler_lock


def _advisory_lock_holders(pg_url) -> int:
    """Nombre de sessions détenant le verrou du scheduler, vu du serveur.

    Une clé sur 64 bits est décomposée par Postgres en (classid, objid) ; la
    clé du scheduler tient sur 32 bits, donc classid vaut 0.
    """
    observer = psycopg2.connect(pg_url, connect_timeout=10)
    observer.autocommit = True
    try:
        with observer.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND classid = 0 AND objid = %s AND granted",
                (_SCHEDULER_LOCK_KEY,),
            )
            return cur.fetchone()[0]
    finally:
        observer.close()


def _terminate(pg_url, pid: int) -> None:
    """Coupe une connexion côté serveur — ce que fait un redémarrage de
    Postgres, un timeout d'idle ou une coupure réseau en production."""
    observer = psycopg2.connect(pg_url, connect_timeout=10)
    observer.autocommit = True
    try:
        with observer.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
    finally:
        observer.close()


@pytest.fixture
def acquire(pg_url):
    """Acquiert des verrous et garantit leur libération, même en cas d'échec."""
    held: list = []

    def _acquire():
        conn = _try_acquire_scheduler_lock(pg_url)
        if conn is not None:
            held.append(conn)
        return conn

    yield _acquire

    for conn in held:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Contention
# ---------------------------------------------------------------------------

class TestContention:
    def test_the_first_caller_gets_the_lock(self, acquire, pg_url):
        conn = acquire()

        assert conn is not None
        assert conn.autocommit is True, "le verrou doit être posé hors transaction"
        assert _advisory_lock_holders(pg_url) == 1

    def test_a_second_connection_cannot_acquire_it_while_the_first_holds_it(self, acquire, pg_url):
        """🔒 Le test qui compte : deux appels concurrents, un seul gagnant.
        `pg_try_advisory_lock` rend FALSE immédiatement au lieu d'attendre —
        c'est ce qui permet au second worker de démarrer quand même son serveur
        web, sans scheduler et sans blocage."""
        first = acquire()
        assert first is not None

        second = acquire()

        assert second is None
        assert _advisory_lock_holders(pg_url) == 1, "une seule session détient le verrou"

    def test_the_loser_closes_its_connection_instead_of_leaking_it(self, acquire, pg_url):
        """Le chemin d'échec ferme la connexion avant de rendre None. Sans ça,
        chaque worker perdant laisserait un socket et un backend Postgres
        ouverts pour la vie du process."""
        first = acquire()
        assert first is not None
        before = _advisory_lock_holders(pg_url)

        assert acquire() is None

        # Le backend du perdant a disparu : il ne reste que le détenteur.
        assert _advisory_lock_holders(pg_url) == before == 1

    def test_a_third_caller_is_refused_too(self, acquire):
        assert acquire() is not None

        assert acquire() is None
        assert acquire() is None


# ---------------------------------------------------------------------------
# Libération
# ---------------------------------------------------------------------------

class TestRelease:
    def test_closing_the_connection_releases_the_lock(self, acquire, pg_url):
        """Portée SESSION : rien n'est jamais appelé pour déverrouiller, c'est
        la fermeture de la connexion qui relâche. D'où l'attribut
        `app._scheduler_lock_conn`, qui n'existe que pour empêcher le ramasse-
        miettes de la fermer."""
        first = acquire()
        first.close()

        assert _advisory_lock_holders(pg_url) == 0

        second = acquire()
        assert second is not None

    def test_a_commit_does_not_release_it(self, acquire, pg_url):
        """Un verrou consultatif de session traverse les transactions : à
        distinguer de `pg_advisory_xact_lock`, qui serait relâché au premier
        commit et laisserait un second worker démarrer son scheduler."""
        conn = acquire()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.commit()

        assert _advisory_lock_holders(pg_url) == 1
        assert acquire() is None

    def test_reacquiring_after_release_works_repeatedly(self, acquire, pg_url):
        """Un redéploiement enchaîne arrêt et démarrage : le verrou doit être
        reprenable, pas consommé une fois pour toutes."""
        for _ in range(3):
            conn = acquire()
            assert conn is not None
            conn.close()
            assert _advisory_lock_holders(pg_url) == 0


# ---------------------------------------------------------------------------
# Robustesse
# ---------------------------------------------------------------------------

class TestFailureModes:
    def test_an_unreachable_database_returns_none_instead_of_raising(self, pg_url):
        """`_start_background_tasks` interprète `None` comme « quelqu'un d'autre
        a le verrou » et se contente de ne pas démarrer le scheduler. Une
        exception, elle, ferait échouer `create_app()` : l'application entière
        refuserait de démarrer parce que le *scheduler* n'a pas pu se
        verrouiller.

        # BUG (confusion de deux cas) : la fonction rend `None` aussi bien pour
        « verrou déjà détenu » que pour « base injoignable ». Le message de log
        affiché ensuite (« verrou déjà détenu ailleurs ») est donc trompeur dans
        le second cas, et un déploiement mono-worker dont la base est en panne
        au démarrage tourne sans scheduler et sans alerte explicite.
        """
        assert _try_acquire_scheduler_lock("postgresql://nobody@localhost:1/absente") is None

    def test_a_terminated_lock_connection_silently_frees_the_lock(self, acquire, pg_url):
        """# BUG : la connexion du verrou est gardée dans
        `app._scheduler_lock_conn` et plus jamais vérifiée — aucun health-check,
        aucune reprise. Si elle tombe (redémarrage de Postgres, coupure réseau,
        `idle_session_timeout`), deux choses se produisent en même temps :

          1. le verrou est relâché côté serveur, donc n'importe quel autre
             worker peut l'acquérir et démarrer SON scheduler ;
          2. ce process-ci ne s'en aperçoit pas et continue de scraper.

        Résultat : la garantie « un seul scheduler » disparaît en silence et les
        notifications repartent en double — exactement ce que le verrou est censé
        empêcher. Comportement ACTUEL figé ici, non corrigé.
        """
        victim = acquire()
        assert _advisory_lock_holders(pg_url) == 1

        _terminate(pg_url, victim.get_backend_pid())

        # 1. Le verrou est libre, un « second worker » l'obtient.
        assert _advisory_lock_holders(pg_url) == 0
        usurper = acquire()
        assert usurper is not None

        # 2. Rien n'a signalé la perte : la connexion gardée est morte, mais
        #    seule une requête le révélerait — et le scheduler n'en fait aucune
        #    sur cette connexion.
        with pytest.raises(psycopg2.OperationalError):
            with victim.cursor() as cur:
                cur.execute("SELECT 1")
