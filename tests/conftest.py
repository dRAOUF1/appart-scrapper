"""Socle de la suite de tests : garde-fous globaux et fixtures partagées.

Trois garde-fous sont *autouse* et non désactivables sans marqueur explicite.
Ils existent parce que chacun correspond à un incident réel de ce dépôt :

1. `DATABASE_URL` est purgé de l'environnement pour tout le run. Cette variable
   pointe la PRODUCTION et peut arriver dans `os.environ` par simple effet de
   bord d'import (un `load_dotenv()` au niveau module). Les tests d'intégration
   lisent exclusivement `TEST_DATABASE_URL` — voir tests/integration/conftest.py.
2. Les répertoires de logs sont redirigés vers `tmp_path`. `scrape_logs/` écrit
   et **supprime** dans `logs/` à la racine du dépôt (données réelles), et
   `ScrapeService.execute()` appelle `cleanup_old_logs()`.
3. Le réseau et Postgres sont coupés pour tout test non marqué. Un test unitaire
   qui joint une vraie base ou un vrai site échoue ici, par construction.

Marqueurs d'échappement, à n'utiliser qu'avec une raison écrite :
    @pytest.mark.integration   — accès Postgres autorisé (tests/integration/)
    @pytest.mark.real_sleep    — `time.sleep` non neutralisé (tests de concurrence)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 1. Aucun test ne doit voir DATABASE_URL
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent


def pytest_configure(config):
    os.environ.pop("DATABASE_URL", None)


def pytest_collection_finish(session):
    """Re-purge après collecte : un import de test peut avoir appelé load_dotenv()."""
    os.environ.pop("DATABASE_URL", None)


def pytest_collection_modifyitems(config, items):
    """Marque automatiquement les tests selon leur répertoire."""
    functional_dir = _THIS_DIR / "functional"
    unit_dir = _THIS_DIR / "unit"
    for item in items:
        path = Path(str(item.fspath)).resolve()
        if path.is_relative_to(functional_dir):
            item.add_marker(pytest.mark.functional)
        elif path.is_relative_to(unit_dir):
            item.add_marker(pytest.mark.unit)


@pytest.fixture(autouse=True, scope="session")
def _no_production_database_url():
    os.environ.pop("DATABASE_URL", None)


# ---------------------------------------------------------------------------
# 2. Le filesystem de logs vit dans tmp_path, jamais dans le dépôt
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def log_dirs(tmp_path, monkeypatch):
    """Redirige toute l'arborescence de logs vers tmp_path.

    `scrape_logs.exporter` importe `SCRAPE_LOGS_DIR`/`EXPORTS_DIR` *par valeur*
    (`from ... import ...`) : patcher le module `storage` ne suffit pas, il faut
    aussi patcher les copies liées dans `exporter`. Même chose pour
    `manager.LOGS_DIR`, qui est une constante distincte.

    Renvoie le chemin racine, pour les tests qui veulent inspecter les fichiers.
    """
    from scrape_logs import exporter as exporter_mod
    from scrape_logs import manager as manager_mod
    from scrape_logs import storage as storage_mod

    logs_dir = tmp_path / "logs"
    scrape_logs_dir = logs_dir / "scrape_logs"
    exports_dir = scrape_logs_dir / "exports"
    scrape_logs_dir.mkdir(parents=True)

    monkeypatch.setattr(storage_mod, "LOGS_DIR", str(logs_dir))
    monkeypatch.setattr(storage_mod, "SCRAPE_LOGS_DIR", str(scrape_logs_dir))
    monkeypatch.setattr(storage_mod, "EXPORTS_DIR", str(exports_dir))
    monkeypatch.setattr(storage_mod, "COUNTER_FILE", str(scrape_logs_dir / "counter.json"))

    monkeypatch.setattr(exporter_mod, "SCRAPE_LOGS_DIR", str(scrape_logs_dir))
    monkeypatch.setattr(exporter_mod, "EXPORTS_DIR", str(exports_dir))
    monkeypatch.setattr(exporter_mod, "IMPORT_LOG_FILE", str(scrape_logs_dir / "imports.log"))

    monkeypatch.setattr(manager_mod, "LOGS_DIR", str(logs_dir))

    return logs_dir


# ---------------------------------------------------------------------------
# 3. Ni réseau ni Postgres pour les tests non marqués
# ---------------------------------------------------------------------------

class NetworkAccessAttempted(AssertionError):
    """Levée quand un test tente une vraie requête HTTP."""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Coupe le transport HTTP réel.

    Patché au niveau de `HTTPAdapter.send`, donc `requests.get`, `requests.post`
    et `requests.Session` sont tous couverts — mais pas `requests_mock`, qui
    monte son propre adaptateur : les tests qui simulent des réponses
    fonctionnent normalement.
    """
    import requests.adapters

    def _blocked(self, request, *args, **kwargs):
        raise NetworkAccessAttempted(
            f"Appel réseau réel bloqué : {request.method} {request.url}. "
            "Utilisez requests_mock ou un double, ou injectez une session factice."
        )

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _blocked)


