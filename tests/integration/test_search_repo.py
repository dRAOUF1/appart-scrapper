"""SearchRepository contre un vrai Postgres.

Deux sujets que seul le moteur peut trancher :

1. **La compatibilité des critères.** Les recherches créées avant
   l'unification du vocabulaire sont stockées telles quelles en JSONB et ne
   sont PAS migrées : `_load_criteria` les convertit au canonique à chaque
   lecture. On écrit donc l'ancien vocabulaire dans une vraie colonne JSONB et
   on vérifie ce qui ressort — un test unitaire ne prouverait que
   `normalize_criteria`, jamais l'aller-retour à travers JSONB.
2. **Le périmètre des UPDATE.** `update_search` filtre sur `id AND user_id`,
   mais quatre autres méthodes filtrent sur `id` SEUL. C'est un contrat de
   sécurité qui ne tient que par la discipline des routes appelantes : les
   tests ci-dessous le figent explicitement, dans les deux sens.
"""

from __future__ import annotations

import json

import pytest

from tests.helpers.factories import make_criteria, make_listing
from tests.integration.conftest import insert_search

# ---------------------------------------------------------------------------
# Aller-retour des critères
# ---------------------------------------------------------------------------


class TestCriteriaRoundTrip:
    def test_canonical_criteria_survive_the_jsonb_round_trip(self, storage, user):
        criteria = make_criteria(
            locations=[
                {"kind": "city", "city": "Paris", "postalCode": "75013", "inseeCode": "75113"},
                {"kind": "department", "code": "33", "name": "Gironde"},
            ],
            rooms=[2, 3],
            bedrooms=[1],
            priceMin=800,
            priceMax=1500,
            surfaceMin=35,
            surfaceMax=90,
            sourceOverrides={"seloger": {"placeIds": ["AD08FR31096"]}},
        )

        created = storage.searches.create_search(
            user["id"], "Paris 13e", "topic-x", "seloger", criteria, 10,
        )
        fetched = storage.searches.get_search(created["id"])

        assert fetched["criteria"] == criteria
        assert fetched["label"] == "Paris 13e"
        assert fetched["ntfy_topic"] == "topic-x"
        assert fetched["scrape_interval"] == 10
        assert fetched["is_active"] is True
        assert fetched["user_id"] == user["id"]
        assert fetched["created_at"] is not None

    def test_empty_criteria_round_trip_as_an_empty_dict(self, storage, user):
        created = storage.searches.create_search(user["id"], "Vide", "topic", "seloger", None, 5)

        assert storage.searches.get_search(created["id"])["criteria"] == {}

    @pytest.mark.parametrize(
        ("legacy", "expected"),
        [
            pytest.param(
                {
                    "placeIds": ["AD08FR31096"],
                    "city": "Paris", "postalCode": "75013",
                    "distributionTypes": ["Rent"],
                    "estateTypes": ["Apartment"],
                    "spaceMin": 40,
                    "rooms": ["2", "3"],
                },
                {
                    "locations": [{"kind": "city", "city": "Paris", "postalCode": "75013"}],
                    "transaction": "rent",
                    "propertyTypes": ["apartment"],
                    "surfaceMin": 40,
                    "rooms": [2, 3],
                    "sourceOverrides": {"seloger": {"placeIds": ["AD08FR31096"]}},
                },
                id="location-seloger-complete",
            ),
            pytest.param(
                {"distributionTypes": ["Sale"], "estateTypes": ["House", "Apartment"]},
                {"transaction": "buy", "propertyTypes": ["house", "apartment"]},
                id="achat-plusieurs-types",
            ),
            pytest.param(
                {"city": "Poitiers", "postalCode": "86000", "spaceMax": 120, "priceMax": 900},
                {
                    "locations": [{"kind": "city", "city": "Poitiers", "postalCode": "86000"}],
                    "surfaceMax": 120,
                    "priceMax": 900,
                },
                id="couple-a-plat-city-postalcode",
            ),
            pytest.param(
                {"estateTypes": ["Chateau"], "distributionTypes": ["Troc"]},
                {},
                id="valeurs-inconnues-ecartees",
            ),
        ],
    )
    def test_criteria_stored_in_the_old_vocabulary_are_read_back_canonical(
        self, storage, user, sql, legacy, expected,
    ):
        """L'ancien vocabulaire est écrit DIRECTEMENT en base (SQL brut) : c'est
        exactement la forme des lignes encore en production, que `create_search`
        ne sait plus produire. Toutes les lectures du repo doivent en sortir du
        canonique."""
        search_id = sql.one(
            "INSERT INTO searches (user_id, label, ntfy_topic, source, criteria, sources)"
            " VALUES (%s, 'Ancienne', 'topic', 'seloger', %s, '[\"seloger\"]') RETURNING id",
            (user["id"], json.dumps(legacy)),
        )

        assert storage.searches.get_search(search_id)["criteria"] == expected
        # Les quatre chemins de lecture passent par le même `_load_criteria`.
        assert storage.searches.get_user_searches(user["id"])[0]["criteria"] == expected
        assert storage.searches.get_all_searches()[0]["criteria"] == expected
        assert storage.searches.get_search_detail(search_id)["criteria"] == expected

    def test_criteria_are_not_rewritten_in_the_database_by_a_read(self, storage, user, sql):
        """La normalisation est à la lecture SEULEMENT : la ligne reste dans
        l'ancien vocabulaire, et le restera jusqu'à une réédition. C'est
        volontaire (pas de migration de données), mais ça veut dire que toute
        nouvelle lecture directe en SQL doit s'attendre à l'ancien format."""
        legacy = {"distributionTypes": ["Rent"], "estateTypes": ["Apartment"]}
        search_id = sql.one(
            "INSERT INTO searches (user_id, label, ntfy_topic, source, criteria)"
            " VALUES (%s, 'Ancienne', 'topic', 'seloger', %s) RETURNING id",
            (user["id"], json.dumps(legacy)),
        )

        storage.searches.get_search(search_id)

        assert sql.one("SELECT criteria FROM searches WHERE id = %s", (search_id,)) == legacy

    def test_a_corrupt_criteria_column_degrades_to_an_empty_dict(self, storage, user, sql):
        """JSONB refuse le JSON invalide, mais une chaîne JSON *scalaire* passe
        (`'"pas un objet"'::jsonb` est valide). `normalize_criteria` ne doit pas
        casser la lecture pour autant : la recherche remonte sans critères."""
        search_id = sql.one(
            "INSERT INTO searches (user_id, label, ntfy_topic, source, criteria)"
            " VALUES (%s, 'Corrompue', 'topic', 'seloger', %s) RETURNING id",
            (user["id"], '"pas un objet"'),
        )

        assert storage.searches.get_search(search_id)["criteria"] == {}


