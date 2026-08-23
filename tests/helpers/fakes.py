"""Doubles de test : storage, notifier et connexion Postgres.

Deux niveaux, à choisir selon ce qu'on teste :

* `fake_storage()` — un `MagicMock` calqué sur `Storage` (mêmes repositories,
  mêmes méthodes). Pour les routes et les services : on vérifie les
  interactions, pas le SQL.
* `RecordingConnection` — une fausse connexion psycopg2 qui enregistre le SQL
  exécuté et rejoue des résultats programmés. Pour tester les *constructeurs de
  requêtes* (`_build_filter_clauses`, `_build_order_clause`) et la gestion des
  connexions, sans base — le SQL réel est validé par tests/integration/.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import psycopg2.extensions

from notifier import Notifier
from repositories.admin_repo import AdminRepository
from repositories.bienici_geo_repo import BienIciGeoRepository
from repositories.century21_geo_repo import Century21GeoRepository
from repositories.foncia_geo_repo import FonciaGeoRepository
from repositories.guyhoquet_geo_repo import GuyHoquetGeoRepository
from repositories.listing_repo import ListingRepository
from repositories.orpi_geo_repo import OrpiGeoRepository
from repositories.pap_geo_repo import PapGeoRepository
from repositories.scrape_log_repo import ScrapeLogRepository
from repositories.search_repo import SearchRepository
from repositories.seloger_geo_repo import SelogerGeoRepository
from repositories.settings_repo import SettingsRepository
from repositories.user_repo import UserRepository
from storage import Storage

_REPOSITORIES = {
    "users": UserRepository,
    "searches": SearchRepository,
    "listings": ListingRepository,
    "scrape_logs": ScrapeLogRepository,
    "admin": AdminRepository,
    "settings": SettingsRepository,
    "seloger_geo": SelogerGeoRepository,
    "bienici_geo": BienIciGeoRepository,
    "century21_geo": Century21GeoRepository,
    "foncia_geo": FonciaGeoRepository,
    "guyhoquet_geo": GuyHoquetGeoRepository,
    "orpi_geo": OrpiGeoRepository,
    "pap_geo": PapGeoRepository,
}


def fake_storage(**repo_overrides) -> MagicMock:
    """`Storage` doublé, avec un mock spécifié par repository.

    `spec=` garantit qu'un appel à une méthode inexistante lève au lieu de
    renvoyer un mock complaisant : si une méthode de repo est renommée, les
    tests qui l'appellent échouent au lieu de continuer à « passer ».

    Les valeurs de retour par défaut sont neutres et sûres (`None` / `[]` / 0)
    pour que les routes de lecture ne plantent pas sans configuration.
    """
    storage = MagicMock(spec=Storage)
    storage.database_url = "postgresql://fake/fake"

    for name, repo_cls in _REPOSITORIES.items():
        setattr(storage, name, MagicMock(spec=repo_cls))

    storage.searches.get_search.return_value = None
    storage.searches.get_user_searches.return_value = []
    storage.searches.get_all_searches.return_value = []
    storage.users.get_user_by_id.return_value = None
    storage.users.get_user_by_username.return_value = None
    storage.users.get_all_users.return_value = []
    storage.listings.get_listings_for_search.return_value = []
    storage.listings.count_listings_for_search.return_value = 0
    storage.listings.get_unnotified_listings_for_search.return_value = []
    storage.listings.get_filter_options.return_value = {}
    storage.listings.save_and_link.return_value = ([], [])
    storage.scrape_logs.get_scrape_logs.return_value = []
    storage.scrape_logs.count_scrape_logs.return_value = 0
    storage.scrape_logs.get_scrape_stats.return_value = {}
    storage.settings.get_setting.return_value = ""
    storage.seloger_geo.get_cached.return_value = None
    storage.bienici_geo.get_cached.return_value = None
    storage.century21_geo.get_cached.return_value = None
    storage.foncia_geo.get_cached.return_value = None
    storage.guyhoquet_geo.get_cached.return_value = None
    storage.orpi_geo.get_cached.return_value = None
    storage.pap_geo.get_cached.return_value = None

    for name, value in repo_overrides.items():
        setattr(storage, name, value)
    return storage


def fake_notifier(send_result: bool = True) -> MagicMock:
    """`Notifier` doublé. `send_result` pilote la valeur de retour des envois.

    Ce booléen est *porteur de sens* : `ScrapeService` ne marque une annonce
    notifiée que si l'envoi a renvoyé `True`.
    """
    notifier = MagicMock(spec=Notifier)
    notifier.send.return_value = send_result
    notifier.notify_new_listing.return_value = send_result
    notifier.notify_summary.return_value = send_result
    notifier.send_test.return_value = send_result
    return notifier


class RecordingCursor:
    """Curseur psycopg2 minimal qui enregistre le SQL et rejoue des résultats.

    `results` est consommée dans l'ordre : un élément par `execute()` qui sera
    suivi d'un `fetchone`/`fetchall`. Un élément `None` signifie « pas de
    résultat » (`cur.description` vaut alors None, comme pour un INSERT).
    """

    def __init__(self, results: list | None = None, recorder: list | None = None, dict_mode: bool = False):
        self._results = list(results or [])
        self._recorder = recorder if recorder is not None else []
        self._current: list | None = None
        self._dict_mode = dict_mode
        self.rowcount = 0
        self.description = None
        self.closed = False

    def _shape(self, row):
        """Adapte la ligne au type de curseur demandé.

        Les tests écrivent leurs résultats en dict (lisible, robuste à l'ordre
        des colonnes). Un curseur ordinaire renvoie des tuples : on convertit,
        pour que `row[0]` fonctionne comme avec psycopg2. Un `RealDictCursor`
        reçoit le dict tel quel.
        """
        if isinstance(row, dict) and not self._dict_mode:
            return tuple(row.values())
        return row

    @property
    def executed(self) -> list[tuple[str, object]]:
        """Liste des `(sql, params)` exécutés, dans l'ordre."""
        return self._recorder

    @property
    def sql(self) -> list[str]:
        """SQL seul, pour les assertions de contenu."""
        return [sql for sql, _ in self._recorder]

    def execute(self, sql, params=None):
        self._recorder.append((sql, params))
        self._current = self._results.pop(0) if self._results else None
        if self._current is None:
            self.description = None
            self.rowcount = 0
        else:
            rows = self._current if isinstance(self._current, list) else [self._current]
            self.description = [("col",)]
            self.rowcount = len(rows)

    def fetchone(self):
        if self._current is None:
            return None
        row = self._current[0] if isinstance(self._current, list) else self._current
        return self._shape(row)

    def fetchall(self):
        if self._current is None:
            return []
        rows = list(self._current) if isinstance(self._current, list) else [self._current]
        return [self._shape(row) for row in rows]

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class RecordingConnection:
    """Fausse connexion psycopg2 : curseurs enregistreurs + suivi commit/rollback."""

    def __init__(self, results: list | None = None, transaction_status: int | None = None):
        self._results = list(results or [])
        self.executed: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.cursors: list[RecordingCursor] = []
        self._transaction_status = (
            psycopg2.extensions.TRANSACTION_STATUS_IDLE if transaction_status is None else transaction_status
        )

    def cursor(self, *args, **kwargs):
        # `_dict_cursor()` passe cursor_factory=RealDictCursor : les lignes
        # doivent alors rester des dicts.
        cur = RecordingCursor(
            results=self._results,
            recorder=self.executed,
            dict_mode=kwargs.get("cursor_factory") is not None,
        )
        self.cursors.append(cur)
        return cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True

    def get_transaction_status(self):
        return self._transaction_status

    @property
    def sql(self) -> list[str]:
        return [sql for sql, _ in self.executed]


def bind_repository(repo_cls, conn: RecordingConnection):
    """Instancie un repository branché sur `conn`, sans pool ni base.

    Passe par `__init__` (contrairement à un `__new__` nu, qui masquerait toute
    initialisation future) puis redirige les emprunts de connexion.
    """
    repo = repo_cls("postgresql://fake/fake")
    repo._get_conn = lambda: conn
    repo._get_conn_for_request = lambda: conn
    repo._get_ddl_conn = lambda: conn
    repo._release_conn = lambda c: None
    repo.release_to_pool = lambda c: None
    return repo
