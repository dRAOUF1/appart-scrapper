"""Tests de `core/scrape_control.py` — la soumission dédoublonnée d'un scrape.

Un scrape est déclenchable depuis quatre endroits (scheduler de fond, API REST,
interface web, back-office admin) qui tournent sur des threads différents. Ce
module est le seul point qui décide si un job part ou non, et son contrat a
deux faces :

* fonctionnelle : un scrape déjà en cours n'est pas relancé, un scrape terminé
  est relançable ;
* de concurrence : la séquence « lire `_scrape_futures` puis soumettre » est
  atomique. C'est un verrou de niveau *module* (`_lock`), donc partagé par
  toutes les recherches — testé ici sous contention réelle.

Les deux messages retournés sont affichés mot pour mot à l'utilisateur (bandeau
flash web, champ `message` de l'API) : ce sont des contrats de chaîne.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor

import pytest

import core.scrape_control as core_scrape_control
from core.scrape_control import file_dattente, submit_scrape

ALREADY_RUNNING = "Scraping déjà en cours pour cette recherche"
STARTED = "Scraping démarré en arrière-plan"


class FakeApp:
    """Le strict nécessaire de ce que `submit_scrape` lit sur l'app Flask.

    Volontairement pas un `MagicMock` : on veut qu'un accès à un attribut non
    prévu échoue, et que `_scrape_futures` soit un vrai dict dont on inspecte
    le contenu final.
    """

    def __init__(self, executor):
        self._scrape_futures: dict[int, object] = {}
        self._scrape_executor = executor
        self.storage = object()
        self.notifier = object()


class RecordingScrapeService:
    """Double de `ScrapeService` qui enregistre sa construction et ses appels.

    `submit_scrape` fait `from services.scrape_service import ScrapeService`
    *au moment de l'appel* : remplacer l'attribut du module suffit, sans avoir
    à toucher aux imports de `core.scrape_control`.
    """

    constructions: list[tuple] = []
    calls: list[tuple] = []
    gate: threading.Event | None = None

    def __init__(self, storage, notifier):
        type(self).constructions.append((storage, notifier))

    def execute(self, search_id, user_id):
        type(self).calls.append((search_id, user_id))
        if type(self).gate is not None:
            # Maintient le future « non terminé » tant que le test ne libère pas.
            type(self).gate.wait(timeout=5)
        return 0


@pytest.fixture
def scrape_service(monkeypatch):
    """Installe le double de ScrapeService et repart d'un état vierge."""
    import services.scrape_service as scrape_service_module

    RecordingScrapeService.constructions = []
    RecordingScrapeService.calls = []
    RecordingScrapeService.gate = None
    monkeypatch.setattr(scrape_service_module, "ScrapeService", RecordingScrapeService)
    return RecordingScrapeService


@pytest.fixture
def app(scrape_service):
    """App factice avec un vrai executor, arrêté proprement en fin de test."""
    executor = ThreadPoolExecutor(max_workers=4)
    fake_app = FakeApp(executor)
    yield fake_app
    if scrape_service.gate is not None:
        scrape_service.gate.set()
    executor.shutdown(wait=True)


def _finished_future(result=0) -> Future:
    """Un future déjà terminé, comme celui d'un scrape précédent."""
    fut: Future = Future()
    fut.set_result(result)
    return fut


def _pending_future() -> Future:
    """Un future jamais résolu, comme celui d'un scrape en cours."""
    return Future()


# ---------------------------------------------------------------------------
# Les trois branches de décision
# ---------------------------------------------------------------------------


def test_a_scrape_is_submitted_when_no_future_is_registered(app, scrape_service):
    submitted, message = submit_scrape(app, search_id=7, user_id=3)

    assert (submitted, message) == (True, STARTED)
    assert 7 in app._scrape_futures
    assert app._scrape_futures[7].result(timeout=5) == 0
    assert scrape_service.calls == [(7, 3)]


