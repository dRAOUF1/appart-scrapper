"""Tests unitaires de la vue GLOBALE des scrape_logs (issue #19).

`ScrapeLogRepository` gagne trois lectures toutes recherches confondues :
get_all_scrape_logs / count_all_scrape_logs / get_global_scrape_stats. Le
stockage reste le JSONL par recherche de `scrape_logs/storage.py` (aucun DDL,
aucun changement des signatures existantes) : ces tests verrouillent donc les
contrats nouveaux —

  * tri chronologique DÉCROISSANT à travers les recherches (pas par dossier) ;
  * filtres combinables statut + search_ids + bornes de dates INCLUSIVES ;
  * pagination limit/offset identique au contrat get_scrape_logs ;
  * taux de succès : un scrape « empty » n'est PAS un échec (contrat repris
    de get_scrape_stats) et « pas de donnée » vaut None, jamais 0 %.

Les répertoires de logs sont redirigés vers tmp_path par la fixture autouse
`log_dirs` (tests/conftest.py) — aucun test n'écrit dans logs/ du dépôt.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from freezegun import freeze_time

import scrape_logs.storage as storage
from repositories.scrape_log_repo import ScrapeLogRepository
from tests.helpers.factories import make_scrape_log_entry

FROZEN = datetime(2026, 7, 26, 12, 0, 0)
REPO = ScrapeLogRepository("postgresql://fake/fake")


def ecrire_entree(search_id: int, **overrides) -> dict:
    """Écrit une entrée via la voie réelle (append_entry → JSONL → read_entries)."""
    entree = make_scrape_log_entry(search_id=search_id, **overrides)
    storage.append_entry(search_id, entree)
    return entree


# ---------------------------------------------------------------------------
# get_all_scrape_logs / count_all_scrape_logs
# ---------------------------------------------------------------------------

class TestGetAllScrapeLogs:
    def test_sans_aucune_recherche_la_liste_est_vide(self, log_dirs):
        assert REPO.get_all_scrape_logs() == []
        assert REPO.count_all_scrape_logs() == 0

    def test_les_entrees_de_toutes_les_recherches_sont_triees_du_plus_recent(self, log_dirs):
        """Le tri traverse les dossiers : une entrée ancienne de search_9 doit
        passer APRÈS une entrée récente de search_1, quel que soit l'ordre du
        listdir."""
        ancienne = ecrire_entree(9, started_at=FROZEN - timedelta(hours=3))
        recente = ecrire_entree(1, started_at=FROZEN - timedelta(minutes=5))

        logs = REPO.get_all_scrape_logs()

        assert [log["id"] for log in logs] == [recente["id"], ancienne["id"]]
        assert [log["search_id"] for log in logs] == [1, 9]

    def test_la_pagination_limite_offset_est_celle_du_contrat_existant(self, log_dirs):
        ids = []
        for i in range(25):  # plus d'une page de 20
            ids.append(
                ecrire_entree(search_id=(i % 3) + 1, started_at=FROZEN - timedelta(minutes=i))["id"]
            )

        premiere_page = REPO.get_all_scrape_logs(limit=20, offset=0)
        seconde_page = REPO.get_all_scrape_logs(limit=20, offset=20)

        assert len(premiere_page) == 20
        assert len(seconde_page) == 5
        assert [log["id"] for log in premiere_page] == ids[:20]
        assert [log["id"] for log in seconde_page] == ids[20:]
        assert REPO.count_all_scrape_logs() == 25

    def test_le_filtre_statut_est_combinable_aux_dates(self, log_dirs):
        succes_recente = ecrire_entree(1, status="success", started_at=FROZEN - timedelta(days=1))
        echec_recent = ecrire_entree(2, status="error", error_message="boom", started_at=FROZEN - timedelta(days=1))
        succes_vieux = ecrire_entree(3, status="success", started_at=FROZEN - timedelta(days=10))

        logs = REPO.get_all_scrape_logs(
            status_filter="success",
            date_from=(FROZEN - timedelta(days=7)).strftime("%Y-%m-%d"),
        )

        assert [log["id"] for log in logs] == [succes_recente["id"]]
        assert echec_recent["id"] not in [log["id"] for log in logs]
        assert succes_vieux["id"] not in [log["id"] for log in logs]

    def test_le_filtre_search_ids_restreint_aux_recherches_donnees(self, log_dirs):
        ici = ecrire_entree(1)
        la_bas = ecrire_entree(2)

        logs = REPO.get_all_scrape_logs(search_ids=[2])

        assert [log["id"] for log in logs] == [la_bas["id"]]
        assert ici["id"] not in [log["id"] for log in logs]

    def test_search_ids_vide_ne_veut_pas_dire_tout_le_monde(self, log_dirs):
        """search_ids=[] est une restriction EXPLICITE (ex. source sans aucune
        recherche) : zéro résultat, jamais l'ensemble complet."""
        ecrire_entree(1)

        assert REPO.get_all_scrape_logs(search_ids=[]) == []
        assert REPO.count_all_scrape_logs(search_ids=[]) == 0

    def test_les_bornes_de_dates_sont_inclusives_sur_le_jour(self, log_dirs):
        """date_from/date_to arrivent au format input[type=date] : la borne
        haute couvre toute la journée (23:59:59), un scrape de 23 h compte."""
        pendant = ecrire_entree(1, started_at=FROZEN.replace(hour=23, minute=30))
        avant = ecrire_entree(2, started_at=FROZEN - timedelta(days=1))
        apres = ecrire_entree(3, started_at=FROZEN + timedelta(days=1))
        jour = FROZEN.strftime("%Y-%m-%d")

        logs = REPO.get_all_scrape_logs(date_from=jour, date_to=jour)

        assert [log["id"] for log in logs] == [pendant["id"]]
        for hors in (avant, apres):
            assert hors["id"] not in [log["id"] for log in logs]

    def test_une_date_illegible_est_ignoree_pas_fatale(self, log_dirs):
        ecrire_entree(1)

        assert REPO.count_all_scrape_logs(date_from="nawak") == 1
        assert REPO.count_all_scrape_logs(date_to="nawak") == 1

    def test_les_repertoires_etrangers_sont_ignores(self, log_dirs):
        """Même contrat que find_entry_any : exports/, counter.json et autres
        noms non conformes ne doivent ni planter ni polluer la vue."""
        import os

        ecrire_entree(1)
        os.makedirs(os.path.join(storage.SCRAPE_LOGS_DIR, "exports"), exist_ok=True)
        with open(os.path.join(storage.SCRAPE_LOGS_DIR, "counter.json"), "w", encoding="utf-8") as f:
            f.write('{"last_id": 1}')
        intrus = os.path.join(storage.SCRAPE_LOGS_DIR, "search_pas_un_id")
        os.makedirs(intrus, exist_ok=True)

        assert REPO.count_all_scrape_logs() == 1


