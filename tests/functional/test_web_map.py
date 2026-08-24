"""Carte d'une recherche + repères personnels (#26) — contrat HTTP.

Trois pans testés ici, sans navigateur :

* `GET /listings/<id>/map-data` — authentification, garde propriétaire (404
  sans divulguer l'existence), et contenu JSON exact (les coordonnées
  stockées sont exposées telles quelles ; les annonces sans coords ne sont
  JAMAIS envoyées, c'est le SQL du repo qui les filtre — prouvé en
  intégration) ;
* CRUD des repères — CSRF obligatoire (client à protection ACTIVE), validation
  stricte des coordonnées (999 et 0 rejetés), ownership strict en 404 pour
  PATCH/DELETE (balayage IDOR stérile) ;
* rendu de la page `/listings/<id>` — onglets Liste/Carte, conteneur carte,
  formulaire de repère avec son jeton CSRF, vendors Leaflet servis localement,
  et NON-RÉGRESSION #14 (échafaudage du scroll infini intact).

Le JS lui-même (`static/map.js`) est volontairement hors de portée ici :
vanilla JS validé au navigateur — ces tests couvrent le contrat serveur.
"""

from __future__ import annotations

import re

import pytest

from tests.functional.conftest import make_view_listing


def make_pin(**overrides) -> dict:
    """Un repère tel que le repo le renvoie (ligne DB)."""
    row = {
        "id": 5,
        "user_id": 1,
        "label": "Bon quartier",
        "note": "proche du parc",
        "icon": "⭐",
        "latitude": 48.8566,
        "longitude": 2.3522,
        "created_at": None,
    }
    row.update(overrides)
    return row


def _map_points() -> list[dict]:
    """Les points que le repo renvoie : UNIQUEMENT des annonces géolocalisées."""
    return [
        {**make_view_listing(listing_id="sl_geo", latitude=48.8566,
                             longitude=2.3522, location_precision="exacte",
                             price="1 200 €", surface="65"),
         },
        {**make_view_listing(listing_id="bi_flou", latitude=44.857861,
                             longitude=-0.576437,
                             location_precision="approximative")},
    ]


@pytest.fixture
def web_client_csrf(app, user):
    """Client connecté sur l'app avec protection CSRF ACTIVE."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user["id"]
        sess["username"] = user["username"]
    return client


def _csrf_token(client) -> str:
    """Le jeton porté par le formulaire d'ajout de repère de la page."""
    page = client.get("/listings/1").data.decode()
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page)
    assert match, "le formulaire d'ajout de repère doit exposer le jeton CSRF"
    return match.group(1)


# ---------------------------------------------------------------------------
# GET /listings/<id>/map-data
# ---------------------------------------------------------------------------


class TestMapDataEndpoint:
    def test_an_anonymous_client_is_redirected_to_login(self, client):
        resp = client.get("/listings/1/map-data")

        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_a_foreign_search_is_a_404_and_fetches_nothing(
        self, web_client, storage, foreign_search
    ):
        resp = web_client.get("/listings/1/map-data")

        assert resp.status_code == 404
        storage.listings.get_map_points_for_search.assert_not_called()
        storage.map_pins.list_for_user.assert_not_called()

    def test_the_payload_carries_geolocated_listings_and_personal_pins(
        self, web_client, storage, owned_search, user
    ):
        points = _map_points()
        pins = [make_pin()]
        storage.listings.get_map_points_for_search.return_value = points
        storage.map_pins.list_for_user.return_value = pins

        resp = web_client.get("/listings/1/map-data")

        assert resp.status_code == 200
        payload = resp.get_json()
        assert [p["listing_id"] for p in payload["points"]] == ["sl_geo", "bi_flou"]
        geoloc = payload["points"][0]
        assert geoloc["latitude"] == 48.8566          # les coords stockées…
        assert geoloc["longitude"] == 2.3522          # …sont exposées telles quelles
        assert geoloc["location_precision"] == "exacte"
        assert geoloc["price"] == "1 200 €"
        pin = payload["pins"][0]
        assert pin["id"] == 5 and pin["label"] == "Bon quartier"
        # Les repères sont GLOBAUX : listés pour L'UTILISATEUR, pas la recherche.
        storage.map_pins.list_for_user.assert_called_once_with(user["id"])

    def test_a_search_without_any_geolocated_listing_still_serves_its_pins(
        self, web_client, storage, owned_search, user
    ):
        """Une annonce sans coordonnées n'est pas une erreur : le serveur
        n'envoie aucun point, la carte reste utilisable pour les repères."""
        storage.listings.get_map_points_for_search.return_value = []
        storage.map_pins.list_for_user.return_value = []

        resp = web_client.get("/listings/1/map-data")

        assert resp.status_code == 200
        assert resp.get_json() == {"points": [], "pins": []}

    def test_points_are_fetched_for_the_requested_search_only(
        self, web_client, storage, user
    ):
        from tests.helpers.factories import make_search_row

        row = make_search_row(id=7, user_id=user["id"])
        storage.searches.get_search.side_effect = lambda search_id: row if search_id == 7 else None
        storage.listings.get_map_points_for_search.return_value = []

        web_client.get("/listings/7/map-data")

        storage.listings.get_map_points_for_search.assert_called_once_with(7)


