"""Tests unitaires de `services/poi_overpass.py`.

Le proxy POI de la carte (#27) porte quatre responsabilités, chacune avec sa
section ici :

* la VALIDATION de bbox — frontière d'entrée du proxy : toute bbox malformée,
  inversée, dégénérée, non finie (NaN/inf) ou trop ample est refusée AVANT
  toute requête Overpass ;
* la CONSTRUCTION de la requête QL — bbox arrondie (~3 décimales) pour
  maximiser les hits du cache, timeout court dans l'en-tête, sortie plafonnée
  (`out body N`) pour ne jamais surcharger la réponse ;
* le PARSING / la normalisation — tags OSM regroupés en types canoniques
  {bus/tram/metro/gare/ferry}, nom en fallback sur le libellé du type, éléments
  incomplets ignorés sans lever ;
* le CACHE mémoire — hit/miss, TTL expiré (freezegun), éviction FIFO à la
  taille max, et surtout : un ÉCHEC n'est jamais mis en cache (un incident
  transitoire ne doit pas être servi pendant tout le TTL).

AUCUN appel réseau réel : le socle bloque le transport HTTP
(tests/conftest.py), `requests_mock` sert d'adaptateur. Un échec simulé
(timeout, 5xx, JSON malformé) doit revenir en `(points=[], degrade=True)` —
JAMAIS une exception propagée vers la route.
"""

from __future__ import annotations

import pytest
import requests
from freezegun import freeze_time

from services import poi_overpass as poi

BBOX_PARIS = (48.812, 2.212, 48.957, 2.457)
BBOX_PARIS_RAW = "48.81234,2.21234,48.95678,2.45678"


# ---------------------------------------------------------------------------
# Validation de bbox
# ---------------------------------------------------------------------------


class TestParseBbox:
    def test_une_bbox_valide_est_arrondie_a_trois_decimales(self):
        assert poi.parse_bbox(BBOX_PARIS_RAW) == BBOX_PARIS

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("49,2.2,48.9,2.4", id="latitude-inversee"),
            pytest.param("48.8,3.0,48.9,2.4", id="longitude-inversee"),
            pytest.param("48.8,2.4,48.8,2.4", id="bbox-degenerate-egale"),
            pytest.param("91,2.2,92,2.4", id="hors-bornes-monde"),
            pytest.param("-95,2.2,-80,2.4", id="sous-borne-sud"),
            pytest.param("48.8,200,49,201", id="longitude-hors-monde"),
            pytest.param("nan,2.2,48.9,2.4", id="nan"),
            pytest.param("inf,2.2,48.9,2.4", id="infini"),
            pytest.param("48.8;2.2;48.9;2.4", id="separateur-faux"),
            pytest.param("48.8,2.2,48.9", id="trois-valeurs"),
            pytest.param("48.8,2.2,48.9,2.4,extra", id="cinq-valeurs"),
            pytest.param("48.8,deux,48.9,2.4", id="non-numerique"),
            pytest.param("", id="chaine-vide"),
        ],
    )
    def test_une_bbox_invalide_est_refusee(self, raw):
        assert poi.parse_bbox(raw) is None

    def test_bbox_absente(self):
        assert poi.parse_bbox(None) is None

    @pytest.mark.parametrize(
        ("raw", "acceptee"),
        [
            pytest.param("48.0,2.0,48.5,2.5", True, id="amplitude-exacte-limite"),
            pytest.param("48.0,2.0,48.51,2.5", False, id="amplitude-lat-depasee"),
            pytest.param("48.0,2.0,48.5,2.51", False, id="amplitude-lon-depassee"),
            pytest.param("48.0,2.0,50.5,2.6", False, id="planetaire"),
        ],
    )
    def test_l_amplitude_est_plafonnee_par_axe(self, raw, acceptee):
        """Une bbox planétaire ne part jamais chez Overpass : plafond ≤ 0,5°."""
        assert (poi.parse_bbox(raw) is not None) is acceptee


