"""Tests unitaires de `services/scrape_service.py`.

C'est le chemin critique métier : ce module décide ce qui est scrapé, ce qui
est notifié, et ce qui est marqué comme traité en base. Une régression ici est
silencieuse (annonces perdues, notifications dupliquées) — d'où la couverture
exhaustive de la matrice des statuts et de la durabilité des notifications.

Ce qui n'est PAS testé ici, volontairement :
  * `mock_app.app_context()` — `ScrapeService` ne dépend d'aucun contexte
    Flask (c'est même sa raison d'être : tourner sur un thread de fond).
    L'ancienne suite traînait cette cérémonie dans 13 tests, sans effet.
  * `storage.settings.get_setting` — jamais consulté dans le chemin de scrape.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest
import requests
from freezegun import freeze_time
from loguru import logger

from parsers.base import BaseParser
from services.scrape_service import ScrapeService
from tests.helpers.factories import make_criteria, make_listing, make_search_row
from tests.helpers.fakes import fake_notifier, fake_storage

FROZEN = "2026-07-25 10:00:00"
FROZEN_DT = datetime.datetime(2026, 7, 25, 10, 0, 0)


# ---------------------------------------------------------------------------
# Outillage
# ---------------------------------------------------------------------------

def make_parser(listings=None, reason=None, scrape_error=None) -> MagicMock:
    """Un parser doublé. `spec=BaseParser` fait échouer l'appel d'une méthode
    qui n'existe pas sur l'interface, au lieu de renvoyer un mock complaisant.
    """
    parser = MagicMock(spec=BaseParser)
    parser.cannot_search_reason.return_value = reason
    if scrape_error is not None:
        parser.scrape.side_effect = scrape_error
    else:
        parser.scrape.return_value = list(listings or [])
    return parser


@pytest.fixture
def patch_parsers(monkeypatch):
    """Installe une table `source -> parser`. Une source absente de la table
    lève `ValueError`, exactement comme le registry réel."""

    def install(parsers_by_source: dict) -> None:
        def fake_get_parser(source, storage=None):
            if source not in parsers_by_source:
                raise ValueError(f"Source '{source}' inconnue. Sources disponibles : seloger")
            return parsers_by_source[source]

        monkeypatch.setattr("parsers.get_parser", fake_get_parser)

    return install


@pytest.fixture
def env(patch_parsers):
    """Storage + notifier doublés, et un `run()` qui exécute un scrape."""

    class Env:
        def __init__(self):
            self.storage = fake_storage()
            self.notifier = fake_notifier()
            self.service = ScrapeService(self.storage, self.notifier)

        def search(self, **overrides):
            row = make_search_row(**overrides)
            self.storage.searches.get_search.return_value = row
            return row

        def run(self, parsers=None, search_id=1, user_id=1):
            patch_parsers(parsers if parsers is not None else {})
            return self.service.execute(search_id, user_id)

        @property
        def log_calls(self):
            return self.storage.scrape_logs.create_scrape_log.call_args_list

        def only_log(self):
            """L'unique appel à create_scrape_log — échoue s'il y en a 0 ou 2."""
            calls = self.log_calls
            assert len(calls) == 1, f"attendu 1 log de scrape, obtenu {len(calls)}: {calls}"
            return calls[0]

    return Env()


# ---------------------------------------------------------------------------
# execute() — autorisation
# ---------------------------------------------------------------------------

class TestAuthorization:
    """`execute` est appelé depuis un thread de fond avec un `user_id` : c'est
    le seul contrôle d'accès du pipeline."""

    @pytest.mark.parametrize(
        ("search_row", "case"),
        [
            (None, "recherche inexistante"),
            (make_search_row(user_id=2), "recherche d'un autre utilisateur"),
        ],
        ids=["missing_search", "foreign_search"],
    )
    @freeze_time(FROZEN)
    def test_returns_zero_without_touching_anything(self, env, search_row, case):
        env.storage.searches.get_search.return_value = search_row

        assert env.run() == 0, case

        # Un simple `== 0` ne prouverait rien : on vérifie aussi qu'aucun effet
        # de bord n'a eu lieu — ni scrape, ni log, ni marquage.
        env.storage.searches.update_last_scraped.assert_not_called()
        env.storage.scrape_logs.create_scrape_log.assert_not_called()
        env.storage.listings.get_unnotified_listings_for_search.assert_not_called()
        env.notifier.notify_new_listing.assert_not_called()

    @freeze_time(FROZEN)
    def test_the_search_is_looked_up_by_its_own_id(self, env):
        env.storage.searches.get_search.return_value = None

        env.run(search_id=42, user_id=7)

        env.storage.searches.get_search.assert_called_once_with(42)