# ---------------------------------------------------------------------------
# sources JSONB
# ---------------------------------------------------------------------------

class TestSources:
    def test_sources_round_trip_as_a_python_list(self, storage, user):
        created = storage.searches.create_search(
            user["id"], "Multi", "topic", "seloger", {}, 5, sources=["seloger", "laforet"],
        )

        fetched = storage.searches.get_search(created["id"])

        assert fetched["sources"] == ["seloger", "laforet"]
        assert fetched["source"] == "seloger"

    def test_sources_defaults_to_the_single_source_when_omitted(self, storage, user):
        created = storage.searches.create_search(user["id"], "Mono", "topic", "laforet", {}, 5)

        assert storage.searches.get_search(created["id"])["sources"] == ["laforet"]

    def test_a_null_sources_column_falls_back_to_source(self, storage, user, sql):
        """Le backfill DDL couvre les lignes existantes, mais `_normalize_sources`
        reste défensif : une ligne à NULL (créée hors repo) doit quand même être
        scrapée, sinon elle disparaît du pipeline en silence."""
        search_id = sql.one(
            "INSERT INTO searches (user_id, label, ntfy_topic, source, sources)"
            " VALUES (%s, 'Sans sources', 'topic', 'laforet', NULL) RETURNING id",
            (user["id"],),
        )

        assert storage.searches.get_search(search_id)["sources"] == ["laforet"]

    def test_an_empty_sources_array_also_falls_back(self, storage, user, sql):
        search_id = sql.one(
            "INSERT INTO searches (user_id, label, ntfy_topic, source, sources)"
            " VALUES (%s, 'Vide', 'topic', 'seloger', '[]') RETURNING id",
            (user["id"],),
        )

        assert storage.searches.get_search(search_id)["sources"] == ["seloger"]


