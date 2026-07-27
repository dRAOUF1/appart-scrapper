"""Tests des dataclasses de `models/` — le contrat entre la base et le reste.

Ces quatre objets ne contiennent presque pas de logique, mais ils portent trois
choses qui cassent silencieusement quand elles bougent :

* `Listing.from_dict` est la frontière base -> objet : elle FILTRE les colonnes
  inconnues, ce qui rend l'ajout d'une colonne DB indolore mais masque aussi
  une faute de frappe sur un nom de champ ;
* `User.public_dict()` est un invariant de sécurité : le jeton d'API ne doit
  jamais sortir dans une réponse HTTP ;
* `Search.should_scrape()` décide la cadence de tous les scrapes — ses bornes
  sont figées ici à l'horloge gelée, jamais à l'horloge réelle.

Aucune assertion sur le NOMBRE de champs : l'ancien test_models.py affirmait
`len(d) == 29`, si bien qu'ajouter `photos` cassait un test dont le message ne
disait rien du problème. On teste la présence et la valeur de ce qui compte.
"""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime, timedelta

import pytest
from freezegun import freeze_time

from models import Listing, ScrapeLog, Search, User
from tests.helpers.factories import (
    make_city_location,
    make_criteria,
    make_listing,
    make_photos_json,
    make_search_row,
    make_user_row,
)

NOW = datetime(2026, 7, 25, 12, 0, 0)


# ===========================================================================
# Listing
# ===========================================================================


@pytest.mark.parametrize(
    ("field_name", "expected"),
    [
        pytest.param("title", "", id="texte-vide-pas-none"),
        pytest.param("source", "", id="source-vide"),
        pytest.param("price_value", None, id="prix-numerique-inconnu"),
        pytest.param("is_private", False, id="booleen-faux"),
        pytest.param("is_new", False, id="is-new-faux"),
        pytest.param("has_3d_visit", False, id="visite-3d-fausse"),
        pytest.param("phone", "[]", id="telephones-chaine-json-vide"),
        pytest.param("photos", "[]", id="photos-chaine-json-vide"),
    ],
)
def test_a_minimal_listing_defaults_to_empty_values_never_none_for_text(field_name, expected):
    """Seul `price_value` peut valoir None (un prix inconnu n'est pas 0) ; tout
    le reste a un défaut typé, pour qu'un template n'affiche jamais « None »."""
    listing = Listing(listing_id="sl_123", url="https://example.com/123")

    assert getattr(listing, field_name) == expected


@pytest.mark.parametrize("field_name", ["phone", "photos"])
def test_phone_and_photos_are_json_strings_not_python_lists(field_name):
    """Piège structurel : ces deux colonnes sont stockées SÉRIALISÉES. Un
    appelant qui ferait `for photo in listing.photos` itérerait sur les
    caractères de la chaîne au lieu des photos, sans lever d'erreur."""
    listing = make_listing(photos=make_photos_json(2), phone='["0102030405"]')

    value = getattr(listing, field_name)

    assert isinstance(value, str)
    assert isinstance(json.loads(value), list)


def test_photos_keep_their_serialized_structure_through_a_round_trip():
    """Les photos portent url/alt/key : la sérialisation doit rester lisible
    par les templates après un aller-retour base."""
    photos_json = make_photos_json(3)
    listing = make_listing(photos=photos_json)

    restored = Listing.from_dict(listing.to_dict())
    decoded = json.loads(restored.photos)

    assert [p["key"] for p in decoded] == ["p0", "p1", "p2"]
    assert all(p["url"].startswith("https://cdn.example/") for p in decoded)


