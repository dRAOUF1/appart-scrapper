"""`routes/api.py` — cycle de vie d'une recherche via l'API JSON.

Le reste de l'API (utilisateurs, sources, annonces, stats, logs) est dans
test_api_misc.py ; l'autorisation croisée entre utilisateurs est dans
test_authorization.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.helpers.factories import make_criteria, make_search_row


class StubParser:
    """Parser doublé, injecté à la place de `parsers.get_parser`.

    Écrit à la main plutôt qu'en `MagicMock` : `/searches/<id>/urls` distingue
    trois issues (source inconnue, reconstruction impossible, aucune URL) et un
    mock complaisant les rendrait indistinguables.
    """

    def __init__(self, source_name="Source Test", urls=None, raises=None, url_note=""):
        self.SOURCE_NAME = source_name
        self.URL_NOTE = url_note
        self._urls = urls if urls is not None else ["https://example.test/recherche"]
        self._raises = raises
        self.build_calls: list[dict] = []

    def build_search_urls(self, criteria):
        self.build_calls.append(criteria)
        if self._raises is not None:
            raise self._raises
        return self._urls


@pytest.fixture
def parsers_by_source(monkeypatch):
    """Remplace la résolution de parser par un dictionnaire piloté par le test.

    Renvoie ce dictionnaire : `parsers_by_source["seloger"] = StubParser(...)`.
    Une source absente lève `ValueError`, comme le vrai registre.
    """
    import parsers

    registry: dict[str, StubParser] = {}

    def fake_get_parser(source, storage=None):
        if source not in registry:
            raise ValueError(f"Source '{source}' inconnue. Sources disponibles : {', '.join(sorted(registry))}")
        registry[source].storage = storage
        return registry[source]

    monkeypatch.setattr(parsers, "get_parser", fake_get_parser)
    return registry


class TestCreateSearch:
    def test_creates_a_search_and_returns_201(self, api_client, storage, user, monkeypatch):
        created = make_search_row(id=7)
        storage.searches.create_search.return_value = created
        remembered = MagicMock()
        monkeypatch.setattr("routes.api.remember_manual_overrides", remembered)

        resp = api_client.post("/api/searches", json={
            "label": "  Paris 13e  ",
            "ntfy_topic": "  topic-1  ",
            "sources": ["seloger", "laforet"],
            "criteria": make_criteria(),
            "scrape_interval": 15,
        })

        assert resp.status_code == 201
        assert resp.get_json()["id"] == 7
        args, kwargs = storage.searches.create_search.call_args
        # label et topic sont strippés ; `sources[0]` devient la source
        # historique (colonne `source`, encore lue par l'admin et le scheduler).
        assert args[:4] == (user["id"], "Paris 13e", "topic-1", "seloger")
        assert args[5] == 15
        assert kwargs["sources"] == ["seloger", "laforet"]
        # `remember_manual_overrides` est appelé APRÈS création, avec les
        # critères normalisés — c'est ce qui capitalise une saisie manuelle.
        remembered.assert_called_once()
        assert remembered.call_args[0][0] == ["seloger", "laforet"]
        assert remembered.call_args[0][1] == args[4]

    def test_a_single_source_string_is_wrapped_into_a_list(self, api_client, storage):
        storage.searches.create_search.return_value = make_search_row()

        resp = api_client.post("/api/searches", json={
            "label": "L", "ntfy_topic": "T", "source": "laforet", "criteria": make_criteria(),
        })

        assert resp.status_code == 201
        assert storage.searches.create_search.call_args[1]["sources"] == ["laforet"]

    def test_default_source_is_seloger(self, api_client, storage):
        storage.searches.create_search.return_value = make_search_row()

        api_client.post("/api/searches", json={"label": "L", "ntfy_topic": "T", "criteria": make_criteria()})

        assert storage.searches.create_search.call_args[1]["sources"] == ["seloger"]

    def test_default_scrape_interval_is_five_minutes(self, api_client, storage):
        storage.searches.create_search.return_value = make_search_row()

        api_client.post("/api/searches", json={"label": "L", "ntfy_topic": "T", "criteria": make_criteria()})

        assert storage.searches.create_search.call_args[0][5] == 5

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"label": "L"},
            {"ntfy_topic": "T"},
            {"label": "   ", "ntfy_topic": "T"},
            {"label": "L", "ntfy_topic": "   "},
        ],
        ids=["vide", "sans-topic", "sans-label", "label-blanc", "topic-blanc"],
    )
    def test_label_and_topic_are_mandatory(self, api_client, storage, payload):
        resp = api_client.post("/api/searches", json=payload)

        assert resp.status_code == 400
        assert resp.get_json()["error"] == "label et ntfy_topic requis"
        storage.searches.create_search.assert_not_called()

    def test_the_no_source_guard_is_unreachable_dead_code(self, api_client, storage):
        """BUG (mineur) : « au moins une source requise » n'est jamais renvoyé.

        `sources = data.get("sources") or [source]` remplace une liste vide par
        `[source]`, et `source` vaut `""` au pire — soit `[""]`, qui est truthy.
        Le `if not sources` qui suit est donc du code mort : le message
        effectivement rendu est l'erreur de source inconnue, moins parlante pour
        un client qui a envoyé `sources: []`.
        """
        resp = api_client.post("/api/searches", json={
            "label": "L", "ntfy_topic": "T", "source": "", "sources": [],
        })

        assert resp.status_code == 400
        assert resp.get_json()["error"] != "au moins une source requise"
        assert "Source '' inconnue" in resp.get_json()["error"]
        storage.searches.create_search.assert_not_called()

    @pytest.mark.parametrize("sources", [["inconnue"], ["seloger", "inconnue"], ["", "seloger"]])
    def test_every_source_is_validated_not_just_the_first(self, api_client, storage, sources):
        """La boucle valide CHAQUE source : une seule inconnue rejette tout.

        Sans quoi une recherche serait créée avec une source que le scheduler ne
        pourra jamais exécuter, sans que l'utilisateur en soit averti.
        """
        resp = api_client.post("/api/searches", json={
            "label": "L", "ntfy_topic": "T", "sources": sources, "criteria": make_criteria(),
        })

        assert resp.status_code == 400
        assert "inconnue" in resp.get_json()["error"]
        storage.searches.create_search.assert_not_called()

    @pytest.mark.parametrize(
        ("payload", "expected_fragment"),
        [
            ({"criteria": {"priceMax": "pas-un-nombre"}}, "Critères invalides"),
            ({"criteria": "une chaîne"}, "criteria doit être un objet JSON"),
            ({"criteria": {"locations": "pas-une-liste"}}, "Critères invalides"),
            ({"scrape_interval": "abc"}, "scrape_interval doit être un entier"),
            ({"scrape_interval": 0}, "entre 1 et 1440"),
            ({"scrape_interval": 1441}, "entre 1 et 1440"),
        ],
        ids=["prix", "criteria-str", "locations-str", "interval-str", "interval-0", "interval-trop-grand"],
    )
    def test_invalid_criteria_or_interval_yield_400(self, api_client, storage, payload, expected_fragment):
        resp = api_client.post("/api/searches", json={"label": "L", "ntfy_topic": "T", **payload})

        assert resp.status_code == 400
        assert expected_fragment in resp.get_json()["error"]
        storage.searches.create_search.assert_not_called()

    def test_a_non_json_body_is_treated_as_an_empty_payload(self, api_client, storage):
        """`get_json(silent=True)` avale l'erreur de parsing : 400 métier, pas 500."""
        resp = api_client.post("/api/searches", data="pas du json", content_type="application/json")

        assert resp.status_code == 400
        storage.searches.create_search.assert_not_called()

    def test_criteria_are_normalized_before_storage(self, api_client, storage):
        """Ce qui est stocké est du canonique pur, quel que soit le vocabulaire entrant."""
        storage.searches.create_search.return_value = make_search_row()

        api_client.post("/api/searches", json={
            "label": "L", "ntfy_topic": "T",
            "criteria": {"city": "Poitiers", "postalCode": "86000", "spaceMin": 30},
        })

        stored = storage.searches.create_search.call_args[0][4]
        assert stored["locations"] == [{"kind": "city", "city": "Poitiers", "postalCode": "86000"}]
        assert stored["surfaceMin"] == 30
        assert "city" not in stored and "spaceMin" not in stored


