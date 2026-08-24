"""Onglet SYSTÈME de l'admin (#21) : santé process, caches géo, paramètres.

Ce que ce module garantit, critère par critère de l'issue :

1. **tab Système** — nav étendue à huit onglets, cartes de santé rendues
   (SHA/uptime/RSS/tick/pool) avec valeurs stubbées, fragment pollé
   (`every 30s`, pattern #16) ;
2. **purges des caches géo** — POST CSRF-protégé scoping registre, audit
   `geo_cache_purged` avec comptes AVANT/APRÈS, toast HX-Trigger, dégradation
   classique sans JS ; une table hors registre est refusée sans audit ;
3. **paramètres branchés** — édition persistée via settings_repo (double
   dynamique get/set), invalides refusés avec message français, et surtout la
   LECTURE À L'USAGE : /admin/cleanup et /admin/logs/purge appliquent le
   réglage relu quand le formulaire ne fournit pas de `days` explicite ;
4. **gardes** — ADMIN_URLS étendue (test_admin.py), CSRF actif sur les deux
   nouvelles routes POST ;
5. **non-régression** — les tabs #16–#20 continuent de rendre après l'ajout
   du huitième onglet.

Les patterns HTMX (fragment + HX-Trigger, modale data-confirm) sont ceux du
socle #16 : re-testés SUR les nouvelles routes.
"""

from __future__ import annotations

import json

import pytest

from tests.functional.conftest import make_admin_stats

# ---------------------------------------------------------------------------
# Doubles de contexte
# ---------------------------------------------------------------------------

def ligne_cache(**extras) -> dict:
    """Ligne de `AdminRepository.get_geo_cache_stats()`."""
    ligne = {"source": "seloger", "table": "seloger_place_ids",
             "entrees": 12, "manquees": 3, "suivi_echecs": True}
    ligne.update(extras)
    return ligne


@pytest.fixture(autouse=True)
def systeme_views(storage):
    """Contexte plausible pour tous les onglets touchés par ce module."""
    storage.admin.get_enhanced_admin_stats.return_value = make_admin_stats()
    storage.admin.get_geo_cache_stats.return_value = [
        ligne_cache(),
        ligne_cache(source="pap", table="pap_geo_ids", entrees=7, manquees=0),
        ligne_cache(source="communes", table="commune_centres", entrees=40,
                    manquees=0, suivi_echecs=False),
    ]
    storage.admin.purge_geo_cache.return_value = 12
    storage.admin.purge_old_logs.return_value = 0
    storage.admin.get_admin_logs.return_value = []
    storage.admin.count_admin_logs.return_value = 0
    storage.users.get_all_users.return_value = []
    storage.searches.get_all_searches.return_value = []
    storage.searches.get_search.return_value = None
    storage.listings.count_all_listings.return_value = 0
    storage.listings.get_all_listings.return_value = []
    storage.listings.get_orphan_listings_count.return_value = 0
    storage.settings.get_setting.return_value = ""
    return storage


def pose_reglages_dynamiques(storage, initial: dict | None = None) -> dict:
    """Branché get/set cohérents sur les clés #21 : la relecture reflète
    l'écriture, comme la vraie table app_settings."""
    etat = dict(initial or {})
    storage.settings.get_setting.side_effect = lambda cle, defaut="": etat.get(cle, defaut)
    storage.settings.set_setting.side_effect = lambda cle, valeur: etat.__setitem__(cle, valeur)
    return etat


