"""Tests unitaires du scheduler et des filtres Jinja de `main.py`.

Périmètre : ce qui tourne **hors requête HTTP**. Le câblage Flask (blueprints,
CSRF, before/teardown_request) est couvert par tests/functional/.

  * `_try_acquire_scheduler_lock` : le verrou consultatif Postgres qui garantit
    qu'un seul process planifie, quel que soit le nombre de workers gunicorn ;
  * `scheduled_scrape_job` : le filtre « cette recherche est-elle due ? »,
    exécuté toutes les 30 secondes sur toutes les recherches de tous les
    utilisateurs ;
  * les filtres `parse_iso_date` / `fr_time`, définis dans `create_app()` et
    donc seulement atteignables via `app.jinja_env.filters` ;
  * le fait qu'**importer `main` ne charge pas `.env`** — l'incident de fuite
    de la base de production est arrivé deux fois.

⚠️ Sur les tests de filtrage : l'ancienne suite avait six tests dont la seule
assertion était `mock_submit.assert_not_called()`. Ils passaient à l'identique
si la closure explosait dès sa première ligne. Chaque test de filtrage ci-
dessous affirme donc AUSSI un effet positif (`get_all_users` /
`get_user_searches` bien appelés), pour distinguer « filtré » de « planté ».

Remplace tests/_legacy/test_scheduler_job.py et test_scheduler_lock.py.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time
from loguru import logger

import main
from tests.helpers.factories import make_criteria, make_search_row, make_user_row
from tests.helpers.fakes import fake_notifier, fake_storage

FROZEN = "2026-07-26 12:00:00"
FROZEN_NAIVE_UTC = datetime(2026, 7, 26, 12, 0, 0)
REPO_ROOT = Path(main.__file__).resolve().parent


# ---------------------------------------------------------------------------
# _try_acquire_scheduler_lock
# ---------------------------------------------------------------------------

class TestSchedulerLock:
    """psycopg2 est coupé par le socle (tests/conftest.py) : `connect` est
    doublé explicitement, ce qui garantit qu'aucune vraie base n'est jointe."""

    @staticmethod
    def fake_connection(acquired: bool):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (acquired,)
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    def test_the_connection_is_returned_and_kept_open_when_the_lock_is_free(self, monkeypatch):
        """La connexion DOIT rester ouverte : le verrou consultatif est lié à
        la session Postgres, le fermer le libérerait aussitôt."""
        conn, cursor = self.fake_connection(acquired=True)
        monkeypatch.setattr("psycopg2.connect", lambda *a, **kw: conn)

        result = main._try_acquire_scheduler_lock("postgresql://u@h/db")

        assert result is conn
        conn.close.assert_not_called()
        assert conn.autocommit is True, "sans autocommit, le verrou serait pris dans une transaction ouverte"
        cursor.execute.assert_called_once_with(
            "SELECT pg_try_advisory_lock(%s)", (main._SCHEDULER_LOCK_KEY,)
        )

    def test_the_connect_timeout_is_bounded(self, monkeypatch):
        """Un démarrage ne doit pas pendre indéfiniment sur une base
        injoignable : `create_app` appelle ce code de façon synchrone."""
        captured: list[tuple] = []
        conn, _ = self.fake_connection(acquired=True)

        def capture(*args, **kwargs):
            captured.append((args, kwargs))
            return conn

        monkeypatch.setattr("psycopg2.connect", capture)

        main._try_acquire_scheduler_lock("postgresql://u@h/db")

        assert captured == [(("postgresql://u@h/db",), {"connect_timeout": 10})]

    def test_a_lock_held_elsewhere_gives_none_and_closes_the_connection(self, monkeypatch):
        """Sans le `close()`, chaque worker gunicorn garderait une connexion
        inutile ouverte pour la vie du process."""
        conn, _ = self.fake_connection(acquired=False)
        monkeypatch.setattr("psycopg2.connect", lambda *a, **kw: conn)

        assert main._try_acquire_scheduler_lock("postgresql://u@h/db") is None

        conn.close.assert_called_once_with()

    @pytest.mark.parametrize(
        ("error", "case"),
        [
            (OSError("could not connect to server"), "base injoignable"),
            (RuntimeError("boom"), "erreur inattendue"),
        ],
        ids=["unreachable", "unexpected"],
    )
    def test_any_failure_degrades_to_no_scheduler_instead_of_crashing(
        self, monkeypatch, error, case
    ):
        """Le démarrage de l'app ne doit pas échouer parce que le verrou n'a pas
        pu être pris : l'app sert alors le web sans planifier de scrape."""
        messages: list[str] = []
        sink_id = logger.add(lambda msg: messages.append(msg.record["message"]), level="DEBUG")
        try:
            monkeypatch.setattr("psycopg2.connect", MagicMock(side_effect=error))

            assert main._try_acquire_scheduler_lock("postgresql://u@h/db") is None, case

            assert any("Impossible d'acquérir le verrou du scheduler" in m for m in messages)
        finally:
            logger.remove(sink_id)

    def test_a_failure_while_querying_the_lock_is_also_caught(self, monkeypatch):
        """L'exception peut survenir APRÈS `connect` (curseur, réseau coupé) :
        le `try` englobe toute la séquence."""
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("connection closed")
        monkeypatch.setattr("psycopg2.connect", lambda *a, **kw: conn)

        assert main._try_acquire_scheduler_lock("postgresql://u@h/db") is None

    def test_the_lock_key_is_stable(self):
        """La clé est arbitraire mais doit être IDENTIQUE dans tous les
        process : la changer réactiverait plusieurs schedulers en parallèle
        pendant un déploiement progressif."""
        assert main._SCHEDULER_LOCK_KEY == 727271


