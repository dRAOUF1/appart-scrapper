"""Tests unitaires de `parsers/foncia.py`.

Foncia expose une API JSON publique (`fnc-api.prod.fonciatech.net`) dont le
contrat a été vérifié en direct le 23/08/2026 — captures réelles dans
tests/fixtures/foncia/ (pages SSR Toulouse/Vannes/multi-villes et un item
d'annonce complet). Ces tests figent ce contrat :

- le corps du POST est le miroir exact des critères canoniques ;
- l'URL humaine joint slugs ET types par « -- » (format natif du site) ;
- la résolution de lieu passe par services.foncia_geocode, doublée ici ;
- échecs = ValueError, jamais une liste vide qui masquerait un problème.

Aucun appel réseau : le socle bloque le transport HTTP (tests/conftest.py),
`requests_mock` sert d'adaptateur.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import parsers.foncia as foncia_module
from parsers.foncia import FonciaParser, _dict_to_listing, _rooms_range, _search_body
from tests.helpers.factories import (
    make_city_location,
    make_criteria,
    make_whole_city_location,
)

SEARCH_API_URL = "https://fnc-api.prod.fonciatech.net/annonces/annonces/search"

TOULOUSE = make_whole_city_location(
    city="Toulouse", postal_codes=("31000", "31200"), insee="31555"
)


def make_storage(slug: str | None = "toulouse-31"):
    """Un storage minimal : juste le cache géo, qui rend `slug`.

    `None` simule un échec de résolution mémorisé."""
    storage = MagicMock()
    storage.foncia_geo.get_cached.return_value = (
        {"area_key": "k", "slug_id": slug, "resolved_at": None} if slug else None
    )
    return storage


# Un item tel que renvoyé par /annonces/search en location (capture réelle
# Toulouse, champs optionnels retirés pour ne garder que ce que lit le parser).
def _ad(reference: str = "331698636", **overrides) -> dict:
    item = {
        "typeAnnonce": "location",
        "typeBien": "appartement",
        "canonicalUrl": f"/location/toulouse-31200/appartement/{reference}.htm",
        "reference": reference,
        "libelle": "APPARTEMENT",
        "description": "T3 avec parking",
        "localisation": {
            "ville": "TOULOUSE",
            "departement": "31",
            "codePostal": "31200",
            "locality": {"libelleDisplay": "Toulouse (31)", "arrondissement": "2"},
        },
        "loyer": 860,
        "loyerAnnexe": 0,
        "mediasCDN": [f"https://cdn.example/{reference}.jpg"],
        "medias": [f"https://storage.example/{reference}.jpg"],
        "surface": {"habitable": 63.5, "totale": 63.5},
        "nbPiece": 3,
        "nbChambre": 0,
        "noteConsoEnergie": "C",
        "noteEmissionGES": "A",
        "exclusivite": False,
        "datePublication": "2026-08-21T17:06:49+02:00",
        "status": "active",
    }
    item.update(overrides)
    return item


class TestRoomsRange:
    @pytest.mark.parametrize(
        ("rooms", "expected"),
        [
            ([], None),
            ([2], (2, 2)),
            ([2, 3], (2, 3)),  # contigu -> plage native fidèle
            ([1, 3], None),  # trou -> PAS d'élargissement, recadrage local
            ([4, 5], (4, None)),  # « 5 » vaut « 5 et plus » : max ouvert
            ([5], (5, None)),
            ([6], (5, None)),  # toute valeur >= 5 signale le « et plus »
        ],
        ids=[
            "vide", "egalite", "contigu", "non-contigu", "borne-ouverte",
            "cinq-plus", "six-signale-cinq-plus",
        ],
    )
    def test_the_canonical_room_list_becomes_a_native_range(self, rooms, expected):
        assert _rooms_range({"rooms": rooms}) == expected


class TestSearchBody:
    def test_the_post_body_mirrors_canonical_criteria(self):
        """Miroir exact : périmètre, types, bornes prix/surface, plage de
        pièces contiguë. C'est CE corps que le scraper envoie."""
        body = _search_body(
            ["apartment"],
            {"priceMin": 500, "priceMax": 900, "surfaceMin": 40, "rooms": [2, 3]},
            ["toulouse-31", "vannes-56000"],
        )

        assert body == {
            "type": "location",
            "filters": {
                "localities": {"slugs": ["toulouse-31", "vannes-56000"]},
                "typesBien": ["appartement"],
                "prix": {"min": 500, "max": 900},
                "surface": {"min": 40},
                "nbPiece": {"min": 2, "max": 3},
            },
            "size": foncia_module.PAGE_SIZE,
        }

    def test_non_contiguous_rooms_go_without_native_filter(self):
        """[1, 3] n'est pas élargi en {1..3} : sans filtre natif, le filtrage
        exact reste local."""
        body = _search_body(["apartment"], {"rooms": [1, 3]}, ["toulouse-31"])

        assert "nbPiece" not in body["filters"]

    def test_page_two_is_carried_in_the_body(self):
        body = _search_body(["apartment"], {}, ["toulouse-31"], page=2)

        assert body["page"] == 2