class TestListSearches:
    def test_returns_only_the_callers_searches(self, api_client, storage, user):
        storage.searches.get_user_searches.return_value = [make_search_row(id=1), make_search_row(id=2)]

        resp = api_client.get("/api/searches")

        assert resp.status_code == 200
        assert [s["id"] for s in resp.get_json()] == [1, 2]
        # L'identité vient de `g.user`, jamais d'un paramètre de requête.
        storage.searches.get_user_searches.assert_called_once_with(user["id"])

    def test_no_search_yields_an_empty_list_not_a_404(self, api_client, storage):
        storage.searches.get_user_searches.return_value = []

        resp = api_client.get("/api/searches")

        assert resp.status_code == 200
        assert resp.get_json() == []


class TestSearchUrls:
    """`GET /searches/<id>/urls` — la route qui a rendu un 500 en production.

    Deux `try` distincts : le premier isole une source inconnue, le second un
    échec de reconstruction (qui peut demander un appel réseau). Aucune des deux
    situations ne doit priver la page de ses autres sources.
    """

    def test_returns_one_entry_per_source(self, api_client, storage, owned_search, parsers_by_source):
        storage.searches.get_search.side_effect = None
        storage.searches.get_search.return_value = make_search_row(
            id=1, user_id=1, sources=["seloger", "laforet"],
        )
        parsers_by_source["seloger"] = StubParser("SeLoger", urls=["https://sl.test/a"])
        parsers_by_source["laforet"] = StubParser("Laforêt", urls=["https://lf.test/b", "https://lf.test/c"])

        resp = api_client.get("/api/searches/1/urls")

        assert resp.status_code == 200
        body = resp.get_json()
        assert [s["source"] for s in body["sources"]] == ["seloger", "laforet"]
        assert body["sources"][0]["url"] == "https://sl.test/a"
        assert body["sources"][0]["error"] is None
        # `urls` porte TOUTES les URL (une source multi-périmètres en produit
        # plusieurs), `url` seulement la première.
        assert body["sources"][1]["urls"] == ["https://lf.test/b", "https://lf.test/c"]
        assert body["sources"][1]["url"] == "https://lf.test/b"

    def test_top_level_fields_mirror_the_first_source(self, api_client, storage, parsers_by_source):
        """Champs rétro-compatibles : d'anciens clients ne lisent que `url`."""
        storage.searches.get_search.return_value = make_search_row(id=1, user_id=1, sources=["laforet", "seloger"])
        parsers_by_source["laforet"] = StubParser("Laforêt", urls=["https://lf.test/b"])
        parsers_by_source["seloger"] = StubParser("SeLoger", urls=["https://sl.test/a"])

        body = api_client.get("/api/searches/1/urls").get_json()

        assert body["source"] == "laforet"
        assert body["url"] == "https://lf.test/b"
        assert body["source_name"] == "Laforêt"

    def test_an_unknown_source_is_reported_per_source_not_as_a_500(
        self, api_client, storage, parsers_by_source
    ):
        """Une source retirée du code laisse des recherches qui la citent."""
        storage.searches.get_search.return_value = make_search_row(id=1, user_id=1, sources=["disparue", "seloger"])
        parsers_by_source["seloger"] = StubParser("SeLoger", urls=["https://sl.test/a"])

        resp = api_client.get("/api/searches/1/urls")

        assert resp.status_code == 200
        entries = {s["source"]: s for s in resp.get_json()["sources"]}
        assert entries["disparue"]["url"] is None
        assert "inconnue" in entries["disparue"]["error"]
        # L'autre source garde son lien : c'est tout l'intérêt du try par source.
        assert entries["seloger"]["url"] == "https://sl.test/a"

    def test_an_unknown_first_source_leaves_top_level_fields_incomplete(
        self, api_client, storage, parsers_by_source
    ):
        """L'entrée d'erreur n'a ni `source_name` ni `urls` : `.get()` renvoie None.

        Un client qui n'affiche que les champs top-level voit donc `url: null`
        alors qu'une autre source, elle, a bien une URL.
        """
        storage.searches.get_search.return_value = make_search_row(id=1, user_id=1, sources=["disparue", "seloger"])
        parsers_by_source["seloger"] = StubParser("SeLoger", urls=["https://sl.test/a"])

        body = api_client.get("/api/searches/1/urls").get_json()

        assert body["url"] is None
        assert body["source_name"] is None

    def test_a_reconstruction_failure_is_reported_per_source(self, api_client, storage, parsers_by_source):
        """Reconstruire une URL peut demander un appel réseau, qui peut tomber."""
        storage.searches.get_search.return_value = make_search_row(id=1, user_id=1, sources=["seloger", "laforet"])
        parsers_by_source["seloger"] = StubParser("SeLoger", raises=RuntimeError("résolution du lieu HS"))
        parsers_by_source["laforet"] = StubParser("Laforêt", urls=["https://lf.test/b"])

        resp = api_client.get("/api/searches/1/urls")

        assert resp.status_code == 200
        entries = {s["source"]: s for s in resp.get_json()["sources"]}
        assert entries["seloger"]["url"] is None
        assert entries["seloger"]["urls"] == []
        assert "résolution du lieu HS" in entries["seloger"]["error"]
        assert entries["laforet"]["url"] == "https://lf.test/b"

    def test_no_url_available_is_an_explicit_error_not_a_silent_null(
        self, api_client, storage, parsers_by_source
    ):
        storage.searches.get_search.return_value = make_search_row(id=1, user_id=1, sources=["seloger"])
        parsers_by_source["seloger"] = StubParser("SeLoger", urls=[])

        entry = api_client.get("/api/searches/1/urls").get_json()["sources"][0]

        assert entry["url"] is None
        assert entry["error"] == "URL reconstruction non disponible pour cette source"

    def test_url_note_is_forwarded_and_empty_becomes_null(self, api_client, storage, parsers_by_source):
        """`URL_NOTE` explique qu'une URL omet volontairement des filtres."""
        storage.searches.get_search.return_value = make_search_row(id=1, user_id=1, sources=["laforet", "seloger"])
        parsers_by_source["laforet"] = StubParser(
            "Laforêt", urls=["https://lf.test/b"], url_note="filtres appliqués côté scraper"
        )
        parsers_by_source["seloger"] = StubParser("SeLoger", urls=["https://sl.test/a"], url_note="")

        entries = {s["source"]: s for s in api_client.get("/api/searches/1/urls").get_json()["sources"]}

        assert entries["laforet"]["note"] == "filtres appliqués côté scraper"
        assert entries["seloger"]["note"] is None

    def test_falls_back_to_the_legacy_source_column(self, api_client, storage, parsers_by_source):
        """Les anciennes lignes n'ont pas de `sources` : la colonne `source` sert."""
        storage.searches.get_search.return_value = make_search_row(id=1, user_id=1, sources=None, source="laforet")
        parsers_by_source["laforet"] = StubParser("Laforêt", urls=["https://lf.test/b"])

        body = api_client.get("/api/searches/1/urls").get_json()

        assert [s["source"] for s in body["sources"]] == ["laforet"]

    def test_a_search_with_no_source_at_all_reports_one_unknown_entry(
        self, api_client, storage, parsers_by_source
    ):
        """`sources: []` retombe sur `[source]`, donc sur `[""]` : jamais une liste vide.

        Le corps garde donc toujours au moins une entrée — la branche
        `results[0] if results else {}` de la route est inatteignable par ce
        chemin. Mieux vaut une entrée en erreur qu'un 500, mais l'utilisateur lit
        « Source '' inconnue » plutôt que « cette recherche n'a pas de source ».
        """
        storage.searches.get_search.return_value = make_search_row(id=1, user_id=1, sources=[], source="")

        body = api_client.get("/api/searches/1/urls").get_json()

        assert body["source"] == ""
        assert body["url"] is None
        assert len(body["sources"]) == 1
        assert "inconnue" in body["sources"][0]["error"]

    def test_the_parser_receives_the_storage_and_the_stored_criteria(
        self, api_client, storage, parsers_by_source
    ):
        """Le storage est injecté : SeLoger a besoin de son cache de placeIds."""
        criteria = make_criteria(priceMax=900)
        storage.searches.get_search.return_value = make_search_row(id=1, user_id=1, criteria=criteria)
        stub = StubParser("SeLoger")
        parsers_by_source["seloger"] = stub

        api_client.get("/api/searches/1/urls")

        assert stub.build_calls == [criteria]
        assert stub.storage is storage