# ---------------------------------------------------------------------------
# _source_has_valid_criteria
# ---------------------------------------------------------------------------

class TestSourceHasValidCriteria:
    @pytest.mark.parametrize(
        ("parser_answer", "expected"),
        [(True, True), (False, False)],
        ids=["parser_accepts", "parser_refuses"],
    )
    def test_the_decision_is_delegated_to_the_source_parser(
        self, monkeypatch, parser_answer, expected
    ):
        """Chaque source encode la localisation à sa façon (placeIds SeLoger vs
        codes INSEE Laforêt) : il n'y a aucune clé unique à vérifier ici."""
        seen: list[dict] = []
        parser = MagicMock()
        parser.has_valid_criteria.side_effect = lambda criteria: seen.append(criteria) or parser_answer
        monkeypatch.setattr("parsers.get_parser", lambda source, storage=None: parser)
        criteria = make_criteria()

        assert main._source_has_valid_criteria("seloger", criteria) is expected
        assert seen == [criteria]

    def test_an_unknown_source_is_false_not_an_exception(self, monkeypatch):
        """Une source inconnue peut venir de la base (colonne `sources`
        éditable, source retirée du code) : elle doit désactiver la recherche,
        pas tuer le cycle du scheduler."""
        def refuse(source, storage=None):
            raise ValueError(f"Source '{source}' inconnue. Sources disponibles : seloger")

        monkeypatch.setattr("parsers.get_parser", refuse)

        assert main._source_has_valid_criteria("nawak", make_criteria()) is False

    @pytest.mark.parametrize(
        ("criteria", "expected"),
        [
            (make_criteria(), True),
            ({"priceMax": 1000}, False),
            ({}, False),
        ],
        ids=["with_location", "without_location", "empty"],
    )
    def test_against_the_real_registry(self, criteria, expected):
        """Sans double : le vrai parser SeLoger exige au moins une localisation
        exploitable."""
        assert main._source_has_valid_criteria("seloger", criteria) is expected


# ---------------------------------------------------------------------------
# scheduled_scrape_job
# ---------------------------------------------------------------------------

class SchedulerHarness:
    """Monte le scheduler avec ses dépendances doublées et expose le job.

    La closure `scheduled_scrape_job` n'est pas importable : elle est capturée
    via l'appel à `add_job` du scheduler doublé, exactement comme le faisait
    l'ancienne suite.
    """

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.storage = fake_storage()
        self.app = MagicMock()
        self.app.storage = self.storage
        self.app.notifier = fake_notifier()
        self.submitted: list[tuple[int, int]] = []
        self.scheduler = MagicMock()
        self.lock_conn = MagicMock()

        monkeypatch.setattr(
            "apscheduler.schedulers.background.BackgroundScheduler",
            lambda **kwargs: self.scheduler,
        )
        monkeypatch.setattr(
            "core.scrape_control.submit_scrape",
            lambda app, search_id, user_id: self.submitted.append((search_id, user_id)),
        )
        monkeypatch.setattr(main, "_try_acquire_scheduler_lock", lambda url: self.lock_conn)

    def with_searches(self, *searches, user_id: int = 1):
        self.storage.users.get_all_users.return_value = [make_user_row(id=user_id)]
        self.storage.searches.get_user_searches.return_value = list(searches)
        return self

    def with_users_and_searches(self, mapping: dict[int, list[dict]]):
        self.storage.users.get_all_users.return_value = [make_user_row(id=uid) for uid in mapping]
        self.storage.searches.get_user_searches.side_effect = lambda uid: mapping[uid]
        return self

    def with_parsers(self, table: dict):
        def get_parser(source, storage=None):
            if source not in table:
                raise ValueError(f"Source '{source}' inconnue. Sources disponibles : seloger")
            return table[source]

        self.monkeypatch.setattr("parsers.get_parser", get_parser)
        return self

    @property
    def job(self):
        main._start_background_tasks(self.app)
        return self.scheduler.add_job.call_args.args[0]

    def run(self):
        self.job()
        return self.submitted

    def assert_the_loop_actually_ran(self, user_id: int = 1):
        """Le complément indispensable d'un `assert submitted == []` : prouve
        que la closure a bien atteint la boucle de filtrage."""
        self.storage.users.get_all_users.assert_called_once_with()
        self.storage.searches.get_user_searches.assert_called_once_with(user_id)


