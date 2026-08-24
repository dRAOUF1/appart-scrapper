"""Tests de `core/web_utils.py` — conversion d'un paramètre de requête en
entier (`to_int`) et mise en forme lisible des critères canoniques
(`format_criteria_lisible`, issue #18).

`to_int` décide de la pagination et des filtres numériques de toutes les
routes : « tout ce qu'`int()` refuse retombe sur le défaut ». Les cas limites
ci-dessous le figent, notamment `"3.5"` qui n'est PAS tronqué à 3.

`format_criteria_lisible` traduit LE contrat central en français affichable :
il ne doit ni inventer de critère absent, ni perdre le moindre identifiant
(code INSEE compris — écran de debug admin), ni jamais lever sur des critères
partiels.
"""

from __future__ import annotations

import pytest

from core.web_utils import format_criteria_lisible, to_int


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        # --- ce qu'int() sait lire ---
        pytest.param("42", 0, 42, id="chaine-numerique"),
        pytest.param(7, 0, 7, id="int-tel-quel"),
        pytest.param(" 42 ", 0, 42, id="chaine-avec-espaces"),
        pytest.param("-5", 0, -5, id="negatif-accepte-tel-quel"),
        pytest.param("0", 9, 0, id="zero-n-est-pas-un-echec"),
        pytest.param(3.5, 0, 3, id="float-tronque-vers-zero"),
        pytest.param(-3.5, 0, -3, id="float-negatif-tronque-vers-zero"),
        pytest.param(True, 0, 1, id="bool-vaut-son-entier"),
        # --- ce qu'int() refuse : ValueError ---
        pytest.param("abc", 5, 5, id="chaine-non-numerique"),
        pytest.param("", 5, 5, id="chaine-vide"),
        pytest.param("3.5", 0, 0, id="float-en-chaine-refuse-pas-tronque"),
        pytest.param("1e3", 0, 0, id="notation-scientifique-en-chaine"),
        pytest.param("0x10", 0, 0, id="hexadecimal-en-chaine"),
        pytest.param("١٢", 0, 12, id="chiffres-arabes-indiens-acceptes-par-int"),
        # --- ce qu'int() refuse : TypeError ---
        pytest.param(None, 5, 5, id="none"),
        pytest.param([], 5, 5, id="liste"),
        pytest.param(["3"], 5, 5, id="liste-non-vide"),
        pytest.param({}, 5, 5, id="dict"),
        pytest.param(object(), 5, 5, id="objet-quelconque"),
    ],
)
def test_to_int_falls_back_to_the_default_on_anything_int_refuses(value, default, expected):
    """`to_int` ne lève jamais : une chaîne de requête arbitraire arrive ici
    depuis l'URL, et un 500 sur `?page=abc` serait inacceptable."""
    assert to_int(value, default) == expected


def test_a_string_float_is_rejected_while_a_real_float_is_truncated():
    """Divergence contre-intuitive à connaître : `int("3.5")` lève, `int(3.5)`
    tronque. Un paramètre d'URL étant toujours une chaîne, `?priceMax=3.5`
    retombe donc sur le défaut au lieu de valoir 3."""
    assert to_int("3.5", 0) == 0
    assert to_int(3.5, 0) == 3


def test_the_default_is_returned_verbatim_without_being_converted():
    """Le défaut est déclaré `int` par la signature mais n'est pas contrôlé :
    il est renvoyé tel quel. Les appelants passent tous un littéral entier —
    ce test fige juste qu'aucune conversion n'est appliquée au repli."""
    sentinel = "je-ne-suis-pas-un-entier"

    assert to_int("abc", sentinel) is sentinel


# ---------------------------------------------------------------------------
# format_criteria_lisible (issue #18)
# ---------------------------------------------------------------------------

def _valeurs(lignes: list[tuple[str, str]]) -> dict[str, str]:
    return dict(lignes)


class TestFormatCriteriaLisible:
    def test_une_recherche_complete_devient_des_lignes_francaises(self):
        from tests.helpers.factories import make_criteria

        lignes = _valeurs(format_criteria_lisible(make_criteria(priceMin=800, priceMax=1200)))

        assert lignes["Localisation"] == "Paris (75013) · INSEE 75113"
        assert lignes["Transaction"] == "Location"
        assert lignes["Types de bien"] == "Appartement"
        assert lignes["Prix"] == "800 – 1200 €"

    def test_le_code_insee_est_affiche_entier_pas_masque(self):
        """Écran de debug admin : l'identifiant qui permet à chaque source de
        retrouver son lieu doit rester visible, jamais masqué."""
        from tests.helpers.factories import make_city_location

        lignes = _valeurs(format_criteria_lisible({"locations": [make_city_location()]}))

        assert "75113" in lignes["Localisation"]

    def test_les_bornes_ouvertes_sont_explicites_sur_leur_sens(self):
        lignes = _valeurs(format_criteria_lisible({"priceMax": 900, "surfaceMin": 25}))

        assert lignes["Prix"] == "900 € (max)"
        assert lignes["Surface"] == "25 m² (min)"

    def test_le_cinq_des_compteurs_porte_le_plus(self):
        """Le 5 du formulaire signifie « 5 et plus » : le résumé doit le dire."""
        lignes = _valeurs(format_criteria_lisible({"rooms": [2, 5]}))

        assert lignes["Pièces"] == "2, 5+"

    def test_les_perimetres_larges_portent_leur_niveau(self):
        from core.criteria import location_label
        from tests.helpers.factories import make_department_location

        lignes = _valeurs(
            format_criteria_lisible({"locations": [make_department_location("33", "Gironde")]})
        )

        assert lignes["Localisation"] == location_label(make_department_location("33", "Gironde"))
        assert "tout le département" in lignes["Localisation"]

    def test_les_surcharges_par_source_sont_listees_pour_debug(self):
        criteria = {"sourceOverrides": {"seloger": {"placeIds": ["AD08FR31096"]}}}

        lignes = _valeurs(format_criteria_lisible(criteria))

        assert lignes["Surcharges (seloger)"] == "placeIds=AD08FR31096"

    def test_aucun_critere_invente_quand_les_criteres_sont_partiels(self):
        lignes = format_criteria_lisible({"transaction": "rent"})

        labels = [label for label, _ in lignes]
        assert labels == ["Transaction"]

    @pytest.mark.parametrize(
        "criteria",
        [None, {}, "pas un dict", []],
        ids=["none", "vide", "chaine", "liste"],
    )
    def test_n_importe_quoi_d_inexploitable_renvoie_une_liste_vide(self, criteria):
        assert format_criteria_lisible(criteria) == []