class TestDeleteSearch:
    def test_deletes_an_owned_search(self, api_client, storage, owned_search):
        resp = api_client.delete("/api/searches/1")

        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}
        storage.searches.delete_search.assert_called_once_with(1)

    def test_a_missing_search_yields_404_without_deleting(self, api_client, storage):
        storage.searches.get_search.return_value = None

        resp = api_client.delete("/api/searches/999")

        assert resp.status_code == 404
        assert resp.get_json() == {"error": "Recherche introuvable"}
        storage.searches.delete_search.assert_not_called()


class TestUpdateCriteria:
    def test_updates_criteria_only(self, api_client, storage, owned_search):
        resp = api_client.put("/api/searches/1/criteria", json={"criteria": make_criteria(priceMax=800)})

        assert resp.status_code == 200
        storage.searches.update_search_criteria.assert_called_once()
        assert storage.searches.update_search_criteria.call_args[0][1]["priceMax"] == 800
        storage.searches.update_scrape_interval.assert_not_called()

    def test_updates_interval_only(self, api_client, storage, owned_search):
        resp = api_client.put("/api/searches/1/criteria", json={"scrape_interval": 42})

        assert resp.status_code == 200
        storage.searches.update_scrape_interval.assert_called_once_with(1, 42)
        storage.searches.update_search_criteria.assert_not_called()

    def test_an_empty_payload_updates_nothing_but_answers_ok(self, api_client, storage, owned_search):
        resp = api_client.put("/api/searches/1/criteria", json={})

        assert resp.status_code == 200
        storage.searches.update_search_criteria.assert_not_called()
        storage.searches.update_scrape_interval.assert_not_called()

    def test_explicit_null_criteria_is_skipped(self, api_client, storage, owned_search):
        """`criteria: null` n'est pas « vider les critères » : c'est « ne pas y toucher »."""
        resp = api_client.put("/api/searches/1/criteria", json={"criteria": None})

        assert resp.status_code == 200
        storage.searches.update_search_criteria.assert_not_called()

    def test_the_two_updates_are_not_transactional(self, api_client, storage, owned_search):
        """BUG : mise à jour partielle possible.

        Les critères sont écrits AVANT que l'intervalle soit validé. Un intervalle
        refusé renvoie donc 400 alors que les critères, eux, sont déjà en base :
        le client croit son appel sans effet, et la recherche part scraper de
        nouveaux critères à l'ancien rythme.
        """
        resp = api_client.put("/api/searches/1/criteria", json={
            "criteria": make_criteria(priceMax=800),
            "scrape_interval": 99999,
        })

        assert resp.status_code == 400
        assert "entre 1 et 1440" in resp.get_json()["error"]
        # Effet de bord conservé malgré le 400 :
        storage.searches.update_search_criteria.assert_called_once()
        storage.searches.update_scrape_interval.assert_not_called()

    def test_invalid_criteria_leave_everything_untouched(self, api_client, storage, owned_search):
        resp = api_client.put("/api/searches/1/criteria", json={
            "criteria": {"priceMax": "gratuit"}, "scrape_interval": 10,
        })

        assert resp.status_code == 400
        storage.searches.update_search_criteria.assert_not_called()
        storage.searches.update_scrape_interval.assert_not_called()


