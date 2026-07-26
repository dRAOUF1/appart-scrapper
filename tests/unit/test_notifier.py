"""Tests unitaires de `notifier.py` (notifications push ntfy.sh).

Le booléen renvoyé par `send()` est un **contrat**, pas une commodité :
`ScrapeService` ne marque une annonce comme notifiée que si l'envoi a renvoyé
`True` (voir tests/unit/test_scrape_service.py
::test_failed_notification_is_not_marked_handled_so_it_is_retried). Tout ce qui
transforme un envoi en `False` condamne donc l'annonce à être re-tentée à
chaque cycle de scrape, indéfiniment — d'où l'attention portée ici à *chaque*
chemin qui peut renvoyer `False`.

Les tests passent par `requests_mock` plutôt que par un `patch` de
`requests.post` : c'est délibéré. La validation des en-têtes HTTP se fait dans
`PreparedRequest.prepare_headers`, donc *avant* le transport — un double de
`requests.post` la court-circuiterait et masquerait exactement le défaut
documenté par TestHeaderInjection.
"""

from __future__ import annotations

import pytest
import requests

from models.listing import Listing
from notifier import Notifier
from tests.helpers.factories import make_listing

TOPIC = "appart-alertes"
ENDPOINT = f"https://ntfy.sh/{TOPIC}"


@pytest.fixture
def notifier():
    return Notifier()


@pytest.fixture
def ntfy(requests_mock):
    """Enregistre l'endpoint ntfy et renvoie le matcher.

    `install(status=..., exc=...)` couvre les trois branches de `send`.
    """

    def install(*, status: int = 200, text: str = "1", exc=None, endpoint: str = ENDPOINT):
        if exc is not None:
            return requests_mock.post(endpoint, exc=exc)
        return requests_mock.post(endpoint, status_code=status, text=text)

    return install


# ---------------------------------------------------------------------------
# Construction du point d'envoi
# ---------------------------------------------------------------------------

class TestEndpoint:
    @pytest.mark.parametrize(
        ("server", "expected"),
        [
            ("https://ntfy.sh", "https://ntfy.sh/mon-sujet"),
            ("https://ntfy.sh/", "https://ntfy.sh/mon-sujet"),
            ("https://ntfy.sh///", "https://ntfy.sh/mon-sujet"),
            ("http://ntfy.interne:8080", "http://ntfy.interne:8080/mon-sujet"),
        ],
        ids=["no_slash", "trailing_slash", "many_slashes", "self_hosted_with_port"],
    )
    def test_trailing_slashes_never_produce_a_double_slash_url(self, server, expected):
        """Un `//` dans le chemin ferait une 404 côté ntfy : le serveur est une
        valeur de configuration, saisie à la main."""
        assert Notifier(server=server)._endpoint("mon-sujet") == expected

    def test_the_url_is_the_one_actually_posted_to(self, requests_mock):
        requests_mock.post("https://ntfy.interne/mon-sujet", status_code=200)

        assert Notifier(server="https://ntfy.interne/").send("mon-sujet", "T", "M") is True
        assert requests_mock.last_request.url == "https://ntfy.interne/mon-sujet"


# ---------------------------------------------------------------------------
# send() — les trois branches
# ---------------------------------------------------------------------------

