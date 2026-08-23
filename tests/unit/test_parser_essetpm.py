"""Tests unitaires de parsers/essetpm.py.

Esset PM expose une API JSON publique (`bo-back.esset-pm.com/v1/public/api`)
dont le contrat a été vérifié en direct le 23/08/2026 — captures réelles dans
tests/fixtures/essetpm/ (POST /offre ville/département/national, code inconnu,
fiche complète). Ces tests figent ce contrat et les pièges qu'il impose :

* le champ `budget` du corps est IGNORÉ par l'API (vérifié en direct : les
  mêmes loyers reviennent avec budget [100000, 200000]) — priceMin/Max sont
  donc rejoués LOCALEMENT sur `loyerCc`, comme surface, pièces, chambres ;
* un POST par code postal pour une ville entière (whole_city), un seul pour
  département/région — les codes INSEE officiels passent tels quels ;
* `codeDepartement` arrive bourré d'espaces dans les captures (« 75 », « 92 »)
  : le filtrage local ne s'appuie QUE sur `codePostal` (strippé), jamais sur
  ce champ sale ;
* `matches_locations(codePostal)` échoue FERMÉ si le CP est vide ;
* le type canonique se lit sur le PREMIER mot de `typeBien` (« Appartement
  duplex F4 » -> appartement) ;
* tout échec est une ValueError explicite, jamais une liste vide qui masque
  le problème — SAUF la perte d'une fiche détail, dégradation gracieuse voulue.

Aucun appel réseau : le socle bloque le transport HTTP (tests/conftest.py),
le double `requests.Session` rejoue les captures JSON réelles.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from parsers.essetpm import (
    _BASE_BODY,
    API_URL,
    BASE_URL,
    IMG_URL,
    EssetPmParser,
    _canonical_type,
    _count_matches,
    _format_eur,
    _passes_filters,
    _perimeter_requests,
    _photo_url,
    _request,
    _search_body,
    _strip_text,
    _to_float,
    _to_int,
)
from tests.helpers.factories import (
    make_city_location,
    make_department_location,
    make_region_location,
    make_whole_city_location,
)

# ---------------------------------------------------------------------------
# Captures réelles du 23/08/2026 (tests/fixtures/essetpm/)
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "essetpm"


def load_fixture(name: str):
    """Une capture JSON réelle, telle que renvoyée par l'API publique."""
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


OFFERS_PARIS_5 = load_fixture("offre_ville_75005.json")
OFFERS_DEPT_92 = load_fixture("offre_departement_92.json")
OFFERS_NATIONAL = load_fixture("offre_national_sans_filtre.json")
DETAIL_29383 = load_fixture("detail_29383-23.json")
# L'offre de liste correspondant à la fiche complète capturée.
OFFER_29383 = next(r for r in OFFERS_DEPT_92 if r["codeAnnonce"] == "29383-23")

# ---------------------------------------------------------------------------
# Périmètres réutilisés — ne jamais muter ces dicts
# ---------------------------------------------------------------------------

PARIS_5 = make_city_location("Paris", "75005", "75105")
PARIS_WHOLE = make_whole_city_location("Paris", ("75005", "75014"), "75056")
HAUTS_DE_SEINE = make_department_location("92", "Hauts-de-Seine")
IDF = make_region_location(
    "11", "Île-de-France", ("75", "77", "78", "91", "92", "93", "94", "95")
)


def offer(code: str = "29383-42", **overrides) -> dict:
    """Une offre brute de POST /offre, au format exact des captures."""
    row = {
        "typeBien": "Appartement F2",
        "codeAnnonce": code,
        "codeRegion": "11",
        "codeDepartement": "92 ",
        "nomDepartement": "Hauts-de-Seine",
        "codePostal": "92800",
        "ville": "PUTEAUX",
        "nbPieces": 2,
        "surface": 50.2,
        "loyerCc": 1395.0,
        "photoCouverture": "/lot/29383/42/1.jpg",
        "nbPhotos": 6,
    }
    row.update(overrides)
    return row


def rent_criteria(**overrides) -> dict:
    """Critères canoniques minimaux : location, Paris 5e."""
    criteria = {"locations": [PARIS_5], "transaction": "rent"}
    criteria.update(overrides)
    return criteria


def expected_post(type_lieu: str, code_lieu: str) -> dict:
    """Le corps POST exact attendu pour un périmètre : le « pas de filtre »
    vérifié en direct (tous les drapeaux à false = 92 offres au lieu de 90),
    complété du seul couple lieu. AUCUN autre critère ne part sur le réseau :
    l'API ignore le budget, tout est rejoué localement."""
    return {**_BASE_BODY, "typeLieu": type_lieu, "codeLieu": code_lieu}


# ---------------------------------------------------------------------------
# Double de requests.Session routé comme l'API publique Esset PM
# ---------------------------------------------------------------------------


