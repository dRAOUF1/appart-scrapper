"""Mécanique de session de la vue administrateur lecture seule (#22).

Ces helpers (`routes/auth.py`) portent TOUTE la sécurité de l'état
d'impersonation : la session bascule vers la cible en mémorisant l'admin réel,
et la sortie restaure depuis ces clés figées. Ils sont testés purs, sur des
dicts et un storage doublé — pas de requête, pas d'app Flask.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from routes.auth import (
    CLE_IMPERSONATEUR_ID,
    CLE_IMPERSONATEUR_USERNAME,
    CLE_IMPERSONE_USERNAME,
    demarrer_impersonation,
    terminer_impersonation,
)

ADMIN = {"id": 99, "username": "root-admin"}
CIBLE = {"id": 1, "username": "alice"}


class TestDemarrerImpersonation:
    def test_la_session_bascule_vers_la_cible(self):
        sess: dict = {"user_id": ADMIN["id"], "username": ADMIN["username"]}

        demarrer_impersonation(sess, ADMIN, CIBLE)

        # L'identité COURANTE est celle de la cible : tous les décorateurs
        # existants résolvent son dashboard, ses recherches, ses annonces.
        assert sess["user_id"] == CIBLE["id"]
        assert sess["username"] == CIBLE["username"]
        # L'admin réel est figé à part — c'est lui que la sortie restaure.
        assert sess[CLE_IMPERSONATEUR_ID] == ADMIN["id"]
        assert sess[CLE_IMPERSONATEUR_USERNAME] == ADMIN["username"]
        assert sess[CLE_IMPERSONE_USERNAME] == CIBLE["username"]


class TestTerminerImpersonation:
    def test_la_session_admin_est_restauree_a_l_identique(self):
        sess: dict = {
            "user_id": CIBLE["id"],
            "username": CIBLE["username"],
            CLE_IMPERSONATEUR_ID: ADMIN["id"],
            CLE_IMPERSONATEUR_USERNAME: ADMIN["username"],
            CLE_IMPERSONE_USERNAME: CIBLE["username"],
        }
        storage = MagicMock()
        storage.users.get_user_by_id.return_value = dict(ADMIN)

        admin_restaure = terminer_impersonation(sess, storage)

        assert admin_restaure == ADMIN
        assert sess["user_id"] == ADMIN["id"]
        assert sess["username"] == ADMIN["username"]
        for cle in (CLE_IMPERSONATEUR_ID, CLE_IMPERSONATEUR_USERNAME, CLE_IMPERSONE_USERNAME):
            assert cle not in sess
        storage.users.get_user_by_id.assert_called_once_with(ADMIN["id"])

    def test_la_cible_supprimee_n_empeche_pas_la_restoration(self):
        """La restauration lit les clés FIGÉES de la session, jamais la ligne
        cible : un compte supprimé pendant la consultation ne peut pas laisser
        l'admin coincé dans une identité morte."""
        sess: dict = {
            "user_id": CIBLE["id"],
            "username": CIBLE["username"],
            CLE_IMPERSONATEUR_ID: ADMIN["id"],
            CLE_IMPERSONATEUR_USERNAME: ADMIN["username"],
            CLE_IMPERSONE_USERNAME: CIBLE["username"],
        }
        storage = MagicMock()
        storage.users.get_user_by_id.return_value = dict(ADMIN)

        admin_restaure = terminer_impersonation(sess, storage)

        assert admin_restaure is not None
        assert sess["user_id"] == ADMIN["id"]

    def test_un_admin_supprime_purge_toute_la_session(self):
        """Si l'ADMIN a disparu entre-temps, aucune identité valide ne peut être
        reconstituée : la session est purgée plutôt que laissée à moitié
        restaurée (le prochain clic repartira du login)."""
        sess: dict = {
            "user_id": CIBLE["id"],
            "username": CIBLE["username"],
            CLE_IMPERSONATEUR_ID: ADMIN["id"],
            CLE_IMPERSONATEUR_USERNAME: ADMIN["username"],
            CLE_IMPERSONE_USERNAME: CIBLE["username"],
        }
        storage = MagicMock()
        storage.users.get_user_by_id.return_value = None

        admin_restaure = terminer_impersonation(sess, storage)

        assert admin_restaure is None
        assert sess == {}

    def test_une_session_deja_partiellement_nettoyee_ne_leve_pas(self):
        """Les clés sont retirées avec pop(default) : rejouer la sortie sur une
        session amputée ne doit jamais lever."""
        sess: dict = {"user_id": 1}
        storage = MagicMock()
        storage.users.get_user_by_id.return_value = None

        assert terminer_impersonation(sess, storage) is None
        assert sess == {}