class TestSend:
    def test_a_200_is_a_success(self, notifier, ntfy):
        ntfy(status=200)

        assert notifier.send(TOPIC, "Titre", "Message") is True

    @pytest.mark.parametrize("status", [201, 202, 301, 400, 401, 403, 404, 429, 500, 503])
    def test_any_status_other_than_exactly_200_is_a_failure(self, notifier, ntfy, status):
        """La comparaison est `== 200`, pas `resp.ok` : un 201 ou un 202 —
        parfaitement valides en HTTP — sont comptés comme des échecs. Choix
        conservateur (l'annonce sera re-tentée), figé ici parce qu'il décide du
        marquage en base."""
        ntfy(status=status, text="quota dépassé")

        assert notifier.send(TOPIC, "Titre", "Message") is False

    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.SSLError,
            requests.exceptions.TooManyRedirects,
        ],
    )
    def test_every_network_exception_is_caught_and_reported_as_a_failure(self, notifier, ntfy, exc):
        ntfy(exc=exc)

        assert notifier.send(TOPIC, "Titre", "Message") is False

    def test_a_non_request_exception_is_not_caught(self, notifier, ntfy):
        """`except requests.RequestException` seulement : une erreur de
        programmation remonte au lieu d'être silencieusement comptée comme un
        échec réseau."""
        ntfy(exc=MemoryError)

        with pytest.raises(MemoryError):
            notifier.send(TOPIC, "Titre", "Message")

    def test_the_body_is_sent_as_utf8_bytes(self, notifier, ntfy, requests_mock):
        """Le corps, contrairement aux en-têtes, accepte l'UTF-8 : c'est là que
        vivent les accents du message."""
        ntfy()

        notifier.send(TOPIC, "Titre", "Prix : 1 250 €/mois — Séjour exposé sud")

        assert requests_mock.last_request.body == (
            "Prix : 1 250 €/mois — Séjour exposé sud".encode()
        )

    @pytest.mark.parametrize(
        ("instance_priority", "call_priority", "expected"),
        [
            ("default", "", "default"),
            ("default", "high", "high"),
            ("low", "", "low"),
            ("low", "urgent", "urgent"),
        ],
        ids=["instance_default", "call_overrides", "instance_low", "call_overrides_low"],
    )
    def test_the_call_priority_overrides_the_instance_priority(
        self, ntfy, requests_mock, instance_priority, call_priority, expected
    ):
        """`priority or self.priority` : la chaîne vide est le « non précisé »."""
        ntfy()

        Notifier(priority=instance_priority).send(TOPIC, "T", "M", priority=call_priority)

        assert requests_mock.last_request.headers["Priority"] == expected

    @pytest.mark.parametrize(
        ("tags", "expected"),
        [(None, "house"), ("bell", "bell"), ("house,new", "house,new")],
        ids=["default", "single", "multiple"],
    )
    def test_tags_default_to_house(self, notifier, ntfy, requests_mock, tags, expected):
        ntfy()

        if tags is None:
            notifier.send(TOPIC, "T", "M")
        else:
            notifier.send(TOPIC, "T", "M", tags=tags)

        assert requests_mock.last_request.headers["Tags"] == expected

    def test_click_and_action_headers_are_added_together_when_a_url_is_given(
        self, notifier, ntfy, requests_mock
    ):
        ntfy()
        url = "https://www.seloger.com/annonces/213456789.htm"

        notifier.send(TOPIC, "T", "M", url=url)

        headers = requests_mock.last_request.headers
        assert headers["Click"] == url
        assert headers["Actions"] == f"view, Voir l'annonce, {url}"

    @pytest.mark.parametrize("url", ["", None], ids=["empty_string", "none"])
    def test_neither_header_is_added_without_a_url(self, notifier, ntfy, requests_mock, url):
        """`if url` : les deux en-têtes sont solidaires. Un `Actions` sans
        `Click` (ou l'inverse) serait une notification à moitié cliquable."""
        ntfy()

        notifier.send(TOPIC, "T", "M", url=url)

        headers = requests_mock.last_request.headers
        assert "Click" not in headers
        assert "Actions" not in headers

    def test_a_url_is_never_sanitized_unlike_the_title(self, notifier, ntfy, requests_mock):
        """Seul `Title` passe par `_sanitize_header`. Une URL contenant un
        caractère non-ASCII (un accent dans un slug SeLoger) part donc telle
        quelle — et `requests` l'encode en latin-1 côté transport."""
        ntfy()

        notifier.send(TOPIC, "T", "M", url="https://www.seloger.com/annonces/13eme/216.htm?q=séjour")

        assert requests_mock.last_request.headers["Click"].endswith("q=séjour")


# ---------------------------------------------------------------------------
# _sanitize_header
# ---------------------------------------------------------------------------