def test_a_running_scrape_is_not_relaunched_and_the_future_is_left_untouched(app, scrape_service):
    """Le cas qui justifie tout le module : deux clics sur « Lancer » ne doivent
    pas déclencher deux scrapes de la même recherche."""
    running = _pending_future()
    app._scrape_futures[7] = running

    submitted, message = submit_scrape(app, search_id=7, user_id=3)

    assert (submitted, message) == (False, ALREADY_RUNNING)
    assert app._scrape_futures[7] is running
    assert scrape_service.calls == []
    assert scrape_service.constructions == []


def test_a_finished_future_is_dropped_and_the_scrape_is_resubmitted(app, scrape_service):
    """Sans la suppression, une recherche ne serait scrapée qu'une fois par vie
    du process."""
    stale = _finished_future()
    app._scrape_futures[7] = stale

    submitted, message = submit_scrape(app, search_id=7, user_id=3)

    assert (submitted, message) == (True, STARTED)
    assert app._scrape_futures[7] is not stale
    assert app._scrape_futures[7].result(timeout=5) == 0
    assert scrape_service.calls == [(7, 3)]


@pytest.mark.parametrize(
    ("outcome", "description"),
    [
        pytest.param("result", "scrape terminé normalement", id="future-avec-resultat"),
        pytest.param("exception", "scrape terminé en erreur", id="future-avec-exception"),
        pytest.param("cancelled", "scrape annulé avant de démarrer", id="future-annule"),
    ],
)
def test_any_terminated_future_makes_the_search_eligible_again(app, scrape_service, outcome, description):
    """`fut.done()` est vrai pour un succès, une exception ET une annulation :
    un scrape qui a planté ne doit pas bloquer définitivement sa recherche."""
    fut: Future = Future()
    if outcome == "result":
        fut.set_result(0)
    elif outcome == "exception":
        fut.set_exception(RuntimeError(description))
    else:
        assert fut.cancel(), "un future jamais démarré doit être annulable"

    submitted, message = submit_scrape(app, search_id=7, user_id=3)

    assert (submitted, message) == (True, STARTED), description
    assert scrape_service.calls == [(7, 3)]


# ---------------------------------------------------------------------------
# Câblage du job
# ---------------------------------------------------------------------------


def test_the_service_is_built_with_the_app_storage_and_notifier(app, scrape_service):
    """Le thread de fond n'a pas de contexte Flask : ses dépendances doivent
    lui être passées explicitement au moment de la soumission."""
    submit_scrape(app, search_id=7, user_id=3)
    app._scrape_futures[7].result(timeout=5)

    assert scrape_service.constructions == [(app.storage, app.notifier)]


def test_the_search_and_user_ids_are_forwarded_in_that_order(app, scrape_service):
    """`execute(search_id, user_id)` — deux entiers de même type côte à côte :
    une inversion notifierait le mauvais utilisateur sans lever d'erreur."""
    submit_scrape(app, search_id=41, user_id=99)
    app._scrape_futures[41].result(timeout=5)

    assert scrape_service.calls == [(41, 99)]


def test_two_different_searches_do_not_block_each_other(app, scrape_service):
    """Le verrou est de niveau module (partagé), mais la déduplication est par
    recherche : un scrape en cours sur la recherche 1 ne doit pas empêcher la
    recherche 2 de partir."""
    app._scrape_futures[1] = _pending_future()

    assert submit_scrape(app, search_id=1, user_id=3) == (False, ALREADY_RUNNING)
    assert submit_scrape(app, search_id=2, user_id=3) == (True, STARTED)

    app._scrape_futures[2].result(timeout=5)
    assert scrape_service.calls == [(2, 3)]


def test_the_same_user_can_have_several_searches_running_at_once(app, scrape_service):
    for search_id in (1, 2, 3):
        assert submit_scrape(app, search_id=search_id, user_id=3) == (True, STARTED)

    for search_id in (1, 2, 3):
        app._scrape_futures[search_id].result(timeout=5)

    assert sorted(scrape_service.calls) == [(1, 3), (2, 3), (3, 3)]