# ---------------------------------------------------------------------------
# update_search : filtré sur (id, user_id)
# ---------------------------------------------------------------------------

class TestUpdateSearch:
    def test_updates_every_field_and_leaves_the_rest_alone(self, storage, user):
        search = insert_search(storage, user["id"], "Avant", scrape_interval=5)

        assert storage.searches.update_search(
            search["id"], user["id"], label="Après", ntfy_topic="autre-topic",
            criteria=make_criteria(priceMax=700), scrape_interval=30, is_active=False,
            sources=["laforet", "seloger"],
        ) is True

        fetched = storage.searches.get_search(search["id"])
        assert fetched["label"] == "Après"
        assert fetched["ntfy_topic"] == "autre-topic"
        assert fetched["criteria"] == make_criteria(priceMax=700)
        assert fetched["scrape_interval"] == 30
        assert fetched["is_active"] is False
        assert fetched["sources"] == ["laforet", "seloger"]
        assert fetched["source"] == "laforet", "`source` suit la première des `sources`"

    def test_updating_a_single_field_touches_nothing_else(self, storage, user):
        search = insert_search(storage, user["id"], "Intacte", scrape_interval=7)
        before = storage.searches.get_search(search["id"])

        assert storage.searches.update_search(search["id"], user["id"], label="Renommée") is True

        after = storage.searches.get_search(search["id"])
        assert after["label"] == "Renommée"
        for column in ("ntfy_topic", "source", "sources", "criteria", "scrape_interval", "is_active"):
            assert after[column] == before[column]

    def test_without_any_field_it_does_not_even_query(self, storage, user):
        search = insert_search(storage, user["id"], "Rien à faire")

        assert storage.searches.update_search(search["id"], user["id"]) is False

    def test_another_users_search_is_not_modified(self, storage, user, other_search):
        """Le `WHERE id = %s AND user_id = %s` est la vraie barrière : le
        rowcount reste à zéro et la ligne d'en face est intacte."""
        assert storage.searches.update_search(
            other_search["id"], user["id"], label="PIRATÉ", is_active=False,
        ) is False

        untouched = storage.searches.get_search(other_search["id"])
        assert untouched["label"] == "Bordeaux"
        assert untouched["is_active"] is True

    def test_an_unknown_search_id_returns_false(self, storage, user):
        assert storage.searches.update_search(999_999, user["id"], label="Fantôme") is False


# ---------------------------------------------------------------------------
# Les UPDATE filtrés sur `id` SEUL — contrat de sécurité fragile
# ---------------------------------------------------------------------------