class TestSanitizeHeader:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Plain ASCII", "Plain ASCII"),
            ("Appartement à Paris", "Appartement ? Paris"),
            ("Nouvelle annonce Laforêt", "Nouvelle annonce Lafor?t"),
            ("65 m² — 1 250 €", "65 m? ? 1 250 ?"),
            ("", ""),
            ("Duplex 東京", "Duplex ??"),
            # Un emoji est hors du BMP : encodé sur 2 unités de substitution en
            # UTF-16, mais un seul caractère Python -> un seul "?".
            ("Coup de cœur 🏡", "Coup de c?ur ?"),
        ],
        ids=["ascii", "single_accent", "circumflex", "symbols", "empty", "cjk", "emoji"],
    )
    def test_non_ascii_becomes_a_question_mark_one_per_character(self, notifier, raw, expected):
        """`errors="replace"` remplace *par caractère source*, pas par octet :
        un « é » (2 octets en UTF-8) donne un seul « ? ». C'est ce qui garde la
        longueur du titre lisible."""
        assert notifier._sanitize_header(raw) == expected

    @pytest.mark.parametrize(
        "control",
        ["\n", "\r", "\r\n", "\t", "\x00", "\x1b"],
        ids=["lf", "cr", "crlf", "tab", "nul", "escape"],
    )
    def test_ascii_control_characters_pass_through_untouched(self, notifier, control):
        """# BUG : `_sanitize_header` ne filtre QUE le non-ASCII (notifier.py:21).

        `\\n`, `\\r`, `\\t` et même `\\x00` sont des caractères ASCII : ils
        traversent la sanitisation intacts et se retrouvent tels quels dans la
        valeur de l'en-tête `Title`. Or ce titre est construit à partir de
        données scrapées chez un tiers (voir TestHeaderInjection pour les
        conséquences réelles).

        Comportement actuel figé ici, non corrigé.
        """
        assert notifier._sanitize_header(f"Titre{control}suite") == f"Titre{control}suite"

    def test_the_sanitized_value_is_always_latin1_encodable(self, notifier):
        """La raison d'être de la fonction : `requests` encode les en-têtes en
        latin-1 et lèverait sur un caractère hors de cette plage."""
        sanitized = notifier._sanitize_header("Appartement 3 pièces — 65 m² 🏡")

        sanitized.encode("latin-1")  # ne doit pas lever
        assert sanitized.isascii()


class TestHeaderInjection:
    """Sécurité : ce que produit un titre d'annonce contenant un retour ligne.

    Les titres viennent de SeLoger et de Laforêt, c'est-à-dire de texte rédigé
    par des agences, potentiellement par n'importe qui déposant une annonce.
    """

    @pytest.mark.parametrize(
        ("payload", "case"),
        [
            ("T2 lumineux\nX-Injected: 1", "en-tête supplémentaire"),
            ("T2 lumineux\r\n\r\nfaux corps de requête", "séparation en-têtes/corps"),
            ("T2 lumineux\rPriority: urgent", "surcharge de priorité par CR seul"),
        ],
        ids=["extra_header", "body_split", "priority_override"],
    )
    def test_a_newline_in_the_title_makes_the_notification_fail_forever(
        self, notifier, ntfy, requests_mock, payload, case
    ):
        """# BUG : injection CRLF dans l'en-tête `Title` (notifier.py:21 et 45).

        Le titre n'est pas débarrassé de ses `\\r`/`\\n` avant d'être posé en
        en-tête HTTP. Deux constats, l'un rassurant et l'autre pas :

        1. `requests` >= 2.11 valide les valeurs d'en-tête dans
           `prepare_headers` et refuse les caractères de retour : **aucune
           requête ne part sur le réseau**, l'injection n'atteint donc pas
           ntfy. La tentative échoue avant le transport.
        2. Mais `requests.exceptions.InvalidHeader` **hérite de
           `RequestException`** : elle est donc avalée par le `except` de
           `send()`, qui la journalise en « erreur réseau » et renvoie `False`.
           Combiné au contrat de `ScrapeService` (une annonce n'est marquée
           notifiée que sur un `True`), cela crée une **boucle de retry
           perpétuelle** : à chaque cycle de scrape — toutes les 5 minutes par
           défaut — la même annonce est re-tentée, re-échoue, et le log annonce
           un problème réseau qui n'existe pas. Un seul titre malformé chez
           l'agence suffit à polluer les logs indéfiniment.

        La protection ne vient donc pas de ce module mais de `requests`, et le
        diagnostic produit est faux. Comportement actuel figé ici, non corrigé.
        """
        ntfy()

        assert notifier.send(TOPIC, payload, "Message") is False, case
        assert requests_mock.call_count == 0, (
            "requests refuse la valeur avant le transport : rien n'atteint ntfy"
        )

    def test_the_failure_is_indistinguishable_from_a_real_network_outage(self, notifier, ntfy):
        """Même valeur de retour, même branche de log qu'une coupure réseau :
        rien ne permet à l'appelant de distinguer « titre malformé, réessayer ne
        servira jamais à rien » de « ntfy est momentanément injoignable »."""
        ntfy(exc=requests.exceptions.ConnectionError)
        outage = notifier.send(TOPIC, "Titre sain", "Message")

        ntfy()
        malformed = notifier.send(TOPIC, "Titre\nmalformé", "Message")

        assert outage is malformed is False

    @pytest.mark.parametrize(
        ("payload", "expected_on_the_wire"),
        [("Titre\x00nul", "Titre\x00nul"), ("Titre\tTab", "Titre\tTab")],
        ids=["nul", "tab"],
    )
    def test_nul_and_tab_do_reach_the_wire(
        self, notifier, ntfy, requests_mock, payload, expected_on_the_wire
    ):
        """# BUG : `\\x00` et `\\t` ne sont filtrés par personne (notifier.py:21).

        Contrairement à `\\r`/`\\n`, ces deux caractères passent la validation
        de `requests` et partent réellement dans l'en-tête `Title`. L'octet nul
        dans une valeur d'en-tête est un classique de la désynchronisation de
        parseurs (un proxy intermédiaire peut le traiter comme une fin de
        chaîne, le serveur suivant non). L'impact dépend d'un tiers, mais
        l'origine est ici : la sanitisation ne regarde que la plage ASCII, pas
        les caractères de contrôle.

        Comportement actuel figé ici, non corrigé.
        """
        ntfy()

        assert notifier.send(TOPIC, payload, "Message") is True
        assert requests_mock.last_request.headers["Title"] == expected_on_the_wire


