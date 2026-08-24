"""Tests unitaires de l'onglet Système (#21) — les pièces pures.

Ce que ce module prouve, pièce par pièce :

1. `_sha_deploye` — la chaîne RENDER_GIT_COMMIT → GIT_COMMIT → 'local', avec
   le cas « variable vide compte pour absente » ;
2. uptime / durée / RSS / âge — formatage et fallbacks SANS crash (l'onglet
   est pollé toutes les 30 s : aucune lecture périphérique ne peut lever) ;
3. `core.scrape_control.enregistrer_tick` / `dernier_tick` — l'état du
   dernier passage du scheduler, thread-safe, snapshot sans fuite interne ;
4. `repositories.base.statistiques_pool` — lecture pure des compteurs du
   pool partagé, None quand la structure n'est pas disponible ;
5. `AdminRepository.get_geo_cache_stats` / `purge_geo_cache` — LES CLAUSES :
   un COUNT par table du registre avec le bon nom de colonne, une purge
   allowlistée qui refuse AVANT toute base ce qui n'est pas au registre.

Le SQL réel contre un vrai Postgres est couvert en intégration
(tests/integration/test_admin_systeme_ops.py) — pas de duplication.
"""

from __future__ import annotations

import threading

import pytest

import core.scrape_control as core_scrape_control
from core.scrape_control import dernier_tick, enregistrer_tick
from repositories.admin_repo import CACHES_GEO, AdminRepository
from repositories.base import BaseRepository, statistiques_pool
from routes.admin import (
    _format_age,
    _format_duree,
    _format_mo,
    _memoire_rss_ko,
    _sha_deploye,
    _uptime_secondes,
)
from tests.helpers.fakes import RecordingConnection, bind_repository

# ---------------------------------------------------------------------------
# SHA déployé
# ---------------------------------------------------------------------------

class TestShaDeploye:
    def test_render_git_commit_gagne(self, monkeypatch):
        monkeypatch.setenv("RENDER_GIT_COMMIT", "abc123def4567890")
        monkeypatch.setenv("GIT_COMMIT", "zzz")

        assert _sha_deploye() == "abc123def4567890"

    def test_git_commit_est_le_second_choix(self, monkeypatch):
        monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
        monkeypatch.setenv("GIT_COMMIT", "fedcba9876543210")

        assert _sha_deploye() == "fedcba9876543210"

    def test_sans_variable_le_fallback_est_local(self, monkeypatch):
        monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
        monkeypatch.delenv("GIT_COMMIT", raising=False)

        assert _sha_deploye() == "local"

    @pytest.mark.parametrize("cle", ["RENDER_GIT_COMMIT", "GIT_COMMIT"])
    def test_une_variable_vide_compte_pour_absente(self, monkeypatch, cle):
        """Un déploiement qui exporte une variable vide ne doit pas afficher
        un SHA fantôme."""
        monkeypatch.setenv(cle, "   ")
        autre = "GIT_COMMIT" if cle == "RENDER_GIT_COMMIT" else "RENDER_GIT_COMMIT"
        monkeypatch.delenv(autre, raising=False)

        assert _sha_deploye() == "local"


# ---------------------------------------------------------------------------
# Uptime, durées, RSS
# ---------------------------------------------------------------------------

class TestUptimeEtDurees:
    def test_uptime_calcule_depuis_le_boot_monotonic(self):
        import time as module_time

        class App:
            config = {"BOOT_MONOTONIC": module_time.monotonic() - 90}

        assert _uptime_secondes(App()) == pytest.approx(90, abs=2)

    def test_uptime_sans_boot_dans_la_config_rend_none(self):
        class App:
            config = {}

        assert _uptime_secondes(App()) is None

    @pytest.mark.parametrize(
        ("secondes", "attendu"),
        [
            pytest.param(42, "00:00:42", id="secondes"),
            pytest.param(3725, "01:02:05", id="heures"),
            pytest.param(2 * 86400 + 3600 + 60 + 1, "2 j 01:01:01", id="jours"),
            pytest.param(None, None, id="aucune_mesure"),
            pytest.param(-5, "00:00:00", id="negatif_borne_a_zero"),
        ],
    )
    def test_le_format_de_duree_est_lisible(self, secondes, attendu):
        assert _format_duree(secondes) == attendu

    @pytest.mark.parametrize(
        ("secondes", "attendu"),
        [
            pytest.param(0, "il y a 0 s", id="instantane"),
            pytest.param(59, "il y a 59 s", id="sous_la_minute"),
            pytest.param(60, "il y a 1 min", id="minute"),
            pytest.param(3599, "il y a 59 min", id="sous_l_heure"),
            pytest.param(7200, "il y a 2 h", id="heures"),
            pytest.param(None, None, id="sans_tick"),
        ],
    )
    def test_le_format_dage_est_court(self, secondes, attendu):
        assert _format_age(secondes) == attendu