# ---------------------------------------------------------------------------
# _do_scrape() — matrice complète des statuts
# ---------------------------------------------------------------------------

class TestStatusMatrix:
    """Un test par ligne de la matrice « situation -> statut loggé / retour »."""

    @pytest.mark.parametrize(
        "criteria",
        [None, {}, [], "locations=Paris", 0],
        ids=["none", "empty_dict", "list", "string", "zero"],
    )
    @freeze_time(FROZEN)
    def test_empty_or_non_dict_criteria_logs_error_and_returns_zero(self, env, criteria):
        env.search(criteria=criteria)

        assert env.run() == 0

        args, kwargs = env.only_log()
        assert args == (1, "error")
        assert kwargs["error_message"] == "Critères vides"
        assert kwargs["started_at"] == FROZEN_DT
        # Aucun parser n'a été sollicité : on n'a pas tenté de scraper.
        env.storage.listings.save_and_link.assert_not_called()

    @freeze_time(FROZEN)
    def test_no_valid_source_logs_the_missing_location_error(self, env):
        """Toutes les sources refusent les critères (`cannot_search_reason`)."""
        env.search(sources=["seloger", "laforet"])
        parsers = {
            "seloger": make_parser(reason="aucune localisation exploitable"),
            "laforet": make_parser(reason="Laforet ne référence pas les terrains"),
        }

        assert env.run(parsers) == 0

        args, kwargs = env.only_log()
        assert args == (1, "error")
        assert kwargs["error_message"] == "Critères vides ou lieu manquant pour toutes les sources"
        # Le statut « aucune source lançable » précède l'agrégation des erreurs :
        # aucun `scrape()` n'a été tenté.
        parsers["seloger"].scrape.assert_not_called()
        parsers["laforet"].scrape.assert_not_called()

    @freeze_time(FROZEN)
    def test_all_sources_failing_concatenates_source_prefixed_messages(self, env):
        env.search(sources=["seloger", "laforet"])
        parsers = {
            "seloger": make_parser(scrape_error=ValueError("bloqué par DataDome")),
            "laforet": make_parser(scrape_error=RuntimeError("502 Bad Gateway")),
        }

        assert env.run(parsers) == 0

        args, kwargs = env.only_log()
        assert args == (1, "error")
        assert kwargs["error_message"] == "seloger: bloqué par DataDome; laforet: 502 Bad Gateway"

    @freeze_time(FROZEN)
    def test_valid_sources_finding_nothing_is_empty_not_error(self, env):
        """« 0 annonce » est un résultat légitime, pas une panne : le statut
        distinct évite de le compter comme une erreur dans l'UI et les stats."""
        env.search(sources=["seloger", "laforet"])
        parsers = {"seloger": make_parser([]), "laforet": make_parser([])}

        assert env.run(parsers) == 0

        args, kwargs = env.only_log()
        assert args == (1, "empty"), "un scrape sain sans résultat ne doit jamais être loggé 'error'"
        assert kwargs["error_message"] == "Aucune annonce ne correspond aux critères"
        assert kwargs["listings_found"] == 0
        assert kwargs["new_listings"] == 0
        assert kwargs["details"] == {"per_source": {"seloger": {"found": 0}, "laforet": {"found": 0}}}
        env.storage.listings.save_and_link.assert_not_called()

    @freeze_time(FROZEN)
    def test_success_logs_counts_already_known_and_per_source(self, env):
        env.search(sources=["seloger", "laforet"])
        found = [make_listing(listing_id="sl_1"), make_listing(listing_id="lf_1")]
        known = [make_listing(listing_id="sl_old")]
        env.storage.listings.save_and_link.return_value = ([found[0]], known)
        parsers = {"seloger": make_parser([found[0]]), "laforet": make_parser([found[1]])}

        assert env.run(parsers) == 1, "le retour est le nombre de NOUVELLES annonces"

        args, kwargs = env.only_log()
        assert args == (1, "success")
        assert kwargs["listings_found"] == 2
        assert kwargs["new_listings"] == 1
        assert kwargs["details"] == {
            "already_known": 1,
            "per_source": {"seloger": {"found": 1}, "laforet": {"found": 1}},
        }
        env.storage.listings.save_and_link.assert_called_once_with(found, 1)


# ---------------------------------------------------------------------------
# Isolation par source
# ---------------------------------------------------------------------------