# ---------------------------------------------------------------------------
# get_global_scrape_stats
# ---------------------------------------------------------------------------

class TestGetGlobalScrapeStats:
    @pytest.fixture(autouse=True)
    def temps_gele(self):
        """« Maintenant » est figé : les fenêtres 24 h / 7 j deviennent
        calculables exactement."""
        with freeze_time(FROZEN):
            yield

    def test_statistiques_neuves_renvoient_none_pas_zero(self, log_dirs):
        stats = REPO.get_global_scrape_stats()

        assert stats["taux_succes_24h"] is None
        assert stats["taux_succes_7j"] is None
        assert stats["duree_moyenne_7j"] is None
        assert stats["top_erreurs"] == []
        assert stats["nb_scrapes_24h"] == 0
        assert stats["nb_scrapes_7j"] == 0

    def test_un_scrape_empty_nest_pas_un_echec_dans_le_taux(self, log_dirs):
        """Contrat repris de get_scrape_stats : « empty » = a tourné, rien
        trouvé légitimement. Il compte AU NUMÉRATEUR du taux de succès."""
        ecrire_entree(1, status="success", started_at=FROZEN - timedelta(hours=1))
        ecrire_entree(1, status="empty", started_at=FROZEN - timedelta(hours=2))

        stats = REPO.get_global_scrape_stats()

        assert stats["taux_succes_24h"] == 100.0

    def test_un_scrape_partial_reste_un_echec_dans_le_taux(self, log_dirs):
        ecrire_entree(1, status="success", started_at=FROZEN - timedelta(hours=1))
        ecrire_entree(1, status="partial", started_at=FROZEN - timedelta(hours=2))

        assert REPO.get_global_scrape_stats()["taux_succes_24h"] == 50.0

    def test_les_fenetres_24h_et_7j_sont_distinctes(self, log_dirs):
        ecrire_entree(1, status="error", started_at=FROZEN - timedelta(days=6))   # hors 24h
        ecrire_entree(1, status="error", started_at=FROZEN - timedelta(hours=10)) # dans 24h

        stats = REPO.get_global_scrape_stats()

        assert stats["taux_succes_24h"] == 0.0
        assert stats["nb_scrapes_24h"] == 1
        assert stats["nb_scrapes_7j"] == 2
        assert stats["taux_succes_7j"] == 0.0

    def test_la_duree_moyenne_ne_compte_que_les_7_derniers_jours(self, log_dirs):
        ecrire_entree(1, duration_sec=10, started_at=FROZEN - timedelta(hours=1))
        ecrire_entree(1, duration_sec=30, started_at=FROZEN - timedelta(hours=2))
        ecrire_entree(1, duration_sec=9999, started_at=FROZEN - timedelta(days=8))

        stats = REPO.get_global_scrape_stats()

        assert stats["duree_moyenne_7j"] == 20.0

    def test_le_top_erreurs_regroupe_par_message_tronque(self, log_dirs):
        """Deux stacktraces partageant leur préfixe comptent ENSEMBLE : la
        troncature précède le regroupement (c'est voulu)."""
        longue = "TimeoutError: connection reset by peer during handshake — " + "x" * 300
        ecrire_entree(1, status="error", error_message=longue, started_at=FROZEN - timedelta(hours=1))
        ecrire_entree(2, status="error", error_message=longue, started_at=FROZEN - timedelta(hours=2))
        ecrire_entree(2, status="error", error_message="HTTP 403 anti-bot", started_at=FROZEN - timedelta(hours=3))

        stats = REPO.get_global_scrape_stats()

        assert len(stats["top_erreurs"]) == 2
        premier = stats["top_erreurs"][0]
        assert premier["occurrences"] == 2
        assert premier["message"].startswith("TimeoutError")
        assert len(premier["message"]) <= 120
        assert stats["top_erreurs"][1]["message"] == "HTTP 403 anti-bot"

    def test_le_top_erreurs_inclut_chaque_erreur_source_d_un_scrape_partiel(self, log_dirs):
        ecrire_entree(
            1,
            status="partial",
            error_message="résumé agrégé à ignorer",
            details={
                "per_source": {
                    "seloger": {"error": "DataDome"},
                    "bienici": {"error": "HTTP 503"},
                    "laforet": {"found": 4},
                },
            },
            started_at=FROZEN - timedelta(hours=1),
        )

        stats = REPO.get_global_scrape_stats()

        assert {entry["message"] for entry in stats["top_erreurs"]} == {"DataDome", "HTTP 503"}

    def test_a_occurrence_egale_l_erreur_la_plus_recente_passe_devant(self, log_dirs):
        vieille = ecrire_entree(1, status="error", error_message="A", started_at=FROZEN - timedelta(days=2))
        recente = ecrire_entree(2, status="error", error_message="B", started_at=FROZEN - timedelta(hours=1))

        stats = REPO.get_global_scrape_stats()

        assert [e["message"] for e in stats["top_erreurs"]] == ["B", "A"]
        assert stats["top_erreurs"][0]["derniere"] == recente["started_at"]
        assert stats["top_erreurs"][1]["derniere"] == vieille["started_at"]

    def test_le_classement_est_plafonne_a_cinq_messages(self, log_dirs):
        for i in range(7):
            ecrire_entree(1, status="error", error_message=f"erreur-{i}", started_at=FROZEN - timedelta(hours=i + 1))

        stats = REPO.get_global_scrape_stats()

        assert len(stats["top_erreurs"]) == 5

    def test_une_erreur_sans_message_est_nommee_plutot_que_perdue(self, log_dirs):
        ecrire_entree(1, status="error", error_message="", started_at=FROZEN - timedelta(hours=1))

        stats = REPO.get_global_scrape_stats()

        assert stats["top_erreurs"][0]["message"] == "Erreur inconnue"