# ---------------------------------------------------------------------------
# Construction de la requête Overpass QL
# ---------------------------------------------------------------------------


class TestConstructionRequete:
    def test_l_entete_porte_timeout_et_bbox_arrondie(self):
        requete = poi._construit_requete(
            poi.COUCHES_POI["transports"]["requete_overpass"],
            poi.parse_bbox(BBOX_PARIS_RAW),
        )

        assert requete.startswith("[out:json][timeout:8][bbox:48.812,2.212,48.957,2.457];")

    def test_la_sortie_est_plafonnee(self):
        requete = poi._construit_requete(
            poi.COUCHES_POI["transports"]["requete_overpass"], BBOX_PARIS
        )

        assert requete.endswith(f"out body {poi._PLAFOND_POINTS};")

    def test_le_mvp_transports_interroge_les_quatre_familles(self):
        clauses = poi.COUCHES_POI["transports"]["requete_overpass"]

        assert 'node["highway"~"^(bus_stop|tram_stop)$"];' in clauses
        assert 'node["railway"~"^(station|halt|tram_stop)$"];' in clauses
        assert 'node["station"="subway"];' in clauses
        assert 'node["amenity"="ferry_terminal"];' in clauses

    def test_le_registre_expose_label_icone_et_requete(self):
        """Contrat du registre : ce que lit la route catalogue et le front."""
        couche = poi.COUCHES_POI["transports"]

        assert couche["label"] == "Transports"
        assert couche["icone"]
        assert couche["requete_overpass"]


# ---------------------------------------------------------------------------
# Typage et parsing des éléments Overpass
# ---------------------------------------------------------------------------


class TestTypage:
    @pytest.mark.parametrize(
        ("tags", "attendu"),
        [
            pytest.param({"highway": "bus_stop"}, "bus", id="arret-bus"),
            pytest.param({"highway": "tram_stop"}, "tram", id="tram-par-highway"),
            pytest.param({"railway": "tram_stop"}, "tram", id="tram-par-railway"),
            pytest.param({"railway": "station"}, "gare", id="gare"),
            pytest.param({"railway": "halt"}, "gare", id="halte"),
            pytest.param({"station": "subway"}, "metro", id="metro-seul"),
            pytest.param({"station": "subway", "railway": "station"}, "metro",
                         id="metro-prioritaire-sur-gare"),
            pytest.param({"amenity": "ferry_terminal"}, "ferry", id="ferry"),
            pytest.param({}, None, id="aucun-tag"),
            pytest.param({"highway": "footway"}, None, id="tag-hors-perimetre"),
            pytest.param({"railway": "platform"}, None, id="quai-hors-perimetre"),
        ],
    )
    def test_les_tags_osm_sont_regroupes_en_types_canoniques(self, tags, attendu):
        assert poi._type_point(tags) == attendu


class TestNormalisationElement:
    def test_un_node_complet_devient_un_point(self):
        point = poi._normalise_element(
            {"type": "node", "lat": 48.85, "lon": 2.35,
             "tags": {"highway": "bus_stop", "name": "République"}}
        )

        assert point == {"lat": 48.85, "lon": 2.35, "nom": "République", "type": "bus"}

    def test_un_nom_manquant_tombe_sur_le_libelle_du_type(self):
        point = poi._normalise_element(
            {"lat": 48.85, "lon": 2.35, "tags": {"railway": "station"}}
        )

        assert point["nom"] == "Gare"

    @pytest.mark.parametrize(
        "element",
        [
            pytest.param({"lon": 2.35, "tags": {"highway": "bus_stop"}}, id="sans-latitude"),
            pytest.param({"lat": 48.85, "tags": {"highway": "bus_stop"}}, id="sans-longitude"),
            pytest.param({"lat": "nord", "lon": 2.35, "tags": {"highway": "bus_stop"}},
                         id="coordonnees-non-numeriques"),
            pytest.param({"lat": float("nan"), "lon": 2.35, "tags": {"highway": "bus_stop"}},
                         id="latitude-nan"),
            pytest.param({"lat": 48.85, "lon": 2.35}, id="sans-tags"),
            pytest.param({"lat": 48.85, "lon": 2.35, "tags": {"highway": "footway"}},
                         id="hors-typage"),
            pytest.param("une-chaine", id="element-pas-un-dict"),
        ],
    )
    def test_un_element_incomplet_est_ignore_sans_lever(self, element):
        assert poi._normalise_element(element) is None