# ---------------------------------------------------------------------------
# Concurrence réelle
# ---------------------------------------------------------------------------


def test_exactly_one_of_many_concurrent_threads_gets_to_submit(app, scrape_service):
    """Contention réelle sur la séquence lire-puis-soumettre.

    Cinq threads appellent `submit_scrape` pour la MÊME recherche, synchronisés
    par une barrière pour arriver ensemble sur le verrou. Le job soumis reste
    bloqué sur `gate` : son future ne peut donc pas passer à `done()` pendant
    que les autres threads regardent, ce qui rend le résultat déterministe sans
    aucun `sleep`.

    L'ancien test (tests/_legacy/test_security.py) pouvait passer À VIDE : il
    n'affirmait que `submitted_count == 1`, ce qui reste vrai si quatre threads
    sur cinq n'ont jamais rien ajouté à `results` (join en timeout). D'où
    l'assertion sur `len(results)`, qui vérifie d'abord que les cinq threads
    ont bel et bien traversé la fonction.
    """
    thread_count = 5
    scrape_service.gate = threading.Event()
    barrier = threading.Barrier(thread_count)
    results: list[tuple[bool, str]] = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait(timeout=5)
        outcome = submit_scrape(app, search_id=1, user_id=3)
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, name=f"submitter-{i}") for i in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not [t.name for t in threads if t.is_alive()], "un thread est resté bloqué"
    # Sans cette ligne, tout le reste du test serait satisfait par des threads
    # qui n'ont jamais appelé submit_scrape.
    assert len(results) == thread_count

    assert [ok for ok, _ in results].count(True) == 1
    assert [msg for ok, msg in results if not ok] == [ALREADY_RUNNING] * (thread_count - 1)
    # Le job n'a été exécuté qu'une fois, pas seulement soumis une fois.
    assert scrape_service.calls == [(1, 3)]
    assert len(app._scrape_futures) == 1

    scrape_service.gate.set()
    app._scrape_futures[1].result(timeout=5)


