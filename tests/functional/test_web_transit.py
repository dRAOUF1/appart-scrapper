"""Tests fonctionnels du bloc Transports (#28) : routes web de bout en bout.

L'app est la vraie (`main.create_app`), Storage doublé : on vérifie que le
formulaire persiste le canonique, que l'édition ré-hydrate le champ caché,
que « transit-seul » est acceptée partout où une recherche l'est déjà, et que
l'autocomplete lignes/stations est réservé aux sessions connectées.
"""

from __future__ import annotations

import json

from tests.helpers.factories import make_search_row

PARIS_PAYLOAD = json.dumps({
    "kind": "city", "city": "Paris", "postalCode": "75013", "inseeCode": "75113",
})

M14 = {"mode": "metro", "line_id": "IDFM:C01388", "stop_ids": ["STIF:StopArea:SP:1:"], "radius_m": 1000}


def _creation_data(**extra) -> dict:
    data = {
        "label": "Près du métro 14",
        "ntfy_topic": "mon-topic",
        "sources": "seloger",
        "location_payload": PARIS_PAYLOAD,
    }
    data.update(extra)
    return data


# ---------------------------------------------------------------------------
# Création
# ---------------------------------------------------------------------------


class TestCreationAvecTransit:
    def test_a_mixed_search_persists_canonical_locations_and_transit(
        self, web_client, storage,
    ):
        resp = web_client.post("/searches", data=_creation_data(
            transit_payload=json.dumps([M14]),
        ))

        assert resp.status_code in (302, 303)
        criteria = storage.searches.create_search.call_args[0][4]
        assert set(criteria) >= {"locations", "transit"}
        assert criteria["transit"] == [M14]
        assert criteria["locations"][0]["postalCode"] == "75013"

    def test_a_transit_only_search_is_accepted(self, web_client, storage):
        """Issue #28 : pas une seule ville choisie, une ligne suffit."""
        resp = web_client.post("/searches", data={
            "label": "Transit seul",
            "ntfy_topic": "t",
            "sources": "seloger",
            "location_city": "",  # aucune localisation
            "transit_payload": json.dumps([M14]),
        })

        assert resp.status_code in (302, 303)
        storage.searches.create_search.assert_called_once()
        criteria = storage.searches.create_search.call_args[0][4]
        assert criteria["transit"] == [M14]
        assert "locations" not in criteria

    def test_a_corrupted_payload_is_flashed_never_crashed(self, web_client, storage):
        resp = web_client.post("/searches", data=_creation_data(
            transit_payload="{corrompu",
        ), follow_redirects=True)

        assert resp.status_code == 200
        assert b"corrompues" in resp.data
        storage.searches.create_search.assert_not_called()

    def test_a_loose_payload_still_creates_the_usable_part(self, web_client, storage):
        """Une entrée sans line_id est écartée par la normalisation, mais si
        rien d'exploitable ne reste ET qu'aucune ville n'est choisie, la
        création est refusée comme avant."""
        web_client.post("/searches", data={
            "label": "Boiteux", "ntfy_topic": "t", "sources": "seloger",
            "transit_payload": json.dumps([{"radius_m": 500}]),
        }, follow_redirects=True)

        # Ni locations ni transit valide : refus classique.
        storage.searches.create_search.assert_not_called()

    def test_a_valid_selection_without_line_id_is_normalized_away(self, web_client, storage):
        web_client.post("/searches", data=_creation_data(
            transit_payload=json.dumps([{"line_id": " ", "radius_m": 500}, M14]),
        ))

        criteria = storage.searches.create_search.call_args[0][4]
        assert criteria["transit"] == [M14]


# ---------------------------------------------------------------------------
# Édition — hydratation du champ caché depuis les critères stockés (#24)
# ---------------------------------------------------------------------------