# ---------------------------------------------------------------------------
# Bout-en-bout : tri, plafond, cache, échecs
# ---------------------------------------------------------------------------


def _stub_overpass(requests_mock_fixture, elements):
    requests_mock_fixture.post(poi.OVERPASS_URL, json={"elements": elements})


class TestRecupererPoi:
    def test_la_reponse_est_normalisee_et_triee_par_pertinence(self, requests_mock):
        _stub_overpass(requests_mock, [
            {"lat": 1, "lon": 1, "tags": {"highway": "bus_stop", "name": "Bus A"}},
            {"lat": 2, "lon": 2, "tags": {"railway": "station", "name": "Gare B"}},
            {"lat": 3, "lon": 3, "tags": {"station": "subway"}},
            {"lat": 4, "lon": 4, "tags": {"highway": "tram_stop"}},
        ])

        points, degrade = poi.recuperer_poi("transports", BBOX_PARIS)

        assert degrade is False
        assert [point["type"] for point in points] == ["gare", "metro", "tram", "bus"]
        assert points[2] == {"lat": 4, "lon": 4, "nom": "Arrêt de tram", "type": "tram"}

    def test_le_plafond_garde_les_plus_pertinents_d_abord(self, requests_mock):
        elements = [{"lat": i * 0.001, "lon": i * 0.001, "tags": {"highway": "bus_stop"}}
                    for i in range(poi._PLAFOND_POINTS + 10)]
        elements += [{"lat": 99, "lon": 99, "tags": {"railway": "station", "name": "Grande Gare"}}]
        _stub_overpass(requests_mock, elements)

        points, degrade = poi.recuperer_poi("transports", BBOX_PARIS)

        assert degrade is False
        assert len(points) == poi._PLAFOND_POINTS
        # La gare, plus pertinente, survit au plafond ; les bus excédentaires non.
        assert points[0]["nom"] == "Grande Gare"

    def test_couche_inconnue_est_une_erreur_explicite(self):
        with pytest.raises(ValueError, match="inconnue"):
            poi.recuperer_poi("ecoles-fantomes", BBOX_PARIS)

    def test_hit_de_cache_une_seule_requete_http(self, requests_mock):
        _stub_overpass(requests_mock, [])

        poi.recuperer_poi("transports", BBOX_PARIS)
        poi.recuperer_poi("transports", BBOX_PARIS)

        assert requests_mock.call_count == 1

    def test_deux_bbox_differentes_font_deux_requetes(self, requests_mock):
        _stub_overpass(requests_mock, [])

        poi.recuperer_poi("transports", BBOX_PARIS)
        poi.recuperer_poi("transports", (44.84, -0.58, 44.90, -0.50))

        assert requests_mock.call_count == 2

    def test_ttl_expire_redemande_a_overpass(self, requests_mock):
        _stub_overpass(requests_mock, [])

        with freeze_time("2026-08-24 12:00:00"):
            poi.recuperer_poi("transports", BBOX_PARIS)
        with freeze_time("2026-08-24 12:05:00"):
            poi.recuperer_poi("transports", BBOX_PARIS)  # encore frais (< TTL)

        assert requests_mock.call_count == 1

        with freeze_time("2026-08-24 12:10:01"):
            poi.recuperer_poi("transports", BBOX_PARIS)  # TTL dépassé

        assert requests_mock.call_count == 2

    def test_eviction_fifo_a_la_taille_max(self, requests_mock):
        _stub_overpass(requests_mock, [])
        bboxes = [(i * 0.01, 0.0, i * 0.01 + 0.1, 0.1) for i in range(poi._TAILLE_CACHE_MAX)]
        for bbox in bboxes:
            poi.recuperer_poi("transports", bbox)
        assert requests_mock.call_count == poi._TAILLE_CACHE_MAX

        # Une bbox de plus : la plus ancienne (bboxes[0]) est évincée…
        poi.recuperer_poi("transports", (99.0, 0.0, 99.1, 0.1))
        assert requests_mock.call_count == poi._TAILLE_CACHE_MAX + 1

        # … donc la re-demander coûte une nouvelle requête.
        poi.recuperer_poi("transports", bboxes[0])
        assert requests_mock.call_count == poi._TAILLE_CACHE_MAX + 2

    def test_le_resultat_en_cache_n_est_jamais_degrade(self, requests_mock):
        _stub_overpass(requests_mock, [{"lat": 1, "lon": 1, "tags": {"railway": "halt"}}])
        poi.recuperer_poi("transports", BBOX_PARIS)

        points, degrade = poi.recuperer_poi("transports", BBOX_PARIS)

        assert degrade is False
        assert len(points) == 1


