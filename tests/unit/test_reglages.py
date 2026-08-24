"""Tests unitaires de `core/reglages.py` — les paramètres éditables (#21).

Deux contrats :

* `valider_jours` — la VALIDATION de saisie (formulaire admin) : entier
  strictement positif, plafonné, messages d'erreur FRANÇAIS destinés au toast.
  Une erreur avalée ou anglophone serait un bug visible côté utilisateur ;
* `lire_jours` — la LECTURE À L'USAGE (pattern #17) : absente, corrompue,
  hors bornes ou base injoignable ⇒ défaut historique (fail-open), jamais une
  purge bloquée par une donnée pourrie dans app_settings.

Les défauts sont les constantes DU CODE : ce sont eux qui garantissent que
« pas de réglage » reste exactement le comportement d'avant l'issue.
"""

from __future__ import annotations

import pytest

from core.reglages import (
    BORNE_MAX_JOURS,
    BORNE_MIN_JOURS,
    CLE_PURGE_LOGS_AUDIT,
    CLE_RETENTION_ANNONCES,
    DEFAUT_PURGE_LOGS_AUDIT,
    DEFAUT_RETENTION_ANNONCES,
    lire_jours,
    valider_jours,
)


class TestClesCanoniques:
    def test_les_cles_sont_le_contrat_ecriture_lecture(self):
        """Une coquille rendrait le formulaire admin inopérant sans aucune
        erreur visible : ces valeurs SONT le contrat avec app_settings."""
        assert CLE_RETENTION_ANNONCES == "retention_listings_days"
        assert CLE_PURGE_LOGS_AUDIT == "purge_logs_days"

    def test_les_defauts_sont_les_valeurs_historiques_du_code(self):
        """#21 exige de trouver les défauts dans le code existant :
        delete_old_listings(days=4) et purge_old_logs(days=30)."""
        assert DEFAUT_RETENTION_ANNONCES == 4
        assert DEFAUT_PURGE_LOGS_AUDIT == 30

    def test_bornes_raisonnables(self):
        assert BORNE_MIN_JOURS == 1
        assert BORNE_MAX_JOURS == 365


class TestValiderJours:
    @pytest.mark.parametrize(
        ("brut", "attendu"),
        [
            pytest.param("7", 7, id="entier_simple"),
            pytest.param(" 12 ", 12, id="espaces_autour"),
            pytest.param("1", 1, id="borne_basse"),
            pytest.param("365", 365, id="borne_haute"),
            pytest.param("070", 70, id="zero_initial"),
        ],
    )
    def test_une_saisie_valide_est_renvoyee_en_entier(self, brut, attendu):
        assert valider_jours(brut, "Rétention") == attendu

    @pytest.mark.parametrize(
        ("brut", "morceau"),
        [
            pytest.param("", "saisie vide", id="vide"),
            pytest.param("   ", "saisie vide", id="blancs"),
            pytest.param(None, "saisie vide", id="none"),
            pytest.param("abc", "n'est pas un nombre entier", id="texte"),
            pytest.param("3.5", "n'est pas un nombre entier", id="decimal"),
            pytest.param("0", "strictement positive", id="zero"),
            pytest.param("-5", "strictement positive", id="negatif"),
            pytest.param("366", "dépasse le maximum autorisé", id="hors_bornes"),
            pytest.param("9999", "dépasse le maximum autorisé", id="tres_grand"),
        ],
    )
    def test_une_saisie_invalide_leve_avec_un_message_francais(self, brut, morceau):
        with pytest.raises(ValueError, match=morceau):
            valider_jours(brut, "Rétention des annonces")

    def test_le_message_porte_le_libelle_du_champ(self):
        """Le toast doit dire QUEL champ est en faute quand il y en a deux."""
        with pytest.raises(ValueError, match="Purge des logs"):
            valider_jours("abc", "Purge des logs")


class _FauxSettings:
    """settings_repo minimal : un dict + compteur de lectures.

    `leverages` prouve qu'une routine RELIT bien à chaque usage — c'est le
    critère d'acceptation « lu au prochain tour » du pattern #17.
    """

    def __init__(self, valeurs: dict | None = None):
        self.valeurs = dict(valeurs or {})
        self.lectures: list[str] = []

    def get_setting(self, cle: str, defaut: str = "") -> str:
        self.lectures.append(cle)
        return self.valeurs.get(cle, defaut)


class TestLireJours:
    def test_la_valeur_persistee_gagne(self):
        settings = _FauxSettings({CLE_RETENTION_ANNONCES: "9"})

        assert lire_jours(settings.get_setting, CLE_RETENTION_ANNONCES, 4) == 9

    def test_absent_retombe_sur_le_defaut(self):
        settings = _FauxSettings()

        assert lire_jours(settings.get_setting, CLE_RETENTION_ANNONCES, 4) == 4

    @pytest.mark.parametrize(
        "stocke",
        [
            pytest.param("", id="chaine_vide"),
            pytest.param("abc", id="non_entier"),
            pytest.param("3.5", id="decimal"),
            pytest.param("0", id="zero_hors_bornes"),
            pytest.param("-2", id="negatif_hors_bornes"),
            pytest.param("366", id="trop_grand"),
            pytest.param(None, id="none"),
        ],
    )
    def test_une_valeur_corrompue_retombe_sur_le_defaut(self, stocke):
        settings = _FauxSettings({CLE_PURGE_LOGS_AUDIT: stocke})

        assert lire_jours(settings.get_setting, CLE_PURGE_LOGS_AUDIT, 30) == 30

    def test_une_base_muette_ne_bloque_pas_l_usage(self):
        def get_setting_muet(*_a, **_k):
            raise RuntimeError("pool épuisé")

        assert lire_jours(get_setting_muet, CLE_RETENTION_ANNONCES, 4) == 4

    def test_le_getter_est_injecte_pas_importe(self):
        """Le garde #17 n'autorise que main.py et admin.py à toucher le
        repository des réglages : core.reglages reçoit le getter par
        injection, sans récepteur objet dans son propre code."""
        import inspect

        from core import reglages

        source = inspect.getsource(reglages.lire_jours)
        assert "get_setting(cle" in source, "appel direct du getter injecté"
        assert ".get_setting(" not in source, "aucun récepteur : le getter est un paramètre"

    def test_la_lecture_se_fait_a_chaque_appel_pas_une_fois(self):
        """Critère d'acceptation #21 : un réglage changé dans l'UI doit être
        lu au prochain tour. La preuve mécanique : chaque appel reconsulte
        settings_repo (via le getter injecté), rien n'est mis en cache."""
        settings = _FauxSettings({CLE_RETENTION_ANNONCES: "6"})
        assert lire_jours(settings.get_setting, CLE_RETENTION_ANNONCES, 4) == 6

        settings.valeurs[CLE_RETENTION_ANNONCES] = "11"
        assert lire_jours(settings.get_setting, CLE_RETENTION_ANNONCES, 4) == 11
        assert len(settings.lectures) == 2