# ---------------------------------------------------------------------------
# _source_label
# ---------------------------------------------------------------------------

class TestSourceLabel:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("seloger", "SeLoger"),
            ("laforet", "Laforêt"),
            ("", ""),
            ("futuresource", "futuresource"),
            ("SELOGER", "SELOGER"),
        ],
        ids=["seloger", "laforet", "empty", "unregistered", "wrong_case"],
    )
    def test_the_label_comes_from_the_parser_registry_with_a_raw_fallback(
        self, notifier, source, expected
    ):
        """Le titre affichait « SeLoger » en dur, y compris pour une annonce
        Laforêt (constaté en production). Le nom vient maintenant du parser de
        la source, avec repli sur la chaîne brute — jamais de crash pour une
        source pas encore enregistrée, et jamais de mensonge sur l'origine.

        `SELOGER` illustre le repli : la recherche est sensible à la casse.
        """
        assert notifier._source_label(source) == expected

    def test_an_unregistered_source_never_raises(self, notifier, monkeypatch):
        """La `ValueError` du registry est rattrapée. Toute AUTRE exception
        remonterait — ce chemin n'est pas défensif au-delà du cas prévu."""
        import parsers

        monkeypatch.setattr(parsers, "get_parser", lambda s, storage=None: (_ for _ in ()).throw(RuntimeError("boum")))

        with pytest.raises(RuntimeError, match="boum"):
            notifier._source_label("seloger")


# ---------------------------------------------------------------------------
# notify_new_listing
# ---------------------------------------------------------------------------