class FakeJsonResponse:
    def __init__(self, payload=None, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeEssetApi:
    """Double de `requests.Session` routé comme l'API Esset PM.

    * POST {API_URL}/offre : la réponse est choisie selon le `codeLieu` reçu
      dans le corps (`offers_by_code_lieu`) — [] par défaut, comme le vrai
      service pour un code inconnu (HTTP 200, vide légitime).
    * GET {API_URL}/offre/{code} : la fiche de `details_by_code`, sinon un
      dict VIDE (= fiche absente : _fetch_detail renvoie None sans bruit).
      `failing_details` force l'échec réseau du GET (après ses retries).

    Tout appel est enregistré : corps POST reçus, codes de fiche demandés.
    """

    def __init__(self, offers_by_code_lieu: dict | None = None,
                 details_by_code: dict | None = None,
                 failing_details: set[str] | None = None):
        self.headers: dict[str, str] = {}
        self.post_bodies: list[dict] = []
        self.detail_codes: list[str] = []
        self._offers = dict(offers_by_code_lieu or {})
        self._details = dict(details_by_code or {})
        self._failing_details = set(failing_details or ())

    def request(self, method, url, timeout=None, **kwargs):
        if method == "POST" and url == f"{API_URL}/offre":
            body = kwargs["json"]
            self.post_bodies.append(body)
            payload = self._offers.get(body.get("codeLieu"), [])
            if isinstance(payload, Exception):
                raise payload
            # Un dict (ex. {"status": "error"}) doit rester un dict : c'est
            # justement la réponse « erreur » que le parser doit détecter.
            if isinstance(payload, dict):
                return FakeJsonResponse(dict(payload))
            return FakeJsonResponse(list(payload))
        if method == "GET" and url.startswith(f"{API_URL}/offre/"):
            code = urllib.parse.unquote(url.rsplit("/", 1)[-1])
            self.detail_codes.append(code)
            if code in self._failing_details:
                raise requests.ConnectionError(f"réseau coupé ({code})")
            return FakeJsonResponse(dict(self._details.get(code, {})))
        raise AssertionError(f"Appel HTTP inattendu : {method} {url}")

    @property
    def urls(self) -> list[str]:
        return [
            f"POST {API_URL}/offre?codeLieu={b.get('codeLieu')}" for b in self.post_bodies
        ]


def run_scrape(criteria: dict, api: FakeEssetApi) -> list:
    """Exécute `scrape()` en substituant `api` à la vraie requests.Session."""
    with patch("requests.Session", return_value=api):
        return EssetPmParser().scrape(criteria)


@pytest.fixture
def logged():
    """Les messages loguru émis pendant le test, sous forme (niveau, message).

    `caplog` ne voit pas loguru : il faut brancher un sink (même socle que
    Century 21 et SeLoger).
    """
    from loguru import logger

    records: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda message: records.append((message.record["level"].name, message.record["message"])),
        level="DEBUG",
    )
    yield records
    logger.remove(sink_id)


# ===========================================================================
# P1 — Traduction canonique -> requêtes Esset PM
# ===========================================================================


class TestPerimeterRequests:
    def test_a_city_is_one_post_with_its_postal_code(self):
        api = FakeEssetApi({"75005": OFFERS_PARIS_5})

        listings = run_scrape(rent_criteria(), api)

        assert api.post_bodies == [expected_post("v", "75005")]
        assert len(listings) == len(OFFERS_PARIS_5)

    def test_a_whole_city_is_one_post_per_postal_code(self):
        """Le site modélise ses lieux comme des couples (ville, code postal) :
        une commune entière se scrape en UN POST PAR CP, jamais en liste."""
        api = FakeEssetApi({"75005": [], "75014": []})
        criteria = {"locations": [PARIS_WHOLE], "transaction": "rent"}

        run_scrape(criteria, api)

        assert api.post_bodies == [
            expected_post("v", "75005"),
            expected_post("v", "75014"),
        ]

    def test_a_department_posts_its_insee_code_directly(self):
        api = FakeEssetApi({"92": OFFERS_DEPT_92})
        criteria = {"locations": [HAUTS_DE_SEINE], "transaction": "rent"}

        listings = run_scrape(criteria, api)

        assert api.post_bodies == [expected_post("d", "92")]
        assert len(listings) == len(OFFERS_DEPT_92)

    def test_a_region_posts_its_insee_code_directly(self):
        api = FakeEssetApi({"11": []})
        criteria = {"locations": [IDF], "transaction": "rent"}

        run_scrape(criteria, api)

        assert api.post_bodies == [expected_post("r", "11")]

    def test_several_perimeters_are_posted_in_order(self):
        api = FakeEssetApi({"92": [], "11": [], "75005": []})
        criteria = {"locations": [HAUTS_DE_SEINE, IDF, PARIS_5], "transaction": "rent"}

        run_scrape(criteria, api)

        assert [(b["typeLieu"], b["codeLieu"]) for b in api.post_bodies] == [
            ("d", "92"),
            ("r", "11"),
            ("v", "75005"),
        ]

    def test_every_filter_flag_is_sent_to_false(self):
        """« Pas de filtre » vérifié en direct : tous les drapeaux à false
        renvoie TOUT (92 offres contre 90 avec drapeaux actifs). Un drapeau
        activerait une exclusion serveur irrécupérable localement."""
        api = FakeEssetApi({"75005": OFFERS_PARIS_5})

        run_scrape(rent_criteria(propertyTypes=["apartment"]), api)

        body = api.post_bodies[0]
        flags = {
            k: v for k, v in body.items()
            if k not in ("lieu", "typeLieu", "codeLieu", "budget")
        }
        assert set(flags.values()) == {False}

    def test_an_unknown_kind_is_refused_by_search_body(self):
        with pytest.raises(ValueError, match="niveau de périmètre inconnu"):
            _search_body({"kind": "galaxy", "code": "X"})

    def test_a_perimeter_without_usable_code_is_refused(self):
        """Garde défensif : une ville sans code postal n'a pas de corps valide.
        (Injoignable via scrape() : normalize_locations écarte déjà ces
        localisations, mais _search_body refuse aussi de fabriquer un POST
        à vide.)"""
        with pytest.raises(ValueError, match="sans code exploitable"):
            _search_body({"kind": "city", "city": "Paris"})

    @pytest.mark.parametrize(
        "location",
        [{"kind": "department"}, {"kind": "region"}],
        ids=["departement_sans_code", "region_sans_code"],
    )
    def test_department_and_region_without_code_raise_value_error_never_key_error(self, location):
        """🔒 Non-régression : l'ancien code lisait `location["code"]` AVANT le
        garde — un département/région sans code levait KeyError au lieu de la
        ValueError attendue. Le garde « sans code exploitable » doit rester
        l'unique issue pour tout périmètre à code absent."""
        with pytest.raises(ValueError, match="sans code exploitable"):
            _search_body(location)

    def test_perimeter_requests_expands_only_whole_cities(self):
        pairs = _perimeter_requests([IDF, PARIS_WHOLE])

        assert [body["typeLieu"] for _, body in pairs] == ["r", "v", "v"]
        assert [body["codeLieu"] for _, body in pairs] == ["11", "75005", "75014"]
        # La localisation associée au POST porte LE code postal du POST :
        # c'est elle que le filtre local consultera.
        assert pairs[1][0]["postalCode"] == "75005"
        assert pairs[2][0]["postalCode"] == "75014"


