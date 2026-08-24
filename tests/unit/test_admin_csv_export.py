"""Tests unitaires du neutraliseur d'injection CSV de l'export admin.

L'export CSV (#20) écrit des cellules issues du SCRAPING (titre, ville…) :
une valeur commençant par « = », « + », « - » ou « @ » serait évaluée comme
formule par Excel/LibreOffice à l'ouverture (CWE-1236 — exfiltration via
HYPERLINK, exécution de commandes). Le contrat OWASP est simple : préfixer
une apostrophe à TOUTE cellule commençant par l'un de ces 4 caractères.
"""

from __future__ import annotations

import pytest

from routes.admin import COLONNES_CSV, _cellules_csv_annonce, _neutraliser_formule_csv

CARACTERES_DANGEREUX = ["=", "+", "-", "@"]


class TestNeutraliserFormuleCsv:
    @pytest.mark.parametrize("caractere", CARACTERES_DANGEREUX)
    def test_a_leading_formula_character_is_prefixed_with_an_apostrophe(self, caractere):
        cellule = f"{caractere}1+1 cmd|' /C calc"

        assert _neutraliser_formule_csv(cellule) == f"'{cellule}"

    @pytest.mark.parametrize("caractere", CARACTERES_DANGEREUX)
    def test_a_formula_character_anywhere_else_changes_nothing(self, caractere):
        cellule = f"Appartement {caractere} 3 pièces"

        assert _neutraliser_formule_csv(cellule) == cellule

    @pytest.mark.parametrize(
        "neutre",
        [
            "",
            "Appartement 3 pièces",
            "Paris 13e (75013)",
            "1 200 €",
            "https://www.seloger.com/annonce/42",
            "'=déjà apostrophé",
        ],
        ids=["vide", "titre", "ville", "prix", "url", "apostrophe-initiale"],
    )
    def test_neutral_cells_are_left_untouched(self, neutre):
        assert _neutraliser_formule_csv(neutre) == neutre


class TestCellulesCsvAnnonce:
    def test_un_titre_malveillant_est_inerte_dans_sa_ligne(self):
        ligne = _cellules_csv_annonce({
            "listing_id": 42,
            "title": '=HYPERLINK("http://evil.example";"voir")',
            "source": "seloger",
            "city": "@Paris",
            "url": "https://www.seloger.com/annonce/42",
        })

        titre = ligne[COLONNES_CSV.index("titre")]
        assert titre == '\'=HYPERLINK("http://evil.example";"voir")'
        assert ligne[COLONNES_CSV.index("ville")] == "'@Paris"
        # Les cellules saines ne sont pas retouchées.
        assert ligne[COLONNES_CSV.index("source")] == "seloger"
        assert ligne[COLONNES_CSV.index("url")].startswith("https://")

    def test_aucune_cellule_d_une_ligne_ne_commence_par_un_caractere_formule(self):
        """Invariant d'export : quel que soit le scraping, aucune cellule
        produite ne peut être évaluée comme formule par un tableur."""
        ligne = _cellules_csv_annonce({
            "title": "=TITRE", "source": "+source", "price": "-prix",
            "surface": "@surface", "rooms": "=pièces", "city": "=ville",
            "url": "=url", "creation_date": "=date",
            "first_seen": None,
        })

        for cellule in ligne:
            assert not cellule.startswith(("=", "+", "-", "@")), cellule