@pytest.fixture
def harness(monkeypatch):
    return SchedulerHarness(monkeypatch)


def accepting_parser():
    parser = MagicMock()
    parser.has_valid_criteria.return_value = True
    return parser


def refusing_parser():
    parser = MagicMock()
    parser.has_valid_criteria.return_value = False
    return parser


class TestSchedulerStartup:
    def test_the_job_is_registered_every_thirty_seconds_without_overlap(self, harness):
        job = harness.job

        _, kwargs = harness.scheduler.add_job.call_args
        assert harness.scheduler.add_job.call_args.args[1] == "interval"
        assert kwargs == {"seconds": 30, "id": "scrape_scheduler", "max_instances": 1}
        assert callable(job)
        harness.scheduler.start.assert_called_once_with()

    def test_the_lock_connection_is_parked_on_the_app_to_stay_alive(self, harness):
        assert callable(harness.job)  # monte le scheduler

        assert harness.app._scheduler_lock_conn is harness.lock_conn, (
            "sans référence vivante, le ramasse-miettes fermerait la connexion "
            "et libérerait le verrou"
        )

    def test_the_lock_is_taken_on_the_configured_database(self, monkeypatch):
        asked: list[str] = []
        app = MagicMock()
        app.storage = fake_storage()
        monkeypatch.setattr("apscheduler.schedulers.background.BackgroundScheduler", MagicMock())
        monkeypatch.setattr(main, "_try_acquire_scheduler_lock", lambda url: asked.append(url) or None)

        main._start_background_tasks(app)

        assert asked == ["postgresql://fake/fake"]

    def test_nothing_starts_when_the_lock_is_held_by_another_process(self, monkeypatch):
        messages: list[str] = []
        sink_id = logger.add(lambda msg: messages.append(msg.record["message"]), level="DEBUG")
        try:
            scheduler = MagicMock()
            app = MagicMock()
            app.storage = fake_storage()
            monkeypatch.setattr(
                "apscheduler.schedulers.background.BackgroundScheduler", lambda **kw: scheduler
            )
            monkeypatch.setattr(main, "_try_acquire_scheduler_lock", lambda url: None)

            main._start_background_tasks(app)

            scheduler.add_job.assert_not_called()
            scheduler.start.assert_not_called()
            assert any("Scheduler non démarré dans ce process" in m for m in messages)
        finally:
            logger.remove(sink_id)