@pytest.fixture
def admin_client_csrf(app, admin_user):
    """Client connecté en admin sur l'app AVEC protection CSRF active — même
    fixture que test_admin_htmx.py / test_admin_pilotage.py, redéfinie ici
    pour éprouver la garde sur les nouvelles routes sans coupler les modules."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = admin_user["id"]
        sess["username"] = admin_user["username"]
    return client


# ---------------------------------------------------------------------------
# 1. Tab Système : nav, rendu, polling
# ---------------------------------------------------------------------------

class TestTabSysteme:
    def test_la_nav_propose_le_huitieme_onglet(self, admin_client):
        page = admin_client.get("/admin").data.decode()

        assert "/admin/system" in page
        assert ">Système</a>" in page

    def test_l_url_directe_rend_la_page_complete(self, admin_client):
        reponse = admin_client.get("/admin/system")

        assert reponse.status_code == 200
        corps = reponse.data.decode()
        assert "<html" in corps
        assert 'id="admin-content"' in corps

    @pytest.mark.parametrize("url", ["/admin/system", "/admin?tab=systeme"])
    def test_les_cartes_de_sante_sont_presentes(self, admin_client, url):
        corps = admin_client.get(url).data.decode()

        assert "Santé du process" in corps
        assert "Version déployée" in corps
        assert "Dernier passage planifié" in corps
        assert "Pool de connexions" in corps
        assert "Caches géo par source" in corps
        assert "Paramètres de scraping" in corps

    def test_les_caches_geo_affichent_compteurs_et_manquees(self, admin_client):
        corps = admin_client.get("/admin/system").data.decode()

        assert "seloger" in corps and "pap" in corps and "communes" in corps
        assert "3 (25 %)" in corps, "3 manquées sur 12 entrées = taux lisible"
        assert "(échecs non mémorisés)" in corps, "commune_centres : pas de taux mensonger"

    def test_les_reglages_affichent_les_valeurs_persistees(self, admin_client, storage):
        pose_reglages_dynamiques(storage, {"retention_listings_days": "9",
                                           "purge_logs_days": "14"})

        page = admin_client.get("/admin/system").data.decode()

        assert 'name="retention_listings_days"' in page
        assert 'value="9"' in page
        assert 'name="purge_logs_days"' in page
        assert 'value="14"' in page

    def test_le_fragment_sante_se_poll_lui_meme(self, admin_client):
        reponse = admin_client.get(
            "/admin?tab=systeme&fragment=sante", headers={"HX-Request": "true"}
        )

        assert reponse.status_code == 200
        corps = reponse.data.decode()
        assert "<html" not in corps
        assert 'id="admin-sante-zone"' in corps
        assert 'hx-trigger="every 30s"' in corps, \
            "le pattern de polling doit vivre sur l'élément échangé"
        assert "fragment=sante" in corps

    def test_un_sha_local_est_affiche_comme_tel(self, admin_client, monkeypatch):
        monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
        monkeypatch.delenv("GIT_COMMIT", raising=False)

        corps = admin_client.get(
            "/admin?tab=systeme&fragment=sante", headers={"HX-Request": "true"}
        ).data.decode()

        assert "local" in corps

    def test_un_tick_enregistre_est_affiche_avec_son_resultat(self, admin_client):
        import core.scrape_control as core_scrape_control

        core_scrape_control._dernier_tick = None
        try:
            core_scrape_control.enregistrer_tick("erreur", "pool épuisé")
            corps = admin_client.get(
                "/admin?tab=systeme&fragment=sante", headers={"HX-Request": "true"}
            ).data.decode()
        finally:
            core_scrape_control._dernier_tick = None

        assert "Erreur" in corps
        assert "pool épuisé" in corps

    def test_des_lectures_peripheriques_muettes_ne_provoquent_jamais_500(
        self, admin_client, storage
    ):
        """Robustesse exigée : fragment pollé toutes les 30 s, chaque lecture
        doit dégrader en badge neutre (« — »), jamais en erreur."""
        storage.admin.get_geo_cache_stats.side_effect = RuntimeError("pool épuisé")

        reponse = admin_client.get("/admin/system")

        assert reponse.status_code == 200
        assert "Compteurs indisponibles." in reponse.data.decode()


# ---------------------------------------------------------------------------
# 2. Purge d'un cache géo
# ---------------------------------------------------------------------------

class TestPurgeCacheGeo:
    def test_la_purge_htmx_toaste_et_reconstruit_l_onglet(self, admin_client, storage):
        storage.admin.get_geo_cache_stats.return_value = [ligne_cache(entrees=12)]
        storage.admin.purge_geo_cache.return_value = 12

        reponse = admin_client.post(
            "/admin/system/caches-geo/purge",
            data={"table": "seloger_place_ids"},
            headers={"HX-Request": "true"},
        )

        assert reponse.status_code == 200
        toast = json.loads(reponse.headers["HX-Trigger"])["admin:toast"]
        assert toast["message"] == "Cache « seloger » purgé — 12 entrée(s) supprimée(s)"
        assert toast["category"] == "success"

        corps = reponse.data.decode()
        assert "<html" not in corps
        assert 'id="admin-content"' in corps

    def test_la_purge_est_scopee_a_sa_table(self, admin_client, storage):
        admin_client.post("/admin/system/caches-geo/purge", data={"table": "pap_geo_ids"})

        storage.admin.purge_geo_cache.assert_called_once_with("pap_geo_ids")

    def test_l_audit_porte_source_et_comptes_avant_apres(self, admin_client, storage):
        """Critère d'acceptation : compte AVANT/APRÈS relus en base, pas
        déduits — c'est ce qui rend une purge vérifiable après coup. Le faux
        repo reflète donc l'état post-purge au deuxième comptage."""
        compteurs = {"seloger_place_ids": 10}

        def stats():
            return [ligne_cache(entrees=compteurs["seloger_place_ids"])]

        def faux_purge(table):
            supprimees = compteurs[table]
            compteurs[table] = 0
            return supprimees

        storage.admin.get_geo_cache_stats.side_effect = stats
        storage.admin.purge_geo_cache.side_effect = faux_purge

        admin_client.post(
            "/admin/system/caches-geo/purge",
            data={"table": "seloger_place_ids"},
            headers={"HX-Request": "true"},
        )

        appel = storage.admin.log_admin_action.call_args
        assert appel.args[0] == "geo_cache_purged"
        details = appel.args[1]
        assert "'seloger'" in details
        assert "seloger_place_ids" in details
        assert "10 avant" in details and "0 après" in details

    def test_un_cache_deja_vide_donner_un_message_info(self, admin_client, storage):
        storage.admin.purge_geo_cache.return_value = 0

        reponse = admin_client.post(
            "/admin/system/caches-geo/purge",
            data={"table": "seloger_place_ids"},
            headers={"HX-Request": "true"},
        )

        toast = json.loads(reponse.headers["HX-Trigger"])["admin:toast"]
        assert toast["message"] == "Cache « seloger » déjà vide"
        assert toast["category"] == "info"

    def test_une_table_hors_registre_est_refusee_sans_audit(self, admin_client, storage):
        reponse = admin_client.post(
            "/admin/system/caches-geo/purge",
            data={"table": "users"},
            headers={"HX-Request": "true"},
        )

        assert reponse.status_code == 200
        toast = json.loads(reponse.headers["HX-Trigger"])["admin:toast"]
        assert "Cache géo inconnu" in toast["message"]
        assert toast["category"] == "error"

        storage.admin.log_admin_action.assert_not_called()

    def test_sans_js_le_post_classique_redirige_avec_flash(self, admin_client):
        reponse = admin_client.post(
            "/admin/system/caches-geo/purge", data={"table": "pap_geo_ids"}, follow_redirects=True
        )

        assert reponse.status_code == 200
        assert "toast-success" in reponse.data.decode()

    def test_le_formulaire_embarque_confirmation_et_table_cachee(self, admin_client, storage):
        storage.admin.get_geo_cache_stats.return_value = [ligne_cache(entrees=12)]

        page = admin_client.get("/admin/system").data.decode()

        assert 'name="table" value="seloger_place_ids"' in page
        assert 'data-confirm="Purger le cache « seloger » ?' in page
        assert "12 entrée(s)" in page, "la modale annonce le compteur actuel"

    def test_la_requete_mutante_sans_jeton_est_refusee(self, admin_client_csrf):
        reponse = admin_client_csrf.post(
            "/admin/system/caches-geo/purge",
            data={"table": "seloger_place_ids"},
        )

        assert reponse.status_code == 400