class TestNotifyNewListing:
    def test_the_message_lists_every_populated_field_in_a_fixed_order(
        self, notifier, ntfy, requests_mock
    ):
        """L'ordre est celui du code (agence, prix, surface, pièces, lieu) : une
        notification lue sur un téléphone doit toujours avoir la même forme."""
        ntfy()
        listing = make_listing(
            agency="Agence Beauséjour", price="1 250 €/mois", surface="65",
            rooms="3", location="Paris 13e",
        )

        notifier.notify_new_listing(TOPIC, listing)

        assert requests_mock.last_request.body.decode() == (
            "Agence: Agence Beauséjour\n"
            "Prix: 1 250 €/mois\n"
            "Surface: 65\n"
            "Pieces: 3\n"
            "Lieu: Paris 13e"
        )

    @pytest.mark.parametrize(
        ("populated", "expected_lines"),
        [
            ({"price": "900 €"}, ["Prix: 900 €"]),
            ({"agency": "Foncia"}, ["Agence: Foncia"]),
            ({"surface": "42"}, ["Surface: 42"]),
            ({"rooms": "2"}, ["Pieces: 2"]),
            ({"location": "Lyon 7e"}, ["Lieu: Lyon 7e"]),
            ({"price": "900 €", "location": "Lyon 7e"}, ["Prix: 900 €", "Lieu: Lyon 7e"]),
        ],
        ids=["price", "agency", "surface", "rooms", "location", "two_fields"],
    )
    def test_every_field_is_optional_and_absent_ones_leave_no_trace(
        self, notifier, ntfy, requests_mock, populated, expected_lines
    ):
        """Aucune ligne vide, aucun « Prix:  » orphelin : les champs manquants
        sont fréquents (un particulier ne renseigne pas d'agence)."""
        ntfy()
        blank = dict.fromkeys(("agency", "price", "surface", "rooms", "location"), "")
        listing = make_listing(**{**blank, **populated})

        notifier.notify_new_listing(TOPIC, listing)

        assert requests_mock.last_request.body.decode().split("\n") == expected_lines

    def test_a_listing_with_nothing_but_an_id_still_produces_a_readable_message(
        self, notifier, ntfy, requests_mock
    ):
        """Le corps d'une requête ntfy ne peut pas être vide (le message
        disparaîtrait) : un défaut explicite est indispensable."""
        ntfy()

        notifier.notify_new_listing(TOPIC, Listing(listing_id="sl_1", url="https://x/1.htm"))

        assert requests_mock.last_request.body.decode() == "Nouvelle annonce disponible"

    @pytest.mark.parametrize(
        ("source", "expected_title"),
        [
            ("seloger", "Nouvelle annonce SeLoger"),
            ("laforet", "Nouvelle annonce Lafor?t"),
            ("futuresource", "Nouvelle annonce futuresource"),
            ("", "Nouvelle annonce"),
        ],
        ids=["seloger", "laforet_accent_replaced", "unregistered", "no_source"],
    )
    def test_the_title_names_the_real_source_and_never_ends_with_a_space(
        self, notifier, ntfy, requests_mock, source, expected_title
    ):
        """Le `.rstrip()` est ce qui évite « Nouvelle annonce  » (espace
        traînant) quand la source est vide — visible tel quel sur le téléphone.
        Et « Laforêt » devient « Lafor?t » : conséquence assumée de
        `_sanitize_header`, les en-têtes HTTP n'acceptant pas l'UTF-8.
        """
        ntfy()

        notifier.notify_new_listing(TOPIC, make_listing(source=source))

        assert requests_mock.last_request.headers["Title"] == expected_title

    def test_priority_high_and_the_house_new_tags_are_forced(self, notifier, ntfy, requests_mock):
        """Une nouvelle annonce doit sonner : la priorité de l'instance
        (`default`, ou `low` sur une instance mal configurée) est écrasée."""
        ntfy()

        Notifier(priority="low").notify_new_listing(TOPIC, make_listing())

        assert requests_mock.last_request.headers["Priority"] == "high"
        assert requests_mock.last_request.headers["Tags"] == "house,new"

    def test_the_listing_url_becomes_the_click_target(self, notifier, ntfy, requests_mock):
        ntfy()
        listing = make_listing(listing_id="sl_42")

        notifier.notify_new_listing(TOPIC, listing)

        assert requests_mock.last_request.headers["Click"] == listing.url
        assert listing.listing_id in requests_mock.last_request.headers["Actions"]

    def test_a_listing_without_a_url_gets_no_click_header(self, notifier, ntfy, requests_mock):
        ntfy()

        notifier.notify_new_listing(TOPIC, Listing(listing_id="sl_1", url=""))

        assert "Click" not in requests_mock.last_request.headers

    @pytest.mark.parametrize(
        ("status", "expected"), [(200, True), (500, False)], ids=["sent", "rejected"]
    )
    def test_the_return_value_is_the_marking_contract_with_scrape_service(
        self, notifier, ntfy, status, expected
    ):
        """`ScrapeService` n'appelle `mark_listings_notified` que sur un `True`.
        Ce booléen est donc la frontière entre « annonce traitée » et « annonce
        re-tentée au prochain cycle » : il doit refléter fidèlement l'envoi."""
        ntfy(status=status)

        assert notifier.notify_new_listing(TOPIC, make_listing()) is expected

    def test_a_title_carrying_a_newline_from_the_source_is_never_delivered(
        self, notifier, ntfy, requests_mock
    ):
        """Le chemin complet du bug d'injection : le titre vient du scrape.
        Ici c'est `source` qui porte le retour ligne (une source est libre en
        base), mais un titre d'annonce ferait la même chose sur les notifications
        qui l'utilisent."""
        ntfy()

        result = notifier.notify_new_listing(TOPIC, make_listing(source="seloger\nX-Evil: 1"))

        assert result is False
        assert requests_mock.call_count == 0


