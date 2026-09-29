"""Contrat de l'empreinte conservatrice de déduplication inter-sites."""

from repositories.listing_repo import _dedup_key
from tests.helpers.factories import make_listing

DESCRIPTION = (
    "Appartement lumineux entièrement rénové, proche des commerces et des transports, "
    "avec grand séjour, cuisine équipée, deux chambres, rangements et balcon exposé sud."
)


def _listing(**overrides):
    values = {
        "description": DESCRIPTION,
        "price": "1 250 €/mois",
        "price_value": 1250,
        "surface": "65 m²",
        "zip_code": "75013",
        "city": "Paris",
        "property_type": "Appartement",
    }
    values.update(overrides)
    return make_listing(
        **values,
    )


def test_same_property_from_two_sources_has_the_same_key():
    first = _listing(listing_id="sl_1", source="seloger", legacy_id="LOT-7842")
    second = _listing(listing_id="bi_9", source="bienici", legacy_id="LOT-7842")

    assert _dedup_key(first) == _dedup_key(second)
    assert _dedup_key(first).startswith("listing:v2:")


def test_accents_case_and_spacing_do_not_change_the_key():
    variant = _listing(
        description=DESCRIPTION.upper().replace("É", "E") + "   ",
        property_type=" appartement ",
        legacy_id=" lot-7842 ",
    )

    assert _dedup_key(variant) == _dedup_key(_listing(legacy_id="LOT-7842"))


def test_a_structuring_difference_keeps_the_listings_distinct():
    original = _listing(legacy_id="LOT-7842")
    assert _dedup_key(_listing(price_value=1300, legacy_id="LOT-7842")) != _dedup_key(original)
    assert _dedup_key(_listing(surface="66 m²", legacy_id="LOT-7842")) != _dedup_key(original)
    assert _dedup_key(_listing(zip_code="75014", legacy_id="LOT-7842")) != _dedup_key(original)


def test_two_lots_with_identical_generic_content_are_not_merged_without_a_common_strong_discriminant():
    first_lot = _listing(listing_id="sl_lot_a", source="seloger", legacy_id="PROGRAMME-A-101")
    second_lot = _listing(listing_id="bi_lot_b", source="bienici", legacy_id="PROGRAMME-B-202")

    assert _dedup_key(first_lot) != _dedup_key(second_lot)

    without_reference_a = _listing(listing_id="sl_lot_a", source="seloger")
    without_reference_b = _listing(listing_id="bi_lot_b", source="bienici")
    assert _dedup_key(without_reference_a) is None
    assert _dedup_key(without_reference_b) is None


def test_exact_coordinates_agency_title_and_rooms_form_a_strong_common_discriminant():
    common = {
        "agency": "Agence des Batignolles",
        "title": "Appartement familial avec balcon",
        "rooms": "3 pièces",
        "latitude": 48.887123,
        "longitude": 2.316789,
        "location_precision": "exacte",
    }

    first = _listing(listing_id="bi_geo", source="bienici", **common)
    second = _listing(
        listing_id="gh_geo",
        source="guyhoquet",
        **{**common, "latitude": 48.887119, "longitude": 2.316793},
    )

    assert _dedup_key(first) == _dedup_key(second)
    assert _dedup_key(first).startswith("listing:v2:")


def test_approximate_coordinates_never_form_a_geo_discriminant():
    listing = _listing(
        agency="Agence des Batignolles",
        title="Appartement familial avec balcon",
        rooms="3 pièces",
        latitude=48.8871,
        longitude=2.3168,
        location_precision="approximative",
    )

    assert _dedup_key(listing) is None


def test_weak_evidence_never_creates_a_key():
    assert _dedup_key(_listing(description="Trop court")) is None
    assert _dedup_key(_listing(price="", price_value=None)) is None
    assert _dedup_key(_listing(surface="")) is None
    assert _dedup_key(_listing(zip_code="", city="")) is None