class TestDictToListing:
    def test_an_item_maps_every_displayed_field(self):
        listing = _dict_to_listing(_ad())

        assert listing is not None
        assert listing.listing_id == "foncia_331698636"
        assert listing.url == (
            "https://fr.foncia.com/location/toulouse-31200/appartement/331698636.htm"
        )
        assert listing.price == "860 €"
        assert listing.price_value == 860.0
        assert listing.surface == "63.5"
        assert listing.rooms == "3"
        assert listing.zip_code == "31200"
        assert listing.city == "Toulouse"
        assert listing.district == "2"
        assert listing.location == "Toulouse (31)"
        assert listing.property_type == "Appartement"
        assert listing.image_url == "https://cdn.example/331698636.jpg"  # CDN d'abord
        assert json.loads(listing.photos) == [
            {"url": "https://cdn.example/331698636.jpg", "alt": "", "key": ""}
        ]
        assert listing.epc == "C"
        assert listing.ges == "A"
        # Issue #12 : ISO avec décalage +02:00 → converti en ISO-8601 UTC.
        assert listing.creation_date == "2026-08-21T15:06:49+00:00"

    def test_an_item_without_canonical_url_is_unusable(self):
        """Pas de lien -> pas d'annonce utilisable, plutôt qu'une fiche morte."""
        assert _dict_to_listing(_ad(canonicalUrl="")) is None

    def test_a_missing_habitable_surface_falls_back_to_totale(self):
        item = _ad(surface={"totale": 70.0})

        assert _dict_to_listing(item).surface == "70.0"

    def test_bedrooms_are_not_reported_while_the_source_underfills_them(self):
        """nbChambre vaut 0 sur tous les items capturés : on ne remonte pas un
        zéro inventé."""
        assert _dict_to_listing(_ad()).rooms == "3"


class TestBuildSearchUrls:
    def test_one_url_joins_slugs_and_types_with_double_dashes(self):
        """Format natif vérifié en direct : le SSR de
        /location/a--b/appartement--maison?advanced= envoie exactement notre
        requête d'union — UNE seule URL pour toute la recherche."""
        parser = FonciaParser(storage=make_storage())
        criteria = {
            "locations": [TOULOUSE, make_city_location(city="Vannes", postal_code="56000",
                                            insee="56260")],
            "propertyTypes": ["apartment", "house"],
        }
        # Vannes : le cache rend son slug (résolution doublée).
        storage = make_storage()
        known = {"city:31555": "toulouse-31", "56260": "vannes-56000"}

        def cached(area_key):
            return {"area_key": area_key, "slug_id": known.get(area_key),
                    "resolved_at": None}

        storage.foncia_geo.get_cached.side_effect = cached

        criteria["priceMax"] = 1300  # appliqué côté API, PAS dans le lien
        parser = FonciaParser(storage=storage)
        assert parser.build_search_urls(criteria) == [
            "https://fr.foncia.com/location/toulouse-31--vannes-56000"
            "/appartement--maison?advanced="
        ]

    def test_a_scope_without_resolved_slug_is_dropped_from_the_url(self):
        storage = make_storage(slug=None)  # échec de résolution mémorisé
        parser = FonciaParser(storage=storage)

        assert parser.build_search_urls({"locations": [TOULOUSE]}) == []

    def test_no_location_means_no_url(self):
        parser = FonciaParser()

        assert parser.build_search_urls({}) == []