class TestToggleActive:
    @pytest.mark.parametrize("new_value", [True, False])
    def test_returns_the_new_state(self, api_client, storage, owned_search, new_value):
        storage.searches.toggle_search_active.return_value = new_value

        resp = api_client.post("/api/searches/1/toggle-active")

        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True, "is_active": new_value}
        storage.searches.toggle_search_active.assert_called_once_with(1)

    def test_a_search_deleted_between_the_two_queries_yields_404(self, api_client, storage, owned_search):
        """Course : `get_search` a réussi, `toggle` ne trouve plus la ligne."""
        storage.searches.toggle_search_active.return_value = None

        resp = api_client.post("/api/searches/1/toggle-active")

        assert resp.status_code == 404
        assert resp.get_json() == {"error": "Recherche introuvable"}


class TestBlacklistMode:
    @pytest.mark.parametrize("mode", ["exclude", "no_notify"])
    def test_accepts_the_two_documented_modes(self, api_client, storage, owned_search, mode):
        resp = api_client.put("/api/searches/1/blacklist-mode", json={"mode": mode})

        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True, "blacklist_mode": mode}
        storage.searches.update_blacklist_mode.assert_called_once_with(1, mode)

    @pytest.mark.parametrize("mode", ["EXCLUDE", "ignore", "", None, 42, "exclude "])
    def test_rejects_anything_else(self, api_client, storage, owned_search, mode):
        """La liste blanche évite d'écrire en base un mode que le pipeline
        interpréterait comme « ne rien exclure »."""
        resp = api_client.put("/api/searches/1/blacklist-mode", json={"mode": mode})

        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Mode invalide. Options: exclude, no_notify"
        storage.searches.update_blacklist_mode.assert_not_called()

    def test_an_absent_mode_defaults_to_exclude(self, api_client, storage, owned_search):
        resp = api_client.put("/api/searches/1/blacklist-mode", json={})

        assert resp.status_code == 200
        storage.searches.update_blacklist_mode.assert_called_once_with(1, "exclude")