@pytest.mark.parametrize(
    "unknown_column",
    [
        pytest.param("first_seen", id="colonne-de-jointure-reelle"),
        pytest.param("notified", id="colonne-de-suivi-de-notification"),
        pytest.param("search_id", id="colonne-de-la-table-de-liaison"),
        pytest.param("id", id="cle-primaire-sql"),
        pytest.param("listingId", id="faute-de-frappe-camelcase"),
        pytest.param("", id="cle-vide"),
    ],
)
def test_from_dict_silently_drops_any_column_the_dataclass_does_not_declare(unknown_column):
    """C'est ce qui rend `SELECT l.*, sl.first_seen FROM ...` utilisable
    directement. Contrepartie assumée : une faute de frappe sur un nom de champ
    est ignorée sans le moindre signal — d'où le cas `listingId` ci-dessus, qui
    ne remplit PAS `listing_id`."""
    listing = Listing.from_dict(
        {"listing_id": "sl_2", "url": "https://x.example/2", "title": "Joli T2", unknown_column: "ignoré"}
    )

    assert listing.listing_id == "sl_2"
    assert listing.title == "Joli T2"
    assert not hasattr(listing, unknown_column)


def test_from_dict_accepts_a_whole_database_row_with_extra_join_columns():
    """Le cas réel : la ligne renvoyée par le repository porte les colonnes de
    la table de liaison en plus de celles de l'annonce."""
    row = {
        **make_listing(listing_id="sl_42", price_value=980.0).to_dict(),
        "id": 17,
        "search_id": 3,
        "first_seen": datetime(2026, 7, 1, 8, 30),
        "notified": True,
    }

    listing = Listing.from_dict(row)

    assert listing.listing_id == "sl_42"
    assert listing.price_value == 980.0
    assert listing.to_dict().keys() == make_listing().to_dict().keys()


@pytest.mark.parametrize("missing", ["listing_id", "url"])
def test_from_dict_still_requires_the_two_mandatory_identity_fields(missing):
    """`listing_id` et `url` n'ont pas de défaut : une ligne incomplète lève au
    lieu de produire une annonce fantôme sans identifiant."""
    data = {"listing_id": "sl_3", "url": "https://x.example/3"}
    del data[missing]

    with pytest.raises(TypeError, match=missing):
        Listing.from_dict(data)


def test_to_dict_exposes_every_declared_field_and_nothing_else():
    """Contrat de forme, sans compter les champs : la sérialisation suit la
    dataclasse, donc ajouter un champ ne casse rien ici."""
    listing = make_listing()

    assert set(listing.to_dict()) == {f.name for f in fields(Listing)}


def test_to_dict_preserves_values_verbatim_without_reformatting_them():
    listing = make_listing(
        listing_id="sl_99",
        title="Appartement T3",
        price="1 200 €/mois",
        price_value=1200.0,
        surface="65",
        is_private=False,
        source="seloger",
    )

    exported = listing.to_dict()
    expected = {
        "listing_id": "sl_99",
        "title": "Appartement T3",
        "price": "1 200 €/mois",
        "price_value": 1200.0,
        "surface": "65",
        "is_private": False,
        "source": "seloger",
    }

    assert {key: exported[key] for key in expected} == expected


def test_to_dict_returns_a_detached_copy():
    """`asdict` copie : muter le dict renvoyé (ce que font les routes pour
    enrichir la réponse) ne doit pas altérer l'annonce."""
    listing = make_listing(title="Original")

    exported = listing.to_dict()
    exported["title"] = "Modifié"

    assert listing.title == "Original"


def test_a_listing_survives_a_full_to_dict_from_dict_round_trip():
    """Invariant de persistance : ce qui est écrit en base est ce qui en
    ressort."""
    listing = make_listing(price_value=1234.5, photos=make_photos_json(1), epc="C", ges="B")

    assert Listing.from_dict(listing.to_dict()) == listing


# ===========================================================================
# Search — modes de liste noire
# ===========================================================================


def test_the_two_blacklist_modes_are_exclude_and_no_notify():
    """Contrat exposé au front (menu déroulant) et validé par les routes : les
    valeurs comptent autant que leur nombre."""
    assert Search.valid_blacklist_modes() == ["exclude", "no_notify"]


def test_the_default_blacklist_mode_is_one_of_the_valid_ones():
    """Garde-fou contre une désynchronisation entre le défaut de la dataclasse
    et la liste blanche — l'une des deux pourrait bouger seule."""
    search = Search(id=1, user_id=1, label="Test", ntfy_topic="test")

    assert search.blacklist_mode in Search.valid_blacklist_modes()
    assert search.blacklist_mode == "exclude"


