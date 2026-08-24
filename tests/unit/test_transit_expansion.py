"""Tests unitaires de `services/transit_expansion.py` (issue #28).

Le module transforme une sélection de transports en localisations classiques
AVANT les parsers — c'est ce qui garantit que « les parsers ne voient jamais
transit ». Tout est testé avec le storage doublé et l'API géo mockée :
aucun réseau, aucune base. Les cas géométriques (haversine/bbox) sont figés
sur des valeurs connues.
"""

from __future__ import annotations

import pytest

from services.transit_expansion import (
    bbox_autour,
    communes_cache_key,
    communes_dans_rayon,
    etendre_criteres,
    etendre_locations,
    haversine_m,
)
from tests.helpers.factories import make_city_location, make_transit_selection
from tests.helpers.fakes import fake_storage

# Une station de référence au centre de Paris, et des coordonnées de communes
# TOUJOURS proches d'elle : le filtre haversine est actif dans tous ces tests,
# une commune « inventée » trop loin est légitimement exclue.
STATION_PARIS = {"id": "S1", "nom": "Châtelet", "lat": 48.858, "lon": 2.347}


def station(id: str = "S1", nom: str = "Châtelet", lat: float = 48.858, lon: float = 2.347) -> dict:
    return {"id": id, "nom": nom, "lat": lat, "lon": lon}


def commune_proche(nom: str, insee: str, dlat_m: float = 0.0, dlon_m: float = 0.0,
                   cp: str = "75001") -> dict:
    """Une commune dont le centre est à ~dlat/dlon mètres de la station."""
    return {
        "nom": nom,
        "code": insee,
        "codesPostaux": [cp],
        "centre": {
            "type": "Point",
            "coordinates": [
                STATION_PARIS["lon"] + dlon_m / (111_320 * 0.66),
                STATION_PARIS["lat"] + dlat_m / 111_320,
            ],
        },
    }


def installe_stations(storage, lignes: dict[str, list[dict]]):
    """Programme le référentiel transit doublé : lignes -> stations."""
    storage.transit.get_line_stops.side_effect = lambda line_id: lignes.get(line_id, [])
    tous = {s["id"]: s for stations in lignes.values() for s in stations}
    storage.transit.get_stops.side_effect = lambda ids: [tous[i] for i in ids if i in tous]
    return storage


@pytest.fixture
def api_communes(monkeypatch):
    """Mocke l'interrogation geo.api.gouv.fr : programme les réponses bbox."""

    def install(reponses: list[list[dict]]):
        appels: list[tuple] = []
        reponses_iter = iter(reponses)

        def fausse_requete(bbox):
            appels.append(bbox)
            return next(reponses_iter, [])

        monkeypatch.setattr("services.transit_expansion._interroge_communes_bbox", fausse_requete)
        return appels

    return install


# ---------------------------------------------------------------------------
# Géométrie pure — cas connus
# ---------------------------------------------------------------------------


class TestHaversine:
    def test_a_same_point_is_zero(self):
        assert haversine_m(48.85, 2.35, 48.85, 2.35) == pytest.approx(0.0)

    @pytest.mark.parametrize(
        ("lat1", "lon1", "lat2", "lon2", "attendu_m"),
        [
            # Un degré de latitude ≈ 111,19 km (rayon terrestre moyen).
            (48.0, 2.0, 49.0, 2.0, 111_195),
            (45.764, 4.8357, 48.8566, 2.3522, 392_000),  # Lyon -> Paris
        ],
        ids=["un-degre-latitude", "lyon-paris"],
    )
    def test_known_distances(self, lat1, lon1, lat2, lon2, attendu_m):
        assert haversine_m(lat1, lon1, lat2, lon2) == pytest.approx(attendu_m, rel=0.01)