# ===========================================================================
# P1 — Rejeu local du budget (l'API ignore le champ `budget`)
# ===========================================================================


class TestBudgetReplay:
    def test_price_max_is_enforced_locally_on_the_full_capture(self):
        """Capture nationale (92 offres, loyers dès 141 €) + priceMax 1000 :
        l'API renverrait tout (budget ignoré), le parser ne rend QUE les
        loyers sous la borne."""
        api = FakeEssetApi({"11": OFFERS_NATIONAL})

        listings = run_scrape(
            {"locations": [IDF], "transaction": "rent", "priceMax": 1000}, api
        )

        expected = [r for r in OFFERS_NATIONAL if r["loyerCc"] <= 1000]
        assert len(listings) == len(expected)
        assert all(li.price_value is not None and li.price_value <= 1000 for li in listings)

    def test_price_min_is_enforced_too(self):
        api = FakeEssetApi({"11": OFFERS_NATIONAL})

        listings = run_scrape(
            {"locations": [IDF], "transaction": "rent", "priceMin": 200}, api
        )

        # Une seule offre de la capture est sous 200 € (141 €/mois).
        assert len(listings) == len(OFFERS_NATIONAL) - 1
        assert all(li.price_value >= 200 for li in listings)

    def test_bounds_are_inclusive(self):
        row = offer(loyerCc=1200.0)

        assert _passes_filters(row, {"priceMin": 1200, "priceMax": 1200},
                               [{"kind": "department", "code": "92"}]) is True

    @pytest.mark.parametrize(
        ("loyer", "criteria", "expected"),
        [
            (1395.0, {"priceMin": 1400}, False),
            (1395.0, {"priceMax": 1300}, False),
            (1395.0, {"priceMin": 1000, "priceMax": 1500}, True),
            # FAIL-OPEN : un loyer illisible n'écarte pas l'annonce (comme le
            # prix chez Century 21) — elle sera jugée plus loin, pas perdue.
            (None, {"priceMax": 1}, True),
        ],
        ids=["sous_le_min", "au_dessus_du_max", "dans_les_bornes", "loyer_illisible"],
    )
    def test_rent_bounds(self, loyer, criteria, expected):
        row = offer(loyerCc=loyer)

        assert _passes_filters(row, criteria, [HAUTS_DE_SEINE]) is expected

    def test_surface_bounds_are_replayed_locally_too(self):
        assert _passes_filters(offer(surface=207.0), {"surfaceMax": 60}, [HAUTS_DE_SEINE]) is False
        assert _passes_filters(offer(surface=25.0), {"surfaceMin": 60}, [HAUTS_DE_SEINE]) is False
        # Surface illisible : fail-open, même contrat que le prix.
        assert _passes_filters(offer(surface=None), {"surfaceMin": 999}, [HAUTS_DE_SEINE]) is True


# ===========================================================================
# P1 — Localisation : matches_locations échoue fermé si le CP est vide
# ===========================================================================


class TestPostalCodeGuard:
    @pytest.mark.parametrize(
        ("zip_code", "expected"),
        [
            ("92800", True),
            # Espaces parasites autour du CP : strippés avant comparaison.
            (" 92800 ", True),
            # Hors périmètre : refusé.
            ("75005", False),
            # ÉCHEC FERMÉ : sans CP, on ne sait PAS situer le bien — jamais
            # le bénéfice du doute (contrairement au prix ou à la surface).
            ("", False),
            ("   ", False),
            (None, False),
        ],
        ids=["cp_valide", "cp_espace", "hors_perimetre", "vide", "espaces", "absent"],
    )
    def test_the_offer_must_land_in_the_perimeter(self, zip_code, expected):
        row = offer(codePostal=zip_code)

        assert _passes_filters(row, {}, [HAUTS_DE_SEINE]) is expected

    def test_an_offer_without_postal_code_is_never_listed(self):
        """Via scrape : une offre dont le CP manque n'apparaît pas, alors que
        l'API l'a pourtant renvoyée dans le périmètre."""
        rows = [offer("ok-1"), offer("ko-sans-cp", codePostal=""), offer("ok-2")]
        api = FakeEssetApi({"92": rows})

        listings = run_scrape({"locations": [HAUTS_DE_SEINE], "transaction": "rent"}, api)

        assert [li.legacy_id for li in listings] == ["ok-1", "ok-2"]

    def test_a_result_outside_the_perimeter_is_filtered_out(self):
        """Filet exact : le contrôle local tient même une réponse API qui
        déborderait (ex. élargissement serveur imprévu)."""
        rows = [offer("dedans", codePostal="92800"), offer("dehors", codePostal="33000")]
        api = FakeEssetApi({"92": rows})

        listings = run_scrape({"locations": [HAUTS_DE_SEINE], "transaction": "rent"}, api)

        assert [li.legacy_id for li in listings] == ["dedans"]

    def test_the_stored_zip_code_is_stripped(self):
        rows = [offer("ok-1", codePostal=" 92800 ")]
        api = FakeEssetApi({"92": rows})

        listings = run_scrape({"locations": [HAUTS_DE_SEINE], "transaction": "rent"}, api)

        assert listings[0].zip_code == "92800"