# ---------------------------------------------------------------------------
# 3. Paramètres : validation, persistance, lecture à l'usage
# ---------------------------------------------------------------------------

class TestEditionParametres:
    def test_des_valeurs_valides_sont_persistees_et_audittees(self, admin_client, storage):
        etat = pose_reglages_dynamiques(storage)

        reponse = admin_client.post(
            "/admin/system/settings",
            data={"retention_listings_days": "9", "purge_logs_days": "14"},
            headers={"HX-Request": "true"},
        )

        assert reponse.status_code == 200
        assert etat == {"retention_listings_days": "9", "purge_logs_days": "14"}

        toast = json.loads(reponse.headers["HX-Trigger"])["admin:toast"]
        assert toast["category"] == "success"
        assert "rétention annonces 9 j" in toast["message"]
        assert "purge logs 14 j" in toast["message"]

        appel = storage.admin.log_admin_action.call_args
        assert appel.args[0] == "settings_updated"
        assert "rétention annonces=9 j" in appel.args[1]

    @pytest.mark.parametrize(
        ("retention", "purge"),
        [
            pytest.param("abc", "14", id="non_entier"),
            pytest.param("0", "14", id="zero"),
            pytest.param("-3", "14", id="negatif"),
            pytest.param("366", "14", id="hors_bornes"),
            pytest.param("", "14", id="vide"),
            pytest.param("7", "abc", id="second_champ_invalide"),
        ],
    )
    def test_une_saisie_invalide_est_refusee_sans_persister(
        self, admin_client, storage, retention, purge
    ):
        etat = pose_reglages_dynamiques(storage)

        reponse = admin_client.post(
            "/admin/system/settings",
            data={"retention_listings_days": retention, "purge_logs_days": purge},
            headers={"HX-Request": "true"},
        )

        assert reponse.status_code == 200
        toast = json.loads(reponse.headers["HX-Trigger"])["admin:toast"]
        assert toast["category"] == "error"
        assert toast["message"], "le refus doit dire pourquoi (message français)"

        assert etat == {}, "aucune clé ne doit être écrite quand une saisie est refusée"
        storage.admin.log_admin_action.assert_not_called()

    def test_apres_enregistrement_le_formulaire_montre_les_nouvelles_valeurs(
        self, admin_client, storage
    ):
        """Le fragment renvoyé reconstruit le contexte depuis settings_repo :
        il reflète DÉJÀ la nouvelle valeur (comme le toggle de pause #17)."""
        etat = pose_reglages_dynamiques(storage)

        reponse = admin_client.post(
            "/admin/system/settings",
            data={"retention_listings_days": "21", "purge_logs_days": "60"},
            headers={"HX-Request": "true"},
        )
        corps = reponse.data.decode()

        assert etat["retention_listings_days"] == "21"
        assert 'value="21"' in corps
        assert 'value="60"' in corps

    def test_la_pause_scheduler_n_est_pas_dupliquee_dans_la_tab(self, admin_client):
        """La pause globale est le toggle du dashboard (#17) : l'onglet
        Système ne doit pas proposer un second interrupteur concurrent."""
        page = admin_client.get("/admin/system").data.decode()

        assert "/admin/scheduler/pause" not in page