class TestSearchQueryParams:
    def test_the_url_carries_no_filter_on_purpose(self):
        """Ouverte à froid (sans session du site), une URL filtrée répond 403
        et, répétée, bannit l'IP du visiteur — vérifié le 23/08/2026, y
        compris sur des liens générés par Foncia lui-même. Le lien partagé
        ne porte donc QUE le périmètre ; les bornes restent au niveau API."""
        from parsers.foncia import _search_query_params

        assert _search_query_params() == [("advanced", "")]


class TestValidityAndCapabilities:
    def test_every_canonical_scope_level_is_usable(self):
        """Tous les niveaux sont dérivables : la géo retrouve le nom officiel
        quand la localisation n'en porte pas."""
        parser = FonciaParser()

        assert parser.has_valid_criteria(make_criteria())
        assert parser.has_valid_criteria(make_criteria(locations=[TOULOUSE]))

    def test_an_empty_search_is_not_valid(self):
        assert not FonciaParser().has_valid_criteria({})

    def test_buying_is_declared_unsupported_before_any_scrape(self):
        """Location seulement : le front prévient l'utilisateur via
        cannot_search_reason, et scrape() refuse défensivement."""
        parser = FonciaParser()
        criteria = make_criteria(transaction="buy")

        reason = parser.cannot_search_reason(criteria)
        assert reason is not None
        assert "Achat" in reason

        with pytest.raises(ValueError, match="que la location"):
            parser.scrape(criteria)

    def test_to_native_never_mutates_the_shared_criteria(self):
        """Les critères sont partagés entre sources pendant un scrape : aucune
        source n'a le droit de les modifier sur place."""
        import copy

        parser = FonciaParser()
        criteria = make_criteria(rooms=[2, 3], priceMax=900)
        snapshot = copy.deepcopy(criteria)

        native = parser.to_native(criteria)

        assert native == snapshot
        assert native is criteria  # identité conservée : rien à traduire


class TestScrape:
    @pytest.fixture
    def post_mock(self, requests_mock):
        def install(pages: list[list[dict]], total: int | None = None):
            responses = [
                {"json": {"annonces": items, "count": len(items),
                          **({"total": total} if total is not None else {})}}
                for items in pages
            ]
            return requests_mock.post(SEARCH_API_URL, responses)

        return install

    def test_a_happy_scrape_returns_local_listings(self, post_mock):
        mock = post_mock([[_ad("r1"), _ad("r2")]])
        parser = FonciaParser(storage=make_storage())

        listings = parser.scrape(make_criteria(
            locations=[TOULOUSE], propertyTypes=["apartment"]
        ))

        assert [ad.listing_id for ad in listings] == ["foncia_r1", "foncia_r2"]
        sent = mock.request_history[0].json()
        assert sent["filters"]["localities"]["slugs"] == ["toulouse-31"]
        assert sent["type"] == "location"

    def test_out_of_perimeter_listings_are_filtered_locally(self, post_mock):
        """Une annonce d'ailleurs (code postal hors périmètre) ne passe pas,
        même si le site la glisse dans la réponse."""
        elsewhere = _ad("r9", localisation={
            "ville": "PARIS", "departement": "75", "codePostal": "75001",
            "locality": {"libelleDisplay": "Paris (75)"},
        })
        post_mock([[_ad("r1"), elsewhere]])
        parser = FonciaParser(storage=make_storage())

        listings = parser.scrape(make_criteria(locations=[TOULOUSE]))

        assert [ad.listing_id for ad in listings] == ["foncia_r1"]

    def test_price_bounds_are_rechecked_locally(self, post_mock):
        post_mock([[_ad("cheap", loyer=100), _ad("ok", loyer=800)]])
        parser = FonciaParser(storage=make_storage())

        listings = parser.scrape(make_criteria(locations=[TOULOUSE], priceMax=900))

        # Le filtre natif prix max 900 est envoyé ; un item à 100 € y répond
        # aussi (pas de prixMin demandé) : les DEUX passent le recadrage.
        assert {ad.listing_id for ad in listings} == {"foncia_cheap", "foncia_ok"}

        parser2 = FonciaParser(storage=make_storage())
        listings = parser2.scrape(make_criteria(locations=[TOULOUSE],
                                                priceMax=900, priceMin=500))
        assert [ad.listing_id for ad in listings] == ["foncia_ok"]

    def test_inactive_and_unknown_type_items_are_skipped(self, post_mock):
        pages = [[
            _ad("r1"),
            _ad("sold", status="inactive"),
            _ad("weird", typeBien="chalet"),
            _ad("r1"),  # doublon exact
        ]]
        post_mock(pages)
        parser = FonciaParser(storage=make_storage())

        listings = parser.scrape(make_criteria(locations=[TOULOUSE]))

        assert [ad.listing_id for ad in listings] == ["foncia_r1"]


