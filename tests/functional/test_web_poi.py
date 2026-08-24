"""Proxy POI côté HTTP (#27) — contrat des routes `/listings/poi*`.

Quatre pans testés ici, sans navigateur ni réseau :

* authentification — routes de SESSION (`@require_login`) comme tout le
  web_bp : un anonyme est renvoyé vers /login, jamais servi ;
* validation d'entrée — bbox strictement validée (400 explicite), couche
  inconnue en 404 : aucune requête Overpass ne part sur une entrée pourrie ;
* contrat d'échec — Overpass indisponible (timeout, 5xx) donne **200** avec
  `points: []` et `degrade: true`, JAMAIS un 500 : l'échec chez le fournisseur
  n'est pas une erreur de notre API, la carte reste utilisable ;
* rendu de la page — la barre d'outils carte porte le contrôle Calques
  (bouton + conteneur + toast), et le JS couches est chargé.

Le cache mémoire du proxy est vidé avant/après chaque test par le socle
(`reset_global_state` -> `poi_overpass.vider_cache()`) : aucun état ne fuit
entre deux tests.
"""

from __future__ import annotations

import pytest
import requests

from services import poi_overpass as poi
from tests.functional.conftest import make_view_listing

BBOX_BORDEAUX = "44.84,-0.58,44.90,-0.50"


# ---------------------------------------------------------------------------
# Authentification : routes de session
# ---------------------------------------------------------------------------


class TestAuthentification:
    @pytest.mark.parametrize(
        "url",
        [
            "/listings/poi",
            f"/listings/poi/transports?bbox={BBOX_BORDEAUX}",
        ],
    )
    def test_un_anonyme_est_renvoye_vers_le_login(self, client, url):
        resp = client.get(url)

        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# Catalogue des couches
# ---------------------------------------------------------------------------


class TestCatalogue:
    def test_le_catalogue_expose_les_couches_du_registre(self, web_client):
        resp = web_client.get("/listings/poi")

        assert resp.status_code == 200
        couches = resp.get_json()["couches"]
        assert {"id": "transports", "label": "Transports", "icone": "🚌"} in couches


# ---------------------------------------------------------------------------
# Validation stricte des entrées
# ---------------------------------------------------------------------------


class TestValidation:
    def test_une_couche_inconnue_est_un_404(self, web_client):
        resp = web_client.get(f"/listings/poi/ecoles?bbox={BBOX_BORDEAUX}")

        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "bbox",
        [
            pytest.param(None, id="absente"),
            pytest.param("44.84,-0.58,44.90", id="trois-valeurs"),
            pytest.param("45,-0.58,44.90,-0.50", id="inversee"),
            pytest.param("nan,-0.58,44.90,-0.50", id="nan"),
            pytest.param("abc,-0.58,44.90,-0.50", id="non-numerique"),
            pytest.param("43,0,46,3", id="amplitude-trop-grande"),
        ],
    )
    def test_une_bbox_invalide_est_un_400_sans_appel_overpass(self, web_client, requests_mock, bbox):
        params = {} if bbox is None else {"bbox": bbox}
        resp = web_client.get("/listings/poi/transports", query_string=params)

        assert resp.status_code == 400
        assert "error" in resp.get_json()
        assert requests_mock.call_count == 0


# ---------------------------------------------------------------------------
# Proxy : succès et échec upstream
# ---------------------------------------------------------------------------


def _stub_overpass(requests_mock_fixture, elements):
    requests_mock_fixture.post(poi.OVERPASS_URL, json={"elements": elements})


class TestProxySucces:
    def test_la_reponse_est_normalisee(self, web_client, requests_mock):
        _stub_overpass(requests_mock, [
            {"lat": 44.855, "lon": -0.569,
             "tags": {"railway": "station", "name": "Bordeaux-Saint-Jean"}},
            {"lat": 44.852, "lon": -0.575,
             "tags": {"highway": "bus_stop"}},  # sans nom -> libellé du type
        ])

        resp = web_client.get(f"/listings/poi/transports?bbox={BBOX_BORDEAUX}")

        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["couche"] == "transports"
        assert payload["degrade"] is False
        # Tri pertinence : la gare d'abord ; fallback de nom appliqué au bus.
        assert payload["points"] == [
            {"lat": 44.855, "lon": -0.569, "nom": "Bordeaux-Saint-Jean", "type": "gare"},
            {"lat": 44.852, "lon": -0.575, "nom": "Arrêt de bus", "type": "bus"},
        ]

    def test_deux_demandes_identiques_ne_frappent_overpass_qu_une_fois(self, web_client, requests_mock):
        _stub_overpass(requests_mock, [])

        web_client.get(f"/listings/poi/transports?bbox={BBOX_BORDEAUX}")
        web_client.get(f"/listings/poi/transports?bbox={BBOX_BORDEAUX}")

        assert requests_mock.call_count == 1

    def test_la_bbox_arrondie_part_chez_overpass(self, web_client, requests_mock):
        from urllib.parse import parse_qs

        _stub_overpass(requests_mock, [])
        web_client.get("/listings/poi/transports?bbox=44.84123,-0.58123,44.89678,-0.49678")

        ql = parse_qs(requests_mock.last_request.text)["data"][0]
        assert "[bbox:44.841,-0.581,44.897,-0.497]" in ql


class TestProxyEchecUpstream:
    """L'échec Overpass ne doit JAMAIS devenir un 500."""

    def test_un_timeout_donne_une_couche_vide_et_degradee(self, web_client, requests_mock):
        requests_mock.post(
            poi.OVERPASS_URL, exc=requests.exceptions.ConnectTimeout()
        )

        resp = web_client.get(f"/listings/poi/transports?bbox={BBOX_BORDEAUX}")

        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["points"] == []
        assert payload["degrade"] is True

    def test_un_500_upstream_est_absorbe(self, web_client, requests_mock):
        requests_mock.post(poi.OVERPASS_URL, status_code=500)

        resp = web_client.get(f"/listings/poi/transports?bbox={BBOX_BORDEAUX}")

        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["degrade"] is True and payload["points"] == []


# ---------------------------------------------------------------------------
# Rendu de la page : contrôle Calques présent
# ---------------------------------------------------------------------------


class TestRenduCalques:
    @pytest.fixture(autouse=True)
    def configure(self, storage, owned_search):
        from datetime import datetime

        rows = [{**make_view_listing(), "found_at": datetime(2026, 7, 1, 9, 0)}]
        storage.listings.count_listings_for_search.return_value = 1
        storage.listings.get_listings_for_search.return_value = rows

    def test_la_page_listings_porte_le_controle_calques(self, web_client):
        resp = web_client.get("/listings/1")

        assert resp.status_code == 200
        text = resp.data.decode()
        # Bouton + panneau des couches + toast d'échec discret.
        assert 'id="poi-panel-btn"' in text
        assert 'aria-controls="poi-controls"' in text
        assert 'id="poi-controls"' in text
        assert 'id="map-toast"' in text
        assert "map.js" in text
