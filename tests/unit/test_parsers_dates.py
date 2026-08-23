"""Contrat issue #12 : date de publication source vs date de récupération scraper.

Chaque parser doit alimenter ``Listing.creation_date`` avec la date de
PUBLICATION de l'annonce chez sa source, normalisée par
``parsers/_dates.normaliser_creation_date`` en ISO-8601 UTC canonique
(``YYYY-MM-DDTHH:MM:SS+00:00``) — ou la sentinelle ``unknown`` quand la
source ne fournit rien (JAMAIS une chaîne vide). La date de récupération
par le scraper vit ailleurs (``first_seen``/``found_at``).

Le normalisateur est LA référence partagée : la migration de données de
``storage.py`` importe ce même module, donc un seul endroit définit ce qui
est parsable (aucune dérive écriture / reprise historique).
"""

from __future__ import annotations

import re

import pytest
from bs4 import BeautifulSoup

from parsers._dates import DATE_INCONNUE, normaliser_creation_date
from parsers.bienici import _dict_to_listing as bienici_dict_to_listing
from parsers.century21 import _dict_to_listing as c21_dict_to_listing
from parsers.citya import _parse_card as citya_parse_card
from parsers.essetpm import EssetPmParser
from parsers.foncia import _dict_to_listing as foncia_dict_to_listing
from parsers.guyhoquet import _dict_to_listing as gh_dict_to_listing
from parsers.laforet import _dict_to_listing as laforet_dict_to_listing
from parsers.orpi import _dict_to_listing as orpi_dict_to_listing
from parsers.pap import _dict_to_listing as pap_dict_to_listing
from parsers.seloger import _dict_to_listing as seloger_dict_to_listing

# ---------------------------------------------------------------------------
# Le normalisateur partagé — contrat du format de stockage
# ---------------------------------------------------------------------------


class TestNormaliserCreationDate:
    def test_a_date_only_seloger_format_becomes_utc_midnight(self):
        assert normaliser_creation_date("2026-07-01") == "2026-07-01T00:00:00+00:00"

    def test_a_z_suffixed_milliseconds_bienici_format_is_normalized(self):
        assert (
            normaliser_creation_date("2026-07-01T00:55:44.275Z")
            == "2026-07-01T00:55:44+00:00"
        )

    def test_an_explicit_offset_is_converted_to_utc(self):
        # 2026-08-06T00:00:00+02:00 == 2026-08-05T22:00:00 UTC : le jour
        # calendaire peut reculer — c'est attendu, l'UTC fait foi.
        assert (
            normaliser_creation_date("2026-08-06T00:00:00+02:00")
            == "2026-08-05T22:00:00+00:00"
        )
        assert (
            normaliser_creation_date("2026-08-21T17:06:49+02:00")
            == "2026-08-21T15:06:49+00:00"
        )

    @pytest.mark.parametrize(
        "brute",
        [
            "2026-08-22 18:08:17",  # guyhoquet : séparateur espace
            "2026-08-22T18:08:17",  # naïf avec T
        ],
        ids=["espace", "T"],
    )
    def test_a_naive_datetime_is_assumed_utc(self, brute):
        assert normaliser_creation_date(brute) == "2026-08-22T18:08:17+00:00"

    def test_a_canonical_value_is_stable(self):
        """Idempotence : normaliser une valeur déjà canonique ne la change pas
        (la migration peut donc tourner deux fois sans réécrire)."""
        valeur = "2026-07-01T12:34:56+00:00"
        assert normaliser_creation_date(valeur) == valeur

    def test_microseconds_are_truncated_for_lexical_sorting_homogeneity(self):
        assert (
            normaliser_creation_date("2026-07-01T12:34:56.999+02:00")
            == "2026-07-01T10:34:56+00:00"
        )

    @pytest.mark.parametrize(
        ("valeur", "etiquette"),
        [
            (None, "None"),
            ("", "vide"),
            ("   ", "blancs"),
            ("unknown", "sentinelle déjà posée"),
            ("abc", "garbage"),
            ("03/08/2026", "format français non ISO"),
            ("2026-13-45", "date calendaire invalide"),
        ],
    )
    def test_anything_unusable_yields_the_unknown_sentinel(self, valeur, etiquette):
        assert normaliser_creation_date(valeur) == DATE_INCONNUE, etiquette


# ---------------------------------------------------------------------------
# Par source : extraction quand le payload porte une date…
# ---------------------------------------------------------------------------


class TestDatesPerSource:
    """Un cas nominal par source dont le payload expose une date."""

    def test_seloger_creationDate(self):
        listing = seloger_dict_to_listing({"id": "1", "creationDate": "2026-07-01"})
        assert listing.creation_date == "2026-07-01T00:00:00+00:00"

    def test_bienici_publicationDate(self):
        listing = bienici_dict_to_listing(
            {"id": "1", "publicationDate": "2026-07-01T00:55:44.275Z"}
        )
        assert listing.creation_date == "2026-07-01T00:55:44+00:00"

    def test_orpi_onMarketSince(self):
        listing = orpi_dict_to_listing(
            {"id": "7", "slug": "annonce-appartement-toulouse-31000",
             "onMarketSince": "2026-08-06T00:00:00+02:00"}
        )
        assert listing.creation_date == "2026-08-05T22:00:00+00:00"

    def test_foncia_datePublication(self):
        listing = foncia_dict_to_listing(
            {
                "reference": "331698636",
                "canonicalUrl": "/location/toulouse-31200/appartement/331698636.htm",
                "datePublication": "2026-08-21T17:06:49+02:00",
            }
        )
        assert listing is not None
        assert listing.creation_date == "2026-08-21T15:06:49+00:00"

    def test_guyhoquet_created_at(self):
        listing = gh_dict_to_listing({"id": "1894191", "created_at": "2026-08-22 18:08:17"})
        assert listing.creation_date == "2026-08-22T18:08:17+00:00"