class TestBboxAutour:
    def test_the_bbox_contains_the_whole_circle(self):
        """Un point à EXACTEMENT rayon au nord/sud doit tomber dans la bbox :
        elle ne sert qu'à pré-filtrer chez l'API géo (sur-estimation voulue)."""
        lat, lon, rayon = 48.85, 2.35, 1000
        sud, ouest, nord, est = bbox_autour(lat, lon, rayon)

        delta_lat = rayon / 111_195  # un degré de latitude en mètres
        assert sud < lat - delta_lat * 0.999
        assert nord > lat + delta_lat * 0.999

    def test_longitudes_are_widened_by_cos_latitude(self):
        lat, lon, rayon = 60.0, 2.0, 100_000
        sud, ouest, nord, est = bbox_autour(lat, lon, rayon)
        # À 60° de latitude, un degré de longitude vaut ~55,6 km : il en faut
        # ~1,8° de chaque côté pour couvrir 100 km.
        assert abs(lon - ouest) == pytest.approx(1.8, abs=0.05)
        assert abs(est - lon) == pytest.approx(1.8, abs=0.05)


# ---------------------------------------------------------------------------
# Cache communes ∩ rayon
# ---------------------------------------------------------------------------


class TestCommunesDansRayon:
    def test_the_cache_key_follows_the_area_key_pattern(self):
        assert communes_cache_key("STOP:1", 2000) == "station:STOP:1:2000m"

    def test_a_cache_hit_is_served_without_any_network_call(self):
        storage = fake_storage()
        storage.transit.get_communes_cache.return_value = [
            {"nom": "Paris", "insee": "75056", "cp": "75001"}
        ]

        result = communes_dans_rayon(station(), 500, storage)

        assert result == [{"nom": "Paris", "insee": "75056", "cp": "75001"}]
        storage.transit.set_communes_cache.assert_not_called()

    def test_an_empty_cache_entry_is_a_legitimate_success(self):
        """[] stocké ≠ jamais calculé : aucune commune dans le rayon est un
        résultat valide, pas un cache miss."""
        storage = fake_storage()
        storage.transit.get_communes_cache.return_value = []

        assert communes_dans_rayon(station(id="S"), 500, storage) == []
        storage.transit.get_communes_cache.assert_called_once_with("station:S:500m")

    def test_a_cache_miss_computes_then_stores(self, api_communes):
        storage = fake_storage()
        appels = api_communes([[commune_proche("Paris", "75056")]])
        storage.transit.get_communes_cache.return_value = None

        result = communes_dans_rayon(station(), 1000, storage)

        assert result == [{"nom": "Paris", "insee": "75056", "cp": "75001"}]
        assert len(appels) == 1  # UNE requête bbox, pas une par commune
        storage.transit.set_communes_cache.assert_called_once_with(
            communes_cache_key("S1", 1000),
            [{"nom": "Paris", "insee": "75056", "cp": "75001"}],
        )

    def test_commune_centers_beyond_the_radius_are_excluded_even_if_in_bbox(
        self, api_communes,
    ):
        """La bbox SUR-estime : un centre à ~2,4 km du point sort au filtre
        haversine alors qu'il était dans la bbox de 2 km."""
        storage = fake_storage()
        storage.transit.get_communes_cache.return_value = None
        api_communes([[
            commune_proche("Proche", "93000", dlat_m=200),
            commune_proche("Lointaine", "77000", dlat_m=2400),
        ]])

        result = communes_dans_rayon(station(), 2000, storage)

        assert [c["nom"] for c in result] == ["Proche"]

    def test_incomplete_communes_are_skipped(self, api_communes):
        storage = fake_storage()
        storage.transit.get_communes_cache.return_value = None
        api_communes([[
            {"nom": "SansCode", "codesPostaux": ["75001"], "centre": {"coordinates": [2.3, 48.8]}},
            {"nom": "SansCentre", "code": "75057", "codesPostaux": ["75001"]},
            {"nom": "SansCP", "code": "75058"},
        ]])

        assert communes_dans_rayon(station(), 1000, storage) == []

    def test_a_failure_is_never_cached(self, monkeypatch):
        """Succès-seuls : un calcul raté ne doit rien écrire, pour retenter au
        scrape suivant plutôt que geler un résultat vide."""
        storage = fake_storage()
        storage.transit.get_communes_cache.return_value = None

        def boom(bbox):
            raise RuntimeError("API géo down")

        monkeypatch.setattr("services.transit_expansion._interroge_communes_bbox", boom)

        with pytest.raises(RuntimeError, match="API géo down"):
            communes_dans_rayon(station(), 500, storage)
        storage.transit.set_communes_cache.assert_not_called()