class TestUpdatesWithoutOwnershipCheck:
    """# BUG (contrat fragile) : ces quatre méthodes ne prennent pas de
    `user_id` et filtrent sur `WHERE id = %s` uniquement. Rien, au niveau du
    repository, n'empêche de modifier la recherche d'un autre utilisateur : la
    seule protection est que chaque route appelante vérifie la propriété AVANT
    d'appeler. Une nouvelle route qui l'oublierait ouvrirait une IDOR sans
    qu'aucun test de repo ne bronche — d'où ces tests, qui figent le
    comportement ACTUEL plutôt que le comportement souhaitable.
    """

    @pytest.mark.parametrize(
        ("method", "args", "column", "expected"),
        [
            pytest.param(
                "update_search_criteria", ({"priceMax": 1},), "criteria",
                {"priceMax": 1}, id="update_search_criteria",
            ),
            pytest.param("update_scrape_interval", (42,), "scrape_interval", 42, id="update_scrape_interval"),
            pytest.param(
                "update_blacklisted_agencies", (["Foncia"],), "blacklisted_agencies",
                ["Foncia"], id="update_blacklisted_agencies",
            ),
            pytest.param(
                "update_blacklist_mode", ("no_notify",), "blacklist_mode",
                "no_notify", id="update_blacklist_mode",
            ),
        ],
    )
    def test_they_happily_modify_a_search_belonging_to_someone_else(
        self, storage, user, other_search, method, args, column, expected,
    ):
        # `user` existe pour matérialiser le second utilisateur : la recherche
        # visée appartient à `bob`, l'appel ne mentionne aucun propriétaire.
        assert user["username"] == "alice"

        assert getattr(storage.searches, method)(other_search["id"], *args) is True

        assert storage.searches.get_search(other_search["id"])[column] == expected

    @pytest.mark.parametrize(
        ("method", "args"),
        [
            ("update_search_criteria", ({"priceMax": 1},)),
            ("update_scrape_interval", (42,)),
            ("update_last_scraped", ()),
            ("update_blacklisted_agencies", (["Foncia"],)),
            ("update_blacklist_mode", ("no_notify",)),
        ],
    )
    def test_an_unknown_id_returns_false(self, storage, method, args):
        assert getattr(storage.searches, method)(999_999, *args) is False

    def test_update_blacklist_mode_refuses_an_unknown_mode_before_touching_the_database(
        self, storage, search,
    ):
        assert storage.searches.update_blacklist_mode(search["id"], "supprime_tout") is False

        assert storage.searches.get_search(search["id"])["blacklist_mode"] == "exclude"

    @pytest.mark.parametrize(
        "agencies",
        [
            pytest.param([], id="liste-vide"),
            pytest.param(["Foncia"], id="une-agence"),
            pytest.param(["Foncia", "Orpi", "Century 21"], id="plusieurs"),
            pytest.param(["Agence, avec virgule", 'Guillemet "double"', "Accolade {littérale}"], id="caracteres-array"),
        ],
    )
    def test_blacklisted_agencies_round_trip_through_the_text_array(self, storage, search, agencies):
        """`TEXT[]` : psycopg2 sérialise la liste en littéral de tableau. Les
        virgules, guillemets et accolades y sont significatifs — c'est
        précisément ce que seul le moteur peut valider."""
        assert storage.searches.update_blacklisted_agencies(search["id"], agencies) is True

        assert storage.searches.get_search(search["id"])["blacklisted_agencies"] == agencies

    def test_update_last_scraped_writes_the_server_clock(self, storage, search):
        assert storage.searches.get_search(search["id"])["last_scraped"] is None

        assert storage.searches.update_last_scraped(search["id"]) is True

        assert storage.searches.get_search(search["id"])["last_scraped"] is not None


# ---------------------------------------------------------------------------
# toggle_search_active
# ---------------------------------------------------------------------------

class TestToggleActive:
    def test_toggle_returns_the_new_value_from_the_returning_clause(self, storage, search):
        assert storage.searches.toggle_search_active(search["id"]) is False
        assert storage.searches.get_search(search["id"])["is_active"] is False

        assert storage.searches.toggle_search_active(search["id"]) is True
        assert storage.searches.get_search(search["id"])["is_active"] is True

    def test_toggle_on_an_unknown_search_returns_none(self, storage):
        """`RETURNING` ne renvoie aucune ligne : la méthode distingue « désormais
        inactive » (False) de « recherche inexistante » (None). Les deux valeurs
        étant fausses en Python, l'appelant doit tester `is None`."""
        assert storage.searches.toggle_search_active(999_999) is None


# ---------------------------------------------------------------------------
# Listes et détail
# ---------------------------------------------------------------------------

