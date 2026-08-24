"""Contrat issue #26 : géolocalisation hybride des annonces.

Chaque source vague 1 doit alimenter ``Listing.latitude`` /
``Listing.longitude`` / ``Listing.location_precision`` depuis son champ NATIF
— sans aucune requête réseau additionnelle — et laisser l'annonce intacte
(coords NULL) quand la source ne fournit rien :

* bienici  : ``realEstateAds.json`` → ``blurInfo.position.lat/lon``, flou
             « disk » (~50 m) ou centre de commune ⇒ 'approximative' ;
* ORPI     : ``/recherche/ajax`` → ``latitude``/``longitude`` directs,
             ``blurredness`` non nul ⇒ 'approximative' ;
* Foncia   : ``annonces[].localisation.geoPoint`` UNIQUEMENT — le
             ``locality.geoPoint`` est le centroïde de toute la commune
             (strictement identique sur toutes les annonces d'une ville) et
             doit rester mort ;
* essetpm  : fiche ``.lat/.lon`` en rejetant les DEUX formes d'absence,
             ``null`` ET ``0.0/0.0``.

Valeurs réalistes recopiées des captures réelles du 23/08/2026
(bienici_test.json, orpi_test2.json, foncia_search_live.json,
detail_29383-23.json). La validation partagée est couverte par
tests/unit/test_coords.py ; ici on prouve que chaque parser LIT LE BON CHAMP.
"""

from __future__ import annotations

import pytest

from models.listing import Listing
from parsers._coords import PRECISION_APPROXIMATIVE, PRECISION_COMMUNE, PRECISION_EXACTE
from parsers.bienici import _dict_to_listing as bienici_dict_to_listing
from parsers.essetpm import _listing_from_detail as essetpm_listing_from_detail
from parsers.foncia import _dict_to_listing as foncia_dict_to_listing
from parsers.orpi import _dict_to_listing as orpi_dict_to_listing

# ---------------------------------------------------------------------------
# bienici — blurInfo.position (capture réelle)
# ---------------------------------------------------------------------------

BIENICI_BLUR_DISK = {
    "id": "bi-test-1",
    "title": "T2 Bordeaux Chartrons",
    "price": 830,
    "transactionType": "rent",
    "city": "Bordeaux",
    "postalCode": "33000",
    # Forme EXACTE de la capture : disque de ~50 m autour du bien.
    "blurInfo": {
        "type": "disk", "radius": 50,
        "position": {"lat": 44.85786105489427, "lon": -0.5764371365744323},
    },
}


class TestBienIciCoords:
    def test_the_disk_blur_yields_approximate_native_coords(self):
        listing = bienici_dict_to_listing(BIENICI_BLUR_DISK)

        assert listing.latitude == pytest.approx(44.85786105489427)
        assert listing.longitude == pytest.approx(-0.5764371365744323)
        assert listing.location_precision == PRECISION_APPROXIMATIVE

    def test_city_or_arrondissement_centre_is_also_approximate(self):
        payload = dict(BIENICI_BLUR_DISK)
        payload["blurInfo"] = {
            "type": "cityOrArrondissement",
            "position": {"lat": 48.8762016, "lon": 2.2358772},
        }

        listing = bienici_dict_to_listing(payload)

        assert (listing.latitude, listing.longitude) == (48.8762016, 2.2358772)
        assert listing.location_precision == PRECISION_APPROXIMATIVE

    def test_an_ad_without_blurinfo_stays_valid_but_ungeolocated(self):
        payload = {k: v for k, v in BIENICI_BLUR_DISK.items() if k != "blurInfo"}

        listing = bienici_dict_to_listing(payload)

        assert listing.listing_id == "bi_bi-test-1"  # l'annonce reste valide…
        assert listing.latitude is None           # …simplement absente de la carte
        assert listing.longitude is None
        assert listing.location_precision == ""

    def test_a_sentinel_zero_position_is_rejected(self):
        payload = dict(BIENICI_BLUR_DISK)
        payload["blurInfo"] = {"type": "disk",
                               "position": {"lat": 0.0, "lon": 0.0}}

        listing = bienici_dict_to_listing(payload)

        assert listing.latitude is None


# ---------------------------------------------------------------------------
# ORPI — latitude/longitude + blurredness (capture réelle)
# ---------------------------------------------------------------------------

ORPI_ITEM = {
    "reference": "bace612c-1111-2222-3333-444455556666",
    "id": "orpi-id-1",
    "slug": "appartement-t3-bordeaux-33000-bace612c",
    "transaction": "rent",
    "type": "appartement",
    "price": 1050,
    "surface": 68,
    "nbRooms": 3,
    "city": {"name": "Bordeaux"},
    "district": {},
    "estatePhotos": [],
    # Valeurs de la capture orpi_test2.json.
    "latitude": 44.8671067,
    "longitude": -0.5526845,
    "blurredness": 1,
}


