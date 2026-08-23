"""La reconstruction d'URLs par source — désormais une route WEB de session.

Cette logique vivait dans l'API (`GET /api/searches/<id>/urls`) et est passée
côté web quand le token a été supprimé (#30) : le modal « Voir l'URL » de la
page Recherches l'appelle en fetch authentifié par le cookie de session.

Deux `try` distincts : le premier isole une source inconnue, le second un
échec de reconstruction (qui peut demander un appel réseau). Aucune des deux
situations ne doit priver la page de ses autres sources.
"""

from __future__ import annotations

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


class TestSearchUrlsWeb:
    def test_returns_one_entry_per_source(self, web_client, storage, owned_search, parsers_by_source):
        storage.searches.get_search.side_effect = None
        storage.searches.get_search.return_value = make_search_row(
            id=1, user_id=1, sources=["seloger", "laforet"],
        )
        parsers_by_source["seloger"] = StubParser("SeLoger", urls=["https://sl.test/a"])
        parsers_by_source["laforet"] = StubParser("Laforêt", urls=["https://lf.test/b", "https://lf.test/c"])

        resp = web_client.get("/searches/1/urls")

        assert resp.status_code == 200
        body = resp.get_json()
        assert [s["source"] for s in body["sources"]] == ["seloger", "laforet"]
        assert body["sources"][0]["url"] == "https://sl.test/a"
        assert body["sources"][0]["error"] is None
        # `urls` porte TOUTES les URL (une source multi-périmètres en produit
        # plusieurs), `url` seulement la première.
        assert body["sources"][1]["urls"] == ["https://lf.test/b", "https://lf.test/c"]
        assert body["sources"][1]["url"] == "https://lf.test/b"

    def test_top_level_fields_mirror_the_first_source(self, web_client, storage, parsers_by_source):
        """Champs hérités du contrat d'origine : les anciens consommateurs ne
        lisent que `url`."""
        storage.searches.get_search.return_value = make_search_row(id=1, user_id=1, sources=["laforet", "seloger"])
        parsers_by_source["laforet"] = StubParser("Laforêt", urls=["https://lf.test/b"])
        parsers_by_source["seloger"] = StubParser("SeLoger", urls=["https://sl.test/a"])

        body = web_client.get("/searches/1/urls").get_json()

        assert body["source"] == "laforet"
        assert body["url"] == "https://lf.test/b"
        assert body["source_name"] == "Laforêt"

    def test_an_unknown_source_is_reported_per_source_not_as_a_500(
        self, web_client, storage, parsers_by_source
    ):
        """Une source retirée du code laisse des recherches qui la citent."""
        storage.searches.get_search.return_value = make_search_row(id=1, user_id=1, sources=["disparue", "seloger"])
        parsers_by_source["seloger"] = StubParser("SeLoger", urls=["https://sl.test/a"])

        resp = web_client.get("/searches/1/urls")

        assert resp.status_code == 200
        entries = {s["source"]: s for s in resp.get_json()["sources"]}
        assert entries["disparue"]["url"] is None
        assert "inconnue" in entries["disparue"]["error"]
        # L'autre source garde son lien : c'est tout l'intérêt du try par source.
        assert entries["seloger"]["url"] == "https://sl.test/a"

    def test_an_unknown_first_source_leaves_top_level_fields_incomplete(
        self, web_client, storage, parsers_by_source
    ):
        """L'entrée d'erreur n'a ni `source_name` ni `urls` : `.get()` renvoie None."""
        storage.searches.get_search.return_value = make_search_row(id=1, user_id=1, sources=["disparue", "seloger"])
        parsers_by_source["seloger"] = StubParser("SeLoger", urls=["https://sl.test/a"])

        body = web_client.get("/searches/1/urls").get_json()

        assert body["url"] is None
        assert body["source_name"] is None

    def test_a_reconstruction_failure_is_reported_per_source(self, web_client, storage, parsers_by_source):
        """Reconstruire une URL peut demander un appel réseau, qui peut tomber."""
        storage.searches.get_search.return_value = make_search_row(id=1, user_id=1, sources=["seloger", "laforet"])
        parsers_by_source["seloger"] = StubParser("SeLoger", raises=RuntimeError("résolution du lieu HS"))
        parsers_by_source["laforet"] = StubParser("Laforêt", urls=["https://lf.test/b"])

        resp = web_client.get("/searches/1/urls")

        assert resp.status_code == 200
        entries = {s["source"]: s for s in resp.get_json()["sources"]}
        assert entries["seloger"]["url"] is None
        assert entries["seloger"]["urls"] == []
        assert "résolution du lieu HS" in entries["seloger"]["error"]
        assert entries["laforet"]["url"] == "https://lf.test/b"

    def test_no_url_available_is_an_explicit_error_not_a_silent_null(
        self, web_client, storage, parsers_by_source
    ):
        storage.searches.get_search.return_value = make_search_row(id=1, user_id=1, sources=["seloger"])
        parsers_by_source["seloger"] = StubParser("SeLoger", urls=[])

        entry = web_client.get("/searches/1/urls").get_json()["sources"][0]

        assert entry["url"] is None
        assert entry["error"] == "URL reconstruction non disponible pour cette source"

    def test_url_note_is_forwarded_and_empty_becomes_null(self, web_client, storage, parsers_by_source):
        """`URL_NOTE` explique qu'une URL omet volontairement des filtres."""
        storage.searches.get_search.return_value = make_search_row(id=1, user_id=1, sources=["laforet", "seloger"])
        parsers_by_source["laforet"] = StubParser(
            "Laforêt", urls=["https://lf.test/b"], url_note="filtres appliqués côté scraper"
        )
        parsers_by_source["seloger"] = StubParser("SeLoger", urls=["https://sl.test/a"], url_note="")

        entries = {s["source"]: s for s in web_client.get("/searches/1/urls").get_json()["sources"]}

        assert entries["laforet"]["note"] == "filtres appliqués côté scraper"
        assert entries["seloger"]["note"] is None

    def test_falls_back_to_the_legacy_source_column(self, web_client, storage, parsers_by_source):
        """Les anciennes lignes n'ont pas de `sources` : la colonne `source` sert."""
        storage.searches.get_search.return_value = make_search_row(id=1, user_id=1, sources=None, source="laforet")
        parsers_by_source["laforet"] = StubParser("Laforêt", urls=["https://lf.test/b"])

        body = web_client.get("/searches/1/urls").get_json()

        assert [s["source"] for s in body["sources"]] == ["laforet"]

    def test_a_search_with_no_source_at_all_reports_one_unknown_entry(
        self, web_client, storage, parsers_by_source
    ):
        """`sources: []` retombe sur `[source]`, donc sur `[""]` : jamais une liste vide."""
        storage.searches.get_search.return_value = make_search_row(id=1, user_id=1, sources=[], source="")

        body = web_client.get("/searches/1/urls").get_json()

        assert body["source"] == ""
        assert body["url"] is None
        assert len(body["sources"]) == 1
        assert "inconnue" in body["sources"][0]["error"]

    def test_the_parser_receives_the_storage_and_the_stored_criteria(
        self, web_client, storage, parsers_by_source
    ):
        """Le storage est injecté : SeLoger a besoin de son cache de placeIds."""
        criteria = make_criteria(priceMax=900)
        storage.searches.get_search.return_value = make_search_row(id=1, user_id=1, criteria=criteria)
        stub = StubParser("SeLoger")
        parsers_by_source["seloger"] = stub

        web_client.get("/searches/1/urls")

        assert stub.build_calls == [criteria]
        assert stub.storage is storage

    def test_a_foreign_search_is_not_served(self, web_client, storage, foreign_search, parsers_by_source):
        """Le contrôle de propriété vient avec la route : un id d'autrui donne
        une 404 JSON, jamais les URLs reconstruites (voir aussi
        test_authorization.py pour le balayage systématique)."""
        resp = web_client.get(f"/searches/{foreign_search['id']}/urls")

        assert resp.status_code == 404
        assert resp.get_json() == {"error": "Recherche introuvable"}