class TestListings:
    def test_get_user_searches_is_scoped_ordered_and_counted(self, storage, user, other_user, sql):
        old = insert_search(storage, user["id"], "Ancienne")
        recent = insert_search(storage, user["id"], "Récente")
        insert_search(storage, other_user["id"], "Chez bob")
        storage.listings.save_and_link(
            [make_listing(listing_id="a"), make_listing(listing_id="b")], old["id"],
        )
        sql.exec("UPDATE searches SET created_at = NOW() - INTERVAL '1 day' WHERE id = %s", (old["id"],))

        rows = storage.searches.get_user_searches(user["id"])

        assert [r["label"] for r in rows] == ["Récente", "Ancienne"]
        assert {r["label"]: r["listing_count"] for r in rows} == {"Récente": 0, "Ancienne": 2}
        assert all("Chez bob" != r["label"] for r in rows)
        assert rows[0]["id"] == recent["id"]

    def test_get_user_searches_of_an_unknown_user_is_empty(self, storage):
        assert storage.searches.get_user_searches(999_999) == []

    def test_get_all_searches_joins_the_owner_and_counts_links(self, storage, user, other_user):
        mine = insert_search(storage, user["id"], "À moi", source="seloger")
        insert_search(storage, other_user["id"], "À bob", source="laforet")
        storage.listings.save_and_link([make_listing(listing_id="x")], mine["id"])

        rows = {r["label"]: r for r in storage.searches.get_all_searches()}

        assert set(rows) == {"À moi", "À bob"}
        assert rows["À moi"]["username"] == "alice"
        assert rows["À moi"]["user_id"] == user["id"]
        assert rows["À moi"]["listing_count"] == 1
        assert rows["À bob"]["listing_count"] == 0, "LEFT JOIN : zéro, pas d'absence de ligne"

    @pytest.mark.parametrize(
        ("user_filter", "source_filter", "expected"),
        [
            pytest.param("", "", {"À moi", "À bob"}, id="aucun-filtre"),
            pytest.param("ali", "", {"À moi"}, id="ilike-fragment"),
            pytest.param("ALI", "", {"À moi"}, id="ilike-insensible-a-la-casse"),
            # Le filtre est interpolé en `%<valeur>%` puis passé en paramètre :
            # pas d'injection SQL, mais les jokers LIKE de la valeur ne sont pas
            # échappés. Un « % » saisi par l'admin élargit donc la recherche au
            # lieu de la restreindre, au lieu d'être cherché littéralement.
            pytest.param("%", "", {"À moi", "À bob"}, id="pourcent-agit-comme-joker-non-echappe"),
            pytest.param("", "laforet", {"À bob"}, id="source-exacte"),
            pytest.param("", "LAFORET", set(), id="source-sensible-a-la-casse"),
            pytest.param("bob", "laforet", {"À bob"}, id="les-deux-filtres"),
            pytest.param("bob", "seloger", set(), id="filtres-contradictoires"),
        ],
    )
    def test_get_all_searches_filters(self, storage, user, other_user, user_filter, source_filter, expected):
        insert_search(storage, user["id"], "À moi", source="seloger")
        insert_search(storage, other_user["id"], "À bob", source="laforet")

        rows = storage.searches.get_all_searches(user_filter=user_filter, source_filter=source_filter)

        assert {r["label"] for r in rows} == expected

    def test_search_detail_carries_the_owner_recent_listings_and_total(self, storage, user):
        search = insert_search(storage, user["id"], "Détaillée")
        storage.listings.save_and_link(
            [make_listing(listing_id=f"d{i:02d}") for i in range(12)], search["id"],
        )

        detail = storage.searches.get_search_detail(search["id"])

        assert detail["username"] == "alice"
        assert detail["label"] == "Détaillée"
        assert detail["total_listings"] == 12
        assert len(detail["recent_listings"]) == 10
        assert detail["recent_listings"][0]["found_at"] is not None
        assert detail["recent_listings"][0]["listing_id"].startswith("d")

    def test_search_detail_of_an_unknown_search_is_none(self, storage):
        assert storage.searches.get_search_detail(999_999) is None

    def test_get_search_of_an_unknown_search_is_none(self, storage):
        assert storage.searches.get_search(999_999) is None


# ---------------------------------------------------------------------------
# delete_search
# ---------------------------------------------------------------------------