def test_valid_blacklist_modes_is_callable_without_an_instance():
    """`@staticmethod` : les routes l'appellent sur la classe, pour peupler le
    formulaire avant qu'aucune recherche n'existe."""
    assert Search.valid_blacklist_modes() == Search(
        id=1, user_id=1, label="x", ntfy_topic="x"
    ).valid_blacklist_modes()


# ===========================================================================
# Search — has_valid_criteria
# ===========================================================================


def _search(**overrides) -> Search:
    """Une `Search` construite depuis la même ligne que les repositories."""
    row = make_search_row(**overrides)
    return Search(**{k: v for k, v in row.items() if k in Search.__dataclass_fields__})


@pytest.mark.parametrize(
    "criteria",
    [
        pytest.param({}, id="dict-vide"),
        pytest.param(None, id="none"),
        pytest.param("locations", id="chaine"),
        pytest.param([], id="liste-vide"),
        pytest.param([make_city_location()], id="liste-de-localisations-au-lieu-d-un-dict"),
        pytest.param(0, id="zero"),
    ],
)
def test_criteria_that_are_not_a_populated_mapping_are_never_valid(criteria):
    """Court-circuit avant même d'interroger les parsers : sans critères, aucune
    source ne peut chercher."""
    assert _search(criteria=criteria).has_valid_criteria() is False


def test_criteria_without_any_usable_location_are_invalid():
    """Un prix seul ne définit pas une recherche : il manque le « où »."""
    assert _search(criteria={"priceMax": 1500}, sources=["seloger", "laforet"]).has_valid_criteria() is False


def test_a_location_with_an_insee_code_is_valid_for_every_source():
    assert _search(criteria=make_criteria(), sources=["seloger", "laforet"]).has_valid_criteria() is True


def test_one_source_out_of_several_is_enough_semantics_of_any():
    """Sémantique `any` : sans aucune localisation, un placeId SeLoger collé à
    la main suffit à lui seul (repli qui court-circuite la résolution
    automatique), mais Laforêt — qui n'a pas d'équivalent — reste inexploitable.
    La recherche reste donc lançable — c'est ce que le pipeline attend, il
    saute simplement la source qui ne peut pas."""
    criteria = {"sourceOverrides": {"seloger": {"placeIds": ["AD08FR31096"]}}}

    assert _search(criteria=criteria, sources=["seloger"]).has_valid_criteria() is True
    assert _search(criteria=criteria, sources=["laforet"]).has_valid_criteria() is False
    assert _search(criteria=criteria, sources=["seloger", "laforet"]).has_valid_criteria() is True


@pytest.mark.parametrize(
    "sources",
    [
        pytest.param(["leboncoin"], id="une-seule-source-inconnue"),
        pytest.param(["leboncoin", "pap"], id="plusieurs-sources-inconnues"),
        pytest.param([""], id="slug-vide"),
    ],
)
def test_an_unknown_source_is_skipped_instead_of_raising(sources):
    """`get_parser` lève `ValueError` sur un slug inconnu, attrapé par un
    `continue` : une recherche dont la source a été retirée du code ne fait pas
    exploser le scheduler, elle devient simplement non lançable."""
    assert _search(criteria=make_criteria(), sources=sources).has_valid_criteria() is False


def test_an_unknown_source_does_not_hide_a_valid_one():
    """Le `continue` doit passer à la source suivante, pas abandonner la boucle."""
    search = _search(criteria=make_criteria(), sources=["leboncoin", "seloger"])

    assert search.has_valid_criteria() is True


@pytest.mark.parametrize(
    ("sources", "source", "expected"),
    [
        pytest.param([], "seloger", True, id="liste-vide-repli-sur-le-champ-source"),
        pytest.param(None, "seloger", True, id="none-repli-sur-le-champ-source"),
        pytest.param([], "leboncoin", False, id="repli-sur-une-source-inconnue"),
        pytest.param(["laforet"], "leboncoin", True, id="la-liste-prime-sur-le-champ-source"),
    ],
)
def test_the_legacy_single_source_field_is_the_fallback_when_sources_is_empty(sources, source, expected):
    """`self.sources or [self.source]` : les recherches créées avant le
    multi-sources n'ont que `source` rempli et doivent continuer de tourner."""
    search = _search(criteria=make_criteria(), sources=sources, source=source)

    assert search.has_valid_criteria() is expected