# ---------------------------------------------------------------------------
# Expansion : union, priorité, plafond, avertissements
# ---------------------------------------------------------------------------


class TestEtendreLocations:
    def test_without_transit_nothing_happens_and_storage_is_untouched(self):
        storage = fake_storage()
        locations, avertissements = etendre_locations({"transaction": "rent"}, storage)

        assert locations == []
        assert avertissements == []
        storage.transit.get_line_stops.assert_not_called()
        storage.transit.get_stops.assert_not_called()

    def test_pinned_stations_use_get_stops_and_generate_city_locations(self, api_communes):
        storage = installe_stations(fake_storage(), {"L14": [STATION_PARIS]})
        api_communes([[commune_proche("Paris", "75056")]])
        criteria = {
            "locations": [],
            "transit": [make_transit_selection(line_id="L14", stop_ids=["S1"], radius_m=500)],
        }

        locations, _ = etendre_locations(criteria, storage)

        storage.transit.get_stops.assert_called_once_with(["S1"])
        storage.transit.get_line_stops.assert_not_called()
        assert locations == [
            {"kind": "city", "city": "Paris", "postalCode": "75001", "inseeCode": "75056"}
        ]

    def test_no_pinned_station_means_the_whole_line(self, api_communes):
        storage = installe_stations(fake_storage(), {
            "L14": [station(id="S1"), station(id="S2", nom="Gare de Lyon")],
        })
        api_communes([
            [commune_proche("Ivry", "94041")],
            [commune_proche("Bercy", "75112")],
        ])

        locations, avertissements = etendre_locations(
            {"transit": [make_transit_selection(line_id="L14", stop_ids=[])]}, storage,
        )

        storage.transit.get_line_stops.assert_called_once_with("L14")
        assert [loc["city"] for loc in locations] == ["Bercy", "Ivry"]  # ordre alpha
        assert avertissements == []

    def test_unknown_pinned_stations_are_warned_and_ignored(self):
        storage = installe_stations(fake_storage(), {"L14": [STATION_PARIS]})

        locations, avertissements = etendre_locations(
            {"transit": [make_transit_selection(line_id="L14", stop_ids=["S1", "GHOST"])]},
            storage,
        )

        assert locations == []
        assert any("GHOST" in message for message in avertissements)

    def test_a_line_without_any_known_station_is_skipped_with_a_warning(self):
        storage = installe_stations(fake_storage(), {})  # référentiel vide

        locations, avertissements = etendre_locations(
            {"transit": [make_transit_selection(line_id="INCONNU", stop_ids=[])]},
            storage,
        )

        assert locations == []
        assert any("INCONNU" in message for message in avertissements)

    def test_user_and_transit_union_is_deduplicated_by_kind_and_insee(self, api_communes):
        """« Dans les villes choisies OU près des stations » : une commune déjà
        choisie à la main ne doit PAS apparaître deux fois — la version de
        l'utilisateur gagne (première), avec SON code postal."""
        storage = installe_stations(fake_storage(), {"L14": [station(
            id="S_MONTROUGE", nom="Mairie de Montrouge", lat=48.8205, lon=2.3204,
        )]})
        api_communes([[{
            "nom": "Paris",
            "code": "75113",
            "codesPostaux": ["75014"],
            "centre": {"type": "Point", "coordinates": [2.3204, 48.8205]},
        }]])
        criteria = {
            "locations": [{"kind": "city", "city": "Paris", "postalCode": "75013", "inseeCode": "75113"}],
            "transit": [make_transit_selection(line_id="L14", stop_ids=["S_MONTROUGE"])],
        }

        locations, _ = etendre_locations(criteria, storage)

        paris = [loc for loc in locations if loc.get("inseeCode") == "75113"]
        assert len(paris) == 1
        assert paris[0]["postalCode"] == "75013"  # la ville choisie par l'utilisateur

    def test_two_stations_touching_the_same_commune_produce_one_location(self, api_communes):
        storage = installe_stations(fake_storage(), {"L14": [STATION_PARIS, station(id="S2")]})
        api_communes([
            [commune_proche("Paris", "75056")],
            [commune_proche("Paris", "75056")],  # même commune des deux côtés
        ])

        locations, _ = etendre_locations(
            {"transit": [make_transit_selection(line_id="L14", stop_ids=["S1", "S2"])]},
            storage,
        )

        assert len(locations) == 1

    def test_the_cap_truncates_line_generated_communes_last_with_a_warning(
        self, api_communes, monkeypatch,
    ):
        import services.transit_expansion as module

        monkeypatch.setattr(module, "PLAFOND_LOCATIONS", 5)
        storage = installe_stations(fake_storage(), {"L14": [STATION_PARIS]})
        api_communes([[
            commune_proche(f"Commune{i}", f"INS{i:02d}", dlat_m=i * 30.0)
            for i in range(10)
        ]])
        criteria = {
            "locations": [make_city_location()],
            "transit": [make_transit_selection(line_id="L14", stop_ids=["S1"])],
        }

        locations, avertissements = etendre_locations(criteria, storage)

        assert len(locations) == 5
        # La ville choisie reste en tête, jamais rognée.
        assert locations[0] == make_city_location()
        assert any("Plafond" in message and "5" in message for message in avertissements)

    def test_when_user_locations_alone_fill_the_cap_everything_transit_is_cut(
        self, api_communes, monkeypatch,
    ):
        import services.transit_expansion as module

        monkeypatch.setattr(module, "PLAFOND_LOCATIONS", 2)
        storage = installe_stations(fake_storage(), {"L14": [STATION_PARIS]})
        api_communes([[commune_proche("X", "X1")]])
        criteria = {
            "locations": [
                make_city_location(city="A", postal_code="75013", insee="75113"),
                make_city_location(city="B", postal_code="75014", insee="75114"),
            ],
            "transit": [make_transit_selection(line_id="L14", stop_ids=["S1"])],
        }

        locations, avertissements = etendre_locations(criteria, storage)

        assert [loc["city"] for loc in locations] == ["A", "B"]
        assert any("Plafond" in message for message in avertissements)

    def test_an_api_failure_on_one_station_does_not_block_the_others(
        self, monkeypatch,
    ):
        storage = installe_stations(fake_storage(), {"L14": [station(id="S1"), station(id="S2")]})

        appels = {"n": 0}

        def fausse_requete(bbox):
            appels["n"] += 1
            if appels["n"] == 1:
                raise RuntimeError("panne")
            return [commune_proche("Ville", "V1")]

        monkeypatch.setattr("services.transit_expansion._interroge_communes_bbox", fausse_requete)

        locations, avertissements = etendre_locations(
            {"transit": [make_transit_selection(line_id="L14", stop_ids=["S1", "S2"])]},
            storage,
        )

        assert [loc["city"] for loc in locations] == ["Ville"]
        assert any("indisponibles" in message for message in avertissements)