class TestScrapePagination:
    @pytest.fixture
    def log_messages(self):
        from loguru import logger

        messages: list[str] = []
        sink_id = logger.add(lambda msg: messages.append(msg.record["message"]), level="DEBUG")
        yield messages
        logger.remove(sink_id)

    def test_pages_are_walked_until_a_short_one(self, requests_mock, monkeypatch):
        """Pagination serveur : on avance tant que la page est pleine ; une
        page courte clôt la boucle."""
        monkeypatch.setattr(foncia_module, "PAGE_SIZE", 2)
        responses = [
            {"json": {"annonces": [_ad("r1"), _ad("r2")], "count": 2, "total": 3}},
            {"json": {"annonces": [_ad("r3")], "count": 1, "total": 3}},
        ]
        mock = requests_mock.post(SEARCH_API_URL, responses)
        parser = FonciaParser(storage=make_storage())

        listings = parser.scrape(make_criteria(locations=[TOULOUSE]))

        assert [ad.listing_id for ad in listings] == ["foncia_r1", "foncia_r2", "foncia_r3"]
        assert mock.call_count == 2
        assert mock.request_history[1].json()["page"] == 2

    def test_truncation_beyond_the_client_cap_is_logged_not_hidden(
        self, requests_mock, monkeypatch, log_messages
    ):
        """MAX_PAGES pleines alors que total dépasse : avertissement explicite,
        jamais un résultat silencieusement incomplet."""
        monkeypatch.setattr(foncia_module, "PAGE_SIZE", 1)
        monkeypatch.setattr(foncia_module, "MAX_PAGES", 2)
        responses = [
            {"json": {"annonces": [_ad("r1")], "count": 1, "total": 10}},
            {"json": {"annonces": [_ad("r2")], "count": 1, "total": 10}},
        ]
        requests_mock.post(SEARCH_API_URL, responses)
        parser = FonciaParser(storage=make_storage())

        listings = parser.scrape(make_criteria(locations=[TOULOUSE]))

        assert len(listings) == 2
        assert any("tronqué" in message for message in log_messages)

    def test_an_unexpected_response_is_a_loud_failure(self, requests_mock):
        """Pas de liste annonces -> ValueError : le pipeline doit distinguer
        un échec d'un périmètre vide légitime."""
        requests_mock.post(SEARCH_API_URL, json={"erreur": "mystère"})
        parser = FonciaParser(storage=make_storage())

        with pytest.raises(ValueError, match="inattendue"):
            parser.scrape(make_criteria(locations=[TOULOUSE]))

    def test_an_empty_perimeter_stays_an_empty_list(self, requests_mock):
        """Un vrai zéro du site est légitime : [] sans erreur."""
        requests_mock.post(SEARCH_API_URL, json={"annonces": [], "count": 0, "total": 0})
        parser = FonciaParser(storage=make_storage())

        assert parser.scrape(make_criteria(locations=[TOULOUSE])) == []

    def test_no_resolved_scope_fails_instead_of_searching_all_france(self):
        """Sans identifiant de lieu résolu : ValueError, jamais une requête
        nationale déguisée en recherche vide."""
        parser = FonciaParser(storage=make_storage(slug=None))

        with pytest.raises(ValueError, match="Aucune localisation"):
            parser.scrape(make_criteria(locations=[TOULOUSE]))