# ===========================================================================
# P1 — Types : premier mot de typeBien, garde terrain
# ===========================================================================


class TestPropertyTypes:
    @pytest.mark.parametrize(
        ("type_bien", "criteria", "expected"),
        [
            # Premier mot gagne : le libellé peut être composé.
            ("Appartement duplex F4", {"propertyTypes": ["apartment"]}, True),
            ("Parking extérieur", {"propertyTypes": ["parking"]}, True),
            ("Maison de village", {"propertyTypes": ["house"]}, True),
            # Type demandé différent : rejeté.
            ("Parking extérieur", {"propertyTypes": ["apartment"]}, False),
            # Type illisible alors que des types SONT demandés : rejeté.
            ("Terrain à bâtir", {"propertyTypes": ["apartment"]}, False),
            (None, {"propertyTypes": ["apartment"]}, False),
            # Aucun type demandé : tout passe, y compris l'illisible.
            ("Terrain à bâtir", {}, True),
        ],
        ids=["appartement_compose", "parking", "maison", "autre_type",
             "terrain_demande_apart", "type_absent", "aucun_type_demande"],
    )
    def test_first_word_of_typebien_decides(self, type_bien, criteria, expected):
        row = offer(typeBien=type_bien)

        assert _passes_filters(row, criteria, [HAUTS_DE_SEINE]) is expected

    def test_no_detail_is_fetched_when_no_offer_survives_the_types(self):
        """Capture ville 100 % appartements + recherche maison : rien n'est
        retenu ET surtout aucune fiche détail n'est gaspillée."""
        api = FakeEssetApi({"75005": OFFERS_PARIS_5})

        listings = run_scrape(
            rent_criteria(propertyTypes=["house"]), api
        )

        assert listings == []
        assert api.detail_codes == []

    def test_a_land_only_request_raises_before_any_http_call(self):
        """Esset PM ne référence aucun terrain : mieux qu'un résultat vide qui
        masquerait le problème — échec immédiat et explicite."""
        api = FakeEssetApi()

        with pytest.raises(ValueError, match="ne référence pas"):
            run_scrape(rent_criteria(propertyTypes=["land"]), api)

        assert api.post_bodies == []

    def test_partially_covered_types_warn_but_proceed(self, logged):
        api = FakeEssetApi({"75005": OFFERS_PARIS_5})
        criteria = rent_criteria(propertyTypes=["apartment", "land"])

        listings = run_scrape(criteria, api)

        assert len(listings) == len(OFFERS_PARIS_5)
        assert any(level == "WARNING" and "land" in message for level, message in logged)


# ===========================================================================
# P1 — Pièces (liste) et chambres (fiche) : « N » signifie « N et plus »
# ===========================================================================


class TestRoomCounts:
    @pytest.mark.parametrize(
        ("count", "allowed", "expected"),
        [
            (3, [2, 3], True),
            (3, [2], False),
            # Le « 5 » canonique signifie « 5 et plus » (voir search_edit.html).
            (7, [5], True),
            (4, [5], False),
            (6, [6], True),
            # Pas de critère : tout passe.
            (2, [], True),
            # Compte absent : laisse passer (on ne rejette pas ce qu'on ne
            # sait pas lire).
            (None, [2], True),
        ],
        ids=["dans_la_liste", "hors_liste", "sept_vs_cinq_plus", "quatre_vs_cinq_plus",
             "six_exact", "aucun_critere", "pieces_absentes"],
    )
    def test_count_matches_contract(self, count, allowed, expected):
        assert _count_matches(count, allowed) is expected

    def test_nb_pieces_is_judged_against_rooms(self):
        row = offer(nbPieces=7)

        assert _passes_filters(row, {"rooms": [5]}, [HAUTS_DE_SEINE]) is True
        assert _passes_filters(row, {"rooms": [2]}, [HAUTS_DE_SEINE]) is False

    def test_bedrooms_from_the_detail_can_reject_the_offer(self):
        """La liste ne porte pas les chambres : seule la fiche les connaît.
        Une fiche qui révèle nbChambres hors critères écarte l'offre."""
        api = FakeEssetApi(
            {"92": [offer("29383-23")]},
            details_by_code={"29383-23": {"nbChambres": 4}},
        )

        listings = run_scrape(
            {"locations": [HAUTS_DE_SEINE], "transaction": "rent", "bedrooms": [2]}, api
        )

        assert listings == []

    def test_matching_bedrooms_keep_the_real_capture_listing(self):
        """Fiche réelle (detail_29383-23.json, nbChambres=1) + bedrooms [1] :
        l'annonce est conservée et enrichie normalement."""
        api = FakeEssetApi(
            {"92": [OFFER_29383]},
            details_by_code={"29383-23": DETAIL_29383},
        )
        criteria = {"locations": [HAUTS_DE_SEINE], "transaction": "rent", "bedrooms": [1]}

        listings = run_scrape(criteria, api)

        assert [li.listing_id for li in listings] == ["essetpm_29383-23"]

    def test_no_bedroom_criteria_keeps_everything(self):
        api = FakeEssetApi(
            {"92": [offer("a"), offer("b")]},
            details_by_code={"a": {"nbChambres": 9}, "b": {"nbChambres": 0}},
        )

        listings = run_scrape(
            {"locations": [HAUTS_DE_SEINE], "transaction": "rent"}, api
        )

        assert [li.legacy_id for li in listings] == ["a", "b"]