class TestDueForScrapeFiltering:
    @freeze_time(FROZEN)
    def test_a_never_scraped_active_search_is_submitted(self, harness):
        harness.with_searches(make_search_row(id=11, last_scraped=None))

        assert harness.run() == [(11, 1)]

    @freeze_time(FROZEN)
    def test_the_search_id_and_the_owner_id_are_both_passed_along(self, harness):
        """`submit_scrape` reçoit le propriétaire : c'est ce `user_id` qui sert
        de contrôle d'accès dans `ScrapeService.execute`."""
        harness.with_users_and_searches(
            {
                4: [make_search_row(id=11, user_id=4)],
                9: [make_search_row(id=22, user_id=9)],
            }
        )

        assert harness.run() == [(11, 4), (22, 9)]

    @pytest.mark.parametrize(
        ("is_active", "expected_submitted"),
        [
            (True, True),
            (False, False),
            (None, False),
            (0, False),
        ],
        ids=["active", "inactive", "null", "zero"],
    )
    @freeze_time(FROZEN)
    def test_only_active_searches_are_submitted(self, harness, is_active, expected_submitted):
        harness.with_searches(make_search_row(id=11, is_active=is_active))

        submitted = harness.run()

        assert submitted == ([(11, 1)] if expected_submitted else [])
        harness.assert_the_loop_actually_ran()

    @freeze_time(FROZEN)
    def test_a_row_without_the_is_active_column_defaults_to_active(self, harness):
        """`s.get("is_active", True)` : une ligne d'avant la colonne (ou une
        projection SQL qui ne la sélectionne pas) reste planifiée."""
        row = make_search_row(id=11)
        row.pop("is_active")
        harness.with_searches(row)

        assert harness.run() == [(11, 1)]

    @pytest.mark.parametrize(
        ("criteria", "case"),
        [
            (None, "critères nuls"),
            ({}, "critères vides"),
            ([], "liste vide"),
            (["locations"], "liste non vide"),
            ("locations=Paris", "chaîne"),
            (0, "zéro"),
        ],
        ids=["none", "empty_dict", "empty_list", "list", "string", "zero"],
    )
    @freeze_time(FROZEN)
    def test_criteria_that_are_empty_or_not_a_dict_are_skipped(self, harness, criteria, case):
        """La colonne `criteria` est du JSON : rien ne garantit sa forme côté
        base. Un `isinstance` évite un `AttributeError` dans le parser."""
        harness.with_searches(make_search_row(id=11, criteria=criteria))

        assert harness.run() == [], case
        harness.assert_the_loop_actually_ran()

    @freeze_time(FROZEN)
    def test_a_search_no_source_can_run_is_skipped(self, harness):
        harness.with_searches(make_search_row(id=11, sources=["seloger", "laforet"]))
        harness.with_parsers({"seloger": refusing_parser(), "laforet": refusing_parser()})

        assert harness.run() == []
        harness.assert_the_loop_actually_ran()

    @freeze_time(FROZEN)
    def test_a_single_runnable_source_is_enough(self, harness):
        """`any(...)` : une source utilisable suffit à planifier le scrape, les
        autres échoueront individuellement dans `ScrapeService`."""
        harness.with_searches(make_search_row(id=11, sources=["laforet", "seloger"]))
        harness.with_parsers({"seloger": accepting_parser(), "laforet": refusing_parser()})

        assert harness.run() == [(11, 1)]

    @freeze_time(FROZEN)
    def test_an_unknown_source_alone_disables_the_search(self, harness):
        harness.with_searches(make_search_row(id=11, sources=["source_supprimée"]))
        harness.with_parsers({"seloger": accepting_parser()})

        assert harness.run() == []
        harness.assert_the_loop_actually_ran()

    @pytest.mark.parametrize(
        ("row_overrides", "expected_sources"),
        [
            ({"sources": ["seloger", "laforet"]}, ["seloger", "laforet"]),
            ({"sources": [], "source": "laforet"}, ["laforet"]),
            ({"sources": None, "source": "laforet"}, ["laforet"]),
        ],
        ids=["explicit", "empty_falls_back", "null_falls_back"],
    )
    @freeze_time(FROZEN)
    def test_sources_fall_back_to_the_legacy_single_source_column(
        self, harness, row_overrides, expected_sources
    ):
        asked: list[str] = []
        harness.with_searches(make_search_row(id=11, **row_overrides))
        harness.monkeypatch.setattr(
            "parsers.get_parser",
            lambda source, storage=None: asked.append(source) or refusing_parser(),
        )

        harness.run()

        assert asked == expected_sources

    @freeze_time(FROZEN)
    def test_a_row_with_neither_sources_nor_source_falls_back_to_seloger(self, harness):
        """Dernier repli en dur : `[s.get("source", "seloger")]`."""
        asked: list[str] = []
        row = make_search_row(id=11)
        row.pop("sources")
        row.pop("source")
        harness.with_searches(row)
        harness.monkeypatch.setattr(
            "parsers.get_parser",
            lambda source, storage=None: asked.append(source) or refusing_parser(),
        )

        harness.run()

        assert asked == ["seloger"]


