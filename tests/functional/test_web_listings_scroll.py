"""Scroll infini des annonces (#14) : endpoint fragment et rendu initial.

La pagination « ← Précédent / Suivant → » a été remplacée par un chargement
automatique des tranches suivantes. Ce fichier teste les deux bouts du
contrat, sans navigateur :

* l'endpoint fragment `GET /listings/<id>/page` — authentification, garde
  propriétaire (404), bornes de page (400), marqueur de fin de liste porté
  par `data-end-of-list`, et reconstruction DES MÊMES filtres/tri/blacklist
  que la vue initiale ;
* le rendu initial de `/listings/<id>` — la première tranche vient du même
  partial `_listings_slice.html` que les fragments (mêmes cartes, donc la
  macro de dates #12 s'applique aussi), la pagination classique a disparu et
  l'échafaudage du scroll (sentinelle, spinner, état « fin ») est en place.

Le JavaScript lui-même (`static/listings_scroll.js`) est volontairement hors
de portée ici : vanilla JS sans dépendance, validé manuellement au
navigateur — les tests fonctionnels couvrent le contrat serveur qu'il consomme.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from tests.functional.conftest import make_view_listing
from tests.helpers.factories import make_search_row

TITLE_RE = re.compile(r'<div class="listing-title">(.*?)</div>')


def _rows(count: int) -> list[dict]:
    """`count` annonces de vue distinctes, prêtes à être rendues."""
    return [
        {**make_view_listing(listing_id=f"sl_{i}", title=f"Annonce {i}"),
         "found_at": datetime(2026, 7, 1, 9, 0)}
        for i in range(count)
    ]


def _configure(storage: object, total: int, rows: list[dict]) -> None:
    """Branche le double de repo sur un total et des lignes donnés."""
    storage.listings.count_listings_for_search.return_value = total
    storage.listings.get_listings_for_search.return_value = rows


# ---------------------------------------------------------------------------
# Endpoint fragment /listings/<id>/page
# ---------------------------------------------------------------------------


class TestListingsPageEndpoint:
    def test_an_anonymous_client_is_redirected_to_login(self, client):
        resp = client.get("/listings/1/page?page=2")

        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_a_foreign_search_is_a_404_and_fetches_nothing(
        self, web_client, storage, foreign_search
    ):
        """Le fragment ne divulgue pas davantage que la page complète : une
        recherche d'autrui est un 404, et surtout aucune requête listings ne
        part — le refus tombe AVANT tout accès aux données."""
        resp = web_client.get("/listings/1/page?page=2")

        assert resp.status_code == 404
        storage.listings.get_listings_for_search.assert_not_called()
        storage.listings.count_listings_for_search.assert_not_called()

    def test_the_fragment_renders_the_expected_cards_with_labeled_dates(
        self, web_client, storage, owned_search
    ):
        """Les cartes du fragment portent exactement le markup du rendu
        initial — y compris la règle #12 : toute date affichée porte son
        label « Publiée le » / « Détectée le »."""
        rows = [
            {**make_view_listing(listing_id="a", creation_date="2026-07-05T09:00:00+00:00"),
             "found_at": datetime(2026, 7, 6, 10, 0)},
            {**make_view_listing(listing_id="b"), "found_at": datetime(2026, 7, 1, 14, 30)},
        ]
        _configure(storage, total=45, rows=rows)

        resp = web_client.get("/listings/1/page?page=2")

        assert resp.status_code == 200
        text = resp.data.decode()
        assert 'data-page="2"' in text
        assert 'data-total-pages="3"' in text
        assert "data-end-of-list" not in text  # reste à charger
        assert text.count('class="listing-card"') == 2
        assert "Publiée le 05/07/2026" in text
        assert "Détectée le 01/07/2026 16:30" in text

    def test_the_fragment_shows_exactly_the_cards_of_the_full_page(
        self, web_client, storage, owned_search
    ):
        """Garantie centrale du partial partagé : pour la page 1, fragment et
        rendu initial affichent LES MÊMES cartes, dans le même ordre."""
        _configure(storage, total=3, rows=_rows(3))

        initial = web_client.get("/listings/1")
        fragment = web_client.get("/listings/1/page?page=1")

        assert initial.status_code == 200
        assert fragment.status_code == 200
        assert TITLE_RE.findall(initial.data.decode()) == TITLE_RE.findall(fragment.data.decode())

    @pytest.mark.parametrize(
        "query",
        [
            pytest.param("", id="absente"),
            pytest.param("?page=0", id="zero"),
            pytest.param("?page=-2", id="negatif"),
            pytest.param("?page=nawak", id="illisible"),
            pytest.param("?page=4", id="au-dela-de-la-derniere"),
        ],
    )
    def test_an_out_of_range_page_is_rejected(self, web_client, storage, owned_search, query):
        """Le client connaît `data-total-pages` et ne doit jamais demander
        au-delà ; le 400 est le filet défensif côté serveur."""
        _configure(storage, total=45, rows=_rows(20))

        resp = web_client.get(f"/listings/1/page{query}")

        assert resp.status_code == 400

    def test_the_last_page_carries_the_end_of_list_marker(
        self, web_client, storage, owned_search
    ):
        _configure(storage, total=45, rows=_rows(5))

        resp = web_client.get("/listings/1/page?page=3")

        assert resp.status_code == 200
        text = resp.data.decode()
        assert 'data-end-of-list="true"' in text

    def test_a_single_page_slice_is_already_the_end(
        self, web_client, storage, owned_search
    ):
        _configure(storage, total=10, rows=_rows(10))

        resp = web_client.get("/listings/1/page?page=1")

        assert resp.status_code == 200
        assert 'data-end-of-list="true"' in resp.data.decode()

    def test_the_fragment_uses_the_same_filters_sort_and_offset_as_the_page(
        self, web_client, storage, owned_search
    ):
        """Filtres et tri sont repris de la querystring — sinon la tranche 2
        ne serait pas la suite de la tranche 1 quand un filtre actif."""
        _configure(storage, total=45, rows=_rows(20))

        web_client.get("/listings/1/page?page=2&city=Paris&q=loft&sort=price_asc")

        kwargs = storage.listings.get_listings_for_search.call_args.kwargs
        assert kwargs["filters"] == {"city": "Paris", "q": "loft"}
        assert kwargs["sort"] == "price_asc"
        assert kwargs["limit"] == 20
        assert kwargs["offset"] == 20
        count_kwargs = storage.listings.count_listings_for_search.call_args.kwargs
        assert count_kwargs["filters"] == kwargs["filters"]

    def test_exclude_mode_blacklist_applies_to_the_fragment(
        self, web_client, storage, user
    ):
        row = make_search_row(
            id=1, user_id=user["id"],
            blacklist_mode="exclude", blacklisted_agencies=["Foncia"],
        )
        storage.searches.get_search.side_effect = lambda search_id: row if search_id == 1 else None
        _configure(storage, total=45, rows=_rows(20))

        web_client.get("/listings/1/page?page=2")

        kwargs = storage.listings.get_listings_for_search.call_args.kwargs
        assert kwargs["blacklisted_agencies"] == ["Foncia"]


# ---------------------------------------------------------------------------
# Rendu initial : échafaudage du scroll infini, plus de pagination
# ---------------------------------------------------------------------------


class TestListingsInitialRender:
    def test_the_initial_page_keeps_grid_counter_and_scroll_scaffold(
        self, web_client, storage, owned_search
    ):
        """Non-régression : grille, compteur d'annonces, sentinelle, spinner,
        état « fin », script — et la première tranche sans marqueur de fin
        tant qu'il reste des pages."""
        _configure(storage, total=45, rows=_rows(20))

        resp = web_client.get("/listings/1")

        assert resp.status_code == 200
        text = resp.data.decode()
        assert 'id="listing-grid"' in text
        assert "45 annonces" in text
        assert 'id="listings-sentinel"' in text
        assert 'id="listings-spinner"' in text
        assert 'id="listings-end"' in text
        assert "listings_scroll.js" in text
        assert 'data-page="1"' in text
        assert 'data-total-pages="3"' in text
        assert "data-end-of-list" not in text

    def test_the_classic_pagination_links_are_gone(self, web_client, storage, owned_search):
        _configure(storage, total=45, rows=_rows(20))

        resp = web_client.get("/listings/1")

        text = resp.data.decode()
        assert "Précédent" not in text
        assert "Suivant" not in text

    def test_a_single_page_initial_render_marks_the_end_immediately(
        self, web_client, storage, owned_search
    ):
        """Tout est déjà affiché : la tranche initiale porte le marqueur de
        fin — le JS n'émettra aucune requête."""
        _configure(storage, total=10, rows=_rows(10))

        resp = web_client.get("/listings/1")

        assert resp.status_code == 200
        assert 'data-end-of-list="true"' in resp.data.decode()

    def test_the_filters_form_still_submits_to_the_first_page(
        self, web_client, storage, owned_search
    ):
        """Les filtres conservent leur comportement (#14 hors périmètre) : la
        soumission GET vise la page sans numéro — retour à la 1re tranche."""
        _configure(storage, total=45, rows=_rows(20))

        resp = web_client.get("/listings/1")

        assert '<form class="filter-bar card" method="GET" action="/listings/1"' in resp.data.decode()
