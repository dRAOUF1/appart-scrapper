"""Tests unitaires des constructeurs de requêtes de `repositories/listing_repo.py`.

C'est la principale surface d'injection SQL du projet : `_build_filter_clauses`
et `_build_order_clause` assemblent des morceaux de requête à partir de
`request.args`, et n'avaient aucun test. Les deux sont *pures* — elles
retournent une chaîne et remplissent une liste de paramètres — donc entièrement
testables sans base.

Deux invariants sont traités comme des contrats de sécurité, et sont les seuls
endroits où l'on assère du SQL littéral :

    1. aucune valeur venant de l'utilisateur n'apparaît dans la chaîne SQL —
       tout passe par `params` (test_no_user_value_ever_reaches_the_sql_string) ;
    2. `sort` est validé par allowlist, jamais interpolé
       (TestBuildOrderClause.test_hostile_values_fall_back_to_the_default).

Tout le reste vérifie la *présence des fragments porteurs de sens* et la
correspondance placeholders/paramètres, jamais la mise en forme du SQL : ce
dernier point est validé contre un vrai Postgres dans tests/integration/.
"""

from __future__ import annotations

import re

import pytest

from repositories.listing_repo import ListingRepository, _clean_string
from tests.helpers.factories import make_listing
from tests.helpers.fakes import RecordingConnection, bind_repository

FAKE_URL = "postgresql://fake/fake"

# Charges hostiles réutilisées partout : la première casse la requête si elle
# est concaténée, la seconde la neutralise.
PAYLOADS = ["; DROP TABLE listings; --", "' OR 1=1 --", "1); DELETE FROM users; --", "%'; --"]


@pytest.fixture
def repo():
    """Un repository non branché : les constructeurs de requêtes ne touchent
    ni au pool ni à une connexion."""
    return ListingRepository(FAKE_URL)


def count_placeholders(sql: str) -> int:
    """Nombre de `%s` dans une clause. `%%` n'apparaît nulle part dans ce repo."""
    return sql.count("%s")


# ---------------------------------------------------------------------------
# _build_filter_clauses — les 18 filtres, un par un
# ---------------------------------------------------------------------------

SURFACE_CAST = "CAST(NULLIF(REGEXP_REPLACE(l.surface, '[^0-9.]', '', 'g'), '') AS NUMERIC)"
ROOMS_CAST = "CAST(NULLIF(REGEXP_REPLACE(l.rooms, '[^0-9.]', '', 'g'), '') AS NUMERIC)"

FILTER_CASES = [
    pytest.param(
        {"q": "duplex"},
        [
            "l.title ILIKE %s",
            "l.location ILIKE %s",
            "l.agency ILIKE %s",
            "l.description ILIKE %s",
        ],
        ["%duplex%"] * 4,
        id="q",
    ),
    pytest.param({"price_min": 800}, ["l.price_value >= %s"], [800], id="price_min"),
    pytest.param({"price_max": 1500}, ["l.price_value <= %s"], [1500], id="price_max"),
    # surface et rooms sont des colonnes TEXT ("65 m²", "3 pièces") : la
    # comparaison numérique passe forcément par un nettoyage + CAST.
    pytest.param({"surface_min": 40}, [f"{SURFACE_CAST} >= %s"], [40], id="surface_min"),
    pytest.param({"surface_max": 90}, [f"{SURFACE_CAST} <= %s"], [90], id="surface_max"),
    pytest.param({"rooms_min": 2}, [f"{ROOMS_CAST} >= %s"], [2], id="rooms_min"),
    pytest.param({"rooms_max": 4}, [f"{ROOMS_CAST} <= %s"], [4], id="rooms_max"),
    pytest.param({"city": "Paris"}, ["l.city ILIKE %s"], ["%Paris%"], id="city"),
    pytest.param({"district": "13e"}, ["l.district ILIKE %s"], ["%13e%"], id="district"),
    pytest.param({"zip_code": "75013"}, ["l.zip_code = %s"], ["75013"], id="zip_code"),
    pytest.param({"property_type": "apartment"}, ["l.property_type = %s"], ["apartment"], id="property_type"),
    pytest.param({"agency": "Foncia"}, ["l.agency = %s"], ["Foncia"], id="agency"),
    pytest.param({"epc": "C"}, ["l.epc = %s"], ["C"], id="epc"),
    pytest.param({"ges": "B"}, ["l.ges = %s"], ["B"], id="ges"),
    pytest.param({"is_private": True}, ["l.is_private = %s"], [True], id="is_private_true"),
    pytest.param({"is_private": False}, ["l.is_private = %s"], [False], id="is_private_false"),
    pytest.param({"is_new": True}, ["l.is_new = %s"], [True], id="is_new_true"),
    pytest.param({"is_new": False}, ["l.is_new = %s"], [False], id="is_new_false"),
    pytest.param({"date_min": "2026-01-01"}, ["l.creation_date >= %s"], ["2026-01-01"], id="date_min"),
    pytest.param(
        {"blacklisted_agencies": ["Foncia", "Nexity"]},
        ["l.agency NOT IN (%s,%s)"],
        ["Foncia", "Nexity"],
        id="blacklisted_agencies",
    ),
]