# ---------------------------------------------------------------------------
# CRUD des repères : CSRF puis ownership puis validation
# ---------------------------------------------------------------------------

VALID_PIN = {"label": "Bon quartier", "latitude": "48.8566",
             "longitude": "2.3522", "note": "proche du parc", "icon": "⭐"}


class TestPinCsrf:
    def test_a_mutation_without_token_is_refused(self, web_client_csrf):
        """CSRFProtect refuse AVANT la route : ni champ csrf_token ni en-tête
        X-CSRFToken → 400, et surtout aucune écriture."""
        resp = web_client_csrf.post("/pins", data=dict(VALID_PIN))

        assert resp.status_code == 400
        assert resp.get_json() is None or "csrf" in resp.get_data(as_text=True).lower()

    def test_the_hidden_form_field_is_accepted(
        self, web_client_csrf, storage, owned_search
    ):
        jeton = _csrf_token(web_client_csrf)
        storage.map_pins.create.return_value = make_pin()
        payload = dict(VALID_PIN, csrf_token=jeton)

        resp = web_client_csrf.post("/pins", data=payload)

        assert resp.status_code == 201
        storage.map_pins.create.assert_called_once()

    def test_the_x_csrftoken_header_is_accepted_too(
        self, web_client_csrf, storage, owned_search
    ):
        """Le DELETE du JS n'a pas de corps : le jeton voyage en en-tête."""
        jeton = _csrf_token(web_client_csrf)
        storage.map_pins.delete.return_value = True

        resp = web_client_csrf.delete(
            "/pins/5", headers={"X-CSRFToken": jeton}
        )

        assert resp.status_code == 200
        storage.map_pins.delete.assert_called_once_with(1, 5)


class TestPinOwnership:
    @pytest.fixture
    def storage_with_foreign_pin(self, storage):
        """`get_owned` ne renvoie QUE les repères de l'utilisateur 1 : un id
        d'un autre utilisateur est None, comme une ligne inexistante."""
        storage.map_pins.get_owned.side_effect = (
            lambda user_id, pin_id: make_pin(id=pin_id) if (pin_id == 5 and user_id == 1) else None
        )
        return storage

    def test_patch_on_someone_elses_pin_is_a_404(self, web_client, storage_with_foreign_pin):
        storage_with_foreign_pin.map_pins.get_owned.side_effect = lambda uid, pid: None

        resp = web_client.patch("/pins/999", data={"label": "Piraté"})

        assert resp.status_code == 404
        storage_with_foreign_pin.map_pins.update.assert_not_called()

    def test_delete_on_someone_elses_pin_is_a_404(self, web_client, storage_with_foreign_pin):
        storage_with_foreign_pin.map_pins.delete.return_value = False

        resp = web_client.delete("/pins/77")

        assert resp.status_code == 404

    def test_delete_scopes_the_sql_on_both_id_and_user(
        self, web_client, storage, owned_search, user
    ):
        storage.map_pins.delete.return_value = True

        web_client.delete("/pins/9")

        storage.map_pins.delete.assert_called_once_with(user["id"], 9)