class TestBlacklistAgencies:
    def test_stores_the_submitted_list(self, api_client, storage, owned_search):
        resp = api_client.put("/api/searches/1/blacklist-agencies", json={"agencies": ["Foncia", "Nexity"]})

        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True, "blacklisted_agencies": ["Foncia", "Nexity"]}
        storage.searches.update_blacklisted_agencies.assert_called_once_with(1, ["Foncia", "Nexity"])

    def test_an_absent_list_clears_the_blacklist(self, api_client, storage, owned_search):
        resp = api_client.put("/api/searches/1/blacklist-agencies", json={})

        assert resp.status_code == 200
        storage.searches.update_blacklisted_agencies.assert_called_once_with(1, [])

    @pytest.mark.parametrize("agencies", ["Foncia", {"a": 1}, 42, [None, 3]])
    def test_the_payload_type_is_never_validated(self, api_client, storage, owned_search, agencies):
        """BUG : `agencies` est passé tel quel au repository.

        Une chaîne, un dict ou une liste de non-chaînes finissent en base sans
        contrôle. Le filtre SQL les compare ensuite à `l.agency` : selon le
        type, l'exclusion devient silencieusement inopérante (une chaîne est
        itérée caractère par caractère par psycopg2 côté `ANY`).
        """
        resp = api_client.put("/api/searches/1/blacklist-agencies", json={"agencies": agencies})

        assert resp.status_code == 200
        storage.searches.update_blacklisted_agencies.assert_called_once_with(1, agencies)