# ===========================================================================
# P1 — Erreurs : ValueError explicites, jamais un silence
# ===========================================================================


class TestScrapeValueErrors:
    def test_buy_transaction_is_refused_without_any_call(self):
        """Vérifié en direct : le portail ne référence QUE de la location."""
        api = FakeEssetApi()

        with pytest.raises(ValueError, match="que de la location"):
            run_scrape(rent_criteria(transaction="buy"), api)

        assert api.post_bodies == []

    @pytest.mark.parametrize(
        "criteria",
        [{}, {"locations": []}, {"locations": [{"kind": "city", "city": "Paris"}]}],
        ids=["vide", "liste_vide", "localisation_incomplete"],
    )
    def test_no_usable_location_raises_before_any_call(self, criteria):
        api = FakeEssetApi()

        with pytest.raises(ValueError, match="au moins une localisation"):
            run_scrape(criteria, api)

        assert api.post_bodies == []

    def test_every_perimeter_failing_raises_an_aggregated_error(self):
        """Tous les périmètres en échec réseau : le scrape LÈVE (une liste
        vide serait indistinguable d'une recherche légitimement vide)."""
        api = FakeEssetApi({"92": [], "33": []})
        real_request = api.request

        def failing_request(method, url, timeout=None, **kwargs):
            real_request(method, url, timeout=timeout, **kwargs)  # enregistre puis…
            raise requests.ConnectionError("réseau coupé")

        api.request = failing_request
        criteria = {
            "locations": [HAUTS_DE_SEINE, make_department_location("33", "Gironde")],
            "transaction": "rent",
        }

        with pytest.raises(ValueError, match="inaccessible après 3 tentatives") as excinfo:
            with patch("requests.Session", return_value=api):
                EssetPmParser().scrape(criteria)

        # Chaque périmètre a été tenté (3 retries chacun) et cité dans l'erreur.
        assert len(api.post_bodies) == 6
        assert {b["codeLieu"] for b in api.post_bodies} == {"92", "33"}
        assert "Hauts-de-Seine" in str(excinfo.value)
        assert "Gironde" in str(excinfo.value)

    def test_one_failed_perimeter_among_two_gives_partial_results(self):
        """Dégradation partielle VOULUE : le périmètre en échec est tracé,
        celui qui fonctionne livre ses annonces — on ne perd pas tout parce
        qu'un périmètre a bronché."""
        api = FakeEssetApi({"92": [], "75005": OFFERS_PARIS_5})
        real_request = api.request

        def flaky_request(method, url, timeout=None, **kwargs):
            if method == "POST" and kwargs.get("json", {}).get("codeLieu") == "92":
                api.post_bodies.append(kwargs["json"])
                raise requests.ConnectionError("réseau coupé")
            return real_request(method, url, timeout=timeout, **kwargs)

        api.request = flaky_request
        criteria = {"locations": [HAUTS_DE_SEINE, PARIS_5], "transaction": "rent"}

        # (Un warning Python ferait échouer le test : filterwarnings = error.)
        with patch("requests.Session", return_value=api):
            listings = EssetPmParser().scrape(criteria)

        assert [li.zip_code for li in listings] == ["75005"] * len(OFFERS_PARIS_5)

    def test_an_unexpected_object_response_is_a_loud_failure(self):
        """L'app réagit à un objet porteur de `status` comme à une erreur :
        un dict là où on attend une liste doit faire échouer le périmètre,
        pas devenir silencieusement zéro annonce."""
        api = FakeEssetApi({"92": {"status": "error"}})

        with pytest.raises(ValueError, match="réponse inattendue"):
            run_scrape({"locations": [HAUTS_DE_SEINE], "transaction": "rent"}, api)

    def test_an_unknown_postal_code_is_a_legitimate_empty_result(self):
        """Capture offre_code_inconnu.json : HTTP 200 [] — résultat vide
        légitime, PAS une erreur."""
        api = FakeEssetApi({"99999": load_fixture("offre_code_inconnu.json")})

        listings = run_scrape(
            {"locations": [make_city_location("Nawak", "99999", "99999")],
             "transaction": "rent"},
            api,
        )

        assert listings == []


# ===========================================================================
# P1 — Déduplication entre périmètres recouvrants
# ===========================================================================


class TestDeduplication:
    def test_overlapping_perimeters_yield_unique_listings(self):
        """Département 92 inclus dans Île-de-France : les 55 offres du 92
        reviennent des DEUX POST — elles ne doivent apparaître qu'une fois,
        et chaque annonce du périmètre n'apparaît qu'une fois au total."""
        api = FakeEssetApi({"92": OFFERS_DEPT_92, "11": OFFERS_NATIONAL})
        criteria = {
            "locations": [HAUTS_DE_SEINE, IDF],
            "transaction": "rent",
        }

        listings = run_scrape(criteria, api)

        ids = [li.listing_id for li in listings]
        assert len(ids) == len(set(ids))
        # Les 55 offres du 92 ont toutes survécu, chacune une seule fois.
        dept_92_ids = {f"essetpm_{r['codeAnnonce']}" for r in OFFERS_DEPT_92}
        assert len(set(ids) & dept_92_ids) == len(dept_92_ids)
        # Et le périmètre large n'a apporté que du neuf (pas de re-listing).
        assert all(i.startswith("essetpm_") for i in ids)

    def test_a_duplicate_inside_one_answer_is_collapsed(self):
        api = FakeEssetApi({"92": [offer("doublon"), offer("unique"), offer("doublon")]})

        listings = run_scrape({"locations": [HAUTS_DE_SEINE], "transaction": "rent"}, api)

        assert [li.legacy_id for li in listings] == ["doublon", "unique"]

    def test_an_offer_with_an_empty_code_is_skipped(self):
        api = FakeEssetApi({"92": [offer("", codeAnnonce=""), offer("ok")]})

        listings = run_scrape({"locations": [HAUTS_DE_SEINE], "transaction": "rent"}, api)

        assert [li.legacy_id for li in listings] == ["ok"]