class TestMemoireRss:
    def test_la_mesure_reussit_sur_ce_process(self):
        """Sous Linux (CI/containers), /proc ou resource existent : la mesure
        renvoie un entier positif et se formate en Mo."""
        ko = _memoire_rss_ko()

        assert ko is not None and ko > 0
        assert _format_mo(ko).endswith("Mo")
        assert _format_mo(ko) != "0 Mo"

    def test_toutes_les_voies_ratees_rendent_none_sans_crash(self, monkeypatch):
        """Conteneur sans /proc ni module resource : la carte mémoire doit
        rendre « — », jamais une 500 sur un fragment pollé."""

        def open_impossible(*a, **k):
            raise OSError("pas de /proc ici")

        monkeypatch.setattr("routes.admin.resource", None)
        monkeypatch.setattr("builtins.open", open_impossible)

        assert _memoire_rss_ko() is None
        assert _format_mo(None) is None


# ---------------------------------------------------------------------------
# Dernier tick du scheduler (#21)
# ---------------------------------------------------------------------------

@pytest.fixture
def tick_propre():
    """Isole l'état global du tick entre tests (globale de module partagée)."""
    core_scrape_control._dernier_tick = None
    yield
    core_scrape_control._dernier_tick = None


class TestDernierTick:
    def test_aucun_tick_renvoie_none(self, tick_propre):
        assert dernier_tick() is None

    def test_le_tick_enregistre_statut_detail_et_age(self, tick_propre):
        enregistrer_tick("ok", "Cycle planifié terminé")

        tick = dernier_tick()
        assert tick["statut"] == "ok"
        assert tick["detail"] == "Cycle planifié terminé"
        assert tick["age_s"] >= 0
        assert "_monotonic" not in tick, "le détail interne ne fuit pas dans le snapshot"

    @pytest.mark.parametrize(
        "statut", ["ok", "pause", "erreur"], ids=["ok", "pause", "erreur"]
    )
    def test_les_trois_statuts_passent_tels_quels(self, tick_propre, statut):
        enregistrer_tick(statut, "")

        assert dernier_tick()["statut"] == statut

    def test_un_nouveau_tick_remplace_le_precedent(self, tick_propre):
        enregistrer_tick("ok", "")
        enregistrer_tick("erreur", "pool épuisé")

        tick = dernier_tick()
        assert tick["statut"] == "erreur"
        assert "pool épuisé" in tick["detail"]

    def test_lecture_pendant_ecriture_ne_corrompt_rien(self, tick_propre):
        """La lecture admin peut arriver pendant l'écriture du tick : le
        verrou garantit un snapshot cohérent, jamais un dict à moitié écrit."""
        enregistrer_tick("ok", "")
        erreurs: list[Exception] = []

        def lit():
            try:
                for _ in range(200):
                    tick = dernier_tick()
                    if tick is not None:
                        assert tick["statut"] == "ok"
            except Exception as e:  # pragma: no cover - seulement en échec
                erreurs.append(e)

        thread = threading.Thread(target=lit)
        thread.start()
        for _ in range(200):
            enregistrer_tick("ok", "")
        thread.join()

        assert erreurs == []


# ---------------------------------------------------------------------------
# Statistiques du pool de connexions
# ---------------------------------------------------------------------------

class FauxPool:
    """La surface interne lue par statistiques_pool, rien d'autre."""

    def __init__(self, libres=2, utilisees=3, mn=1, mx=20):
        from threading import Lock

        self._lock = Lock()
        self._pool = list(range(libres))
        self._used = {i: i for i in range(utilisees)}
        self.minconn = mn
        self.maxconn = mx


class TestStatistiquesPool:
    def test_les_compteurs_sont_lus_sans_toucher_au_pool(
        self, monkeypatch
    ):
        monkeypatch.setitem(BaseRepository._pools, "postgresql://fake/pool", FauxPool())

        stats = statistiques_pool("postgresql://fake/pool")

        assert stats == {"min": 1, "max": 20, "libres": 2, "utilisees": 3}

    def test_pool_pas_encree_cree_rend_none(self):
        assert statistiques_pool("postgresql://jamais/emprunte") is None

    def test_une_structure_interne_inattendue_rend_none(self, monkeypatch):
        """Le driver psycopg2 peut changer sa structure interne : la lecture
        doit dégrader en None plutôt que casser le fragment pollé."""
        monkeypatch.setitem(BaseRepository._pools, "postgresql://fake/bizarre", object())

        assert statistiques_pool("postgresql://fake/bizarre") is None


# ---------------------------------------------------------------------------
# Registre des caches géo — les clauses SQL
# ---------------------------------------------------------------------------