# ===========================================================================
# Search — should_scrape
# ===========================================================================


@freeze_time(NOW)
def test_a_never_scraped_active_search_with_valid_criteria_is_due():
    search = _search(last_scraped=None)

    assert search.should_scrape(datetime.now()) is True


@freeze_time(NOW)
def test_an_inactive_search_is_never_due_even_when_long_overdue():
    """Première condition de la cascade : la mise en pause doit primer sur tout
    le reste."""
    search = _search(is_active=False, last_scraped=NOW - timedelta(days=30))

    assert search.should_scrape(datetime.now()) is False


@freeze_time(NOW)
def test_a_search_without_usable_criteria_is_never_due():
    """Deuxième condition : inutile de réveiller un scrape qui n'a aucune source
    capable de chercher."""
    search = _search(criteria={"priceMax": 1500}, last_scraped=None)

    assert search.should_scrape(datetime.now()) is False


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        pytest.param(timedelta(0), False, id="scrape-a-l-instant"),
        pytest.param(timedelta(minutes=4, seconds=59), False, id="4min59-trop-tot"),
        pytest.param(
            timedelta(minutes=5) - timedelta(microseconds=1),
            False,
            id="une-microseconde-avant-l-intervalle",
        ),
        pytest.param(timedelta(minutes=5), True, id="pile-l-intervalle-borne-incluse"),
        pytest.param(
            timedelta(minutes=5) + timedelta(microseconds=1),
            True,
            id="une-microseconde-apres-l-intervalle",
        ),
        pytest.param(timedelta(minutes=10), True, id="deux-fois-l-intervalle"),
    ],
)
@freeze_time(NOW)
def test_the_interval_boundary_is_inclusive_to_the_microsecond(elapsed, expected):
    """Troisième condition : `last_scraped > now - interval` -> pas encore l'heure.

    La comparaison est stricte, donc `last_scraped` exactement égal au seuil
    déclenche le scrape. Avec un intervalle de 5 minutes et un scheduler qui
    tourne toutes les 30 s, se tromper d'un signe ici double ou halve la
    fréquence réelle de tous les scrapes du parc.
    """
    search = _search(scrape_interval=5, last_scraped=NOW - elapsed)

    assert search.should_scrape(datetime.now()) is expected


@pytest.mark.parametrize("interval", [1, 5, 60, 1440])
@freeze_time(NOW)
def test_the_boundary_holds_for_every_allowed_interval(interval):
    """Les mêmes bornes exactes, sur toute la plage acceptée par
    `validate_scrape_interval` (1 minute à 24 h)."""
    exactly_due = _search(scrape_interval=interval, last_scraped=NOW - timedelta(minutes=interval))
    one_tick_early = _search(
        scrape_interval=interval,
        last_scraped=NOW - timedelta(minutes=interval) + timedelta(microseconds=1),
    )

    assert exactly_due.should_scrape(datetime.now()) is True
    assert one_tick_early.should_scrape(datetime.now()) is False


@freeze_time(NOW)
def test_a_last_scraped_in_the_future_blocks_the_scrape():
    """Une horloge de base en avance (ou un fuseau mal réglé) suffit à geler une
    recherche : il n'y a aucun garde-fou contre un `last_scraped` futur, et le
    comportement observable est un blocage silencieux jusqu'à ce que l'heure
    réelle rattrape."""
    search = _search(scrape_interval=5, last_scraped=NOW + timedelta(hours=2))

    assert search.should_scrape(datetime.now()) is False


@freeze_time(NOW)
def test_should_scrape_uses_the_now_it_is_given_not_the_wall_clock():
    """`now` est un paramètre pour que le scheduler impose un instant unique à
    tout un tour de boucle. Un `datetime.now()` interne rendrait le résultat
    dépendant de la durée de la boucle."""
    search = _search(scrape_interval=5, last_scraped=NOW)

    assert search.should_scrape(NOW) is False
    assert search.should_scrape(NOW + timedelta(minutes=5)) is True