# ===========================================================================
# P1 — Détail indisponible : dégradation gracieuse
# ===========================================================================


class TestGracefulDetailDegradation:
    def test_a_failing_detail_keeps_the_unenriched_listing(self, logged):
        """La liste suffit à notifier : la fiche complète reviendra au
        prochain passage. L'échec du GET ne fait PAS échouer le scrape."""
        api = FakeEssetApi({"75005": OFFERS_PARIS_5}, failing_details={"42107-43"})

        listings = run_scrape(rent_criteria(), api)

        assert len(listings) == len(OFFERS_PARIS_5)
        degraded = next(li for li in listings if li.legacy_id == "42107-43")
        assert degraded.title == "Appartement F2 à PARIS"  # titre de liste
        assert degraded.description == ""
        assert degraded.photos == "[]"
        assert degraded.epc == ""
        assert any(
            level == "WARNING" and "42107-43 indisponible" in message
            for level, message in logged
        )

    def test_an_empty_detail_dict_degrades_without_noise(self):
        """Fiche absente ({} -> None) : ni exception ni warning, l'annonce
        brute est livrée telle quelle."""
        api = FakeEssetApi({"75005": OFFERS_PARIS_5})  # détails vides par défaut

        with patch("parsers.essetpm.logger.warning") as mock_warning:
            listings = run_scrape(rent_criteria(), api)

        mock_warning.assert_not_called()
        assert len(listings) == len(OFFERS_PARIS_5)

    def test_the_backoff_sleeps_between_detail_fetches(self, slept):
        """Anti-burst : 0,3 s entre deux fiches (jamais avant la première)."""
        rows = [offer("a"), offer("b")]
        api = FakeEssetApi({"92": rows})

        run_scrape({"locations": [HAUTS_DE_SEINE], "transaction": "rent"}, api)

        assert slept == [0.3]


# ===========================================================================
# P1 — Mapping Listing : identifiants, URL publique, champs affichés
# ===========================================================================


class TestListingMapping:
    def test_a_raw_offer_maps_the_common_schema(self):
        """Offre de liste sans fiche (détails vides) : tous les champs viennent
        du seul POST /offre."""
        api = FakeEssetApi({"75005": OFFERS_PARIS_5})

        listings = run_scrape(rent_criteria(), api)

        raw = next(li for li in listings if li.legacy_id == "42107-43")
        assert raw.listing_id == "essetpm_42107-43"
        assert raw.url == f"{BASE_URL}/location/42107-43"
        assert raw.source == "essetpm"
        assert raw.agency == "Esset Property Management"
        assert raw.title == "Appartement F2 à PARIS"
        assert raw.price == "1 500 € CC"
        assert raw.price_value == 1500.0
        assert raw.surface == "42.7"
        assert raw.rooms == "2"
        assert raw.city == "PARIS"
        assert raw.location == "PARIS"
        assert raw.zip_code == "75005"
        assert raw.property_type == "apartment"

    def test_listing_id_and_url_follow_the_code_annonce(self):
        api = FakeEssetApi({"92": [OFFER_29383]})

        listings = run_scrape({"locations": [HAUTS_DE_SEINE], "transaction": "rent"}, api)

        assert listings[0].listing_id == "essetpm_29383-23"
        assert listings[0].url == f"{BASE_URL}/location/29383-23"

    def test_the_cover_photo_path_is_percent_encoded(self):
        """Capture réelle : « Capture d'écran 2026-05-14 181739.png » —
        espaces et apostrophes quotés, chemin servi par le CDN Foncia."""
        api = FakeEssetApi({"75005": OFFERS_PARIS_5})

        listings = run_scrape(rent_criteria(), api)

        cover = next(li for li in listings if li.legacy_id == "42107-54")
        assert cover.image_url == (
            f"{IMG_URL}/lot/42107/54/"
            "Capture%20d%27%C3%A9cran%202026-05-14%20181739.png"
        )


class TestDetailEnrichment:
    def test_the_real_capture_is_fully_enriched(self):
        """Fiche réelle detail_29383-23.json : titre (accroche), description
        HTML mise en texte brut, DPE/GES, photos absolues, programme et
        précisions financières compactes."""
        api = FakeEssetApi(
            {"92": [OFFER_29383]},
            details_by_code={"29383-23": DETAIL_29383},
        )

        listings = run_scrape({"locations": [HAUTS_DE_SEINE], "transaction": "rent"}, api)

        enriched = listings[0]
        assert enriched.title == "2 pièces de 50 m²"
        assert "<p>" not in enriched.description
        assert "&nbsp;" not in enriched.description
        assert "séjour lumineux" in enriched.description
        assert enriched.epc == "60"
        assert enriched.ges == "1"
        photos = json.loads(enriched.photos)
        assert [p["url"] for p in photos] == [f"{IMG_URL}{path}" for path in DETAIL_29383["photos"]]
        assert enriched.image_url == f"{IMG_URL}/lot/29383/23/1.jpg"
        assert enriched.headline == "DOMAINE VICTORINE"
        assert enriched.price_details == (
            "loyer HC 1 270 € · provisions de charges 125 € "
            "· honoraires 759 € · dépôt de garantie 1 270 €"
        )

    def test_an_accroche_less_detail_keeps_the_list_title(self):
        api = FakeEssetApi(
            {"92": [offer("sans-accroche")]},
            details_by_code={"sans-accroche": {"nomProgramme": "LE DOMAINE"}},
        )

        listings = run_scrape({"locations": [HAUTS_DE_SEINE], "transaction": "rent"}, api)

        assert listings[0].title == "Appartement F2 à PUTEAUX"
        assert listings[0].headline == "LE DOMAINE"