class TestSourceIsolation:
    """Une source down ne doit jamais empêcher les autres de tourner : chacune
    encode la localisation différemment et tombe indépendamment."""

    @freeze_time(FROZEN)
    def test_unknown_parser_is_reported_per_source_without_stopping_the_others(self, env):
        env.search(sources=["nawak", "laforet"])
        listing = make_listing(listing_id="lf_1")
        env.storage.listings.save_and_link.return_value = ([listing], [])

        assert env.run({"laforet": make_parser([listing])}) == 1

        _, kwargs = env.only_log()
        per_source = kwargs["details"]["per_source"]
        assert per_source["nawak"]["error"].startswith("Parser inconnu: Source 'nawak' inconnue")
        assert per_source["laforet"] == {"found": 1}

    @freeze_time(FROZEN)
    def test_cannot_search_reason_skips_the_source_with_its_reason(self, env):
        env.search(sources=["seloger", "laforet"])
        listing = make_listing(listing_id="lf_1")
        env.storage.listings.save_and_link.return_value = ([listing], [])
        refusing = make_parser(reason="Laforet ne référence pas les parkings")
        parsers = {"seloger": make_parser([listing]), "laforet": refusing}

        assert env.run(parsers) == 1

        _, kwargs = env.only_log()
        assert kwargs["details"]["per_source"]["laforet"] == {
            "error": "Laforet ne référence pas les parkings"
        }
        refusing.scrape.assert_not_called(), "une source refusée ne doit pas être appelée"

    @freeze_time(FROZEN)
    def test_one_failing_source_does_not_block_the_others(self, env):
        env.search(sources=["seloger", "laforet"])
        listing = make_listing(listing_id="lf_1")
        env.storage.listings.save_and_link.return_value = ([listing], [])
        parsers = {
            "seloger": make_parser(scrape_error=ValueError("SeLoger down")),
            "laforet": make_parser([listing]),
        }

        assert env.run(parsers) == 1

        _, kwargs = env.only_log()
        assert kwargs["details"]["per_source"] == {
            "seloger": {"error": "SeLoger down"},
            "laforet": {"found": 1},
        }
        env.storage.listings.save_and_link.assert_called_once_with([listing], 1)

    @pytest.mark.parametrize(
        ("reason", "scrape_error", "expected_message"),
        [
            # any_valid = False : rien n'était lançable.
            ("lieu manquant", None, "Critères vides ou lieu manquant pour toutes les sources"),
            # any_valid = True, any_success = False : tout a été lancé et a planté.
            (None, ValueError("boom"), "seloger: boom"),
        ],
        ids=["nothing_runnable", "everything_crashed"],
    )
    @freeze_time(FROZEN)
    def test_any_valid_and_any_success_are_distinct_failures(
        self, env, reason, scrape_error, expected_message
    ):
        """« Rien de lançable » et « tout a planté » ne se diagnostiquent pas
        pareil : le message d'erreur doit les distinguer."""
        env.search(sources=["seloger"])

        assert env.run({"seloger": make_parser(reason=reason, scrape_error=scrape_error)}) == 0

        _, kwargs = env.only_log()
        assert kwargs["error_message"] == expected_message

    @pytest.mark.parametrize(
        ("row_overrides", "expected_sources"),
        [
            ({"sources": ["seloger", "laforet"]}, ["seloger", "laforet"]),
            ({"sources": [], "source": "laforet"}, ["laforet"]),
            ({"sources": None, "source": "laforet"}, ["laforet"]),
        ],
        ids=["explicit_sources", "empty_sources_falls_back", "null_sources_falls_back"],
    )
    @freeze_time(FROZEN)
    def test_sources_fall_back_to_the_legacy_single_source_column(
        self, env, patch_parsers, row_overrides, expected_sources
    ):
        """Rétrocompat : les lignes créées avant la colonne `sources`."""
        env.search(**row_overrides)
        asked: list[str] = []

        def recording_get_parser(source, storage=None):
            asked.append(source)
            return make_parser([])

        patch_parsers({})  # remplacé juste après, mais garde le monkeypatch actif
        import parsers as parsers_mod

        parsers_mod.get_parser = recording_get_parser
        env.service.execute(1, 1)

        assert asked == expected_sources


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