class TestLectureALUsage:
    """Critère d'acceptation : un réglage changé dans l'UI est bien lu au
    prochain tour concerné — mécanisme identique à la pause #17."""

    def test_le_nettoyage_annonce_lit_le_reglage_quand_days_absent(self, admin_client, storage):
        storage.settings.get_setting.side_effect = lambda cle, defaut="": (
            "9" if cle == "retention_listings_days" else defaut
        )

        admin_client.post("/admin/cleanup")

        storage.listings.delete_old_listings.assert_called_once_with(days=9)

    def test_le_nettoyage_annonce_prefere_le_jour_explicite(self, admin_client, storage):
        """Compatibilité ascendante : un formulaire qui poste `days` garde le
        dernier mot (l'utilisateur demande explicitement autre chose)."""
        pose_reglages_dynamiques(storage, {"retention_listings_days": "9"})

        admin_client.post("/admin/cleanup", data={"days": "2"})

        storage.listings.delete_old_listings.assert_called_once_with(days=2)

    def test_le_nettoyage_annonce_reste_auditue(self, admin_client, storage):
        storage.settings.get_setting.side_effect = lambda cle, defaut="": (
            "9" if cle == "retention_listings_days" else defaut
        )

        admin_client.post("/admin/cleanup")

        appel = storage.admin.log_admin_action.call_args
        assert appel.args[0] == "cleanup_executed"
        assert "older than 9 days" in appel.args[1]

    def test_la_purge_logs_lit_le_reglage_quand_days_absent(self, admin_client, storage):
        storage.settings.get_setting.side_effect = lambda cle, defaut="": (
            "12" if cle == "purge_logs_days" else defaut
        )

        admin_client.post("/admin/logs/purge")

        storage.admin.purge_old_logs.assert_called_once_with(days=12)

    def test_la_bouton_logs_affiche_le_reglage_courant(self, admin_client, storage):
        pose_reglages_dynamiques(storage, {"purge_logs_days": "45"})

        page = admin_client.get("/admin/logs").data.decode()

        assert "Purger anciens logs (&gt;45 j)" in page

    def test_le_bouton_nettoyage_du_dashboard_affiche_le_reglage(self, admin_client, storage):
        pose_reglages_dynamiques(storage, {"retention_listings_days": "6"})

        page = admin_client.get("/admin").data.decode()

        assert "Nettoyer annonces (&gt;6 j)" in page


# ---------------------------------------------------------------------------
# 4. Non-régression des tabs existants (#16–#20)
# ---------------------------------------------------------------------------

class TestNonRegressionTabs:
    @pytest.mark.parametrize(
        "url",
        [
            pytest.param("/admin", id="dashboard"),
            pytest.param("/admin/users", id="users"),
            pytest.param("/admin/searches", id="searches"),
            pytest.param("/admin/listings", id="listings"),
            pytest.param("/admin/scrapes", id="scrapes"),
            pytest.param("/admin/database", id="database"),
            pytest.param("/admin/logs", id="logs"),
        ],
    )
    def test_les_sept_premiers_onglets_rendent_encore(self, admin_client, url):
        reponse = admin_client.get(url)

        assert reponse.status_code == 200
        assert "<html" in reponse.data.decode()

    def test_le_fragment_queue_du_dashboard_survit(self, admin_client):
        reponse = admin_client.get(
            "/admin?tab=dashboard&fragment=queue", headers={"HX-Request": "true"}
        )

        assert reponse.status_code == 200
        assert 'id="admin-queue-zone"' in reponse.data.decode()


# ---------------------------------------------------------------------------
# 5. Garde CSRF sur l'édition des paramètres
# ---------------------------------------------------------------------------

class TestGardeCsrfParametres:
    def test_une_edition_sans_jeton_est_refusee(self, admin_client_csrf):
        reponse = admin_client_csrf.post(
            "/admin/system/settings",
            data={"retention_listings_days": "9", "purge_logs_days": "14"},
        )

        assert reponse.status_code == 400
