"""Stats par utilisateur (#22) : `get_user_stats` et `get_dernier_scrape_resultat`.

Deux sources de données très différentes, testées chacune à sa façon :

* `get_user_stats` (SQL agrégé) est branché sur une `RecordingConnection` :
  on vérifie le SQL construit — les fenêtres 7 j / 30 j doivent être évaluées
  PAR POSTGRES, pas en Python — et le dédoublonnage des deux vocabulaires de
  sources. Le SQL réel est validé par tests/integration/.
* `get_dernier_scrape_resultat` lit les JSONL par recherche : `read_entries`
  est doublé au niveau du module repo pour piloter les entrées sans fichiers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from repositories.scrape_log_repo import ScrapeLogRepository
from repositories.user_repo import UserRepository
from tests.helpers.fakes import bind_repository

USER_ID = 7


class RecordingConnectionStub:
    """Connexion factice minimale : une seule instance curseur qui rejoue
    `results` dans l'ordre des execute(). Suffisant ici : get_user_stats fait
    exactement deux requêtes, toutes les deux en dict cursor."""

    def __init__(self):
        self.executed: list[tuple[str, object]] = []
        self._results: list = []

    def programmer(self, results: list) -> None:
        self._results = list(results)

    def cursor(self, *args, **kwargs):
        return _StubCursor(self.executed, self._results)

    def commit(self):
        pass


class _StubCursor:
    def __init__(self, executed, results):
        self._executed = executed
        self._results = results
        self._current = None
        self.description = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._executed.append((sql, params))
        self._current = self._results.pop(0) if self._results else [{}]

    def fetchone(self):
        return self._current[0] if isinstance(self._current, list) else self._current

    def fetchall(self):
        return list(self._current) if isinstance(self._current, list) else [self._current]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestGetUserStats:
    @pytest.fixture
    def repo_et_conn(self):
        conn = RecordingConnectionStub()
        repo = bind_repository(UserRepository, conn)
        return repo, conn

    def test_une_seule_requete_agregee_puis_les_sources(self, repo_et_conn):
        """Deux exécutions SQL au total : les compteurs d'un coup, puis le
        distinct des sources. Jamais un aller-retour par recherche."""
        repo, conn = repo_et_conn
        conn.programmer(
            [
                [{
                    "recherches_total": 3,
                    "recherches_actives": 2,
                    "recherches_inactives": 1,
                    "annonces_7j": 4,
                    "annonces_30j": 11,
                }],
                [{"src": "seloger"}, {"src": "laforet"}],
            ]
        )

        stats = repo.get_user_stats(USER_ID)

        assert len(conn.executed) == 2
        assert stats == {
            "recherches_total": 3,
            "recherches_actives": 2,
            "recherches_inactives": 1,
            "annonces_7j": 4,
            "annonces_30j": 11,
            "sources_utilisees": ["seloger", "laforet"],
        }

    def test_le_compte_des_recherches_est_scope_par_utilisateur(self, repo_et_conn):
        repo, conn = repo_et_conn
        conn.programmer([[{"recherches_total": 0, "recherches_actives": 0,
                           "recherches_inactives": 0, "annonces_7j": 0, "annonces_30j": 0}], []])

        repo.get_user_stats(USER_ID)

        sql_stats, params_stats = conn.executed[0]
        # Chaque sous-requête scalaire porte le même user_id lié (jamais une
        # interpolation de chaîne) : 5 occurrences pour 5 sous-requêtes.
        assert sql_stats.count("user_id") == 5
        assert params_stats == (USER_ID,) * 5

    def test_les_fenetres_7j_et_30j_sont_en_sql_postgres(self, repo_et_conn):
        """Les bornes temporelles vivent dans la requête (CURRENT_TIMESTAMP -
        INTERVAL), évaluées par la base : ni calcul Python ni paramètre date."""
        repo, conn = repo_et_conn
        conn.programmer([[{"recherches_total": 0, "recherches_actives": 0,
                           "recherches_inactives": 0, "annonces_7j": 0, "annonces_30j": 0}], []])

        repo.get_user_stats(USER_ID)

        sql_stats, _params = conn.executed[0]
        assert "INTERVAL '7 days'" in sql_stats
        assert "INTERVAL '30 days'" in sql_stats
        # Les annonces sont comptées sur search_listings.found_at (#22),
        # joint aux recherches du bon utilisateur.
        assert "search_listings" in sql_stats
        assert "found_at" in sql_stats

    def test_actif_inactif_suivent_la_convention_python(self, repo_et_conn):
        """`COALESCE(is_active, TRUE)` : NULL vaut actif, comme
        `searches.get('is_active', True)` côté Python — et l'inactif est son
        complément exact (`NOT COALESCE`), jamais un troisième compte."""
        repo, conn = repo_et_conn
        conn.programmer([[{"recherches_total": 0, "recherches_actives": 0,
                           "recherches_inactives": 0, "annonces_7j": 0, "annonces_30j": 0}], []])

        repo.get_user_stats(USER_ID)

        sql_stats, _params = conn.executed[0]
        assert "COALESCE(is_active, TRUE)" in sql_stats
        assert "NOT COALESCE(is_active, TRUE)" in sql_stats

    def test_les_sources_union_jsonb_et_colonne_legacy(self, repo_et_conn):
        """Les recherches récentes portent `sources` (JSONB), les anciennes la
        colonne legacy `source` : l'UNION dédoublonne les deux vocabulaires."""
        repo, conn = repo_et_conn
        conn.programmer([[{"recherches_total": 0, "recherches_actives": 0,
                           "recherches_inactives": 0, "annonces_7j": 0, "annonces_30j": 0}], []])

        repo.get_user_stats(USER_ID)

        sql_sources, params_sources = conn.executed[1]
        assert "jsonb_array_elements_text(sources)" in sql_sources
        assert "SELECT source AS src" in sql_sources
        assert "UNION" in sql_sources
        assert params_sources == (USER_ID, USER_ID)