class TestEtendreCriteres:
    def test_the_transit_key_never_reaches_the_parsers(self, api_communes):
        storage = installe_stations(fake_storage(), {"L14": [STATION_PARIS]})
        api_communes([[commune_proche("Paris", "75056")]])
        criteria = {
            "transaction": "rent",
            "transit": [make_transit_selection(line_id="L14", stop_ids=["S1"])],
        }

        etendus, _ = etendre_criteres(criteria, storage)

        assert "transit" not in etendus
        assert etendus["transaction"] == "rent"
        assert any(loc["kind"] == "city" for loc in etendus["locations"])

    def test_a_search_without_transit_is_returned_as_is(self):
        storage = fake_storage()
        criteria = {"transaction": "rent"}

        etendus, avertissements = etendre_criteres(criteria, storage)

        assert etendus is criteria
        assert avertissements == []

    def test_a_transit_only_search_ends_up_with_locations_only(self, api_communes):
        storage = installe_stations(fake_storage(), {"L14": [STATION_PARIS]})
        api_communes([[commune_proche("Paris", "75056")]])

        etendus, _ = etendre_criteres(
            {"transaction": "rent", "transit": [make_transit_selection(line_id="L14", stop_ids=["S1"])]},
            storage,
        )

        assert set(etendus) == {"transaction", "locations"}
        assert etendus["locations"][0]["city"] == "Paris"