class TestNotifications:
    """La liste notifiée vient de la BASE, pas du scrape : c'est ce qui permet
    de rattraper les annonces d'un scrape précédent resté en échec d'envoi."""

    def _prepare(self, env, pending, **row_overrides):
        env.search(sources=["seloger"], **row_overrides)
        scraped = [make_listing(listing_id="sl_fresh")]
        env.storage.listings.save_and_link.return_value = (scraped, [])
        env.storage.listings.get_unnotified_listings_for_search.return_value = pending
        return scraped

    @freeze_time(FROZEN)
    def test_pending_listings_are_reread_from_the_database(self, env):
        """Une annonce restée non notifiée d'un scrape antérieur (absente du
        scrape courant) doit quand même partir."""
        leftover = make_listing(listing_id="sl_previous", agency="Agence A")
        scraped = self._prepare(env, [leftover])

        env.run({"seloger": make_parser(scraped)})

        env.storage.listings.get_unnotified_listings_for_search.assert_called_once_with(1)
        env.notifier.notify_new_listing.assert_called_once_with("test-topic", leftover)
        env.storage.listings.mark_listings_notified.assert_called_once_with(1, ["sl_previous"])

    @pytest.mark.parametrize(
        ("blacklist_mode", "notified_agencies", "handled_ids"),
        [
            # Remplace tests/_legacy/test_blacklist_mode.py (167 lignes, ~1
            # assertion utile, dont deux tests indiscernables).
            ("exclude", [], ["sl_bad", "sl_good"]),
            ("no_notify", ["Bonne Agence"], ["sl_bad", "sl_good"]),
            (None, [], ["sl_bad", "sl_good"]),
        ],
        ids=["exclude", "no_notify", "default_is_exclude"],
    )
    @freeze_time(FROZEN)
    def test_blacklist_modes_mark_listings_handled_without_notifying_them(
        self, env, blacklist_mode, notified_agencies, handled_ids
    ):
        """Dans les deux modes, l'annonce blacklistée est marquée TRAITÉE sans
        notification : sinon elle serait re-considérée à chaque scrape.

        `exclude` retire aussi les non-blacklistées de l'envoi ? Non : seule la
        blacklistée est filtrée — c'est ce que l'ancien test ne vérifiait pas,
        il n'assertait que `notify_new_listing.assert_not_called()`.
        """
        bad = make_listing(listing_id="sl_bad", agency="Mauvaise Agence")
        good = make_listing(listing_id="sl_good", agency="Bonne Agence")
        overrides = {"blacklisted_agencies": ["Mauvaise Agence"]}
        if blacklist_mode is not None:
            overrides["blacklist_mode"] = blacklist_mode
        else:
            # Colonne absente : le service doit retomber sur "exclude".
            row = make_search_row(sources=["seloger"], **overrides)
            row.pop("blacklist_mode")
            env.storage.searches.get_search.return_value = row
            env.storage.listings.save_and_link.return_value = ([bad, good], [])
            env.storage.listings.get_unnotified_listings_for_search.return_value = [bad, good]
            env.run({"seloger": make_parser([bad, good])})
            assert [c.args[1].agency for c in env.notifier.notify_new_listing.call_args_list] == [
                "Bonne Agence"
            ]
            env.storage.listings.mark_listings_notified.assert_called_once_with(1, handled_ids)
            return

        scraped = self._prepare(env, [bad, good], **overrides)
        env.storage.listings.save_and_link.return_value = (scraped, [])

        env.run({"seloger": make_parser(scraped)})

        sent = [c.args[1].agency for c in env.notifier.notify_new_listing.call_args_list]
        if blacklist_mode == "exclude":
            # `needs_filter` retire la blacklistée ; la bonne part normalement.
            assert sent == ["Bonne Agence"]
        else:
            assert sent == notified_agencies
        env.storage.listings.mark_listings_notified.assert_called_once_with(1, handled_ids)

    @freeze_time(FROZEN)
    def test_failed_notification_is_not_marked_handled_so_it_is_retried(self, env):
        pending = [make_listing(listing_id="sl_ok"), make_listing(listing_id="sl_ko")]
        scraped = self._prepare(env, pending)
        env.storage.listings.save_and_link.return_value = (scraped, [])
        env.notifier.notify_new_listing.side_effect = [True, False]

        env.run({"seloger": make_parser(scraped)})

        env.storage.listings.mark_listings_notified.assert_called_once_with(1, ["sl_ok"])

    @freeze_time(FROZEN)
    def test_marking_happens_in_a_single_call_even_for_many_listings(self, env):
        pending = [make_listing(listing_id=f"sl_{i}") for i in range(5)]
        scraped = self._prepare(env, pending)
        env.storage.listings.save_and_link.return_value = (scraped, [])

        env.run({"seloger": make_parser(scraped)})

        env.storage.listings.mark_listings_notified.assert_called_once_with(
            1, ["sl_0", "sl_1", "sl_2", "sl_3", "sl_4"]
        )

    @freeze_time(FROZEN)
    def test_throttles_three_tenths_of_a_second_per_listing(self, env, slept):
        """Le throttling ntfy n'était ni testé ni neutralisé : 20 annonces
        faisaient dormir la suite 6 secondes pour de vrai."""
        pending = [make_listing(listing_id=f"sl_{i}") for i in range(4)]
        scraped = self._prepare(env, pending)
        env.storage.listings.save_and_link.return_value = (scraped, [])

        env.run({"seloger": make_parser(scraped)})

        assert slept == [0.3, 0.3, 0.3, 0.3]

    @freeze_time(FROZEN)
    def test_blacklisted_listings_do_not_pay_the_throttle(self, env, slept):
        """Une annonce filtrée sort par `continue` AVANT le `time.sleep(0.3)`.

        Le throttle ne coûte donc que pour les annonces réellement notifiées,
        ce qui est le comportement souhaitable : il protège ntfy, et aucun
        appel réseau n'a lieu pour une annonce blacklistée.
        """
        bad = make_listing(listing_id="sl_bad", agency="Mauvaise Agence")
        scraped = self._prepare(
            env, [bad], blacklisted_agencies=["Mauvaise Agence"], blacklist_mode="exclude"
        )
        env.storage.listings.save_and_link.return_value = (scraped, [])

        env.run({"seloger": make_parser(scraped)})

        env.notifier.notify_new_listing.assert_not_called()
        assert slept == []
        # L'annonce est tout de même marquée traitée : elle ne doit pas être
        # re-tentée à chaque scrape.
        env.storage.listings.mark_listings_notified.assert_called_once_with(1, ["sl_bad"])

    @freeze_time(FROZEN)
    def test_summary_notification_stays_disabled(self, env):
        """`notify_summary` est commenté dans le code (anti-spam). Ce test
        existe pour qu'une réactivation accidentelle soit rouge, pas silencieuse.
        """
        scraped = self._prepare(env, [])
        env.storage.listings.save_and_link.return_value = (scraped, [])

        env.run({"seloger": make_parser(scraped)})

        env.notifier.notify_summary.assert_not_called()

    @freeze_time(FROZEN)
    def test_notifications_disabled_skip_sending_but_still_mark_everything_handled(self, env):
        """#10 : `notify_enabled=False` court-circuite l'ENVOI, jamais le
        marquage. Toutes les annonces en attente (celles du scrape courant ET
        les restes d'un scrape antérieur) sont marquées traitées sans envoi :
        c'est ce qui garantit qu'à la réactivation, seules les annonces
        FUTURES partiront — jamais de salve rétrospective.
        """
        leftover = make_listing(listing_id="sl_previous")
        fresh = make_listing(listing_id="sl_fresh")
        scraped = self._prepare(env, [leftover, fresh], notify_enabled=False)

        env.run({"seloger": make_parser(scraped)})

        env.notifier.notify_new_listing.assert_not_called()
        env.storage.listings.mark_listings_notified.assert_called_once_with(1, ["sl_previous", "sl_fresh"])

    @freeze_time(FROZEN)
    def test_disabled_notifications_pay_no_throttle(self, env, slept):
        """Le throttle ntfy protège l'envoi : aucune notification partant,
        aucun sommeil — un scrape silencieux ne doit pas ralentir."""
        pending = [make_listing(listing_id=f"sl_{i}") for i in range(3)]
        scraped = self._prepare(env, pending, notify_enabled=False)

        env.run({"seloger": make_parser(scraped)})

        assert slept == []
        env.storage.listings.mark_listings_notified.assert_called_once_with(
            1, ["sl_0", "sl_1", "sl_2"]
        )

    @freeze_time(FROZEN)
    def test_a_row_without_the_notify_flag_keeps_notifying(self, env):
        """Rétrocompat : une ligne lue sans la clé (lectures qui n'auraient pas
        encore la colonne) retombe sur le défaut « notifications activées »."""
        listing = make_listing(listing_id="sl_1")
        row = make_search_row(sources=["seloger"])
        row.pop("notify_enabled")
        env.storage.searches.get_search.return_value = row
        env.storage.listings.save_and_link.return_value = ([listing], [])
        env.storage.listings.get_unnotified_listings_for_search.return_value = [listing]

        env.run({"seloger": make_parser([listing])})

        env.notifier.notify_new_listing.assert_called_once_with("test-topic", listing)
        env.storage.listings.mark_listings_notified.assert_called_once_with(1, ["sl_1"])

    @freeze_time(FROZEN)
    def test_disabled_notifications_beat_an_empty_blacklist_and_still_mark(self, env):
        """Le flag #10 s'ajoute au filtrage blacklist : sans blacklist, toutes
        les annonces sont quand même marquées traitées, sans envoi."""
        listings = [make_listing(listing_id="sl_a"), make_listing(listing_id="sl_b")]
        scraped = self._prepare(
            env, list(listings),
            notify_enabled=False, blacklist_mode="exclude", blacklisted_agencies=[],
        )

        env.run({"seloger": make_parser(scraped)})

        env.notifier.notify_new_listing.assert_not_called()
        env.storage.listings.mark_listings_notified.assert_called_once_with(1, ["sl_a", "sl_b"])

    @freeze_time(FROZEN)
    def test_blacklisted_agencies_null_in_database_is_harmless_in_exclude_mode(self, env):
        """La colonne est nullable : `None` ne doit pas faire dérailler le
        filtrage (`needs_filter` devient simplement faux)."""
        listing = make_listing(listing_id="sl_1")
        scraped = self._prepare(
            env, [listing], blacklisted_agencies=None, blacklist_mode="exclude"
        )
        env.storage.listings.save_and_link.return_value = (scraped, [])

        assert env.run({"seloger": make_parser(scraped)}) == 1

        env.notifier.notify_new_listing.assert_called_once_with("test-topic", listing)

    @freeze_time(FROZEN)
    def test_blacklisted_agencies_null_crashes_the_whole_scrape_in_no_notify_mode(self, env):
        """# BUG : `set(blacklisted_agencies)` sans garde `or []`.

        services/scrape_service.py:156 fait
        `set(blacklisted_agencies) if blacklist_mode == "no_notify" else set()`.
        La colonne `blacklisted_agencies` étant nullable, une recherche en mode
        `no_notify` dont la liste vaut NULL lève `TypeError: 'NoneType' object
        is not iterable` — APRÈS `save_and_link`, donc les annonces sont en
        base mais aucune notification ne partira jamais et le scrape est loggé
        « error » à chaque cycle. Comportement actuel figé ici.
        """
        listing = make_listing(listing_id="sl_1")
        scraped = self._prepare(
            env, [listing], blacklisted_agencies=None, blacklist_mode="no_notify"
        )
        env.storage.listings.save_and_link.return_value = (scraped, [])

        with pytest.raises(TypeError, match="not iterable"):
            env.run({"seloger": make_parser(scraped)})

        # L'exception passe par le bloc `except` : le scrape est loggé en erreur.
        args, kwargs = env.only_log()
        assert args == (1, "error")
        assert "TypeError" in kwargs["error_message"] or "not iterable" in kwargs["error_message"]