class TestPinValidation:
    def test_a_valid_pin_is_created_for_the_current_user(self, web_client, storage, user):
        storage.map_pins.create.return_value = make_pin()

        resp = web_client.post("/pins", data=dict(VALID_PIN))

        assert resp.status_code == 201
        kwargs = storage.map_pins.create.call_args.kwargs
        assert kwargs == {
            "user_id": user["id"],
            "label": "Bon quartier",
            "note": "proche du parc",
            "icon": "⭐",
            "latitude": pytest.approx(48.8566),
            "longitude": pytest.approx(2.3522),
        }

    @pytest.mark.parametrize(
        ("latitude", "longitude"),
        [
            pytest.param("999", "2.35", id="latitude-hors-borne"),
            pytest.param("-91", "2.35", id="latitude-sous-borne"),
            pytest.param("48.85", "999", id="longitude-hors-borne"),
            pytest.param("abc", "2.35", id="latitude-illisible"),
            pytest.param("", "2.35", id="latitude-absente"),
        ],
    )
    def test_out_of_bounds_or_unreadable_coordinates_are_rejected(
        self, web_client, storage, latitude, longitude
    ):
        resp = web_client.post(
            "/pins", data={**VALID_PIN, "latitude": latitude, "longitude": longitude}
        )

        assert resp.status_code == 400
        storage.map_pins.create.assert_not_called()

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"latitude": "0", "longitude": "0"}, id="zero-zero-golfe-de-guinee"),
            pytest.param({"latitude": "0.000000", "longitude": "12.5"}, id="latitude-zero-seule"),
        ],
    )
    def test_zero_coordinates_never_become_pins(self, web_client, storage, payload):
        """La sentinelle « pas de position » est rejetée par la route aussi :
        aucun pin au golfe de Guinée, jamais."""
        resp = web_client.post(
            "/pins",
            data={"label": "Nawak", **payload},
        )

        assert resp.status_code == 400
        storage.map_pins.create.assert_not_called()

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({}, id="sans-libelle"),
            pytest.param({"label": ""}, id="libelle-vide"),
            pytest.param({"label": "   "}, id="libelle-blanc"),
            pytest.param({"label": "x" * 81}, id="libelle-trop-long"),
        ],
    )
    def test_an_invalid_label_is_rejected(self, web_client, storage, payload):
        data = {**payload, "latitude": "48.85", "longitude": "2.35"}
        resp = web_client.post("/pins", data=data)

        assert resp.status_code == 400
        storage.map_pins.create.assert_not_called()

    def test_an_unknown_icon_is_rejected(self, web_client, storage):
        resp = web_client.post(
            "/pins", data={**VALID_PIN, "icon": "<script>"}
        )

        assert resp.status_code == 400
        storage.map_pins.create.assert_not_called()

    def test_a_note_over_500_chars_is_rejected(self, web_client, storage):
        resp = web_client.post(
            "/pins", data={**VALID_PIN, "note": "n" * 501}
        )

        assert resp.status_code == 400
        storage.map_pins.create.assert_not_called()

    def test_patch_rejects_an_empty_label_and_keeps_the_old_one(
        self, web_client, storage
    ):
        storage.map_pins.get_owned.return_value = make_pin()

        resp = web_client.patch("/pins/5", data={"label": "   "})

        assert resp.status_code == 400
        storage.map_pins.update.assert_not_called()


# ---------------------------------------------------------------------------
# Rendu de la page : échafaudage carte + non-régression #14
# ---------------------------------------------------------------------------


class TestCarteRender:
    @pytest.fixture(autouse=True)
    def configure(self, storage, owned_search):
        from datetime import datetime

        rows = [{**make_view_listing(), "found_at": datetime(2026, 7, 1, 9, 0)}]
        storage.listings.count_listings_for_search.return_value = 1
        storage.listings.get_listings_for_search.return_value = rows

    def test_leaflet_vendors_are_served_locally_non_empty(self, client):
        for nom in ("leaflet.js", "leaflet.css"):
            reponse = client.get(f"/static/vendor/{nom}")

            assert reponse.status_code == 200
            assert len(reponse.data) > 1000, f"{nom} semble tronqué ou vide"
            reponse.close()

    def test_the_listings_page_carries_the_map_scaffold(
        self, web_client, storage, owned_search
    ):
        resp = web_client.get("/listings/1")

        assert resp.status_code == 200
        text = resp.data.decode()
        # Onglets + vue carte + conteneur + panneau des repères.
        assert 'id="tab-liste"' in text
        assert 'id="tab-carte"' in text
        assert 'id="carte-view"' in text
        assert 'id="map-canvas"' in text
        assert 'id="pin-form"' in text
        assert 'name="csrf_token"' in text
        assert 'id="map-empty"' in text
        # Attribution OSM exigée par la licence tuiles : portée par leaflet.css
        # + attribution param dans map.js — le script doit être chargé.
        assert "vendor/leaflet.js" in text
        assert "vendor/leaflet.css" in text
        assert "map.js" in text

    def test_the_infinite_scroll_scaffold_survives_the_tabs(
        self, web_client, storage, owned_search
    ):
        """Non-régression #14 : la grille et sa sentinelle vivent toujours
        dans #liste-view, le script du scroll reste chargé."""
        resp = web_client.get("/listings/1")

        text = resp.data.decode()
        assert 'id="liste-view"' in text
        assert 'id="listing-grid"' in text
        assert 'id="listings-sentinel"' in text
        assert 'id="listings-spinner"' in text
        assert 'id="listings-end"' in text
        assert "listings_scroll.js" in text

    def test_the_carte_tab_exists_even_for_a_search_without_any_listing(
        self, web_client, storage, owned_search, user
    ):
        row = owned_search
        storage.searches.get_search.return_value = row
        storage.listings.count_listings_for_search.return_value = 0
        storage.listings.get_listings_for_search.return_value = []

        resp = web_client.get("/listings/1")

        assert resp.status_code == 200
        text = resp.data.decode()
        assert 'id="tab-carte"' in text
        assert 'id="carte-view"' in text