class TestScrapeInterval:
    @pytest.mark.parametrize(
        ("minutes_ago", "interval", "expected_submitted"),
        [
            (1, 5, False),
            (4, 5, False),
            (5, 5, True),
            (6, 5, True),
            (0, 5, False),
            (61, 60, True),
            (59, 60, False),
        ],
        ids=["1_of_5", "4_of_5", "exactly_5", "6_of_5", "just_now", "61_of_60", "59_of_60"],
    )
    @freeze_time(FROZEN)
    def test_the_interval_is_respected_to_the_minute(
        self, harness, minutes_ago, interval, expected_submitted
    ):
        """La comparaison est `last_scraped > now - interval` : à l'échéance
        EXACTE, la recherche repart (borne non stricte du côté du scrape)."""
        last = FROZEN_NAIVE_UTC - timedelta(minutes=minutes_ago)
        harness.with_searches(make_search_row(id=11, scrape_interval=interval, last_scraped=last))

        submitted = harness.run()

        assert submitted == ([(11, 1)] if expected_submitted else [])
        harness.assert_the_loop_actually_ran()

    @freeze_time(FROZEN)
    def test_the_default_interval_is_five_minutes(self, harness):
        """`s.get("scrape_interval", 5)` : une ligne sans la colonne suit le
        même rythme que le défaut de l'UI."""
        row = make_search_row(id=11, last_scraped=FROZEN_NAIVE_UTC - timedelta(minutes=4))
        row.pop("scrape_interval")
        harness.with_searches(row)

        assert harness.run() == []

        row["last_scraped"] = FROZEN_NAIVE_UTC - timedelta(minutes=6)
        harness.submitted.clear()
        assert harness.run() == [(11, 1)]

    @pytest.mark.parametrize(
        ("last_scraped", "expected_submitted"),
        [
            ("2026-07-26T11:59:00", False),
            ("2026-07-26T11:00:00", True),
            ("2026-07-26T11:59:00.123456", False),
        ],
        ids=["recent_iso", "old_iso", "iso_with_microseconds"],
    )
    @freeze_time(FROZEN)
    def test_an_iso_string_last_scraped_is_parsed(self, harness, last_scraped, expected_submitted):
        """La colonne remonte parfois en chaîne (JSON, driver, cache) : le job
        la reparse plutôt que de la comparer telle quelle."""
        harness.with_searches(make_search_row(id=11, last_scraped=last_scraped))

        assert harness.run() == ([(11, 1)] if expected_submitted else [])

    @freeze_time(FROZEN)
    def test_a_future_last_scraped_postpones_the_search(self, harness):
        """Horloge de la base en avance : la recherche est simplement différée,
        sans erreur."""
        harness.with_searches(
            make_search_row(id=11, last_scraped=FROZEN_NAIVE_UTC + timedelta(hours=1))
        )

        assert harness.run() == []
        harness.assert_the_loop_actually_ran()

    @pytest.mark.parametrize(
        ("delta", "expected_submitted"),
        [
            (timedelta(minutes=5, seconds=-1), False),
            (timedelta(minutes=5), True),
        ],
        ids=["one_second_early", "exactly_on_time"],
    )
    @freeze_time(FROZEN)
    def test_the_reference_instant_is_naive_utc_to_the_second(
        self, harness, delta, expected_submitted
    ):
        """`datetime.now(UTC).replace(tzinfo=None)` : de l'UTC **naïf**, pour se
        comparer aux colonnes TIMESTAMP que Postgres remplit avec
        `CURRENT_TIMESTAMP` (la session base doit être en UTC pour que ce soit
        correct). Un `datetime.now()` local avancerait le `now` du job du
        décalage horaire de la machine, et toutes les recherches repartiraient
        à chaque cycle de 30 secondes.

        La borne est ici vérifiée à la seconde près contre un `utcnow()` figé —
        c'est la précision maximale atteignable en test, freezegun rendant
        `now()` et `now(UTC)` indiscernables (voir aussi
        `test_the_scheduler_reads_the_clock_in_utc`).
        """
        harness.with_searches(
            make_search_row(id=11, scrape_interval=5, last_scraped=datetime.utcnow() - delta)
        )

        submitted = harness.run()

        assert submitted == ([(11, 1)] if expected_submitted else [])
        harness.assert_the_loop_actually_ran()

    def test_the_scheduler_reads_the_clock_in_utc(self):
        """Vérification statique du choix de fuseau, qu'aucune assertion
        dynamique ne peut couvrir (cf. ci-dessus).

        Un passage à `datetime.now()` serait invisible pour les tests et pour
        un développeur en UTC, mais planifierait un scrape par cycle sur toute
        machine décalée — exactement le genre de régression que ce test rend
        rouge.
        """
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")

        assert "now = datetime.now(UTC).replace(tzinfo=None)" in source
        assert "now = datetime.now()" not in source