# Filtres testés par *vérité* : une valeur falsy est ignorée.
TRUTHY_ONLY_FILTERS = {
    "q": "",
    "city": "",
    "district": "",
    "zip_code": "",
    "property_type": "",
    "agency": "",
    "epc": "",
    "ges": "",
    "date_min": "",
    "blacklisted_agencies": [],
}

# Filtres testés par `is not None` : une valeur falsy est un filtre valide.
NOT_NONE_FILTERS = {
    "price_min": 0,
    "price_max": 0,
    "surface_min": 0,
    "surface_max": 0,
    "rooms_min": 0,
    "rooms_max": 0,
    "is_private": False,
    "is_new": False,
}


class TestBuildFilterClauses:
    @pytest.mark.parametrize(("filters", "fragments", "expected_params"), FILTER_CASES)
    def test_each_filter_emits_its_clause_and_its_params(self, repo, filters, fragments, expected_params):
        params: list = []

        clause = repo._build_filter_clauses(filters, params)

        for fragment in fragments:
            assert fragment in clause
        assert params == expected_params

    @pytest.mark.parametrize(("filters", "fragments", "expected_params"), FILTER_CASES)
    def test_every_filter_keeps_placeholders_and_params_in_sync(self, repo, filters, fragments, expected_params):
        """Un `%s` de plus que de paramètres et psycopg2 lève « not enough
        arguments » ; un de moins, et il lève « not all arguments converted ».
        Dans les deux cas la page casse — l'invariant se vérifie sans base."""
        params: list = []

        clause = repo._build_filter_clauses(filters, params)

        assert count_placeholders(clause) == len(params)

    def test_no_filter_yields_an_empty_string(self, repo):
        """L'appelant n'ajoute ` AND ` que si la chaîne est non vide : retourner
        autre chose que `""` produirait un `WHERE ... AND ` bancal."""
        params: list = []

        assert repo._build_filter_clauses({}, params) == ""
        assert params == []

    def test_unknown_keys_are_ignored(self, repo):
        """Les filtres viennent de `request.args` : une clé inconnue (ou forgée)
        ne doit rien produire, pas lever."""
        params: list = []

        clause = repo._build_filter_clauses({"nimporte_quoi": "x", "l.agency": "y", "1=1": "z"}, params)

        assert clause == ""
        assert params == []

    def test_the_search_clause_is_parenthesized(self, repo):
        """Sans les parenthèses autour des OR, `AND a OR b OR c` élargirait
        silencieusement le résultat (précédence : OR est moins prioritaire), et
        un filtre de prix ou de blacklist deviendrait inopérant."""
        params: list = []

        clause = repo._build_filter_clauses({"q": "loft"}, params)

        assert clause.startswith("(")
        assert clause.endswith(")")
        assert clause.count(" OR ") == 3
        assert " AND " not in clause
        # L'ordre des colonnes fixe l'ordre des 4 paramètres.
        assert re.findall(r"l\.(\w+) ILIKE", clause) == ["title", "location", "agency", "description"]

    @pytest.mark.parametrize(("key", "falsy_value"), sorted(TRUTHY_ONLY_FILTERS.items()))
    def test_falsy_values_are_ignored_for_truthiness_based_filters(self, repo, key, falsy_value):
        """Ces filtres sont testés par vérité : une chaîne vide (le cas normal
        d'un champ de formulaire non rempli) ne doit pas produire de clause."""
        params: list = []

        assert repo._build_filter_clauses({key: falsy_value}, params) == ""
        assert params == []

    @pytest.mark.parametrize(("key", "falsy_value"), sorted(NOT_NONE_FILTERS.items()))
    def test_falsy_values_are_kept_for_is_not_none_filters(self, repo, key, falsy_value):
        """Asymétrie délibérée et facile à casser : ces filtres utilisent
        `is not None`, donc `0` et `False` sont des valeurs *significatives*
        (« annonces déjà vues », « prix minimum nul »). Les basculer sur un test
        de vérité ferait disparaître le filtre `is_private=False` sans un mot.
        """
        params: list = []

        clause = repo._build_filter_clauses({key: falsy_value}, params)

        assert clause != ""
        assert params == [falsy_value]

    @pytest.mark.parametrize("key", sorted(NOT_NONE_FILTERS))
    def test_none_is_ignored_for_is_not_none_filters(self, repo, key):
        params: list = []

        assert repo._build_filter_clauses({key: None}, params) == ""
        assert params == []

    @pytest.mark.parametrize("agencies", [[], ["A"], ["A", "B"], ["A", "B", "C", "D", "E"]])
    def test_blacklist_generates_exactly_one_placeholder_per_agency(self, repo, agencies):
        """Le `NOT IN (...)` est le seul endroit où le nombre de placeholders est
        calculé à la main. Une divergence ici n'est pas une erreur de syntaxe
        mais un décalage de paramètres : le filtre porterait sur les mauvaises
        valeurs, ou la requête entière échouerait."""
        params: list = []

        clause = repo._build_filter_clauses({"blacklisted_agencies": agencies}, params)

        if not agencies:
            # `NOT IN ()` n'est pas du SQL valide : une liste vide doit disparaître.
            assert clause == ""
            assert params == []
        else:
            assert f"l.agency NOT IN ({','.join(['%s'] * len(agencies))})" in clause
            assert count_placeholders(clause) == len(agencies)
            assert params == agencies

    @pytest.mark.parametrize("prefix", ["l.", "", "listings.", "sub."])
    def test_the_prefix_is_applied_to_every_column(self, repo, prefix):
        """Le préfixe qualifie les colonnes dans une requête jointe. Une colonne
        oubliée donnerait « column reference is ambiguous » — mais seulement pour
        la combinaison de filtres qui la déclenche."""
        params: list = []
        filters = {
            "q": "x", "price_min": 1, "price_max": 2, "surface_min": 3, "surface_max": 4,
            "rooms_min": 5, "rooms_max": 6, "city": "c", "district": "d", "zip_code": "z",
            "property_type": "p", "agency": "a", "epc": "e", "ges": "g",
            "is_private": True, "is_new": True, "date_min": "2026-01-01",
            "blacklisted_agencies": ["b"],
        }

        clause = repo._build_filter_clauses(filters, params, prefix=prefix)

        for column in (
            "title", "location", "agency", "description", "price_value", "surface", "rooms",
            "city", "district", "zip_code", "property_type", "epc", "ges", "is_private",
            "is_new", "creation_date",
        ):
            assert f"{prefix}{column}" in clause, f"colonne {column} non préfixée"
        if prefix:
            # Aucune colonne nue ne subsiste : chaque occurrence est préfixée.
            assert not re.search(r"(?<![\w.])price_value", clause)

    def test_clauses_are_and_joined_and_params_follow_the_clause_order(self, repo):
        """Les paramètres sont positionnels : leur ordre doit suivre exactement
        celui des clauses, sinon un prix est comparé à une ville."""
        params: list = []
        filters = {
            "q": "loft", "price_min": 800, "price_max": 1500, "surface_min": 40, "surface_max": 90,
            "rooms_min": 2, "rooms_max": 4, "city": "Paris", "district": "13e", "zip_code": "75013",
            "property_type": "apartment", "agency": "Foncia", "epc": "C", "ges": "B",
            "is_private": False, "is_new": True, "date_min": "2026-01-01",
            "blacklisted_agencies": ["Nexity", "Orpi"],
        }

        clause = repo._build_filter_clauses(filters, params)

        assert params == [
            "%loft%", "%loft%", "%loft%", "%loft%",
            800, 1500, 40, 90, 2, 4,
            "%Paris%", "%13e%", "75013", "apartment", "Foncia", "C", "B",
            False, True, "2026-01-01", "Nexity", "Orpi",
        ]
        assert count_placeholders(clause) == len(params)
        # 18 filtres actifs, dont un (`q`) produit une clause unique parenthésée.
        assert len(clause.split(" AND ")) == 18

    def test_preexisting_params_are_preserved(self, repo):
        """Les appelants amorcent `params = [search_id]` : le constructeur doit
        *ajouter*, pas remplacer."""
        params: list = [42]

        repo._build_filter_clauses({"city": "Paris"}, params)

        assert params == [42, "%Paris%"]

    @pytest.mark.parametrize("payload", PAYLOADS)
    def test_no_user_value_ever_reaches_the_sql_string(self, repo, payload):
        """🔒 L'invariant central. Chaque filtre reçoit une charge hostile ;
        aucune ne doit se retrouver dans la chaîne renvoyée — elles doivent
        toutes atterrir dans `params`, où psycopg2 les échappera.
        """
        params: list = []
        filters = {
            "q": payload, "city": payload, "district": payload, "zip_code": payload,
            "property_type": payload, "agency": payload, "epc": payload, "ges": payload,
            "date_min": payload, "price_min": payload, "price_max": payload,
            "surface_min": payload, "surface_max": payload, "rooms_min": payload,
            "rooms_max": payload, "is_private": payload, "is_new": payload,
            "blacklisted_agencies": [payload, payload],
        }

        clause = repo._build_filter_clauses(filters, params)

        assert payload not in clause
        for fragment in ("DROP", "DELETE", "--", "1=1"):
            assert fragment not in clause
        # La valeur n'est pas perdue pour autant : elle est bien paramétrée.
        assert payload in params
        assert count_placeholders(clause) == len(params)