# ---------------------------------------------------------------------------
# notify_summary
# ---------------------------------------------------------------------------

class TestNotifySummary:
    def test_zero_new_listings_short_circuits_before_any_network_call(
        self, notifier, ntfy, requests_mock
    ):
        """Anti-spam : le scheduler tourne toutes les 30 s et la plupart des
        cycles ne trouvent rien. Le `True` renvoyé signifie « rien à faire »,
        pas « envoyé »."""
        ntfy()

        assert notifier.notify_summary(TOPIC, new_count=0, total_scanned=30) is True
        assert requests_mock.call_count == 0

    @pytest.mark.parametrize(
        ("new_count", "expected_title", "expected_message"),
        [
            (1, "1 nouvelle annonce", "30 annonces scannees, 1 nouvelle"),
            (2, "2 nouvelles annonces", "30 annonces scannees, 2 nouvelles"),
            (30, "30 nouvelles annonces", "30 annonces scannees, 30 nouvelles"),
        ],
        ids=["singular", "plural_two", "plural_many"],
    )
    def test_pluralisation_is_consistent_across_the_three_places_it_appears(
        self, notifier, ntfy, requests_mock, new_count, expected_title, expected_message
    ):
        """Trois `{'s' if new_count > 1 else ''}` indépendants (deux dans le
        titre, un dans le message) : ils doivent s'accorder entre eux."""
        ntfy()

        notifier.notify_summary(TOPIC, new_count=new_count, total_scanned=30)

        assert requests_mock.last_request.headers["Title"] == expected_title
        assert requests_mock.last_request.body.decode() == expected_message

    @pytest.mark.parametrize("new_count", [-1, -10], ids=["minus_one", "minus_ten"])
    def test_a_negative_count_is_sent_with_singular_wording(
        self, notifier, ntfy, requests_mock, new_count
    ):
        """`new_count == 0` est le seul court-circuit : un compteur négatif
        (impossible aujourd'hui, mais rien ne l'interdit) partirait bel et bien,
        au singulier. Comportement figé pour documenter la garde manquante."""
        ntfy()

        assert notifier.notify_summary(TOPIC, new_count=new_count, total_scanned=5) is True
        assert requests_mock.last_request.headers["Title"] == f"{new_count} nouvelle annonce"

    def test_the_search_url_is_optional_and_becomes_the_click_target(
        self, notifier, ntfy, requests_mock
    ):
        ntfy()

        notifier.notify_summary(TOPIC, new_count=2, total_scanned=30, search_url="https://x/search")
        with_url = requests_mock.last_request.headers
        assert with_url["Click"] == "https://x/search"

        notifier.notify_summary(TOPIC, new_count=2, total_scanned=30)
        assert "Click" not in requests_mock.last_request.headers

    def test_the_summary_uses_the_bell_tag_and_the_instance_priority(
        self, ntfy, requests_mock
    ):
        """Contrairement à `notify_new_listing`, le résumé ne force PAS la
        priorité : c'est une information, pas une alerte."""
        ntfy()

        Notifier(priority="low").notify_summary(TOPIC, new_count=2, total_scanned=30)

        assert requests_mock.last_request.headers["Tags"] == "bell"
        assert requests_mock.last_request.headers["Priority"] == "low"


# ---------------------------------------------------------------------------
# send_test
# ---------------------------------------------------------------------------

class TestSendTest:
    def test_the_configuration_check_is_low_priority_and_self_describing(
        self, notifier, ntfy, requests_mock
    ):
        """Envoyé depuis l'interface pour valider un topic : il ne doit pas
        réveiller l'utilisateur, et son contenu doit être reconnaissable."""
        ntfy()

        assert notifier.send_test(TOPIC) is True

        headers = requests_mock.last_request.headers
        assert headers["Title"] == "Appart Scraper - Test"
        assert headers["Priority"] == "low"
        assert headers["Tags"] == "white_check_mark"
        assert "Click" not in headers, "aucune URL : rien à ouvrir pour un test"
        assert requests_mock.last_request.body.decode() == (
            "Les notifications fonctionnent ! Le scraper est pret."
        )

    @pytest.mark.parametrize(
        ("status", "expected"), [(200, True), (404, False)], ids=["ok", "unknown_topic"]
    )
    def test_the_result_tells_the_user_whether_the_topic_works(
        self, notifier, ntfy, status, expected
    ):
        ntfy(status=status)

        assert notifier.send_test(TOPIC) is expected