def test_concurrent_submissions_for_distinct_searches_all_go_through(app, scrape_service):
    """Le verrou sérialise mais ne refuse pas : N recherches distinctes
    soumises en parallèle donnent N soumissions, pas une."""
    search_ids = list(range(1, 6))
    scrape_service.gate = threading.Event()
    barrier = threading.Barrier(len(search_ids))
    results: dict[int, tuple[bool, str]] = {}
    results_lock = threading.Lock()

    def worker(search_id):
        barrier.wait(timeout=5)
        outcome = submit_scrape(app, search_id, 3)
        with results_lock:
            results[search_id] = outcome

    threads = [threading.Thread(target=worker, args=(sid,)) for sid in search_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert set(results) == set(search_ids)
    assert all(outcome == (True, STARTED) for outcome in results.values())
    assert set(app._scrape_futures) == set(search_ids)

    scrape_service.gate.set()
    for search_id in search_ids:
        app._scrape_futures[search_id].result(timeout=10)
    assert sorted(scrape_service.calls) == [(sid, 3) for sid in search_ids]


# ---------------------------------------------------------------------------
# Vue file d'attente (#17) — file_dattente
# ---------------------------------------------------------------------------


class TestFileDAttente:
    """Le snapshot lu par le fragment admin « File d'attente des scrapes ».

    Contrats : rangs séquentiels selon l'ordre de soumission (executor
    max_workers=1 ⇒ FIFO), âges en secondes depuis la soumission, marquage
    des futures terminés, purge mémoire des entrées disparues — et JAMAIS
    d'exception ni d'accès base (le fragment est pollé toutes les 5 s).
    """

    def test_an_empty_queue_is_an_empty_list(self, app):
        assert file_dattente(app) == []

    def test_pending_futures_are_ranked_in_submission_order(self, app, scrape_service):
        """Rang 1 = scrape présumé en cours, rangs suivants = en attente :
        c'est exactement ce que produit max_workers=1 (exécution FIFO).
        La gate maintient les trois jobs vivants pendant la lecture."""
        scrape_service.gate = threading.Event()
        submit_scrape(app, search_id=7, user_id=3)
        submit_scrape(app, search_id=9, user_id=3)
        submit_scrape(app, search_id=2, user_id=3)

        entrees = file_dattente(app)

        assert [e["search_id"] for e in entrees] == [7, 9, 2]
        assert [e["rang"] for e in entrees] == [1, 2, 3]
        assert all(not e["termine"] for e in entrees)

    def test_a_terminated_future_is_flagged_not_dropped(self, app, scrape_service):
        """Un future terminé mais pas encore purgé de _scrape_futures reste
        visible avec termine=True : la vue peut afficher sa trace sans le
        compter comme actif."""
        submit_scrape(app, search_id=7, user_id=3)  # tourne puis se termine…
        app._scrape_futures[7].result(timeout=5)
        submit_scrape(app, search_id=9, user_id=3)  # …et 9 part derrière

        entrees = file_dattente(app)

        par_id = {e["search_id"]: e for e in entrees}
        assert par_id[7]["termine"] is True
        assert par_id[9]["termine"] is False
        assert par_id[9]["rang"] == 2, "le rang reflète l'ordre du dict, pas l'activité"

    def test_the_age_counts_seconds_since_submission(self, app, scrape_service, monkeypatch):
        """age_s est calculé sur time.monotonic (insensible aux changements
        d'horloge système), capturé AU MOMENT de la soumission."""
        horloge = [1000.0]
        monkeypatch.setattr("core.scrape_control.time.monotonic", lambda: horloge[0])

        submit_scrape(app, search_id=7, user_id=3)
        assert file_dattente(app)[0]["age_s"] == 0

        horloge[0] += 42.6
        assert file_dattente(app)[0]["age_s"] == 42

    def test_a_future_submitted_before_this_feature_has_a_zero_age(self, app):
        """Compat : un future présent dans _scrape_futures sans timestamp
        (déployé avant #17, ou injecté par un tiers) ne fait pas lever le
        snapshot — il affiche un âge neutre."""
        app._scrape_futures[11] = _pending_future()

        entree = file_dattente(app)[0]

        assert entree["search_id"] == 11
        assert entree["age_s"] == 0
        assert entree["termine"] is False

    def test_timestamps_of_purged_futures_are_cleaned_up(self, app, scrape_service):
        """Sans purge, _soumissions croîtrait pour toute la vie du process :
        chaque recherche jamais scrapée y laisserait une entrée."""
        submit_scrape(app, search_id=7, user_id=3)
        app._scrape_futures[7].result(timeout=5)
        del app._scrape_futures[7]

        file_dattente(app)

        assert 7 not in core_scrape_control._soumissions

    def test_the_snapshot_never_touches_storage_or_raises(self, app, scrape_service):
        """Le fragment est pollé toutes les 5 s : aucun accès base, aucune
        exception possible même sur un état interne inattendu."""
        app._scrape_futures[13] = None  # valeur absurde : le code doit survivre

        entrees = file_dattente(app)

        assert [e["search_id"] for e in entrees] == [13]
        assert entrees[0]["termine"] is False

    def test_manual_submission_works_while_the_scheduler_is_paused(self, app, scrape_service):
        """Critère #17 : la pause globale filtre le TICK planifié (main.py),
        pas les soumissions manuelles — submit_scrape ne consulte AUCUN
        réglage, même quand la clé vaut 'true'."""
        from unittest.mock import MagicMock

        app.storage = MagicMock()
        app.storage.settings.get_setting.return_value = "true"  # pause ACTIVE

        submitted, message = submit_scrape(app, search_id=21, user_id=4)

        assert (submitted, message) == (True, STARTED)
        assert scrape_service.calls == [(21, 4)]
        app.storage.settings.get_setting.assert_not_called(), (
            "la soumission manuelle ne doit même pas lire l'état de pause"
        )