# NOTE : cette cascade de trois conditions est DUPLIQUÉE dans
# `main.scheduled_scrape_job` (voir main.py, autour de la ligne 215), qui
# reconstruit la même logique à la main sur des dicts de lignes SQL sans jamais
# instancier `Search`. Les deux implémentations peuvent donc diverger dès qu'une
# règle change ici — et c'est celle de main.py qui pilote réellement les scrapes
# automatiques. Voir le rapport : `Search.should_scrape` n'a aujourd'hui aucun
# appelant en production.


# ===========================================================================
# Search — to_dict
# ===========================================================================


def test_search_to_dict_serializes_datetimes_to_iso_and_keeps_the_rest_as_is():
    """Le dict part directement en JSON dans les réponses d'API : un `datetime`
    non converti lèverait un TypeError au moment de la sérialisation."""
    search = _search(
        created_at=datetime(2026, 1, 1, 12, 0, 0),
        last_scraped=datetime(2026, 7, 25, 9, 30, 15),
    )

    exported = search.to_dict()

    assert exported["created_at"] == "2026-01-01T12:00:00"
    assert exported["last_scraped"] == "2026-07-25T09:30:15"
    assert exported["criteria"] == make_criteria()
    assert exported["is_active"] is True
    assert exported["blacklist_mode"] == "exclude"


def test_search_to_dict_leaves_absent_datetimes_as_none():
    """`None` reste `None` : « jamais scrapé » ne doit pas devenir une date."""
    search = _search(created_at=None, last_scraped=None)

    exported = search.to_dict()

    assert exported["last_scraped"] is None
    assert exported["created_at"] is None


def test_search_to_dict_exposes_every_declared_field():
    assert set(_search().to_dict()) == {f.name for f in fields(Search)}


def test_search_to_dict_returns_a_detached_copy_of_the_nested_criteria():
    """`asdict` recopie en profondeur : une route qui enrichit les critères de
    la réponse ne doit pas modifier ceux de la recherche."""
    search = _search(criteria={"priceMax": 1500})

    exported = search.to_dict()
    exported["criteria"]["priceMax"] = 1

    assert search.criteria["priceMax"] == 1500


# ===========================================================================
# User — l'invariant de sécurité
# ===========================================================================


def test_public_dict_never_exposes_the_api_token():
    """INVARIANT DE SÉCURITÉ. `api_token` est le seul secret d'authentification
    de l'API : il ne doit apparaître dans aucune réponse HTTP. Ce test est là
    pour échouer bruyamment si `public_dict` est un jour réécrit à partir de
    `asdict` sans le `pop`."""
    user = User(**make_user_row(api_token="secret-tres-sensible"))

    public = user.public_dict()

    assert "api_token" not in public
    assert "secret-tres-sensible" not in json.dumps(public, default=str)


def test_public_dict_still_carries_everything_the_front_needs():
    """Retirer le jeton ne doit pas retirer l'identité : le front affiche le nom
    d'utilisateur et la date d'inscription."""
    user = User(id=7, username="alice", api_token="token", created_at=datetime(2026, 1, 1, 12, 0, 0))

    assert user.public_dict() == {
        "id": 7,
        "username": "alice",
        "created_at": "2026-01-01T12:00:00",
    }


def test_to_dict_keeps_the_token_because_it_is_the_internal_serialization():
    """La distinction entre les deux méthodes est tout l'objet de la classe :
    `to_dict` sert en interne (session, journalisation), `public_dict` sort."""
    user = User(**make_user_row(api_token="token-alice"))

    assert user.to_dict()["api_token"] == "token-alice"


@pytest.mark.parametrize(
    ("created_at", "expected"),
    [
        pytest.param(datetime(2026, 1, 1, 12, 0, 0), "2026-01-01T12:00:00", id="datetime-vers-iso"),
        pytest.param(None, None, id="none-reste-none"),
    ],
)
def test_user_to_dict_only_converts_a_real_datetime(created_at, expected):
    user = User(id=1, username="alice", api_token="token", created_at=created_at)

    assert user.to_dict()["created_at"] == expected