class TestDeleteSearch:
    def test_deleting_a_search_cascades_to_its_links_only(self, storage, user, sql, log_dirs):
        """`ON DELETE CASCADE` sur `search_listings`, mais PAS sur `listings` :
        l'annonce reste en base (elle peut appartenir à d'autres recherches) et
        devient orpheline. Les logs de scrape, eux, sont des fichiers : le repo
        les supprime explicitement."""
        search = insert_search(storage, user["id"], "À supprimer")
        keeper = insert_search(storage, user["id"], "À garder")
        storage.listings.save_and_link([make_listing(listing_id="partagee")], search["id"])
        storage.listings.save_and_link([make_listing(listing_id="partagee")], keeper["id"])
        storage.scrape_logs.create_scrape_log(search["id"], "success", 1, 1)
        assert (log_dirs / "scrape_logs" / f"search_{search['id']}").exists()

        assert storage.searches.delete_search(search["id"]) is True

        assert storage.searches.get_search(search["id"]) is None
        assert sql.one("SELECT COUNT(*) FROM search_listings WHERE search_id = %s", (search["id"],)) == 0
        assert storage.listings.get_listing_detail("partagee") is not None
        assert sql.one("SELECT COUNT(*) FROM search_listings WHERE search_id = %s", (keeper["id"],)) == 1
        assert not (log_dirs / "scrape_logs" / f"search_{search['id']}").exists()

    def test_deleting_an_unknown_search_returns_false(self, storage):
        assert storage.searches.delete_search(999_999) is False


# ---------------------------------------------------------------------------
# Notifications par recherche (issue #10)
# ---------------------------------------------------------------------------

class TestNotifyEnabled:
    def test_a_creation_without_the_flag_notifies_by_default(self, storage, user):
        """Rétrocompat : `create_search` sans mention du flag crée une recherche
        qui notifie — la colonne porte elle-même DEFAULT TRUE."""
        created = insert_search(storage, user["id"], "Par défaut")
        fetched = storage.searches.get_search(created["id"])

        assert fetched["notify_enabled"] is True

    @pytest.mark.parametrize("read", ["get_search", "get_user_searches", "get_all_searches", "get_search_detail"])
    def test_an_explicit_false_survives_every_read_path(self, storage, user, read):
        """Le flag est dans TOUS les SELECT explicites : chaque lecture doit le
        restituer — sinon l'écran et le pipeline divergent silencieusement."""
        muted = insert_search(storage, user["id"], "Silencieuse", notify_enabled=False)

        if read == "get_user_searches":
            rows = storage.searches.get_user_searches(user["id"])
            value = {r["label"]: r["notify_enabled"] for r in rows}["Silencieuse"]
        elif read == "get_all_searches":
            rows = storage.searches.get_all_searches()
            value = {r["label"]: r["notify_enabled"] for r in rows}["Silencieuse"]
        elif read == "get_search_detail":
            value = storage.searches.get_search_detail(muted["id"])["notify_enabled"]
        else:
            value = storage.searches.get_search(muted["id"])["notify_enabled"]

        assert value is False

    def test_update_search_can_mute_and_unmute(self, storage, user):
        search = insert_search(storage, user["id"], "Paris 13e")

        assert storage.searches.update_search(
            search["id"], user["id"], notify_enabled=False,
        ) is True
        assert storage.searches.get_search(search["id"])["notify_enabled"] is False

        assert storage.searches.update_search(
            search["id"], user["id"], notify_enabled=True,
        ) is True
        assert storage.searches.get_search(search["id"])["notify_enabled"] is True

    def test_toggle_notifications_flips_the_value_and_returns_it(self, storage, user):
        search = insert_search(storage, user["id"], "À basculer")

        assert storage.searches.toggle_search_notifications(search["id"]) is False
        assert storage.searches.get_search(search["id"])["notify_enabled"] is False
        assert storage.searches.toggle_search_notifications(search["id"]) is True
        assert storage.searches.get_search(search["id"])["notify_enabled"] is True

    def test_toggling_notifications_of_an_unknown_search_is_none(self, storage):
        assert storage.searches.toggle_search_notifications(999_999) is None

    def test_toggling_does_not_touch_scraping_activity(self, storage, user):
        """Le flag #10 pilote l'envoi ntfy uniquement : `is_active` doit sortir
        intact d'une bascule de notifications."""
        search = insert_search(storage, user["id"], "Active")

        storage.searches.toggle_search_notifications(search["id"])

        fetched = storage.searches.get_search(search["id"])
        assert fetched["is_active"] is True
        assert fetched["notify_enabled"] is False