# ---------------------------------------------------------------------------
# _build_order_clause — allowlist, jamais d'interpolation
# ---------------------------------------------------------------------------

SORT_MAP = {
    "found_at_desc": "sl.found_at DESC",
    "found_at_asc": "sl.found_at ASC",
    "price_asc": "l.price_value ASC NULLS LAST",
    "price_desc": "l.price_value DESC NULLS LAST",
    "surface_asc": "CAST(NULLIF(REGEXP_REPLACE(l.surface, '[^0-9.]', '', 'g'), '') AS NUMERIC) ASC NULLS LAST",
    "surface_desc": "CAST(NULLIF(REGEXP_REPLACE(l.surface, '[^0-9.]', '', 'g'), '') AS NUMERIC) DESC NULLS LAST",
    "date_desc": "l.creation_date DESC NULLS LAST",
    "date_asc": "l.creation_date ASC NULLS LAST",
}

DEFAULT_ORDER = "sl.found_at DESC"


class TestBuildOrderClause:
    @pytest.mark.parametrize(("sort", "expected"), sorted(SORT_MAP.items()))
    def test_every_allowed_sort_maps_to_its_clause(self, sort, expected):
        assert ListingRepository(FAKE_URL)._build_order_clause(sort) == expected

    def test_the_allowlist_has_exactly_eight_entries(self, repo):
        """Fige la taille de l'allowlist : ajouter un tri sans l'ajouter ici (et
        au formulaire) passerait inaperçu, et *retirer* une entrée ferait
        silencieusement retomber les URLs existantes sur le tri par défaut."""
        accepted = {sort for sort in SORT_MAP if repo._build_order_clause(sort) != DEFAULT_ORDER}
        # `found_at_desc` est le défaut : il est accepté sans être distinguable.
        assert accepted == set(SORT_MAP) - {"found_at_desc"}
        assert len(SORT_MAP) == 8

    def test_the_default_is_the_most_recent_first(self, repo):
        """Le défaut porte sur `sl.found_at` (date de découverte pour CETTE
        recherche), pas sur une colonne de `listings` : c'est ce qui met en haut
        ce que l'utilisateur n'a pas encore vu."""
        assert repo._build_order_clause() == DEFAULT_ORDER

    @pytest.mark.parametrize(
        "hostile",
        [
            "; DROP TABLE listings",
            "price_value; --",
            "l.price_value ASC; DELETE FROM users",
            "price_asc, (SELECT username FROM users)",
            "found_at_desc UNION SELECT 1",
            "PRICE_ASC",
            "price_asc ",
            " price_asc",
            "",
            None,
            0,
            "inconnu",
        ],
    )
    def test_hostile_values_fall_back_to_the_default(self, repo, hostile):
        """🔒 `sort` arrive brut depuis `request.args` et est interpolé en
        f-string dans `ORDER BY {…}` : l'allowlist est l'unique défense. Toute
        valeur non exactement présente dans la table doit rendre le défaut, et
        la valeur brute ne doit apparaître nulle part dans la clause.
        """
        clause = repo._build_order_clause(hostile)

        assert clause == DEFAULT_ORDER
        if isinstance(hostile, str) and hostile.strip():
            assert hostile not in clause

    def test_an_unhashable_sort_raises_instead_of_defaulting(self, repo):
        """`sort_map.get(sort)` suppose `sort` hachable. Une liste lève
        `TypeError` au lieu de retomber sur le défaut.

        Non exploitable aujourd'hui : toutes les routes lisent `sort` via
        `request.args.get`, qui rend une `str` ou `None`. Le test fige le
        comportement actuel pour qu'un futur endpoint JSON (où un client peut
        envoyer `{"sort": []}`) ne découvre pas le 500 en production.
        """
        with pytest.raises(TypeError, match="unhashable"):
            repo._build_order_clause([])

    def test_the_clause_is_taken_from_a_closed_set_of_literals(self, repo):
        """Vu autrement : la clause renvoyée est toujours l'une des 8 constantes
        du code, jamais une composition. Robuste à toute charge hostile future."""
        allowed = set(SORT_MAP.values())

        for candidate in [*SORT_MAP, "inconnu", "", None, "; DROP TABLE listings"]:
            assert repo._build_order_clause(candidate) in allowed