class TestDatesMissingPerSource:
    """…et sentinelle « unknown » quand la clé manque ou vaut None."""

    def test_seloger_absente_et_none(self):
        assert (
            seloger_dict_to_listing({"id": "1"}).creation_date == DATE_INCONNUE
        )
        assert (
            seloger_dict_to_listing({"id": "1", "creationDate": None}).creation_date
            == DATE_INCONNUE
        )

    def test_bienici_none(self):
        assert (
            bienici_dict_to_listing({"id": "1", "publicationDate": None}).creation_date
            == DATE_INCONNUE
        )

    def test_orpi_absente(self):
        listing = orpi_dict_to_listing({"id": "7", "slug": "annonce-x-33000"})
        assert listing.creation_date == DATE_INCONNUE

    def test_foncia_absente(self):
        listing = foncia_dict_to_listing(
            {"reference": "r1", "canonicalUrl": "/location/x/appartement/1.htm"}
        )
        assert listing is not None
        assert listing.creation_date == DATE_INCONNUE

    def test_guyhoquet_absente(self):
        assert gh_dict_to_listing({"id": "1"}).creation_date == DATE_INCONNUE


# ---------------------------------------------------------------------------
# Sources dont le payload n'expose AUCUNE date : sentinelle documentée.
# Chaque docstring du parser explique pourquoi (ne jamais inventer un champ).
# ---------------------------------------------------------------------------


def laforet_data() -> dict:
    return {
        "reference": "REF123",
        "url": "https://www.laforet.com/agence-immobiliere/toulouse/annonce/location-REF123",
        "city": "Toulouse",
        "zip_code": "31000",
        "price_value": 850,
        "surface": "65 m²",
        "rooms": "3 pièces",
        "agency": "Laforêt Toulouse",
    }


def pap_data() -> dict:
    return {
        "uid": "12345678",
        "url": "https://www.pap.fr/annonces/appartement-toulouse-g439-12345678",
        "title": "Appartement T3 Toulouse",
        "price_text": "850 €",
        "price_value": 850.0,
        "surface": "65 m²",
        "rooms": "3 pièces",
        "city": "Toulouse",
        "zip_code": "31000",
        "description": "Bel appartement.",
        "image_url": "",
        "property_type": "appartement",
    }


def century21_data() -> dict:
    return {
        "uid": "15581564422",
        "url": "https://www.century21.fr/trouver_logement/detail/15581564422/",
        "title": "Appartement Toulouse",
        "surface": "65 m²",
        "rooms": "3 pièces",
        "city": "Toulouse",
        "zip_code": "31000",
        "description": "Bel appartement.",
        "image_url": "/imagesBien/s3/x.jpg",
        "property_type": "appartement",
        "price_value": 850.0,
        # Century21 : _dict_to_listing attend l'objet match du prix extrait
        # de la carte (même contrat que _parse_cards).
        "price": re.search(r"(\d[\d\s]*)", "850 €"),
    }


class TestSourcesWithoutAnyDate:
    """Cinq sources ne publient aucune date exploitable dans leur flux :
    la docstring de chaque parser documente l'enquête qui le prouve."""

    def test_laforet_les_time_datetimes_sont_des_articles_de_blog_pas_des_annonces(self):
        listing = laforet_dict_to_listing(laforet_data())
        assert listing.creation_date == DATE_INCONNUE

    def test_pap_la_date_de_publication_n_existe_que_sur_les_fiches_detail(self):
        listing = pap_dict_to_listing(pap_data())
        assert listing.creation_date == DATE_INCONNUE

    def test_century21_le_balisage_des_cartes_ne_porte_pas_de_date(self):
        listing = c21_dict_to_listing(century21_data())
        assert listing.creation_date == DATE_INCONNUE

    def test_essetpm_datecommandedpe_est_une_date_de_dpe_pas_de_publication(self):
        parser = EssetPmParser()
        listing = parser._to_listing(
            {"codeAnnonce": "29383-23", "dateCommandeDpe": "2025-02-17"}
        )
        assert listing.creation_date == DATE_INCONNUE

    def test_citya_les_property_card_ne_portent_pas_de_date(self):
        html = (
            '<div class="property-card" data-itemid="GES12040005-198" '
            'data-itemname="Appartement T3" data-price="486">'
            '<a href="/annonces/location/appartement/bordeaux-33063/GES12040005-198"></a>'
            "<p>Bordeaux (33000)</p></div>"
        )
        card = BeautifulSoup(html, "html.parser").select_one("div.property-card")
        listing = citya_parse_card(card)
        assert listing is not None
        assert listing.creation_date == DATE_INCONNUE
