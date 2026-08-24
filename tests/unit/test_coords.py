"""Tests unitaires de parsers/_coords.py — la validation partagée des
coordonnées natives (issue #26).

Ces helpers sont LE point de passage obligé des quatre sources vague 1 : leur
contrat « une composante illisible, nulle (sentinelle) ou hors bornes = pas de
coordonnées » porte les pièges documentés par l'issue (essetpm null ET
0.0/0.0, etc.). Un relâchement ici mettrait un pin au golfe de Guinée.
"""

from __future__ import annotations

import math

import pytest

from parsers._coords import (
    PRECISION_APPROXIMATIVE,
    PRECISION_COMMUNE,
    PRECISION_EXACTE,
    coordonnee_valide,
    extraire_coordonnees,
)


class TestCoordonneeValideLatitude:
    @pytest.mark.parametrize(
        "brute",
        [44.857861, 48.8762016, -21.3390615, 0.0000001, "43.615976", 43],
        ids=["flottant", "paris", "reunion", "presque-zero", "chaine-numerique", "entier"],
    )
    def test_accepts_readable_nonzero_in_bounds(self, brute):
        assert coordonnee_valide(brute, min_bound=-90.0, max_bound=90.0) == float(brute)

    @pytest.mark.parametrize(
        "brute",
        [None, "", "   ", "abc", "43,6", 0, 0.0, "0.0", True, False],
        ids=["none", "vide", "blancs", "abc", "virgule-fr", "int-zero", "float-zero",
             "str-zero", "bool-true", "bool-false"],
    )
    def test_rejects_unreadable_or_zero(self, brute):
        assert coordonnee_valide(brute, min_bound=-90.0, max_bound=90.0) is None

    @pytest.mark.parametrize("hors_bornes", [90.00001, -90.00001, 999, -999], ids=list("abcd"))
    def test_rejects_out_of_bounds(self, hors_bornes):
        assert coordonnee_valide(hors_bornes, min_bound=-90.0, max_bound=90.0) is None

    def test_nan_and_infinity_are_not_coordinates(self):
        for brute in (math.nan, math.inf, -math.inf):
            assert coordonnee_valide(brute, min_bound=-90.0, max_bound=90.0) is None

    def test_the_exact_bounds_are_accepted(self):
        """±90 / ±180 inclus : les bornes sont géographiquement définies."""
        assert coordonnee_valide(90.0, min_bound=-90.0, max_bound=90.0) == 90.0
        assert coordonnee_valide(-180.0, min_bound=-180.0, max_bound=180.0) == -180.0


class TestExtraireCoordonnees:
    def test_a_valid_pair_round_trips(self):
        assert extraire_coordonnees(44.85786105489427, -0.5764371365744323) == (
            44.85786105489427,
            -0.5764371365744323,
        )

    # Le piège essetpm : absence en DEUX formes.
    def test_null_pair_is_rejected(self):
        assert extraire_coordonnees(None, None) is None

    def test_the_00_sentinel_is_rejected(self):
        """Le (0.0, 0.0) « golfe de Guinée » — jamais stocké, jamais affiché."""
        assert extraire_coordonnees(0.0, 0.0) is None

    @pytest.mark.parametrize(
        ("lat", "lon"),
        [(0.0, 2.35), (44.85, 0.0), ("", 2.35), (44.85, None), ("abc", 2.35), (999, 2.35)],
        ids=["lat-zero", "lon-zero", "lat-vide", "lon-absente", "lat-abc", "lat-hors-borne"],
    )
    def test_one_bad_component_kills_the_whole_pair(self, lat, lon):
        """UNE seule composante inexploitable suffit : un point faux est pire
        qu'aucun point (l'utilisateur irait regarder un quartier erroné)."""
        assert extraire_coordonnees(lat, lon) is None

    def test_numeric_strings_are_tolerated(self):
        assert extraire_coordonnees("43.615976", "1.43466") == (43.615976, 1.43466)


def test_the_precision_vocabulary_is_stable():
    """Le vocabulaire traverse parser → base → JSON → JS : le renommer casserait
    la carte et les annonces existantes en base."""
    assert PRECISION_EXACTE == "exacte"
    assert PRECISION_APPROXIMATIVE == "approximative"
    assert PRECISION_COMMUNE == "commune"