# ---------------------------------------------------------------------------
# execute() — update_last_scraped et gestion d'exception
# ---------------------------------------------------------------------------

class TestExecuteLifecycle:
    @freeze_time(FROZEN)
    def test_last_scraped_is_marked_after_the_scrape_completes(self, env):
        order: list[str] = []
        listing = make_listing(listing_id="sl_1")
        env.search(sources=["seloger"])
        env.storage.listings.save_and_link.side_effect = lambda listings, sid: (
            order.append("save_and_link"),
            ([listing], []),
        )[1]
        env.storage.scrape_logs.create_scrape_log.side_effect = lambda *a, **k: (
            order.append("create_scrape_log"),
            99,
        )[1]
        env.storage.searches.update_last_scraped.side_effect = lambda sid: order.append(
            "update_last_scraped"
        )

        env.run({"seloger": make_parser([listing])})

        assert order == ["save_and_link", "create_scrape_log", "update_last_scraped"]
        env.storage.searches.update_last_scraped.assert_called_once_with(1)

    @pytest.mark.parametrize(
        "criteria",
        [{}, make_criteria()],
        ids=["early_return", "full_path"],
    )
    @freeze_time(FROZEN)
    def test_last_scraped_is_marked_even_on_a_handled_failure(self, env, criteria):
        """Un échec *géré* (critères vides, source down) consomme le cycle :
        inutile de retenter dans 30 secondes."""
        env.search(sources=["seloger"], criteria=criteria)

        env.run({"seloger": make_parser(scrape_error=ValueError("down"))})

        env.storage.searches.update_last_scraped.assert_called_once_with(1)

    @freeze_time(FROZEN)
    def test_unexpected_exception_leaves_the_search_retriable(self, env):
        """Pas de `update_last_scraped` sur une exception inattendue : la
        recherche doit repartir au prochain cycle, pas dans un intervalle plein.
        """
        env.search(sources=["seloger"])
        listing = make_listing(listing_id="sl_1")
        env.storage.listings.save_and_link.side_effect = RuntimeError("pool épuisé")

        with pytest.raises(RuntimeError, match="pool épuisé"):
            env.run({"seloger": make_parser([listing])})

        env.storage.searches.update_last_scraped.assert_not_called()

    @freeze_time(FROZEN)
    def test_exception_is_logged_as_error_and_reraised(self, env):
        env.search(sources=["seloger"])
        listing = make_listing(listing_id="sl_1")
        env.storage.listings.save_and_link.side_effect = RuntimeError("pool épuisé")

        with pytest.raises(RuntimeError, match="pool épuisé"):
            env.run({"seloger": make_parser([listing])})

        args, kwargs = env.only_log()
        assert args == (1, "error")
        assert kwargs["error_message"] == "Exception: pool épuisé"
        assert kwargs["started_at"] == FROZEN_DT

    @freeze_time(FROZEN)
    def test_a_failure_to_log_the_failure_still_reraises_the_original(self, env):
        """Si la base de logs est elle aussi cassée, l'exception d'origine doit
        remonter — pas celle du logging, qui masquerait la cause."""
        env.search(sources=["seloger"])
        listing = make_listing(listing_id="sl_1")
        env.storage.listings.save_and_link.side_effect = RuntimeError("cause racine")
        env.storage.scrape_logs.create_scrape_log.side_effect = OSError("disque plein")

        with pytest.raises(RuntimeError, match="cause racine"):
            env.run({"seloger": make_parser([listing])})

    @freeze_time(FROZEN)
    def test_cleanup_runs_on_both_the_success_and_the_exception_path(self, env, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(
            "scrape_logs.manager.SearchLogManager.cleanup_old_logs",
            staticmethod(lambda: calls.append("cleanup") or 0),
        )
        env.search(sources=["seloger"])
        listing = make_listing(listing_id="sl_1")
        env.storage.listings.save_and_link.return_value = ([listing], [])
        env.run({"seloger": make_parser([listing])})
        assert calls == ["cleanup"]

        env.storage.listings.save_and_link.side_effect = RuntimeError("boum")
        with pytest.raises(RuntimeError, match="boum"):
            env.run({"seloger": make_parser([listing])})
        assert calls == ["cleanup", "cleanup"]


# ---------------------------------------------------------------------------
# Logs bruts et fuite de handler loguru
# ---------------------------------------------------------------------------

class TestRawLogsAndHandlerLeak:
    @freeze_time(FROZEN)
    def test_raw_logs_are_attached_only_on_the_success_path(self, env):
        env.search(sources=["seloger"])
        listing = make_listing(listing_id="sl_1")
        env.storage.listings.save_and_link.return_value = ([listing], [])
        env.storage.scrape_logs.create_scrape_log.return_value = 77

        env.run({"seloger": make_parser([listing])})

        args = env.storage.scrape_logs.update_scrape_log_raw.call_args.args
        assert args[0] == 77
        assert "[LogManager] Capture démarrée pour search_id=1" in args[1]

    @pytest.mark.parametrize(
        "case",
        ["empty_criteria", "no_valid_source", "all_sources_failed", "empty_result", "exception"],
    )
    @freeze_time(FROZEN)
    def test_no_raw_logs_are_stored_for_a_failed_scrape(self, env, case):
        """Les logs bruts sont attachés dans le bloc de succès uniquement
        (scrape_service.py:196-197). Conséquence : aucun log brut consultable
        pour un scrape en erreur — précisément le cas où on en a besoin pour
        diagnostiquer. Divergence documentée, pas corrigée.
        """
        self._run_case(env, case)

        env.storage.scrape_logs.update_scrape_log_raw.assert_not_called()

    @pytest.mark.parametrize(
        ("case", "leaks"),
        [
            ("unauthorized", True),
            ("empty_criteria", True),
            ("no_valid_source", True),
            ("all_sources_failed", True),
            ("empty_result", True),
            ("exception", True),
            ("success", False),
        ],
    )
    @freeze_time(FROZEN)
    def test_loguru_handler_is_leaked_on_every_path_but_success(self, env, case, leaks):
        """# BUG : fuite de handler loguru (scrape_service.py:31 vs 196).

        `SearchLogManager.start()` fait un `logger.add(...)` global, mais
        `log_mgr.stop()` n'est appelé QUE dans le bloc de succès. Le retour
        d'autorisation (ligne 36), tous les retours anticipés de `_do_scrape`
        et le bloc `except` laissent le sink attaché : chaque scrape non réussi
        ajoute un handler et un descripteur de fichier qui ne seront jamais
        libérés tant que le process vit.

        Effets observés en production : un scheduler qui tourne toutes les
        30 s sur une recherche en erreur accumule ~2 900 handlers/jour, chacun
        recevant TOUS les logs du process (voir aussi
        tests/unit/test_scrape_logs_manager.py).

        Ce test fige le comportement actuel : `leaks=True` documente le bug.
        """
        before = len(logger._core.handlers)

        self._run_case(env, case)

        after = len(logger._core.handlers)
        if leaks:
            assert after == before + 1, (
                f"le chemin '{case}' devrait fuir exactement un handler "
                "(comportement actuel, buggé)"
            )
        else:
            assert after == before, "le chemin de succès est le seul à retirer son sink"

    @staticmethod
    def _run_case(env, case):
        listing = make_listing(listing_id="sl_1")
        if case == "unauthorized":
            env.storage.searches.get_search.return_value = None
            env.run()
            return
        if case == "empty_criteria":
            env.search(criteria={})
            env.run()
            return
        env.search(sources=["seloger"])
        if case == "no_valid_source":
            env.run({"seloger": make_parser(reason="lieu manquant")})
            return
        if case == "all_sources_failed":
            env.run({"seloger": make_parser(scrape_error=ValueError("down"))})
            return
        if case == "empty_result":
            env.run({"seloger": make_parser([])})
            return
        if case == "exception":
            env.storage.listings.save_and_link.side_effect = RuntimeError("boum")
            with pytest.raises(RuntimeError, match="boum"):
                env.run({"seloger": make_parser([listing])})
            return
        env.storage.listings.save_and_link.return_value = ([listing], [])
        env.run({"seloger": make_parser([listing])})


# ---------------------------------------------------------------------------
# Fallback géocodage commune (#26) — best effort, jamais bloquant
# ---------------------------------------------------------------------------


class TestGeocodageCommuneFallback:
    """Le fallback geo.api.gouv.fr est appelé AU SCRAPE, avant la sauvegarde ;
    son échec ne doit jamais faire perdre un scrape qui a réussi."""

    @staticmethod
    def _listing_sans_coords(listing_id: str):
        return make_listing(
            listing_id=listing_id, zip_code="31500", city="Toulouse",
            latitude=None, longitude=None, location_precision="",
        )

    def test_une_annonce_sans_coords_est_completee_avant_la_sauvegarde(self, env, monkeypatch):
        from services import geocode_commune

        annonce = self._listing_sans_coords("sl_1")
        env.search()
        env.storage.listings.save_and_link.return_value = ([annonce], [])

        monkeypatch.setattr(geocode_commune, "_resolve_uncached", lambda cp, ville: (43.6007, 1.4328))

        env.run({"seloger": make_parser([annonce])})

        sauvegardee = env.storage.listings.save_and_link.call_args.args[0][0]
        assert (sauvegardee.latitude, sauvegardee.longitude) == (43.6007, 1.4328)
        assert sauvegardee.location_precision == "commune"

    def test_le_cache_commune_est_consulte_via_le_storage_injecte(self, env, monkeypatch):
        from services import geocode_commune

        annonce = self._listing_sans_coords("sl_cache")
        env.search()

        def refuse(*args, **kwargs):
            raise AssertionError("Aucun réseau attendu quand le cache répond")

        monkeypatch.setattr(geocode_commune.requests, "get", refuse)
        env.storage.commune_geo.get_cached.return_value = {
            "area_key": "postal:31500", "latitude": 43.6, "longitude": 1.44,
        }

        env.run({"seloger": make_parser([annonce])})

        sauvegardee = env.storage.listings.save_and_link.call_args.args[0][0]
        assert (sauvegardee.latitude, sauvegardee.longitude) == (43.6, 1.44)

    @pytest.mark.parametrize(
        "panne",
        [
            pytest.param(RuntimeError("DB down"), id="repo-en-echec"),
            pytest.param(requests.ConnectTimeout(), id="reseau-indisponible"),
        ],
    )
    def test_une_panne_du_fallback_ne_fait_pas_echoer_le_scrape(self, env, panne, monkeypatch):
        annonce = self._listing_sans_coords("sl_2")
        env.search()
        env.storage.listings.save_and_link.return_value = ([annonce], [])
        env.storage.commune_geo.get_cached.side_effect = panne

        result = env.run({"seloger": make_parser([annonce])})

        assert result == 1
        args, kwargs = env.only_log()
        assert args[1] == "success"
        env.storage.listings.save_and_link.assert_called_once()

    def test_un_scrape_vide_ne_touche_pas_au_fallback(self, env):
        env.search()

        env.run({"seloger": make_parser([])})  # résultat vide légitime

        env.storage.commune_geo.get_cached.assert_not_called()