class TestEditionHydratation:
    def test_the_edit_page_rehydrates_the_hidden_field_from_stored_criteria(
        self, web_client, owned_search,
    ):
        row = make_search_row(
            id=owned_search["id"],
            user_id=owned_search["user_id"],
            criteria={
                "locations": [{"kind": "city", "city": "Paris", "postalCode": "75013"}],
                "transaction": "rent",
                "transit": [M14],
            },
        )
        owned_search.update(row)
        owned_search.setdefault("ntfy_topic", "t")
        owned_search.setdefault("label", "L")
        owned_search.setdefault("scrape_interval", 5)
        owned_search.setdefault("notify_enabled", True)
        owned_search.setdefault("is_active", True)
        owned_search.setdefault("source", "seloger")

        resp = web_client.get(f"/searches/{owned_search['id']}/edit")

        assert resp.status_code == 200
        assert b'transit_payload' in resp.data
        assert b'IDFM:C01388' in resp.data
        assert b'STIF:StopArea:SP:1:' in resp.data

    def test_an_edited_search_keeps_its_transit_when_only_locations_change(
        self, web_client, storage, owned_search,
    ):
        critères = {
            "locations": [{"kind": "city", "city": "Paris", "postalCode": "75013"}],
            "transaction": "rent",
            "transit": [M14],
        }
        owned_search["criteria"] = critères
        owned_search.setdefault("ntfy_topic", "t")
        owned_search.setdefault("label", "L")
        owned_search.setdefault("source", "seloger")

        web_client.post(f"/searches/{owned_search['id']}/edit", data={
            "label": "Éditée",
            "ntfy_topic": "t2",
            "sources": "seloger",
            "location_payload": PARIS_PAYLOAD,
            "transit_payload": json.dumps([dict(M14, radius_m=2000)]),
            "notify_enabled_present": "1",
        })

        criteria = storage.searches.update_search.call_args.kwargs["criteria"]
        assert criteria["transit"][0]["radius_m"] == 2000
        assert criteria["locations"][0]["postalCode"] == "75013"


# ---------------------------------------------------------------------------
# Autocomplete lignes / stations — session-only
# ---------------------------------------------------------------------------


class TestAutocompleteTransit:
    def test_lines_are_served_with_labels_and_ids(self, web_client, storage):
        storage.transit.search_lines.return_value = [
            {"id": "IDFM:C01388", "mode": "metro", "code_ligne": "14", "nom_ligne": "Olympiades"},
            {"id": "IDFM:C01742", "mode": "train", "code_ligne": "D", "nom_ligne": "Creil <> Melun"},
        ]

        resp = web_client.get("/locations/transit/lines?q=14&mode=metro")

        assert resp.status_code == 200
        items = resp.json["items"]
        assert items == [
            {"id": "IDFM:C01388", "label": "Métro 14 · Olympiades"},
            {"id": "IDFM:C01742", "label": "Train D · Creil <> Melun"},
        ]
        storage.transit.search_lines.assert_called_once_with("14", mode="metro", limit=10)

    def test_lines_query_is_required(self, web_client, storage):
        resp = web_client.get("/locations/transit/lines?q=")

        assert resp.json == {"items": []}
        storage.transit.search_lines.assert_not_called()

    def test_an_unknown_mode_falls_back_to_no_filter(self, web_client, storage):
        storage.transit.search_lines.return_value = []

        web_client.get("/locations/transit/lines?q=x&mode=funiculaire")

        storage.transit.search_lines.assert_called_once_with("x", mode=None, limit=10)

    def test_a_line_label_without_name_degrades_cleanly(self, web_client, storage):
        storage.transit.search_lines.return_value = [
            {"id": "X", "mode": "tram", "code_ligne": "T9", "nom_ligne": ""},
        ]

        items = web_client.get("/locations/transit/lines?q=t9").json["items"]

        assert items == [{"id": "X", "label": "Tram T9"}]

    def test_stops_of_a_line_are_served_alphabetically_as_full_list(self, web_client, storage):
        stations = [
            {"id": f"S{i}", "nom": nom, "lat": 48.0, "lon": 2.0}
            for i, nom in enumerate(["Bercy", "Châtelet"])
        ]
        storage.transit.get_line_stops.return_value = stations

        resp = web_client.get("/locations/transit/stops?line=IDFM:C01388")

        assert resp.status_code == 200
        assert resp.json["items"] == [
            {"id": "S0", "label": "Bercy"},
            {"id": "S1", "label": "Châtelet"},
        ]
        storage.transit.get_line_stops.assert_called_once_with("IDFM:C01388")

    def test_stops_require_a_line(self, web_client, storage):
        assert web_client.get("/locations/transit/stops?line=").json == {"items": []}
        storage.transit.get_line_stops.assert_not_called()

    def test_autocomplete_requires_a_session(self, app, storage):
        """Pas de session -> redirection vers le login : ces endpoints lisent
        notre référentiel et restent derrière la session web (#30)."""
        anonymous = app.test_client()

        for url in ("/locations/transit/lines?q=métro", "/locations/transit/stops?line=L"):
            resp = anonymous.get(url)
            assert resp.status_code == 302
            assert "/login" in resp.headers["Location"]