class TestOrpiCoords:
    def test_direct_coords_with_blurredness_are_approximate(self):
        listing = orpi_dict_to_listing(ORPI_ITEM)

        assert listing.latitude == pytest.approx(44.8671067)
        assert listing.longitude == pytest.approx(-0.5526845)
        assert listing.location_precision == PRECISION_APPROXIMATIVE

    def test_without_blurredness_the_position_is_exact(self):
        payload = {k: v for k, v in ORPI_ITEM.items() if k != "blurredness"}
        payload["latitude"] = 44.8457833
        payload["longitude"] = -0.5816855

        listing = orpi_dict_to_listing(payload)

        assert listing.location_precision == PRECISION_EXACTE

    def test_missing_coords_leave_the_item_ungeolocated(self):
        payload = {
            k: v for k, v in ORPI_ITEM.items()
            if k not in ("latitude", "longitude")
        }

        listing = orpi_dict_to_listing(payload)

        assert listing.latitude is None
        assert listing.location_precision == ""


# ---------------------------------------------------------------------------
# Foncia — localisation.geoPoint JAMAIS locality.geoPoint (captures réelles)
# ---------------------------------------------------------------------------

# Centroïde de Toulouse, STRICTEMENT identique sur toutes les annonces de la
# capture — c'est précisément ce qu'il ne faut jamais lire.
FONCIA_CENTROIDE_TLS = {"lon": 1.4486309, "lat": 43.6389829}


def foncia_item(geo_point: dict | None) -> dict:
    return {
        "reference": "331698636",
        "canonicalUrl": "/location/toulouse-31000/appartement/331698636.htm",
        "typeBien": "Appartement",
        "loyer": 760,
        "nbPiece": 2,
        "localisation": {
            "ville": "TOULOUSE",
            "codePostal": "31000",
            # Position PROPRE au bien (83 % de couverture, capture réelle).
            "geoPoint": geo_point,
            # Le piège : toujours présent, toujours identique.
            "locality": {"libelleDisplay": "Toulouse - Bonnefoy",
                         "geoPoint": FONCIA_CENTROIDE_TLS},
        },
    }


class TestFonciaCoords:
    def test_the_annonce_geopoint_wins_never_the_locality_one(self):
        listing = foncia_dict_to_listing(foncia_item({"lon": 1.43466, "lat": 43.615976}))

        assert listing.latitude == pytest.approx(43.615976)   # pas 43.6389829 !
        assert listing.longitude == pytest.approx(1.43466)    # pas 1.4486309 !
        assert listing.location_precision == PRECISION_EXACTE

    def test_every_annonce_of_a_city_must_not_share_the_same_point(self):
        """Deux annonces d'une même ville doivent atterrir à DEUX endroits :
        si un jour elles partagent le centroïde, c'est que le mauvais champ
        est lu."""
        premiere = foncia_dict_to_listing(
            foncia_item({"lon": 1.43466, "lat": 43.615976})
        )
        seconde = foncia_dict_to_listing(
            foncia_item({"lon": 1.430114, "lat": 43.665832})
        )

        assert (premiere.latitude, premiere.longitude) != (
            seconde.latitude, seconde.longitude
        )

    def test_no_localisation_geopoint_means_ungeolocated_even_with_locality(self):
        """L'annonce n'a PAS de geoPoint propre mais le centroïde de la
        commune est là, tentant : il ne doit PAS être utilisé à la place."""
        item = foncia_item(None)

        listing = foncia_dict_to_listing(item)

        assert listing.city.title() == "Toulouse"
        assert listing.latitude is None
        assert listing.location_precision == ""


# ---------------------------------------------------------------------------
# essetpm — fiche .lat/.lon, double piège null ET 0.0/0.0 (fiche réelle)
# ---------------------------------------------------------------------------


class TestEssetPmCoords:
    @staticmethod
    def enrich(detail: dict) -> Listing:
        base = Listing(listing_id="essetpm_x", url="https://x/")
        return essetpm_listing_from_detail(base, detail)

    def test_real_detail_coordinates_are_extracted_as_exact(self):
        listing = self.enrich({
            "accroche": "Appartement T1 bis",
            # Valeurs exactes de tests/fixtures/essetpm/detail_29383-23.json.
            "lat": 48.8762016,
            "lon": 2.2358772,
        })

        assert listing.latitude == pytest.approx(48.8762016)
        assert listing.longitude == pytest.approx(2.2358772)
        assert listing.location_precision == PRECISION_EXACTE

    def test_null_coordinates_are_the_first_form_of_absence(self):
        listing = self.enrich({"accroche": "X", "lat": None, "lon": None})

        assert listing.latitude is None
        assert listing.location_precision == ""

    def test_zero_zero_is_the_second_form_of_absence(self):
        """Le golfe de Guinée observé en direct : 0.0/0.0 au lieu de null."""
        listing = self.enrich({"accroche": "X", "lat": 0.0, "lon": 0.0})

        assert listing.latitude is None
        assert listing.longitude is None

    def test_missing_keys_are_also_tolerated(self):
        listing = self.enrich({"accroche": "X"})

        assert listing.latitude is None


# ---------------------------------------------------------------------------
# Vocabulaire de précision — le fallback commune (#26 transverse) s'appuie
# sur le même vocabulaire ; ce test fige la cohérence inter-modules.
# ---------------------------------------------------------------------------


def test_commune_precision_completes_the_vocabulary():
    assert {PRECISION_EXACTE, PRECISION_APPROXIMATIVE, PRECISION_COMMUNE} == {
        "exacte", "approximative", "commune",
    }