class TestSchedulerErrorHandling:
    @freeze_time(FROZEN)
    def test_a_failing_storage_call_is_logged_and_does_not_kill_the_scheduler(self, harness):
        """`except Exception` global : APScheduler retirerait le job si la
        closure levait de façon répétée. Le cycle suivant retentera."""
        messages: list[str] = []
        sink_id = logger.add(lambda msg: messages.append(msg.record["message"]), level="DEBUG")
        try:
            harness.storage.users.get_all_users.side_effect = RuntimeError("pool épuisé")

            harness.run()  # ne doit pas lever

            assert any("Erreur scheduled_scrape_job: pool épuisé" in m for m in messages)
        finally:
            logger.remove(sink_id)

    @freeze_time(FROZEN)
    def test_a_malformed_iso_date_aborts_the_whole_cycle(self, harness):
        """# BUG (borné) : une seule ligne corrompue neutralise le cycle entier.

        `datetime.fromisoformat` (main.py:240) lève sur une valeur illisible, et
        le seul `try` est celui qui entoure TOUTE la boucle (main.py:216-248).
        Les recherches suivantes — celles des autres utilisateurs comprises —
        ne sont donc pas examinées pendant ce cycle.

        En pratique le cycle revient 30 s plus tard et échouera pareil : tant
        que la ligne fautive est là, aucune recherche située après elle n'est
        plus jamais planifiée. Un `try` par recherche isolerait la panne.
        """
        harness.with_searches(
            make_search_row(id=11, last_scraped="pas-une-date"),
            make_search_row(id=12, last_scraped=None),
        )

        assert harness.run() == [], "la recherche 12, parfaitement valide, n'est jamais atteinte"
        harness.assert_the_loop_actually_ran()

    @freeze_time(FROZEN)
    def test_a_submit_failure_also_stops_the_remaining_searches(self, harness):
        """Même conséquence pour une exception venue de `submit_scrape` : les
        recherches suivantes du cycle sont perdues."""
        harness.with_searches(
            make_search_row(id=11, last_scraped=None),
            make_search_row(id=12, last_scraped=None),
        )
        harness.monkeypatch.setattr(
            "core.scrape_control.submit_scrape",
            MagicMock(side_effect=RuntimeError("executor arrêté")),
        )

        harness.run()  # avalé par le except global

        harness.assert_the_loop_actually_ran()

    @freeze_time(FROZEN)
    def test_the_job_runs_inside_an_application_context(self, harness):
        """`submit_scrape` construit un `ScrapeService` depuis `app.storage` et
        les routes attendues plus bas dans la pile utilisent `current_app` : le
        contexte est ouvert pour tout le cycle."""
        harness.with_searches(make_search_row(id=11))

        harness.run()

        harness.app.app_context.assert_called_once_with()

    @freeze_time(FROZEN)
    def test_no_users_means_no_search_lookup(self, harness):
        harness.storage.users.get_all_users.return_value = []

        assert harness.run() == []

        harness.storage.users.get_all_users.assert_called_once_with()
        harness.storage.searches.get_user_searches.assert_not_called()


# ---------------------------------------------------------------------------
# Filtres Jinja (définis dans create_app)
# ---------------------------------------------------------------------------

@pytest.fixture
def jinja_filters(monkeypatch):
    """Les filtres Jinja réels, extraits d'une app montée comme en production.

    Ils sont définis DANS `create_app()` (décorateurs `@app.template_filter`) :
    il n'y a pas d'autre façon de les atteindre que de construire l'app. Les
    dépendances lourdes sont doublées, comme dans tests/functional/conftest.py —
    dont `load_dotenv`, pour que le `.env` de production n'entre jamais dans
    l'environnement du test.
    """
    monkeypatch.setattr(main, "load_dotenv", lambda *a, **kw: False)
    monkeypatch.setattr(main, "Storage", lambda database_url: fake_storage())
    monkeypatch.setattr(main, "Notifier", lambda **kwargs: fake_notifier())
    monkeypatch.setattr(main, "_start_background_tasks", lambda app: None)
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")

    return main.create_app().jinja_env.filters


PARIS = ZoneInfo("Europe/Paris")