def test_public_dict_is_safe_to_call_on_a_user_without_a_creation_date():
    """Un utilisateur créé par script peut ne pas avoir de `created_at` : la
    protection du jeton ne doit pas dépendre de ce champ."""
    user = User(id=1, username="alice", api_token="secret")

    assert user.public_dict() == {"id": 1, "username": "alice", "created_at": None}


def test_public_dict_returns_a_copy_so_popping_does_not_damage_the_user():
    user = User(id=1, username="alice", api_token="secret")

    user.public_dict()

    assert user.api_token == "secret"
    assert user.to_dict()["api_token"] == "secret"


# ===========================================================================
# ScrapeLog
# ===========================================================================


def test_scrape_log_to_dict_serializes_both_timestamps_to_iso():
    log = ScrapeLog(
        id=3,
        search_id=1,
        started_at=datetime(2026, 7, 1, 10, 0, 0),
        completed_at=datetime(2026, 7, 1, 10, 0, 30),
        status="success",
        listings_found=12,
        new_listings=3,
        duration_sec=30.0,
    )

    exported = log.to_dict()

    assert exported["started_at"] == "2026-07-01T10:00:00"
    assert exported["completed_at"] == "2026-07-01T10:00:30"
    assert exported["duration_sec"] == 30.0
    assert exported["status"] == "success"


@pytest.mark.parametrize(
    ("field_name", "expected"),
    [
        pytest.param("id", None, id="id-absent-avant-insertion"),
        pytest.param("search_id", 0, id="search-id-a-zero"),
        pytest.param("started_at", None, id="debut-inconnu"),
        pytest.param("completed_at", None, id="fin-inconnue-scrape-en-cours"),
        pytest.param("status", "", id="statut-vide"),
        pytest.param("listings_found", 0, id="aucune-annonce-trouvee"),
        pytest.param("new_listings", 0, id="aucune-nouveaute"),
        pytest.param("error_message", "", id="pas-d-erreur"),
        pytest.param("details", {}, id="details-vides"),
        pytest.param("duration_sec", 0.0, id="duree-nulle"),
        pytest.param("raw_logs", "", id="logs-bruts-vides"),
    ],
)
def test_a_fresh_scrape_log_has_neutral_defaults(field_name, expected):
    """Un log est créé au DÉBUT du scrape, avant de connaître ses résultats :
    tous les champs doivent avoir un défaut neutre insérable en base."""
    assert ScrapeLog().to_dict()[field_name] == expected


def test_a_running_scrape_log_keeps_its_completion_date_as_none():
    """`completed_at` à None est ce qui distingue un scrape en cours d'un scrape
    fini : la conversion ISO ne doit pas le transformer en date."""
    log = ScrapeLog(search_id=1, started_at=datetime(2026, 7, 1, 10, 0, 0), status="running")

    exported = log.to_dict()

    assert exported["started_at"] == "2026-07-01T10:00:00"
    assert exported["completed_at"] is None


def test_scrape_log_details_are_kept_as_a_structured_dict_not_stringified():
    """`details` porte le décompte par source ; il est stocké en JSONB et doit
    rester une structure exploitable côté template."""
    details = {"seloger": {"found": 10, "new": 2}, "laforet": {"found": 2, "new": 1}}
    log = ScrapeLog(search_id=1, details=details)

    exported = log.to_dict()

    assert exported["details"] == details
    assert exported["details"]["seloger"]["found"] == 10


def test_scrape_log_details_default_is_not_shared_between_instances():
    """`field(default_factory=dict)` : deux logs créés sans détails ne doivent
    pas partager le même dict, sinon le premier scrape pollue tous les autres."""
    first, second = ScrapeLog(), ScrapeLog()

    first.details["seloger"] = {"found": 1}

    assert second.details == {}


def test_scrape_log_to_dict_returns_a_detached_copy_of_details():
    log = ScrapeLog(search_id=1, details={"seloger": {"found": 10}})

    exported = log.to_dict()
    exported["details"]["seloger"]["found"] = 0

    assert log.details["seloger"]["found"] == 10


def test_scrape_log_to_dict_exposes_every_declared_field():
    assert set(ScrapeLog().to_dict()) == {f.name for f in fields(ScrapeLog)}