# ---------------------------------------------------------------------------
# _clean_string — les surrogates cassent l'encodage vers Postgres
# ---------------------------------------------------------------------------

class TestCleanString:
    def test_none_stays_none(self):
        """Une colonne nullable doit rester NULL, pas devenir la chaîne vide."""
        assert _clean_string(None) is None

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Appartement 3 pièces",
            "Loft à Saint-Étienne — 65 m²",
            "prix : 1 200 €/mois",
            "emoji 🏠 dans un titre",
            "guillemets \" et ' et \\ antislash",
        ],
    )
    def test_valid_text_is_returned_unchanged(self, text):
        """Le nettoyage ne doit rien abîmer : accents, symboles, emoji et
        caractères qui *ressemblent* à de l'injection passent intacts (c'est
        psycopg2 qui échappe, pas cette fonction)."""
        assert _clean_string(text) == text

    # Construits par `chr()` : écrits en littéraux, ruff les prend à tort pour
    # des doublons (PT014) alors que ce sont trois points de code distincts.
    @pytest.mark.parametrize("code_point", [0xD800, 0xDFFF, 0xDCFF])
    def test_a_lone_surrogate_is_replaced_instead_of_raising(self, code_point):
        surrogate = chr(code_point)
        """Les parsers récupèrent parfois des demi-paires de surrogates depuis du
        HTML mal encodé. Non nettoyées, elles font lever l'INSERT entier
        (`UnicodeEncodeError`) et tout le lot d'annonces est perdu.
        Le double aller-retour `surrogatepass` → `replace` les remplace par U+FFFD.
        """
        cleaned = _clean_string(surrogate)

        assert surrogate not in cleaned
        assert "�" in cleaned
        # La chaîne nettoyée est encodable, ce qui est tout l'objectif.
        assert cleaned.encode("utf-8")

    def test_surrogates_are_replaced_without_touching_the_rest(self):
        cleaned = _clean_string("Studio \ud800 Paris")

        assert cleaned.startswith("Studio ")
        assert cleaned.endswith(" Paris")
        assert "�" in cleaned
        assert cleaned.encode("utf-8")