class TestParseIsoDateFilter:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2026-07-26T12:00:00", datetime(2026, 7, 26, 12, 0)),
            (
                "2026-07-26T12:00:00Z",
                datetime(2026, 7, 26, 12, 0, tzinfo=ZoneInfo("UTC")),
            ),
            (
                "2026-07-26T12:00:00+02:00",
                datetime(2026, 7, 26, 12, 0, tzinfo=PARIS),
            ),
            ("2026-07-26", datetime(2026, 7, 26, 0, 0)),
            ("2026-07-26T12:00:00.123456", datetime(2026, 7, 26, 12, 0, 0, 123456)),
        ],
        ids=["naive", "zulu", "offset", "date_only", "microseconds"],
    )
    def test_iso_strings_are_parsed_including_the_zulu_suffix(self, jinja_filters, value, expected):
        """Le suffixe « Z » n'est accepté par `fromisoformat` qu'à partir de
        Python 3.11 ; le `replace("Z", "+00:00")` explicite garde le filtre
        indépendant de la version, et c'est la forme que rendent les API JSON."""
        assert jinja_filters["parse_iso_date"](value) == expected

    @pytest.mark.parametrize(
        ("value", "case"),
        [
            (None, "None"),
            ("", "chaîne vide"),
            (0, "zéro (falsy)"),
            ([], "liste vide"),
        ],
        ids=["none", "empty", "zero", "empty_list"],
    )
    def test_falsy_values_give_none_without_parsing(self, jinja_filters, value, case):
        """Un template affiche souvent une colonne nullable : le filtre doit
        rendre None, jamais lever."""
        assert jinja_filters["parse_iso_date"](value) is None, case

    @pytest.mark.parametrize(
        ("value", "case"),
        [
            ("pas une date", "texte libre"),
            ("2026-13-45", "date impossible"),
            ("26/07/2026", "format français"),
            (12345, "entier : AttributeError sur .replace"),
            (["2026-07-26"], "liste : AttributeError"),
            ({"date": "2026-07-26"}, "dict : AttributeError"),
        ],
        ids=["text", "impossible", "french_format", "int", "list", "dict"],
    )
    def test_anything_unparsable_gives_none(self, jinja_filters, value, case):
        """`except (ValueError, AttributeError)` couvre les deux familles : date
        illisible (ValueError) et valeur qui n'est pas une chaîne
        (AttributeError sur `.replace`)."""
        assert jinja_filters["parse_iso_date"](value) is None, case

    @pytest.mark.parametrize(
        "value",
        [datetime(2026, 7, 26, 12, 0), date(2026, 7, 26)],
        ids=["datetime", "date"],
    )
    def test_a_real_date_object_raises_typeerror_instead_of_being_passed_through(
        self, jinja_filters, value
    ):
        """# BUG : le filtre n'est pas idempotent et lève sur un `date`/`datetime`.

        main.py:103 fait `value.replace("Z", "+00:00")`. Sur un objet date,
        `.replace()` existe mais attend des composantes numériques : Python lève
        `TypeError: 'str' object cannot be interpreted as an integer`, et le
        `except (ValueError, AttributeError)` de la ligne 105 ne l'attrape PAS.

        Conséquence : appliquer `| parse_iso_date` à une colonne déjà typée
        `datetime` (ce que rendent la plupart des repositories) fait planter le
        rendu du template en 500, au lieu de laisser passer la valeur. Ajouter
        `TypeError` à la clause — ou rendre `value` tel quel s'il est déjà un
        `datetime` — corrigerait les deux aspects.
        """
        with pytest.raises(TypeError, match="cannot be interpreted as an integer"):
            jinja_filters["parse_iso_date"](value)


class TestFrTimeFilter:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (datetime(2026, 7, 26, 12, 0), datetime(2026, 7, 26, 14, 0, tzinfo=PARIS)),
            (datetime(2026, 1, 15, 12, 0), datetime(2026, 1, 15, 13, 0, tzinfo=PARIS)),
        ],
        ids=["summer_utc_plus_2", "winter_utc_plus_1"],
    )
    def test_a_naive_datetime_is_read_as_utc_then_shown_in_paris(
        self, jinja_filters, value, expected
    ):
        """Les colonnes TIMESTAMP de Postgres sont naïves et contiennent de
        l'UTC : le filtre les étiquette UTC avant de convertir. L'heure d'été
        est gérée par `zoneinfo`, d'où les deux cas."""
        assert jinja_filters["fr_time"](value) == expected

    def test_an_aware_datetime_is_converted_not_relabelled(self, jinja_filters):
        instant = datetime(2026, 7, 26, 12, 0, tzinfo=ZoneInfo("America/New_York"))

        result = jinja_filters["fr_time"](instant)

        assert result == instant, "le même instant"
        assert result.utcoffset() == timedelta(hours=2), "mais exprimé à Paris"

    def test_a_plain_date_becomes_midnight_paris_without_conversion(self, jinja_filters):
        """Une `date` n'a pas d'heure : la traiter comme de l'UTC la décalerait
        au 25 juillet 22 h. Le filtre la place directement à minuit heure
        française — c'est ce qui garde « 26 juillet » affiché « 26 juillet »."""
        result = jinja_filters["fr_time"](date(2026, 7, 26))

        assert result == datetime(2026, 7, 26, 0, 0, tzinfo=PARIS)
        assert result.day == 26

    def test_none_stays_none(self, jinja_filters):
        assert jinja_filters["fr_time"](None) is None

    @pytest.mark.parametrize(
        ("value", "case"),
        [
            ("2026-07-26T12:00:00", "chaîne ISO"),
            (1785067200, "epoch"),
        ],
        ids=["iso_string", "epoch"],
    )
    def test_a_non_datetime_value_raises_inside_the_template(self, jinja_filters, value, case):
        """# BUG (borné) : `fr_time` ne valide pas son entrée.

        `dt.tzinfo` sur une chaîne ou un entier lève `AttributeError`. Dans un
        template Jinja, cela remonte en erreur de rendu (500) — contrairement à
        `parse_iso_date`, qui rattrape et rend None. Les deux filtres sont
        souvent chaînés (`... | parse_iso_date | fr_time`), ce qui protège le
        cas courant ; appliquer `fr_time` seul à une colonne texte ne pardonne
        pas.
        """
        with pytest.raises(AttributeError, match="tzinfo"):
            jinja_filters["fr_time"](value)