# ===========================================================================
# P1 — Le inseeCode survive partout
# ===========================================================================


class TestInseeCodeSurvival:
    def test_scrape_never_mutates_the_criteria(self):
        """Les critères sont partagés entre toutes les sources d'une recherche
        pendant un scrape : les locations (et leur inseeCode) doivent sortir
        intactes du pipeline Esset PM."""
        criteria = rent_criteria(
            locations=[dict(PARIS_5), HAUTS_DE_SEINE],
            propertyTypes=["apartment"],
            priceMax=1500,
            rooms=[2, 3],
        )
        snapshot = json.loads(json.dumps(criteria))
        api = FakeEssetApi({"75005": [], "92": []})

        run_scrape(criteria, api)

        assert criteria == snapshot
        assert criteria["locations"][0]["inseeCode"] == "75105"


# ===========================================================================
# P2 — Helpers purs
# ===========================================================================


class TestCanonicalType:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Appartement duplex F4", "apartment"),
            ("APPARTEMENT", "apartment"),
            ("Maison de village", "house"),
            ("Parking extérieur", "parking"),
            ("parking", "parking"),
            # Type hors registre : None (et non une chaîne vide).
            ("Studio meublé", None),
            ("Cave", None),
            ("", None),
            ("   ", None),
            (None, None),
        ],
        ids=["duplex", "majuscules", "maison", "parking_libelle", "parking_minuscule",
             "studio", "cave", "vide", "espaces", "none"],
    )
    def test_first_word_lookup(self, label, expected):
        assert _canonical_type(label) == expected


class TestPhotoUrl:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            # Chemin normal : préfixé du CDN Foncia.
            ("/lot/29383/23/1.jpg", f"{IMG_URL}/lot/29383/23/1.jpg"),
            # Sans séparateur initial : il est ajouté.
            ("lot/x.jpg", f"{IMG_URL}/lot/x.jpg"),
            # Espaces et apostrophes : fréquents côté Esset, toujours quotés.
            (
                "/lot/42107/54/Capture d'écran 2026-05-14 181739.png",
                f"{IMG_URL}/lot/42107/54/Capture%20d%27%C3%A9cran%202026-05-14%20181739.png",
            ),
            # Les "/" du chemin restent littéraux.
            ("/a b/c.jpg", f"{IMG_URL}/a%20b/c.jpg"),
            ("", ""),
            ("   ", ""),
            (None, ""),
        ],
        ids=["chemin_normal", "sans_slash_initial", "capture_ecran", "slash_conserves",
             "vide", "espaces", "absent"],
    )
    def test_absolute_quoted_urls(self, path, expected):
        assert _photo_url(path) == expected


class TestFormatEur:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (5277.0, "5 277 €"),
            (999, "999 €"),
            (759.63, "759 €"),
            (0, "0 €"),
            (None, ""),
        ],
        ids=["milliers", "simple", "centimes_tronques", "zero", "absent"],
    )
    def test_french_formatting(self, value, expected):
        assert _format_eur(value) == expected


class TestNumericConversions:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("12.5", 12.5),
            (42, 42.0),
            (None, None),
            ("abc", None),
            (object(), None),
        ],
    )
    def test_to_float(self, value, expected):
        assert _to_float(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("3", 3), (3.7, 3), (None, None), ("beaucoup", None)],
    )
    def test_to_int(self, value, expected):
        assert _to_int(value) == expected


class TestStripText:
    @pytest.mark.parametrize(
        ("html_text", "expected"),
        [
            ("<p>Bonjour</p>", "Bonjour"),
            ("<p><strong>A</strong></p><p>B</p>", "A B"),
            ("Mot1&nbsp;&nbsp;mot2", "Mot1 mot2"),
            ("déjà &quot;propre&quot;", 'déjà "propre"'),
            ("  espaces   aplatis  ", "espaces aplatis"),
            ("", ""),
            (None, ""),
        ],
        ids=["balise_simple", "balises_multiples", "nbsp", "entites", "aplatis",
             "vide", "absent"],
    )
    def test_html_becomes_flat_text(self, html_text, expected):
        assert _strip_text(html_text) == expected


# ===========================================================================
# P2 — Contrat BaseParser : validation, traduction triviale, URLs
# ===========================================================================


class TestHasValidCriteria:
    @pytest.mark.parametrize(
        "location",
        [PARIS_5, PARIS_WHOLE, HAUTS_DE_SEINE, IDF],
        ids=["commune", "ville_entiere", "departement", "region"],
    )
    def test_every_perimeter_level_is_valid_on_its_own(self, location):
        assert EssetPmParser().has_valid_criteria({"locations": [location]}) is True

    @pytest.mark.parametrize(
        "criteria",
        [{}, {"locations": []}, {"locations": [{"kind": "city", "city": "Paris"}]}],
        ids=["vide", "liste_vide", "ville_sans_cp"],
    )
    def test_everything_else_is_invalid(self, criteria):
        assert EssetPmParser().has_valid_criteria(criteria) is False

    def test_validation_never_touches_the_network(self):
        """Créer une recherche ne doit dépendre d'aucun appel HTTP : la
        résolution d'Esset PM est triviale (codes officiels embarqués)."""
        with patch("requests.Session") as mock_session:
            assert EssetPmParser().has_valid_criteria({"locations": [PARIS_5]}) is True
        mock_session.assert_not_called()