# ---------------------------------------------------------------------------
# save_and_link / mark_listings_notified — les branches sans base
# ---------------------------------------------------------------------------

class FakeExecuteValues:
    """Double de `psycopg2.extras.execute_values`.

    Le vrai appelle `cur.mogrify`, que la fausse connexion n'implémente pas — et
    ce n'est pas ce qu'on teste ici : on veut voir *quels tuples* sont envoyés
    et avec quelles options. `newly_linked` pilote ce que renvoie l'appel
    `fetch=True`, c'est-à-dire les liens réellement insérés par Postgres.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.newly_linked: set[str] | None = None

    def __call__(self, cur, sql, argslist, page_size=100, fetch=False, **kwargs):
        rows = list(argslist)
        self.calls.append({"sql": sql, "rows": rows, "page_size": page_size, "fetch": fetch})
        if not fetch:
            return None
        ids = [row[1] for row in rows]
        if self.newly_linked is not None:
            ids = [listing_id for listing_id in ids if listing_id in self.newly_linked]
        return [(listing_id,) for listing_id in ids]


@pytest.fixture
def execute_values(monkeypatch):
    fake = FakeExecuteValues()
    monkeypatch.setattr("repositories.listing_repo.execute_values", fake)
    return fake


class TestSaveAndLink:
    def test_an_empty_batch_touches_neither_pool_nor_database(self, execute_values):
        """Le scraper appelle `save_and_link` à chaque tour, y compris quand il
        n'a rien trouvé : le court-circuit évite un emprunt de connexion (et un
        `execute_values` avec une liste vide, qui lèverait)."""
        conn = RecordingConnection()
        repo = bind_repository(ListingRepository, conn)

        assert repo.save_and_link([], search_id=1) == ([], [])
        assert conn.executed == []
        assert execute_values.calls == []
        assert conn.commits == 0

    def test_every_link_is_written_with_notified_false(self, execute_values):
        """La colonne `notified` a un `DEFAULT TRUE` (hérité du backfill qui a
        marqué l'existant comme déjà notifié). Ne pas la préciser ferait
        considérer chaque nouvelle annonce comme déjà envoyée : plus aucune
        notification, et silencieusement.
        """
        conn = RecordingConnection()
        repo = bind_repository(ListingRepository, conn)
        listings = [make_listing(), make_listing(), make_listing()]

        repo.save_and_link(listings, search_id=7)

        link_call = next(call for call in execute_values.calls if call["fetch"])
        assert link_call["rows"] == [(7, item.listing_id, False) for item in listings]
        assert "notified" in link_call["sql"]
        assert all(row[2] is False for row in link_call["rows"])

    def test_listings_are_upserted_before_being_linked(self, execute_values):
        """Ordre imposé par la clé étrangère : `search_listings` référence
        `listings`. Et le `ON CONFLICT DO NOTHING` est ce qui rend le scrape
        idempotent."""
        conn = RecordingConnection()
        repo = bind_repository(ListingRepository, conn)

        repo.save_and_link([make_listing()], search_id=1)

        assert len(execute_values.calls) == 2
        insert_call, link_call = execute_values.calls
        assert "INSERT INTO listings" in insert_call["sql"]
        assert "ON CONFLICT (listing_id) DO NOTHING" in insert_call["sql"]
        assert insert_call["fetch"] is False
        assert "INSERT INTO search_listings" in link_call["sql"]
        assert "RETURNING listing_id" in link_call["sql"]
        assert link_call["fetch"] is True

    def test_the_returned_split_follows_what_postgres_actually_inserted(self, execute_values):
        """`new_for_search` vient du `RETURNING`, pas d'un calcul côté Python :
        c'est ce qui garantit qu'une annonce déjà liée par un scrape concurrent
        n'est pas notifiée deux fois."""
        conn = RecordingConnection()
        repo = bind_repository(ListingRepository, conn)
        fresh, known = make_listing(), make_listing()
        execute_values.newly_linked = {fresh.listing_id}

        new_for_search, already_linked = repo.save_and_link([fresh, known], search_id=1)

        assert [item.listing_id for item in new_for_search] == [fresh.listing_id]
        assert [item.listing_id for item in already_linked] == [known.listing_id]
        assert conn.commits == 1

    def test_batches_are_chunked(self, execute_values):
        """`page_size=100` borne la taille de la requête envoyée : sans lui, un
        lot de 500 annonces produit une seule requête énorme."""
        conn = RecordingConnection()
        repo = bind_repository(ListingRepository, conn)

        repo.save_and_link([make_listing()], search_id=1)

        assert [call["page_size"] for call in execute_values.calls] == [100, 100]

    def test_surrogates_are_cleaned_out_of_the_text_columns(self, execute_values):
        """Le nettoyage s'applique bien aux valeurs envoyées, sinon l'INSERT
        entier lève et tout le lot est perdu."""
        conn = RecordingConnection()
        repo = bind_repository(ListingRepository, conn)
        listing = make_listing(title="Studio \ud800", agency="Agence \ud800", city="Paris")

        repo.save_and_link([listing], search_id=1)

        row = execute_values.calls[0]["rows"][0]
        assert "\ud800" not in "".join(value for value in row if isinstance(value, str))

    def test_a_failure_rolls_back_and_re_raises(self, execute_values, monkeypatch):
        """Le lot doit être tout-ou-rien : sans le rollback, la connexion
        repartirait au pool en transaction avortée et empoisonnerait la requête
        suivante. Et l'exception doit remonter pour que le scrape se journalise
        en erreur au lieu de conclure « 0 nouvelle annonce ».
        """
        conn = RecordingConnection()
        repo = bind_repository(ListingRepository, conn)
        boom = RuntimeError("insert refusé")

        def explode(*args, **kwargs):
            raise boom

        monkeypatch.setattr("repositories.listing_repo.execute_values", explode)

        with pytest.raises(RuntimeError, match="insert refusé"):
            repo.save_and_link([make_listing()], search_id=1)

        assert conn.rollbacks == 1
        assert conn.commits == 0


class TestMarkListingsNotified:
    def test_an_empty_list_returns_without_any_query(self):
        """Appelé après chaque tour de notifications, y compris quand aucune n'a
        abouti : un UPDATE avec `ANY('{}')` ne servirait à rien."""
        conn = RecordingConnection()
        repo = bind_repository(ListingRepository, conn)

        assert repo.mark_listings_notified(1, []) is None
        assert conn.executed == []
        assert conn.commits == 0

    def test_ids_are_passed_as_a_single_array_parameter(self):
        """`= ANY(%s)` avec la liste en paramètre unique : pas de `IN (...)`
        construit à la main, donc pas de nombre de placeholders à synchroniser."""
        conn = RecordingConnection()
        repo = bind_repository(ListingRepository, conn)

        repo.mark_listings_notified(7, ["sl_1", "sl_2"])

        (sql, params), = conn.executed
        assert "UPDATE search_listings" in sql
        assert "notified = TRUE" in sql
        assert "ANY(%s)" in sql
        assert params == (7, ["sl_1", "sl_2"])
        assert conn.commits == 1

    def test_the_update_is_scoped_to_the_search(self):
        """Un `listing_id` est partagé entre recherches : sans le `search_id`
        dans le WHERE, notifier une annonce pour Alice la marquerait notifiée
        pour Bob, qui ne la recevrait jamais."""
        conn = RecordingConnection()
        repo = bind_repository(ListingRepository, conn)

        repo.mark_listings_notified(7, ["sl_1"])

        sql, params = conn.executed[0]
        assert "search_id = %s" in sql
        assert params[0] == 7


# ---------------------------------------------------------------------------
# Parité liste / comptage — sinon la pagination ment
# ---------------------------------------------------------------------------

def where_body(sql: str, anchor: str) -> str:
    """Le corps du WHERE, ancre et tri exclus.

    `rsplit` et non `split` : `get_all_listings` contient un sous-select avec son
    propre `WHERE`, c'est le dernier qui porte les filtres.
    """
    body = sql.rsplit(anchor, 1)[1]
    return body.split(" ORDER BY ", 1)[0].strip()


PARITY_FILTER_SETS = [
    pytest.param({}, None, id="aucun-filtre"),
    pytest.param({"q": "loft"}, None, id="q"),
    pytest.param({"price_min": 800, "price_max": 1500}, None, id="fourchette-de-prix"),
    pytest.param({"is_private": False, "is_new": False}, None, id="booleens-faux"),
    pytest.param({"city": "Paris", "zip_code": "75013", "epc": "C"}, None, id="localisation"),
    pytest.param({}, ["Foncia", "Nexity"], id="blacklist-seule"),
    pytest.param(
        {"q": "loft", "surface_min": 40, "rooms_max": 4, "date_min": "2026-01-01"},
        ["Foncia"],
        id="filtres-et-blacklist",
    ),
]


class TestListCountParity:
    @pytest.mark.parametrize(("filters", "blacklist"), PARITY_FILTER_SETS)
    def test_search_listing_and_count_share_the_same_where(self, filters, blacklist):
        """Les deux méthodes reconstruisent leur WHERE séparément. Si elles
        divergent, le compteur total ne correspond plus aux lignes affichées :
        pagination fantôme (dernière page vide) ou annonces inatteignables.
        C'est la duplication la plus dangereuse du repository.
        """
        list_conn = RecordingConnection(results=[[]])
        count_conn = RecordingConnection(results=[{"cnt": 0}])

        bind_repository(ListingRepository, list_conn).get_listings_for_search(
            7, limit=25, offset=50, blacklisted_agencies=blacklist, filters=filters,
        )
        bind_repository(ListingRepository, count_conn).count_listings_for_search(
            7, blacklisted_agencies=blacklist, filters=filters,
        )

        anchor = "WHERE sl.search_id = %s"
        list_sql, list_params = list_conn.executed[0]
        count_sql, count_params = count_conn.executed[0]

        assert where_body(list_sql, anchor) == where_body(count_sql, anchor)
        # La liste porte en plus LIMIT/OFFSET : à cela près, mêmes paramètres.
        assert list_params[:-2] == count_params
        assert list_params[-2:] == [25, 50]

    @pytest.mark.parametrize(
        ("search_term", "source_filter"),
        [("", ""), ("loft", ""), ("", "seloger"), ("loft", "laforet")],
    )
    def test_admin_listing_and_count_share_the_same_where(self, search_term, source_filter):
        """Même duplication côté admin (`get_all_listings` / `count_all_listings`),
        avec une conséquence identique sur la pagination."""
        list_conn = RecordingConnection(results=[[]])
        count_conn = RecordingConnection(results=[{"cnt": 0}])

        bind_repository(ListingRepository, list_conn).get_all_listings(
            limit=25, offset=50, search_term=search_term, source_filter=source_filter,
        )
        bind_repository(ListingRepository, count_conn).count_all_listings(
            search_term=search_term, source_filter=source_filter,
        )

        list_sql, list_params = list_conn.executed[0]
        count_sql, count_params = count_conn.executed[0]

        if search_term or source_filter:
            assert where_body(list_sql, " WHERE ") == where_body(count_sql, " WHERE ")
        else:
            # `get_all_listings` porte un sous-select qui a son propre WHERE
            # (`SELECT COUNT(*) FROM search_listings WHERE listing_id = l.listing_id`) :
            # sans filtre, c'est le WHERE de premier niveau — celui qui suit
            # `FROM listings l` — qui doit être absent.
            assert " WHERE " not in list_sql.rsplit("FROM listings l", 1)[1]
            assert " WHERE " not in count_sql.rsplit("FROM listings l", 1)[1]
        assert list_params[:-2] == count_params
        assert list_params[-2:] == [25, 50]

    @pytest.mark.parametrize("payload", PAYLOADS)
    def test_the_admin_listing_search_term_stays_a_parameter(self, payload):
        """`get_all_listings` construit son WHERE à part de
        `_build_filter_clauses` : l'invariant doit être vérifié ici aussi."""
        conn = RecordingConnection(results=[[]])

        bind_repository(ListingRepository, conn).get_all_listings(search_term=payload, source_filter=payload)

        sql, params = conn.executed[0]
        assert payload not in sql
        assert f"%{payload}%" in params
        assert payload in params
        assert count_placeholders(sql) == len(params)

    def test_the_blacklist_argument_and_the_filter_key_are_interchangeable(self):
        """Les routes passent la blacklist tantôt en argument nommé, tantôt dans
        `filters` : les deux voies doivent produire la même requête."""
        via_arg = RecordingConnection(results=[[]])
        via_filters = RecordingConnection(results=[[]])

        bind_repository(ListingRepository, via_arg).get_listings_for_search(
            7, blacklisted_agencies=["Foncia"],
        )
        bind_repository(ListingRepository, via_filters).get_listings_for_search(
            7, filters={"blacklisted_agencies": ["Foncia"]},
        )

        assert via_arg.executed == via_filters.executed

    def test_the_caller_filters_dict_is_not_mutated(self):
        """La blacklist est injectée dans une *copie* : sinon l'appelant
        (une route qui réutilise son dict pour la liste puis le comptage)
        accumulerait des clés."""
        conn = RecordingConnection(results=[[]])
        filters = {"city": "Paris"}

        bind_repository(ListingRepository, conn).get_listings_for_search(
            7, blacklisted_agencies=["Foncia"], filters=filters,
        )

        assert filters == {"city": "Paris"}


# ---------------------------------------------------------------------------
# get_filter_options — la seule f-string sur un nom de colonne
# ---------------------------------------------------------------------------

class TestGetFilterOptions:
    EXPECTED_COLUMNS = ["city", "district", "zip_code", "property_type", "agency", "epc", "ges"]

    def _run(self):
        results = [[{"value": f"v{i}"}] for i in range(7)]
        results.append({"a": 3, "b": 0, "c": 1, "d": 0})
        conn = RecordingConnection(results=results)
        options = bind_repository(ListingRepository, conn).get_filter_options(7)
        return conn, options

    def test_interpolated_columns_come_from_a_closed_literal_list(self):
        """🔒 `get_filter_options` interpole `col` directement dans le SQL. C'est
        sûr *parce que* `col` itère sur un tuple littéral du code — mais un futur
        « et si on prenait les colonnes depuis request.args » en ferait une
        injection immédiate. Ce test fige la liste fermée.
        """
        conn, _ = self._run()

        interpolated = set()
        for sql in conn.sql[:7]:
            for line in sql.splitlines():
                # La condition de jointure cite `l.listing_id`, qui est écrit en
                # dur dans la requête et non interpolé : seules les autres
                # occurrences de `l.<col>` viennent de la boucle sur les colonnes.
                if "JOIN" in line:
                    continue
                # Le lookbehind écarte les colonnes de l'alias `sl.` (search_listings).
                interpolated.update(re.findall(r"(?<![\w.])l\.(\w+)", line))

        assert interpolated == set(self.EXPECTED_COLUMNS)

    def test_one_query_per_column_plus_one_for_the_boolean_facets(self):
        conn, options = self._run()

        assert len(conn.executed) == len(self.EXPECTED_COLUMNS) + 1
        assert [re.search(r"SELECT DISTINCT l\.(\w+)", sql).group(1) for sql in conn.sql[:7]] == self.EXPECTED_COLUMNS

    def test_only_the_search_id_is_parameterized(self):
        """Aucun nom de colonne ne transite par `params` : le seul paramètre est
        le `search_id`, sur chacune des 8 requêtes."""
        conn, _ = self._run()

        assert all(params == (7,) for _, params in conn.executed)

    def test_boolean_facets_are_derived_from_the_counts(self):
        """Les facettes pilotent l'affichage des cases à cocher : un comptage nul
        doit masquer la case, pas la proposer sur un résultat vide."""
        _, options = self._run()

        assert options["has_private"] is True
        assert options["has_non_private"] is False
        assert options["has_new"] is True
        assert options["has_not_new"] is False

    def test_every_column_gets_its_own_option_list(self):
        _, options = self._run()

        for column in self.EXPECTED_COLUMNS:
            assert options[column] == [f"v{self.EXPECTED_COLUMNS.index(column)}"]