class TestFilterChaining:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-07-26T12:00:00Z", datetime(2026, 7, 26, 14, 0, tzinfo=PARIS)),
            ("2026-07-26T12:00:00", datetime(2026, 7, 26, 14, 0, tzinfo=PARIS)),
            ("2026-07-26T12:00:00+02:00", datetime(2026, 7, 26, 12, 0, tzinfo=PARIS)),
        ],
        ids=["zulu", "naive_assumed_utc", "already_paris"],
    )
    def test_the_two_filters_chain_as_the_templates_use_them(self, jinja_filters, raw, expected):
        """L'usage réel dans les templates : `value | parse_iso_date | fr_time`."""
        parsed = jinja_filters["parse_iso_date"](raw)

        assert jinja_filters["fr_time"](parsed) == expected

    def test_an_unparsable_date_chains_safely_to_none(self, jinja_filters):
        """C'est ce qui rend la chaîne sûre : `parse_iso_date` rend None, et
        `fr_time(None)` rend None au lieu de lever."""
        parsed = jinja_filters["parse_iso_date"]("jamais scrapé")

        assert jinja_filters["fr_time"](parsed) is None


# ---------------------------------------------------------------------------
# Non-régression : importer `main` ne doit pas charger `.env`
# ---------------------------------------------------------------------------

class TestDotenvIsNotLoadedOnImport:
    """`.env` contient le `DATABASE_URL` de PRODUCTION.

    Un `load_dotenv()` au niveau module de `main.py` suffit à ce qu'un simple
    `import main` — ce que fait ce fichier de tests — peuple `os.environ` et
    fasse tourner une suite de tests contre la base de production. L'incident
    s'est produit DEUX fois ; le correctif est l'appel déplacé dans
    `create_app()` (main.py:40).
    """

    def test_load_dotenv_is_only_called_inside_create_app(self):
        """Vérification statique : aucune autre fonction, et surtout pas le
        corps du module, ne doit appeler `load_dotenv`."""
        tree = ast.parse((REPO_ROOT / "main.py").read_text(encoding="utf-8"))
        callers: list[str] = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            if name != "load_dotenv":
                continue
            enclosing = [
                parent.name
                for parent in ast.walk(tree)
                if isinstance(parent, ast.FunctionDef) and node in ast.walk(parent)
            ]
            callers.append(enclosing[0] if enclosing else "<module>")

        assert callers == ["create_app"], (
            "load_dotenv ne doit être appelé que dans create_app() — "
            f"appels trouvés dans : {callers}"
        )

    def test_importing_main_in_a_fresh_interpreter_leaves_the_environment_alone(self):
        """Vérification dynamique, dans un interpréteur neuf : `import main` ne
        doit ni appeler `load_dotenv` ni introduire `DATABASE_URL`.

        Un sous-process est nécessaire : `main` est déjà importé ici, et le
        réimporter ne rejouerait pas son corps.
        """
        program = (
            "import os, sys, dotenv\n"
            "calls = []\n"
            "dotenv.load_dotenv = lambda *a, **k: calls.append(a) or False\n"
            "os.environ.pop('DATABASE_URL', None)\n"
            "import main\n"
            "assert calls == [], f'load_dotenv appelé à l\\'import : {calls}'\n"
            "assert 'DATABASE_URL' not in os.environ, 'DATABASE_URL a fui dans os.environ'\n"
            "print('ok')\n"
        )

        result = subprocess.run(
            [sys.executable, "-c", program],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().endswith("ok")

    def test_the_env_file_would_actually_have_been_a_problem(self):
        """Garde-fou du garde-fou : si `.env` n'existe pas ou ne contient pas
        `DATABASE_URL`, le test ci-dessus perd son mordant sans devenir rouge.
        Ce test le signale explicitement plutôt que de laisser croire à une
        protection qui ne prouve rien.
        """
        env_file = REPO_ROOT / ".env"
        if not env_file.exists():
            pytest.skip(".env absent de cet environnement (CI) : rien à faire fuir")

        assert "DATABASE_URL" in env_file.read_text(encoding="utf-8"), (
            "le .env local ne contient plus DATABASE_URL : le test de non-régression "
            "ci-dessus ne protège plus de rien, vérifier avant de le supprimer"
        )
