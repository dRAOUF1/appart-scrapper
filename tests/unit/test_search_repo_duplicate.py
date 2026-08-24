"""Tests de `SearchRepository.duplicate_search` (issue #18).

La duplication n'a pas besoin d'une vraie base pour être vérifiée : elle
ORCHESTRE deux méthodes existantes (`get_search` — lecture normalisée
canonique — et `create_search`), et tout ce qui compte est dans les arguments
qu'elle leur transmet. Le SQL lui-même est couvert par tests/integration/.
"""

from __future__ import annotations

import pytest

from repositories.search_repo import SearchRepository
from tests.helpers.factories import make_criteria, make_search_row
from tests.helpers.fakes import bind_repository


@pytest.fixture
def repo():
    """Un SearchRepository réel, débranché de toute connexion.

    `get_search`/`create_search` sont stubbés PAR INSTANCE : on observe
    l'orchestration, pas le SQL (déjà couvert ailleurs)."""
    return bind_repository(SearchRepository, conn=None)  # type: ignore[arg-type]


def capture_create(repo):
    """Remplace `create_search` par un capteur qui renvoie une ligne plausible."""
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return {"id": 42, **kwargs}

    repo.create_search = fake_create  # type: ignore[method-assign]
    return captured


def test_la_copie_est_inactive_avec_le_meme_proprietaire_et_les_memes_criteres(repo):
    source = make_search_row(
        id=7,
        user_id=8,
        label="Paris 13e T2-T3",
        scrape_interval=15,
        notify_enabled=False,
        sources=["seloger", "pap"],
        criteria=make_criteria(priceMax=1400),
    )
    repo.get_search = lambda search_id: source if search_id == 7 else None  # type: ignore[method-assign]
    captured = capture_create(repo)

    copie = repo.duplicate_search(7, "Copie de Paris 13e T2-T3")

    assert copie["id"] == 42
    assert captured["user_id"] == 8, "le propriétaire est conservé tel quel"
    assert captured["label"] == "Copie de Paris 13e T2-T3"
    assert captured["is_active"] is False, "une copie ne doit jamais se scraper d'elle-même"
    assert captured["criteria"] == source["criteria"], "critères canoniques identiques"
    assert captured["notify_enabled"] is False, "l'état de notification est copié"
    assert captured["sources"] == ["seloger", "pap"]
    assert captured["scrape_interval"] == 15
    assert captured["ntfy_topic"] == "test-topic"


def test_un_intervalle_absent_retombe_sur_le_defaut_de_cinq_minutes(repo):
    repo.get_search = lambda search_id: make_search_row(id=1, scrape_interval=None)  # type: ignore[method-assign]
    captured = capture_create(repo)

    repo.duplicate_search(1, "Copie")

    assert captured["scrape_interval"] == 5


def test_une_recherche_absente_ne_cree_rien(repo):
    repo.get_search = lambda search_id: None  # type: ignore[method-assign]
    captured = capture_create(repo)

    assert repo.duplicate_search(404, "Copie") is None
    assert captured == {}
