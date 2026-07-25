"""Tests de `core/web_utils.py` — la conversion d'un paramètre de requête en entier.

Une seule fonction, mais c'est elle qui décide de la pagination et des filtres
numériques de toutes les routes : `?page=abc` doit afficher la page 1, pas
renvoyer un 500. Le contrat tient en une phrase — « tout ce qu'`int()` refuse
retombe sur le défaut » — et les cas limites ci-dessous le figent, notamment
`"3.5"` qui n'est PAS tronqué à 3 alors que le float `3.5` l'est.
"""

from __future__ import annotations

import pytest

from core.web_utils import to_int


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