class TestToNative:
    def test_the_criteria_pass_through_unchanged(self):
        """Rien à traduire : les codes attendus par l'API (CP, INSEE) vivent
        déjà dans les localisations canoniques. Jamais de mutation sur place."""
        parser = EssetPmParser()
        criteria = {
            "locations": [PARIS_5],
            "transaction": "rent",
            "propertyTypes": ["apartment"],
            "priceMax": 1500,
            "sourceOverrides": {"seloger": {"placeIds": ["AD08FR31096"]}},
        }
        snapshot = json.loads(json.dumps(criteria))

        native = parser.to_native(criteria)

        assert native == criteria
        assert criteria == snapshot


class TestBuildSearchUrls:
    def test_one_portal_url_whatever_the_perimeters(self):
        """L'application Esset n'encode AUCUN critère dans ses URL (état React)
        : une seule URL de portail dès qu'une localisation existe — quel que
        soit leur nombre ou leur niveau."""
        parser = EssetPmParser()
        criteria = {"locations": [PARIS_5, PARIS_WHOLE, HAUTS_DE_SEINE, IDF]}

        urls = parser.build_search_urls(criteria)

        assert urls == [f"{BASE_URL}/notre-offre"]
        # build_search_url est le premier élément de la liste.
        assert parser.build_search_url(criteria) == urls[0]

    @pytest.mark.parametrize(
        "criteria",
        [{}, {"locations": []}],
        ids=["vide", "liste_vide"],
    )
    def test_no_url_without_any_location(self, criteria):
        parser = EssetPmParser()

        assert parser.build_search_urls(criteria) == []
        assert parser.build_search_url(criteria) is None

    def test_the_url_note_explains_the_missing_filters(self):
        """Sans note, un lien nu passerait pour un bug (« où sont mes filtres ?
        ») : Esset PM déclare explicitement pourquoi son URL n'en porte pas."""
        assert "URL" in EssetPmParser.URL_NOTE or "url" in EssetPmParser.URL_NOTE
        assert EssetPmParser().URL_NOTE != ""

    def test_capabilities_declare_rent_and_three_types(self):
        parser = EssetPmParser()

        assert parser.SUPPORTED_TRANSACTIONS == ("rent",)
        assert set(parser.SUPPORTED_PROPERTY_TYPES) == {"apartment", "house", "parking"}
        # Le front doit pouvoir prévenir : achat et terrain ne passeraient pas.
        reason = parser.cannot_search_reason(
            {"locations": [PARIS_5], "transaction": "buy", "propertyTypes": ["land"]}
        )
        assert reason is not None and "ne référence pas" in reason
        # Hors capacités mais critères vides : aucune raison de refuser.
        assert parser.cannot_search_reason({"locations": [PARIS_5]}) is None


# ===========================================================================
# P2 — Transport : retries et backoff (aucun sleep réel : fixture `slept`)
# ===========================================================================


class RetrySession:
    """Session minimale qui rejoue une file de résultats (réponses OU exceptions)."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def request(self, method, url, timeout=None, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class TestRequestRetries:
    def test_a_success_costs_no_retry_nor_sleep(self, slept):
        session = RetrySession([FakeJsonResponse([])])

        resp = _request(session, "POST", f"{API_URL}/offre", json={})

        assert resp.json() == []
        assert session.calls == 1
        assert slept == []

    @pytest.mark.parametrize("status", [403, 429, 500, 503], ids=["403", "429", "500", "503"])
    def test_transient_statuses_are_retried_with_backoff(self, status, slept):
        """CloudFront répond 403 aux requêtes trop nues, 429 en cas de burst :
        backoff exponentiel (2 s puis 4 s), jamais de sleep réel en test."""
        session = RetrySession([
            FakeJsonResponse(None, status_code=status),
            FakeJsonResponse({"ok": True}),
        ])

        resp = _request(session, "GET", f"{API_URL}/offre/X")

        assert resp.json() == {"ok": True}
        assert session.calls == 2
        # Un seul échec -> un seul backoff (2 s), posé AVANT la retentative.
        assert slept == [2]

    def test_exhausted_retries_raise_an_api_error(self, slept):
        session = RetrySession([
            FakeJsonResponse(None, status_code=503),
            FakeJsonResponse(None, status_code=503),
            FakeJsonResponse(None, status_code=503),
        ])

        with pytest.raises(RuntimeError, match="après 3 tentatives"):
            _request(session, "GET", f"{API_URL}/offre/X")

        assert session.calls == 3
        assert slept == [2.0, 4.0]

    def test_a_network_error_is_retried_too(self, slept):
        session = RetrySession([
            requests.ConnectionError("réseau coupé"),
            FakeJsonResponse([]),
        ])

        assert _request(session, "GET", f"{API_URL}/offre/X").json() == []
        assert session.calls == 2
        assert slept == [2.0]

    def test_a_definitive_404_is_raised_without_retry(self, slept):
        """Un code non transitoire passe directement par raise_for_status :
        retenter un 404 ne ferait que traîner."""
        session = RetrySession([FakeJsonResponse(None, status_code=404)])

        with pytest.raises(requests.HTTPError, match="HTTP 404"):
            _request(session, "GET", f"{API_URL}/offre/inconnu")

        assert session.calls == 1
        assert slept == []

    def test_browser_headers_are_installed_on_the_session(self):
        """CloudFront filtre les requêtes trop nues (403 vu en direct) : les
        en-têtes navigateur complets doivent être posés sur la session."""
        api = FakeEssetApi({"75005": []})

        with patch("requests.Session", return_value=api):
            EssetPmParser().scrape(rent_criteria())

        assert api.headers["Origin"] == BASE_URL
        assert api.headers["Referer"] == f"{BASE_URL}/notre-offre"
        assert "User-Agent" in api.headers