@pytest.fixture(autouse=True)
def no_real_database(request, monkeypatch):
    """Interdit toute connexion Postgres hors tests marqués `integration`.

    C'est ce qui rend la séparation unit/integration structurelle plutôt que
    déclarative : un test unitaire qui oublie un double ne passe pas au vert en
    tapant une vraie base.
    """
    if request.node.get_closest_marker("integration"):
        return

    import psycopg2
    import psycopg2.pool

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "Connexion Postgres réelle bloquée dans un test non-integration. "
            "Doublez `storage`/le repository, ou marquez le test "
            "@pytest.mark.integration et placez-le dans tests/integration/."
        )

    monkeypatch.setattr(psycopg2, "connect", _blocked)
    monkeypatch.setattr(psycopg2.pool, "ThreadedConnectionPool", _blocked)


@pytest.fixture(autouse=True)
def no_real_sleep(request, monkeypatch):
    """Neutralise `time.sleep` et enregistre les durées demandées.

    Le pipeline dort pour de vrai (0,3 s par notification dans ScrapeService,
    backoff exponentiel dans le scraper). Aucun test ne doit payer ça — et
    plusieurs veulent au contraire *affirmer* le throttling : la liste des
    durées est exposée via la fixture `slept`.

    Neutralisé par `@pytest.mark.real_sleep` pour les tests de concurrence.
    """
    if request.node.get_closest_marker("real_sleep"):
        return None

    import time

    recorded: list[float] = []
    real_sleep = time.sleep

    def _fake_sleep(seconds):
        recorded.append(seconds)
        # Laisse le GIL tourner sans coûter de temps mural.
        real_sleep(0)

    monkeypatch.setattr(time, "sleep", _fake_sleep)
    return recorded


@pytest.fixture
def slept(no_real_sleep):
    """Liste des durées passées à `time.sleep`, dans l'ordre."""
    assert no_real_sleep is not None, "test marqué real_sleep : rien n'est enregistré"
    return no_real_sleep


# ---------------------------------------------------------------------------
# 4. Reset des états globaux (caches process, registry, pools)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_global_state():
    """Vide tous les caches et registres de niveau module entre deux tests.

    Sans ça, l'ordre d'exécution devient significatif : les caches géo de
    `core.geocode` sont mémoïsés pour la vie du process (y compris les `None`),
    `ParserRegistry._parsers` est un dict de classe qu'une sous-classe de test
    pollue définitivement, et `BaseRepository._pools` garde des connexions.
    """
    from core import geocode
    from parsers.base import ParserRegistry
    from repositories.base import BaseRepository
    from scrape_logs import storage as log_storage
    from services import poi_overpass

    parsers_snapshot = dict(ParserRegistry._parsers)

    for cache in (
        geocode._INSEE_CACHE,
        geocode._REGION_DEPARTMENTS_CACHE,
        geocode._DEPARTMENT_MAIN_CITY_CACHE,
    ):
        cache.clear()
    log_storage._LOCKS.clear()
    poi_overpass.vider_cache()

    yield

    for cache in (
        geocode._INSEE_CACHE,
        geocode._REGION_DEPARTMENTS_CACHE,
        geocode._DEPARTMENT_MAIN_CITY_CACHE,
    ):
        cache.clear()
    log_storage._LOCKS.clear()
    poi_overpass.vider_cache()
    ParserRegistry._parsers.clear()
    ParserRegistry._parsers.update(parsers_snapshot)
    BaseRepository._pools.clear()


@pytest.fixture(autouse=True)
def reset_proxy_cache(monkeypatch):
    """Isole le cache de proxies du scraper SeLoger (TTL 180 s, partagé)."""
    from scraper import seloger as scraper_seloger

    monkeypatch.setattr(scraper_seloger, "_PROXY_CACHE", [], raising=False)
    monkeypatch.setattr(scraper_seloger, "_PROXY_CACHE_TIME", 0, raising=False)


@pytest.fixture(autouse=True)
def clean_loguru_handlers():
    """Détache les sinks loguru laissés en place par un test.

    `SearchLogManager.start()` fait un `logger.add(...)` global et
    `ScrapeService` ne le retire que sur le chemin de succès : sans ce filet, un
    test qui échoue laisse un handler écrire dans un tmp_path déjà supprimé.
    """
    from loguru import logger

    before = set(logger._core.handlers)
    yield
    for handler_id in set(logger._core.handlers) - before:
        try:
            logger.remove(handler_id)
        except ValueError:
            pass