def registre_par_defaut() -> tuple:
    """Le registre réel, isolé d'un éventuel enrichissement futur."""
    return CACHES_GEO


class TestRegistreCachesGeo:
    def test_chaque_entree_porte_les_quatre_cles(self):
        for cache in registre_par_defaut():
            assert set(cache) == {"source", "table", "colonne", "suivi_echecs"}, cache

    def test_les_tables_et_sources_sont_uniques(self):
        tables = [c["table"] for c in registre_par_defaut()]
        sources = [c["source"] for c in registre_par_defaut()]

        assert len(tables) == len(set(tables)), "deux entrées pointent la même table"
        assert len(sources) == len(set(sources)), "deux entrées pour la même source"

    def test_les_cinq_sources_de_l_issue_sont_couvertes(self):
        """L'issue #21 nomme cinq caches minimum : ils doivent tous être là."""
        sources = {c["source"] for c in registre_par_defaut()}
        assert {"seloger", "bienici", "century21", "orpi", "pap"} <= sources


class TestGetGeoCacheStatsClauses:
    """Les clauses construites, contre une connexion enregistreuse : le SQL
    réel est validé en intégration."""

    @pytest.fixture
    def repo(self):
        conn = RecordingConnection()
        return bind_repository(AdminRepository, conn), conn

    def test_une_ligne_par_cache_du_registre(self, repo):
        storage_repo, conn = repo
        conn._results = [[{"entrees": 3, "manquees": 1}] for _ in CACHES_GEO]

        stats = storage_repo.get_geo_cache_stats()

        assert [s["table"] for s in stats] == [c["table"] for c in CACHES_GEO]
        assert [s["source"] for s in stats] == [c["source"] for c in CACHES_GEO]

    def test_le_count_interroge_la_table_et_sa_colonne_identifiante(self, repo):
        storage_repo, conn = repo
        conn._results = [[{"entrees": 3, "manquees": 1}] for _ in CACHES_GEO]

        storage_repo.get_geo_cache_stats()

        par_table = dict(zip([c["table"] for c in CACHES_GEO], conn.sql, strict=True))
        assert "COUNT(*) - COUNT(place_id)" in par_table["seloger_place_ids"]
        assert "FROM seloger_place_ids" in par_table["seloger_place_ids"]
        assert "COUNT(slug_id)" in par_table["orpi_geo_ids"]
        assert "COUNT(geo_id)" in par_table["pap_geo_ids"]

    def test_le_cache_sans_echec_memorise_ne_compte_pas_de_colonne(self, repo):
        """commune_centres ne mémorise que des succès : compter une colonne
        y serait du bruit — la requête ne porte qu'un COUNT(*)."""
        storage_repo, conn = repo
        conn._results = [[{"entrees": 40, "manquees": 0}] for _ in CACHES_GEO]

        stats = storage_repo.get_geo_cache_stats()

        communes = next(s for s in stats if s["table"] == "commune_centres")
        sql_communes = conn.sql[[c["table"] for c in CACHES_GEO].index("commune_centres")]
        assert "COUNT(*) - COUNT" not in sql_communes
        assert communes["suivi_echecs"] is False


class TestPurgeGeoCacheClause:
    @pytest.fixture
    def repo(self):
        conn = RecordingConnection()
        return bind_repository(AdminRepository, conn), conn

    @pytest.mark.parametrize(
        "table_hostile",
        [
            pytest.param("users; DROP TABLE users", id="chaine_sql"),
            pytest.param("seloger_place_ids ", id="espace_final"),
            pytest.param("pg_shadow", id="table_systeme"),
            pytest.param("*", id="joker"),
            pytest.param("", id="vide"),
            pytest.param("SELGER_PLACE_IDS", id="casse_differentes"),
        ],
    )
    def test_une_table_hors_registre_refuse_avant_la_base(self, repo, table_hostile):
        """🔒 Le nom est interpolé dans le DELETE : l'allowlist doit rejeter
        AVANT tout accès base, jamais compter sur Postgres."""
        storage_repo, conn = repo

        with pytest.raises(ValueError, match="Cache géo inconnu"):
            storage_repo.purge_geo_cache(table_hostile)

        assert conn.executed == [], "rien ne doit atteindre la base"

    def test_une_table_du_registre_parte_un_delete_simple(self, repo):
        storage_repo, conn = repo
        # Un DELETE « supprime 7 lignes » : rowcount simulé par 7 résultats.
        conn._results = [[{}, {}, {}, {}, {}, {}, {}]]

        supprimees = storage_repo.purge_geo_cache("pap_geo_ids")

        assert supprimees == 7
        assert conn.sql == ["DELETE FROM pap_geo_ids"]
        assert conn.commits == 1