class TestGetDernierScrapeResultat:
    @pytest.fixture
    def repo(self, monkeypatch):
        entrees_par_recherche: dict[int, list[dict]] = {}

        def fake_read_entries(search_id):
            return entrees_par_recherche.get(search_id, [])

        monkeypatch.setattr("repositories.scrape_log_repo.read_entries", fake_read_entries)
        repo = ScrapeLogRepository("postgresql://fake/fake")
        return repo, entrees_par_recherche

    def test_aucun_log_renvoie_deux_fois_none(self, repo):
        sc_repo, entrees = repo

        resultat = sc_repo.get_dernier_scrape_resultat([1, 2])

        assert resultat == {"dernier_succes": None, "dernier_echec": None}

    def test_liste_vide_renvoie_deux_fois_none(self, repo):
        sc_repo, _entrees = repo

        resultat = sc_repo.get_dernier_scrape_resultat([])

        assert resultat == {"dernier_succes": None, "dernier_echec": None}

    def test_le_plus_recent_gagne_toutes_recherches_confondues(self, repo):
        """Les recherches sont balayées dans l'ordre donné : le dernier scrape
        réussi/échec doit quand même être LE plus récent global."""
        sc_repo, entrees = repo
        vieux_succes = datetime(2026, 8, 10, 8, 0)
        recent_echec = datetime(2026, 8, 20, 9, 0)
        entrees[3] = [{"status": "success", "started_at": vieux_succes}]
        entrees[4] = [{"status": "error", "started_at": recent_echec}]

        resultat = sc_repo.get_dernier_scrape_resultat([3, 4])

        assert resultat["dernier_succes"] == vieux_succes
        assert resultat["dernier_echec"] == recent_echec

    def test_empty_n_est_ni_un_succes_ni_un_echec(self, repo):
        """Un scrape « empty » a tourné et légitimement rien trouvé : même
        convention que get_scrape_stats, il ne compte dans aucune carte."""
        sc_repo, entrees = repo
        entrees[1] = [
            {"status": "empty", "started_at": datetime(2026, 8, 21, 10, 0)},
            {"status": "success", "started_at": datetime(2026, 8, 20, 10, 0)},
            {"status": "error", "started_at": datetime(2026, 8, 19, 10, 0)},
        ]

        resultat = sc_repo.get_dernier_scrape_resultat([1])

        assert resultat["dernier_succes"] == datetime(2026, 8, 20, 10, 0)
        assert resultat["dernier_echec"] == datetime(2026, 8, 19, 10, 0)

    def test_une_entree_sans_timestamp_valide_est_ignoree(self, repo):
        """Une entrée boiteuse (started_at absent ou non-datetime) ne doit ni
        gagner le classement ni lever."""
        sc_repo, entrees = repo
        succes = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        entrees[1] = [
            {"status": "success", "started_at": "pas-une-date"},
            {"status": "error", "started_at": None},
            {"status": "success", "started_at": succes},
        ]

        resultat = sc_repo.get_dernier_scrape_resultat([1])

        assert resultat["dernier_succes"] == succes
        assert resultat["dernier_echec"] is None