class TestEchecsOverpass:
    """Un échec upstream = liste vide + signal discret, JAMAIS d'exception."""

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(requests.exceptions.ConnectTimeout(), id="connexion-timeout"),
            pytest.param(requests.exceptions.ReadTimeout(), id="lecture-timeout"),
            pytest.param(requests.exceptions.ConnectionError(), id="reseau-coupe"),
        ],
    )
    def test_une_erreur_transport_revient_vide_et_degradee(self, requests_mock, exc):
        requests_mock.post(poi.OVERPASS_URL, exc=exc)

        points, degrade = poi.recuperer_poi("transports", BBOX_PARIS)

        assert points == []
        assert degrade is True

    def test_un_500_upstream_revient_vide_et_degrade(self, requests_mock):
        requests_mock.post(poi.OVERPASS_URL, status_code=500)

        points, degrade = poi.recuperer_poi("transports", BBOX_PARIS)

        assert points == []
        assert degrade is True

    def test_un_json_malforme_revient_vide_et_degrade(self, requests_mock):
        requests_mock.post(poi.OVERPASS_URL, text="<html>gateway error</html>")

        points, degrade = poi.recuperer_poi("transports", BBOX_PARIS)

        assert points == []
        assert degrade is True

    def test_une_reponse_sans_elements_est_degradee(self, requests_mock):
        requests_mock.post(poi.OVERPASS_URL, json={"remarque": "pas d'elements"})

        points, degrade = poi.recuperer_poi("transports", BBOX_PARIS)

        assert points == []
        assert degrade is True

    def test_un_echec_n_est_pas_mis_en_cache(self, requests_mock):
        """Un incident transitoire ne doit pas être servi pendant tout le TTL :
        chaque demande retente réellement Overpass."""
        requests_mock.post(poi.OVERPASS_URL, status_code=503)

        for _ in range(3):
            points, degrade = poi.recuperer_poi("transports", BBOX_PARIS)
            assert (points, degrade) == ([], True)
        assert requests_mock.call_count == 3

        # Le succès suivant repeuple le cache normalement.
        requests_mock.post(
            poi.OVERPASS_URL,
            json={"elements": [{"lat": 1, "lon": 1, "tags": {"highway": "bus_stop"}}]},
        )
        points, degrade = poi.recuperer_poi("transports", BBOX_PARIS)
        assert degrade is False and len(points) == 1

    def test_la_requete_postee_porte_la_bonne_url_et_le_qle(self, requests_mock):
        from urllib.parse import parse_qs

        _stub_overpass(requests_mock, [])

        poi.recuperer_poi("transports", BBOX_PARIS)

        requetes = [r for r in requests_mock.request_history if r.method == "POST"]
        assert len(requetes) == 1
        ql = parse_qs(requetes[0].text)["data"][0]
        assert ql.startswith("[out:json][timeout:8][bbox:48.812,2.212,48.957,2.457];")