class TestScrape:
    def test_submits_a_job_and_answers_202(self, api_client, app, storage, owned_search, user):
        resp = api_client.post("/api/scrape/1")

        assert resp.status_code == 202
        assert resp.get_json() == {"message": "Scraping démarré en arrière-plan"}
        app._scrape_executor.submit.assert_called_once()
        # Le job est enregistré : c'est ce qui rend le second appel idempotent.
        assert 1 in app._scrape_futures

    def test_a_running_scrape_answers_409(self, api_client, app, storage, owned_search):
        running = MagicMock()
        running.done.return_value = False
        app._scrape_futures[1] = running

        resp = api_client.post("/api/scrape/1")

        assert resp.status_code == 409
        assert resp.get_json()["error"] == "Scraping déjà en cours pour cette recherche"
        app._scrape_executor.submit.assert_not_called()

    def test_a_finished_scrape_can_be_relaunched(self, api_client, app, storage, owned_search):
        finished = MagicMock()
        finished.done.return_value = True
        app._scrape_futures[1] = finished

        resp = api_client.post("/api/scrape/1")

        assert resp.status_code == 202
        app._scrape_executor.submit.assert_called_once()
        assert app._scrape_futures[1] is not finished

    def test_a_missing_search_is_never_submitted(self, api_client, app, storage):
        storage.searches.get_search.return_value = None

        resp = api_client.post("/api/scrape/999")

        assert resp.status_code == 404
        app._scrape_executor.submit.assert_not_called()
